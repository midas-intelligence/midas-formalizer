"""Source-only identifier-fragment experiment; production search is unchanged.

Uses an existing production SQLite index and frozen query/target TSV files.
No Lean execution, optional packages, network, or JSON processing. Output paths
must be new. Timings exclude shared scope preparation and checked lookup.
"""
import argparse
import csv
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from statistics import median
from time import perf_counter

from midas.source import parse_identifier
from midas.tools import bm25

EXTRA = 32
VARIANTS = ('baseline', 'merge-only', 'rank-only', 'fragments')


def read_tsv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream, delimiter='\t'))


def tsv(path, header, rows):
    with path.open('w') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(header)
        writer.writerows(rows)


def digest(path):
    value = sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def hints(query, *, compound_only=False):
    """Compound names take precedence over camelCase context words.

    A fragment is a contiguous sequence of whole identifier components, not an
    arbitrary character substring. Preserve raw spelling for boundary checks.
    """
    compound, camel = [], []
    for word in query.split():
        word = word.strip('`,;:()[]{}"').removeprefix('_root_.')
        if parse_identifier(word) is None or len(bm25.token_text(word).split()) < 2:
            continue
        if '_' in word or '.' in word:
            compound.append(word)
        elif re.search(r'[a-z][A-Z]', word):
            camel.append(word)
    return tuple(dict.fromkeys(compound if compound_only else compound or camel))


def matcher(fragments):
    patterns = [re.compile(r'(?<![^\W_])' + re.escape(word.casefold()) + r'(?![^\W_])')
                for word in fragments]
    return lambda name: any(pattern.search(name.casefold()) for pattern in patterns)


def expression(query):
    return ' OR '.join('"' + word.replace('"', '""') + '"'
                       for word in dict.fromkeys(bm25.token_text(query).split()))


def scope(db, prefixes):
    db.execute('DROP TABLE IF EXISTS temp.scope_docs')
    db.execute('CREATE TEMP TABLE scope_docs(id INTEGER PRIMARY KEY, preferred INTEGER NOT NULL)')
    for prefix in prefixes:
        db.execute('''INSERT INTO scope_docs
            SELECT d.id,f.path=? FROM files f JOIN documents d ON d.file=f.id
            WHERE f.path=? OR (f.path>=? AND f.path<?)
            ON CONFLICT(id) DO UPDATE SET preferred=max(preferred,excluded.preferred)''',
            (prefix, prefix, prefix+'/', prefix+'0'))


def retrieve(db, query, variant, *, limit=bm25.TOTAL, compound_only=False):
    """Return (id,name,raw BM25,fragment match), plus bounded candidate audit.

    Name candidates are ordered by the ORIGINAL full-query score. A separate
    FTS name-phrase subquery supplies eligibility without changing corpus IDF.
    The reranker sorts by fragment match, then the existing file-adjusted score.
    Exact indexed names are prepended in every arm, modeling existing direct
    lookup priority only for source-indexed names. No checker claims are made.
    """
    if variant not in VARIANTS:
        raise ValueError('Unknown experiment variant')
    expr = expression(query)
    fragment = hints(query, compound_only=compound_only) if variant != 'baseline' else ()
    matches = matcher(fragment)
    candidates = list(db.execute('''SELECT rowid,-rank FROM terms WHERE terms MATCH ?
        AND EXISTS (SELECT 1 FROM scope_docs s WHERE s.id=terms.rowid)
        ORDER BY rank LIMIT ?''', (expr, bm25.CANDIDATES))) if expr else []
    ordinary = {id for id, _ in candidates}
    extra = []
    if fragment and variant in ('merge-only', 'fragments'):
        name_expr = ' OR '.join('name : "' + bm25.token_text(word).replace('"', '""') + '"'
                                for word in fragment)
        db.create_function('name_hint', 1, matches)
        # FTS5's rowid-IN plan initializes a full query once per name hit. It is
        # cheap for rare fragments but disastrous for common names. Unary +
        # disables that constraint pushdown, retaining the rank-ordered scan.
        # This bounded probe selects a plan, never a different candidate set.
        probe = db.execute('SELECT rowid FROM terms WHERE terms MATCH ? LIMIT ?',
                           (name_expr, EXTRA+1)).fetchall()
        rowid = 'rowid' if len(probe) <= EXTRA else '+rowid'
        extra = list(db.execute('''SELECT rowid,-rank FROM terms WHERE terms MATCH ?
            AND ROWID_EXPRESSION IN (SELECT rowid FROM terms WHERE terms MATCH ?)
            AND EXISTS (SELECT 1 FROM scope_docs s WHERE s.id=terms.rowid)
            AND EXISTS (SELECT 1 FROM documents d WHERE d.id=terms.rowid AND name_hint(d.name))
            ORDER BY rank LIMIT ?'''.replace('ROWID_EXPRESSION', rowid), (expr, name_expr, EXTRA)))
    combined = dict(candidates)
    combined.update(extra)
    rows = []
    for id, score in combined.items():
        name, preferred = db.execute('''SELECT d.name,s.preferred FROM documents d
            JOIN scope_docs s ON s.id=d.id WHERE d.id=?''', (id,)).fetchone()
        rows.append((id, name, score, int(matches(name)), preferred))
    rerank = variant in ('rank-only', 'fragments')
    rows.sort(key=lambda row: (-(row[3] if rerank else 0), -row[2]*(1.5 if row[4] else 1)))
    exact_name = query.strip().removeprefix('_root_.')
    exact = db.execute('SELECT id,name FROM documents WHERE name=? ORDER BY id LIMIT 1',
                       (exact_name,)).fetchone() if parse_identifier(exact_name) is not None else None
    selected, seen = [], set()
    if exact is not None:
        # Existing direct identifier lookup is independent of discovery scope.
        selected.append((*exact, 0.0, int(matches(exact[1]))))
        seen.add(exact[1])
    for id, name, score, match, _ in rows:
        if len(selected) >= limit:
            break
        if name and name in seen:
            continue
        selected.append((id, name, score, match))
        seen.add(name)
    return selected, (len(ordinary), len(extra), len(set(combined)-ordinary), len(combined))


