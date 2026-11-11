"""Reviewer-to-child declaration delivery with offline compiler/provider fixtures."""
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

from midas.application import declarations, records as r
from midas.application.service import commit
from midas.checker import CompilerRequest
from midas.domain import DeliveryCertainty, ResultStatus, Termination, Usage, WorkerResult
from midas.domain import encoding as codec
from midas.policy import Config, ModelRoute
from midas.prompt import prover
from tests import test_application as fixtures
from tests.test_checker import name
from tests.test_strategy import decomposition, model_json, proof, response


class Compiler(fixtures.Compiler):
    lookups = []
    fail_lookup = False

    def prepare(self, operation, *args, **kwargs):
        request = super().prepare(operation, *args, **kwargs)
        if operation == 'binding' and kwargs.get('statement'):
            value = request.decode()
            truth = ['const', name('True'), []]
            target = truth
            for _ in range(kwargs['statement'].count('∧')):
                target = ['app', ['app', ['const', name('And'), []], truth], target]
            value['packet']['signature']['target'] = target
            request = CompilerRequest(codec.encode(value))
        return request

    def execute(self, frozen, context):
        request = CompilerRequest(context.reader.read_artifact(frozen.operation_payload, max_bytes=r.DATA))
        value = request.decode()
        if value['operation'] != 'lookup':
            return super().execute(frozen, context)
        packet = value['packet']
        self.lookups.append(packet)
        if self.fail_lookup:
            report = context.staging.write_bounded(r.pack(dict(request=frozen.operation_payload.to_dict(),
                outcome='setup_failure', phases=[])), max_bytes=r.META)
            return WorkerResult(context.invocation_id, context.request_hash, ResultStatus.FAILED,
                (report,), report, None, Usage(), DeliveryCertainty.DELIVERED, Termination.TERMINATED)
        rows = [dict(query=query, status='unavailable', error='Unknown declaration', source_range=None)
                if query == 'Missing.name' else dict(query=query, status='available',
                    kind='def' if query == 'Nat.add' else 'theorem', type='Nat → Nat', source_range=None)
                for query in packet['names']]
        evidence = context.staging.write_bounded(codec.encode(dict(request=request.sha256, records=rows)), max_bytes=r.DATA)
        return WorkerResult(context.invocation_id, context.request_hash, ResultStatus.SUCCEEDED,
            (evidence,), evidence, None, Usage(), DeliveryCertainty.DELIVERED, Termination.TERMINATED)


