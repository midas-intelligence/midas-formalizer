"""Sweep added BPE lengths and BM25 compound field weights, without Lean/JSON.

Reuse models and control indices from benchmark_search_long_tokens. Output must
be a fresh directory. All indices open read-only during retrieval. Length means
Unicode surface characters, including underscores, as in the previous experiment.
"""
import argparse
from pathlib import Path
import sqlite3
from time import perf_counter

from midas.tools import bm25
from tests.benchmark_search_long_tokens import Tokenizer, build, compound_tokens, evaluate, retrieve
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv


def build_compounds(corpus, path):
    start = perf_counter()
    with sqlite3.connect(path) as db, readonly(corpus) as source:
        db.executescript('''CREATE TABLE documents(id INTEGER PRIMARY KEY,name TEXT);
          CREATE INDEX names ON documents(name);
          CREATE VIRTUAL TABLE terms USING fts5(name,documentation,statement,
            compound_name,compound_documentation,compound_statement,
            content='',tokenize='unicode61 remove_diacritics 0');''')
        count = 0
        for id,name,doc,statement in source.execute('SELECT id,name,documentation,statement FROM documents ORDER BY id'):
            values = (name,doc,statement)
            fields = [bm25.token_text(value) for value in values]
            fields += [' '.join(compound_tokens(value)) for value in values]
            count += sum(len(field.split()) for field in fields)
            db.execute('INSERT INTO documents VALUES(?,?)',(id,name))
            db.execute('INSERT INTO terms(rowid,name,documentation,statement,compound_name,compound_documentation,compound_statement) VALUES(?,?,?,?,?,?,?)', (id,*fields))
        db.execute("INSERT INTO terms(terms) VALUES('optimize')")
        db.commit()
        elapsed = perf_counter()-start
        db.execute("CREATE VIRTUAL TABLE temp.vocabulary USING fts5vocab(main,terms,'row')")
        unique, = db.execute('SELECT count(*) FROM vocabulary').fetchone()
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    return elapsed,path.stat().st_size,count,unique


def weighted_retrieve(db, query, weight):
    tokens = dict.fromkeys(Tokenizer('compounds')(query).split())
    expression = ' OR '.join('"'+word.replace('"','""')+'"' for word in tokens)
    # FTS5 column weights multiply term frequency before BM25 saturation.
    # Total document length stays identical at every weight, including 1x.
    rank = f'bm25(3,2,1,{3*weight},{2*weight},{weight})'
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
    parser.add_argument('--minimums',type=int,nargs='+',default=[1,2,3,4,5,6])
    parser.add_argument('--weights',type=int,nargs='+',default=[1,2,4,8])
    parser.add_argument('--trials',type=int,default=3)
    args = parser.parse_args()
    if args.trials < 2 or any(v<1 for v in args.minimums+args.weights) or len(set(args.minimums)) != len(args.minimums) or len(set(args.weights)) != len(args.weights):
        parser.error('Use distinct positive minimums/weights and at least two trials')
    inputs = read_tsv(args.previous/'inputs.tsv')
    for row in inputs:
        assert digest(Path(row['path'])) == row['sha256'],row['input']
    paths = {row['input']:Path(row['path']) for row in inputs}
    args.output.mkdir(parents=True,exist_ok=False)
    inputs += [dict(input='sweep',path=str(Path(__file__).resolve()),sha256=digest(Path(__file__)))]
    for size in (8192,32768):
        model = args.previous/f'bpe{size}.model'
        inputs.append(dict(input=f'model{size}',path=str(model.resolve()),sha256=digest(model)))
    tsv(args.output/'inputs.tsv',('input','path','sha256'),((r['input'],r['path'],r['sha256']) for r in inputs))
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE builds(variant TEXT PRIMARY KEY,seconds REAL,bytes INTEGER,tokens INTEGER,unique_terms INTEGER);
      CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    old = readonly(args.previous/'results.sqlite')
    variants = [('baseline',args.previous/'baseline.sqlite',Tokenizer('baseline'),None)]
    reused = [('baseline','baseline')]
    compound_path = args.output/'compounds.sqlite'
    stats = build_compounds(paths['corpus'],compound_path)
    results.execute('INSERT INTO builds VALUES(?,?,?,?,?)',('compound-fields',*stats))
    results.commit()
    print('Built compound fields',stats,flush=True)
    for weight in args.weights:
        variants.append((f'compound-x{weight}',compound_path,None,weight))
    for size in (8192,32768):
        for minimum in args.minimums:
            label = f'bpe{size}-min{minimum}'
            tokenizer = Tokenizer('append',args.previous/f'bpe{size}.model',minimum=minimum)
            if minimum in (1,4):
                control = f'bpe{size}-'+('append' if minimum==1 else 'long')
                path = args.previous/(control+'.sqlite')
                reused.append((label,control))
            else:
                path = args.output/(label+'.sqlite')
                stats = build(paths['corpus'],path,tokenizer)
                results.execute('INSERT INTO builds VALUES(?,?,?,?,?)',(label,*stats))
                results.commit()
                print('Built',label,stats,flush=True)
            variants.append((label,path,tokenizer,None))
    for label,control in reused:
        stats = old.execute('SELECT seconds,bytes,tokens,unique_terms FROM builds WHERE variant=?',(control,)).fetchone()
        results.execute('INSERT INTO builds VALUES(?,?,?,?,?)',(label,*stats))
    results.commit()
    tsv(args.output/'builds.tsv',('variant','seconds','bytes','tokens','unique_terms'),results.execute('SELECT * FROM builds'))
    tsv(args.output/'indices.tsv',('variant','path','sha256','compound_weight','minimum_piece_length','reused'),
        ((label,str(path.resolve()),digest(path),weight or '',tokenizer.minimum if label.startswith('bpe') else '',int(label in dict(reused))) for label,path,tokenizer,weight in variants))
    queries = read_tsv(paths['queries'])
    controls = dict(reused)
    controls['compound-x1'] = 'compounds'
    for trial in range(args.trials):
        for label,path,tokenizer,weight in (variants if trial%2==0 else list(reversed(variants))):
            with readonly(path) as db:
                for row in queries:
                    qid,query = int(row['id']),row['query']
                    start = perf_counter()
                    hits = weighted_retrieve(db,query,weight) if weight else retrieve(db,query,tokenizer)
                    elapsed = perf_counter()-start
                    results.execute('INSERT INTO runs VALUES(?,?,?,?)',(label,qid,trial,elapsed))
                    if trial == 0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',((label,qid,rank,id,name) for rank,(id,name) in enumerate(hits,1)))
                        if label in controls:
                            assert hits == old.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(controls[label],qid)).fetchall(),(label,qid,'control mismatch')
                    else:
                        assert hits == results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall(),(label,qid,'unstable')
            results.commit()
            print('Replayed',label,'trial',trial,flush=True)
    evaluate(results,queries,read_tsv(paths['targets']),[label for label,_,_,_ in variants],args.output)
    assert results.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    old.close()
    results.close()
    print('Finished:',args.output/'summary.tsv',flush=True)


if __name__ == '__main__':
    main()
