"""Alternating controller lifecycle/idle timings, without compiler or network I/O."""
from concurrent.futures import Future
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory
from timeit import timeit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.controller import Controller as OldController, Result
from midas_formalizer.scheduling import Limits
from midas_formalizer.store import RunStore
from midas_formalizer.tasks import UsageReport
from tests.test_controller import ControllerTests


class Immediate:
    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        pass


def compare(label, old, new, *, count=30):
    samples = ([], [])
    for index in range(7):
        for side in (index % 2, 1-index % 2):
            samples[side].append(timeit((old, new)[side], number=count)/count)
    before, after = map(median, samples)
    print(f'{label}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}', flush=True)


def main():
    fixture = ControllerTests()
    fixture.setUp()
    current = fixture.build()
    current.step()
    try:
        with TemporaryDirectory() as temporary, RunStore.create(Path(temporary)/'old') as store:
            store.start_session({})
            store.tasks.set_turn_limit(10000)
            old = OldController(store, Limits(model_concurrency=2))
            old.pool.shutdown()
            old.pool = Immediate()
            ref = store.artifacts.put(b'inputs', max_bytes=100)
            with store.transaction(store._session) as db:
                db.execute("INSERT INTO task_owners VALUES ('owner',0)")
            counters = [0, 0]

            def previous():
                counters[0] += 1
                old.publish(key=['job', counters[0]], operation='generate', owner_id='owner', generation=0,
                            input_artifact=ref, lineage='test', memory_mib=0, downstream=1)
                old.tick(None, lambda *a: Result(b'ok', usage=UsageReport('receipt', 10, 5, 100)), context=lambda _: b'inputs')
                token = old.poll()[0]
                with store.transaction(store._session) as db:
                    _, receipt = store.tasks._acceptance(db, token)
                    store.tasks._accept(db, token, receipt)
                    old.finish(db, token)

            def rewritten():
                counters[1] += 1
                fixture.publish(fixture.planned('job'+str(counters[1]), downstream=1))
                current.step()
                fixture.executor.finish()
                current.step()

            compare('idle', lambda: old.tick(None, lambda *a: None), current.step, count=100)
            compare('model_lifecycle', previous, rewritten, count=10)
            compare('idle_with_history', lambda: old.tick(None, lambda *a: None), current.step, count=100)
            old.close()
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
