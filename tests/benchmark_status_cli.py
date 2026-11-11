"""Alternating v2/new timings for status inspection and command parsing."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.cli import parser as old_parser
from midas_formalizer.run_cli import inspect_run
from midas_formalizer.store import RunStore
from midas.cli.arguments import parser
from midas.application.inspection import Inspector
from tests.benchmark_application import compare
from tests.test_application import Contracts


def main():
    compare('build/parse status command', lambda: old_parser().parse_args(['inspect', '--run-dir', '/tmp/run', '--json']),
        lambda: parser().parse_args(['status', '/tmp/run', '--json']), 100)
    fixture = Contracts()
    fixture.setUp()
    try:
        with fixture.start() as run, TemporaryDirectory() as temporary:
            with RunStore.create(Path(temporary)/'v2') as old:
                old.start_session({})
                old.tasks.set_turn_limit(400)
                reader = Inspector()
                compare('read committed status', lambda: inspect_run(old.directory),
                    lambda: reader.inspect(run.writer.directory), 100)
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
