"""Export contracts with deterministic compiler evidence; no Lean processes."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import unittest

from midas.checker import Checker, prepare_proof, worker_payload
from midas.checker import evidence as ev
from midas.checker.requests import signature
from midas.domain import (AdmittedCheck, AdmittedReview, ArtifactRef, Candidate, CheckKind,
    Dependency, FrozenRequest, Invocation, Job, JobKind, ObligationIdentity, OwnerVersion,
    ProofGraph, ProofNode, ProofState, ReceiptBinding, ResourceLimits, ResultStatus, Session, accept_assembly)
from midas.domain import encoding as codec
from midas.executor import Diagnostic, LaunchSpec, Mount, Outcome, ProcessResult
from midas.export import AdmittedStage, Export, Library, Request, read_bundle
from midas.export import proofs
from midas.export.layout import materialize
from tests.test_checker import Artifacts, certificate, name, selection


def issue(request, artifacts):
    value = request.decode()
    kind = request.kind
    job = Job(request.sha256, kind, OwnerVersion(*value['owner']), request.inputs,
              request.expected_versions, ResourceLimits(120000, 4096, 1024**2, 64*1024**2))
    ref = artifacts.save(request.payload)
    return Invocation(str(uuid4()), Session(str(uuid4()), str(uuid4()), 1),
                      FrozenRequest(job, value['environment'], ref, ref), artifacts.save(b'reservation'))


class Harness:
    def __init__(self, root):
        self.root, self.artifacts, self.selection = root, Artifacts(), selection()
        self.owner = OwnerVersion(self.selection.identities[0].id, 0)
        self.modules, self.pins, self.launches = {}, {}, []
        self.environment = SimpleNamespace(id=self.selection.environment_id, toolchain_id='a'*64, roots={},
            manifest=SimpleNamespace(settings=SimpleNamespace(version='4.35.0-rc3', options=(), plugins=(), variables=()),
                                     modules=self.modules, inventory=SimpleNamespace(by_path=self.pins)),
            selection_inputs=lambda: SimpleNamespace(sources_by_path={'Main.lean': SimpleNamespace(
                sha256=sha256(self.selection.source).hexdigest(), byte_count=len(self.selection.source))}),
            materialize=lambda *a, **k: None, validate=lambda **k: None)
        def launch(arguments, *, build, **kwargs):
            return LaunchSpec(('/compiler', *arguments), (Mount(str(build), '/build', True), Mount('/tmp', '/project'),
                Mount('/tmp', '/toolchain'), Mount('/tmp', '/runtime')), '/usr/bin/false', '0'*64,
                (('LEAN_PATH', '/project:/toolchain/lib/lean'),))
        self.environment.launch = launch
        runtime = SimpleNamespace(id='4'*64, environment_id=self.environment.id, retained=False,
            launch=lambda *a, **k: LaunchSpec(('/audit',), (Mount('/tmp', '/project'),), '/usr/bin/false', '0'*64))
        self.checker = Checker(self.environment, root, runtime=runtime)
        self.export = Export(self.checker)
        self.authority = SimpleNamespace(verify_receipt=lambda *a: True)
        self.candidate = Candidate(str(uuid4()), self.owner, self.environment.id, self.artifacts.save(b'by trivial'))
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts)
        cert = certificate(request.decode()['packet'], request.sha256)
        self.graph = ProofGraph(0, (self.owner.obligation_id,), (self.node(self.candidate, cert),))

    def receipt(self):
        return ReceiptBinding(str(uuid4()), *(self.artifacts.save(v) for v in (b'request', b'result', b'seal')))

    def node(self, candidate, cert, *, conditional=False):
        admitted = AdmittedCheck(candidate, CheckKind.CONDITIONAL if conditional else CheckKind.DIRECT,
                                 self.receipt(), self.artifacts.save(codec.encode(cert)))
        interface = self.artifacts.save(codec.encode({'constant': name('target'), 'signature': cert['signature']}))
        return ProofNode(candidate.owner, candidate.environment_id, interface, selected=admitted,
                         review=AdmittedReview(candidate, self.receipt(), True) if conditional else None)

    def conditional(self):
        identity = ObligationIdentity(codec.encode(['const', name('Nat'), []]), codec.encode([]),
                                       codec.encode(['closed-telescope']), self.environment.id)
        owner = OwnerVersion(identity.id, 0)
        child = Candidate(str(uuid4()), owner, self.environment.id, self.artifacts.save(b'0'))
        cert = certificate({'signature': signature(identity), 'candidate': '0'}, 'b'*64)
        node = self.node(child, cert)
        node = replace(node, interface=self.artifacts.save(codec.encode({'constant': name('h'), 'signature': signature(identity)})))
        candidate = replace(self.candidate, source=self.artifacts.save(b'by have _ := h; trivial'),
                            dependencies=(Dependency(owner, 'h', node.interface),))
        request = prepare_proof(self.selection, 0, candidate, self.artifacts, dependencies=(identity,))
        cert = certificate(request.decode()['packet'], request.sha256)
        cert['assumptions'] = [{'parameter': name('h'), 'signature': signature(identity)}]
        parent = self.node(candidate, cert, conditional=True)
        self.graph = ProofGraph(0, (self.owner.obligation_id,), (parent, node))
        return request

    def run(self, request, *, corrupt_build=False, mutate_previous=False):
        inv = issue(request, self.artifacts)
        workspace = self.root / str(uuid4())
        workspace.mkdir()
        empty = Diagnostic(b'', 0, False)
        def run(recipe, **kwargs):
            self.launches.append(recipe)
            if '-o' in recipe.argv:
                build = Path(next(m.source for m in recipe.mounts if m.target == '/build'))
                source = (build / recipe.argv[-1].removeprefix('/build/')).read_bytes()
                destination = build / recipe.argv[recipe.argv.index('-o')+1].removeprefix('/build/')
                destination.write_bytes(b'wrong' if corrupt_build else b'compiled:' + source)
                destination.with_suffix('.ilean').write_bytes(b'info:' + source)
                if mutate_previous and destination.name == 'Second.olean':
                    (destination.parent/'Dep.olean').write_bytes(b'changed by a later module')
                return ProcessResult(Outcome.COMPLETED, 0, 1, 100, 100, None, empty, empty)
            packet = codec.decode((workspace / 'Packet.bin').read_bytes())
            value = (ev.replay_receipt(packet['certificate'], request.sha256) if packet['mode'] == 'replay' else
                     {'format': 'midas-source-audit-sorry-v1' if packet.get('allow_sorry') else 'midas-source-audit-v1',
                      'request': request.sha256, 'targets': packet['targets']})
            data = codec.encode(value)
            return ProcessResult(Outcome.COMPLETED, 0, 1, 100, 100, None, empty, empty,
                                 evidence=Diagnostic(data, len(data), False))
        context = SimpleNamespace(workspace=workspace, invocation_id=inv.original_execution_id,
            request_hash=inv.request.request_hash, reader=self.artifacts, staging=self.artifacts,
            check=lambda: None, run_process=run, limits=inv.request.job.limits)
        result = self.export.execute(inv.request, context)
        receipt = ReceiptBinding(inv.original_execution_id, inv.request.request_ref,
            self.artifacts.save(worker_payload(result)), self.artifacts.save(b'seal'))
        return inv, result, receipt

    def admit(self, request, *, integration=None, parent_request=None):
        inv, result, receipt = self.run(request)
        if result.status != ResultStatus.SUCCEEDED:
            raise AssertionError(self.artifacts.data[result.diagnostics.id] if result.diagnostics else result)
        return self.export.admit_receipt(request, inv, result, receipt, self.artifacts, self.authority,
            selection=self.selection, graph=self.graph, integration=integration, parent_request=parent_request)

    def integrate(self, **kwargs):
        request = self.export.prepare_integration(self.selection, self.graph, self.artifacts, self.artifacts, **kwargs)
        return self.admit(request)

    def verify(self, integration):
        request = self.export.prepare_verification(integration, self.selection, self.graph, self.artifacts)
        return self.admit(request, integration=integration)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.h = Harness(self.root)

    def test_lifecycle_and_clean_verification_use_distinct_stages(self):
        h = self.h
        self.assertEqual(h.export.plan(h.selection, h.graph)[0].kind, JobKind.INTEGRATE)
        integration = h.integrate()
        self.assertEqual(h.export.plan(h.selection, h.graph, integration=integration)[0].kind, JobKind.EXPORT_VERIFY)
        verified = h.verify(integration)
        self.assertEqual(h.export.plan(h.selection, h.graph, integration=integration, verification=verified), ())
        with patch.object(Request, 'decode', side_effect=AssertionError('planning decoded source payload')):
            self.assertEqual(h.export.plan(h.selection, h.graph, integration=integration, verification=verified), ())
        self.assertNotEqual(verified.request.sha256, integration.request.sha256)
        for launch in h.launches[1:]:
            self.assertNotIn('/project', {m.target for m in launch.mounts})
        self.assertEqual(h.launches[-1].argv, ('/audit',))
        with self.assertRaises(ValueError):
            materialize(integration, h.artifacts, self.root/'premature')

    def test_conditional_plan_requires_assembly_and_exact_child_proofs(self):
        h = self.h
        parent = h.conditional()
        work = h.export.plan(h.selection, h.graph)
        self.assertEqual(work[0].kind, JobKind.ASSEMBLE)
        with self.assertRaises(ValueError):
            h.integrate()
        request = h.export.prepare_assembly(h.selection, h.graph, h.owner, parent, h.artifacts)
        admitted = h.admit(request, parent_request=parent)
        self.assertEqual(admitted.kind, CheckKind.ASSEMBLY)
        h.graph = accept_assembly(h.graph, admitted)
        integration = h.integrate()
        self.assertIn('dependencies', integration.request.decode()['packet']['source'])
        h.verify(integration)

    def test_partial_export_of_unresolved_root_preserves_graph_and_recheck_option(self):
        h = self.h
        h.graph = replace(h.graph, nodes=(replace(h.graph.nodes[0], selected=None),))
        original = h.graph
        self.assertEqual(h.export.plan(h.selection, h.graph), ())
        with self.assertRaises(ValueError):
            h.integrate()
        work = h.export.plan(h.selection, h.graph, allow_sorry=True)
        self.assertEqual(work[0].kind, JobKind.INTEGRATE)
        integrated = h.integrate(allow_sorry=True)
        self.assertTrue(integrated.request.allow_sorry)
        self.assertEqual(integrated.request.proofs, ())
        self.assertIn('sorry', integrated.request.decode()['packet']['source'])
        verified = h.verify(integrated)
        self.assertIs(h.graph, original)
        self.assertEqual(h.graph.nodes[0].state, ProofState.UNRESOLVED)
        with patch.object(Request, 'decode', side_effect=AssertionError('planning decoded source payload')):
            self.assertEqual(h.export.plan(h.selection, h.graph, allow_sorry=True,
                integration=integrated, verification=verified), ())
        self.assertFalse(h.export.current(verified, h.selection, h.graph, allow_sorry=False))
        destination = self.root/'partial'
        h.export.materialize(verified, h.selection, h.graph, h.artifacts, destination)
        saved, reader = read_bundle(destination)
        self.assertTrue(saved.allow_sorry)
        self.assertTrue(h.export.prepare_recheck(saved, reader).decode()['packet']['allow_sorry'])
        for option in (None, 1, 'yes'):
            with self.assertRaises(ValueError):
                h.integrate(allow_sorry=option)

    def test_partial_export_keeps_conditional_parent_and_fences_unresolved_child(self):
        h = self.h
        h.conditional()
        child = h.graph.nodes[1]
        h.graph = replace(h.graph, nodes=(h.graph.nodes[0], replace(child, selected=None)))
        original = h.graph
        integrated = h.integrate(allow_sorry=True)
        source = integrated.request.decode()['packet']['source']
        self.assertIn('by have _ := h; trivial', source)
        self.assertIn('dependencies', source)
        self.assertIn('sorry', source)
        self.assertIn(child.owner, integrated.request.expected_versions)
        inv = issue(integrated.request, h.artifacts)
        with self.assertRaises(ValueError):
            integrated.request.validate_for(replace(inv.request,
                job=replace(inv.request.job, expected_versions=(h.owner,))), h.artifacts)
        verified = h.verify(integrated)
        self.assertIs(h.graph, original)
        self.assertEqual(h.graph.nodes[0].state, ProofState.CONDITIONAL)
        h.graph = replace(h.graph, nodes=(h.graph.nodes[0], child))
        self.assertFalse(h.export.current(verified, h.selection, h.graph))
        with self.assertRaises(ValueError):
            h.export.materialize(verified, h.selection, h.graph, h.artifacts, self.root/'stale-partial')
        inv, result, receipt = h.run(integrated.request)
        with self.assertRaises(ValueError):
            h.export.admit_receipt(integrated.request, inv, result, receipt, h.artifacts, h.authority,
                                  selection=h.selection, graph=h.graph)
        self.assertEqual(h.export.plan(h.selection, h.graph, allow_sorry=True)[0].kind, JobKind.INTEGRATE)
        self.assertEqual(h.export.plan(h.selection, h.graph)[0].kind, JobKind.ASSEMBLE)
        closed_source = h.integrate(allow_sorry=True).request.decode()['packet']['source']
        self.assertNotIn('sorry', closed_source)
        self.assertIsNone(h.graph.nodes[0].assembly)

    def test_partial_audit_evidence_and_option_cannot_be_confused_with_strict_export(self):
        h = self.h
        integration = h.integrate(allow_sorry=True)
        value = integration.request.decode()
        strict = {'format': 'midas-source-audit-v1', 'request': integration.request.sha256,
                  'targets': value['packet']['targets']}
        with self.assertRaises(ValueError):
            Checker._audit_result(strict, value['packet'], integration.request.sha256)
        partial = dict(strict, format='midas-source-audit-sorry-v1')
        with self.assertRaises(ValueError):
            Checker._audit_result(partial, dict(value['packet'], allow_sorry=False), integration.request.sha256)
        value['packet']['allow_sorry'] = False
        with self.assertRaises(ValueError):
            Request(codec.encode(value))
        self.assertEqual(h.export.plan(h.selection, h.graph, integration=integration)[0].kind, JobKind.INTEGRATE)

    def test_partial_deep_shared_graph_is_rendered_once_without_recursive_expansion(self):
        h = self.h
        nodes = []
        count = 1050
        for index in range(count):
            identity = ObligationIdentity(codec.encode(['forall', name('n' + str(index)), 'explicit',
                ['const', name('Nat'), []], ['const', name('Nat'), []]]), codec.encode([]),
                codec.encode(['closed-telescope']), h.environment.id)
            sig = signature(identity)
            interface = h.artifacts.save(codec.encode({'constant': name('h' + str(index)), 'signature': sig}))
            owner = OwnerVersion(identity.id, 0)
            if not nodes:
                nodes.append(ProofNode(owner, h.environment.id, interface))
                continue
            alias = 'h' + str(index - 1)
            deps = (Dependency(nodes[-1].owner, alias, nodes[-1].interface),)
            if index > 1:
                deps += (Dependency(nodes[0].owner, 'h0', nodes[0].interface),)
            code = 'fun _ => ' + alias + ' 0'
            candidate = Candidate(str(uuid4()), owner, h.environment.id, h.artifacts.save(code.encode()), deps)
            cert = certificate({'signature': sig, 'candidate': code}, 'b'*64)
            cert['assumptions'] = [{'parameter': name(d.alias),
                'signature': codec.decode(h.artifacts.data[d.interface.id])['signature']} for d in deps]
            nodes.append(replace(h.node(candidate, cert, conditional=True), interface=interface))
        alias = 'h' + str(count - 1)
        candidate = replace(h.candidate, dependencies=(Dependency(nodes[-1].owner, alias, nodes[-1].interface),))
        cert = certificate({'signature': signature(h.selection.identities[0]),
                            'candidate': 'by have _ := ' + alias + ' 0; trivial'}, 'c'*64)
        cert['assumptions'] = [{'parameter': name(alias),
            'signature': codec.decode(h.artifacts.data[nodes[-1].interface.id])['signature']}]
        root = h.node(candidate, cert, conditional=True)
        h.graph = ProofGraph(0, (h.owner.obligation_id,), (root, *nodes))
        self.assertEqual(len(proofs.snapshot(h.selection, h.graph)), count + 1)
        with patch.object(proofs, 'load', wraps=proofs.load) as load, \
                patch('json.dumps', side_effect=AssertionError('large JSON')), \
                patch('json.loads', side_effect=AssertionError('large JSON')):
            source = proofs.render_partial(h.selection, h.graph, h.artifacts).data
        self.assertEqual(load.call_count, count)
        self.assertEqual(source.count(b'sorry'), 1)
        self.assertLess(len(source), count * 6000)

    def test_receipt_rejects_changed_roots_unfenced_children_and_false_seals(self):
        h = self.h
        parent = h.conditional()
        request = h.export.prepare_assembly(h.selection, h.graph, h.owner, parent, h.artifacts)
        inv, result, receipt = h.run(request)
        with self.assertRaises(ValueError):
            request.validate_for(replace(inv.request, job=replace(inv.request.job, expected_versions=(h.owner,))), h.artifacts)
        with self.assertRaises(ValueError):
            h.export.admit_receipt(request, inv, result, receipt, h.artifacts,
                SimpleNamespace(verify_receipt=lambda *a: False), selection=h.selection, graph=h.graph, parent_request=parent)
        node = h.graph.nodes[1]
        changed = replace(node, selected=replace(node.selected, receipt=h.receipt()))
        graph = replace(h.graph, nodes=(h.graph.nodes[0], changed))
        with self.assertRaises(ValueError):
            h.export.admit_receipt(request, inv, result, receipt, h.artifacts, h.authority,
                selection=h.selection, graph=graph, parent_request=parent)

    def test_stale_integration_and_verification_do_not_complete_or_publish(self):
        h = self.h
        integrated = h.integrate()
        verified = h.verify(integrated)
        node = h.graph.nodes[0]
        h.graph = replace(h.graph, nodes=(replace(node, selected=replace(node.selected, receipt=h.receipt())),))
        self.assertEqual(h.export.plan(h.selection, h.graph, integration=integrated, verification=verified)[0].kind,
                         JobKind.INTEGRATE)
        with self.assertRaises(ValueError):
            h.export.prepare_verification(integrated, h.selection, h.graph, h.artifacts)
        with self.assertRaises(ValueError):
            h.export.materialize(verified, h.selection, h.graph, h.artifacts, self.root/'stale')

    def test_library_rebuild_is_exact_and_rejects_changed_compiled_outputs(self):
        h = self.h
        source = b'def value : Nat := 1\n'
        ref = h.artifacts.save(source)
        compiled = {'.olean': sha256(b'compiled:' + source).hexdigest(), '.ilean': sha256(b'info:' + source).hexdigest()}
        h.modules['Dep'] = 'library/Dep.olean'
        h.pins.update({'library/Dep' + suffix: SimpleNamespace(sha256=digest) for suffix, digest in compiled.items()})
        library = Library(h.environment.id, codec.encode([{'name': 'Dep', 'source': ref.to_dict(), 'compiled': compiled}]))
        integrated = h.integrate(profile='library', library=library)
        h.verify(integrated)
        request = h.export.prepare_verification(integrated, h.selection, h.graph, h.artifacts)
        _, result, _ = h.run(request, corrupt_build=True)
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertIn(b'Rebuilt dependency differs', h.artifacts.data[result.diagnostics.id])
        other = b'def second : Nat := 2\n'
        other_ref = h.artifacts.save(other)
        other_compiled = {'.olean': sha256(b'compiled:' + other).hexdigest(), '.ilean': sha256(b'info:' + other).hexdigest()}
        h.modules['Second'] = 'library/Second.olean'
        h.pins.update({'library/Second' + suffix: SimpleNamespace(sha256=digest) for suffix, digest in other_compiled.items()})
        library = Library(h.environment.id, codec.encode([*library.decode(),
            {'name': 'Second', 'source': other_ref.to_dict(), 'compiled': other_compiled}]))
        integrated = h.integrate(profile='library', library=library)
        request = h.export.prepare_verification(integrated, h.selection, h.graph, h.artifacts)
        _, result, _ = h.run(request, mutate_previous=True)
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertIn(b'Rebuilt dependency changed', h.artifacts.data[result.diagnostics.id])

    def test_publication_is_idempotent_rejects_conflicts_links_and_tampering(self):
        h = self.h
        verified = h.verify(h.integrate())
        output = self.root/'export'
        materialize(verified, h.artifacts, output)
        materialize(verified, h.artifacts, output)
        request, reader = read_bundle(output)
        self.assertEqual(request.payload, verified.request.payload)
        recheck = h.export.prepare_recheck(request, reader)
        self.assertNotIn('export', recheck.decode())
        self.assertNotIn(h.graph.nodes[0].proof.evidence.to_dict(), recheck.decode()['inputs'])
        original = (output/'src/Main.lean').read_bytes()
        (output/'src/Main.lean').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            materialize(verified, h.artifacts, output)
        with self.assertRaises(ValueError):
            read_bundle(output)
        self.assertEqual((output/'src/Main.lean').read_bytes(), b'changed')
        (output/'src/Main.lean').write_bytes(original)
        (self.root/'link').symlink_to(output, target_is_directory=True)
        with self.assertRaises(ValueError):
            materialize(verified, h.artifacts, self.root/'link')
        with self.assertRaises(ValueError):
            materialize(verified, h.artifacts, self.root/'nested', protected=(self.root,))

    def test_no_large_json_and_bounded_publication_cleanup(self):
        h = self.h
        node = h.graph.nodes[0]
        cert = proofs.load(h.artifacts, node.proof.evidence)
        cert['declarations'][0]['source'] += '\n-- ' + 'x'*500000
        node = replace(node, selected=replace(node.selected, evidence=h.artifacts.save(codec.encode(cert))))
        h.graph = replace(h.graph, nodes=(node,))
        with patch('json.dumps', side_effect=AssertionError('large JSON')), patch('json.loads', side_effect=AssertionError('large JSON')):
            source = proofs.render(h.selection, (node.proof,), h.artifacts)
            self.assertGreater(len(source.data), 500000)
        verified = h.verify(h.integrate())
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            materialize(verified, h.artifacts, self.root/'binary')
            read_bundle(self.root/'binary')
        def stop():
            raise TimeoutError('stopped')
        with self.assertRaises(TimeoutError):
            materialize(verified, h.artifacts, self.root/'cancelled', check=stop)
        self.assertFalse((self.root/'cancelled').exists())
        self.assertFalse(tuple(self.root.glob('.midas-export-*')))

    def test_missing_build_or_changed_output_cannot_authenticate_verification(self):
        h = self.h
        integration = h.integrate()
        request = h.export.prepare_verification(integration, h.selection, h.graph, h.artifacts)
        inv, result, receipt = h.run(request)
        report = codec.load_record(h.artifacts.data[result.output.id], limit=65536)
        report['phases'].pop(0)
        altered = h.artifacts.save(b''.join(codec.record_parts(report, limit=65536)))
        result = replace(result, output=altered, staged_artifacts=(*result.staged_artifacts, altered))
        receipt = replace(receipt, result_ref=h.artifacts.save(worker_payload(result)))
        with self.assertRaises(ValueError):
            h.export.admit_receipt(request, inv, result, receipt, h.artifacts, h.authority,
                selection=h.selection, graph=h.graph, integration=integration)

    def test_export_controller_adapter_uses_current_inputs(self):
        from midas.controller import export_operation
        h = self.h
        request = h.export.prepare_integration(h.selection, h.graph, h.artifacts, h.artifacts)
        inv, result, receipt = h.run(request)
        operation = export_operation(h.export, lambda *args: None,
                                    lambda *args: {'selection': h.selection, 'graph': h.graph})
        self.assertIsInstance(operation.admit(inv, result, receipt, h.artifacts, h.authority).value, AdmittedStage)

    def test_audit_cannot_drop_the_selected_target_signature(self):
        h = self.h
        request = h.export.prepare_integration(h.selection, h.graph, h.artifacts, h.artifacts)
        value = request.decode()
        value['packet']['targets'] = []
        altered = Request(codec.encode(value))
        inv, result, receipt = h.run(altered)
        with self.assertRaises(ValueError):
            h.export.admit_receipt(altered, inv, result, receipt, h.artifacts, h.authority,
                                  selection=h.selection, graph=h.graph)


if __name__ == '__main__':
    unittest.main()
