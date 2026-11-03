"""Non-overlap budgets and uncovered-component scoring; no Lean or network."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_compound_lengths import build
from tests.benchmark_search_compound_fallback import fallback_retrieve
from tests.benchmark_search_uncovered import CoverScore, spans, uncovered_ranked, uncovered_retrieve


class UncoveredTests(unittest.TestCase):
    def test_longest_span_suppresses_overlapping_pairs_and_covered_components(self):
        score=CoverScore()
        for start,end,value in [(0,2,-10),(1,3,-10),(0,3,-4),(0,1,-100),(2,3,-100),(-1,-1,-999)]:
            score.step(3,start,end,value,.5,.5)
        self.assertEqual(score.finalize(),-4)

    def test_only_uncovered_positions_receive_discounted_credit(self):
        score=CoverScore()
        for start,end,value in [(0,2,-3),(1,3,-50),(0,1,-100),(1,2,-100),(2,3,-6)]:
            score.step(3,start,end,value,.5,.5)
        # Equal-length overlapping spans select the leftmost, independently of
        # term scores. Pair gets 2/3 budget; uncovered last term gets 0.5/3.
        self.assertEqual(score.finalize(),-3)
        score.partial=0
        self.assertEqual(score.finalize(),-2)
        self.assertEqual(spans('a__b')[1],[])

    def test_sql_credits_missing_component_and_preserves_no_compound_fallback(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with sqlite3.connect(root/'source.sqlite') as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',[
                    (1,'A.order_iso','fin',''),(2,'B.order_iso','absent',''),
                    (3,'C.order_iso_fin','',''),(4,'D','order iso fin',''),
                    (5,'E','eq dvd pi',''),*[(i,'Unrelated'+str(i),'','') for i in range(6,25)]])
            build(root/'source.sqlite',root/'index.sqlite')
            with sqlite3.connect(root/'index.sqlite') as db:
                def score(token,id):
                    return db.execute('SELECT bm25(terms,3,2,1,3,2,1,3,2,1) FROM terms WHERE terms MATCH ? AND rowid=?',('"'+token+'"',id)).fetchone()[0]
                only=dict(uncovered_ranked(db,'orderIsoFin',0.))
                partial=dict(uncovered_ranked(db,'orderIsoFin',.5))
                self.assertAlmostEqual(partial[1]-only[1],score('fin',1)/6,places=10)
                self.assertEqual(partial[2],only[2])
                self.assertEqual(partial[3],only[3])
                self.assertEqual(partial[4],only[4])
                for query in ('eq dvd pi','the and'):
                    self.assertEqual(uncovered_retrieve(db,query,.5),fallback_retrieve(db,query,.5))
                self.assertEqual(uncovered_retrieve(db,'C.order_iso_fin',.5)[0],(3,'C.order_iso_fin'))


if __name__=='__main__':
    unittest.main()
