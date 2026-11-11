"""Offline longer-token experiment; no Lean, network, or JSON operations.

Uses the extracted corpus.sqlite and frozen query/target TSVs from the earlier
search benchmark. SentencePiece is optional except for training/custom BPE runs.
All outputs are written to a fresh, flat directory. Run with --help from new/.
"""
import argparse
from functools import lru_cache
from pathlib import Path
import re
import sqlite3
from statistics import median
from time import perf_counter

from midas.tools import bm25
from tests.benchmark_search_pretrained import digest, read_tsv, readonly, tsv


IDENTIFIER = re.compile(r'[^\W]+', re.UNICODE)
CAMEL = re.compile(r'([a-z])([A-Z])')


def atoms(value):
    # Keep underscores, but do not learn across namespace dots or whitespace.
    return IDENTIFIER.findall(value.casefold())


def compound_tokens(value):
    for identifier in IDENTIFIER.findall(value):
        parts = CAMEL.sub(r'\1_\2', identifier).casefold().split('_')
        # Empty components preserve boundaries: a__b must not become a_b.
        for size in (2, 3):
            for start in range(len(parts)-size+1):
                group = parts[start:start+size]
                if all(group):
                    # Hex encoding avoids FTS splitting punctuation/collisions.
                    yield 'longx' + '_'.join(group).encode().hex()


class Tokenizer:
    def __init__(self, mode, model=None, minimum=1):
        self.mode, self.minimum = mode, minimum
        if model is not None:
            import sentencepiece as sp
            self.processor = sp.SentencePieceProcessor(model_file=str(model))
            self.word = lru_cache(maxsize=100_000)(self._word)

    def _word(self, word):
        return tuple('bpex'+str(id) for id in self.processor.encode(word)
                     if not self.processor.is_unknown(id)
                     and len(self.processor.id_to_piece(id)) >= self.minimum)

    def __call__(self, value):
        base = bm25.token_text(value)
        if self.mode == 'baseline':
            return base
        if self.mode == 'compounds':
            extra = compound_tokens(value)
        else:
            extra = (piece for word in atoms(value) if word not in bm25.STOP for piece in self.word(word))
        return ' '.join([*(() if self.mode == 'replace' else (base,)), *extra])


def train(corpus, prefix, size):
    import sentencepiece as sp
    def sentences():
        with readonly(corpus) as db:
            for fields in db.execute('SELECT name,documentation,statement FROM documents ORDER BY id'):
                for field in fields:
                    # Each lexical unit is a sentence; no oversized input strings.
                    yield from (word for word in atoms(field) if word not in bm25.STOP)
    start = perf_counter()
    sp.SentencePieceTrainer.train(
        sentence_iterator=sentences(), model_prefix=str(prefix), model_type='bpe',
        vocab_size=size, character_coverage=1.0, normalization_rule_name='identity',
        add_dummy_prefix=False, split_by_unicode_script=False, split_by_number=False,
        split_digits=False, bos_id=-1, eos_id=-1, pad_id=-1, hard_vocab_limit=False,
        shuffle_input_sentence=False, num_threads=1, max_sentence_length=16384,
        minloglevel=1)
    return perf_counter()-start


def build(corpus, path, tokenize):
    start = perf_counter()
    with sqlite3.connect(path) as db, readonly(corpus) as source:
        db.executescript('''CREATE TABLE documents(id INTEGER PRIMARY KEY,name TEXT);
          CREATE INDEX names ON documents(name);
          CREATE VIRTUAL TABLE terms USING fts5(name,documentation,statement,
            content='',tokenize='unicode61 remove_diacritics 0');''')
        db.execute("INSERT INTO terms(terms,rank) VALUES('rank',?)", (bm25.WEIGHTED_RANK,))
        count = 0
        for id, name, doc, statement in source.execute('SELECT id,name,documentation,statement FROM documents ORDER BY id'):
            fields = [tokenize(value) for value in (name,doc,statement)]
            count += sum(len(field.split()) for field in fields)
            db.execute('INSERT INTO documents VALUES(?,?)', (id,name))
            db.execute('INSERT INTO terms(rowid,name,documentation,statement) VALUES(?,?,?,?)', (id,*fields))
        db.execute("INSERT INTO terms(terms) VALUES('optimize')")
        db.commit()
        elapsed = perf_counter()-start
        db.execute("CREATE VIRTUAL TABLE temp.vocabulary USING fts5vocab(main,terms,'row')")
        unique, = db.execute('SELECT count(*) FROM vocabulary').fetchone()
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    return elapsed, path.stat().st_size, count, unique


