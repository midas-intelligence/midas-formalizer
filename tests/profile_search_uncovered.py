"""Profile and replay frozen BM25 scorers; no Lean, network or JSON."""
import argparse
import cProfile
from math import isclose
from pathlib import Path
import pstats
import sqlite3
from statistics import median
from threading import Event
from time import monotonic, perf_counter

from tests.benchmark_search_compound_fallback import fallback_ranked
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv
from tests.benchmark_search_uncovered import uncovered_ranked
from tests.benchmark_search_uncovered_fast import fast_ranked


def render(db, query, rows):
    exact = [doc for doc, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                      (query.strip().removeprefix('_root_.'),))]
    seen, found = set(), []
    for doc in dict.fromkeys([*exact, *(doc for doc, _ in rows)]):
        name, = db.execute('SELECT name FROM documents WHERE id=?', (doc,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        found.append((doc,name))
        if len(found)==50:
            break
    return found


def configure(db):
    """Production temp storage and cancellation/deadline polling settings."""
    cancel, deadline = Event(), monotonic()+3600
    db.execute('PRAGMA temp_store=FILE')
    db.set_progress_handler(lambda: int(cancel.is_set() or monotonic()>=deadline),1000)


VARIANTS = [('previous',fallback_ranked),('reference',uncovered_ranked),('optimized',fast_ranked)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--references',nargs='+',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--trials',type=int,default=4)
    args=parser.parse_args()
    if args.trials<3:
        parser.error('Use at least three trials; the first is warmup')
    args.output.mkdir(parents=True,exist_ok=False)
    manifest={}
    cases=[]
    for root in args.references:
        for filename,pathfield in [('inputs.tsv','path'),('indices.tsv','path')]:
            for row in read_tsv(root/filename):
                path=Path(row[pathfield])
                if path not in manifest:
                    assert digest(path)==row['sha256'],path
                    manifest[path]=row['sha256']
        paths={r['input']:Path(r['path']) for r in read_tsv(root/'inputs.tsv')}
        index=Path(next(r['path'] for r in read_tsv(root/'indices.tsv') if r['kind']=='compounds'))
        queries=read_tsv(paths['queries'])
        known={int(r['id']) for r in read_tsv(paths['targets'])}
        cases.append((root,index,queries,known))
    for name in ('profile_search_uncovered.py','benchmark_search_uncovered_fast.py'):
        path=Path(__file__).with_name(name).resolve()
        manifest[path]=digest(path)
    tsv(args.output/'inputs.tsv',('path','sha256'),((str(p.resolve()),h) for p,h in manifest.items()))
    results=sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''
      CREATE TABLE runs(dataset TEXT,variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,
                        PRIMARY KEY(dataset,variant,query_id,trial));
      CREATE TABLE candidates(dataset TEXT,variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,score REAL,
                              PRIMARY KEY(dataset,variant,query_id,rank));
    ''')
    comparisons=[]
    for root,index,queries,known in cases:
        dataset=root.name
        with readonly(index) as db, readonly(root/'results.sqlite') as frozen:
            configure(db)
            expected={(label,int(q['id'])):frozen.execute(
                'SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',
                ('baseline' if label=='previous' else 'spans-fallback',int(q['id']))).fetchall()
                for label,_ in VARIANTS for q in queries}
            for trial in range(args.trials):
                for q in queries:
                    qid,query=int(q['id']),q['query']
                    rotation=(qid+trial)%len(VARIANTS)
                    for label,ranked in VARIANTS[rotation:]+VARIANTS[:rotation]:
                        start=perf_counter()
                        rows=ranked(db,query,.5)
                        hits=render(db,query,rows)
                        elapsed=perf_counter()-start
                        assert hits==expected[label,qid],(dataset,label,qid,'top50 changed')
                        results.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(dataset,label,qid,trial,elapsed))
                        if trial==0:
                            results.executemany('INSERT INTO candidates VALUES(?,?,?,?,?,?)',
                                ((dataset,label,qid,rank,doc,score) for rank,(doc,score) in enumerate(rows,1)))
                        else:
                            stored=results.execute('SELECT document FROM candidates WHERE dataset=? AND variant=? AND query_id=? ORDER BY rank',(dataset,label,qid)).fetchall()
                            assert [doc for doc,_ in rows]==[doc for doc, in stored],(dataset,label,qid,'unstable top100')
                results.commit()
                print(dataset,'trial',trial,'complete',flush=True)
        for q in queries:
            qid=int(q['id'])
            def rows(label):
                return results.execute('SELECT document,score FROM candidates WHERE dataset=? AND variant=? AND query_id=? ORDER BY rank',(dataset,label,qid)).fetchall()
            reference,optimized=rows('reference'),rows('optimized')
            assert [doc for doc,_ in reference]==[doc for doc,_ in optimized],(dataset,qid,'top100 differs')
            for (_,a),(_,b) in zip(reference,optimized):
                assert isclose(a,b,rel_tol=1e-13,abs_tol=1e-15),(dataset,qid,a,b)
            comparisons.append((dataset,qid,q['query'],len(reference),max((abs(a-b) for (_,a),(_,b) in zip(reference,optimized)),default=0)))
    tsv(args.output/'equivalence.tsv',('dataset','id','query','identical_top_candidates','max_absolute_score_difference'),comparisons)
    summary,byquery=[],[]
    for root,index,queries,known in cases:
        dataset=root.name
        for label,_ in VARIANTS:
            for subset,qids in [('all',{int(q['id']) for q in queries}),('known',known)]:
                times=[seconds*1000 for qid,seconds in results.execute('SELECT query_id,seconds FROM runs WHERE dataset=? AND variant=? AND trial>0',(dataset,label)) if qid in qids]
                summary.append((dataset,subset,label,len(qids),len(times),median(times),sorted(times)[int(.95*(len(times)-1))],max(times)))
            for q in queries:
                times=[s*1000 for s, in results.execute('SELECT seconds FROM runs WHERE dataset=? AND variant=? AND query_id=? AND trial>0',(dataset,label,int(q['id'])))]
                byquery.append((dataset,label,q['id'],q['query'],median(times),min(times),max(times)))
    tsv(args.output/'summary.tsv',('dataset','subset','variant','queries','warm_runs','median_ms','p95_ms','max_ms'),summary)
    tsv(args.output/'by-query.tsv',('dataset','variant','id','query','median_ms','min_ms','max_ms'),byquery)
    assert results.execute('PRAGMA integrity_check').fetchone()==('ok',)
    results.close()
    # Profile after timing; cProfile samples are not used as latency measurements.
    root,index,queries,_=cases[-1]
    for label,ranked in VARIANTS:
        with readonly(index) as db:
            configure(db)
            profiler=cProfile.Profile()
            profiler.enable()
            for q in queries:
                render(db,q['query'],ranked(db,q['query'],.5))
            profiler.disable()
        profiler.dump_stats(str(args.output/(label+'.prof')))
        with (args.output/(label+'-profile.txt')).open('w') as stream:
            pstats.Stats(profiler,stream=stream).strip_dirs().sort_stats('cumulative').print_stats(40)
    print('Finished',args.output,flush=True)


if __name__=='__main__':
    main()
