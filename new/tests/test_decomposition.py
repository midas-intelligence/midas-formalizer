"""Proof/guidance routing with offline compiler and provider fixtures."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from midas.application import candidates, declarations, records as r
from midas.application.service import commit
from midas.domain import AdmittedReview, ProofState, load_submission
from midas.prompt import prover, reviewer
from midas.strategy import Draft
from tests import test_application as fixtures, test_child_declarations as child_fixtures
from tests.test_strategy import model_json, proof, response


class DecompositionTests(unittest.TestCase):
    setUp = child_fixtures.ChildDeclarationsTests.setUp
    tearDown = fixtures.Contracts.tearDown
    start = fixtures.Contracts.start
    inputs = fixtures.Contracts.inputs
    until = child_fixtures.ChildDeclarationsTests.until

    def test_all_lean_children_use_only_the_original_model_turn_and_resume(self):
        for child in self.action['children']:
            child.update(proof_body='by trivial', proof='')
        with patch('midas.prompt.reviewer.render', side_effect=AssertionError('Unexpected review')):
            with self.start(workflow='scripted', config=self.config,
                    script=(('prover', response(self.action)),)) as run:
                root = run.workflow.graph(run.writer).nodes[0].owner
                self.until(run, lambda: run.writer.node(root.obligation_id).node.selected is not None)
                self.assertEqual(run.writer.status_snapshot().accounting.turns, 1)
                self.assertEqual(run.writer.counter('application.script', 'reviewer'), 0)
                for node in run.workflow.graph(run.writer).nodes:
                    self.assertIsNotNone(node.selected)
                    self.assertIsNone(run.writer.record('application.guidance', node.owner.obligation_id))
                    if node.review:
                        self.assertEqual(node.review.receipt, node.selected.receipt)
            with self.app.resume(self.root/'run') as run:
                self.assertEqual(run.writer.status_snapshot().accounting.turns, 1)
                for node in run.workflow.graph(run.writer).nodes:
                    self.assertNotEqual(node.state, ProofState.UNRESOLVED)
                    _, state = r.owner_state(run.writer, node.owner.obligation_id)
                    if state.submission:
                        saved = candidates.submission(run.writer, state.submission)
                        draft, _, _ = candidates.entries(run.writer, saved)
                        self.assertEqual(draft.review_entries, ())

    def test_mixed_children_only_english_gets_review_suggestions_and_a_prover_context(self):
        # Exercise both a checked prerequisite and a Lean child conditional on
        # the English obligation. Neither supplied proof gets a model call.
        for lean_index in (0, 1):
            with self.subTest(lean_index=lean_index):
                action = dict(self.action, children=[dict(child) for child in self.action['children']])
                lean, english = action['children'][lean_index], action['children'][1-lean_index]
                lean.update(proof_body='by trivial', proof='Optional explanation for a supplied term.')
                captured, reviews = [], []
                original_prover, original_reviewer = prover.render, reviewer.render
                def capture(snapshot):
                    captured.append(snapshot)
                    return original_prover(snapshot)
                def capture_review(snapshot):
                    reviews.append(snapshot)
                    return original_reviewer(snapshot)
                with patch('midas.prompt.prover.render', side_effect=capture), \
                     patch('midas.prompt.reviewer.render', side_effect=capture_review), self.start(
                        name='mixed-'+str(lean_index), workflow='scripted', config=self.config,
                        script=(('prover', response(action)), ('reviewer', model_json(self.review)),
                                ('prover', response(proof())))) as run:
                    root = run.workflow.graph(run.writer).nodes[0].owner
                    self.until(run, lambda: all(n.selected is not None for n in run.workflow.graph(run.writer).nodes)
                               and len(run.workflow.graph(run.writer).nodes) == 3)
                    self.assertEqual(len(captured), 2)
                    self.assertEqual(len(reviews), 1)
                    _, root_state = r.owner_state(run.writer, root.obligation_id)
                    draft = r.read(run.writer, root_state.base, 17*1024**2)
                    draft = Draft.load(draft)
                    saved = candidates.submission(run.writer, load_submission(draft.submission)['submission_id'])
                    lean_owner = saved['rows'][lean['name']]['owner']
                    english_owner = saved['rows'][english['name']]['owner']
                    self.assertEqual(captured[1].owners[0].obligation_id, english_owner)
                    self.assertEqual(reviews[0].owners[0], root)
                    self.assertIsNone(run.writer.record('application.guidance', lean_owner))
                    self.assertIsNotNone(run.writer.record('application.guidance', english_owner))
                    self.assertEqual({row.key for row in run.writer.records(declarations.SCOPE, limit=10)},
                                     {english_owner})
                    self.assertEqual(run.writer.status_snapshot().accounting.turns, 3)

    def test_resume_rejects_local_acceptance_for_an_english_guided_composition(self):
        with self.start(workflow='scripted', config=self.config,
                script=(('prover', response(self.action)), ('reviewer', model_json(self.review)),
                        ('prover', response(proof())), ('prover', response(proof())))) as run:
            root = run.workflow.graph(run.writer).nodes[0].owner
            self.until(run, lambda: run.writer.node(root.obligation_id).node.selected is not None)
            graph = run.workflow.graph(run.writer)
            node = graph.node(root)
            changed = replace(node, review=AdmittedReview(node.selected.candidate, node.selected.receipt, True))
            commit(run.writer, 'invalid-local-acceptance', r.graph_changes(run.writer, graph, graph._replace(changed)))
        with self.assertRaisesRegex(ValueError, 'Missing independent review'):
            self.app.resume(self.root/'run')

    def test_null_revision_delivers_guidance_to_already_bound_child_after_resume(self):
        self.clear_child_after_failure('Use the constructor in the new child context.')

    def test_null_revision_delivers_empty_guidance_without_an_extra_revision(self):
        self.clear_child_after_failure('')

    def clear_child_after_failure(self, guidance):
        from midas.controller import Accepted
        from midas.policy import Failure
        for child in self.action['children']:
            child.update(proof_body='by trivial', proof='', usefulness='')
        revision = dict(mode='revise', rationale='Delegate the failed child.', edits=[
            dict(path='/entries/first/proof', old=None, new=None)])
        if guidance:
            revision['edits'].append(dict(path='/children/first/proof', old=None, new=guidance))
        captured = []
        original = prover.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.prover.render', side_effect=capture):
            with self.start(workflow='scripted', config=self.config,
                    script=(('prover', response(self.action)), ('prover', response(revision)),
                            ('reviewer', model_json(self.review)), ('prover', response(proof())))) as run:
                operation = run.controller.operations.proof
                checks = []
                def reject_first(*args, **kwargs):
                    checks.append(1)
                    return Accepted(failure=Failure.REJECTION) if len(checks) == 1 else operation.admit(*args, **kwargs)
                run.controller.operations = replace(run.controller.operations,
                                                    proof=replace(operation, admit=reject_first))
                self.until(run, lambda: bool(run.writer.records('application.guidance', limit=10)))
                self.assertEqual(len(captured), 2)
                failed = next(candidates.submission(run.writer, row.key)
                              for row in run.writer.records('application.submissions', limit=10)
                              if candidates.submission(run.writer, row.key)['phase'] == 'failed')
                owner = failed['rows']['first']['owner']
                self.assertEqual(run.writer.records('application.guidance', limit=10)[0].key, owner)
            with self.app.resume(self.root/'run') as run:
                self.until(run, lambda: len(captured) == 3)
                self.assertEqual(captured[0].owners[0], captured[1].owners[0])
                self.assertEqual(captured[2].owners[0].obligation_id, owner)
                shown = next(s.text for s in captured[2].sections if s.name == 'proof-guidance')
                self.assertEqual(guidance.encode(), shown)
                self.assertEqual(run.writer.counter('application.script', 'reviewer'), 1)
                self.assertEqual(run.writer.counter('application.script', 'prover'), 3)


if __name__ == '__main__':
    unittest.main()
