"""Offline compiler-feedback formatting; no Lean or subprocess invocation."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from midas.prompt.diagnostics import compiler_feedback
from midas.prompt.protocol import clean_diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_each_error_keeps_first_and_last_five_lines_without_warning_details(self):
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=newline):
                first = ['Main.lean:7:2: error: unsolved goals', *[f'context {i}' for i in range(1, 19)], '⊢ α = α']
                second = ['Main.lean:30:8: error: unknown identifier', '  missing']
                data = newline.join([
                    'MIDAS_THREAD_GUARD count=1', *first, '',
                    'Main.lean:12:5: warning: deprecated', 'Warning details', '',
                    *second, '', 'MIDAS_TRACE tasks=2 syscalls=1 remaining=0 ready=1']).encode()
                result = compiler_feedback(data)
                self.assertIn(newline.join(first[:5]), result)
                self.assertIn(newline.join(first[-5:]), result)
                self.assertIn('... [10 lines omitted] ...', result)
                self.assertIn(newline.join(second), result)
                for excluded in ('context 8', 'Warning details', 'deprecated', 'MIDAS_'):
                    self.assertNotIn(excluded, result)
                self.assertNotIn('warning', result)

    def test_short_errors_are_complete_and_five_line_boundaries_do_not_overlap(self):
        for count in (1, 5, 9, 10, 11):
            with self.subTest(count=count):
                lines = ['Main.lean:1:1: error: failed', *[f'line {i}' for i in range(1, count)]]
                data = '\n'.join(lines)
                result = compiler_feedback(data.encode())
                if count <= 10:
                    self.assertEqual(result, data)
                else:
                    self.assertEqual(result, '\n'.join((*lines[:5], '... [1 lines omitted] ...', *lines[-5:])))

    def test_headers_are_reserved_and_oversized_details_do_not_block_other_excerpts(self):
        first = 'Main.lean:1:1: error: first\nsmall first detail'
        second = 'Main.lean:2:1: error: second\n' + 'β' * 600
        third = 'Main.lean:3:1: error: third\nsmall later detail'
        result = compiler_feedback('\n\n'.join((first, second, third)).encode(), max_bytes=512)
        self.assertIn(first, result)
        for header in (second.split('\n')[0], third.split('\n')[0]):
            self.assertIn(header, result)
        self.assertNotIn('β', result)
        self.assertIn('small later detail', result)
        self.assertIn('1 error(s) shown without details', result)
        self.assertLessEqual(len(result.encode()), 512)

    def test_detail_budget_covers_different_kinds_before_repeated_errors(self):
        headers = ['Main.lean:10:1: error: unsolved goals',
                   'Main.lean:10:2: error: unsolved goals',
                   'Main.lean:20:1: error: unsolved goals',
                   'Main.lean:30:1: error: Type mismatch: term has wrong type']
        details = [f'detail {i}: ' + 'x' * 180 for i in range(4)]
        data = '\n\n'.join(h+'\n'+d for h, d in zip(headers, details))
        result = compiler_feedback(data.encode(), max_bytes=850)
        self.assertIn(details[0], result)
        self.assertIn(details[3], result)
        self.assertNotIn(details[1], result)
        self.assertNotIn(details[2], result)
        self.assertEqual([result.index(h) for h in headers], sorted(result.index(h) for h in headers))
        self.assertLessEqual(len(result.encode()), 850)

    def test_detail_budget_visits_distinct_files_and_lines_before_repeated_locations(self):
        headers = ['Main.lean:10:1: error: unsolved goals',
                   'Main.lean:10:2: error: unsolved goals',
                   'Main.lean:20:1: error: unsolved goals',
                   'Other.lean:10:1: error: unsolved goals']
        details = [f'detail {i}: ' + 'x' * 250 for i in range(4)]
        data = '\n\n'.join(h+'\n'+d for h, d in zip(headers, details))
        result = compiler_feedback(data.encode(), max_bytes=1200)
        for i in (0, 2, 3):
            self.assertIn(details[i], result)
        self.assertNotIn(details[1], result)
        for header in headers:
            self.assertIn(header, result)
        self.assertLessEqual(len(result.encode()), 1200)

    def test_kind_selection_groups_unknown_names_and_uses_explicit_diagnostic_codes(self):
        for first, second in (
            ('error: Unknown identifier `foo`', 'error: Unknown identifier `bar`'),
            ('error(lean.unknownIdentifier): Missing foo', 'error(lean.unknownIdentifier): Unbound bar'),
        ):
            with self.subTest(first=first):
                headers = ['Main.lean:10:1: '+first, 'Main.lean:20:1: '+second,
                           'Main.lean:30:1: error: unsolved goals']
                details = [f'detail {i}: '+'x' * 180 for i in range(3)]
                result = compiler_feedback('\n\n'.join(h+'\n'+d for h, d in zip(headers, details)).encode(),
                                           max_bytes=800)
                self.assertIn(details[0], result)
                self.assertIn(details[2], result)
                self.assertNotIn(details[1], result)
                for header in headers:
                    self.assertIn(header, result)

    def test_same_kind_round_robin_matches_v2_location_selection_and_is_deterministic(self):
        from midas.prompt.diagnostics import _detail_order
        # Same-kind messages use v2's location rounds, rather than only one
        # distinct-location pass followed by all duplicates from the first line.
        rows = [('', 0, 0, ('Main.lean', line), 'unsolved goals')
                for line in ('1', '1', '1', '2', '2', '3', '3')]
        for _ in range(2):
            self.assertEqual(list(_detail_order(rows)), [0, 3, 5, 1, 4, 6, 2])

    def test_many_errors_and_long_unicode_headers_have_explicit_bounded_omissions(self):
        for data in (
            '\n\n'.join(f'Main.lean:{i}:1: error: failed {i}\n  detail' for i in range(1000)),
            'Main.lean:1:1: error: ' + 'α😀' * 20000 + '\n  detail\nMain.lean:2:1: error: later',
        ):
            with self.subTest(prefix=data[:60]), patch('json.dumps', side_effect=AssertionError('JSON encode')), \
                    patch('json.loads', side_effect=AssertionError('JSON decode')):
                result = compiler_feedback(data.encode(), max_bytes=512)
                self.assertLessEqual(len(result.encode()), 512)
                self.assertIn('further error(s) omitted', result)
                self.assertNotIn('\ufffd', result)

    def test_locationless_errors_and_checker_failures_are_preserved(self):
        data = (b'error: import failed\n  unknown module\n\nMIDAS_CHECK_REJECTED\n'
                b'unexpected internal error: failed to restore environment\n')
        result = compiler_feedback(data)
        self.assertIn('error: import failed\n  unknown module', result)
        self.assertIn('unexpected internal error: failed to restore environment', result)
        self.assertNotIn('MIDAS_CHECK_REJECTED', result)

    def test_secondary_sorry_axiom_footers_are_removed_after_compiler_errors(self):
        for newline in (b'\n', b'\r\n'):
            for header in (b'error: import failed', b'Main.lean:3:9: error: unsolved goals',
                           b'Main.lean:3:9: error(lean.invalidField): Invalid field'):
                for prefix in (b'', b'unexpected internal error: '):
                    for origin in (b'', b' (from MidasCandidateResult)'):
                        with self.subTest(newline=newline, header=header, prefix=prefix, origin=origin):
                            primary = header + newline + b'  original detail' + newline
                            footer = prefix + b'unapproved axiom: sorryAx' + origin
                            # Also covers a footer without a final newline.
                            data = primary + b'MIDAS_CHECK_REJECTED' + newline + footer
                            self.assertEqual(clean_diagnostics(data), primary + b'MIDAS_CHECK_REJECTED' + newline)
                            self.assertEqual(compiler_feedback(data), primary.decode().rstrip())

    def test_sorry_axiom_rejections_remain_without_preceding_compiler_errors(self):
        for footer in (b'unapproved axiom: sorryAx',
                       b'unexpected internal error: unapproved axiom: sorryAx (from MidasCandidateResult)'):
            for preceding in (b'', b'Main.lean:3:9: warning: declaration uses sorry\n',
                              b'info: elaboration finished\n', b'MIDAS_CHECK_REJECTED\n',
                              b'unexpected internal error: failed to restore environment\n'):
                with self.subTest(footer=footer, preceding=preceding):
                    data = preceding + footer + b'\n'
                    self.assertEqual(clean_diagnostics(data), data)
                    self.assertIn(footer.decode(), compiler_feedback(data))
            data = footer + b'\nMain.lean:8:2: error: a later failure\n'
            self.assertEqual(clean_diagnostics(data), data)
            self.assertIn(footer.decode(), compiler_feedback(data))

    def test_other_axioms_and_ordinary_sorry_mentions_are_preserved(self):
        primary = b'Main.lean:3:9: error: failed\n'
        for message in (b'unapproved axiom: otherAx (from MidasCandidateResult)',
                        b'unexpected internal error: unapproved axiom: otherAx',
                        b'unapproved axiom: sorryAxHelper',
                        b'Main.lean:4:1: error: Unknown identifier `sorryAx`',
                        b'  expected term: sorryAx\n  unapproved axiom: sorryAx',
                        b'unapproved axiom: sorryAx is mentioned in this explanation'):
            with self.subTest(message=message):
                data = primary + message + b'\n'
                self.assertEqual(clean_diagnostics(data), data)
                self.assertIn(message.decode(), compiler_feedback(data))

    def test_unstructured_failures_are_not_lost_and_invalid_utf8_is_bounded(self):
        for data in (b'', b'process exceeded memory limit',
                     b'crash\n' + b'\xff' * 10000):
            with self.subTest(prefix=data[:30]):
                result = compiler_feedback(data, max_bytes=512)
                self.assertLessEqual(len(result.encode()), 512)
                if len(data) < 512:
                    self.assertEqual(result, data.decode())
                else:
                    self.assertTrue(result.startswith('crash\n'))
                    self.assertIn('text truncated', result)

    def test_warnings_are_silent_even_without_error_headers(self):
        self.assertEqual(compiler_feedback(b'warning: deprecated\n  hint\n'), '')
        self.assertEqual(compiler_feedback(b'process exceeded memory limit\nwarning: deprecated\n  hint\n'),
                         'process exceeded memory limit')
        self.assertEqual(compiler_feedback(b'warning: deprecated\n  hint\nMIDAS_CHECK_TIMEOUT\nprocess timed out'),
                         'MIDAS_CHECK_TIMEOUT\nprocess timed out')
        self.assertEqual(compiler_feedback(b'warning: deprecated\n  hint\ninfo: useful\n  detail'),
                         'info: useful\n  detail')

    def test_proof_failure_feedback_survives_storage_memory_and_prompt_projection(self):
        from midas.application import presentation
        from midas.application.workflow import Workflow
        from midas.domain import ArtifactRef
        from midas.policy import Failure
        from midas.strategy import Memory, Observation
        from tests.test_domain import artifact

        # An earlier error followed by enough warnings to hide it in a suffix.
        data = ('Main.lean:17:8: error: first guard\ncontext\n⊢ first goal\n' +
                '\n'.join(f'Main.lean:{i+20}:1: warning: deprecated\n' + 'hint ' * 80 for i in range(20)) +
                '\nMain.lean:80:2: error: second guard\n⊢ second goal\n'
                'unexpected internal error: unapproved axiom: sorryAx (from MidasCandidateResult)').encode()
        evidence = artifact('receipt')
        reader = SimpleNamespace(
            result=lambda _: SimpleNamespace(diagnostics=artifact('diagnostic')),
            receipt=lambda _: SimpleNamespace(result_ref=evidence))
        invocation = SimpleNamespace(original_execution_id='execution',
            request=SimpleNamespace(job=SimpleNamespace(semantic_key='proof-job')))
        def fail_candidate(*args, diagnostics, entry_key, stage):
            self.assertEqual((entry_key, stage), ('parent', 'proof'))
            return diagnostics
        flow = SimpleNamespace(fail_candidate=fail_candidate)
        with patch('midas.application.workflow.r.value', return_value=['proof', 'submission', 'parent']), \
                patch('midas.application.workflow.candidates.submission', return_value={'current': 'proof-job'}), \
                patch('midas.application.workflow.r.read', return_value=data):
            messages = Workflow.reduce(flow, reader, invocation, SimpleNamespace(failure=Failure.REJECTION))
        observation = Observation.select('checker', 'rejection', evidence, messages)
        self.assertEqual(observation.omitted, 0)
        restored = Memory.load(Memory(observations=(observation,)).payload)
        displayed = presentation.memory(restored)[0].text.decode()
        for expected in ('17:8: error: first guard', '⊢ first goal', '80:2: error: second guard', '⊢ second goal'):
            self.assertIn(expected, displayed)
        self.assertNotIn('warning', displayed)
        self.assertNotIn('sorryAx', displayed)
        self.assertIsInstance(restored.observations[0].evidence, ArtifactRef)


if __name__ == '__main__':
    unittest.main()
