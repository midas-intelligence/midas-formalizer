"""Offline guards for the BM25 weight experiment; no Lean or BPE dependency."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_long_tokens import Tokenizer, build, compound_tokens, retrieve
from tests.benchmark_search_token_weights import build_compounds, weighted_retrieve


class TokenWeightTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root/'source.sqlite'
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
            db.executemany('INSERT INTO documents VALUES(?,?,?,?)',[
                (1,'Target.pi_eq','pi_eq unrelated text','eq'),
                (2,'Other.eq_pi','eq pi','pi_eq'),
                (3,'Separate.pi','pi eq','pi'),
                (4,'Camel.toOrderHom','order hom',''),
                *[(i,'Unrelated'+str(i),'padding words','') for i in range(5,150)]])
        build(self.source,self.root/'old.sqlite',Tokenizer('compounds'))
        build_compounds(self.source,self.root/'split.sqlite')

    def test_unit_weight_preserves_scores_rankings_and_exact_lookup(self):
        with sqlite3.connect(self.root/'old.sqlite') as old, sqlite3.connect(self.root/'split.sqlite') as split:
            for query in ('pi_eq','pi','eq pi','toOrderHom','Target.pi_eq','the and'):
                self.assertEqual(weighted_retrieve(split,query,1),retrieve(old,query,Tokenizer('compounds')))
            expression = ' OR '.join('"'+t+'"' for t in Tokenizer('compounds')('pi_eq').split())
            before = dict(old.execute('SELECT rowid,bm25(terms,3,2,1) FROM terms WHERE terms MATCH ?',(expression,)))
            after = dict(split.execute('SELECT rowid,bm25(terms,3,2,1,3,2,1) FROM terms WHERE terms MATCH ?',(expression,)))
            self.assertEqual(before,after)
            self.assertEqual(weighted_retrieve(split,'_root_.Target.pi_eq',8)[0],(1,'Target.pi_eq'))
            self.assertEqual(weighted_retrieve(split,'the and',8),[])

    def test_only_compound_matching_scores_receive_boost(self):
        expression = ' OR '.join('"'+t+'"' for t in Tokenizer('compounds')('pi_eq').split())
        with sqlite3.connect(self.root/'split.sqlite') as db:
            scores = [dict(db.execute('SELECT rowid,rank FROM terms WHERE terms MATCH ? AND rank MATCH ?',
                                     (expression,f'bm25(3,2,1,{3*w},{2*w},{w})'))) for w in (1,2,4,8)]
            self.assertTrue(all(scores[i+1][1] < scores[i][1] for i in range(3)))
            self.assertTrue(all(s[3] == scores[0][3] for s in scores))
            # Retrieval must apply the configured weight in its ranked query.
            for weight,score in zip((1,2,4,8),scores):
                self.assertEqual([id for id,_ in weighted_retrieve(db,'pi_eq',weight)],
                                 sorted(score,key=score.get))

    def test_piece_threshold_uses_surface_length_and_retains_base_words(self):
        class Processor:
            def encode(self, word):
                return [0,1,2,3]
            def is_unknown(self, id):
                return id == 0
            def id_to_piece(self, id):
                return ['unknown','eq','_mul','alpha'][id]
        for minimum,expected in [(2,('bpex1','bpex2','bpex3')),(4,('bpex2','bpex3')),(5,('bpex3',)),(6,())]:
            tokenizer = Tokenizer('append',minimum=minimum)
            tokenizer.processor = Processor()
            tokenizer.word = tokenizer._word
            self.assertEqual(tokenizer.word('eq'),expected)
            self.assertEqual(tokenizer('eq').split(),['eq',*expected])


if __name__ == '__main__':
    unittest.main()