class ChildDeclarationsTests(unittest.TestCase):
    start = fixtures.Contracts.start
    inputs = fixtures.Contracts.inputs
    tearDown = fixtures.Contracts.tearDown

    def setUp(self):
        fixtures.Contracts.setUp(self)
        self.app.checker_factory = Compiler
        Compiler.lookups = []
        Compiler.fail_lookup = False
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        self.config = replace(Config(), prover=route, reviewer=route)
        self.action = decomposition()
        self.action['children'][0]['statement'] = 'True ∧ True ∧ True'
        self.action['children'][1]['statement'] = 'True ∧ True'
        self.review = dict(verdict='accept', rationale='Useful intermediate steps.', findings=[], question='', urls=[],
            child_declarations=[dict(child='first', names=['Nat.add', 'Nat.add', 'Missing.name']),
                                dict(child='second', names=['Nat.add_zero', 'first'])])

    def script(self):
        return (('prover', response(self.action)), ('reviewer', model_json(self.review)),
                ('prover', response(proof())), ('prover', response(proof())))

    def until(self, run, predicate):
        for _ in range(1000):
            run.step()
            if predicate():
                return
            time.sleep(.001)
        self.fail(str(run.writer.status_snapshot()))

    def test_each_child_gets_checked_declarations_without_a_retrieval_turn_and_resumes(self):
        captured = []
        original = prover.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.prover.render', side_effect=capture):
            with self.start(workflow='scripted', config=self.config, script=self.script()) as run:
                self.until(run, lambda: bool(run.writer.records(declarations.SCOPE, limit=10)))
                self.assertEqual(len(captured), 1)
            with self.app.resume(self.root/'run') as run:
                self.until(run, lambda: len(captured) == 3)
                by_owner = {d.owners[0].obligation_id: d for d in captured[1:]}
                for saved in run.writer.records(declarations.SCOPE, limit=10):
                    record = r.value(run.writer, declarations.SCOPE, saved.key)
                    self.assertEqual(record['completed'], record['names'])
                    display = by_owner[saved.key]
                    text = b'\n'.join(s.text for s in display.sections if s.kind == 'evidence')
                    self.assertIn(b'Reviewer-selected declarations for this child', text)
                    if 'Nat.add' in record['names']:
                        self.assertIn('def Nat.add : Nat → Nat'.encode(), text)
                        self.assertIn(b'Missing.name: Unknown declaration', text)
                        self.assertLess(text.index(b'Missing.name: Unknown declaration'), text.index(b'def Nat.add :'))
                        self.assertNotIn(b'Nat.add_zero', text)
                    else:
                        self.assertIn('theorem Nat.add_zero : Nat → Nat'.encode(), text)
                        self.assertNotIn(b'Missing.name', text)
                    owner = run.writer.node(saved.key).node.owner
                    self.assertEqual(run.workflow.snapshot(run.writer, owner).usage.retrieval, 0)
                    self.assertIsNone(declarations.plan(run.workflow, run.writer, run.workflow.snapshot(run.writer, owner)))
                self.assertEqual(run.writer.counter('application.script', 'prover'), 3)
                lookup_count = len(Compiler.lookups)
                for packet in Compiler.lookups:
                    self.assertEqual(packet['names'], sorted(set(packet['names'])))
                second = next(p for p in Compiler.lookups if p['names'] == ['Nat.add_zero', 'first'])
                self.assertEqual(len(second['lookup_aliases']), 1)
                self.assertTrue(second['include_body'])
                self.assertEqual(second['body_format'], 'source')
            with self.app.resume(self.root/'run') as run:
                for saved in run.writer.records(declarations.SCOPE, limit=10):
                    owner = run.writer.node(saved.key).node.owner
                    self.assertIsNone(declarations.plan(run.workflow, run.writer, run.workflow.snapshot(run.writer, owner)))
            with self.app.fork(self.root/'run', self.root/'fork') as run:
                rows = run.writer.records(declarations.SCOPE, limit=10)
                self.assertEqual(len(rows), 2)
                for saved in rows:
                    owner = run.writer.node(saved.key).node.owner
                    self.assertIsNone(declarations.plan(run.workflow, run.writer, run.workflow.snapshot(run.writer, owner)))
            self.assertEqual(len(Compiler.lookups), lookup_count)

    def test_failed_automatic_lookup_still_delivers_the_child_problem(self):
        Compiler.fail_lookup = True
        captured = []
        original = prover.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.prover.render', side_effect=capture), self.start(
                workflow='scripted', config=self.config, script=self.script()) as run:
            self.until(run, lambda: len(captured) == 3)
            for display in captured[1:]:
                self.assertIn('goal', {s.name for s in display.sections})
                self.assertIn(b'Automatic declaration lookup failed', b'\n'.join(s.text for s in display.sections))

    def test_unknown_children_are_ignored_while_valid_child_suggestions_are_kept(self):
        self.review['child_declarations'].extend([
            dict(child='not-a-child', names=['Missing.extra']),
            dict(child='not-a-child', names=['by trivial']),
            dict(child='parent', names=['Missing.parent']),
            dict(child='first', names=['by trivial'])])
        captured = []
        original = prover.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.prover.render', side_effect=capture), self.start(
                workflow='scripted', config=self.config, script=self.script()) as run:
            self.until(run, lambda: len(captured) == 3)
            self.assertEqual(len(run.writer.records(declarations.SCOPE, limit=10)), 2)
            looked_up = {name for packet in Compiler.lookups for name in packet['names']}
            self.assertEqual(looked_up, {'Nat.add', 'Missing.name', 'Nat.add_zero', 'first'})

    def test_only_unknown_children_do_not_schedule_lookups_or_reject_the_review(self):
        self.review['child_declarations'] = [dict(child='not-a-child', names=['Nat.add'])]
        captured = []
        original = prover.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.prover.render', side_effect=capture), self.start(
                workflow='scripted', config=self.config, script=self.script()) as run:
            self.until(run, lambda: len(captured) == 3)
            self.assertFalse(run.writer.records(declarations.SCOPE, limit=10))
            self.assertFalse(Compiler.lookups)

    def test_shared_child_merges_names_once_and_recommendations_are_context_scoped(self):
        self.action['children'][0].update(statement='True ∧ True', depends_on=[])
        with self.start(workflow='scripted', config=self.config, script=self.script()) as run:
            self.until(run, lambda: bool(run.writer.records(declarations.SCOPE, limit=10)))
            rows = run.writer.records(declarations.SCOPE, limit=10)
            self.assertEqual(len(rows), 1)
            owner = run.writer.node(rows[0].key).node.owner
            _, state = r.owner_state(run.writer, owner.obligation_id)
            saved = declarations.current(run.writer, owner, state.context)
            self.assertEqual(saved['names'], ['Missing.name', 'Nat.add', 'Nat.add_zero', 'first'])
            self.assertIsNone(declarations.current(run.writer, replace(owner, generation=owner.generation+1), state.context))
            self.assertIsNone(declarations.current(run.writer, owner, replace(state.context, sha256='0'*64)))
            snapshot = run.workflow.snapshot(run.writer, owner)
            completed = dict(saved, completed=saved['names'])
            commit(run.writer, 'complete-suggestions', r.record(run.writer, declarations.SCOPE, owner.obligation_id, completed))
            self.assertIsNone(declarations.plan(run.workflow, run.writer, snapshot))


if __name__ == '__main__':
    unittest.main()
