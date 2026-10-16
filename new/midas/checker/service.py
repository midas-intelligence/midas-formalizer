"""Compiler execution and trusted receipt admission, without storage or scheduling."""
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..domain import (AdmittedBinding, AdmittedCheck, ArtifactRef, CheckKind, DeliveryCertainty,
    ObligationIdentity, OwnerVersion, ResultStatus, Termination, Usage, WorkerResult)
from ..domain import encoding as codec
from ..environment import AuthorizedInput
from ..executor import Mount, Outcome, WorkerStopped
from . import evidence as ev
from .requests import (CompilerRequest, MAX_BYTES, _candidate, read, prepare_binding,
                       prepare_lookup, prepare_probe, prepare_proof, prepare_audit)
from .runtime import prepare_runtime

META_BYTES = 64 * 1024


class ReceiptVerifier(Protocol):
    """Controller/storage capability; must authenticate its durable receipt seal."""
    def verify_receipt(self, receipt, invocation, result) -> bool: ...


@dataclass(frozen=True, slots=True)
class Advisory:
    operation: str
    evidence: ArtifactRef
    receipt: object


@dataclass(frozen=True, slots=True)
class Binding:
    admitted: AdmittedBinding
    identity: ObligationIdentity
    parameters: tuple[bytes, ...]
    evidence: ArtifactRef


@dataclass(frozen=True, slots=True)
class ProbeProgress:
    case_id: str
    sequence: int
    evidence: ArtifactRef | None


def admit_probe_progress(request, invocation, message, reader):
    """Validate executor-delivered advisory progress; never mint proof authority."""
    value = request.validate_for(invocation.request, reader)
    if value['operation'] != 'probe' or message.invocation_id != invocation.original_execution_id:
        raise ValueError('Probe progress differs from invocation')
    index, complete = divmod(message.sequence, 2)
    cases = value['packet']['cases']
    if index >= len(cases):
        raise ValueError('Unexpected probe progress sequence')
    case = cases[index]
    expected = ('probe_completed:' if complete else 'probe_started:') + case['id']
    if message.kind != expected or len(message.artifacts) != complete:
        raise ValueError('Probe progress differs from case')
    reference = message.artifacts[0] if complete else None
    if reference is not None:
        ev.probe_for(codec.decode(read(reader, reference, invocation.request.job.limits.evidence_bytes)), case, request.sha256)
    return ProbeProgress(case['id'], message.sequence, reference)


