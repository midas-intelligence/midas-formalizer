"""Test per-query-identifier BM25 budgets, before the candidate cutoff.

Use existing nine-field compound postings. Equal-weight terms are scored in one
FTS MATCH, then weighted BM25 contributions are summed across all matches. This
is an exact scoring experiment, not reranking the old top100. No Lean or JSON.
"""
import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import sqlite3
from time import perf_counter

from midas.tools import bm25
from tests.benchmark_search_long_tokens import IDENTIFIER, Tokenizer, compound_tokens, evaluate, retrieve
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv

RANK = 'bm25(3,2,1,3,2,1,3,2,1)'


def groups(query):
    """A dot-delimited identifier segment is one unit, like earlier tokenization.

    Repeated equivalent units get no extra query weight. Keep original casing
    until camel splitting. Spaces/dots/punctuation separate units; underscores
    stay inside a unit. STOP filtering is the existing lexical preprocessing.
    """
    seen = set()
    for word in IDENTIFIER.findall(query):
        lexical = tuple(dict.fromkeys(bm25.token_text(word).split()))
        compounds = tuple(dict.fromkeys(compound_tokens(word)))
        signature = (lexical,compounds)
        if (lexical or compounds) and signature not in seen:
            seen.add(signature)
            yield word,lexical,compounds


def coefficients(query, mode):
    if mode == 'raw':
        return dict.fromkeys(Tokenizer('compounds')(query).split(),Fraction(1))
    result = defaultdict(Fraction)
    if mode == 'compounds':
        # Hold existing lexical weights fixed; normalize only added compounds.
        result.update(dict.fromkeys(bm25.token_text(query).split(),Fraction(1)))
    for _,lexical,compounds in groups(query):
        if mode == 'compounds':
            for token in compounds:
                result[token] += Fraction(1,len(compounds))
        elif mode == 'identifier':
            terms = tuple(dict.fromkeys((*lexical,*compounds)))
            for token in terms:
                result[token] += Fraction(1,len(terms))
        else:
            raise ValueError('Unknown normalization mode: '+mode)
    return dict(result)


def expression(tokens):
    return ' OR '.join('"'+token.replace('"','""')+'"' for token in tokens)


def ranked(db, coefficients, limit=bm25.CANDIDATES):
    buckets = defaultdict(list)
    for token,weight in coefficients.items():
        buckets[weight].append(token)
    if not buckets:
        return []
    if len(buckets) == 1:
        weight,tokens = next(iter(buckets.items()))
        # A shared positive factor cannot change ordering; keep FTS's fast path.
        return [(id,score*float(weight)) for id,score in db.execute(
            'SELECT rowid,rank FROM terms WHERE terms MATCH ? AND rank MATCH ? ORDER BY rank LIMIT ?',
            (expression(tokens),RANK,limit))]
    parts,params = [],[]
    for weight,tokens in buckets.items():
        parts.append('SELECT rowid,bm25(terms,3,2,1,3,2,1,3,2,1)*? AS score FROM terms WHERE terms MATCH ?')
        params.extend((float(weight),expression(tokens)))
    # MATERIALIZED keeps FTS auxiliary functions inside their matching cursor.
    # There is deliberately no per-bucket LIMIT: every posting can contribute.
    sql = ('WITH scores AS MATERIALIZED ('+' UNION ALL '.join(parts)+') '
           'SELECT rowid,sum(score) AS total FROM scores GROUP BY rowid '
           'ORDER BY total,rowid LIMIT ?')
    return db.execute(sql,(*params,limit)).fetchall()


def normalized_retrieve(db, query, mode):
    rows = ranked(db,coefficients(query,mode))
    exact = [id for id, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                     (query.strip().removeprefix('_root_.'),))]
    seen,found = set(),[]
    for id in dict.fromkeys([*exact,*(id for id,_ in rows)]):
        name, = db.execute('SELECT name FROM documents WHERE id=?',(id,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        found.append((id,name))
        if len(found) == bm25.TOTAL:
            break
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--trials',type=int,default=3)
    args = parser.parse_args()
    if args.trials < 2:
        parser.error('At least two trials are required')
    inputs = read_tsv(args.previous/'inputs.tsv')
    for row in inputs:
        assert digest(Path(row['path']))==row['sha256'],row['input']
    paths = {row['input']:Path(row['path']) for row in inputs}
    indices = read_tsv(args.previous/'indices.tsv')
    for row in indices:
        assert digest(Path(row['path']))==row['sha256'],row['kind']
    index = {row['kind']:Path(row['path']) for row in indices}
    args.output.mkdir(parents=True,exist_ok=False)
    inputs.append(dict(input='identifier_budgets',path=str(Path(__file__).resolve()),sha256=digest(Path(__file__))))
    tsv(args.output/'inputs.tsv',('input','path','sha256'),((r['input'],r['path'],r['sha256']) for r in inputs))
    tsv(args.output/'indices.tsv',('kind','path','sha256'),((r['kind'],r['path'],r['sha256']) for r in indices))
    queries = read_tsv(paths['queries'])
    variants = [('baseline',None),('compound-raw','raw'),('compound-budget','compounds'),('identifier-budget','identifier')]
    tsv(args.output/'query-weights.tsv',('variant','id','token','numerator','denominator'),
        ((label,row['id'],token,weight.numerator,weight.denominator) for label,mode in variants if mode
         for row in queries for token,weight in coefficients(row['query'],mode).items()))
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    old = readonly(args.previous/'results.sqlite')
    for trial in range(args.trials):
        for label,mode in (variants if trial%2==0 else list(reversed(variants))):
            with readonly(index['compounds' if mode else 'baseline']) as db:
                for row in queries:
                    qid,query = int(row['id']),row['query']
                    start = perf_counter()
                    hits = normalized_retrieve(db,query,mode) if mode else retrieve(db,query,Tokenizer('baseline'))
                    elapsed = perf_counter()-start
                    results.execute('INSERT INTO runs VALUES(?,?,?,?)',(label,qid,trial,elapsed))
                    if trial == 0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',((label,qid,rank,id,name) for rank,(id,name) in enumerate(hits,1)))
                        if label in ('baseline','compound-raw'):
                            control = 'baseline' if label=='baseline' else 'pair1-triple1'
                            assert hits==old.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(control,qid)).fetchall(),(label,qid,'control mismatch')
                    else:
                        assert hits==results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall(),(label,qid,'unstable')
                    if qid % 50 == 0:
                        print(label,'trial',trial,'query',qid,flush=True)
            results.commit()
            print('Replayed',label,'trial',trial,flush=True)
    evaluate(results,queries,read_tsv(paths['targets']),[label for label,_ in variants],args.output)
    assert results.execute('PRAGMA integrity_check').fetchone()==('ok',)
    results.close()
    old.close()
    print('Finished:',args.output/'summary.tsv',flush=True)


if __name__=='__main__':
    main()
