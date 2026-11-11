"""Compare downloaded binary SentencePiece tokenizers with production BM25.

Run with --help from new/. Requires sentencepiece and protobuf in an experimental
venv. No training, Lean processes, network requests, or JSON corpus operations.
Outputs use SQLite and TSV. Existing output directories cannot be overwritten.
"""
import argparse
import csv
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import sqlite3
from statistics import median
from time import perf_counter

from midas.tools import bm25


def read_tsv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream, delimiter='\t'))


def tsv(path, header, rows):
    with path.open('w') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(header)
        writer.writerows(rows)


def digest(path):
    with path.open('rb') as stream:
        h = sha256()
        for block in iter(lambda: stream.read(1024**2), b''):
            h.update(block)
        return h.hexdigest()


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


class Pretrained:
    """Hold lexical preprocessing constant; subtokenize each baseline word.

    Remove SentencePiece's artificial word-start marker so a fragment can match
    at either position. Retain one-character and byte pieces, but omit unknown,
    control, and unused IDs. Map canonical piece surfaces to opaque FTS tokens;
    SQLite must not split or normalize the tokenizer's piece spelling again.
    """
    def __init__(self, path):
        import sentencepiece as sp
        from sentencepiece import sentencepiece_model_pb2 as proto
        self.processor = sp.SentencePieceProcessor(model_file=str(path))
        model = proto.ModelProto.FromString(path.read_bytes())
        self.model_type = proto.TrainerSpec.ModelType.Name(model.trainer_spec.model_type)
        self.size = self.processor.vocab_size()
        canonical = {}
        self.tokens = []
        for id in range(self.size):
            piece = self.processor.id_to_piece(id).replace('▁', '')
            if not piece or self.processor.is_unknown(id) or self.processor.is_control(id) or self.processor.is_unused(id):
                self.tokens.append('')
            else:
                self.tokens.append(canonical.setdefault(piece, 'spxtoken' + str(id)))
        self.word = lru_cache(maxsize=100_000)(self._word)

    def _word(self, word):
        return tuple(self.tokens[id] for id in self.processor.encode(word) if self.tokens[id])

    def encode(self, baseline):
        return ' '.join(piece for word in baseline.split() for piece in self.word(word))

    def display(self, word):
        return ' | '.join(self.processor.encode(word, out_type=str))


def query_tokens(query, tokenizer=None, mode='baseline'):
    baseline = bm25.token_text(query)
    if mode == 'baseline':
        return baseline
    pieces = tokenizer.encode(baseline)
    return pieces if mode == 'replace' else baseline + ' ' + pieces


def retrieve(db, query, tokenizer=None, mode='baseline'):
    tokens = dict.fromkeys(query_tokens(query, tokenizer, mode).split())
    if not tokens:
        return []
    expression = ' OR '.join('"' + word.replace('"', '""') + '"' for word in tokens)
    ids = [id for id, in db.execute(
        'SELECT rowid FROM terms WHERE terms MATCH ? ORDER BY rank LIMIT ?',
        (expression, bm25.CANDIDATES))]
    # The real application does exact checked lookup before discovery. This
    # source-only guard represents priority only for indexed exact names.
    exact = [id for id, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                    (query.strip().removeprefix('_root_.'),))]
    seen, rows = set(), []
    for id in dict.fromkeys([*exact, *ids]):
        name, = db.execute('SELECT name FROM documents WHERE id=?', (id,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        rows.append((id, name))
        if len(rows) == bm25.TOTAL:
            break
    return rows


def extract(corpus, path, reference):
    start = perf_counter()
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY,name TEXT,path TEXT,line INTEGER,documentation TEXT,statement TEXT)')
    ref = readonly(reference)
    expected = iter(ref.execute('SELECT d.id,d.name,f.path,d.line FROM documents d JOIN files f ON f.id=d.file ORDER BY d.id'))
    fingerprint = sha256()
    total = 0
    for source in sorted((corpus / 'Mathlib').rglob('*.lean')):
        if source.stat().st_size > bm25.FILE_BYTES:
            raise ValueError('Source file exceeds production allowance')
        raw = source.read_bytes()
        logical = 'mathlib/' + source.relative_to(corpus).as_posix()
        fingerprint.update(logical.encode())
        fingerprint.update(sha256(raw).digest())
        for name, line, _, _, _, doc, header in bm25.documents(raw.decode()):
            total += 1
            assert next(expected) == (total, name, logical, line), 'Corpus differs from baseline'
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)', (total, name, logical, line, doc, header))
    assert total and next(expected, None) is None
    db.commit()
    ref.close()
    db.close()
    return total, fingerprint.hexdigest(), perf_counter()-start


