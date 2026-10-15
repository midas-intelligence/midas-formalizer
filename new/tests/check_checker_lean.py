"""Opt-in checker integration checks. Root AGENTS.md requires Lean permission."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import sys
import time

from midas.checker import Checker, ProbeCase, admit_probe_progress, inspect, worker_payload
from midas.domain import (Candidate, CapacityGrant, Dependency, DependencyOrigin, ObligationIdentity, OwnerVersion,
    ReceiptBinding, ResourceLimits, ResultStatus, SelectionRequest)
from midas.domain import encoding as codec
from midas.environment import Environment, PackageConfig
from midas.executor import Executor
from tests.test_checker import Artifacts, issue, name
from tests.test_executor import invocation, success


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    artifacts = Artifacts()
    with TemporaryDirectory(prefix='midas-checker-lean-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        (project / 'Main.lean').write_text('import Lean\n\ntheorem target : True := by sorry\n')
        (project / 'Joint.lean').write_text('def witness : Nat := sorry\ntheorem property : witness = witness := by sorry\n')
        config = PackageConfig('4.35.0-rc3', toolchain, (('project', project),),
                               ('project/Main.lean', 'project/Joint.lean'), ('project', 'toolchain/lib/lean'))
        with Executor(root, workers=1, compiler_slots=1, compiler_memory_mib=4096) as executor:
            requests = {}
            def execute(inv, callback, *, compiler_request=None, cancel_after=None):
                if compiler_request is not None:
                    requests[inv.original_execution_id] = compiler_request, inv
                workspace = root / str(uuid4())
                workspace.mkdir()
                executor.start(inv, callback, grant=CapacityGrant(inv), workspace=workspace,
                               reader=artifacts, staging=artifacts)
                started = time.monotonic()
                while not (completed := executor.poll()):
                    for progress in executor.poll_progress():
                        issued, original = requests[progress.invocation_id]
                        admit_probe_progress(issued, original, progress, artifacts)
                    if cancel_after is not None and time.monotonic() - started > cancel_after:
                        executor.cancel(inv.original_execution_id)
                        cancel_after = None
                    time.sleep(.02)
                for progress in executor.poll_progress():
                    issued, original = requests[progress.invocation_id]
                    admit_probe_progress(issued, original, progress, artifacts)
                return completed[0]

            state = {}
            def setup(request, context):
                env = Environment.prepare(root / 'package', config, context)
                print('environment: prepared', flush=True)
                checker = Checker(env, '/tmp/midas-new-checker-runtime')
                selected = inspect(checker, SelectionRequest('Main.lean', all_unfinished=True), context)
                joint = inspect(checker, SelectionRequest('Joint.lean', all_unfinished=True), context)
                state.update(checker=checker, selection=selected, joint=joint)
                print('native runtime and binary inspection: passed', flush=True)
                return success(request, context)

            boot = execute(invocation(limits=ResourceLimits(600000, 4096, 4*1024**2, 64*1024**2)), setup)
            if boot.result.status != ResultStatus.SUCCEEDED:
                raise AssertionError(boot)
            checker, selected = state['checker'], state['selection']
            owner = OwnerVersion(selected.identities[0].id, 0)
            authority = SimpleNamespace(verify_receipt=lambda *a: True)

            def check(label, request, *, candidate=None, valid=True, limits=None, cancel_after=None, expected_outcome=None):
                inv = issue(request, artifacts, limits=limits)
                start = time.monotonic()
                completed = execute(inv, checker.execute, compiler_request=request, cancel_after=cancel_after)
                result = completed.result
                if (result.status == ResultStatus.SUCCEEDED) != valid:
                    print(completed, flush=True)
                    for ref in result.staged_artifacts:
                        data = artifacts.data[ref.id]
                        if data.startswith(codec.MAGIC) and len(data) < 10000:
                            print(codec.decode(data), flush=True)
                        elif data and data[0] >= 32 and len(data) < 10000:
                            print(data.decode(errors='replace'), flush=True)
                    raise AssertionError(label)
                if expected_outcome is not None:
                    report = codec.load_record(artifacts.data[result.output.id], limit=65536)
                    assert report['outcome'] == expected_outcome, report
                if valid:
                    report = codec.load_record(artifacts.data[result.output.id], limit=65536)
                    for phase in report['phases']:
                        if phase['outcome'] != 'completed':
                            print('fallback:', phase['phase'], phase['outcome'],
                                  artifacts.data[phase['stderr']['artifact']['id']].decode(errors='replace')[:2000],
                                  artifacts.data[phase['stdout']['artifact']['id']].decode(errors='replace')[:2000], flush=True)
                    if checker.retained and checker.runtime.retained:
                        assert all(p['outcome'] == 'completed' for p in report['phases']), 'Unexpected retained fallback'
                    receipt = ReceiptBinding(inv.original_execution_id, inv.request.request_ref,
                        artifacts.save(worker_payload(result)), artifacts.save(b'test-controller-seal'))
                    admitted = checker.admit_receipt(request, inv, result, receipt, artifacts,
                                                     authority, candidate=candidate)
                else:
                    admitted = None
                print(f'{label}: passed ({time.monotonic()-start:.2f}s)', flush=True)
                return result, admitted

            candidate = Candidate(str(uuid4()), owner, selected.environment_id, artifacts.save(b'by trivial'))
            def heartbeat_checks(label):
                from midas.policy import load_config
                policy = load_config(b'{"format":"midas-config-v1","checker":{"max_heartbeat":12345}}')
                limits = policy.checker.limits()
                for count in (12345, 0):
                    code = ('by\n  run_tac do\n'
                            '    unless Lean.maxHeartbeats.get (← Lean.getOptions) == ' + str(count) + ' do\n'
                            '      throwError "heartbeat limit mismatch"\n  trivial').encode()
                    proof = replace(candidate, id=str(uuid4()), source=artifacts.save(code))
                    check(label + ' configured heartbeat ' + str(count),
                        checker.prepare('proof', selected, 0, proof, artifacts), candidate=proof,
                        limits=replace(limits, max_heartbeat=count))
                result, _ = check(label + ' heartbeat interruption', checker.prepare('probe', selected, 0,
                    candidate.owner, (ProbeCase('heartbeat', 'repeat skip'),)), limits=limits)
                report = codec.load_record(artifacts.data[result.output.id], limit=65536)
                rows = codec.load_record(artifacts.data[report['evidence']['id']], limit=65536)
                outcome = codec.decode(artifacts.data[rows[0]['id']])['row']['result']
                assert outcome['status'] == 'interrupted', outcome
                assert '12345' in outcome['diagnostic'] and 'heartbeat' in outcome['diagnostic'], outcome

            heartbeat_checks('retained')
            check('direct proof with native replay', checker.prepare('proof', selected, 0, candidate, artifacts), candidate=candidate)
            checker.retained = False
            heartbeat_checks('fresh')
            check('explicit fresh proof with native replay', checker.prepare('proof', selected, 0, candidate, artifacts), candidate=candidate)
            checker.retained = True
            for code in (b'by sorry', b'by exact False.elim (by trivial)',
                         b'by run_tac Lean.Elab.Command.liftTermElabM (pure ()); trivial'):
                bad = replace(candidate, id=str(uuid4()), source=artifacts.save(code))
                check('proof rejection', checker.prepare('proof', selected, 0, bad, artifacts), candidate=bad, valid=False)
            check('target binding', checker.prepare('binding', selected, 0, owner, parameters=('h',)))
            result, _ = check('elaborated statement binding/replay',
                checker.prepare('binding', selected, 0, owner, statement='Nat', parameters=('h',)))
            report = codec.load_record(artifacts.data[result.output.id], limit=65536)
            bound = codec.decode(artifacts.data[report['evidence']['id']])
            sig = bound['certificate']['signature']
            derived = ObligationIdentity(*(codec.encode(sig[k]) for k in ('target', 'universes', 'context')),
                                          selected.environment_id, selected.identities[0].prerequisite_interfaces)
            proof = Candidate(str(uuid4()), OwnerVersion(derived.id, 0), selected.environment_id, artifacts.save(b'1'))
            check('derived definition proof', checker.prepare('proof', selected, 0, proof, artifacts,
                  identity=derived, statement='Nat'), candidate=proof)
            child = ObligationIdentity(codec.encode(['const', name('False'), []]), codec.encode([]),
                                       codec.encode(['closed-telescope']), selected.environment_id)
            iface = artifacts.save(codec.encode({'constant': name('h'), 'signature': {
                k: codec.decode(getattr(child, k)) for k in ('target', 'universes', 'context')}}))
            conditional = replace(candidate, id=str(uuid4()), source=artifacts.save(b'by exact False.elim h'),
                                  dependencies=(Dependency(OwnerVersion(child.id, 0), 'h', iface),))
            check('conditional proof and assumption audit', checker.prepare('proof', selected, 0,
                  conditional, artifacts, dependencies=(child,)), candidate=conditional)
            check('lookup and unavailable target', checker.prepare('lookup', selected, 0, owner,
                  ('True', 'target', 'Nat.add'), include_body=True))
            check('lookup source ranges', checker.prepare('lookup', selected, 0, owner,
                  ('Nat.add',), include_body=True, source_ranges=True))
            check('probe open, closed and sorry', checker.prepare('probe', selected, 0, owner,
                (ProbeCase('open', 'skip'), ProbeCase('closed', 'trivial'), ProbeCase('sorry', 'sorry'))))
            check('probe expression and error', checker.prepare('probe', selected, 0, owner,
                (ProbeCase('term', '1', 'term'), ProbeCase('error', 'does_not_exist'))))
            check('whole-source kernel audit', checker.prepare('audit', selected, owner,
                  artifacts.save(b'theorem target : True := by trivial\n'), artifacts))
            check('whole-source rejects unrelated sorry', checker.prepare('audit', selected, owner,
                  artifacts.save(b'theorem target : True := by trivial\ntheorem other : True := by sorry\n'), artifacts), valid=False)
            joint = state['joint']
            submission = str(uuid4())
            witness = Candidate(str(uuid4()), OwnerVersion(joint.identities[0].id, 0), joint.environment_id,
                                artifacts.save(b'0'), joint_submission_id=submission)
            _, admitted = check('joint definition', checker.prepare('proof', joint, 0, witness, artifacts), candidate=witness)
            iface = artifacts.save(codec.encode({'constant': codec.load_json(joint.selected.targets[0].constant),
                'signature': {k: codec.decode(getattr(joint.identities[0], k)) for k in ('target', 'universes', 'context')}}))
            dependency = Dependency(witness.owner, 'w', iface, DependencyOrigin.CLOSED_DEFINITION, admitted.evidence, submission)
            prop = Candidate(str(uuid4()), OwnerVersion(joint.identities[1].id, 0), joint.environment_id,
                artifacts.save(b'by rfl'), (dependency,), joint_submission_id=submission)
            check('joint property replays closed definition', checker.prepare('proof', joint, 1, prop, artifacts,
                dependencies=(joint.identities[0],), closed_definitions=(admitted,)), candidate=prop)
            peer = Dependency(witness.owner, 'w', iface, DependencyOrigin.FROZEN_PEER)
            conditional_peer = replace(prop, dependencies=(peer,), joint_submission_id=None)
            check('frozen peer abstraction and source binding', checker.prepare('proof', joint, 1, conditional_peer, artifacts,
                dependencies=(joint.identities[0],)), candidate=conditional_peer)
            check('evidence budget failure', checker.prepare('proof', selected, 0, candidate, artifacts),
                candidate=candidate, valid=False, limits=ResourceLimits(120000, 4096, 1024**2, 64), expected_outcome='resource_limit')
            check('wall budget failure', checker.prepare('probe', selected, 0, owner,
                (ProbeCase('loop', 'repeat skip'),)), valid=False,
                limits=ResourceLimits(500, 4096, 1024**2, 64*1024**2), expected_outcome='resource_limit')
            cancelled, _ = check('cancellation', checker.prepare('probe', selected, 0, owner,
                (ProbeCase('loop', 'repeat skip'),)), valid=False, cancel_after=.5,
                expected_outcome='cancelled')
            assert cancelled.status == ResultStatus.CANCELLED and cancelled.termination.value == 'TERMINATED'
            env = checker.environment
            configured = Environment(env.directory, replace(env.manifest,
                settings=replace(env.manifest.settings, options=(('maxRecDepth', '2048'),))), env.toolchain, env.launcher)
            checker = Checker(configured, '/tmp/midas-new-checker-runtime')
            selected = replace(selected, environment_id=configured.id,
                identities=tuple(replace(i, environment_id=configured.id) for i in selected.identities))
            candidate = replace(candidate, environment_id=configured.id, owner=OwnerVersion(selected.identities[0].id, 0))
            check('configured interpreted checker', checker.prepare('proof', selected, 0, candidate, artifacts), candidate=candidate)
            heartbeat_checks('interpreted')
    print('checker Lean checks passed', flush=True)


if __name__ == '__main__':
    main()
