"""SQLite-only experiment guards; no Lean or isolated worker execution."""
import sqlite3
import unittest

from midas.tools import bm25
from tests.benchmark_search_fragments import (EXTRA, VARIANTS, hints, matcher,
                                             retrieve, scope)


class FragmentBenchmarkTests(unittest.TestCase):
    def database(self, rows):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT);
          INSERT INTO files VALUES(1,'lib/A.lean'),(2,'lib/B.lean');
          CREATE TABLE documents(id INTEGER PRIMARY KEY,name TEXT,file INTEGER);
          CREATE INDEX names ON documents(name);
          CREATE VIRTUAL TABLE terms USING fts5(name,documentation,statement,content='',
            tokenize='unicode61 remove_diacritics 0');''')
        db.execute("INSERT INTO terms(terms,rank) VALUES('rank',?)", (bm25.WEIGHTED_RANK,))
        for id, name, doc, statement, file in rows:
            db.execute('INSERT INTO documents VALUES(?,?,?)', (id, name, file))
            db.execute('INSERT INTO terms(rowid,name,documentation,statement) VALUES(?,?,?,?)',
                       (id, *(bm25.token_text(text) for text in (name, doc, statement))))
        scope(db, ('lib',))
        return db

    def test_compound_beats_context_and_boundaries_reject_partial_words(self):
        self.assertEqual(hints('mem_cthickening infDist'), ('mem_cthickening',))
        self.assertEqual(hints('KanComplex extension'), ('KanComplex',))
        self.assertEqual(hints('KanComplex extension', compound_only=True), ())
        self.assertEqual(hints('mem_cthickening infDist', compound_only=True), ('mem_cthickening',))
        self.assertEqual(hints('continuous function on compact set'), ())
        matches = matcher(('mem_cthickening',))
        self.assertTrue(matches('Metric.mem_cthickening_iff'))
        self.assertFalse(matches('Metric.notMem_cthickening_iff'))
        self.assertFalse(matches('Metric.mem_cthickenings_iff'))

    def test_separate_retrieval_recovers_target_beyond_bm25_cutoff(self):
        db = self.database([(1, 'N.mem_cthickening_iff', 'unrelated '*5000, '', 1)] +
            [(i, 'noise'+str(i), 'mem cthickening infDist '*10, '', 1) for i in range(2, 152)])
        query = 'mem_cthickening infDist'
        for variant in ('baseline', 'merge-only', 'rank-only'):
            rows, _ = retrieve(db, query, variant)
            self.assertNotIn('N.mem_cthickening_iff', [row[1] for row in rows])
        rows, audit = retrieve(db, query, 'fragments')
        self.assertEqual(rows[0][1], 'N.mem_cthickening_iff')
        self.assertEqual(audit, (100, 1, 1, 101))

    def test_exact_guard_survives_cutoff_scope_and_name_deduplication(self):
        db = self.database([(1, 'N.name', 'unrelated '*5000, '', 2),
                            (2, 'N.name', '', '', 2)] +
            [(i, 'other'+str(i), 'N name '*10, '', 1) for i in range(3, 153)])
        scope(db, ('lib/A.lean',))
        for variant in VARIANTS:
            rows, _ = retrieve(db, '_root_.N.name', variant)
            self.assertEqual(rows[0][:2], (1, 'N.name'))
            self.assertEqual(sum(row[1] == 'N.name' for row in rows), 1)
            self.assertLessEqual(len(rows), 50)

    def test_scope_precedes_name_candidate_limit(self):
        db = self.database([(i, 'N.needle_part_'+str(i), '', 'context', 1) for i in range(1, 101)] +
                           [(101, 'N.needle_part_target', '', 'context', 2)])
        scope(db, ('lib/B.lean',))
        rows, audit = retrieve(db, 'needle_part context', 'fragments')
        self.assertEqual([row[1] for row in rows], ['N.needle_part_target'])
        self.assertEqual(audit, (1, 1, 0, 1))
        scope(db, ('lib',))
        _, audit = retrieve(db, 'needle_part context', 'fragments')
        self.assertEqual(audit[1], EXTRA)
        self.assertLessEqual(audit[3], 100+EXTRA)

    def test_no_hint_and_merge_only_preserve_order_and_raw_scores(self):
        db = self.database([(1, 'N.apple', '', 'Nat', 1), (2, 'N.berry', '', 'Nat', 2)])
        scope(db, ('lib', 'lib/B.lean'))
        baseline, _ = retrieve(db, 'Nat', 'baseline')
        self.assertEqual(baseline[0][1], 'N.berry')
        for variant in VARIANTS:
            self.assertEqual(retrieve(db, 'Nat', variant)[0], baseline)

    def test_common_hint_plan_preserves_full_query_scores(self):
        db = self.database([(i, 'N.needle_part_'+str(i), 'context '*(i % 4), '', 1)
                            for i in range(1, 101)])
        query = 'needle_part context'
        rows, audit = retrieve(db, query, 'fragments')
        self.assertEqual(audit[1], EXTRA)
        for id, _, score, _ in rows:
            expected = -db.execute('SELECT bm25(terms,3,2,1) FROM terms WHERE terms MATCH ? AND rowid=?',
                                   ('needle OR part OR context', id)).fetchone()[0]
            self.assertEqual(score, expected)

    def test_name_phrase_does_not_promote_partial_word_matches(self):
        db = self.database([(1, 'N.notMem_cthickening_iff', '', '', 1),
                            (2, 'N.mem_cthickening_iff', 'unrelated '*50, '', 1)])
        rows, audit = retrieve(db, 'mem_cthickening infDist', 'fragments')
        self.assertEqual(audit[1], 1)
        self.assertEqual(rows[0][1], 'N.mem_cthickening_iff')


if __name__ == '__main__':
    unittest.main()
