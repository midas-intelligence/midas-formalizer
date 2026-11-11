"""Offline per-candidate gating and fixed-budget scoring tests."""
from fractions import Fraction
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_compound_lengths import build
from tests.benchmark_search_compound_fallback import fallback_ranked, fallback_retrieve
from tests.benchmark_search_identifier_budgets import expression, groups, normalized_retrieve


class CompoundFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name)
        with sqlite3.connect(root/'source.sqlite') as db:
            db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
            db.executemany('INSERT INTO documents VALUES(?,?,?,?)',[
                (1,'First.to_order','to order hom succ above pi',''),
                (2,'Parts','to order hom succ above pi',''),
                (3,'Second.succAbove','to order hom pi',''),
                (4,'Whole.toOrderHom','succ above pi',''),
                (5,'Ordinary','eq dvd pi',''),
                *[(i,'Unrelated'+str(i),'','') for i in range(6,30)]])
        build(root/'source.sqlite',root/'index.sqlite')
        self.db=sqlite3.connect(root/'index.sqlite')
        self.addCleanup(self.db.close)

    def score(self,id,terms):
        row=self.db.execute('SELECT bm25(terms,3,2,1,3,2,1,3,2,1) FROM terms WHERE terms MATCH ? AND rowid=?',
                            (expression(terms),id)).fetchone() if terms else None
        return row[0] if row else None

    def test_grouped_scores_match_candidate_and_identifier_specific_oracle(self):
        query='toOrderHom succAbove pi'
        for discount in (Fraction(0),Fraction(1,4),Fraction(1)):
            actual=dict(fallback_ranked(self.db,query,discount,limit=100))
            for id in range(1,30):
                expected=0.
                for _,lexical,compounds in groups(query):
                    compound_score=self.score(id,compounds)
                    if compound_score is not None:
                        expected+=compound_score/len(compounds)
                    else:
                        lexical_score=self.score(id,lexical)
                        if lexical_score is not None:
                            expected+=lexical_score/len(lexical)*(float(discount) if compounds else 1)
                if expected<0:
                    self.assertAlmostEqual(actual[id],expected,places=10)
                else:
                    self.assertNotIn(id,actual)

    def test_compound_matches_suppress_own_components_and_do_not_suppress_other_candidates(self):
        query='toOrderHom'
        zero=dict(fallback_ranked(self.db,query,Fraction(0)))
        quarter=dict(fallback_ranked(self.db,query,Fraction(1,4)))
        full=dict(fallback_ranked(self.db,query,Fraction(1)))
        for id in (1,4):
            self.assertEqual(zero[id],quarter[id])
            self.assertEqual(quarter[id],full[id])
        # Candidate 1 matches only one of three generated compounds: denominator
        # stays three, and lexical matches add nothing to this unit's score.
        _,lexical,compounds=next(groups(query))
        self.assertAlmostEqual(quarter[1],self.score(1,compounds)/3,places=10)
        self.assertNotIn(2,zero)
        self.assertIn(2,quarter)
        self.assertAlmostEqual(quarter[2],full[2]/4,places=10)

    def test_no_compounds_and_exact_names_keep_previous_behavior(self):
        for query in ('eq dvd pi','the and'):
            for discount in (Fraction(0),Fraction(1,4),Fraction(1)):
                self.assertEqual(fallback_retrieve(self.db,query,discount),normalized_retrieve(self.db,query,'identifier'))
        self.assertEqual(fallback_retrieve(self.db,'_root_.Whole.toOrderHom',Fraction(0))[0],(4,'Whole.toOrderHom'))


if __name__=='__main__':
    unittest.main()
