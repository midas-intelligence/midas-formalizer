"""Offline prompt material fidelity; no compiler, network or provider calls."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from midas.application import presentation
from midas.prompt import Snapshot, prover, subject
from midas.strategy import Lesson, Memory, Observation, expand, fragments
from tests.test_domain import artifact, uid
from tests.test_source import signature
from tests.test_strategy import context, decomposition


class PresentationTests(unittest.TestCase):
    def test_candidate_contains_dependency_map_guidance_and_exact_fragments_once(self):
        action = decomposition()
        action['proof'] = 'An English plan with ``` fences.\n\n'
        action['definitions'] = [dict(kind='abbrev', name='Shared', type='Prop', value='True')]
        ctx = context()
        draft = expand(action, ctx, uid('display-candidate'))
        before = draft.payload
        # No JSON operation is needed even for arbitrarily large authored strings.
        with patch('json.dumps', side_effect=AssertionError('JSON encode')), \
             patch('json.loads', side_effect=AssertionError('JSON decode')):
            text = presentation.candidate(draft, ctx, selected='parent').text
        self.assertEqual(draft.payload, before)
        self.assertIn(b'Selected review entry: parent', text)
        self.assertIn(b'first <- local entry first', text)
        self.assertIn(b'Definition: abbrev Shared', text)
        self.assertIn(b'Definitions in statement: Shared', text)
        self.assertIn(b'Proof: pending obligation', text)
        self.assertNotIn(b'by sorry', text)
        self.assertNotIn(uid('display-candidate').encode(), text)
        for path, raw in fragments(draft, ctx):
            self.assertEqual(text.count(b'Editable fragment: ' + path.encode() + b'\n'), 1)
            self.assertIn(presentation.exact(raw), text)
        self.assertIn(b'````\nAn English plan with ``` fences.\n\n\n````', text)
        with self.assertRaisesRegex(ValueError, 'missing'):
            presentation.candidate(draft, ctx, selected='absent')
        with self.assertRaisesRegex(ValueError, 'differs'):
            presentation.candidate(draft, ctx, selected='parent', checked=b'by trivial')
        reviewed = presentation.candidate(draft, ctx, selected='parent', checked=action['proof_body'].encode())
        self.assertEqual(reviewed.text.count(action['proof_body'].encode()), 1)

    def test_exact_string_boundaries_round_trip_large_unicode_and_empty_fields(self):
        for raw in ('', '\n', 'α\\"\n```\ntrailing\n\n' * 20000):
            with patch('json.dumps', side_effect=AssertionError('JSON encode')):
                framed = presentation.exact(raw)
            fence, _, rest = framed.partition(b'\n')
            self.assertEqual(rest[:-len(fence)-1].decode(), raw)
            self.assertNotIn(fence, raw.encode())

    def test_memory_preserves_lesson_ids_diagnostics_pins_and_omissions(self):
        old = Observation('checker', 'rejected', artifact('old'), ('old failure\n  exact line',), 2)
        latest = replace(old, status='binding_failed', messages=('latest failure\n  another line',), omitted=1)
        saved = Memory(strategy='Try an invariant.', lessons=(Lesson('invariant', 'Keep its boundary case.'),),
                       observations=(old, latest), pins=('result-a',), evicted=3)
        sections = presentation.memory(saved)
        rendered = prover.render(Snapshot((subject('goal', signature()), *sections)))
        history = next(s for s in rendered.sections if s.kind == 'history')
        feedback = next(s for s in rendered.sections if s.kind == 'feedback')
        memory = next(s for s in rendered.sections if s.kind == 'memory')
        self.assertIn(old.messages[0].encode(), history.text)
        self.assertIn(b'2 diagnostic message(s) omitted', history.text)
        self.assertIn(latest.messages[0].encode(), feedback.text)
        self.assertIn(b'1 diagnostic message(s) omitted', feedback.text)
        self.assertIn(b'Lesson invariant:\nKeep its boundary case.', memory.text)
        self.assertIn(b'Pinned evidence:\nresult-a', memory.text)
        self.assertIn(b'3 older memory record(s) evicted', memory.text)
        self.assertNotIn(old.evidence.id.encode(), history.text)


if __name__ == '__main__':
    unittest.main()
