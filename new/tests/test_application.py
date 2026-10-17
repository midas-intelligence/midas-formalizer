"""Application contracts using real storage, executor, provider and controller."""
from dataclasses import fields, replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import time
import unittest
from unittest.mock import patch

from midas.application import Application
from midas.checker import Checker
from midas.checker.service import Advisory, Binding
from midas.domain import (AdmittedBinding, AdmittedCheck, ArtifactRef, CheckKind, DeliveryCertainty,
    ObligationIdentity, OwnerVersion, ResultStatus, Termination, Usage, WorkerResult)
from midas.domain import encoding as codec
from midas.policy import Config, ModelRoute, Prices
from midas.storage import RunLocked, open_reader
from tests.test_checker import certificate, selection
from tests.test_strategy import model_json, proof, response
from tests.check_application_lean import submission


class Compiler(Checker):
    """Deterministic compiler fixture; no Lean process or kernel claims in tests."""
    calls = []

    def __init__(self, environment, cache):
        super().__init__(environment, cache)
        self.runtime = SimpleNamespace(id='a'*64)

    def execute(self, frozen, context):
        from midas.checker import CompilerRequest
        request = CompilerRequest(context.reader.read_artifact(frozen.operation_payload, max_bytes=64*1024**2))
        value, packet = request.decode(), request.decode()['packet']
        self.calls.append(value['operation'])
        refs = []
        def save(data):
            ref = context.staging.write_bounded(data, max_bytes=64*1024**2)
            refs.append(ref)
            return ref
        if value['operation'] == 'binding':
            iface = save(codec.encode(dict(constant=packet['constant'], signature=packet['signature'])))
            evidence = save(codec.encode(dict(interface=iface.to_dict())))
        elif value['operation'] == 'proof':
            evidence = save(codec.encode(certificate(packet, request.sha256)))
        elif value['operation'] == 'lookup':
            evidence = save(codec.encode(dict(request=request.sha256, records=[
                dict(query=name, status='available', source_range=None) for name in packet['names']])))
        elif value['operation'] == 'probe':
            from midas.application.records import pack
            cases = [save(codec.encode(dict(request=request.sha256, row=dict(result=dict(
                id=case['id'], operation=case['operation'], status='closed_exploratory', goals=[],
                contains_sorry=False, unresolved_metavariables=False, inspection={}), messages=[]))))
                for case in packet['cases']]
            evidence = save(pack([ref.to_dict() for ref in cases]))
        else:
            from midas.application.records import pack
            evidence = save(pack(dict(phases=[])))
        return WorkerResult(context.invocation_id, context.request_hash, ResultStatus.SUCCEEDED,
            tuple(refs), evidence, None, Usage(0, 0, 0, 0), DeliveryCertainty.DELIVERED, Termination.TERMINATED)

    def admit_receipt(self, request, invocation, result, receipt, reader, authority, *, candidate=None):
        assert authority.verify_receipt(receipt, invocation, result)
        value = request.validate_for(invocation.request, reader)
        packet = value['packet']
        if value['operation'] == 'binding':
            ident = ObligationIdentity(*(codec.encode(packet['signature'][k]) for k in ('target', 'universes', 'context')),
                value['environment'], tuple(codec.encode(v) for v in packet['prerequisite_interfaces']))
            iface = ArtifactRef.from_dict(codec.decode(reader.read_artifact(result.output, max_bytes=65536))['interface'])
            owner = OwnerVersion(*packet['bound_owner']) if packet.get('bound_owner') else OwnerVersion(ident.id, value['owner'][1])
            admitted = AdmittedBinding(owner, value['environment'], iface, receipt)
            return Binding(admitted, ident, (), result.output)
        if value['operation'] == 'proof':
            return AdmittedCheck(candidate, CheckKind.CONDITIONAL if candidate.logical_dependencies else CheckKind.DIRECT,
                                 receipt, result.output)
        return Advisory(value['operation'], result.output, receipt)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.selected = selection()
        self.environment = SimpleNamespace(id=self.selected.environment_id, directory=self.root/'package',
            toolchain_id='c'*64, roots={},
            manifest=SimpleNamespace(modules=(), settings=SimpleNamespace(version='4.35.0-rc3', options=(), plugins=(), variables=())),
            selection_inputs=lambda: self.selected.inputs if hasattr(self.selected, 'inputs') else self.inputs())
        self.app = Application(self.root/'cache', checker_factory=Compiler)
        self.app.environment = lambda *args, **kwargs: self.environment
        Compiler.calls = []

    def inputs(self):
        from midas.domain import SelectionInputs, SourceBinding
        return SelectionInputs(self.environment.id, (SourceBinding('Main.lean', sha256(self.selected.source).hexdigest(), len(self.selected.source)),))

    def tearDown(self):
        self.temp.cleanup()

    def start(self, name='run', **kwargs):
        run = self.app.start(self.root/name, package=self.environment, selection=self.selected,
                             workflow=kwargs.pop('workflow', 'supplied'), **kwargs)
        def strict(reducer):
            def apply(*args):
                try:
                    return reducer(*args)
                except (ValueError, KeyError) as error:
                    raise AssertionError('Unexpected workflow reduction error') from error
            return apply
        run.controller.operations = replace(run.controller.operations, **{field.name:
            replace(op, reduce=strict(op.reduce)) for field in fields(run.controller.operations)
            if (op := getattr(run.controller.operations, field.name)) is not None})
        return run

    def finish(self, run):
        deadline = time.monotonic()+5
        for _ in range(1000):
            run.step()
            view = run.workflow.progress(run.writer)
            if not run.executor.state().running and not view.ready and not view.waiting:
                return run.workflow.policy.completion(view)
            if time.monotonic() > deadline:
                break
            time.sleep(.001)
        self.fail(str(run.writer.status_snapshot()))

    def test_supplied_runs_binding_proof_integration_and_verification(self):
        with self.start() as run:
            run.submit(submission(self.selected))
            result = self.finish(run)
            self.assertEqual(result.exit_code, 0, result)
            self.assertEqual(Compiler.calls, ['binding', 'proof', 'integrate', 'audit'])
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 0)
            run.materialize(self.root/'export')
            run.writer.validate(force_artifacts=True)
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(self.finish(run).exit_code, 0)
        self.assertEqual(self.app.inspect(self.root/'run').completion.exit_code, 0)

    def test_scripted_model_shares_supplied_candidate_path(self):
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, reviewer=route)
        action = proof()
        action['proof_body'] = 'by trivial'
        with self.start(workflow='scripted', config=config, script=(('prover', response(action)),)) as run:
            result = self.finish(run)
            self.assertEqual(result.exit_code, 0, result)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 1)
            self.assertEqual(Compiler.calls, ['binding', 'proof', 'integrate', 'audit'])

    def test_protocol_rejection_reaches_next_model_prompt(self):
        from midas.application import records as r
        from midas.provider import ModelOutput, Route
        from midas.strategy import Memory
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses',
                           prompt_cache='explicit')
        invalid = (
            ('recall', dict(mode='recall', rationale='Inspect the definition.', result_id='Nat.add'),
             b'recall requires an advertised saved result_id'),
            ('research', dict(mode='research', rationale='Find the definition.', question='What is Nat.add?', urls=[]),
             b'missing_research_configuration'),
            ('page', dict(mode='page', rationale='Read the next page.', cursor='missing:1'),
             b'page requires an advertised cursor'),
        )
        for name, action, expected in invalid:
            with self.subTest(action=name):
                calls = []
                def transport(request, raw, *args):
                    calls.append(raw)
                    if len(calls) == 1:
                        reply = response(action)
                    else:
                        self.assertIn(b'Action rejected: '+expected, raw)
                        self.assertIn(b'Correct the action using the current CAPABILITIES', raw)
                        reply = response(proof())
                    return ModelOutput(raw=reply, usage=Usage(0, 0, 0, 0))
                self.app.transports = {Route.from_config(route).scope: transport}
                with self.start(name=name, workflow='live', config=replace(Config(), prover=route, reviewer=route)) as run:
                    self.assertEqual(self.finish(run).exit_code, 0)
                    self.assertEqual(len(calls), 2)
                    owner = run.workflow.graph(run.writer).nodes[0].owner.obligation_id
                    _, state = r.owner_state(run.writer, owner)
                    memory = Memory.load(r.read(run.writer, state.memory))
                    self.assertIn(expected.decode(), memory.observations[0].messages[0])
                    self.assertEqual(run.writer.counter('owner:'+owner, 'protocol'), 1)
                    self.assertEqual(run.writer.counter('owner:'+owner, 'retrieval'), 0)
                    self.assertEqual(run.writer.counter('owner:'+owner, 'research'), 0)
                    self.assertEqual(calls[0].partition(b'QUESTION next-action')[0],
                                     calls[1].partition(b'FEEDBACK observation-1')[0])

    def test_inspect_does_not_open_services_or_claim_writer(self):
        with self.start() as run:
            with patch.object(self.app, 'environment', side_effect=AssertionError('unexpected environment')):
                result = self.app.inspect(self.root/'run')
                self.assertEqual(result.workflow, 'supplied')
                self.assertEqual(len(self.app.nodes(self.root/'run')), 1)
            with self.assertRaises(RunLocked):
                self.app.resume(self.root/'run')

    def test_submission_is_frozen_before_dispatch_and_survives_resume(self):
        with self.start() as run:
            payload = submission(self.selected)
            run.submit(payload)
            with self.assertRaises(ValueError):
                run.submit(payload)
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(self.finish(run).exit_code, 0)

    def test_large_candidate_uses_no_json_operations(self):
        with self.start() as run:
            payload = submission(self.selected, 'by\n'+ '-- padding\n'*20000+'trivial')
            with patch('json.dumps', side_effect=AssertionError('JSON serialization')):
                run.submit(payload)
            self.assertGreater(len(payload), 100000)

    def test_live_transport_uses_the_same_controller_and_candidate_workflow(self):
        from midas.provider import ModelOutput, Route
        from midas.status import record, terminal
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses',
                           prices=Prices(2_000_000, 500_000, 6_000_000, 3_000_000))
        action = proof()
        action['proof_body'] = 'by trivial'
        calls = []
        def transport(request, raw, setup, cancel, deadline):
            calls.append(request.wire.sha256)
            return ModelOutput(raw=response(action), usage=Usage(100, 20, 10), cache_write_tokens=10)
        self.app.transports = {Route.from_config(route).scope: transport}
        with self.start(workflow='live', config=replace(Config(), prover=route, reviewer=route)) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(run.writer.status_snapshot().accounting.actual, Usage(100, 20, 10, 240))
            run.writer.validate(force_artifacts=True)
        view = self.app.inspect(self.root/'run')
        self.assertEqual(record(view)['usage']['cost_microusd'], 240)
        self.assertIn('cost: $0.000240', terminal(view))
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(run.writer.status_snapshot().accounting.actual.cost_microusd, 240)
        self.assertEqual(len(calls), 1)

    def test_finite_budget_with_unknown_projection_returns_blocked(self):
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, run=replace(Config().run, max_input_tokens=100))
        with self.start(workflow='scripted', config=config, script=(('prover', response(proof())),)) as run:
            result = run.run()
            self.assertEqual(result.exit_code, 1)
            self.assertIn('unknown_usage_bound:input_tokens', result.reasons)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 0)

    def test_retry_costs_are_charged_once_per_execution(self):
        from midas.controller.records import run_usage
        from midas.provider import ModelOutput, Route
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses',
                           prices=Prices(2_000_000, 500_000, 6_000_000))
        action = proof()
        action['proof_body'] = 'by trivial'
        calls = []
        def transport(request, *args):
            calls.append(request.wire)
            return (ModelOutput(error='timeout', phase='reading', delivery=DeliveryCertainty.UNKNOWN,
                                usage=Usage(10, 0, 2)) if len(calls) == 1 else
                    ModelOutput(raw=response(action), usage=Usage(100, 20, 10)))
        self.app.transports = {Route.from_config(route).scope: transport}
        config = replace(Config(), prover=route, reviewer=route,
                         recovery=replace(Config().recovery, unknown_retries=1, backoff_ms=1))
        with self.start(workflow='live', config=config) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], calls[1])
            accounting = run.writer.status_snapshot().accounting
            self.assertEqual(accounting.turns, 2)
            self.assertEqual(accounting.actual, Usage(110, 20, 12, 262))
            self.assertEqual(run_usage(run.writer, 0).budget.cost_microusd, 262)
            run.writer.validate(force_artifacts=True)
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(run.writer.status_snapshot().accounting.actual.cost_microusd, 262)
        self.assertEqual(len(calls), 2)

    def test_fork_has_lineage_and_reuses_evidence_without_compiler_or_model_calls(self):
        with self.start() as run:
            run.submit(submission(self.selected))
            self.assertEqual(self.finish(run).exit_code, 0)
            parent = run.writer.run_id
        Compiler.calls = []
        with self.app.fork(self.root/'run', self.root/'child') as child:
            self.assertEqual(child.writer.frozen_run().parent_run_id, parent)
            self.assertEqual(self.finish(child).exit_code, 0)
            self.assertEqual(child.writer.status_snapshot().accounting.turns, 0)
            self.assertEqual(Compiler.calls, [])
        (self.root/'run').rename(self.root/'moved-parent')
        with self.app.resume(self.root/'child') as child:
            self.assertEqual(self.finish(child).exit_code, 0)
        with self.app.fork(self.root/'child', self.root/'grandchild') as child:
            self.assertEqual(self.finish(child).exit_code, 0)
            self.assertEqual(Compiler.calls, [])

    def test_policy_increase_is_prospective_and_does_not_change_frozen_config(self):
        with self.start() as run:
            frozen = run.writer.frozen_run().configuration
        with self.app.resume(self.root/'run', increases={'run': {'turns': 401}}) as run:
            self.assertEqual(run.workflow.policy.config.run.turns, 401)
            self.assertEqual(run.writer.frozen_run().configuration, frozen)
        with self.assertRaises(ValueError):
            self.app.resume(self.root/'run', increases={'run': {'turns': 1}})

    def test_cache_diagnostic_retains_unknown_telemetry(self):
        from midas.provider import ModelOutput, Route
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses',
                           prices=Prices(2_000_000, 500_000, 6_000_000))
        calls = []
        def transport(*args):
            calls.append(1)
            return ModelOutput(raw=b'{"ok":true}', usage=Usage(100, 0, 5))
        self.app.transports = {Route.from_config(route).scope: transport}
        result = self.app.cache_probe(self.root/'cache-probe', config=replace(Config(), prover=route))
        self.assertEqual(len(calls), 4)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['observation'], 'unknown')
        with open_reader(self.root/'cache-probe') as reader:
            self.assertEqual(reader.status_snapshot().accounting.actual.cost_microusd, 920)

    def test_received_model_is_adopted_without_a_second_model_charge(self):
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        action = proof()
        action['proof_body'] = 'by trivial'
        run = self.start(workflow='scripted', config=replace(Config(), prover=route, reviewer=route),
                         script=(('prover', response(action)),))
        with patch.object(run.controller, '_accept', return_value=0):
            for _ in range(100):
                run.step()
                if run.writer.pending_receipts():
                    break
                time.sleep(.001)
            self.assertTrue(run.writer.pending_receipts())
            run.close()
        with self.app.resume(self.root/'run') as resumed:
            self.assertEqual(self.finish(resumed).exit_code, 0)
            self.assertEqual(resumed.writer.status_snapshot().accounting.turns, 1)

    def test_search_lookup_and_probe_deliver_readable_evidence_and_continue(self):
        from midas.application import helpers, records as r
        from midas.tools import Match, SearchService
        self.app.search_factory = lambda: SearchService(transport=lambda *args: (Match(('Nat.add',)),))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        search = dict(mode='search', rationale='Find addition.', query='addition', backend=dict(name='leanexplore', prefixes=[]))
        lookup = dict(mode='lookup', rationale='Inspect addition.', names=['Nat.add'], inspect=True)
        probe = dict(mode='probe', rationale='Try the proof.', cases=[dict(id='try',
            code='Lean.Elab.Tactic.evalTactic (← `(tactic| trivial))'),
            dict(id='second', code='Lean.Elab.Tactic.evalTactic (← `(tactic| exact True.intro))')])
        action = proof()
        action['proof_body'] = 'by trivial'
        with self.start(workflow='scripted', config=replace(Config(), prover=route, reviewer=route),
            script=tuple(('prover', response(a)) for a in (search, lookup, probe, action))) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            scope = 'owner:'+self.selected.identities[0].id
            self.assertEqual(run.writer.counter(scope, 'retrieval'), 2)
            self.assertEqual(run.writer.counter(scope, 'probe'), 2)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 4)
            owner = run.workflow.graph(run.writer).nodes[0].owner
            pages = [helpers.page_text(r.read(run.writer, a.pages[0], 16000)).decode()
                     for a in helpers.advertised(run.writer, owner)]
            self.assertEqual(len(pages), 3)
            self.assertIn('Search (leanexplore): addition\n', pages[0])
            self.assertIn('\nNat.add\n', pages[0])
            self.assertIn('Probe try (tactic): closed_exploratory (advisory)', pages[2])
            self.assertIn('Probe second (tactic): closed_exploratory (advisory)', pages[2])
            self.assertIn('try.code (line 1 below):\nrun_tac do\n  '+probe['cases'][0]['code'], pages[2])
            self.assertNotRegex('\n'.join(pages), r'(?m)^\d+(?:/[^:]+)?:')

    def test_resume_rejects_selected_proof_that_differs_from_its_receipt(self):
        from midas.application.service import commit
        from midas.application import records as r
        from midas.storage import Changes, NodeChange
        from midas.controller import Effect
        with self.start() as run:
            run.submit(submission(self.selected))
            self.assertEqual(self.finish(run).exit_code, 0)
            saved = run.writer.node(self.selected.identities[0].id)
            source = run.writer.put(b'by sorry', max_bytes=100)
            changed = replace(saved.node, selected=replace(saved.node.selected,
                              candidate=replace(saved.node.selected.candidate, source=source)))
            commit(run.writer, 'tamper', Effect(Changes(nodes=(NodeChange(saved.version, changed),),
                graph_revision=run.writer.status_snapshot().graph_revision)))
        with self.assertRaisesRegex(ValueError, 'proof differs'):
            self.app.resume(self.root/'run')

    def test_displayed_tool_pages_and_retrieval_choices_do_not_overlap(self):
        from midas.application import records as r
        from midas.application.helpers import recall_effect
        from midas.application.service import commit
        from midas.application.workflow import Buffer
        from midas.controller import Effect, artifact
        from midas.prompt import Options, prover
        from midas.strategy import Work
        from midas.tools import Scope, prepare_archive
        for large in (False, True):
            with self.subTest(large=large), self.start(name='large' if large else 'small') as run:
                flow, reader = run.workflow, run.writer
                owner = flow.graph(reader).nodes[0].owner
                _, state = r.owner_state(reader, owner.obligation_id)
                buffer = Buffer()
                declaration = 'def useful : Nat := 1'
                archive = prepare_archive(Scope(owner, flow.selection.environment_id, state.context),
                    'a'*64, (declaration, 'x'*30000) if large else (declaration,), (state.context,), buffer)
                saved = artifact(archive.payload)
                state = replace(state, evidence=(archive.pages[0], archive.pages[0]))
                commit(reader, 'tool-result', r.merge(Effect(artifacts=(*buffer.artifacts, saved)),
                    r.record(reader, 'application.archives', owner.obligation_id, [r.ref(saved.reference)]),
                    r.save_owner(reader, owner.obligation_id, state)))
                snapshot = replace(flow.snapshot(reader, owner), workflow='live')
                display = flow.display(reader, snapshot)
                results = [s for s in prover.render(display).sections if s.kind == 'evidence']
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].name, 'tool-result-1')
                self.assertIn(f'Result ID: {archive.id}\nPage 1 of {len(archive.pages)}.'.encode(), results[0].text)
                self.assertEqual(results[0].text.count(declaration.encode()), 1)
                self.assertEqual(snapshot.allowed_pins, (archive.id,))
                self.assertEqual(snapshot.recallable, ())
                self.assertEqual(snapshot.cursors, tuple(f'{archive.id}:{i}' for i in range(1, len(archive.pages))))
                work = flow.strategy.plan(snapshot, flow.policy).work[0]
                prepared = flow.strategy.prepare_model(snapshot, flow.policy, work, display, Options('codex', 'fixture'))
                capabilities = next(s.text for s in prepared.wire.snapshot.sections if s.kind == 'capabilities')
                self.assertNotIn(f'recall result_id={archive.id}'.encode(), capabilities)
                for cursor in snapshot.cursors:
                    self.assertIn(f'page cursor={cursor}'.encode(), capabilities)
                if not large:
                    self.assertIn(b'Complete result included below.', results[0].text)
                    self.assertNotIn(b'Saved evidence not included', capabilities)

                # A replayed delivery must not append the page again.
                recall = Work('recall', owner, detail=r.pack([archive.id, prepared.turn.id]))
                unchanged, _ = recall_effect(flow, reader, snapshot, prepared.turn, recall, state)
                self.assertEqual(unchanged, state)
                if large:
                    cursor = snapshot.cursors[0]
                    page = Work('page', owner, detail=r.pack([cursor, prepared.turn.id]))
                    updated, _ = recall_effect(flow, reader, snapshot, prepared.turn, page, state)
                    self.assertEqual(updated.evidence, archive.pages[:2])
                    commit(reader, 'next-page', r.save_owner(reader, owner.obligation_id, updated))
                    self.assertNotIn(cursor, flow.snapshot(reader, owner).cursors)

                # Once a first page leaves the prompt, it is available for recall.
                offscreen = replace(state, evidence=())
                commit(reader, 'offscreen', r.save_owner(reader, owner.obligation_id, offscreen))
                snapshot = replace(flow.snapshot(reader, owner), workflow='live')
                self.assertEqual(snapshot.recallable, (archive.id,))
                restored, _ = recall_effect(flow, reader, snapshot, prepared.turn, recall, offscreen)
                self.assertEqual(restored.evidence, (archive.pages[0],))

    def test_retrieval_search_lookup_and_research_resume_the_prover(self):
        from midas.application import helpers, records as r
        from midas.prompt import build
        from midas.tools import Match, SearchService
        self.app.search_factory = lambda: SearchService(transport=lambda *args: (Match(('Nat.add',)),))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        retrieve = dict(mode='retrieve', rationale='Check a discovered name.', queries=['addition'], names=[],
            backends=[dict(name='leanexplore', prefixes=[])], offset=0, inspect=False)
        research = dict(mode='research', rationale='Read supporting context.', question='What proves True?', urls=[])
        action = proof()
        action['proof_body'] = 'by trivial'
        answer = model_json(dict(answer='Use True.intro.', sources=[], unresolved=[]))
        research_route = ModelRoute(provider='openai', model='fixture')
        with patch('midas.application.helpers.build', wraps=build) as research_prompt, self.start(
            workflow='scripted', config=replace(Config(), prover=route, reviewer=route, research=research_route),
            script=(('prover', response(retrieve)), ('prover', response(research)), ('research', answer),
                    ('prover', response(action)))) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            scope = 'owner:'+self.selected.identities[0].id
            self.assertEqual(run.writer.counter(scope, 'retrieval'), 1)
            self.assertEqual(run.writer.counter(scope, 'research'), 1)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 4)
            self.assertEqual(Compiler.calls.count('lookup'), 1)
            owner = run.workflow.graph(run.writer).nodes[0].owner
            archive = helpers.advertised(run.writer, owner)[0]
            text = helpers.page_text(r.read(run.writer, archive.pages[0], 16000)).decode()
            self.assertIn('Search (leanexplore): addition', text)
            self.assertIn('Nat.add', text)
            self.assertNotRegex(text, r'(?m)^\d+(?:/[^:]+)?:')
            self.assertEqual(research_prompt.call_count, 1)
            display = research_prompt.call_args.args[0]
            sections = {s.name: s for s in display.sections}
            self.assertEqual(sections['question'].text, b'What proves True?')
            self.assertIn('goal', sections)
            self.assertIn('source-prefix', sections)
            self.assertIn(b'target:', sections['required-subjects'].text)
            self.assertFalse({'memory', 'history', 'evidence', 'candidate'} & {s.kind for s in display.sections})
            self.assertNotIn(self.environment.id.encode(), b'\n'.join(s.text for s in display.sections))

    def test_frozen_sources_resolve_starter_modules_in_their_admitted_roots(self):
        from midas.application.preparation import FrozenSources
        from midas.environment import File
        from midas.source import SourceFile
        from midas.tools import lookup_sources
        data = {
            'project/ChallengeDeps.lean': b'def helper := 0\n',
            'lean-pool/LeanPool/ClassificationOfSurfaces/LeanEval/ChallengeDeps.lean': b'def unrelated := 1\n',
            'dep/src/Support.lean': b'def support := 2\n',
            'other/Support.lean': b'def uncompiled := 3\n',
            'toolchain/src/lean/Init/Prelude.lean': b'def init := 4\n',
        }
        modules = {
            'ChallengeDeps': 'project/ChallengeDeps.olean',
            'LeanPool.ClassificationOfSurfaces.LeanEval.ChallengeDeps':
                'lean-pool/LeanPool/ClassificationOfSurfaces/LeanEval/ChallengeDeps.olean',
            'Support': 'dep/.lake/build/lib/lean/Support.olean',
            'Init.Prelude': 'toolchain/lib/lean/Init/Prelude.olean',
        }
        roots, files = {}, []
        for path, payload in data.items():
            target = self.root/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            root = path.split('/', 1)[0]
            roots[root] = self.root/root
            files.append(File(path, len(payload), False, sha256(payload).hexdigest()))
        environment = SimpleNamespace(id=self.environment.id, roots=roots,
            manifest=SimpleNamespace(modules=modules, inventory=SimpleNamespace(files=tuple(files))))
        sources = FrozenSources(environment)
        self.assertIn('project', sources.index.roots)
        self.assertEqual(sources.index.resolve(('ChallengeDeps',)), ('project/ChallengeDeps.lean',))
        expected = ('project/ChallengeDeps.lean',
            'lean-pool/LeanPool/ClassificationOfSurfaces/LeanEval/ChallengeDeps.lean',
            'dep/src/Support.lean', 'toolchain/src/lean/Init/Prelude.lean')
        for module, path in zip(modules, expected):
            logical = path.replace('toolchain/src/lean/', 'lean/')
            self.assertEqual(sources.index.by_module[module][0].path, logical)
            body = data[path].decode().strip()
            records = ({'query': 'helper', 'status': 'available', 'source_range': {
                'module': module, 'start': [1, 0], 'end': [1, len(body)]}},)
            result = lookup_sources(records, environment_id=environment.id, index=sources.index,
                                    prefix=SourceFile('Submission.lean', b''), reader=sources)
            self.assertEqual(result[0], body)
        self.assertIsNone(sources.index.by_path['other/Support.lean'].module)
        from midas.tools import SearchService
        self.app.search_factory = lambda: SearchService(index=sources.index, source_reader=sources)
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        search = dict(mode='search', rationale='Find a project helper.', query='helper',
                      backend=dict(name='frozen-source', prefixes=['project']))
        with self.start(workflow='scripted', config=replace(Config(), prover=route),
                        script=(('prover', response(search)), ('prover', response(proof())))) as run:
            owner = run.workflow.graph(run.writer).nodes[0].owner
            display = run.workflow.display(run.writer, run.workflow.snapshot(run.writer, owner))
            roots = next(s.text for s in display.sections if s.name == 'search-roots')
            self.assertIn(b'project', roots)
            self.assertEqual(self.finish(run).exit_code, 0)
            from midas.application import helpers, records as r
            archive = helpers.advertised(run.writer, owner)[0]
            text = helpers.page_text(r.read(run.writer, archive.pages[0], 16000))
            self.assertIn(b'Source: project/ChallengeDeps.lean:1', text)
            self.assertIn(b'def helper := 0', text)

    def test_export_usage_is_idempotent_and_malformed_usage_blocks_retry(self):
        from midas.application import records as r
        from midas.application.service import commit
        with self.start() as run:
            run.submit(submission(self.selected))
            self.assertEqual(self.finish(run).exit_code, 0)
            stage = run.workflow.stage(run.writer, 'integration')
            invocation = run.writer.invocation(stage.receipt.original_execution_id)
            result = run.writer.result(invocation.original_execution_id)
            before = r.value(run.writer, 'application.export', 'usage')
            self.assertIsNone(run.workflow.export_usage(run.writer, invocation, result))
            self.assertEqual(r.value(run.writer, 'application.export', 'usage'), before)
            bad = run.writer.put(r.pack(dict(phases=[dict(elapsed_ms='unknown', stdout=None, stderr=None)])), max_bytes=1000)
            another = replace(invocation, original_execution_id=str(uuid4()))
            changed = replace(result, invocation_id=another.original_execution_id, output=bad, staged_artifacts=(bad,))
            commit(run.writer, 'malformed-usage', run.workflow.export_usage(run.writer, another, changed))
            self.assertFalse(run.workflow.export_retry(run.writer, another))


if __name__ == '__main__':
    unittest.main()
