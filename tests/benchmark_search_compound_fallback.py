"""Per-candidate compound-first BM25 with discounted component fallback.

Keep one normalized score budget per query identifier. A candidate's compound
match suppresses lexical contributions for that identifier only. All postings
are considered before the candidate cutoff. No Lean, network, or JSON needed.
"""
import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import sqlite3
from time import perf_counter

from midas.tools import bm25
from tests.benchmark_search_identifier_budgets import coefficients, expression, groups, normalized_retrieve, ranked
from tests.benchmark_search_long_tokens import Tokenizer, evaluate, retrieve
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv


def fallback_ranked(db, query, discount, limit=bm25.CANDIDATES):
    units = list(groups(query))
    if not any(compounds for _,_,compounds in units):
        return ranked(db,coefficients(query,'identifier'),limit)
    parts,params = [],[]
    ordinary = defaultdict(Fraction)
    def add(unit,channel,tokens,weight):
        if tokens:
            parts.append('SELECT rowid,? AS unit,? AS compound,bm25(terms,3,2,1,3,2,1,3,2,1)*? AS score FROM terms WHERE terms MATCH ?')
            params.extend((unit,channel,float(weight),expression(tokens)))
    for unit,(_,lexical,compounds) in enumerate(units):
        if compounds:
            add(unit,1,compounds,Fraction(1,len(compounds)))
            # Zero-discount lexical postings cannot affect scores or matching.
            if lexical and discount:
                add(unit,0,lexical,discount/len(lexical))
        else:
            for token in lexical:
                ordinary[token] += Fraction(1,len(lexical))
    buckets = defaultdict(list)
    for token,weight in ordinary.items():
        buckets[weight].append(token)
    for weight,tokens in buckets.items():
        add(-1,0,tokens,weight)
    sql = ('WITH scores AS MATERIALIZED ('+' UNION ALL '.join(parts)+'), '
           'per_unit AS (SELECT rowid,unit,max(compound) AS matched_compound, '
           'sum(CASE WHEN compound=1 THEN score ELSE 0 END) AS compound_score, '
           'sum(CASE WHEN compound=0 THEN score ELSE 0 END) AS lexical_score '
           'FROM scores GROUP BY rowid,unit) '
           'SELECT rowid,sum(CASE WHEN matched_compound THEN compound_score ELSE lexical_score END) AS total '
           'FROM per_unit GROUP BY rowid HAVING total<0 ORDER BY total,rowid LIMIT ?')
    return db.execute(sql,(*params,limit)).fetchall()


def fallback_retrieve(db, query, discount):
    rows = fallback_ranked(db,query,discount)
    exact = [id for id, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                     (query.strip().removeprefix('_root_.'),))]
    seen,found = set(),[]
    for id in dict.fromkeys([*exact,*(id for id,_ in rows)]):
        name, = db.execute('SELECT name FROM documents WHERE id=?',(id,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        found.append((id,name))
        if len(found)==bm25.TOTAL:
            break
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--discounts',nargs='+',default=['0','0.1','0.25','0.5','1'])
    parser.add_argument('--trials',type=int,default=3)
    args = parser.parse_args()
    try:
        discounts = [Fraction(value) for value in args.discounts]
        assert args.trials>=2 and len(set(discounts))==len(discounts)
        assert all(0<=value<=1 for value in discounts)
    except (ValueError,ZeroDivisionError,AssertionError):
        parser.error('Use distinct discounts between 0 and 1 and at least two trials')
    inputs = read_tsv(args.previous/'inputs.tsv')
    for row in inputs:
        assert digest(Path(row['path']))==row['sha256'],row['input']
    paths = {row['input']:Path(row['path']) for row in inputs}
    indices = read_tsv(args.previous/'indices.tsv')
    for row in indices:
        assert digest(Path(row['path']))==row['sha256'],row['kind']
    index = {row['kind']:Path(row['path']) for row in indices}
    args.output.mkdir(parents=True,exist_ok=False)
    inputs.append(dict(input='compound_fallback',path=str(Path(__file__).resolve()),sha256=digest(Path(__file__))))
    tsv(args.output/'inputs.tsv',('input','path','sha256'),((r['input'],r['path'],r['sha256']) for r in inputs))
    tsv(args.output/'indices.tsv',('kind','path','sha256'),((r['kind'],r['path'],r['sha256']) for r in indices))
    queries = read_tsv(paths['queries'])
    variants = [('baseline',None),('identifier-budget',None)]+[(f'fallback-{float(d):g}',d) for d in discounts]
    tsv(args.output/'settings.tsv',('variant','fallback_numerator','fallback_denominator'),
        ((label,d.numerator if d is not None else '',d.denominator if d is not None else '') for label,d in variants))
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    old = readonly(args.previous/'results.sqlite')
    for trial in range(args.trials):
        for label,discount in (variants if trial%2==0 else list(reversed(variants))):
            with readonly(index['baseline' if label=='baseline' else 'compounds']) as db:
                for row in queries:
                    qid,query = int(row['id']),row['query']
                    start = perf_counter()
                    if label=='baseline':
                        hits = retrieve(db,query,Tokenizer('baseline'))
                    elif label=='identifier-budget':
                        hits = normalized_retrieve(db,query,'identifier')
                    else:
                        hits = fallback_retrieve(db,query,discount)
                    elapsed = perf_counter()-start
                    results.execute('INSERT INTO runs VALUES(?,?,?,?)',(label,qid,trial,elapsed))
                    if trial==0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',((label,qid,rank,id,name) for rank,(id,name) in enumerate(hits,1)))
                        if discount is None:
                            assert hits==old.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall(),(label,qid,'control mismatch')
                    else:
                        assert hits==results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall(),(label,qid,'unstable')
                    if qid%50==0:
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
