import unittest

from midas.checker import evidence
from midas.checker.probe import repair_candidates
from midas.tools import expand_probe_bodies, probe_summary


def case(body):
    return expand_probe_bodies([dict(code=body)])[0]


class ProbeRepair(unittest.TestCase):
    def test_multiline_sequence_preserves_nested_proof_and_unicode(self):
        source = case('Lean.Elab.Tactic.evalTactic (← `(tactic|\n'
                      '  have α : True := by\n    exact True.intro\n  exact α))')['code']
        self.assertEqual(repair_candidates(source), (source.replace('tactic|', 'tactic| first |'),))

    def test_closer_precedes_next_do_statement(self):
        source = case('Lean.Elab.Tactic.evalTactic (← `(tactic| skip) -- end of quote\n'
                      'Lean.logInfo "next statement"')['code']
        self.assertEqual(repair_candidates(source), (source.replace('skip)', 'skip))'),))

    def test_combined_repairs_try_fewer_edits_first(self):
        source = case('Lean.Elab.Tactic.evalTactic (← `(tactic|\n'
                      '  skip\n  exact True.intro)\nLean.logInfo "finished"')['code']
        closed = source.replace('True.intro)', 'True.intro))')
        self.assertEqual(repair_candidates(source), (closed, closed.replace('tactic|', 'tactic| first |')))

    def test_inline_bullet_aligns_its_first_tactic(self):
        source = case('Lean.Elab.Tactic.evalTactic (← `(tactic| · skip\n  exact True.intro))')['code']
        self.assertEqual(repair_candidates(source), (source.replace('· skip', '·\n    skip'),))

    def test_inline_constructor_moves_into_indented_sequence(self):
        source = case('Lean.Elab.Tactic.evalTactic (← `(tactic| constructor\n'
                      '  · exact True.intro\n  · exact True.intro))')['code']
        self.assertEqual(repair_candidates(source),
                         (source.replace('tactic| constructor', 'tactic| first |\n    constructor'),))

    def test_comments_strings_chars_and_quoted_names_are_not_code(self):
        fake = 'Lean.Elab.Tactic.evalTactic (← `(tactic| skip)'
        for literal in ('-- ' + fake, '/- outer /- nested -/ ' + fake + ' -/',
                        '"' + fake + '"', 'r#"' + fake + '"#', '«' + fake + '»'):
            with self.subTest(literal=literal):
                self.assertEqual(repair_candidates(literal), ())
        prefix = 'let a := \')\'\nlet b := r#"unbalanced ) \" ("#\n'
        source = case(prefix + fake)['code']
        self.assertEqual(repair_candidates(source), (source + ')',))

    def test_dynamic_or_incomplete_syntax_is_not_guessed(self):
        for body in ('Lean.Elab.Tactic.evalTactic (← `(tactic|\n  skip\n  exact $proof))',
                     'Lean.Elab.Tactic.evalTactic (← `(tactic| run_tac do\n'
                     '  Lean.Elab.Tactic.evalTactic (← `(tactic| skip)))',
                     'Lean.Elab.Tactic.evalTactic (← `(tactic| skip',
                     'Lean.Elab.Tactic.evalTactic (← `(tactic| skip])',
                     'Lean.Elab.Tactic.evalTactic (← `(tactic| skip)\n/- unfinished',
                     'Lean.Elab.Tactic.evalTactic (← `(tactic| skip)\n"unfinished'):
            with self.subTest(body=body):
                self.assertEqual(repair_candidates(body), ())

    def test_interpolated_string_contents_are_not_rewritten(self):
        fake = 'Lean.Elab.Tactic.evalTactic (← `(tactic| skip)'
        self.assertEqual(repair_candidates('Lean.logInfo m!"' + fake + ' {1}"'), ())
        nested = 'Lean.logInfo m!"{String.length "' + fake + '"}"'
        self.assertEqual(repair_candidates(nested), ())

    def test_generated_code_stays_within_utf8_budget(self):
        source = case('Lean.Elab.Tactic.evalTactic (← `(tactic| skip)')['code']
        source += '\n-- ' + 'α' * ((16000 - len(source.encode()) - 4) // 2)
        source += ' ' * (16000 - len(source.encode()))
        self.assertEqual(len(source.encode()), 16000)
        self.assertEqual(repair_candidates(source), ())

    def test_evidence_binds_selected_code_and_rendering_uses_it(self):
        spec = case('Lean.Elab.Tactic.evalTactic (← `(tactic|\n  skip\n  exact True.intro))')
        code, = repair_candidates(spec['code'])
        row = dict(code=code, result=dict(id=spec['id'], operation='tactic', status='closed_exploratory',
            goals=[], contains_sorry=False, unresolved_metavariables=False, inspection={}), messages=[])
        result = dict(format='midas-probe-case-v1', request='digest', row=row)
        evidence.probe_for(result, spec, 'digest')
        rendered = probe_summary(spec, row)
        self.assertIn(code, rendered)
        self.assertNotIn(spec['code'], rendered)
        self.assertNotIn('repair', rendered.lower())
        for invalid in ('sorry', '', None, spec['code']):
            with self.subTest(code=invalid), self.assertRaisesRegex(ValueError, 'syntax repairs'):
                evidence.probe_for(dict(result, row=dict(row, code=invalid)), spec, 'digest')
        with self.assertRaisesRegex(ValueError, 'syntax repairs'):
            evidence.probe_for(result, dict(spec, operation='term'), 'digest')

    def test_parsed_input_source_is_displayed_exactly_with_its_locations(self):
        spec = case('Lean.logInfo "computed input"')
        text = '\n'.join(f'  have α{i} : True := True.intro' for i in range(20))
        label = spec['id'] + '.code.input1'
        source = dict(file=label, code=text)
        row = dict(result=dict(id=spec['id'], operation='tactic', status='error',
                               diagnostic=label + ':15:2: failure'), messages=[], sources=[source])
        result = dict(format='midas-probe-case-v1', request='digest', row=row)
        evidence.probe_for(result, spec, 'digest')
        rendered = probe_summary(spec, row)
        self.assertIn(label + ' (line 1 below):\n' + text, rendered)
        self.assertIn(label + ':15:2: failure', rendered)
        self.assertNotIn('omitted', rendered)
        for invalid in (None, [], [source, source], [dict(source, code=None)],
                        [dict(source, file='other.code.input1')], [dict(source, file=label + '.nested')],
                        [dict(source, file=spec['id'] + '.code.input0')]):
            with self.subTest(sources=invalid), self.assertRaisesRegex(ValueError, 'input source'):
                evidence.probe_for(dict(result, row=dict(row, sources=invalid)), spec, 'digest')


if __name__ == '__main__':
    unittest.main()
