"""Opt-in controller/compiler/storage integration. Requires AGENTS.md approval."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import sys
import time

from midas.checker import Checker, ProbeCase, inspect
from midas.controller import Controller, Effect, Operations, Planned, checker_operation
from midas.domain import (ArtifactRef, Candidate, CapacityGrant, FrozenRequest, Job, JobKind,
    OwnerVersion, ProofGraph, ProofNode, ProofState, ResourceLimits, ResultStatus, SelectionRequest,
    accept_checked_proof)
from midas.environment import Environment, PackageConfig
from midas.executor import Executor
from midas.policy import AdmissionRequest, Config, Policy
from midas.storage import Changes, FrozenRun, Intent, NodeChange, create
from tests.test_checker import Artifacts
from tests.test_executor import invocation, success


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-controller-lean-') as temporary:
        root = Path(temporary)
        project = root/'project'
        project.mkdir()
        (project/'Main.lean').write_text('import Lean\ntheorem target : True := by sorry\n')
        config = PackageConfig('4.35.0-rc3', toolchain, (('project', project),),
                               ('project/Main.lean',), ('project', 'toolchain/lib/lean'))
        values, state = Artifacts(), {}
        with Executor(root, workers=1, compiler_slots=1, compiler_memory_mib=4096, progress_capacity=1) as executor:
            def setup(request, context):
                environment = Environment.prepare(root/'package', config, context)
                checker = Checker(environment, '/tmp/midas-new-checker-runtime')
                selected = inspect(checker, SelectionRequest('Main.lean', all_unfinished=True), context)
                state.update(checker=checker, selected=selected)
                return success(request, context)
            bootstrap = invocation(limits=ResourceLimits(600000, 4096, 4*1024**2, 64*1024**2))
            workspace = root/'bootstrap'
            workspace.mkdir()
            executor.start(bootstrap, setup, grant=CapacityGrant(bootstrap), workspace=workspace, reader=values, staging=values)
            while not (completed := executor.poll()):
                time.sleep(.02)
            assert completed[0].result.status == ResultStatus.SUCCEEDED, completed
            checker, selected = state['checker'], state['selected']
            print('environment and source inspection: passed', flush=True)
            with create(root/'run') as writer:
                ref = writer.put(b'frozen controller inputs', max_bytes=100)
                owner = OwnerVersion(selected.identities[0].id, 0)
                node = ProofNode(owner, selected.environment_id, ref)
                with writer.transaction(writer.session) as tx:
                    tx.freeze(FrozenRun(selected.environment_id, ref, ref))
                    tx.apply('initial', Changes(nodes=(NodeChange(None, node),), roots=(owner.obligation_id,), graph_revision=0))
                proof = Candidate(str(uuid4()), owner, selected.environment_id, writer.put(b'by trivial', max_bytes=100))
                policy = Policy(Config())
                jobs = []
                for key, kind, request, depends in (
                    ('binding', JobKind.CHECK_BINDING, checker.prepare('binding', selected, 0, owner), ()),
                    ('proof', JobKind.CHECK_PROOF, checker.prepare('proof', selected, 0, proof, writer), ('binding',)),
                    ('probe', JobKind.PROBE, checker.prepare('probe', selected, 0, owner,
                        (ProbeCase('open', 'skip'), ProbeCase('closed', 'trivial'))), ())):
                    payload = writer.put(request.payload, max_bytes=len(request.payload))
                    inputs = tuple(ArtifactRef.from_dict(r) for r in request.decode()['inputs'])
                    job = Job(key, kind, owner, inputs, (owner,), policy.config.checker.limits(), depends)
                    jobs.append(Planned(Intent(FrozenRequest(job, selected.environment_id, payload, payload)), AdmissionRequest(job)))
                def reduce(reader, invocation, accepted):
                    if accepted.failure:
                        raise AssertionError((invocation.request.job.semantic_key, accepted.failure))
                    current = reader.node(owner.obligation_id)
                    revision = reader.status_snapshot().graph_revision
                    if invocation.request.job.kind == JobKind.CHECK_BINDING:
                        node = replace(current.node, binding=accepted.value.admitted, interface=accepted.value.admitted.interface)
                    elif invocation.request.job.kind == JobKind.CHECK_PROOF:
                        graph = ProofGraph(revision, (owner.obligation_id,), (current.node,))
                        node = accept_checked_proof(graph, accepted.value).node(owner)
                    else:
                        return Effect()
                    return Effect(Changes(nodes=(NodeChange(current.version, node),), graph_revision=revision))
                operation = checker_operation(checker, reduce, candidate=lambda *args: proof)
                controller = Controller(writer, executor, policy,
                    Operations(binding=operation, proof=operation, probe=operation), batch=1)
                controller.publish('checks', Effect(work=tuple(jobs)))
                until = time.monotonic()+180
                while dict(writer.status_snapshot().intents).get('DONE', 0) != 3 and time.monotonic() < until:
                    controller.step()
                    time.sleep(.005)
                assert dict(writer.status_snapshot().intents).get('DONE') == 3, writer.status_snapshot()
                assert writer.node(owner.obligation_id).node.state == ProofState.VERIFIED
                probe_id = writer.intent_state('probe')[1]
                assert len(writer.progress(probe_id)) == 4
                assert writer.status_snapshot().accounting.turns == 0
                assert controller.drain(timeout=5)
                writer.validate(force_artifacts=True)
                print('binding, direct proof, probe progress, receipts and drain: passed', flush=True)


if __name__ == '__main__':
    main()
