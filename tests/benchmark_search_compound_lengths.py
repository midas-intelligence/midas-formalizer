"""Compare separate pair/triple BM25 weights over frozen source search inputs.

No Lean, SentencePiece dependency, network, or JSON operations. Reuse the
completed token-weight sweep for inputs and equal-weight ranking controls.
"""
import argparse
from pathlib import Path
import sqlite3
from time import perf_counter

from midas.tools import bm25
from tests.benchmark_search_long_tokens import Tokenizer, compound_tokens, evaluate, retrieve
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv


def split_compounds(value):
    pairs, triples = [], []
    for token in compound_tokens(value):
        size = bytes.fromhex(token[5:]).decode().count('_')+1
        (pairs if size == 2 else triples).append(token)
    return ' '.join(pairs), ' '.join(triples)


def build(corpus, path):
    start = perf_counter()
    with sqlite3.connect(path) as db, readonly(corpus) as source:
        db.executescript('''CREATE TABLE documents(id INTEGER PRIMARY KEY,name TEXT);
          CREATE INDEX names ON documents(name);
          CREATE VIRTUAL TABLE terms USING fts5(name,documentation,statement,
            pair_name,pair_documentation,pair_statement,
            triple_name,triple_documentation,triple_statement,
            content='',tokenize='unicode61 remove_diacritics 0');''')
        count = 0
        for id,name,doc,statement in source.execute('SELECT id,name,documentation,statement FROM documents ORDER BY id'):
            values = (name,doc,statement)
            split = [split_compounds(value) for value in values]
            fields = [bm25.token_text(value) for value in values]
            fields += [pair for pair,_ in split]+[triple for _,triple in split]
            count += sum(len(field.split()) for field in fields)
            db.execute('INSERT INTO documents VALUES(?,?)',(id,name))
            db.execute('INSERT INTO terms(rowid,name,documentation,statement,pair_name,pair_documentation,pair_statement,triple_name,triple_documentation,triple_statement) VALUES(?,?,?,?,?,?,?,?,?,?)',(id,*fields))
        db.execute("INSERT INTO terms(terms) VALUES('optimize')")
        db.commit()
        elapsed = perf_counter()-start
        db.execute("CREATE VIRTUAL TABLE temp.vocabulary USING fts5vocab(main,terms,'row')")
        unique, = db.execute('SELECT count(*) FROM vocabulary').fetchone()
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    return elapsed,path.stat().st_size,count,unique


def weighted_retrieve(db, query, pair_weight, triple_weight):
    tokens = dict.fromkeys(Tokenizer('compounds')(query).split())
    expression = ' OR '.join('"'+word.replace('"','""')+'"' for word in tokens)
    weights = (3,2,1,3*pair_weight,2*pair_weight,pair_weight,3*triple_weight,2*triple_weight,triple_weight)
    rank = 'bm25('+','.join(map(str,weights))+')'
    ids = [id for id, in db.execute(
        'SELECT rowid FROM terms WHERE terms MATCH ? AND rank MATCH ? ORDER BY rank LIMIT ?',
        (expression,rank,bm25.CANDIDATES))] if tokens else []
    exact = [id for id, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                     (query.strip().removeprefix('_root_.'),))]
    seen, rows = set(), []
    for id in dict.fromkeys([*exact,*ids]):
        name, = db.execute('SELECT name FROM documents WHERE id=?',(id,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        rows.append((id,name))
        if len(rows) == bm25.TOTAL:
            break
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--weights',nargs='+',default=['1:1','2:2','4:4','8:8','1:2','1:4','2:4','2:8'])
    parser.add_argument('--trials',type=int,default=3)
    args = parser.parse_args()
    try:
        weights = [tuple(map(int,value.split(':'))) for value in args.weights]
        assert args.trials >= 2 and len(set(weights)) == len(weights)
        assert all(len(pair)==2 and min(pair)>0 for pair in weights)
    except (ValueError,AssertionError):
        parser.error('Use distinct positive pair:triple weights and at least two trials')
    inputs = read_tsv(args.previous/'inputs.tsv')
    for row in inputs:
        assert digest(Path(row['path'])) == row['sha256'],row['input']
    paths = {row['input']:Path(row['path']) for row in inputs}
    baseline = next(row for row in read_tsv(args.previous/'indices.tsv') if row['variant']=='baseline')
    assert digest(Path(baseline['path'])) == baseline['sha256']
    args.output.mkdir(parents=True,exist_ok=False)
    inputs.append(dict(input='length_sweep',path=str(Path(__file__).resolve()),sha256=digest(Path(__file__))))
    tsv(args.output/'inputs.tsv',('input','path','sha256'),((r['input'],r['path'],r['sha256']) for r in inputs))
    index = args.output/'compounds.sqlite'
    stats = build(paths['corpus'],index)
    tsv(args.output/'builds.tsv',('variant','seconds','bytes','tokens','unique_terms'),[('compound-length-fields',*stats)])
    print('Built pair/triple fields',stats,flush=True)
    variants = [('baseline',0,0)]+[(f'pair{p}-triple{t}',p,t) for p,t in weights]
    tsv(args.output/'indices.tsv',('kind','path','sha256'),[(name,str(path.resolve()),digest(path)) for name,path in [('baseline',Path(baseline['path'])),('compounds',index)]])
    tsv(args.output/'settings.tsv',('variant','pair_weight','triple_weight'),variants)
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    queries = read_tsv(paths['queries'])
    old = readonly(args.previous/'results.sqlite')
    for trial in range(args.trials):
        for label,pair,triple in (variants if trial%2==0 else list(reversed(variants))):
            with readonly(index if pair else Path(baseline['path'])) as db:
                for row in queries:
                    qid,query = int(row['id']),row['query']
                    start = perf_counter()
                    hits = weighted_retrieve(db,query,pair,triple) if pair else retrieve(db,query,Tokenizer('baseline'))
                    elapsed = perf_counter()-start
                    results.execute('INSERT INTO runs VALUES(?,?,?,?)',(label,qid,trial,elapsed))
                    if trial == 0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',((label,qid,rank,id,name) for rank,(id,name) in enumerate(hits,1)))
                        control = 'baseline' if pair==0 else f'compound-x{pair}' if pair==triple else None
                        if control and old.execute('SELECT 1 FROM runs WHERE variant=? LIMIT 1',(control,)).fetchone():
                            assert hits == old.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(control,qid)).fetchall(),(label,qid,'control mismatch')
                    else:
                        assert hits == results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall(),(label,qid,'unstable')
            results.commit()
            print('Replayed',label,'trial',trial,flush=True)
    evaluate(results,queries,read_tsv(paths['targets']),[label for label,_,_ in variants],args.output)
    assert results.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    results.close()
    old.close()
    print('Finished:',args.output/'summary.tsv',flush=True)


if __name__ == '__main__':
    main()