def production_names(db, query, prefixes):
    db.execute('DROP TABLE IF EXISTS temp.scope_docs')
    rows = bm25.ranked(db, query, prefixes)
    exact_name = query.strip().removeprefix('_root_.')
    exact = db.execute('SELECT name FROM documents WHERE name=? ORDER BY id LIMIT 1',
                       (exact_name,)).fetchone()
    names = [row[0] for row in rows]
    if exact:
        names = [exact[0], *(name for name in names if name != exact[0])][:bm25.TOTAL]
    return names


def summarize(output, results, queries, targets, *, compound_only=False):
    cases = {int(row['id']): row for row in queries}
    def query_hints(query):
        return hints(query, compound_only=compound_only)
    positive = {qid for qid in cases if targets.get(qid)}
    groups = [('verified', sorted(positive)),
              ('hint-verified', sorted(qid for qid in positive if query_hints(cases[qid]['query']))),
              ('no-hint-verified', sorted(qid for qid in positive if not query_hints(cases[qid]['query'])))]
    groups += [(family, sorted(qid for qid in positive if cases[qid].get('family') == family))
               for family in sorted({row.get('family', '') for row in queries})]
    names, first = {}, {}
    detail, timing, paired, summary = [], [], [], []
    for variant in VARIANTS:
        for qid in cases:
            found = [name for name, in results.execute('SELECT name FROM hits WHERE variant=? AND query_id=? ORDER BY rank', (variant, qid))]
            names[variant, qid] = found
            first[variant, qid] = next((i for i, name in enumerate(found, 1) if name in targets.get(qid, ())), 0)
            detail.append((qid, cases[qid]['query'], variant, ' | '.join(query_hints(cases[qid]['query'])),
                           len(targets.get(qid, ())), first[variant, qid],
                           *(len(set(found[:k]) & targets.get(qid, set())) for k in (12, 50))))
        for label, ids in [('all', sorted(cases)), ('hint', [q for q in cases if query_hints(cases[q]['query'])]),
                           ('no-hint', [q for q in cases if not query_hints(cases[q]['query'])])]:
            times = sorted(seconds*1000 for qid, seconds in results.execute(
                'SELECT query_id,seconds FROM runs WHERE variant=? AND trial>0', (variant,)) if qid in ids)
            if times:
                timing.append((label, variant, len(ids), median(times), times[int(.95*(len(times)-1))]))
    for label, ids in groups:
        if not ids:
            continue
        for variant in VARIANTS:
            summary.append((label, variant, len(ids),
                            *(sum(0 < first[variant, qid] <= k for qid in ids) for k in (1, 5, 12, 50)),
                            sum(1/first[variant, qid] if first[variant, qid] else 0 for qid in ids)/len(ids)))
            for k in (1, 5, 12, 50):
                wins = [q for q in ids if 0 < first[variant, q] <= k and not 0 < first['baseline', q] <= k]
                losses = [q for q in ids if 0 < first['baseline', q] <= k and not 0 < first[variant, q] <= k]
                paired.append((label, variant, k, len(wins), len(losses), ' '.join(map(str, wins)), ' '.join(map(str, losses))))
    tsv(output/'summary.tsv', ('subset', 'variant', 'queries', 'success_1', 'success_5', 'success_12', 'success_50', 'mrr_50'), summary)
    tsv(output/'timings.tsv', ('subset', 'variant', 'queries', 'warm_median_ms', 'warm_p95_ms'), timing)
    tsv(output/'by-query.tsv', ('id', 'query', 'variant', 'hints', 'targets', 'first_target', 'targets_12', 'targets_50'), detail)
    tsv(output/'paired.tsv', ('subset', 'variant', 'cutoff', 'wins', 'losses', 'win_ids', 'loss_ids'), paired)
    tsv(output/'changes.tsv', ('id', 'query', 'baseline_first', 'fragment_first', 'baseline_top5', 'fragment_top5'),
        ((q, cases[q]['query'], first['baseline', q], first['fragments', q],
          ' | '.join(names['baseline', q][:5]), ' | '.join(names['fragments', q][:5]))
         for q in cases if names['baseline', q] != names['fragments', q]))
    for row in summary:
        if row[0] == 'verified':
            print(row, flush=True)
    for row in timing:
        if row[0] == 'all':
            print(row, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--queries', type=Path, required=True)
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prefix', action='append', help='Canonical source scope; default: mathlib')
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--compound-only', action='store_true', help='Disable camelCase fallback hints')
    args = parser.parse_args()
    if args.trials < 2:
        parser.error('At least two trials required')
    args.output.mkdir(parents=True, exist_ok=False)
    queries, target_rows = read_tsv(args.queries), read_tsv(args.targets)
    prefixes = tuple(args.prefix or ('mathlib',))
    targets = {}
    with readonly(args.index) as db:
        for row in target_rows:
            if db.execute('SELECT 1 FROM documents WHERE name=?', (row['name'],)).fetchone() is None:
                raise ValueError('Target absent from index: ' + row['name'])
            targets.setdefault(int(row['id']), set()).add(row['name'])
    tsv(args.output/'inputs.tsv', ('input', 'path', 'sha256'),
        ((label, str(path.resolve()), digest(path)) for label, path in
         [('index', args.index), ('queries', args.queries), ('targets', args.targets),
          ('benchmark', Path(__file__)), ('production', Path(bm25.__file__))]))
    tsv(args.output/'settings.tsv', ('key', 'value'), [
        ('prefixes', ' | '.join(prefixes)), ('baseline_candidates', bm25.CANDIDATES),
        ('extra_candidates', EXTRA), ('max_combined', bm25.CANDIDATES+EXTRA),
        ('trials', args.trials), ('hints', 'compound identifiers only' if args.compound_only else 'compound identifiers; camelCase fallback'),
        ('ranking', 'fragment match first, then original file-adjusted BM25'),
        ('exact', 'same source-only direct-name guard in every arm'),
        ('timing', 'warm retrieval/ranking; scope, excerpts, checked lookup excluded')])
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,
        ordinary INTEGER,name_candidates INTEGER,added INTEGER,combined INTEGER,PRIMARY KEY(variant,query_id,trial));
        CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,
        score REAL,fragment INTEGER,PRIMARY KEY(variant,query_id,rank));''')
    reference = readonly(args.index)
    for trial in range(args.trials):
        # Rotate and reverse arm order to reduce systematic cache/order effects.
        variants = VARIANTS[trial % len(VARIANTS):] + VARIANTS[:trial % len(VARIANTS)]
        if trial % 2:
            variants = tuple(reversed(variants))
        with readonly(args.index) as db:
            scope(db, prefixes)
            for row in queries:
                qid, query = int(row['id']), row['query']
                for variant in variants:
                    start = perf_counter()
                    hits, audit = retrieve(db, query, variant, compound_only=args.compound_only)
                    elapsed = perf_counter()-start
                    assert audit[3] <= bm25.CANDIDATES+EXTRA and len(hits) <= bm25.TOTAL
                    results.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (variant, qid, trial, elapsed, *audit))
                    if trial == 0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?,?,?)',
                            ((variant, qid, rank, *hit) for rank, hit in enumerate(hits, 1)))
                        if variant == 'baseline':
                            assert [hit[1] for hit in hits] == production_names(reference, query, prefixes), qid
                    else:
                        assert hits == results.execute('SELECT document,name,score,fragment FROM hits WHERE variant=? AND query_id=? ORDER BY rank', (variant, qid)).fetchall(), (variant, qid)
                results.commit()
            print('Completed trial', trial, flush=True)
    reference.close()
    assert results.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    summarize(args.output, results, queries, targets, compound_only=args.compound_only)
    results.close()
    print('Finished:', args.output, flush=True)


if __name__ == '__main__':
    main()
