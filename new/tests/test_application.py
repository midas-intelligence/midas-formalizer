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
            assert packet['include_body'] is True and packet.get('body_format') == 'source'
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

    def test_import_display_distinguishes_active_source_and_additional_imports(self):
        from midas.controller import artifact
        from midas.domain import ByteSpan, SelectionInputs, SelectionRequest, SourceBinding, freeze_selection
        prefix = b'import ChallengeDeps\nimport Submission.Helpers\n\n'
        source = prefix + self.selected.source
        declaration = self.selected.declarations[0]
        declaration = replace(declaration,
            declaration=ByteSpan(declaration.declaration.start + len(prefix), declaration.declaration.end + len(prefix)),
            replacement=ByteSpan(declaration.replacement.start + len(prefix), declaration.replacement.end + len(prefix)))
        inputs = SelectionInputs(self.environment.id,
            (SourceBinding('Main.lean', sha256(source).hexdigest(), len(source)),))
        self.selected = freeze_selection(SelectionRequest('Main.lean', all_unfinished=True), source,
            (declaration,), frozenset(self.selected.imported_constants),
            {declaration.constant: self.selected.signatures[0]}, inputs)
        with self.start() as run:
            snapshot = run.workflow.snapshot(run.writer, OwnerVersion(self.selected.identities[0].id, 0))
            for imports, inherited in (((), ()), (('Extra.Geometry',), ()),
                                       (('Extra.Geometry',), ('Extra.Geometry',))):
                context = replace(snapshot.context, imports=imports, inherited_imports=inherited)
                version = replace(snapshot.version, context=artifact(context.payload).reference)
                display = run.workflow.display(run.writer, replace(snapshot, context=context, version=version))
                sections = {s.name: s.text for s in display.sections}
                self.assertEqual(sections['source-prefix'], prefix)
                self.assertIn(b'imports in SOURCE source-prefix are already active', sections['imports'])
                self.assertEqual(b'Additional prepared modules available for import: Extra.Geometry' in
                                 sections['imports'], bool(imports))
                self.assertEqual(b'the source imports and their transitive dependencies remain active' in
                                 sections['imports'], not imports)
                self.assertNotIn(b'available for import: none', sections['imports'])
                self.assertIn(b'Develop missing intermediate lemmas as proof obligations.', sections['imports'])
                self.assertNotIn(b'Inherited imports: none', sections['imports'])
                self.assertEqual(b'Additional active imports inherited from parent: Extra.Geometry' in
                                 sections['imports'], bool(inherited))

    def test_stop_approval_and_rejection_use_review_and_feedback(self):
        from midas.application import records as r
        from midas.prompt import prover, reviewer
        from midas.strategy import Memory
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        action = dict(mode='stop', rationale='No proof route is available.', objection=None)
        advice = 'Use the constructor True.intro.'
        finding = dict(category='progress', location='goal', issue='True is inhabited.', suggested_revision=advice)
        for verdict in ('accept', 'reject'):
            with self.subTest(verdict=verdict):
                raw = model_json(dict(verdict=verdict, rationale='Independent review decision.',
                    findings=[finding] if verdict == 'reject' else [], question='', urls=[]))
                captured = []
                original = prover.render
                def capture(snapshot):
                    captured.append(snapshot)
                    return original(snapshot)
                with patch('midas.prompt.prover.render', side_effect=capture), self.start(name=verdict,
                        workflow='scripted', config=replace(Config(), prover=route, reviewer=route),
                        script=(('prover', response(action)), ('reviewer', raw),
                                ('prover', response(dict(proof(), proof_body='by trivial'))))) as run:
                    completion = self.finish(run)
                    _, state = r.owner_state(run.writer, self.selected.identities[0].id)
                    self.assertIsNone(state.stop_request)
                    self.assertEqual(state.stopped, verdict == 'accept')
                    self.assertEqual(completion.exit_code, 1 if verdict == 'accept' else 0)
                    self.assertEqual(run.writer.status_snapshot().accounting.turns, 2 if verdict == 'accept' else 3)
                    self.assertEqual(run.writer.counter('owner:'+self.selected.identities[0].id, 'protocol'), 0)
                    self.assertEqual(run.writer.counter('owner:'+self.selected.identities[0].id, 'stop_requests'), 1)
                    observation = Memory.load(r.read(run.writer, state.memory)).observations[-1]
                    self.assertEqual(observation.status, 'stop_approved' if verdict == 'accept' else 'stop_rejected')
                    if verdict == 'reject':
                        self.assertIn(advice.encode(), b'\n'.join(s.text for s in captured[-1].sections if s.kind == 'feedback'))
                    else:
                        self.assertEqual(len(captured), 1)
                        self.assertFalse(Compiler.calls)
                    # Saved role instructions must include stop review guidance.
                    self.assertIn(reviewer.STOP_INSTRUCTIONS, run.workflow.instructions['reviewer'])
                with self.app.resume(self.root/verdict) as resumed:
                    self.assertEqual(self.finish(resumed).exit_code, 1 if verdict == 'accept' else 0)

    def test_pending_stop_and_review_receipt_survive_resume(self):
        from midas.application import records as r
        from midas.application.inspection import node_details
        from midas.strategy import Turn
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        action = dict(mode='stop', rationale='The statement cannot be proved.', objection=None)
        review = model_json(dict(verdict='accept', rationale='The request is justified.', findings=[], question='', urls=[]))
        run = self.start(workflow='scripted', config=replace(Config(), prover=route, reviewer=route),
                         script=(('prover', response(action)), ('reviewer', review)))
        owner = self.selected.identities[0].id
        try:
            for _ in range(1000):
                run.step()
                _, state = r.owner_state(run.writer, owner)
                if state.stop_request:
                    break
                time.sleep(.001)
            self.assertIsNotNone(state.stop_request)
            self.assertFalse(state.stopped)
            self.assertIn(('stop_request', state.stop_request), node_details(run.writer, owner).artifacts)
            self.assertIsNone(r.value(run.writer, 'application.objections', owner))
            with patch.object(run.controller, '_accept', return_value=0):
                for _ in range(1000):
                    run.step()
                    if run.writer.pending_receipts():
                        break
                    time.sleep(.001)
                self.assertTrue(run.writer.pending_receipts())
                job = r.value(run.writer, 'application.pending', owner)
                binding = r.value(run.writer, 'application.jobs', job)
                turn = Turn.load(r.read(run.writer, r.unref(binding[1])))
                self.assertEqual(turn.operation, 'review_stop')
                run.close()
        finally:
            if not run.closed:
                run.close()
        with self.app.resume(self.root/'run') as resumed:
            self.assertEqual(self.finish(resumed).exit_code, 1)
            _, state = r.owner_state(resumed.writer, owner)
            self.assertTrue(state.stopped)
            self.assertIsNone(state.stop_request)
            self.assertEqual(resumed.writer.status_snapshot().accounting.turns, 2)

    def test_fourth_stop_request_ends_node_after_resume(self):
        from midas.application import records as r
        from midas.controller import owner_usage
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        action = response(dict(mode='stop', rationale='Definitions are not shown.',
            objection=dict(category='context_mismatch', evidence='The source prefix has no declaration.')))
        review = model_json(dict(verdict='reject', rationale='Inspect the imported definitions.',
            findings=[dict(category='context_mismatch', location='goal', issue='Definitions are uninspected.',
                           suggested_revision='Use retrieve to inspect the definitions.')], question='', urls=[]))
        script = tuple(('prover', action) for _ in range(4)) + tuple(('reviewer', review) for _ in range(3))
        run = self.start(workflow='scripted', config=replace(Config(), prover=route, reviewer=route), script=script)
        owner = self.selected.identities[0].id
        try:
            for _ in range(1000):
                run.step()
                _, state = r.owner_state(run.writer, owner)
                if state.stop_request:
                    break
                time.sleep(.001)
            self.assertIsNotNone(state.stop_request)
            self.assertEqual(owner_usage(run.writer, OwnerVersion(owner, 0)).stop_requests, 1)
            # Persist an unapplied review receipt before closing; resume must not
            # charge the already committed stop request a second time.
            with patch.object(run.controller, '_accept', return_value=0):
                for _ in range(1000):
                    run.step()
                    if run.writer.pending_receipts():
                        break
                    time.sleep(.001)
                self.assertTrue(run.writer.pending_receipts())
                run.close()
        finally:
            if not run.closed:
                run.close()
        with self.app.resume(self.root/'run') as resumed:
            self.assertEqual(self.finish(resumed).exit_code, 1)
            _, state = r.owner_state(resumed.writer, owner)
            self.assertTrue(state.stopped)
            self.assertEqual(state.reason, 'stop_request_limit_exceeded')
            self.assertIsNone(state.stop_request)
            self.assertIsNone(r.value(resumed.writer, 'application.objections', owner))
            self.assertEqual(resumed.writer.status_snapshot().accounting.turns, 7)
            self.assertEqual(resumed.writer.counter('application.script', 'prover'), 4)
            self.assertEqual(resumed.writer.counter('application.script', 'reviewer'), 3)
            for generation in (0, 1):
                self.assertEqual(owner_usage(resumed.writer, OwnerVersion(owner, generation)).stop_requests, 4)
            self.assertEqual(owner_usage(resumed.writer, OwnerVersion('obl_'+'f'*64, 0)).stop_requests, 0)
            self.assertFalse(Compiler.calls)
        with self.app.resume(self.root/'run') as resumed:
            self.assertEqual(self.finish(resumed).exit_code, 1)
            self.assertEqual(resumed.writer.status_snapshot().accounting.turns, 7)
            self.assertEqual(owner_usage(resumed.writer, OwnerVersion(owner, 0)).stop_requests, 4)

    def test_stop_review_research_and_invalid_verdict_keep_request_pending(self):
        from midas.application import records as r
        from midas.prompt import reviewer
        from midas.strategy import Memory
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        research_route = ModelRoute(provider='openai', model='fixture')
        action = dict(mode='stop', rationale='No route found.', objection=None)
        research = model_json(dict(verdict='research', rationale='Investigate.', findings=[], question='Which constructor?', urls=[]))
        answer = model_json(dict(answer='Use True.intro as the proof.', sources=[], unresolved=[]))
        finding = dict(category='progress', location='goal', issue='There is a constructor.', suggested_revision='')
        malformed = dict(verdict='reject', rationale='Use the constructor.', findings=[finding], question='', urls=[])
        rejected = dict(malformed, findings=[dict(finding, suggested_revision='Apply True.intro.')])
        captured, original = [], reviewer.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.reviewer.render', side_effect=capture), self.start(workflow='scripted',
                config=replace(Config(), prover=route, reviewer=route, research=research_route),
                script=(('prover', response(action)), ('reviewer', research), ('research', answer),
                        ('reviewer', model_json(malformed)), ('reviewer', model_json(rejected)),
                        ('prover', response(dict(proof(), proof_body='by trivial'))))) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(len(captured), 3)
            for display in captured:
                self.assertEqual(next(s.text for s in display.sections if s.name == 'stop-request'),
                    next(s.text for s in captured[0].sections if s.name == 'stop-request'))
            evidence = b'\n'.join(s.text for s in captured[1].sections if s.kind == 'evidence')
            # Scripted research has no web events; its admitted failure still
            # returns to this review instead of resuming the prover.
            self.assertIn(b'Research performed no recorded web action', evidence)
            _, state = r.owner_state(run.writer, self.selected.identities[0].id)
            self.assertFalse(state.stopped)
            self.assertIsNone(state.stop_request)
            self.assertEqual(run.writer.counter('owner:'+self.selected.identities[0].id, 'protocol'), 1)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 6)
            self.assertEqual(run.writer.counter('owner:'+self.selected.identities[0].id, 'stop_requests'), 1)
            self.assertIn('Apply True.intro.', Memory.load(r.read(run.writer, state.memory)).observations[-1].messages)

    def test_protocol_rejection_reaches_next_model_prompt(self):
        from midas.application import records as r
        from midas.provider import ModelOutput, Route
        from midas.strategy import Memory
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses',
                           prompt_cache='explicit')
        invalid = (
            ('recall', dict(mode='recall', rationale='Inspect the definition.', result_id='Nat.add'),
             b'Unknown strategy action'),
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
        from midas.application import token_usage
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
            owner = run.workflow.graph(run.writer).nodes[0].owner
            sample = token_usage.sample(run.writer, owner, 'prover', Route.from_config(route))
            self.assertGreater(sample[0], 0)
            self.assertEqual(sample[1], 100)
            self.assertIsNone(token_usage.sample(run.writer, owner, 'reviewer', Route.from_config(route)))
            self.assertIsNone(token_usage.sample(run.writer, owner, 'prover', Route.from_config(replace(route, model='other'))))
            run.writer.validate(force_artifacts=True)
        view = self.app.inspect(self.root/'run')
        self.assertEqual(record(view)['usage']['cost_microusd'], 240)
        self.assertIn('cost: $0.000240', terminal(view))
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(run.writer.status_snapshot().accounting.actual.cost_microusd, 240)
            self.assertEqual(token_usage.sample(run.writer, owner, 'prover', Route.from_config(route)), sample)
        self.assertEqual(len(calls), 1)

    def test_calibrated_token_window_reaches_provider_without_deleting_saved_pages(self):
        from midas.application import records as r, token_usage
        from midas.application.service import commit
        from midas.application.workflow import Buffer
        from midas.controller import Effect, artifact
        from midas.provider import ModelOutput, Route
        from midas.tools import Scope, prepare_archive
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses',
                           input_token_budget=30000)
        provider_route = Route.from_config(route)
        calls = []
        def transport(request, raw, *args):
            calls.append(raw)
            # Unknown usage must leave the previous usable sample intact.
            return ModelOutput(raw=response(proof()))
        self.app.transports = {provider_route.scope: transport}
        with self.start(workflow='live', config=replace(Config(), prover=route)) as run:
            reader, flow = run.writer, run.workflow
            owner = flow.graph(reader).nodes[0].owner
            _, state = r.owner_state(reader, owner.obligation_id)
            buffer = Buffer()
            archives = tuple(prepare_archive(Scope(owner, flow.selection.environment_id, state.context),
                f'{i:064x}', (f'Evidence {i}\n'+'x'*4000,), (state.context,), buffer) for i in range(12))
            indexes = tuple(artifact(a.payload) for a in archives)
            evidence = tuple(a.pages[0] for a in archives)
            commit(reader, 'evidence-and-usage', r.merge(
                Effect(artifacts=(*buffer.artifacts, *indexes)),
                r.record(reader, 'application.archives', owner.obligation_id, [r.ref(a.reference) for a in indexes]),
                r.save_owner(reader, owner.obligation_id, replace(state, evidence=evidence)),
                r.record(reader, 'application.prompt_usage', owner.obligation_id+':prover',
                         [provider_route.scope, 1000, 500])))
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(archives[0].id.encode(), calls[0])
            self.assertIn(archives[-1].id.encode(), calls[0])
            self.assertGreater(calls[0].count(b'Result ID:'), 2)
            self.assertLess(calls[0].count(b'Result ID:'), 12)
            self.assertIn(b'older evidence page(s) omitted', calls[0])
            self.assertEqual(r.owner_state(reader, owner.obligation_id)[1].evidence, evidence)
            self.assertEqual(token_usage.sample(reader, owner, 'prover', provider_route), (1000, 500))
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(token_usage.sample(run.writer, owner, 'prover', provider_route), (1000, 500))
            self.assertEqual(r.owner_state(run.writer, owner.obligation_id)[1].evidence, evidence)

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

    def test_resume_raised_export_limits_retries_without_rechecking_proofs(self):
        from midas.application import records as r
        from midas.controller import Accepted
        from midas.policy import Failure
        with self.start() as run:
            frozen = run.writer.frozen_run().configuration
            run.submit(submission(self.selected))
            operation = run.controller.operations.integrate
            run.controller.operations = replace(run.controller.operations,
                integrate=replace(operation, admit=lambda *args: Accepted(failure=Failure.MEMORY)))
            self.assertNotEqual(self.finish(run).exit_code, 0)
            nodes = tuple(run.writer.nodes(limit=100))
            failed = next(row.key for row in run.writer.records('application.jobs', limit=100)
                          if row.key.startswith('export:'))
            original = next(invocation for key in run.writer.executions('RECEIVED', limit=100)
                            if (invocation := run.writer.invocation(key)).request.job.semantic_key == failed)
            receipt = run.writer.receipt(original.original_execution_id)
            usage = r.value(run.writer, 'application.export', 'usage')
        Compiler.calls = []
        with self.app.resume(self.root/'run', increases={'run': {'turns': 401}}) as run:
            self.assertNotEqual(self.finish(run).exit_code, 0)
            self.assertEqual(Compiler.calls, [])
        with self.app.resume(self.root/'run', increases={
                'checker': {'memory_mib': 8192, 'wall_ms': 480000},
                'export': {'memory_mib': 8192, 'timeout_ms': 480000},
                'scheduling': {'compiler_memory_mib': 16384, 'compiler_concurrency': 2}}) as run:
            self.assertEqual(r.value(run.writer, 'application.export', 'usage'), usage)
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(tuple(run.writer.nodes(limit=100)), nodes)
            self.assertEqual(run.writer.frozen_run().configuration, frozen)
            self.assertEqual(run.writer.invocation(original.original_execution_id), original)
            self.assertEqual(run.writer.receipt(original.original_execution_id), receipt)
            stage = run.workflow.stage(run.writer, 'integration')
            retried = run.writer.invocation(stage.receipt.original_execution_id)
            self.assertNotEqual(retried.request.job.semantic_key, failed)
            self.assertEqual(retried.request.operation_payload, original.request.operation_payload)
            self.assertEqual(retried.request.job.limits.memory_mib, 8192)
            self.assertEqual(retried.request.job.limits.wall_ms, 480000)
            self.assertEqual(Compiler.calls, ['integrate', 'audit'])
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 0)
            run.writer.validate(force_artifacts=True)
        with self.app.resume(self.root/'run') as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(run.workflow.policy.config.export.memory_mib, 8192)
            self.assertEqual(Compiler.calls, ['integrate', 'audit'])

    def test_pending_export_keeps_frozen_limits_then_retries_with_increase(self):
        from midas.application import records as r
        from midas.controller import Accepted
        from midas.policy import Failure
        with self.start() as run:
            run.submit(submission(self.selected))
            dispatch = run.controller._dispatch
            def defer_export():
                if r.value(run.writer, 'application.export', 'integration'):
                    return 0, ()
                return dispatch()
            with patch.object(run.controller, '_dispatch', side_effect=defer_export):
                for _ in range(1000):
                    run.step()
                    pending = r.value(run.writer, 'application.export', 'integration')
                    if pending:
                        break
                    time.sleep(.001)
            self.assertIsNotNone(pending)
            old_key = pending[1]
            self.assertEqual(run.writer.intent_state(old_key)[0], 'PENDING')
        Compiler.calls = []
        with self.app.resume(self.root/'run', increases={
                'export': {'memory_mib': 8192},
                'scheduling': {'compiler_memory_mib': 8192}}) as run:
            operation = run.controller.operations.integrate
            observed = []
            def admit(invocation, *args):
                observed.append(invocation.request.job.limits.memory_mib)
                if invocation.request.job.limits.memory_mib == 4096:
                    return Accepted(failure=Failure.MEMORY)
                return operation.admit(invocation, *args)
            run.controller.operations = replace(run.controller.operations,
                integrate=replace(operation, admit=admit))
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(observed, [4096, 8192])
            self.assertEqual(run.writer.intent_state(old_key)[0], 'DONE')
            self.assertEqual(Compiler.calls, ['integrate', 'integrate', 'audit'])

    def test_failed_export_gets_only_one_new_attempt_per_limit_increase(self):
        from midas.controller import Accepted
        from midas.policy import Failure
        def fail(run):
            operation = run.controller.operations.integrate
            run.controller.operations = replace(run.controller.operations,
                integrate=replace(operation, admit=lambda *args: Accepted(failure=Failure.MEMORY)))
            self.assertNotEqual(self.finish(run).exit_code, 0)
        with self.start() as run:
            run.submit(submission(self.selected))
            fail(run)
        Compiler.calls = []
        for memory in (8192, 16384):
            with self.app.resume(self.root/'run', increases={
                    'export': {'memory_mib': memory},
                    'scheduling': {'compiler_memory_mib': memory}}) as run:
                fail(run)
                count = len(Compiler.calls)
                fail(run)
                self.assertEqual(len(Compiler.calls), count)
            with self.app.resume(self.root/'run') as run:
                fail(run)
                self.assertEqual(len(Compiler.calls), count)
        self.assertEqual(Compiler.calls, ['integrate', 'integrate'])

    def test_resume_raised_export_deadline_keeps_consumed_budget(self):
        from midas.application import records as r
        from midas.application.service import commit
        with self.start() as run:
            spent = run.workflow.policy.config.export.deadline_ms
            commit(run.writer, 'spent-export-budget',
                   r.record(run.writer, 'application.export', 'usage', [spent, 0, 0]))
            run.submit(submission(self.selected))
            self.assertNotEqual(self.finish(run).exit_code, 0)
            self.assertIn('deadline', r.value(run.writer, 'application.export', 'integration')[1])
        Compiler.calls = []
        with self.app.resume(self.root/'run', increases={'export': {'deadline_ms': spent+90000}}) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(r.value(run.writer, 'application.export', 'usage')[0], spent)
            self.assertEqual(Compiler.calls, ['integrate', 'audit'])

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

    def test_retrieve_and_probe_deliver_readable_evidence_and_continue(self):
        from midas.application import helpers, records as r
        from midas.tools import Match, SearchService
        self.app.search_factory = lambda: SearchService(transport=lambda *args: (Match(('Nat.add',)),))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        retrieve = dict(mode='retrieve', rationale='Find and inspect addition.', queries=['addition'], names=[],
                        backends=[dict(name='leanexplore', prefixes=[])])
        probe = dict(mode='probe', rationale='Try the proof.', cases=[dict(id='try',
            code='Lean.Elab.Tactic.evalTactic (← `(tactic| trivial))'),
            dict(id='second', code='Lean.Elab.Tactic.evalTactic (← `(tactic| exact True.intro))')])
        action = proof()
        action['proof_body'] = 'by trivial'
        with self.start(workflow='scripted', config=replace(Config(), prover=route, reviewer=route),
            script=tuple(('prover', response(a)) for a in (retrieve, probe, action))) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            scope = 'owner:'+self.selected.identities[0].id
            self.assertEqual(run.writer.counter(scope, 'retrieval'), 1)
            self.assertEqual(run.writer.counter(scope, 'probe'), 2)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 3)
            self.assertEqual(Compiler.calls.count('lookup'), 1)
            owner = run.workflow.graph(run.writer).nodes[0].owner
            pages = [helpers.page_text(r.read(run.writer, a.pages[0], 16000)).decode()
                     for a in helpers.advertised(run.writer, owner)]
            self.assertEqual(len(pages), 2)
            self.assertIn('Search (leanexplore): addition\n', pages[0])
            self.assertIn('\nNat.add\n', pages[0])
            self.assertIn('Probe try (tactic): closed_exploratory (advisory)', pages[1])
            self.assertIn('Probe second (tactic): closed_exploratory (advisory)', pages[1])
            self.assertIn('try.code (line 1 below):\nrun_tac do\n  '+probe['cases'][0]['code'], pages[1])
            self.assertNotRegex('\n'.join(pages), r'(?m)^\d+(?:/[^:]+)?:')

    def test_named_retrieval_rejects_excluded_names_without_stalling(self):
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        invalid = dict(mode='retrieve', rationale='Inspect a name.', names=['Midas.secret'],
                       queries=[], backends=[])
        named = dict(invalid, names=['Nat.add'])
        with self.start(workflow='scripted', config=replace(Config(), prover=route),
                script=tuple(('prover', response(action)) for action in (invalid, named, proof()))) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            owner = run.workflow.graph(run.writer).nodes[0].owner
            self.assertEqual(run.writer.counter('owner:'+owner.obligation_id, 'retrieval'), 1)
            self.assertEqual(Compiler.calls.count('lookup'), 1)
            self.assertEqual(run.writer.status_snapshot().accounting.turns, 3)

    def test_local_retrieval_delivers_twelve_details_fifty_names_and_named_retrieval(self):
        from midas.application import helpers, records as r
        from midas.tools import SearchService, SourceEntry, SourceIndex
        from midas.tools.search import source_search
        from tests.test_tools import Artifacts
        artifacts = Artifacts()
        source = 'namespace Fixture\n'+''.join(f'def term{i:02} : Nat := {i}\n' for i in range(70))+'end Fixture\n'
        index = SourceIndex(self.environment.id, (SourceEntry('project/Support.lean', artifacts.save(source.encode())),))
        self.app.search_factory = lambda: SearchService(index=index, source_reader=artifacts)
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        retrieve = dict(mode='retrieve', rationale='Find declarations.', queries=['Nat'], names=[],
                        backends=[dict(name='frozen-source', prefixes=['project'])])
        named = dict(mode='retrieve', rationale='Expand a tail name.', names=['Fixture.term49'],
                     queries=[], backends=[])
        with patch('midas.tools.search.source_search', wraps=source_search) as searches, self.start(
                workflow='scripted', config=replace(Config(), prover=route),
                script=tuple(('prover', response(action)) for action in (retrieve, named, proof()))) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            self.assertEqual(run.workflow.search.cache_directory, self.app.cache/'search')
            owner = run.workflow.graph(run.writer).nodes[0].owner
            archives = helpers.advertised(run.writer, owner)
            self.assertEqual(len(archives), 2)
            self.assertEqual(len(archives[0].pages), 1)
            text = helpers.page_text(r.read(run.writer, archives[0].pages[0], 16000)).decode()
            self.assertEqual(text.count('Source:'), 12)
            self.assertIn('50. Fixture.term49', text)
            self.assertNotIn('Fixture.term50', text)
            expanded = helpers.page_text(r.read(run.writer, archives[1].pages[0], 16000)).decode()
            self.assertIn('Fixture.term49', expanded)
            self.assertEqual(run.writer.counter('owner:'+owner.obligation_id, 'retrieval'), 2)
            self.assertEqual(Compiler.calls.count('lookup'), 2)
            self.assertEqual(searches.call_count, 1)

    def test_runs_and_resume_reuse_package_postings_with_fresh_application_caches(self):
        from threading import Event
        from midas.application import preparation
        from midas.environment import File
        from midas.tools import bm25
        source = b'def helper : Nat := 0\n'
        project = self.environment.directory/'project'
        project.mkdir(parents=True)
        (project/'Support.lean').write_bytes(source)
        self.environment.roots['project'] = project
        self.environment.manifest.inventory = SimpleNamespace(files=(
            File('project/Support.lean', len(source), False, sha256(source).hexdigest()),))
        self.environment.manifest.modules = {}
        preparation.operate(lambda context: preparation.index_sources(self.environment, context))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        retrieve = dict(mode='retrieve', rationale='Find a helper.', queries=['helper'], names=[],
                        backends=[dict(name='frozen-source', prefixes=['project'])])
        options = dict(workflow='scripted', config=replace(Config(), prover=route),
                       script=(('prover', response(retrieve)), ('prover', response(proof()))))
        with patch.object(bm25, '_build', side_effect=AssertionError('unexpected rebuild')):
            for number in range(2):
                self.app.cache = self.root/f'cache-{number}'
                with self.start(f'run-{number}', **options) as run:
                    self.assertEqual(run.workflow.search.prepared_path,
                                     self.environment.directory/preparation.SEARCH_INDEX)
                    if number == 0:
                        self.assertEqual(self.finish(run).exit_code, 0)
                self.assertFalse((self.app.cache/'search').exists())
            self.app.cache = self.root/'resume-cache'
            with self.app.resume(self.root/'run-1') as run:
                self.assertEqual(self.finish(run).exit_code, 0)
                owner = run.workflow.graph(run.writer).nodes[0].owner
                from midas.application import helpers, records as r
                archive = helpers.advertised(run.writer, owner)[0]
                self.assertIn(b'def helper', helpers.page_text(r.read(run.writer, archive.pages[0], 16000)))
            self.assertFalse((self.app.cache/'search').exists())

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
        from midas.application.helpers import page_effect
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
                    'a'*64, (declaration, 'x'*300000) if large else (declaration,), (state.context,), buffer)
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

                if large:
                    self.assertGreater(len(archive.pages), 8)
                    for ordinal in range(1, len(archive.pages)):
                        cursor = f'{archive.id}:{ordinal}'
                        self.assertIn(cursor, snapshot.cursors)
                        page = Work('page', owner, detail=r.pack([cursor, prepared.turn.id]))
                        state, _ = page_effect(flow, reader, snapshot, prepared.turn, page, state)
                        self.assertEqual(state.evidence, archive.pages[:ordinal+1])
                        # A replayed delivery must not append the page again.
                        unchanged, _ = page_effect(flow, reader, snapshot, prepared.turn, page, state)
                        self.assertEqual(unchanged, state)
                        commit(reader, f'page-{ordinal}', r.save_owner(reader, owner.obligation_id, state))
                        snapshot = replace(flow.snapshot(reader, owner), workflow='live')
                        self.assertNotIn(cursor, snapshot.cursors)
                    self.assertEqual(snapshot.cursors, ())
                    display = flow.display(reader, snapshot)
                    results = [s for s in prover.render(display).sections if s.kind == 'evidence']
                    self.assertEqual(len(results), len(archive.pages))
                    self.assertIn(declaration.encode(), results[0].text)
                    self.assertNotIn(b'omitted', b''.join(s.text for s in results))

    def test_delivered_results_and_archive_labels_survive_growth_and_resume(self):
        from midas.application import helpers, records as r
        from midas.prompt import prover
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, helpers=replace(Config().helpers, retrieval=25))
        retrieve = dict(mode='retrieve', rationale='Inspect the declaration.', names=['Nat.add'],
                        queries=[], backends=[])
        script = (('prover', response(retrieve)),)*25 + (('prover', response(proof())),)
        with self.start(workflow='scripted', config=config, script=script) as run:
            self.assertEqual(self.finish(run).exit_code, 0)
            owner = run.workflow.graph(run.writer).nodes[0].owner
            archives = helpers.advertised(run.writer, owner)
            self.assertEqual(len(archives), 25)
            _, state = r.owner_state(run.writer, owner.obligation_id)
            expected = tuple(a.pages[0] for a in archives)
            self.assertEqual(state.evidence, expected)
        with self.app.resume(self.root/'run') as run:
            _, state = r.owner_state(run.writer, owner.obligation_id)
            self.assertEqual(state.evidence, expected)
            snapshot = run.workflow.snapshot(run.writer, owner)
            sections = prover.render(run.workflow.display(run.writer, snapshot)).sections
            evidence = [s for s in sections if s.kind == 'evidence']
            self.assertEqual(len(evidence), 25)
            for section, archive in zip(evidence, archives):
                self.assertIn(f'Result ID: {archive.id}'.encode(), section.text)

    def test_retrieval_search_lookup_and_research_resume_the_prover(self):
        from midas.application import helpers, records as r
        from midas.prompt import build
        from midas.tools import Match, SearchService
        self.app.search_factory = lambda: SearchService(transport=lambda *args: (Match(('Nat.add',)),))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        retrieve = dict(mode='retrieve', rationale='Check a discovered name.', queries=['addition'], names=[],
            backends=[dict(name='leanexplore', prefixes=[])])
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

    def test_review_uses_read_only_candidate_relevant_lookups_and_its_own_research(self):
        from midas.prompt import reviewer
        from tests.test_strategy import decomposition
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        research_route = ModelRoute(provider='openai', model='fixture')
        retrieve = dict(mode='retrieve', rationale='Inspect names.', names=['Nat.add', 'Nat.mul'],
                        queries=[], backends=[])
        probe = dict(mode='probe', rationale='Inspect internals.', cases=[dict(id='enumeration', code='pure ()')])
        action = decomposition()
        action['children'] = [dict(name='support', statement='True ∧ True', proof='Use Nat.add and the constructor.',
                                   usefulness='Provides the required fact.', depends_on=[])]
        action['proof_body'] = 'by exact support'
        invalid = dict(mode='recall', rationale='Read missing evidence.', result_id='missing-result')
        research = model_json(dict(verdict='research', rationale='Check the plan.', findings=[],
                                  question='Can the child use the constructor?', urls=[]))
        accept = model_json(dict(verdict='accept', rationale='The constructor suffices.', findings=[], question='', urls=[]))
        answer = model_json(dict(answer='Review-specific supporting answer.', sources=[], unresolved=[]))
        child = proof()
        child['proof_body'] = 'by trivial'
        # The existing compiler fixture returns the selected root's identity for
        # every binding. Give this derived child its distinct synthetic interface.
        class ReviewCompiler(Compiler):
            def prepare(self, operation, *args, **kwargs):
                request = super().prepare(operation, *args, **kwargs)
                if operation == 'binding' and kwargs.get('statement') == 'True ∧ True':
                    from midas.checker import CompilerRequest
                    from tests.test_checker import name
                    value = request.decode()
                    truth = ['const', name('True'), []]
                    value['packet']['signature']['target'] = ['app', ['app', ['const', name('And'), []], truth], truth]
                    request = CompilerRequest(codec.encode(value))
                return request
        self.app.checker_factory = ReviewCompiler
        captured = []
        original = reviewer.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.reviewer.render', side_effect=capture), self.start(
                workflow='scripted', config=replace(Config(), prover=route, reviewer=route, research=research_route),
                script=(('prover', response(invalid)), ('prover', response(retrieve)), ('prover', response(probe)),
                        ('prover', response(action)), ('reviewer', research), ('research', answer),
                        ('reviewer', accept), ('prover', response(child)))) as run:
            for _ in range(1000):
                run.step()
                if len(captured) == 2:
                    break
                time.sleep(.001)
        self.assertEqual(len(captured), 2)
        for display in captured:
            self.assertFalse({'memory', 'history', 'feedback'} & {s.kind for s in display.sections})
            text = b'\n'.join(s.text for s in display.sections)
            for clutter in (b'Editable fragment', b'/entries/', b'missing-result', b'Probe enumeration',
                            b'Nat.mul', b'Helper modes', b'Candidate modes', b'Frozen-source roots'):
                self.assertNotIn(clutter, text)
            evidence = b'\n'.join(s.text for s in display.sections if s.kind == 'evidence')
            self.assertIn(b'Nat.add', evidence)
            self.assertIn(b'English proof plan:', text)
        before, after = (b'\n'.join(s.text for s in d.sections if s.kind == 'evidence') for d in captured)
        self.assertNotIn(b'Review-specific supporting answer', before)
        # Scripted research has no web events; its failure still belongs to this review.
        self.assertIn(b'Research performed no recorded web action', after)

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
        retrieve = dict(mode='retrieve', rationale='Find a project helper.', queries=['helper'], names=[],
                        backends=[dict(name='frozen-source', prefixes=['project'])])
        with self.start(workflow='scripted', config=replace(Config(), prover=route),
                        script=(('prover', response(retrieve)), ('prover', response(proof())))) as run:
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