def build(raw, path, tokenizer=None, mode='baseline'):
    start = perf_counter()
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE documents(id INTEGER PRIMARY KEY,name TEXT);
      CREATE INDEX names ON documents(name);
      CREATE VIRTUAL TABLE terms USING fts5(name,documentation,statement,
        content='',tokenize='unicode61 remove_diacritics 0');''')
    db.execute("INSERT INTO terms(terms,rank) VALUES('rank',?)", (bm25.WEIGHTED_RANK,))
    tokens = 0
    source = readonly(raw)
    for id, name, doc, statement in source.execute('SELECT id,name,documentation,statement FROM documents ORDER BY id'):
        fields = [query_tokens(value, tokenizer, mode) for value in (name, doc, statement)]
        tokens += sum(len(field.split()) for field in fields)
        db.execute('INSERT INTO documents VALUES(?,?)', (id, name))
        db.execute('INSERT INTO terms(rowid,name,documentation,statement) VALUES(?,?,?,?)', (id, *fields))
    db.execute("INSERT INTO terms(terms) VALUES('optimize')")
    db.commit()
    elapsed = perf_counter()-start
    db.execute("CREATE VIRTUAL TABLE temp.vocabulary USING fts5vocab(main,terms,'row')")
    unique, = db.execute('SELECT count(*) FROM vocabulary').fetchone()
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    db.close()
    source.close()
    return elapsed, path.stat().st_size, tokens, unique


def evaluate(results, judgments, labels, output):
    grades = {(int(row['id']), row['name']): row['grade'] for row in judgments}
    cases = sorted({qid for qid, _ in grades})
    targets = {qid: {name for (id, name), grade in grades.items() if id == qid and grade == 'direct'} for qid in cases}
    summary, detail = [], []
    for label in labels:
        per_query = []
        for qid in cases:
            names = [name for name, in results.execute('SELECT name FROM hits WHERE variant=? AND query_id=? ORDER BY rank', (label, qid))]
            rank = next((i for i, name in enumerate(names, 1) if name in targets[qid]), 0)
            found = [len(set(names[:k]) & targets[qid]) for k in (5, 12, 50)]
            unknown = sum((qid, name) not in grades for name in names[:5])
            row = (label, qid, len(targets[qid]), rank, *found, unknown)
            detail.append(row)
            per_query.append(row)
        times = [value*1000 for value, in results.execute('SELECT seconds FROM runs WHERE variant=? AND trial>0', (label,))]
        tokens = [value for value, in results.execute('SELECT query_tokens FROM runs WHERE variant=? AND trial=0', (label,))]
        build_row = results.execute('SELECT seconds,bytes,tokens,unique_terms FROM builds WHERE variant=?', (label,)).fetchone()
        # These are recovery metrics for the previous review's finite target set,
        # not precision, full-corpus recall, or a fresh relevance evaluation.
        summary.append((label, *build_row, median(times), sorted(times)[int(.95*(len(times)-1))], median(tokens),
            sum(0 < row[3] <= 1 for row in per_query),
            *(sum(0 < row[3] <= k for row in per_query) for k in (5, 12, 50)),
            *(sum(row[i] for row in per_query) for i in (4, 5, 6, 7))))
    tsv(output/'summary.tsv', ('variant','build_seconds','index_bytes','indexed_tokens','unique_terms','warm_median_ms','warm_p95_ms','median_query_tokens',
        'known_success_1','known_success_5','known_success_12','known_success_50','known_targets_5','known_targets_12','known_targets_50','unjudged_top5'), summary)
    tsv(output/'by-query.tsv', ('variant','query_id','known_targets','first_known_direct','found_5','found_12','found_50','unjudged_top5'), detail)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', required=True, type=Path)
    parser.add_argument('--queries', required=True, type=Path, help='TSV with id and query columns')
    parser.add_argument('--judgments', required=True, type=Path, help='Existing TSV with id/name/grade columns')
    parser.add_argument('--reference-index', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--model', action='append', required=True, help='label=/absolute/path/to/binary.model')
    parser.add_argument('--trials', type=int, default=3)
    args = parser.parse_args()
    if args.trials < 2:
        parser.error('At least two trials are required')
    models = [(label, Path(path)) for label, path in (arg.split('=', 1) for arg in args.model)]
    if len({label for label, _ in models}) != len(models) or any(not label.isalnum() for label, _ in models):
        parser.error('Model labels must be unique and alphanumeric')
    tokenizers = [(label, path, Pretrained(path)) for label, path in models]
    args.output.mkdir(parents=True, exist_ok=False)
    tsv(args.output/'models.tsv', ('label','path','sha256','vocabulary_size','model_type'),
        ((label,str(path.resolve()),digest(path),model.size,model.model_type) for label,path,model in tokenizers))
    tsv(args.output/'inputs.tsv', ('input','path','sha256'),
        ((key,str(path.resolve()),digest(path)) for key,path in [('queries',args.queries),('judgments',args.judgments),
         ('reference',args.reference_index),('benchmark',Path(__file__)),('bm25',Path(bm25.__file__))]))
    corpus = args.output/'corpus.sqlite'
    count, fingerprint, seconds = extract(args.corpus, corpus, args.reference_index)
    tsv(args.output/'corpus.tsv', ('documents','hash','extraction_seconds'), [(count,fingerprint,seconds)])
    print('Extracted', count, 'declarations in', round(seconds, 2), 'seconds', flush=True)
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE builds(variant TEXT PRIMARY KEY,seconds REAL,bytes INTEGER,tokens INTEGER,unique_terms INTEGER);
      CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,query_tokens INTEGER,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    variants = [('baseline',None,'baseline')]
    variants += [(label+'-'+mode,model,mode) for label,_,model in tokenizers for mode in ('replace','append')]
    queries = [(int(row['id']),row['query']) for row in read_tsv(args.queries)]
    tsv(args.output/'query-pieces.tsv', ('model','query_id','word','pieces'),
        ((label,qid,word,model.display(word)) for label,_,model in tokenizers for qid,query in queries for word in bm25.token_text(query).split()))
    for label, model, mode in variants:
        stats = build(corpus, args.output/(label+'.sqlite'), model, mode)
        results.execute('INSERT INTO builds VALUES(?,?,?,?,?)', (label,*stats))
        results.commit()
        print('Built', label, tuple(round(x, 2) for x in stats), flush=True)
    # Alternate configuration order to reduce systematic cache/order bias.
    for trial in range(args.trials):
        for label, model, mode in (variants if trial % 2 == 0 else list(reversed(variants))):
            db = readonly(args.output/(label+'.sqlite'))
            for qid, query in queries:
                start = perf_counter()
                rows = retrieve(db, query, model, mode)
                elapsed = perf_counter()-start
                results.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (label,qid,trial,elapsed,len(query_tokens(query,model,mode).split())))
                if trial == 0:
                    results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)', ((label,qid,rank,id,name) for rank,(id,name) in enumerate(rows,1)))
                else:
                    assert rows == results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank', (label,qid)).fetchall()
            db.close()
            results.commit()
            print('Replayed', label, 'trial', trial, flush=True)
    evaluate(results, read_tsv(args.judgments), [label for label,_,_ in variants], args.output)
    assert results.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    results.close()
    print('Finished:', args.output/'summary.tsv', flush=True)


if __name__ == '__main__':
    main()
