"""Offline usage-calibrated context selection, without provider or Lean calls."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from midas.domain import DomainError
from midas.policy import Config, ModelRoute, load_config
from midas.prompt import Prompt, Section
from midas.prompt.budget import byte_budget, input_bytes, window
from midas.prompt.protocol import obj, schema_bytes


def prompt():
    return Prompt('prover', 'fixture', 'fixture', b'instructions', schema_bytes(obj()), (
        Section('source', 'source', b'source'*20),
        *(Section('evidence', f'page-{n}', str(n).encode()*1000) for n in range(4)),
        Section('candidate', 'proof', b'proof'*20),
        Section('question', 'question', b'Continue.')))


class BudgetTests(unittest.TestCase):
    def test_configuration_round_trip_and_validation(self):
        route = ModelRoute(provider='azure', model='test', input_token_budget=100000)
        config = replace(Config(), prover=route)
        self.assertEqual(load_config(config.encode()), config)
        self.assertIsNone(ModelRoute().input_token_budget)
        for invalid in (0, -1, True, '1000', 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(DomainError):
                replace(route, input_token_budget=invalid)

    def test_null_budget_keeps_all_pages(self):
        value = prompt()
        self.assertIs(window(value, None), value)

    def test_cold_window_keeps_newest_whole_pages_and_all_required_material(self):
        value = prompt()
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            result = window(value, 3300)
        self.assertEqual([s for s in result.sections if s.kind == 'evidence'], list(value.sections[3:5]))
        for section in value.sections:
            if section.kind != 'evidence':
                self.assertIn(section, result.sections)
        self.assertIn(b'2 older evidence page(s) omitted', result.sections[-2].text)
        self.assertLessEqual(input_bytes(result), byte_budget(3300))

    def test_reported_token_density_changes_the_window(self):
        value = prompt()
        self.assertIs(window(value, 3000, (10000, 2500)), value)
        result = window(value, 3300, (10000, 10000))
        self.assertEqual(len([s for s in result.sections if s.kind == 'evidence']), 2)
        self.assertEqual(byte_budget(10000, (40000, 10000)), 36000)

    def test_wire_tracks_only_declarations_in_the_selected_prompt(self):
        from midas.prompt import Options, Snapshot, build
        value = prompt()
        value = replace(value, sections=tuple(replace(section, declarations=(section.name,))
                                             for section in value.sections))
        wire = build(Snapshot(value.sections), Options('codex', 'fixture'),
                     renderer=lambda _: value, input_token_budget=3300)
        self.assertEqual(wire.declarations, ('source', 'page-2', 'page-3', 'proof', 'question'))
        self.assertNotIn(b'EVIDENCE page-0', wire.wire)

    def test_budget_cannot_silently_hide_newest_result_or_required_context(self):
        with self.assertRaisesRegex(DomainError, 'Input token budget cannot fit'):
            window(prompt(), 1000)
        value = replace(prompt(), sections=(Section('source', 'source', b'x'*4000),))
        with self.assertRaisesRegex(DomainError, 'Input token budget cannot fit'):
            window(value, 1000)

    def test_notice_labels_do_not_conflict(self):
        value = prompt()
        value = replace(value, sections=(*value.sections, Section('feedback', 'evidence-window', b'Other notice.')))
        result = window(value, 3000)
        self.assertEqual(len({s.name for s in result.sections}), len(result.sections))

    def test_window_feedback_precedes_prover_memory(self):
        value = prompt()
        memory = Section('memory', 'memory', b'Lesson next-plan:\nUse induction.')
        value = replace(value, sections=(*value.sections[:-1], memory, value.sections[-1]))
        result = window(value, 3300)
        kinds = [s.kind for s in result.sections]
        self.assertLess(kinds.index('feedback'), kinds.index('memory'))
        self.assertEqual(result.sections[-1], value.sections[-1])
        self.assertLessEqual(input_bytes(result), byte_budget(3300))


if __name__ == '__main__':
    unittest.main()
