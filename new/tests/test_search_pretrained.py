"""Offline benchmark guards; no Lean, downloads, or optional packages required."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_pretrained import build, retrieve


class PretrainedBenchmarkTests(unittest.TestCase):
    def test_exact_names_survive_candidate_cutoff_and_duplicate_names(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.sqlite'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                db.execute('INSERT INTO documents VALUES(1,?,?,?)', ('needle', 'unrelated '*1000, ''))
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',
                    ((i, 'needle_other'+str(i), 'needle '*10, '') for i in range(2, 112)))
                db.execute('INSERT INTO documents VALUES(112,?,?,?)', ('needle', '', ''))
            build(source, root/'index.sqlite')
            with sqlite3.connect(root/'index.sqlite') as db:
                rows = retrieve(db, '_root_.needle')
                self.assertEqual(rows[0], (1, 'needle'))
                self.assertEqual(sum(name == 'needle' for _, name in rows), 1)
                self.assertLessEqual(len(rows), 50)
                self.assertEqual(retrieve(db, 'the and'), [])

    def test_each_variant_uses_matching_document_and_query_tokenization(self):
        class Pieces:
            def encode(self, text):
                return ' '.join('piececard pieceend' if word == 'cardinality' else 'piececard'
                                for word in text.split())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.sqlite'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                db.execute("INSERT INTO documents VALUES(1,'card','','')")
            for mode in ('baseline', 'replace', 'append'):
                path = root/(mode+'.sqlite')
                build(source, path, Pieces(), mode)
                with sqlite3.connect(path) as db:
                    found = retrieve(db, 'cardinality', Pieces(), mode)
                self.assertEqual(found, [] if mode == 'baseline' else [(1, 'card')])


if __name__ == '__main__':
    unittest.main()