class ExportScheduling(unittest.TestCase):
    def fixture(self):
        from unittest.mock import Mock
        from midas.domain import JobKind
        request = SimpleNamespace(sha256='a' * 64)
        work = SimpleNamespace(kind=JobKind.ASSEMBLE, owner=SimpleNamespace(obligation_id='owner'))
        flow = SimpleNamespace(graph=lambda reader: SimpleNamespace(revision=2), library=None,
            stage=lambda *args: None, selection=None, allow_sorry=False,
            export=SimpleNamespace(plan=Mock(return_value=(work,)),
                                   prepare_assembly=Mock(return_value=request)),
            parent_request=Mock(), policy=SimpleNamespace(config=Config(), export_limits=Mock()),
            compiler_job=Mock(side_effect=AssertionError('Duplicate export job')))
        reader = Mock()
        reader.counter.return_value = 0
        return flow, reader, 'export:' + request.sha256

    def test_unrelated_graph_change_keeps_pending_export(self):
        from midas.application.workflow import Workflow
        flow, reader, key = self.fixture()
        for state in ('PENDING', 'RUNNING'):
            reader.intent_state.return_value = (state, None)
            with patch('midas.application.records.value', return_value=['pending', key, 1]):
                self.assertIsNone(Workflow.export_plan(flow, reader))
        flow.export.prepare_assembly.assert_not_called()
        flow.compiler_job.assert_not_called()

    def test_unchanged_failed_export_keeps_existing_intent(self):
        from midas.application.workflow import Workflow
        flow, reader, key = self.fixture()
        reader.intent_state.return_value = ('DONE', None)
        with patch('midas.application.records.value', side_effect=[['failed', 'checker', 1], None]):
            self.assertIsNone(Workflow.export_plan(flow, reader))
        flow.export.prepare_assembly.assert_called_once()
        reader.intent_state.assert_called_once_with(key)
        flow.compiler_job.assert_not_called()


if __name__ == '__main__':
    unittest.main()
