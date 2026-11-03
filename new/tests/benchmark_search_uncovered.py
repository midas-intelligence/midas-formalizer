"""Exact experimental span scoring over all BM25 postings, without Lean/JSON.

Select longest matching non-overlapping compounds, breaking equal-length ties
left-to-right. A selected span owns length/N of its query identifier's budget.
Uncovered components receive partial_discount/N each. If no compound matches,
retain the previous 50% mean-lexical fallback, including the whole camel token.
"""
import argparse
from pathlib import Path
import sqlite3
from time import perf_counter

from midas.tools import bm25
from tests.benchmark_search_compound_fallback import fallback_retrieve
from tests.benchmark_search_identifier_budgets import expression, groups, normalized_retrieve
from tests.benchmark_search_long_tokens import CAMEL, evaluate
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv


def spans(word):
    parts = CAMEL.sub(r'\1_\2',word).casefold().split('_')
    compounds = []
    for length in (3,2):
        for start in range(len(parts)-length+1):
            group = parts[start:start+length]
            if all(group):
                token = 'longx'+'_'.join(group).encode().hex()
                compounds.append((token,start,start+length))
    return parts,compounds


class CoverScore:
    """One bounded accumulator per (candidate, query identifier)."""
    def __init__(self):
        self.parts = {}
        self.compounds = {}
        self.lexical = 0.
        self.n = 0
        self.fallback = 1.
        self.partial = 0.

    def step(self,n,start,end,score,fallback,partial):
        self.n,self.fallback,self.partial = n,fallback,partial
        if start < 0:
            self.lexical += score
        elif end-start == 1:
            self.parts[start] = score
        else:
            self.compounds[start,end] = score

    def finalize(self):
        if not self.compounds:
            return self.fallback*self.lexical
        covered = set()
        score = 0.
        for (start,end),value in sorted(self.compounds.items(),key=lambda item: (-(item[0][1]-item[0][0]),item[0][0])):
            span = set(range(start,end))
            if not span & covered:
                covered.update(span)
                score += (end-start)*value/self.n
        score += self.partial*sum(value for pos,value in self.parts.items() if pos not in covered)/self.n
        return score


def uncovered_ranked(db,query,partial_discount,limit=bm25.CANDIDATES):
    units = list(groups(query))
    if not any(compounds for _,_,compounds in units):
        from tests.benchmark_search_identifier_budgets import coefficients,ranked
        return ranked(db,coefficients(query,'identifier'),limit)
    tokens,uses = {},[]
    def use(token,unit,n,start,end,weight,fallback):
        token_id = tokens.setdefault(token,len(tokens))
        uses.append((token_id,unit,n,start,end,weight,fallback))
    for unit,(word,lexical,compounds) in enumerate(units):
        parts,locations = spans(word)
        n = sum(bool(part) for part in parts)
        fallback = .5 if compounds else 1.
        for token in lexical:
            use(token,unit,n,-1,-1,1/len(lexical),fallback)
        if compounds:
            for token,start,end in locations:
                use(token,unit,n,start,end,1.,fallback)
            if partial_discount:
                for pos,token in enumerate(parts):
                    if token and token not in bm25.STOP:
                        use(token,unit,n,pos,pos+1,1.,fallback)
    # Read each unique term's complete posting stream once. A small VALUES table
    # describes how that token participates in each unit; no JSON encodings.
    posting_sql = ' UNION ALL '.join('SELECT rowid,? AS token,bm25(terms,3,2,1,3,2,1,3,2,1) AS score FROM terms WHERE terms MATCH ?' for _ in tokens)
    params = [value for token,id in tokens.items() for value in (id,expression([token]))]
    values = ','.join('(?,?,?,?,?,?,?)' for _ in uses)
    params.extend(value for use_row in uses for value in use_row)
    db.create_aggregate('cover_score',6,CoverScore)
    sql = ('WITH postings AS MATERIALIZED ('+posting_sql+'), '
           'uses(token,unit,n,start,end,weight,fallback) AS (VALUES '+values+'), '
           'per_unit AS (SELECT p.rowid,u.unit,cover_score(u.n,u.start,u.end,p.score*u.weight,u.fallback,?) AS score '
           'FROM postings p JOIN uses u ON p.token=u.token GROUP BY p.rowid,u.unit) '
           'SELECT rowid,sum(score) AS total FROM per_unit GROUP BY rowid '
           'HAVING total<0 ORDER BY total,rowid LIMIT ?')
    return db.execute(sql,(*params,partial_discount,limit)).fetchall()


