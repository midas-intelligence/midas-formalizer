"""Compare sparse scoring against the frozen full-posting reference."""
from math import isclose
from pathlib import Path
from random import Random
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_compound_lengths import build
from tests.benchmark_search_uncovered import uncovered_ranked, uncovered_retrieve
from tests.benchmark_search_uncovered_fast import fast_ranked, fast_retrieve


class FastUncoveredTests(unittest.TestCase):
    def test_all_scores_on_overlaps_repeats_camel_stopwords_and_empty_queries(self):
        rng = Random(793)
        vocabulary = ['order','iso','fin','above','eq','dvd','pi','the','and']
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root/'source.sqlite') as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                rows = [(1,'A.order_iso','fin',''), (2,'B.order_iso_fin','',''),
                        (3,'C.the_and','',''), (4,'D.order_iso','order fin fin',''),
                        (5,'E.order_order','order iso','')]
                for doc in range(6,106):
                    parts = rng.choices(vocabulary,k=rng.randrange(1,7))
                    name = 'N'+str(doc)+'.'+'_'.join(parts)
                    rows.append((doc,name,' '.join(rng.choices(vocabulary,k=4)),''))
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',rows)
            build(root/'source.sqlite',root/'index.sqlite')
            # Opening the corpus read-only still permits connection-local TEMP.
            with sqlite3.connect((root/'index.sqlite').as_uri()+'?mode=ro',uri=True) as db:
                queries = ['orderIsoFin','order_iso_fin_above','order_order_iso_order',
                           'order__iso_fin','the_and','and_the_and','the and','',
                           'unknown_compound','A.order_iso','orderIsoFin order_iso_fin',
                           'eq dvd pi','order_iso fin eq']
                queries += ['_'.join(rng.choices(vocabulary,k=rng.randrange(2,7))) for _ in range(30)]
                for query in queries:
                    for discount in (0.,.5,1.):
                        with self.subTest(query=query,discount=discount):
                            reference = dict(uncovered_ranked(db,query,discount,limit=1000))
                            candidate = dict(fast_ranked(db,query,discount,limit=1000))
                            self.assertEqual(reference.keys(),candidate.keys())
                            for doc,score in reference.items():
                                self.assertTrue(isclose(score,candidate[doc],rel_tol=1e-13,abs_tol=1e-15),
                                                (doc,score,candidate[doc]))
                            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_temp_master WHERE name='span_replacements'").fetchone()[0],0)
                for query in ('A.order_iso','the and','eq dvd pi'):
                    self.assertEqual(fast_retrieve(db,query),uncovered_retrieve(db,query,.5))

    def test_partial_credit_includes_all_compound_candidates_across_batches(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with sqlite3.connect(root/'source.sqlite') as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',
                    [(doc,'N'+str(doc)+'.order_iso','fin' if doc%3 else 'unrelated','') for doc in range(1,1102)])
            build(root/'source.sqlite',root/'index.sqlite')
            with sqlite3.connect(root/'index.sqlite') as db:
                reference=dict(uncovered_ranked(db,'order_iso_fin',.5,limit=2000))
                candidate=dict(fast_ranked(db,'order_iso_fin',.5,limit=2000))
                self.assertEqual(reference.keys(),candidate.keys())
                for doc,score in reference.items():
                    self.assertTrue(isclose(score,candidate[doc],rel_tol=1e-13,abs_tol=1e-15))


if __name__=='__main__':
    unittest.main()
