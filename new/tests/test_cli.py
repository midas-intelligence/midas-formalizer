"""CLI workflow, output and stop-protocol contracts; no Lean or model calls."""
from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from midas.application import files
from midas.cli import main
from midas.cli.arguments import parser
from midas.cli.control import Signals, endpoint, listen, message, stop
from midas.cli.display import Display
from midas.domain import encode_selection
from tests import test_application as fixtures


class CliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Contracts()
        self.fixture.setUp()
        self.root, self.app = self.fixture.root, self.fixture.app
        self.app.select = lambda *a, **kw: self.fixture.selected

    def tearDown(self):
        self.fixture.tearDown()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main([str(v) for v in args], stdout=out, stderr=err, application_factory=lambda *a, **kw: self.app)
        return code, out.getvalue(), err.getvalue()

    def test_consolidated_commands_and_rejected_ambiguous_flags(self):
        for args in (['targets'], ['inspect'], ['nodes'], ['preflight'], ['finalize'],
                     ['status', 'run', '--limit', '0'], ['stop', 'run', '--timeout', 'nan'],
                     ['status', 'run', '--limit', '1', '--limit', '2'], ['select', 'p', '--source', 'a.lean', '--tar', 'x']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(args)
        self.assertEqual(parser().parse_args(['status', 'run']).command, 'status')

    def test_select_start_status_resume_export_and_verify(self):
        selection = self.root/'selection.bin'
        code, text, error = self.cli('select', self.root/'package', '--source', 'Main.lean', '--output', selection, '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual(selection.read_bytes(), encode_selection(self.fixture.selected))
        self.assertEqual([json.loads(line)['type'] for line in text.splitlines()], ['selection', 'target'])
        candidate = self.root/'candidate.bin'
        candidate.write_bytes(fixtures.submission(self.fixture.selected))
        with patch.object(self.app, 'environment', wraps=self.app.environment) as environment:
            code, text, error = self.cli('start', self.root/'run', '--package', self.root/'package',
                '--selection', selection, '--candidates', candidate, '--quiet', '--json')
            environment.assert_called_once()
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)['outcome'], 'export_verified')
        for command in ('status', 'resume'):
            code, text, error = self.cli(command, self.root/'run', '--json')
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(text)['exit_code'], 0)
        code, text, error = self.cli('export', self.root/'run', self.root/'bundle', '--quiet')
        self.assertEqual(code, 0, error)
        code, text, error = self.cli('verify', self.root/'bundle', '--package', self.root/'package', '--json')
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(text)['verified'])

    def test_source_start_reuses_environment_checker_and_cancellation(self):
        candidate = self.root/'candidate.bin'
        candidate.write_bytes(fixtures.submission(self.fixture.selected))
        inspected = []
        def select(checker, request, *, cancellation):
            self.assertEqual(request.source, 'Main.lean')
            self.assertEqual(request.names, ('answer',))
            self.assertIsInstance(cancellation, threading.Event)
            inspected.append(checker)
            return self.fixture.selected
        with patch.object(self.app, 'environment', wraps=self.app.environment) as environment, \
             patch.object(self.app, 'checker_factory', wraps=self.app.checker_factory) as checker, \
             patch('midas.application.preparation.select', side_effect=select) as selection, \
             patch.object(self.app, '_build', wraps=self.app._build) as build:
            code, _, error = self.cli('start', self.root/'run', '--package', self.root/'package',
                '--source', 'Main.lean', '--target', 'answer', '--candidates', candidate, '--quiet')
            self.assertEqual(code, 0, error)
            environment.assert_called_once()
            checker.assert_called_once()
            selection.assert_called_once()
            self.assertIs(build.call_args.args[2], inspected[0])

    def test_submit_fork_and_explicit_partial_export(self):
        with self.fixture.start():
            pass
        candidate = self.root/'candidate'
        candidate.write_bytes(fixtures.submission(self.fixture.selected))
        code, _, error = self.cli('submit', self.root/'run', '--candidates', candidate, '--quiet')
        self.assertEqual(code, 0, error)
        code, text, error = self.cli('fork', self.root/'run', self.root/'child', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)['type'], 'fork')
        with self.fixture.start('partial'):
            pass
        code, _, error = self.cli('export', self.root/'partial', self.root/'partial-bundle', '--allow-sorry', '--quiet')
        self.assertEqual(code, 0, error)
        code, text, _ = self.cli('status', self.root/'partial', '--json')
        self.assertEqual(json.loads(text)['proof'], 'incomplete')

    def test_script_manifest_keeps_large_replies_out_of_json(self):
        response = self.root/'reply'
        response.write_bytes(b' '+b'x'*1024**2)
        manifest = self.root/'script.json'
        manifest.write_text('[{"role":"prover","file":"reply"}]')
        original = json.loads
        def bounded(value, **kwargs):
            self.assertLessEqual(len(value), 16384)
            return original(value, **kwargs)
        with patch('json.loads', side_effect=bounded):
            self.assertEqual(files.script(manifest), (('prover', response.read_bytes()),))
            manifest.write_text('[{"role":"research","file":"reply"},{"role":"web_search","file":"reply"}]')
            self.assertEqual(tuple(role for role, _ in files.script(manifest)), ('research', 'web_search'))
        oversized = self.root/'oversized'
        oversized.write_bytes(b' '*16385)
        with patch('json.loads', side_effect=AssertionError('large JSON')), self.assertRaises(ValueError):
            files.document(oversized)
        for bad in (b'{"x":1,"x":2}', b'['*17+b']'*17, b'{"x":NaN}'):
            manifest.write_bytes(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                files.document(manifest)

    def test_helper_instruction_roles_are_distinct(self):
        from midas.cli.service import instructions
        research, web = self.root/'research', self.root/'web'
        research.write_text('Answer questions.')
        web.write_text('Find references.')
        self.assertEqual(instructions((f'researcher={research}', f'web-searcher={web}')),
                         (('researcher', b'Answer questions.'), ('web-searcher', b'Find references.')))

    def test_review_instruction_roles_are_distinct(self):
        from midas.cli.service import instructions
        review, stop = self.root/'review', self.root/'stop'
        review.write_text('Review the decomposition.')
        stop.write_text('Review the stop request.')
        values = (f'reviewer={review}', f'stop-reviewer={stop}')
        self.assertEqual(instructions(values),
                         (('reviewer', b'Review the decomposition.'), ('stop-reviewer', b'Review the stop request.')))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            instructions((*values, values[1]))

    def test_invalid_candidates_and_conflicting_modes_do_not_create_runs(self):
        candidate = self.root/'invalid'
        candidate.write_bytes(b'not a submission')
        code, _, _ = self.cli('start', self.root/'run', '--package', self.root/'package',
                              '--source', 'Main.lean', '--all-unfinished', '--candidates', candidate)
        self.assertEqual(code, 2)
        self.assertFalse((self.root/'run').exists())
        code, _, _ = self.cli('start', self.root/'run', '--package', self.root/'package',
                              '--source', 'Main.lean', '--workflow', 'supplied')
        self.assertEqual(code, 2)
        self.assertFalse((self.root/'run').exists())

    def test_artifact_stream_publishes_after_verification_and_never_replaces(self):
        data = b'proof\n'*40000
        with self.fixture.start() as run:
            ref = run.writer.put(data, max_bytes=len(data))
            code, text, error = self.cli('artifact', self.root/'run', ref.id, '--output', self.root/'bytes', '--json')
            self.assertEqual(code, 0, error)
            self.assertEqual((self.root/'bytes').read_bytes(), data)
            self.assertNotIn('proof', text)
            code, _, _ = self.cli('artifact', self.root/'run', ref.id, '--output', self.root/'bytes')
            self.assertEqual(code, 2)
            code, _, _ = self.cli('artifact', self.root/'run', ref.id, '--output', self.root/'small', '--max-bytes', '1')
            self.assertEqual(code, 2)
            self.assertFalse((self.root/'small').exists())
            code, _, _ = self.cli('artifact', self.root/'run', ref.id, '--output', self.root/'run'/'bad')
            self.assertEqual(code, 2)

    def test_stop_is_session_fenced_and_restores_signal_handlers(self):
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with self.fixture.start() as run:
            with Signals() as signals, listen(run, signals):
                with socket.socket(socket.AF_UNIX) as peer:
                    peer.connect(endpoint(run.writer.directory, run.writer.session))
                    peer.sendall(message(replace(run.writer.session, epoch=run.writer.session.epoch+1)))
                    self.assertEqual(peer.recv(128), b'REJECTED\n')
                self.assertFalse(run.controller._stop.is_set())
                self.assertTrue(stop(run.writer.directory))
                self.assertTrue(run.controller._stop.is_set())
                self.assertEqual(run.run().exit_code, 130)
        self.assertFalse(stop(self.root/'run'))
        self.assertEqual(handlers, {sig: signal.getsignal(sig) for sig in handlers})

    def test_progress_sink_never_blocks_controller(self):
        entered, release = threading.Event(), threading.Event()
        class Blocked:
            def write(self, text):
                entered.set()
                release.wait(5)
            def flush(self):
                pass
        try:
            with self.fixture.start() as run:
                with Display(Blocked()) as display:
                    view = self.app.inspect(self.root/'run')
                    display.publish(view)
                    self.assertTrue(entered.wait(1))
                    start = time.monotonic()
                    for _ in range(1000):
                        display.publish(view)
                    run.run(observe=display.publish)
                    self.assertLess(time.monotonic()-start, 1)
        finally:
            release.set()

    def test_entrypoint_help_does_not_import_application(self):
        code = "import sys; from midas.cli import main\ntry: main(['--help'])\nexcept SystemExit as e: assert e.code == 0\nassert 'midas.application' not in sys.modules"
        subprocess.run([sys.executable, '-B', '-c', code], check=True, stdout=subprocess.DEVNULL,
                       cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    unittest.main()
