"""Frozen BM25 integration contracts; no Lean, network or model execution."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from midas.application.helpers import page_text
from midas.domain import JobKind, OwnerVersion, ResourceLimits
from midas.tools import (Backend, Lookup, Match, Scope, Search, SearchResult,
    SourceEntry, SourceIndex, Stage, execute_search, prepare_archive, search_summary)
from midas.tools import bm25
from tests.test_tools import Artifacts


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.artifacts = Artifacts()
        self.scope = Scope(OwnerVersion('obl_'+'1'*64, 0), 'env_'+'2'*64, self.artifacts.save(b'context'))
        self.cancel, self.deadline = Event(), time.monotonic()+30

    def index(self, *files):
        return SourceIndex(self.scope.environment_id,
            tuple(SourceEntry(path, self.artifacts.save(source.encode())) for path, source in files))

    def search(self, index, query, prefixes=('Mathlib',), limit=50, *, prepared_path=None):
        action = Search(query, Backend('frozen-source', prefixes), limit)
        stage = Stage('3'*64, 0, self.scope, action)
        return execute_search(stage, self.artifacts, self.cancel, self.deadline,
                              index=index, cache_directory=self.cache, prepared_path=prepared_path)

    def isolated_search(self, index, query, *, prefixes=(), memory_mib=128, prepared_path=None, cancellation=None):
        from midas.application.preparation import operate
        stage = Stage('3'*64, 0, self.scope, Search(query, Backend('frozen-source', prefixes)))
        return operate(lambda context: execute_search(stage, self.artifacts, context.cancellation,
            context.deadline, index=index, cache_directory=self.cache, prepared_path=prepared_path,
            context=context), limits=ResourceLimits(10000, memory_mib, 1024**2, 1024**2),
            cancellation=cancellation)

    def test_isolated_search_matches_rankings_and_reuses_postings(self):
        index = self.index(('lib/Mathlib/A.lean', '/-- apple apple -/\ndef apple : Nat := 0\n'),
                           ('lib/Mathlib/B.lean', 'def other : Nat := 1\n'))
        first = self.isolated_search(index, 'apple')
        self.assertEqual(first, self.search(index, 'apple', prefixes=()))
        self.artifacts.reads.clear()
        second = self.isolated_search(index, 'apple', prepared_path=self.cache/'missing.sqlite')
        self.assertEqual(first, second)
        self.assertEqual(self.artifacts.reads, [index.entries[0].artifact])

    def test_isolated_index_memory_failure_does_not_kill_application(self):
        # A small source expands into many lexical offsets and FTS documents.
        # Exercise a real child address-space limit, not a mocked MemoryError.
        index = self.index(('lib/Mathlib/Large.lean',
                            ''.join(f'def value{i} : Nat := {i}\n' for i in range(50000))))
        with self.assertRaisesRegex(RuntimeError, '[Mm]emory|allocate'):
            self.isolated_search(index, 'value', memory_mib=64)
        self.assertFalse(list(self.cache.glob('*.sqlite*')))
        small = self.index(('lib/Mathlib/Small.lean', 'def apple : Nat := 0\n'))
        self.assertEqual(self.isolated_search(small, 'apple').records[0].names, ('apple',))

    def test_isolated_preparation_uses_existing_source_files(self):
        from midas.application.preparation import operate
        from midas.tools.search_process import prepare_index
        index = self.index(('lib/Mathlib/A.lean', 'def apple : Nat := 0\n'))
        source = self.cache/'source.lean'
        source.write_bytes(self.artifacts.data[index.entries[0].artifact.id])
        class Reader:
            def artifact_path(self, ref):
                return source
            def read_artifact(self, *args, **kwargs):
                raise AssertionError('Package sources must not be copied by the parent')
        path = self.cache/'prepared.sqlite'
        operate(lambda context: prepare_index(index, Reader(), path, context),
                limits=ResourceLimits(10000, 128, 1024**2, 1024**2))
        self.assertTrue(path.is_file())
        self.assertEqual(self.isolated_search(index, 'apple', prepared_path=path).records[0].names, ('apple',))

    def test_cancel_isolated_builder_waiting_for_lock(self):
        import fcntl
        from threading import Timer
        index = self.index(('lib/Mathlib/A.lean', 'def apple : Nat := 0\n'))
        lock_path = self.cache/(index.search_key+'.lock')
        cancel = Event()
        with lock_path.open('ab') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            timer = Timer(.3, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError, '[Cc]ancel'):
                    self.isolated_search(index, 'apple', cancellation=cancel)
            finally:
                timer.cancel()
                timer.join()
        self.assertLess(time.monotonic()-started, 3)
        self.assertFalse(list(self.cache.glob('*.sqlite*')))
        self.assertEqual(self.isolated_search(index, 'apple').records[0].names, ('apple',))

    def test_hosted_worker_protocol_uses_bounded_binary_records(self):
        import io
        from types import SimpleNamespace
        from midas.executor import Outcome
        from midas.tools.search_process import hosted_search, _main
        context = SimpleNamespace(workspace=self.cache, check=lambda: None)
        def run_search(request):
            output = io.BytesIO()
            with patch('sys.stdout', SimpleNamespace(buffer=output)):
                _main(request)
            return SimpleNamespace(outcome=Outcome.COMPLETED, stdout=SimpleNamespace(data=output.getvalue()))
        context.run_search = run_search
        for backend in ('leanexplore', 'leansearch'):
            stage = Stage('3'*64, 0, self.scope, Search('addition', Backend(backend)))
            self.assertEqual(stage.kind, JobKind.NETWORK_SEARCH)
            self.assertEqual(Stage.load(stage.payload), stage)
            with patch('midas.tools.search.hosted', return_value=(Match(('Nat.add',)),)) as hosted:
                result = SearchResult(stage.sha256, hosted_search(stage, context))
                self.assertEqual(result.validate_for(stage).records, (Match(('Nat.add',)),))
                self.assertEqual(hosted.call_args.args[0], stage.action)
                with self.assertRaises(ValueError):
                    SearchResult(stage.sha256, (Match(('Nat.add',), 'lib/X.lean', 1, 1),)).validate_for(stage)

    def test_hosted_worker_filters_before_returning_name_hints(self):
        import io
        from types import SimpleNamespace
        from midas.executor import Outcome
        from midas.tools.search_process import _main
        context = SimpleNamespace(workspace=self.cache, check=lambda: None)
        def run_search(request):
            output = io.BytesIO()
            with patch('sys.stdout', SimpleNamespace(buffer=output)):
                _main(request)
            return SimpleNamespace(outcome=Outcome.COMPLETED, stdout=SimpleNamespace(data=output.getvalue()))
        context.run_search = run_search
        payload = br'''{"results":[
            {"name":"fwdDiff_iter_eq_sum_shift", "informalization":"Iterated forward difference."},
            {"name":"Multiset.choose", "informalization":"Unique selection from a multiset."}
        ]}'''
        stage = Stage('3'*64, 0, self.scope,
                      Search('polynomial forward difference choose', Backend('leanexplore')))
        with patch('midas.tools.search.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(payload)
            result = execute_search(stage, self.artifacts, self.cancel, self.deadline, context=context)
        self.assertEqual(result.records, (Match(('fwdDiff_iter_eq_sum_shift',)),))
        self.assertEqual(SearchResult.load(result.payload), result)

    def test_tokenization_preserves_multiplicity_without_stemming(self):
        self.assertEqual(bm25.token_text('Functor.map_comp compactManifold functors objects α α the'),
            'functor map comp compactmanifold compact manifold functors objects α α')
        index = self.index(('lib/Mathlib/X.lean',
            'def singular : Functor := by exact bodiesOnly\n'
            'def plural : Functors := by exact singular\n'))
        self.assertEqual([r.names for r in self.search(index, 'functors').records], [('plural',)])
        self.assertFalse(self.search(index, 'bodiesOnly').records)
        self.assertFalse(self.search(index, 'the and').records)
        self.assertFalse(self.search(index, 'NEAR(foo) OR "bar"*').records)

    def test_compound_scoring_matches_frozen_reference_and_isolated_worker(self):
        from math import isclose
        from tests.benchmark_search_uncovered import uncovered_ranked
        from tests.benchmark_search_compound_lengths import split_compounds
        from midas.tools import bm25_score
        source = ('namespace N\n'
                  '/-- fin -/\ndef order_iso : Nat := 0\n'
                  'def order_iso_fin : Nat := 0\n'
                  '/-- order iso fin -/\ndef fallback : Nat := 0\n'
                  'def order_order_iso : Nat := 0\n'
                  'def the_and : Nat := 0\nend N\n')
        index = self.index(('lib/Mathlib/A.lean', source))
        for name, _, _, _, _, doc, header in bm25.documents(source):
            values = name, doc, header
            split = [split_compounds(v) for v in values]
            expected = (*map(bm25.token_text, values), *(p for p,t in split), *(t for p,t in split))
            self.assertEqual(bm25_score.fields(*values), expected)
        with bm25.database(index, self.artifacts, self.cancel, self.deadline, self.cache) as db:
            for query in ('orderIsoFin', 'order_iso_fin', 'order_order_iso_fin', 'the_and', 'the and'):
                reference = uncovered_ranked(db, query, .5)
                expected = [(db.execute('SELECT name FROM documents WHERE id=?', (doc,)).fetchone()[0], -score)
                            for doc,score in reference]
                result = bm25.ranked(db, query, ('lib/Mathlib',))
                self.assertEqual([r[0] for r in result], [name for name,score in expected])
                self.assertTrue(all(isclose(r[-1], score, rel_tol=1e-13, abs_tol=1e-15)
                                    for r,(_,score) in zip(result,expected)))
                self.assertFalse(db.in_transaction)
                self.assertFalse(db.execute('SELECT name FROM sqlite_temp_master').fetchall())
        self.assertEqual(self.isolated_search(index, 'orderIsoFin'),
                         self.search(index, 'orderIsoFin', prefixes=()))

    def test_compound_path_preference_promotes_before_top100_with_global_scores(self):
        from tests.benchmark_search_uncovered import uncovered_ranked
        index = self.index(('lib/Mathlib/A.lean',
                            ''.join(f'def other{i}_order_iso_fin : Nat := 0\n' for i in range(150))),
                           ('lib/Mathlib/B.lean', 'def target_order_iso_fin : Nat := 0\n'))
        self.assertNotIn(('target_order_iso_fin',), [r.names for r in self.search(index, 'order_iso_fin').records])
        result = self.search(index, 'order_iso_fin', ('Mathlib/B.lean',))
        self.assertEqual(result.records[0].names, ('target_order_iso_fin',))
        self.assertEqual(len(result.records), 50)
        with bm25.database(index, self.artifacts, self.cancel, self.deadline, self.cache) as db:
            doc, = db.execute("SELECT id FROM documents WHERE name='target_order_iso_fin'").fetchone()
            reference = uncovered_ranked(db, 'order_iso_fin', .5, limit=1000)
            self.assertGreater([id for id, score in reference].index(doc), 100)
            expected = -dict(reference)[doc]
            self.assertAlmostEqual(result.records[0].score, expected, places=13)

    def test_compound_cancellation_cleans_query_tables_and_preserves_outer_transaction(self):
        index = self.index(('lib/Mathlib/A.lean', 'def order_iso : Fin 4 := by sorry\n'))
        with bm25.database(index, self.artifacts, self.cancel, self.deadline, self.cache) as db:
            def cancel_in_scoring():
                if db.execute("SELECT 1 FROM sqlite_temp_master WHERE name='path_weights'").fetchone():
                    raise TimeoutError('cancelled compound scoring')
            with self.assertRaisesRegex(TimeoutError, 'compound scoring'):
                bm25.ranked(db, 'order_iso_fin', ('lib/Mathlib',), check=cancel_in_scoring)
            self.assertFalse(db.in_transaction)
            self.assertFalse(db.execute('SELECT name FROM sqlite_temp_master').fetchall())
            db.execute('BEGIN')
            self.assertTrue(bm25.ranked(db, 'order_iso_fin', ('lib/Mathlib',)))
            self.assertTrue(db.in_transaction)
            db.rollback()

    def test_old_index_format_rebuilds_without_modifying_prepared_package(self):
        import sqlite3
        index = self.index(('lib/Mathlib/A.lean', 'def order_iso : Fin 4 := by sorry\n'))
        path = self.cache/'prepared.sqlite'
        with patch.object(bm25, 'FORMAT', b'midas-bm25-source-v2'):
            old_key = bm25.corpus_key(index)
        self.assertNotEqual(index.search_key, old_key)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY)')
            db.execute('INSERT INTO metadata VALUES(?)', (old_key,))
            db.execute("CREATE VIRTUAL TABLE terms USING fts5(name,documentation,statement,content='')")
        before = path.read_bytes()
        self.assertEqual(self.search(index, 'order_iso_fin', prepared_path=path).records[0].names, ('order_iso',))
        self.assertEqual(path.read_bytes(), before)
        with sqlite3.connect(self.cache/(index.search_key+'.sqlite')) as db:
            self.assertEqual(len(db.execute('PRAGMA table_info(terms)').fetchall()), 9)

    def test_sqlite_interrupt_during_path_weight_write_preserves_error_and_cleans_state(self):
        import sqlite3
        index = self.index(('lib/Mathlib/A.lean', 'def order_iso : Fin 4 := by sorry\n'))
        with bm25.database(index, self.artifacts, self.cancel, self.deadline, self.cache) as db:
            state = {'armed': False, 'fired': False}
            def authorize(action, table, *args):
                if action == sqlite3.SQLITE_INSERT and table == 'path_weights':
                    state['armed'] = True
                return sqlite3.SQLITE_OK
            def interrupt():
                if state['armed'] and not state['fired']:
                    state['fired'] = True
                    return 1
                return 0
            db.set_authorizer(authorize)
            db.set_progress_handler(interrupt, 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                bm25.ranked(db, 'order_iso_fin', ('lib/Mathlib',))
            db.set_authorizer(None)
            db.set_progress_handler(None, 0)
            self.assertTrue(state['fired'])
            self.assertFalse(db.in_transaction)
            self.assertFalse(db.execute('SELECT name FROM sqlite_temp_master').fetchall())
            self.assertTrue(bm25.ranked(db, 'order_iso_fin', ('lib/Mathlib',)))

    def test_lexical_documents_bind_headers_docs_and_source_ranges(self):
        source = ('namespace N\n/-- α documentation -/\n@[simp]\n'
            'theorem result (n : Nat := 2) : n = n := by\n  rfl\n\n'
            '/-- A different declaration. -/\naxiom next : True\n'
            'end N\nnamespace Other\ndef separate : Nat := 0\nend Other\n')
        docs = list(bm25.documents(source))
        self.assertEqual([d[0] for d in docs], ['N.result', 'N.next', 'Other.separate'])
        name, line, start_line, start, end, doc, header = docs[0]
        self.assertEqual((line, start_line), (4, 2))
        self.assertIn('α documentation', doc)
        self.assertNotIn('different', doc)
        self.assertNotIn('simp', doc)
        self.assertIn('(n : Nat := 2)', header)
        self.assertNotIn('rfl', header)
        excerpt = source.encode()[start:end].decode()
        self.assertTrue(excerpt.startswith('/-- α'))
        self.assertTrue(excerpt.endswith('theorem result (n : Nat := 2) : n = n'))
        self.assertNotIn('rfl', excerpt)
        self.assertEqual(docs[1][-1], 'axiom next : True')
        self.assertEqual(bm25.statement('def complex : let x := Nat; x := 0'), '')
        self.assertEqual(bm25.statement('def unbalanced (x : Nat := 0'), '')

    def test_search_keeps_theorem_statements_and_definition_bodies(self):
        source = ('namespace N\n/-- α documentation with := and by. -/\n'
            '@[simp] theorem «résult:=» (n : Nat := 2) :\n'
            '    (let x := n; x = x) := by\n  exact theoremProof\n'
            'protected lemma consequence : True := lemmaProof\n'
            'lemma equations : ∀ n : Nat, n = n\n  | n => equationProof\n'
            'theorem complex : let x := Nat; x = x := complexProof\n'
            'def value : Nat := by\n  exact definitionBody\n'
            'abbrev abbreviation : Nat := abbreviationBody\n'
            'instance : Inhabited Nat := ⟨instanceBody⟩\nend N\n')
        index = self.index(('lib/Mathlib/X.lean', source))
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            result = self.search(index, 'Nat True')
        excerpts = {r.names[0]: r.source for r in result.records if r.names}
        self.assertEqual(excerpts['N.«résult:=»'],
            '/-- α documentation with := and by. -/\n@[simp] theorem «résult:=» (n : Nat := 2) :\n'
            '    (let x := n; x = x)')
        self.assertEqual(excerpts['N.consequence'], 'protected lemma consequence : True')
        self.assertEqual(excerpts['N.equations'], 'lemma equations : ∀ n : Nat, n = n')
        # Uncertain lexical boundaries retain a discoverable name, with checked
        # types available through lookup rather than an untrimmed proof excerpt.
        complex_result = self.search(index, 'N.complex')
        self.assertEqual(complex_result.records[0].names, ('N.complex',))
        self.assertEqual(complex_result.records[0].source, '')
        self.assertIn(':= by\n  exact definitionBody', excerpts['N.value'])
        self.assertIn(':= abbreviationBody', excerpts['N.abbreviation'])
        self.assertIn(':= ⟨instanceBody⟩', next(r.source for r in result.records if not r.names))
        display = '\n'.join(search_summary(result, Search('Nat True', Backend('frozen-source', ('Mathlib',)))))
        for proof in ('theoremProof', 'lemmaProof', 'equationProof', 'complexProof'):
            self.assertNotIn(proof, display)

    def test_identifier_queries_use_bm25_and_scores_roundtrip_losslessly(self):
        index = self.index(('lib/Mathlib/A.lean', 'namespace N\ndef result : Nat := 0\nend N\n'),
            ('lib/Mathlib/B.lean', '/-- N result result result -/\ndef other : Nat := 0\n'))
        result = self.search(index, ' _root_.N.result ', ('Mathlib', 'Mathlib/B.lean'))
        self.assertEqual(result.records[0].names, ('other',))
        self.assertEqual(result.records[1].names, ('N.result',))
        self.assertTrue(all(row.score > 0 for row in result.records))
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            self.assertEqual(SearchResult.load(result.payload), result)
        for score in (float('nan'), float('inf'), -1, True):
            with self.assertRaises(ValueError):
                Match(('N.bad',), 'lib/Mathlib/A.lean', 1, 1, '', score)

    def test_file_preference_is_soft_canonical_and_hints_do_not_stack(self):
        index = self.index(('lib/Mathlib/A.lean', 'def apple : Nat := 0\n'),
                          ('lib/Mathlib/B.lean', 'def berry : Nat := 0\n'))
        baseline = self.search(index, 'Nat')
        self.assertEqual([r.names for r in baseline.records], [('apple',), ('berry',)])
        for selector in ('Mathlib.B', 'Mathlib/B', 'lib/Mathlib/B.lean'):
            result = self.search(index, 'Nat', ('Mathlib', selector))
            self.assertEqual([r.names for r in result.records], [('berry',), ('apple',)])
            self.assertEqual([r.score for r in result.records], [r.score for r in baseline.records])
        duplicate_scope = self.search(index, 'Nat', ('Mathlib', 'lib/Mathlib'))
        self.assertEqual(duplicate_scope.records, baseline.records)
        once = self.search(index, 'Nat', ('Mathlib/B.lean',))
        repeated = self.search(index, 'Nat', ('Mathlib/B', 'Mathlib.B', 'lib/Mathlib', 'Mathlib/B.lean'))
        self.assertEqual(once.records, repeated.records)

    def test_empty_prefixes_search_all_frozen_roots(self):
        index = self.index(('lib/Mathlib/A.lean', 'def apple : Nat := 0\n'),
                          ('project/Main.lean', 'def berry : Nat := 0\n'))
        result = self.search(index, 'Nat', ())
        self.assertEqual({r.names for r in result.records}, {('apple',), ('berry',)})
        self.assertEqual(result.records, self.search(index, 'Nat', index.roots).records)
        self.assertEqual([r.names for r in self.search(index, 'Nat', ('project',)).records],
                         [('berry',), ('apple',)])

    def test_disjoint_hints_receive_equal_full_preference_at_different_depths(self):
        index = self.index(('lib/Mathlib/A/Basic.lean', 'def apple : Nat := 0\n'),
                          ('lib/Mathlib/B/C/Basic.lean', 'def berry : Nat := 0\n'),
                          ('lib/Mathlib/Other.lean', 'def other : Nat := 0\n'))
        hints = ('Mathlib/A', 'Mathlib/B/C/Basic.lean')
        expected = self.search(index, 'Nat', hints)
        self.assertEqual([r.names for r in expected.records], [('apple',), ('berry',), ('other',)])
        self.assertEqual(self.search(index, 'Nat', ('Mathlib', 'Mathlib/B', *hints)).records, expected.records)
        self.assertEqual(self.isolated_search(index, 'Nat', prefixes=hints).records, expected.records)

    def test_file_and_directory_hints_have_the_same_meaning_across_roots(self):
        index = self.index(('lib/Package/Basic.lean', 'def apple : Nat := 0\n'),
                          ('project/Package/Basic/More.lean', 'def berry : Nat := 0\n'),
                          ('project/Package/Other.lean', 'def other : Nat := 0\n'))
        expected = self.search(index, 'Nat', ('Package.Basic',))
        self.assertEqual([r.names for r in expected.records], [('apple',), ('berry',), ('other',)])
        for selector in ('Package/Basic', 'Package/Basic.lean'):
            self.assertEqual(self.search(index, 'Nat', (selector,)).records, expected.records)

    def test_missing_hints_rank_shared_components_in_direct_and_isolated_execution(self):
        index = self.index(
            ('other/Main.lean', 'def other : Nat := 0\n'),
            ('mathlib/Archive/Other.lean', 'def archive : Nat := 0\n'),
            ('mathlib/Mathlib/Analysis/Other.lean', 'def outside : Nat := 0\n'),
            ('mathlib/Mathlib/RingTheory/Polynomial/HilbertPoly.lean',
             'def hilbert : Nat := 0\n'),
            ('mathlib/Mathlib/Data/Nat/ChooseExtra.lean', 'def nearby : Nat := 0\n'),
            ('mathlib/Mathlib/Data/Nat/Choose/Basic.lean', 'def choose : Nat := 0\n'))
        requested = ('mathlib/Mathlib/RingTheory/HilbertPolynomial', 'mathlib/Mathlib/Data/Nat/Choose')
        expected = self.search(index, 'Nat', requested)
        self.assertEqual([row.names[0] for row in expected.records],
                         ['choose', 'nearby', 'hilbert', 'other', 'archive', 'outside'])
        self.assertEqual(self.search(index, 'Nat', (requested[0]+'.lean', requested[1]+'.lean')).records, expected.records)
        self.assertEqual(self.isolated_search(index, 'Nat', prefixes=requested).records, expected.records)
        baseline = self.search(index, 'Nat', ())
        self.assertEqual({row.names: row.score for row in expected.records},
                         {row.names: row.score for row in baseline.records})
        self.assertEqual(self.search(index, 'Nat', ('Unknown/Absent.lean',)).records, baseline.records)

    def test_path_preference_promotes_before_candidates_and_uses_global_idf(self):
        index = self.index(('lib/Mathlib/A.lean', ''.join(f'def needle{i} : Nat := 0\n' for i in range(150))),
                          ('lib/Mathlib/B.lean', 'def target : Nat := 0\n'))
        result = self.search(index, 'Nat', ('Mathlib/B.lean',))
        self.assertNotIn(('target',), [r.names for r in self.search(index, 'Nat').records])
        self.assertEqual(result.records[0].names, ('target',))
        self.assertEqual(len(result.records), 50)
        with bm25.database(index, self.artifacts, self.cancel, self.deadline, self.cache) as db:
            expected = -db.execute("SELECT bm25(terms,3,2,1) FROM terms WHERE terms MATCH 'nat' "
                "AND rowid=(SELECT id FROM documents WHERE name='target')").fetchone()[0]
        self.assertEqual(result.records[0].score, expected)
        self.assertEqual(self.search(index, 'needle0', ('Mathlib/B.lean',)).records[0].names, ('needle0',))

    def test_file_bonus_can_promote_from_below_fifty_and_remains_soft(self):
        index = self.index(('lib/Mathlib/A.lean', ''.join(f'def a{i:02} : Nat := 0\n' for i in range(70))),
                          ('lib/Mathlib/B.lean', ''.join(f'def b{i:02} : Nat := 0\n' for i in range(10))))
        self.assertTrue(all(r.names[0].startswith('a') for r in self.search(index, 'Nat').records))
        preferred = self.search(index, 'Nat', ('Mathlib', 'Mathlib/B.lean'))
        self.assertEqual([r.names[0] for r in preferred.records[:10]], [f'b{i:02}' for i in range(10)])
        self.assertEqual(len(preferred.records), 50)
        index = self.index(('lib/Mathlib/A.lean', 'def strong : One Two Three := by sorry\n'),
                          ('lib/Mathlib/B.lean', 'def weak : One := by sorry\n'))
        result = self.search(index, 'one two three', ('Mathlib', 'Mathlib/B.lean'))
        self.assertEqual(result.records[0].names, ('strong',))
        self.assertGreater(result.records[0].score, result.records[1].score*1.5)

    def test_module_preference_resolves_package_source_directories(self):
        index = self.index(('dep/src/Support.lean', 'def berry : Nat := 0\n'),
                          ('dep/A.lean', 'def apple : Nat := 0\n'))
        index = replace(index, entries=(replace(index.entries[0], module='Support'), index.entries[1]))
        self.assertEqual(index.resolve(('Support',)), ('dep/src/Support.lean',))
        self.assertEqual(self.search(index, 'Nat', ('dep', 'Support')).records[0].names, ('berry',))

    def test_exact_module_below_srcdir_retains_its_path_preference(self):
        index = self.index(('dep/src/Package/Support.lean', 'def berry : Nat := 0\n'),
                           ('dep/Package/Other.lean', 'def apple : Nat := 0\n'))
        index = replace(index, entries=(replace(index.entries[0], module='Package.Support'), index.entries[1]))
        self.assertEqual(index.resolve(('Package.Support',)), ('dep/src/Package/Support.lean',))
        self.assertEqual([row.names for row in self.search(index, 'Nat', ('Package.Support',)).records],
                         [('berry',), ('apple',)])

    def test_weighted_fields_prefer_name_then_documentation_then_header(self):
        index = self.index(('lib/Mathlib/X.lean',
            'def needle : Nat := 0\n/-- needle -/\ndef middle : Nat := 0\n'
            'def last : Needle := by exact needle\n'))
        # Case does not change the FTS terms.
        result = self.search(index, 'NEEDLE')
        self.assertEqual([r.names for r in result.records], [('needle',), ('middle',), ('last',)])
        self.assertTrue(all(a.score > b.score for a, b in zip(result.records, result.records[1:])))

    def test_fifty_results_twelve_sources_and_tail_lookup(self):
        index = self.index(('lib/Mathlib/X.lean', ''.join(f'def term{i:02} : Nat := {i}\n' for i in range(70))))
        result = self.search(index, 'Nat')
        self.assertEqual(len(result.records), 50)
        self.assertTrue(all(r.source for r in result.records[:12]))
        self.assertTrue(all(not r.source for r in result.records[12:]))
        self.assertEqual(len({r.names for r in result.records}), 50)
        action = Search('Nat', Backend('frozen-source', ('Mathlib',)))
        self.assertEqual(action.limit, 50)
        rows = search_summary(result, action)
        display = '\n'.join(rows)
        self.assertEqual(display.count('Source:'), 12)
        for rank, row in enumerate(result.records[12:], 13):
            self.assertIn(f'{rank}. {row.names[0]}', rows)
        self.assertEqual(Lookup(result.records[-1].names).names, ('term49',))
        archive = prepare_archive(self.scope, 'a'*64, rows, (self.scope.context,), self.artifacts)
        self.assertEqual(len(archive.pages), 1)
        self.assertIn(b'50. term49', page_text(self.artifacts.data[archive.pages[0].id]))
        self.assertEqual(len(self.search(index, 'Nat', limit=13).records), 13)
        self.assertEqual(SearchResult.load(result.payload), result)
        with self.assertRaises(ValueError):
            Search('Nat', action.backend, 51)

    def test_large_details_put_all_names_first_and_preserve_paged_excerpts(self):
        records = tuple(Match((f'N.term{i:02}',), 'lib/Mathlib/X.lean', 1, 1,
            'α'*2500 if i < 12 else '', .25) for i in range(50))
        result = SearchResult('a'*64, records)
        rows = search_summary(result, Search('term', Backend('frozen-source', ('Mathlib',))))
        archive = prepare_archive(self.scope, 'a'*64, rows, (self.scope.context,), self.artifacts)
        pages = [page_text(self.artifacts.data[p.id]).decode() for p in archive.pages]
        self.assertGreater(len(pages), 1)
        for row in records:
            self.assertIn(row.names[0], pages[0])
        self.assertEqual('\n'.join(pages).count('α'), 12*2500)
        self.assertTrue(all(p.byte_count <= 16000 for p in archive.pages))

    def test_hosted_results_still_display_names_without_claiming_source_details(self):
        result = SearchResult('a'*64, tuple(Match((f'N.term{i:02}',)) for i in range(50)))
        rows = search_summary(result, Search('term', Backend('leanexplore')))
        display = '\n'.join(rows)
        self.assertIn('50. N.term49', rows)
        self.assertNotIn('detailed', display)
        self.assertNotIn('Source:', display)

    def test_anonymous_instances_retain_source_in_tail(self):
        source = ''.join(f'def term{i:02} : Nat := 0\n' for i in range(12))+'instance : Nat := 0\n'
        index = self.index(('lib/Mathlib/X.lean', source))
        result = self.search(index, 'Nat')
        anonymous = next(r for r in result.records if not r.names)
        self.assertIn('instance : Nat', anonymous.source)
        # Exercise projection independently of where this anonymous header ranks.
        reordered = replace(result, records=tuple(r for r in result.records if r.names)+(anonymous,))
        display = '\n'.join(search_summary(reordered, Search('Nat', Backend('frozen-source', ('Mathlib',)))))
        self.assertIn('instance : Nat', display)

    def test_cache_reuses_postings_rechecks_excerpts_and_invalidates_changed_corpus(self):
        index = self.index(('lib/Mathlib/A.lean', 'def first : Nat := 0\n'),
                          ('lib/Mathlib/B.lean', 'def other : String := ""\n'))
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            first = self.search(index, 'Nat')
            self.artifacts.reads.clear()
            self.assertEqual(self.search(index, 'Nat'), first)
        self.assertEqual(self.artifacts.reads, [index.entries[0].artifact])
        changed = replace(index, entries=(replace(index.entries[0], artifact=self.artifacts.save(b'def second : Nat := 1\n')),
                                         index.entries[1]))
        self.assertEqual(self.search(changed, 'Nat').records[0].names, ('second',))
        self.assertEqual(len(list(self.cache.glob('*.sqlite'))), 2)
        self.artifacts.data[index.entries[0].artifact.id] = b'modified'
        with self.assertRaises(ValueError):
            self.search(index, 'Nat')

    def test_failed_build_is_not_published_and_corrupt_cache_is_rebuilt(self):
        index = self.index(('lib/Mathlib/X.lean', 'def needle : Nat := 0\n'))
        with patch.object(bm25, 'documents', side_effect=ValueError('interrupted build')):
            with self.assertRaisesRegex(ValueError, 'interrupted build'):
                self.search(index, 'Nat')
        self.assertFalse(list(self.cache.glob('*.sqlite')))
        result = self.search(index, 'Nat')
        next(self.cache.glob('*.sqlite')).write_bytes(b'corrupt')
        self.assertEqual(self.search(index, 'Nat'), result)
        self.cancel.set()
        with self.assertRaises(TimeoutError):
            self.search(index, 'Nat')

    def test_prepared_index_is_reused_readonly_without_a_runtime_cache(self):
        index = self.index(('lib/Mathlib/A.lean', 'def first : Nat := 0\n'),
                          ('lib/Mathlib/B.lean', 'def other : String := ""\n'))
        path = self.cache/'package'/'search.sqlite'
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            self.assertEqual(bm25.prepare(index, self.artifacts, self.cancel, self.deadline, path), path)
        self.assertEqual(self.artifacts.reads, [e.artifact for e in index.entries])
        self.artifacts.reads.clear()
        before = path.stat()
        path.chmod(0o444)
        path.parent.chmod(0o555)
        try:
            with patch.object(bm25, '_build', side_effect=AssertionError('unexpected rebuild')):
                bm25.prepare(index, self.artifacts, self.cancel, self.deadline, path)
                for run in ('first-run', 'second-run'):
                    self.cache = path.parent.parent/run
                    result = self.search(index, 'Nat', prepared_path=path)
                    self.assertEqual(result.records[0].names, ('first',))
                    self.assertFalse(self.cache.exists())
        finally:
            path.parent.chmod(0o755)
        self.assertEqual(self.artifacts.reads, [index.entries[0].artifact]*2)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_stale_missing_or_corrupt_prepared_index_uses_disposable_cache(self):
        original = self.index(('lib/Mathlib/X.lean', 'def first : Nat := 0\n'))
        path = self.cache/'package'/'search.sqlite'
        bm25.prepare(original, self.artifacts, self.cancel, self.deadline, path)
        index = self.index(('lib/Mathlib/X.lean', 'def second : Nat := 0\n'))
        before = path.read_bytes()
        self.cache = path.parent.parent/'runtime-cache'
        self.assertEqual(self.search(index, 'Nat', prepared_path=path).records[0].names, ('second',))
        self.assertEqual(path.read_bytes(), before)
        path.write_bytes(b'corrupt')
        with patch.object(bm25, '_build', side_effect=AssertionError('unexpected rebuild')):
            self.assertEqual(self.search(index, 'Nat', prepared_path=path).records[0].names, ('second',))
            path.unlink()
            self.assertEqual(self.search(index, 'Nat', prepared_path=path).records[0].names, ('second',))

    def test_concurrent_builds_publish_once(self):
        index = self.index(('lib/Mathlib/X.lean', 'def needle : Nat := 0\n'))
        entered, release = Event(), Event()
        original = bm25.documents
        def pause(source):
            entered.set()
            release.wait(5)
            return original(source)
        outputs, failures = [], []
        def run():
            try:
                outputs.append(self.search(index, 'Nat'))
            except BaseException as error:
                failures.append(error)
        with patch.object(bm25, 'documents', side_effect=pause) as extraction:
            first, second = Thread(target=run), Thread(target=run)
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            release.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(failures)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(extraction.call_count, 1)


if __name__ == '__main__':
    unittest.main()
