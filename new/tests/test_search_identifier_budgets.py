"""Offline budget conservation, exact scoring and pre-cutoff normalization."""
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_compound_lengths import build, weighted_retrieve
from tests.benchmark_search_identifier_budgets import coefficients, groups, normalized_retrieve, ranked


class IdentifierBudgetTests(unittest.TestCase):
    def test_budget_counts_unmatched_terms_and_deduplicates_repeated_units(self):
        query = 'toOrderHom succ_above'
        weight = coefficients(query,'identifier')
        self.assertEqual(sum(weight.values()),2)
        for word,lexical,compounds in groups(query):
            expected = Fraction(1,len(lexical)+len(compounds))
            self.assertTrue(all(weight[t]==expected for t in (*lexical,*compounds)))
        self.assertEqual(weight,coefficients(query+' toOrderHom','identifier'))
        self.assertEqual(coefficients('the and','identifier'),{})
        self.assertEqual(coefficients('eq dvd pi','identifier'),dict.fromkeys(('eq','dvd','pi'),Fraction(1)))
        shared = coefficients('foo_bar foo_baz','identifier')
        self.assertEqual(shared['foo'],Fraction(2,3))
        self.assertEqual(sum(shared.values()),2)

    def test_compound_only_budget_preserves_lexical_weights(self):
        weight = coefficients('toOrderHom succ_above','compounds')
        for _,lexical,compounds in groups('toOrderHom succ_above'):
            self.assertTrue(all(weight[t]==1 for t in lexical))
            self.assertTrue(all(weight[t]==Fraction(1,len(compounds)) for t in compounds))

    def test_grouped_scores_match_individual_term_oracle_and_recover_beyond_cutoff(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root/'source.sqlite') as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',
                    [(i,'Noise'+str(i),'a','') for i in range(1,121)]+
                    [(121,'Target','b '+'padding '*1000,'')]+
                    [(i,'Unrelated'+str(i),'','') for i in range(122,1001)])
            build(root/'source.sqlite',root/'index.sqlite')
            with sqlite3.connect(root/'index.sqlite') as db:
                self.assertEqual(normalized_retrieve(db,'a b','raw'),weighted_retrieve(db,'a b',1,1))
                self.assertNotIn(121,[id for id,_ in ranked(db,{'a':Fraction(1),'b':Fraction(1)})])
                weights={'a':Fraction(1,3),'b':Fraction(100),'padding':Fraction(1,3)}
                actual=ranked(db,weights,limit=2000)
                oracle=defaultdict(float)
                for token,weight in weights.items():
                    for id,score in db.execute('SELECT rowid,bm25(terms,3,2,1,3,2,1,3,2,1) FROM terms WHERE terms MATCH ?',('"'+token+'"',)):
                        oracle[id]+=float(weight)*score
                self.assertEqual(set(oracle),{id for id,_ in actual})
                for id,score in actual:
                    self.assertAlmostEqual(score,oracle[id],places=10)
                self.assertEqual(actual[0][0],121)
                self.assertEqual(normalized_retrieve(db,'_root_.Target','identifier')[0],(121,'Target'))


if __name__=='__main__':
    unittest.main()
