"""Alternating local storage benchmarks; no Lean or semantic recovery services."""
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory
from timeit import timeit
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.store import RunStore
from midas_formalizer.staged_artifacts import StagedArtifacts
from midas_formalizer.tasks import UsageReport
from midas.domain import (DeliveryCertainty, FrozenRequest, Job, JobKind, OwnerVersion, ProofNode,
                          ResourceLimits, ResultStatus, Termination, Usage, WorkerResult)
from midas.storage import Changes, FrozenRun, Intent, NodeChange, create


def compare(label, old, new, *, count=30):
    samples = ([], [])
    for i in range(7):
        for side in (i % 2, 1-i % 2):
            samples[side].append(timeit((old, new)[side], number=count)/count)
    before, after = map(median, samples)
    print(f'{label}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


def main():
    with TemporaryDirectory() as root, RunStore.create(Path(root)/'old') as old, create(Path(root)/'new') as new:
        old.start_session({})
        for size in (128, 8192, 65536, 1024*1024):
            data = b'by\n  simp\n'*(size//10) + b'x'*(size % 10)
            counters = [0, 0]
            def before():
                counters[0] += 1
                return old.artifacts.put(counters[0].to_bytes(8, 'little')+data, max_bytes=size+8)
            def current():
                counters[1] += 1
                return new.put(counters[1].to_bytes(8, 'little')+data, max_bytes=size+8)
            compare(f'publish_{size}', before, current, count=40 if size < 65536 else 10)
            prior = old.artifacts.put(data, max_bytes=size)
            ref = new.put(data, max_bytes=size)
            compare(f'read_{size}', lambda: old.artifacts.read(prior, max_bytes=size),
                    lambda: new.read_artifact(ref, max_bytes=size), count=50 if size < 65536 else 10)
        compare('status', old.tasks.summary, new.status_snapshot, count=100)
        for size in (128, 1024*1024):
            old_stage = StagedArtifacts(old.directory)
            new_stage = new.staging(max_bytes=256*1024**2)
            counts = [0, 0]
            data = b'x'*size
            def stage_old():
                counts[0] += 1
                return old_stage.put(counts[0].to_bytes(8, 'little')+data, max_bytes=size+8)
            def stage_new():
                counts[1] += 1
                return new_stage.write_bounded(counts[1].to_bytes(8, 'little')+data, max_bytes=size+8)
            compare(f'stage_{size}', stage_old, stage_new, count=20)
            old_stage.close()
            new_stage.close()
        old.tasks.set_turn_limit(10000)
        prior = old.artifacts.put(b'request', max_bytes=100)
        ref = new.put(b'request', max_bytes=100)
        owner, env = OwnerVersion('obl_'+'a'*64, 0), 'env_'+'b'*64
        with new.transaction(new.session) as tx:
            tx.freeze(FrozenRun(env, ref, ref))
            tx.apply('initial', Changes(nodes=(NodeChange(None, ProofNode(owner, env, ref)),), graph_revision=0))
        def old_lifecycle():
            task = old.tasks.create(owner_id=owner.obligation_id, kind='model', resource='MODEL', request_artifact=prior)
            token = old.tasks.reserve(task, provider='offline', model='offline')
            old.tasks.mark_dispatched(token)
            old.tasks.save_response(token, b'ok', max_bytes=2, usage=UsageReport('receipt', 10, 5, 100))
            old.tasks.accept(token)
        def new_lifecycle():
            key = str(uuid4())
            request = FrozenRequest(Job(key, JobKind.MODEL, owner, (ref,), (owner,), ResourceLimits(1000,128,1000,1000)), env, ref, ref)
            with new.transaction(new.session) as tx:
                tx.apply(key, Changes(intents=(Intent(request),)))
                invocation = tx.reserve(key, ref, expected_revision=new.status_snapshot().revision)
            with new.transaction(new.session) as tx:
                tx.mark_dispatched(invocation)
            result = WorkerResult(invocation.original_execution_id, ref.sha256, ResultStatus.SUCCEEDED,
                (ref,), ref, None, Usage(10, 0, 5, 100), DeliveryCertainty.DELIVERED, Termination.TERMINATED)
            receipt = new.seal_result(invocation, result)
            with new.transaction(new.session) as tx:
                tx.receive(invocation, result, receipt)
                tx.apply('result:'+key, Changes(), receipt=receipt)
        compare('ledger_lifecycle', old_lifecycle, new_lifecycle, count=5)
        compare('status_with_history', old.tasks.summary, new.status_snapshot, count=100)
        data = b'raw proof '*500000
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            ref = new.put(data, max_bytes=len(data))
            assert new.read_artifact(ref, max_bytes=len(data)) == data
        print(f'large_no_json: {len(data)} bytes published and read')


if __name__ == '__main__':
    main()