def uncovered_retrieve(db,query,partial_discount):
    rows = uncovered_ranked(db,query,partial_discount)
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
    parser.add_argument('--queries',type=Path)
    parser.add_argument('--targets',type=Path)
    parser.add_argument('--trials',type=int,default=3)
    args = parser.parse_args()
    if args.trials<2 or bool(args.queries)!=bool(args.targets):
        parser.error('Use at least two trials and supply both queries and targets overrides')
    inputs = read_tsv(args.previous/'inputs.tsv')
    for row in inputs:
        assert digest(Path(row['path']))==row['sha256'],row['input']
    paths = {row['input']:Path(row['path']) for row in inputs}
    if args.queries:
        paths.update(queries=args.queries,targets=args.targets)
        inputs = [row for row in inputs if row['input'] not in ('queries','targets')]
        inputs.extend(dict(input=key,path=str(path.resolve()),sha256=digest(path)) for key,path in [('queries',args.queries),('targets',args.targets)])
    indices = read_tsv(args.previous/'indices.tsv')
    for row in indices:
        assert digest(Path(row['path']))==row['sha256'],row['kind']
    index = next(Path(row['path']) for row in indices if row['kind']=='compounds')
    args.output.mkdir(parents=True,exist_ok=False)
    inputs.append(dict(input='uncovered',path=str(Path(__file__).resolve()),sha256=digest(Path(__file__))))
    tsv(args.output/'inputs.tsv',('input','path','sha256'),((r['input'],r['path'],r['sha256']) for r in inputs))
    tsv(args.output/'indices.tsv',('kind','path','sha256'),((r['kind'],r['path'],r['sha256']) for r in indices))
    queries = read_tsv(paths['queries'])
    variants = [('baseline',None),('spans-only',0.),('spans-fallback',.5)]
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    old = readonly(args.previous/'results.sqlite')
    for trial in range(args.trials):
        for label,partial in (variants if trial%2==0 else list(reversed(variants))):
            with readonly(index) as db:
                for row in queries:
                    qid,query = int(row['id']),row['query']
                    start = perf_counter()
                    hits = fallback_retrieve(db,query,.5) if partial is None else uncovered_retrieve(db,query,partial)
                    elapsed = perf_counter()-start
                    results.execute('INSERT INTO runs VALUES(?,?,?,?)',(label,qid,trial,elapsed))
                    if trial==0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',((label,qid,rank,id,name) for rank,(id,name) in enumerate(hits,1)))
                        if label=='baseline' and not args.queries:
                            assert hits==old.execute("SELECT document,name FROM hits WHERE variant='fallback-0.5' AND query_id=? ORDER BY rank",(qid,)).fetchall(),qid
                    else:
                        assert hits==results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall(),(label,qid)
                    if qid%25==0:
                        print(label,'trial',trial,'query',qid,flush=True)
            results.commit()
            print('Replayed',label,'trial',trial,flush=True)
    evaluate(results,queries,read_tsv(paths['targets']),[label for label,_ in variants],args.output)
    assert results.execute('PRAGMA integrity_check').fetchone()==('ok',)
    old.close()
    results.close()
    print('Finished:',args.output/'summary.tsv',flush=True)


if __name__=='__main__':
    main()
