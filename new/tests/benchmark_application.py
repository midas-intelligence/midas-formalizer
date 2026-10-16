"""Alternating application inspection/idle timings; no Lean or model calls."""
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from timeit import timeit
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.run_cli import inspect_run
from midas_formalizer.store import RunStore
from midas_formalizer.controller import Controller as OldController
from midas_formalizer.scheduling import Limits
from tests.test_application import Contracts


def compare(name, old, new, number):
    samples = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1-sample % 2):
            samples[side].append(timeit((old, new)[side], number=number)/number)
    before, after = map(median, samples)
    print(f'{name}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


def main():
    fixture = Contracts()
    fixture.setUp()
    try:
        with fixture.start() as run, TemporaryDirectory() as temporary:
            with RunStore.create(Path(temporary)/'v2') as store:
                store.start_session({})
                store.tasks.set_turn_limit(400)
                store.artifacts.put(b'inputs', max_bytes=100)
                old = OldController(store, Limits(model_concurrency=2))
                try:
                    run.step()
                    compare('inspect command', lambda: inspect_run(store.directory),
                        lambda: fixture.app.inspect(fixture.root/'run'), 30)
                    compare('idle application/controller', lambda: old.tick(None, None),
                        lambda: (run.step(), run.workflow.progress(run.writer)), 300)
                finally:
                    old.close()
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