def worker_payload(result):
    """Exact small result bytes for the future controller's receipt boundary."""
    value = {'invocation': result.invocation_id, 'request': result.request_hash,
        'status': result.status.value, 'artifacts': [r.to_dict() for r in result.staged_artifacts],
        'output': result.output.to_dict() if result.output else None,
        'diagnostics': result.diagnostics.to_dict() if result.diagnostics else None,
        'usage': [getattr(result.usage, k) for k in ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')],
        'delivery': result.delivery_certainty.value, 'termination': result.termination.value}
    return b''.join(codec.record_parts(value, limit=META_BYTES))


class _Rejected(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


class Checker:
    def __init__(self, environment, cache, *, runtime=None, retained=True):
        self.environment, self.cache = environment, Path(cache)
        self.runtime, self.retained = runtime, retained
        if type(retained) is not bool:
            raise ValueError('Expected retained-worker flag')
        if runtime is not None and runtime.environment_id != environment.id:
            raise ValueError('Checker runtime belongs to another environment')

    def prepare(self, operation, selection, *args, **kwargs):
        if selection.environment_id != self.environment.id:
            raise ValueError('Compiler selection belongs to another environment')
        source = self.environment.selection_inputs().sources_by_path.get(selection.selected.request.source)
        if source is None or source.byte_count != len(selection.source) or source.sha256 != sha256(selection.source).hexdigest():
            raise ValueError('Compiler source differs from frozen environment input')
        operations = {'binding': prepare_binding, 'proof': prepare_proof,
                      'lookup': prepare_lookup, 'probe': prepare_probe, 'audit': prepare_audit}
        if operation not in operations:
            raise ValueError('Unknown compiler operation')
        return operations[operation](selection, *args, available=frozenset(self.environment.manifest.modules), **kwargs)

    def inspect(self, request, context):
        from .inspection import inspect
        return inspect(self, request, context)

    def execute(self, frozen, context):
        """Executor callback. Every attempt and partial result uses this staging path."""
        staged, phases = [], []
        accepted = output = diagnostics = None
        outcome, termination = 'setup_failure', Termination.TERMINATED
        limits = frozen.job.limits
        runtime = self.runtime
        clean_workspace = None

        def save(data, maximum=MAX_BYTES):
            if type(data) is not bytes or len(data) > maximum:
                raise ValueError('Staged compiler artifact exceeds limit')
            reference = context.staging.write_bounded(data, max_bytes=maximum)
            reference.verify(data, max_bytes=maximum)
            if reference not in staged:
                staged.append(reference)
            return reference

        def stage(name, process):
            nonlocal termination
            if process.termination != Termination.TERMINATED:
                termination = Termination.UNKNOWN
            streams = {}
            for label in ('stdout', 'stderr', 'evidence'):
                stream = getattr(process, label)
                if stream is None:
                    streams[label] = None
                else:
                    ref = save(stream.data, limits.evidence_bytes if label == 'evidence' else limits.output_bytes)
                    streams[label] = {'artifact': ref.to_dict(), 'observed': stream.observed_bytes,
                                      'truncated': stream.truncated}
            phases.append({'phase': name, 'outcome': process.outcome.value, 'returncode': str(process.returncode) if process.returncode is not None else None,
                           'limit': process.limit, 'elapsed_ms': process.elapsed_ms,
                           'peak_rss_bytes': process.peak_rss_bytes, 'peak_anonymous_bytes': process.peak_anonymous_bytes,
                           'detail': process.detail,
                           'termination': process.termination.value, **streams})

        def run(name, packet, native=None):
            nonlocal diagnostics
            context.check()
            payload = codec.encode(packet)
            request_path = context.workspace / 'Packet.bin'
            request_path.write_bytes(payload)
            files = [request_path]
            if native is not None:
                native_path = context.workspace / (packet['native_proof'] + '.olean')
                native_path.write_bytes(native)
                files.append(native_path)
            inputs = self.environment.materialize(tuple(AuthorizedInput(p.name, p,
                sha256(payload if p == request_path else native).hexdigest(), p.stat().st_size) for p in files),
                context.workspace / ('inputs-' + str(uuid4())), check=context.check)
            channel = context.workspace / ('channel-' + str(uuid4()))
            channel.mkdir()
            fresh = runtime.launch(self.environment, limits.memory_mib, inputs=inputs)
            fresh = replace(fresh, mounts=(*fresh.mounts, Mount(str(channel), '/channel')),
                            variables=(*fresh.variables, ('MIDAS_EVIDENCE_PATH', '/channel/evidence')))
            if clean_workspace is not None:
                from .rebuild import clean_launch
                fresh = clean_launch(fresh, clean_workspace)
            if self.retained and runtime.retained and clean_workspace is None:
                attempts = context.run_import(runtime.recipe(self.environment, limits.memory_mib, files),
                    fresh=fresh, evidence_path=channel / 'evidence').attempts
            else:
                attempts = (context.run_process(fresh, evidence_path=channel / 'evidence'),)
            for result in attempts:
                stage(name, result)
            result = attempts[-1]
            if result.outcome != Outcome.COMPLETED:
                if phases[-1]['stderr'] is not None:
                    diagnostics = ArtifactRef.from_dict(phases[-1]['stderr']['artifact'])
                failure = result.outcome.value
                if result.outcome == Outcome.COMPILER_ERROR:
                    failure = ('recursion_depth' if b'MIDAS_RECURSION_DEPTH\n' in result.stderr.data else
                               'resource_limit' if b'MIDAS_EVIDENCE_LIMIT\n' in result.stderr.data else
                               'invalid_candidate' if b'MIDAS_CHECK_REJECTED\n' in result.stderr.data else 'crash')
                raise _Rejected(failure)
            if (result.returncode != 0 or result.termination != Termination.TERMINATED
                    or result.evidence is None or result.evidence.truncated
                    or result.stdout.truncated or result.stderr.truncated):
                raise ValueError('Incomplete compiler execution evidence')
            return ev.decode_output(result.evidence.data, maximum=limits.evidence_bytes)

        try:
            context.check()
            request = CompilerRequest(read(context.reader, frozen.operation_payload))
            value = request.validate_for(frozen, context.reader)
            if value['environment'] != self.environment.id:
                raise ValueError('Checker environment differs from request')
            if context.request_hash != frozen.request_hash:
                raise ValueError('Worker context differs from request')
            runtime = runtime or prepare_runtime(self.environment, context, self.cache)
            self.runtime = runtime
            operation, packet, digest = value['operation'], value['packet'], request.sha256
            packet.update(request=digest, max_heartbeat=limits.max_heartbeat, max_rec_depth=limits.max_rec_depth)
            if operation == 'proof':
                cert, binaries = run('produce', dict(packet, mode='produce'))
                ev.proof_for(cert, packet, digest)
                expected = ev.replay_receipt(cert, digest)
                replay = dict(packet, mode='replay', candidate=None, certificate=cert, replay_receipt=expected)
                native = next((data for name, data in binaries if name == 'producer'), None)
                if native is not None:
                    replay['native_proof'] = 'MidasDecl_' + sha256(native).hexdigest()
                result, _ = run('replay', replay, native)
                if result != expected:
                    raise ValueError('Kernel replay receipt differs')
                accepted = save(codec.encode(cert), limits.evidence_bytes)
            elif operation == 'replay':
                cert = dict(packet['certificate'], request=digest)
                ev.certificate(cert, digest)
                if cert['format'] != ev.PROOF or 'assumptions' in cert:
                    raise ValueError('Assembly requires a closed source certificate')
                expected = ev.replay_receipt(cert, digest)
                result, _ = run('replay', dict(packet, mode='replay', candidate=None,
                    certificate=cert, replay_receipt=expected))
                if result != expected:
                    raise ValueError('Assembly kernel replay receipt differs')
                accepted = save(codec.encode(cert), limits.evidence_bytes)
            elif operation == 'binding':
                if packet['interface'] is not None:
                    packet['interface']['request'] = digest
                result, _ = run('produce', dict(packet, mode='bindings'))
                ev.binding_for(result, packet, digest)
                if packet['interface'] is not None:
                    replay = dict(packet, mode='bindings', interface=dict(packet['interface'], mode='replay',
                                                                        certificate=result['certificate']))
                    checked, _ = run('replay', replay)
                    ev.binding_for(checked, packet, digest)
                    if codec.encode(checked) != codec.encode(result):
                        raise ValueError('Binding replay changed the interface')
                accepted = save(codec.encode(result), limits.evidence_bytes)
                sig = result['certificate']['signature'] if result['certificate'] else packet['signature']
                save(codec.encode({'constant': packet['constant'], 'signature': sig}), limits.evidence_bytes)
            elif operation == 'lookup':
                result, _ = run('lookup', dict(packet, mode='lookup'))
                ev.lookup_for(result, packet, digest)
                accepted = save(codec.encode(result), limits.evidence_bytes)
            elif operation in ('audit', 'integrate'):
                if operation == 'audit' and 'modules' in packet:
                    from .rebuild import rebuild
                    clean_workspace, built = rebuild(self.environment, packet, context,
                        maximum=value.get('export', {}).get('maximum', 16*1024**3))
                    stage('build', built)
                    if built.outcome != Outcome.COMPLETED or built.returncode != 0:
                        diagnostics = ArtifactRef.from_dict(phases[-1]['stderr']['artifact'])
                        raise _Rejected('invalid_candidate' if built.outcome == Outcome.COMPILER_ERROR else built.outcome.value)
                result, _ = run('audit', dict(packet, mode='audit'))
                self._audit_result(result, packet, digest)
                accepted = save(codec.encode(result), limits.evidence_bytes)
            elif operation == 'probe':
                rows = []
                cases = packet.pop('cases')
                for case in cases:
                    context.emit('probe_started:' + case['id'])
                    result, _ = run('probe:' + case['id'], dict(packet, mode='probe', case=case))
                    ev.probe_for(result, case, digest)
                    reference = save(codec.encode(result), limits.evidence_bytes)
                    context.emit('probe_completed:' + case['id'], (reference,))
                    rows.append(reference.to_dict())
                accepted = save(b''.join(codec.record_parts(rows, limit=META_BYTES)), META_BYTES)
            else:
                raise ValueError('Unsupported compiler execution operation')
            outcome = 'succeeded'
        except _Rejected as error:
            outcome = error.outcome
        except WorkerStopped:
            outcome = 'cancelled' if context.cancellation.is_set() else 'resource_limit'
        except codec.GraphLimit:
            outcome = 'resource_limit'
        except Exception as error:
            # A bad artifact, setup failure or staging cap never becomes proof evidence.
            outcome = 'setup_failure' if isinstance(error, (OSError, ValueError, TypeError, KeyError)) else 'crash'
            try:
                diagnostics = save(str(error).encode(errors='replace')[:4096], 4096)
            except (OSError, ValueError):
                pass
        try:
            output = save(b''.join(codec.record_parts({'format': 'midas-compiler-result-v1',
                'request': frozen.operation_payload.to_dict(), 'runtime': runtime.id if runtime else None,
                'outcome': outcome, 'evidence': accepted.to_dict() if outcome == 'succeeded' else None,
                'phases': phases}, limit=META_BYTES)), META_BYTES)
        except (OSError, ValueError):
            outcome = 'setup_failure'
        status = (ResultStatus.SUCCEEDED if outcome == 'succeeded' else ResultStatus.CANCELLED
                  if outcome == 'cancelled' else ResultStatus.FAILED)
        return WorkerResult(context.invocation_id, frozen.request_hash, status, tuple(staged), output, diagnostics,
            Usage(0, 0, 0, 0), DeliveryCertainty.DELIVERED, termination)

    def admit_receipt(self, request, invocation, result, receipt, reader, authority, *, candidate=None):
        """Authenticate exact execution, then return a domain handoff or advisory evidence."""
        result.validate_for(invocation)
        value = request.validate_for(invocation.request, reader)
        if (receipt.original_execution_id != invocation.original_execution_id
                or receipt.request_ref != invocation.request.request_ref
                or read(reader, receipt.result_ref, META_BYTES) != worker_payload(result)
                or authority.verify_receipt(receipt, invocation, result) is not True):
            raise ValueError('Unauthenticated compiler receipt')
        if (result.status != ResultStatus.SUCCEEDED or result.termination != Termination.TERMINATED
                or result.delivery_certainty != DeliveryCertainty.DELIVERED or result.output is None):
            raise ValueError('Compiler receipt did not complete successfully')
        report = codec.load_record(read(reader, result.output, META_BYTES), limit=META_BYTES)
        codec.fields(report, ('format', 'request', 'runtime', 'outcome', 'evidence', 'phases'))
        if (report['format'] != 'midas-compiler-result-v1' or report['outcome'] != 'succeeded'
                or report['request'] != invocation.request.operation_payload.to_dict()
                or self.runtime is None or report['runtime'] != self.runtime.id
                or value['environment'] != self.environment.id):
            raise ValueError('Compiler report differs from admitted runtime/request')
        reference = ArtifactRef.from_dict(report['evidence'])
        if reference not in result.staged_artifacts:
            raise ValueError('Unstaged proof evidence')
        operation, packet, digest = value['operation'], value['packet'], request.sha256
        expected = (['produce', 'replay'] if operation == 'proof' or operation == 'binding' and packet['interface']
                    else ['replay'] if operation == 'replay'
                    else ['build', 'audit'] if operation == 'audit' and 'modules' in packet
                    else ['audit'] if operation in ('audit', 'integrate')
                    else ['produce'] if operation == 'binding' else [operation] if operation == 'lookup'
                    else ['probe:' + c['id'] for c in packet['cases']])
        outputs = self._admit_phases(report['phases'], expected, result, invocation, reader)
        payload = read(reader, reference, invocation.request.job.limits.evidence_bytes)
        if operation == 'proof':
            if candidate is None or _candidate(candidate) != value['candidate']:
                raise ValueError('Accepted candidate differs from request')
            cert = ev.proof_for(codec.decode(payload), packet, digest)
            if codec.encode(outputs[0]) != payload or outputs[1] != ev.replay_receipt(cert, digest):
                raise ValueError('Proof evidence differs from producer/replay')
            return AdmittedCheck(candidate, CheckKind.CONDITIONAL if candidate.logical_dependencies else CheckKind.DIRECT,
                                 receipt, reference)
        if operation == 'replay':
            cert = dict(packet['certificate'], request=digest)
            ev.certificate(cert, digest)
            if (cert['format'] != ev.PROOF or 'assumptions' in cert
                    or codec.encode(cert) != payload or outputs[0] != ev.replay_receipt(cert, digest)):
                raise ValueError('Assembly evidence differs from kernel replay')
            return Advisory(operation, reference, receipt)
        if operation == 'binding':
            bound = ev.binding_for(codec.decode(payload), packet, digest)
            if any(codec.encode(o) != payload for o in outputs):
                raise ValueError('Binding differs from producer/replay')
            sig = bound['certificate']['signature'] if bound['certificate'] else packet['signature']
            identity = ObligationIdentity(*(codec.encode(sig[k]) for k in ('target', 'universes', 'context')),
                value['environment'], tuple(codec.encode(i) for i in packet['prerequisite_interfaces']))
            interface = codec.encode({'constant': packet['constant'], 'signature': sig})
            matching = [r for r in result.staged_artifacts if r.sha256 == sha256(interface).hexdigest()
                        and r.byte_count == len(interface)]
            if not matching or read(reader, matching[0]) != interface:
                raise ValueError('Missing checked binding interface')
            admitted = AdmittedBinding(OwnerVersion(identity.id, value['owner'][1]), value['environment'], matching[0], receipt)
            return Binding(admitted, identity, tuple(codec.encode(n) for n in bound['parameters']), reference)
        if operation == 'lookup':
            ev.lookup_for(codec.decode(payload), packet, digest)
            if codec.encode(outputs[0]) != payload:
                raise ValueError('Lookup evidence differs from execution')
        elif operation in ('audit', 'integrate'):
            if operation == 'audit' and 'modules' in packet:
                from .rebuild import admit_build
                admit_build(outputs.pop(0), packet)
            self._audit_result(codec.decode(payload), packet, digest)
            if codec.encode(outputs[0]) != payload:
                raise ValueError('Audit evidence differs from execution')
        else:
            refs = codec.load_record(payload, limit=META_BYTES)
            if len(refs) != len(packet['cases']):
                raise ValueError('Probe batch differs')
            for case, raw, output in zip(packet['cases'], refs, outputs):
                ref = ArtifactRef.from_dict(raw)
                if ref not in result.staged_artifacts:
                    raise ValueError('Unstaged probe case')
                data = read(reader, ref, invocation.request.job.limits.evidence_bytes)
                ev.probe_for(codec.decode(data), case, digest)
                if codec.encode(output) != data:
                    raise ValueError('Probe evidence differs from execution')
        return Advisory(operation, reference, receipt)

    @staticmethod
    def _audit_result(result, packet, digest):
        codec.fields(result, ('format', 'request', 'targets'))
        expected = 'midas-source-audit-sorry-v1' if packet.get('allow_sorry', False) else 'midas-source-audit-v1'
        if (result['format'] != expected or result['request'] != digest
                or codec.encode(result['targets']) != codec.encode(packet['targets'])):
            raise ValueError('Source audit differs from requested targets')

    @staticmethod
    def _admit_phases(phases, expected, result, invocation, reader):
        if type(phases) is not list or not 1 <= len(phases) <= 16:
            raise ValueError('Invalid compiler phase count')
        limits = invocation.request.job.limits
        used = {'stdout': 0, 'stderr': 0, 'evidence': 0}
        outputs, index = [], 0
        for phase in phases:
            codec.fields(phase, ('phase', 'outcome', 'returncode', 'limit', 'elapsed_ms',
                                'termination', 'detail', 'peak_rss_bytes', 'peak_anonymous_bytes', 'stdout', 'stderr', 'evidence'))
            from ..domain.records import natural
            natural(phase['elapsed_ms'], 'Compiler elapsed time')
            for label in ('peak_rss_bytes', 'peak_anonymous_bytes'):
                if phase[label] is not None:
                    natural(phase[label], label)
            if (type(phase['detail']) is not str or len(phase['detail']) > 4096
                    or phase['limit'] not in (None, 'wall', 'memory', 'output', 'evidence')):
                raise ValueError('Invalid compiler diagnostic metadata')
            if index >= len(expected) or phase['phase'] != expected[index] or phase['termination'] != 'TERMINATED':
                raise ValueError('Compiler phase ordering or termination differs')
            data = {}
            for label in used:
                stream = phase[label]
                if stream is None:
                    data[label] = b''
                    continue
                codec.fields(stream, ('artifact', 'observed', 'truncated'))
                ref = ArtifactRef.from_dict(stream['artifact'])
                if (ref not in result.staged_artifacts or type(stream['observed']) is not int
                        or stream['observed'] < ref.byte_count or type(stream['truncated']) is not bool):
                    raise ValueError('Invalid compiler stream accounting')
                used[label] += stream['observed']
                data[label] = read(reader, ref, limits.evidence_bytes if label == 'evidence' else limits.output_bytes)
                if stream['truncated'] and phase['outcome'] == 'completed':
                    raise ValueError('Truncated successful compiler receipt')
            if phase['outcome'] == 'completed':
                if phase['returncode'] != '0' or phase['limit'] is not None:
                    raise ValueError('Invalid successful compiler phase')
                outputs.append(ev.decode_output(data['evidence'], maximum=limits.evidence_bytes)[0])
                index += 1
            elif phase['outcome'] not in ('setup_failure', 'crash'):
                raise ValueError('Failed phase cannot establish proof evidence')
        if (index != len(expected) or used['stdout'] + used['stderr'] > limits.output_bytes
                or used['evidence'] > limits.evidence_bytes):
            raise ValueError('Incomplete or over-budget compiler receipt')
        return outputs