def retrieve(db, query, tokenize):
    tokens = list(dict.fromkeys(tokenize(query).split()))
    expression = ' OR '.join('"'+word.replace('"','""')+'"' for word in tokens)
    ids = [id for id, in db.execute('SELECT rowid FROM terms WHERE terms MATCH ? ORDER BY rank LIMIT ?',
                                   (expression,bm25.CANDIDATES))] if tokens else []
    exact = [id for id, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                     (query.strip().removeprefix('_root_.'),))]
    seen, rows = set(), []
    for id in dict.fromkeys([*exact,*ids]):
        name, = db.execute('SELECT name FROM documents WHERE id=?', (id,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        rows.append((id,name))
        if len(rows) == bm25.TOTAL:
            break
    return rows


def evaluate(results, queries, targets, variants, output):
    target = {}
    for row in targets:
        target.setdefault(int(row['id']),set()).add(row['name'])
    rank, detail = {}, []
    for label in variants:
        for row in queries:
            qid = int(row['id'])
            names = [name for name, in results.execute('SELECT name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid))]
            first = next((i for i,name in enumerate(names,1) if name in target.get(qid,())),0)
            rank[label,qid] = first
            detail.append((label,qid,row['query'],len(target.get(qid,())),first))
    tsv(output/'by-query.tsv', ('variant','id','query','known_targets','first_direct'), detail)
    slices = [('all',set(target)),('nonexact',{int(row['id']) for row in queries if int(row['id']) in target and row['query'].strip().removeprefix('_root_.') not in target[int(row['id'])]}),
              ('eq-dvd-pi',{int(row['id']) for row in queries if int(row['id']) in target and set(bm25.token_text(row['query']).split()) & {'eq','dvd','pi'}})]
    slices += [(family,{int(row['id']) for row in queries if row.get('family') == family and int(row['id']) in target}) for family in sorted({row['family'] for row in queries if 'family' in row})]
    summary, paired = [], []
    for subset,qids in slices:
        if not qids:
            continue
        for label in variants:
            times = [s*1000 for qid,s in results.execute('SELECT query_id,seconds FROM runs WHERE variant=? AND trial>0',(label,)) if qid in qids]
            summary.append((subset,label,len(qids),*(sum(0<rank[label,qid]<=k for qid in qids) for k in (1,5,12,50)),
                            sum(1/rank[label,qid] if rank[label,qid] else 0 for qid in qids)/len(qids),median(times),sorted(times)[int(.95*(len(times)-1))]))
            for k in (5,12,50):
                wins = sorted(qid for qid in qids if 0<rank[label,qid]<=k and not 0<rank['baseline',qid]<=k)
                losses = sorted(qid for qid in qids if 0<rank['baseline',qid]<=k and not 0<rank[label,qid]<=k)
                paired.append((subset,label,k,len(wins),len(losses),' '.join(map(str,wins)),' '.join(map(str,losses))))
    tsv(output/'summary.tsv',('subset','variant','queries','success_1','success_5','success_12','success_50','mrr_50','warm_median_ms','warm_p95_ms'),summary)
    tsv(output/'paired.tsv',('subset','variant','cutoff','wins','losses','win_ids','loss_ids'),paired)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--queries', type=Path, required=True)
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--vocab-sizes', type=int, nargs='*', default=[8192,32768])
    parser.add_argument('--trials', type=int, default=3)
    args = parser.parse_args()
    if args.trials < 2 or any(size < 256 for size in args.vocab_sizes) or len(set(args.vocab_sizes)) != len(args.vocab_sizes):
        parser.error('Use at least two trials and distinct vocabulary sizes >= 256')
    args.output.mkdir(parents=True,exist_ok=False)
    tsv(args.output/'inputs.tsv',('input','path','sha256'),
        ((label,str(path.resolve()),digest(path)) for label,path in [('corpus',args.corpus),('queries',args.queries),('targets',args.targets),('benchmark',Path(__file__)),('bm25',Path(bm25.__file__))]))
    variants = [('baseline',Tokenizer('baseline')),('compounds',Tokenizer('compounds'))]
    training = []
    for size in args.vocab_sizes:
        prefix = args.output/('bpe'+str(size))
        seconds = train(args.corpus,prefix,size)
        model = prefix.with_suffix('.model')
        training.append((size,seconds,digest(model)))
        variants.extend([(f'bpe{size}-append',Tokenizer('append',model)),
                         (f'bpe{size}-long',Tokenizer('append',model,minimum=4))])
        print('Trained',size,'in',round(seconds,2),'seconds',flush=True)
    tsv(args.output/'training.tsv',('vocabulary_size','seconds','model_sha256'),training)
    queries = read_tsv(args.queries)
    results = sqlite3.connect(args.output/'results.sqlite')
    results.executescript('''CREATE TABLE builds(variant TEXT PRIMARY KEY,seconds REAL,bytes INTEGER,tokens INTEGER,unique_terms INTEGER);
      CREATE TABLE runs(variant TEXT,query_id INTEGER,trial INTEGER,seconds REAL,PRIMARY KEY(variant,query_id,trial));
      CREATE TABLE hits(variant TEXT,query_id INTEGER,rank INTEGER,document INTEGER,name TEXT,PRIMARY KEY(variant,query_id,rank));''')
    for label,tokenize in variants:
        stats = build(args.corpus,args.output/(label+'.sqlite'),tokenize)
        results.execute('INSERT INTO builds VALUES(?,?,?,?,?)',(label,*stats))
        results.commit()
        print('Built',label,stats,flush=True)
    tsv(args.output/'builds.tsv',('variant','seconds','bytes','tokens','unique_terms'),results.execute('SELECT * FROM builds'))
    tsv(args.output/'query-tokens.tsv',('variant','id','tokens'),((label,row['id'],tokenize(row['query'])) for label,tokenize in variants for row in queries))
    for trial in range(args.trials):
        for label,tokenize in (variants if trial%2 == 0 else list(reversed(variants))):
            with readonly(args.output/(label+'.sqlite')) as db:
                for row in queries:
                    qid,query = int(row['id']),row['query']
                    start = perf_counter()
                    hits = retrieve(db,query,tokenize)
                    elapsed = perf_counter()-start
                    results.execute('INSERT INTO runs VALUES(?,?,?,?)',(label,qid,trial,elapsed))
                    if trial == 0:
                        results.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',((label,qid,rank,id,name) for rank,(id,name) in enumerate(hits,1)))
                    else:
                        assert hits == results.execute('SELECT document,name FROM hits WHERE variant=? AND query_id=? ORDER BY rank',(label,qid)).fetchall()
            results.commit()
            print('Replayed',label,'trial',trial,flush=True)
    evaluate(results,queries,read_tsv(args.targets),[label for label,_ in variants],args.output)
    assert results.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    results.close()
    print('Finished:',args.output/'summary.tsv',flush=True)


if __name__ == '__main__':
    main()
