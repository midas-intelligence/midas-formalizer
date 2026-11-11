"""Pure checker contracts; these tests do not launch Lean or bubblewrap."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import unittest

from midas.checker import (Checker, CompilerRequest, ProbeCase, admit_probe_progress, inventory, prepare_binding,
    prepare_lookup, prepare_probe, prepare_proof, worker_payload)
from midas.checker import evidence as ev
from midas.checker.requests import signature
from midas.domain import (ArtifactRef, ByteSpan, Candidate, CapacityGrant, Declaration, Dependency,
    FrozenRequest, Invocation, Job, JobKind, ObligationIdentity, OwnerVersion, ReceiptBinding,
    ResourceLimits, SelectionInputs, SelectionRequest, Session, Signature, SourceBinding,
    Termination, WorkerProgress, freeze_selection)
from midas.domain import encoding as codec
from midas.executor import Diagnostic, Outcome, ProcessResult
from tests.test_executor import invocation


class Artifacts:
    def __init__(self):
        self.data = {}

    def write_bounded(self, data, *, max_bytes):
        if len(data) > max_bytes:
            raise ValueError('artifact cap')
        ref = ArtifactRef(str(uuid4()), sha256(data).hexdigest(), len(data))
        self.data[ref.id] = data
        return ref

    def save(self, data):
        return self.write_bounded(data, max_bytes=max(1, len(data)))

    def read_artifact(self, reference, *, max_bytes):
        return reference.verify(self.data[reference.id], max_bytes=max_bytes)


def name(text):
    return ['str', ['anonymous'], text]


def selection(environment='env_' + '2' * 64):
    source = b'theorem target : True := by sorry\n'
    sig = Signature(codec.encode(['const', name('True'), []]), codec.encode([]), codec.encode(['closed-telescope']))
    constant = codec.canonical(name('target'))
    declaration = Declaration('target', constant, 'theorem', ByteSpan(0, len(source)-1),
                              ByteSpan(source.index(b':='), len(source)-1), True,
                              type_references=(codec.canonical(name('True')),))
    inputs = SelectionInputs(environment, (SourceBinding('Main.lean', sha256(source).hexdigest(), len(source)),))
    return freeze_selection(SelectionRequest('Main.lean', all_unfinished=True), source, (declaration,),
                            frozenset((codec.canonical(name('True')),)), {constant: sig}, inputs)


def issue(request, artifacts, *, limits=None):
    value = request.decode()
    owner = OwnerVersion(*value['owner'])
    refs = tuple(ArtifactRef.from_dict(r) for r in value['inputs'])
    owners = tuple(dict.fromkeys((owner, *(OwnerVersion(d[0], d[1]) for d in
                        (value['candidate']['dependencies'] if value['candidate'] else [])))))
    kinds = {'proof': JobKind.CHECK_PROOF, 'binding': JobKind.CHECK_BINDING,
             'lookup': JobKind.LOOKUP, 'probe': JobKind.PROBE, 'audit': JobKind.EXPORT_VERIFY,
             'replay': JobKind.ASSEMBLE}
    job = Job(request.sha256, kinds[value['operation']], owner, refs, owners,
              limits or ResourceLimits(120000, 4096, 1024**2, 64 * 1024**2))
    frozen = FrozenRequest(job, value['environment'], artifacts.save(request.payload),
                           artifacts.save(codec.encode({'request': request.sha256, 'kind': job.kind.value})))
    return Invocation(str(uuid4()), Session(str(uuid4()), str(uuid4()), 1), frozen, artifacts.save(b'reservation'))


def certificate(packet, digest):
    return {'format': ev.PROOF, 'request': digest, 'purpose': 'proof', 'signature': packet['signature'],
            'declarations': [{'name': ev.ROOT, 'signature': packet['signature'],
                              'source': packet['candidate'], 'requires': []}]}


class Contracts(unittest.TestCase):
    def setUp(self):
        self.artifacts = Artifacts()
        self.selection = selection()
        self.owner = OwnerVersion(self.selection.identities[0].id, 0)
        self.candidate = Candidate(str(uuid4()), self.owner, self.selection.environment_id,
                                   self.artifacts.save(b'by trivial'))

    def test_request_binds_exact_candidate_and_reservation(self):
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts)
        inv = issue(request, self.artifacts)
        request.validate_for(inv.request, self.artifacts)
        with self.assertRaises(ValueError):
            request.validate_for(replace(inv.request, environment_id='env_' + '3'*64), self.artifacts)
        with self.assertRaises(ValueError):
            request.validate_for(replace(inv.request, job=replace(inv.request.job, input_refs=())), self.artifacts)
        self.artifacts.data[self.candidate.source.id] = b'by sorry'
        with self.assertRaises(ValueError):
            prepare_proof(self.selection, 0, self.candidate, self.artifacts)

    def test_request_admission_reuses_bytes_but_checks_each_reservation(self):
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts)
        inv = issue(request, self.artifacts)
        with patch.object(CompilerRequest, '_validate', side_effect=AssertionError('revalidated bytes')):
            admitted = CompilerRequest(request.payload)
            admitted.validate_for(inv.request, self.artifacts)
            with self.assertRaises(ValueError):
                admitted.validate_for(replace(inv.request, job=replace(inv.request.job, input_refs=())), self.artifacts)
            self.artifacts.data[inv.request.operation_payload.id] = b'changed request'
            with self.assertRaises(ValueError):
                admitted.validate_for(inv.request, self.artifacts)
        with self.assertRaises(ValueError):
            CompilerRequest(bytearray(request.payload))

    def test_large_source_and_compact_signatures_never_use_json(self):
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts)
        packet = request.decode()['packet']
        packet['candidate'] = 'by trivial\n--' + 'x' * 500000
        with patch('json.dumps', side_effect=AssertionError('large JSON')), patch('json.loads', side_effect=AssertionError('large JSON')):
            encoded = codec.encode(packet)
            self.assertEqual(codec.decode(encoded)['candidate'], packet['candidate'])
            cert = certificate(packet, request.sha256)
            ev.proof_for(codec.decode(codec.encode(cert)), packet, request.sha256)

    def test_proof_source_types_are_bounded_and_preserve_the_submitted_statement(self):
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts, statement='True -- authored type')
        packet = request.decode()['packet']
        cert = certificate(packet, request.sha256)
        cert['type_source'] = cert['declarations'][0]['type_source'] = packet['statement']
        ev.proof_for(cert, packet, request.sha256)
        for container in (cert, cert['declarations'][0]):
            for invalid in ('False', 'x' * (1024**2 + 1), None):
                container['type_source'] = invalid
                with self.assertRaises(ValueError):
                    ev.proof_for(cert, packet, request.sha256)
            container['type_source'] = packet['statement']

    def test_lookup_only_uses_prefix_and_no_goal_expansion(self):
        with patch('midas.checker.requests.signature', side_effect=AssertionError('decoded goal')):
            request = prepare_lookup(self.selection, 0, self.owner, ('True',))
        self.assertNotIn('theorem target', request.decode()['packet']['source'])
        with self.assertRaises(ValueError):
            prepare_lookup(self.selection, 0, self.owner, ('by trivial',))

    def test_probe_and_binding_shape(self):
        with self.assertRaises(ValueError):
            prepare_probe(self.selection, 0, self.owner, (ProbeCase('one', 'skip'),)*2)
        request = prepare_binding(self.selection, 0, self.owner, statement='True', parameters=('h',))
        self.assertEqual(request.decode()['packet']['interface']['statement'], 'True')
        with self.assertRaises(ValueError):
            prepare_binding(self.selection, 0, self.owner, parameters=('h', 'h'))

    def test_rebinding_requires_the_explicit_subject_generation_fence(self):
        rebound = OwnerVersion('obl_'+'c'*64, 2)
        request = prepare_binding(self.selection, 0, self.owner, subject_owner=rebound)
        invocation = issue(request, self.artifacts)
        with self.assertRaisesRegex(ValueError, 'not fenced'):
            request.validate_for(invocation.request, self.artifacts)
        job = replace(invocation.request.job, expected_versions=(self.owner, rebound))
        request.validate_for(replace(invocation.request, job=job), self.artifacts)

    def test_dependency_identity_and_interface_are_both_checked(self):
        child = ObligationIdentity(codec.encode(['const', name('False'), []]), codec.encode([]),
                                   codec.encode(['closed-telescope']), self.selection.environment_id)
        iface = self.artifacts.save(codec.encode({'constant': name('h'), 'signature': signature(child)}))
        dep = Dependency(OwnerVersion(child.id, 0), 'h', iface)
        candidate = replace(self.candidate, dependencies=(dep,))
        request = prepare_proof(self.selection, 0, candidate, self.artifacts, dependencies=(child,))
        inv = issue(request, self.artifacts)
        request.validate_for(inv.request, self.artifacts)
        with self.assertRaises(ValueError):
            request.validate_for(replace(inv.request, job=replace(inv.request.job, expected_versions=(self.owner,))), self.artifacts)
        with self.assertRaises(ValueError):
            prepare_proof(self.selection, 0, candidate, self.artifacts, dependencies=(self.selection.identities[0],))

    def test_native_binary_frames_reject_truncation_duplicates_and_json(self):
        proof = {'a': 'x'*10000}
        header = codec.encode({'format': 'midas-native-declarations-v2', 'proof': proof,
                               'artifacts': [{'id': 'producer', 'bytes': 3}]})
        frame = ev.NATIVE + len(header).to_bytes(8, 'little') + header + b'abc'
        self.assertEqual(ev.decode_output(frame, maximum=20000), (proof, (('producer', b'abc'),)))
        for bad in (frame[:-1], frame+b'x', b'{"a":1}', ev.NATIVE):
            with self.assertRaises(ValueError):
                ev.decode_output(bad, maximum=20000)

    def test_certificates_cannot_change_source_subject_or_assumptions(self):
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts)
        packet = request.decode()['packet']
        for field, replacement in [('request', '0'*64), ('purpose', 'interface'), ('assumptions', [])]:
            cert = certificate(packet, request.sha256)
            cert[field] = replacement
            with self.assertRaises(ValueError):
                ev.proof_for(cert, packet, request.sha256)
        cert = certificate(packet, request.sha256)
        cert['declarations'][0]['source'] = 'by sorry'
        with self.assertRaises(ValueError):
            ev.proof_for(cert, packet, request.sha256)

    def test_probe_progress_is_bound_to_case_invocation_and_sequence(self):
        request = prepare_probe(self.selection, 0, self.owner, (ProbeCase('one', 'skip'),))
        inv = issue(request, self.artifacts)
        progress = WorkerProgress(inv.original_execution_id, 0, 'probe_started:one')
        self.assertIsNone(admit_probe_progress(request, inv, progress, self.artifacts).evidence)
        for bad in (replace(progress, sequence=2), replace(progress, kind='probe_started:other'),
                    replace(progress, invocation_id=str(uuid4())),
                    replace(progress, artifacts=(self.artifacts.save(b'fake'),))):
            with self.assertRaises(ValueError):
                admit_probe_progress(request, inv, bad, self.artifacts)

    def test_lookup_has_no_expression_tree_projection(self):
        request = prepare_lookup(self.selection, 0, self.owner, ('True',))
        packet = request.decode()['packet']
        result = {'format': 'midas-lookup-result-context-v1', 'request': request.sha256,
                  'records': [{'query': 'True', 'status': 'available', 'constant': name('True'),
                               'kind': 'inductive', 'type': 'Prop', 'body': None, 'error': None}]}
        ev.lookup_for(result, packet, request.sha256)
        result['records'][0]['signature'] = {'target': 'not a public lookup field'}
        with self.assertRaises(ValueError):
            ev.lookup_for(result, packet, request.sha256)

    def test_source_inventory_rejects_wrong_source_and_duplicate_declarations(self):
        declaration = self.selection.declarations[0]
        row = {'signature': signature(self.selection.identities[0]), 'presentation': '', 'selector': 'target',
               'constant': name('target'), 'kind': 'theorem', 'declaration': [0, len(self.selection.source)-1],
               'replacement': [declaration.replacement.start, declaration.replacement.end], 'unfinished': True,
               'supported': True, 'source_authored': True, 'type_references': [name('True')], 'value_references': []}
        value = {'format': 'midas-source-inventory-v1', 'declarations': [row], 'imported_constants': [name('True')]}
        inputs = SelectionInputs(self.selection.environment_id, (SourceBinding('Main.lean',
            sha256(self.selection.source).hexdigest(), len(self.selection.source)),))
        found = inventory(codec.encode(value), self.selection.selected.request, self.selection.source, inputs)
        self.assertEqual(found.id, self.selection.id)
        with self.assertRaises(ValueError):
            inventory(codec.encode(value), self.selection.selected.request, self.selection.source+b' ', inputs)
        value['declarations'].append(row)
        with self.assertRaises(ValueError):
            inventory(codec.encode(value), self.selection.selected.request, self.selection.source, inputs)

    def test_runtime_cache_detects_mutation_and_symlinks(self):
        from midas.checker.runtime import Runtime, _inventory
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'native').write_bytes(b'original')
            runtime = Runtime(root, '0'*64, self.selection.environment_id, False, tuple(map(tuple, _inventory(root))))
            runtime.validate()
            (root/'native').write_bytes(b'modified')
            with self.assertRaises(ValueError):
                runtime.validate()
            (root/'link').symlink_to(root/'native')
            with self.assertRaises(ValueError):
                runtime.validate()

    def test_full_execution_receipt_and_failure_admission(self):
        request = prepare_proof(self.selection, 0, self.candidate, self.artifacts)
        inv = issue(request, self.artifacts, limits=ResourceLimits(120000, 4096, 1024**2, 64*1024**2, 12345, 321))
        with TemporaryDirectory() as temp:
            root = Path(temp)
            environment = SimpleNamespace(id=self.selection.environment_id,
                materialize=lambda *a, **k: None)
            from midas.executor import LaunchSpec
            runtime = SimpleNamespace(id='4'*64, environment_id=environment.id, retained=False,
                launch=lambda *a, **k: LaunchSpec(('/compiler',), (), '/usr/bin/false', '0'*64))
            checker = Checker(environment, root, runtime=runtime)
            def run(spec, **kwargs):
                packet = codec.decode((root / 'Packet.bin').read_bytes())
                self.assertEqual(packet['max_heartbeat'], 12345)
                self.assertEqual(packet['max_rec_depth'], 321)
                value = (certificate(packet, request.sha256) if packet['mode'] == 'produce'
                         else ev.replay_receipt(packet['certificate'], request.sha256))
                data = codec.encode(value)
                return ProcessResult(Outcome.COMPLETED, 0, 1, 100, 100, None,
                    Diagnostic(b'', 0, False), Diagnostic(b'', 0, False),
                    evidence=Diagnostic(data, len(data), False))
            ctx = SimpleNamespace(workspace=root, invocation_id=inv.original_execution_id,
                request_hash=inv.request.request_hash, reader=self.artifacts, staging=self.artifacts,
                check=lambda: None, run_process=run)
            result = checker.execute(inv.request, ctx)
            self.assertEqual(result.status.value, 'SUCCEEDED')
            receipt = ReceiptBinding(inv.original_execution_id, inv.request.request_ref,
                                     self.artifacts.save(worker_payload(result)), self.artifacts.save(b'seal'))
            authority = SimpleNamespace(verify_receipt=lambda *a: True)
            admitted = checker.admit_receipt(request, inv, result, receipt, self.artifacts, authority, candidate=self.candidate)
            self.assertEqual(admitted.candidate, self.candidate)
            with self.assertRaises(ValueError):
                checker.admit_receipt(request, inv, result, receipt, self.artifacts,
                    SimpleNamespace(verify_receipt=lambda *a: False), candidate=self.candidate)
            with self.assertRaises(ValueError):
                checker.admit_receipt(request, inv, replace(result, termination=Termination.UNKNOWN),
                                      receipt, self.artifacts, authority, candidate=self.candidate)
            report = codec.load_record(self.artifacts.data[result.output.id], limit=65536)
            for alteration in ('over_budget', 'missing_replay', 'wrong_phase', 'different_runtime'):
                corrupted = codec.load_record(self.artifacts.data[result.output.id], limit=65536)
                if alteration == 'over_budget':
                    corrupted['phases'][0]['stdout']['observed'] = inv.request.job.limits.output_bytes+1
                elif alteration == 'missing_replay':
                    corrupted['phases'].pop()
                elif alteration == 'wrong_phase':
                    corrupted['phases'][1]['phase'] = 'produce'
                else:
                    corrupted['runtime'] = '5'*64
                changed = self.artifacts.save(b''.join(codec.record_parts(corrupted, limit=65536)))
                forged = replace(result, output=changed, staged_artifacts=(*result.staged_artifacts, changed))
                forged_receipt = replace(receipt, result_ref=self.artifacts.save(worker_payload(forged)))
                with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                    checker.admit_receipt(request, inv, forged, forged_receipt, self.artifacts, authority, candidate=self.candidate)

    def test_resources_use_binary_evidence_and_only_small_control_json(self):
        from midas.checker.runtime import RESOURCE, sources
        for path in RESOURCE.glob('*.lean'):
            text = path.read_text()
            self.assertNotIn('Json.parse', text)
            self.assertNotIn('MIDAS_CERTIFICATE ', text)
            if path.name != 'isolated_import_main.lean':
                self.assertNotIn('.compress', text)
        self.assertIn(b'Midas.Binary.encode', sources()['Native.lean'])
        self.assertIn('MidasCheck.lean', sources(native=False))


if __name__ == '__main__':
    unittest.main()
