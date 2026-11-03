"""Offline independent pair/triple weighting checks; no Lean or BPE package."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_compound_lengths import build, split_compounds, weighted_retrieve
from tests.benchmark_search_long_tokens import Tokenizer
from tests.benchmark_search_token_weights import build_compounds, weighted_retrieve as uniform_retrieve


class CompoundLengthTests(unittest.TestCase):
    def test_split_keeps_pair_and_triple_multiplicity_and_unicode(self):
        pair,triple = split_compounds('αEqBeta α_eq_beta')
        decode = lambda field: [bytes.fromhex(t[5:]).decode() for t in field.split()]
        self.assertEqual(decode(pair),['αeq_beta','α_eq','eq_beta'])
        self.assertEqual(decode(triple),['α_eq_beta'])
        pair,triple = split_compounds('toOrderHom toOrderHom')
        self.assertEqual(len(pair.split()),4)
        self.assertEqual(len(triple.split()),2)

    def test_separate_weights_preserve_uniform_control_and_boost_only_matching_length(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.sqlite'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',[
                    (1,'Target.pi_eq_zero','',''),(2,'Other.pi_eq','zero',''),
                    (3,'Other.eq_zero','pi',''),(4,'Words','pi eq zero',''),
                    *[(i,'Unrelated'+str(i),'','') for i in range(5,30)]])
            build_compounds(source,root/'uniform.sqlite')
            build(source,root/'lengths.sqlite')
            with sqlite3.connect(root/'uniform.sqlite') as old, sqlite3.connect(root/'lengths.sqlite') as new:
                for weight in (1,2,4,8):
                    for query in ('pi_eq_zero','pi_eq','pi','Target.pi_eq_zero','the and'):
                        self.assertEqual(weighted_retrieve(new,query,weight,weight),uniform_retrieve(old,query,weight))
                expression = ' OR '.join('"'+t+'"' for t in Tokenizer('compounds')('pi_eq_zero').split())
                scores = [dict(new.execute('SELECT rowid,rank FROM terms WHERE terms MATCH ? AND rank MATCH ?',
                            (expression,f'bm25(3,2,1,3,2,1,{3*w},{2*w},{w})'))) for w in (1,4)]
                self.assertLess(scores[1][1],scores[0][1])
                self.assertEqual(scores[1][2],scores[0][2])
                self.assertEqual(scores[1][3],scores[0][3])
                self.assertEqual(scores[1][4],scores[0][4])
                self.assertEqual(weighted_retrieve(new,'pi_eq',1,1),weighted_retrieve(new,'pi_eq',1,8))


if __name__ == '__main__':
    unittest.main()
