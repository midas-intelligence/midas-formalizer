"""Executor contracts. Raw Python children test supervision; no Lean/bwrap here."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from midas.domain import (ArtifactRef, CapacityGrant, DeliveryCertainty, FrozenRequest,
    Invocation, Job, JobKind, OwnerVersion, ResourceLimits, ResultStatus, Session,
    Termination, Usage, WorkerProgress, WorkerResult)
from midas.executor import (Executor, ImportInput, ImportRecipe, LaunchSpec, Mount,
    Outcome, WorkerStopped, import_runtime_source, import_worker_platform_supported)
from midas.executor.imports import _ImportWorker
from midas.executor.isolation import _launch
from midas.executor.process import (_Capture, _Discovery, _ProcessTree, _metadata,
                                    _run_process, _status_messages, failure)


def reference(data=b'x'):
    return ArtifactRef(str(uuid4()), sha256(data).hexdigest(), len(data))


def invocation(*, kind=JobKind.CHECK_PROOF, limits=None):
    owner = OwnerVersion('obl_' + '1' * 64, 0)
    job = Job('test', kind, owner, (), (owner,), limits or ResourceLimits(2000, 128, 4096, 4096))
    request = FrozenRequest(job, 'env_' + '2' * 64, reference(), reference())
    return Invocation(str(uuid4()), Session(str(uuid4()), str(uuid4()), 1), request, reference())


def success(request, context):
    return WorkerResult(context.invocation_id, request.request_hash, ResultStatus.SUCCEEDED,
                        (), None, None, Usage(0, 0, 0, 0), DeliveryCertainty.DELIVERED, Termination.TERMINATED)


def recipe(source=b'first', *, launch=None):
    launch = launch or LaunchSpec(('/native',), (), '/usr/bin/false', '0' * 64)
    return ImportRecipe(launch, 'env_' + '2' * 64, '3' * 64, 'Request.bin',
                        (ImportInput('Request.bin', sha256(source).hexdigest(), len(source)),))


class ProcessTests(unittest.TestCase):
    def run_child(self, text, **kwargs):
        limits = kwargs.pop('limits', ResourceLimits(2000, 128, 4096, 4096))
        return _run_process((sys.executable, '-c', text), limits, **kwargs)

    def test_success_errors_and_signals(self):
        result = self.run_child('import os; os.write(1,b"out"); os.write(2,b"err")')
        self.assertEqual((result.outcome, result.stdout.data, result.stderr.data), (Outcome.COMPLETED, b'out', b'err'))
        self.assertEqual(result.termination, Termination.TERMINATED)
        self.assertEqual(self.run_child('raise SystemExit(1)').outcome, Outcome.COMPILER_ERROR)
        killed = self.run_child('import os,signal; os.kill(os.getpid(),signal.SIGTERM)')
        self.assertEqual((killed.outcome, killed.signal), (Outcome.CRASH, signal.SIGTERM))
        self.assertIsNone(self.run_child('raise SystemExit(143)').signal)

    def test_output_combined_and_large_bytes_never_json(self):
        with patch('json.dumps', side_effect=AssertionError('no payload JSON')), patch('json.loads', side_effect=AssertionError('no payload JSON')):
            result = self.run_child('import os; os.write(1,b"a"*2048); os.write(2,b"b"*8192)')
        self.assertEqual((result.outcome, result.limit), (Outcome.RESOURCE_LIMIT, 'output'))
        self.assertEqual(len(result.stdout.data) + len(result.stderr.data), 4096)
        self.assertTrue(result.stdout.truncated or result.stderr.truncated)

    def test_wall_and_memory(self):
        result = self.run_child('import time; time.sleep(10)', limits=ResourceLimits(60, 128, 4096, 4096))
        self.assertEqual((result.outcome, result.limit), (Outcome.RESOURCE_LIMIT, 'wall'))
        result = self.run_child('import time; data=bytearray(32*1024*1024); time.sleep(10)', limits=ResourceLimits(2000, 16, 4096, 4096))
        self.assertEqual((result.outcome, result.limit), (Outcome.RESOURCE_LIMIT, 'memory'))
        self.assertGreater(result.peak_anonymous_bytes, 16 * 1024 * 1024)

    def test_cancellation_before_and_during_launch(self):
        cancel = threading.Event()
        cancel.set()
        with patch('subprocess.Popen', side_effect=AssertionError('launched')):
            result = self.run_child('pass', cancel=cancel)
        self.assertEqual(result.outcome, Outcome.CANCELLED)
        cancel.clear()
        timer = threading.Timer(0.06, cancel.set)
        timer.start()
        try:
            result = self.run_child('import time; time.sleep(10)', cancel=cancel)
        finally:
            timer.join()
        self.assertEqual((result.outcome, result.termination), (Outcome.CANCELLED, Termination.TERMINATED))

    def test_descendant_in_another_session_is_dead_before_result(self):
        child = 'import os,time; os.setsid(); print(os.getpid(),flush=True); time.sleep(10)'
        result = self.run_child('import subprocess,time; subprocess.Popen([' + repr(sys.executable) + ',"-c",' + repr(child) + ']); time.sleep(.15)')
        pid = int(result.stdout.data)
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            pass
        else:
            import select
            try:
                self.assertTrue(select.select([fd], [], [], 0)[0])
            finally:
                os.close(fd)
        self.assertEqual(result.termination, Termination.TERMINATED)
        self.assertLess(result.elapsed_ms, 1000)

    def test_fifo_evidence_cap_and_descriptor_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / 'evidence'
            os.mkfifo(fifo)
            fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            result = self.run_child('import os; fd=os.open(' + repr(str(fifo)) + ',os.O_WRONLY); os.write(fd,b"e"*8192)', evidence_fd=fd)
            self.assertEqual((result.outcome, result.limit), (Outcome.RESOURCE_LIMIT, 'evidence'))
            self.assertEqual(len(result.evidence.data), 4096)
            self.assertTrue(result.evidence.truncated)
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_launch_failure_closes_transferred_descriptors(self):
        read_fd, write_fd = os.pipe()
        result = _run_process(('/missing-executor-program',), ResourceLimits(1000, 128, 4096, 4096), status_pipe=(read_fd, write_fd))
        self.assertEqual(result.outcome, Outcome.SETUP_FAILURE)
        for fd in (read_fd, write_fd):
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_monitor_cleanup_failure_still_kills_leader_and_reports_unknown(self):
        with patch.object(_ProcessTree, 'terminate', side_effect=OSError('failed observation')):
            result = self.run_child('import time; time.sleep(10)', limits=ResourceLimits(60, 128, 4096, 4096))
        self.assertEqual(result.termination, Termination.UNKNOWN)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertLess(result.elapsed_ms, 1000)

    def test_missing_namespace_status_is_not_success(self):
        read_fd, write_fd = os.pipe()
        result = self.run_child('pass', status_pipe=(read_fd, write_fd))
        self.assertEqual(result.outcome, Outcome.SETUP_FAILURE)

    def test_control_metadata_bounded_before_json(self):
        self.assertEqual(_status_messages(b'{"child-pid":4}\n{"exit-code":0}\n'), [{'child-pid': 4}, {'exit-code': 0}])
        for value in (b' ' * 16385, b'[' * 9 + b']' * 9):
            with patch('json.JSONDecoder.raw_decode', side_effect=AssertionError('decoded before bounds')):
                with self.assertRaises(ValueError):
                    _status_messages(value)
        self.assertEqual(_metadata(b'{"text":"[\\\"["}'), '{"text":"[\\\"["}')

    def test_capture_tracks_observed_without_retaining_excess(self):
        capture = _Capture(5)
        self.assertTrue(capture.append(0, b'123'))
        self.assertFalse(capture.append(1, b'4567'))
        self.assertFalse(capture.append(0, b'89'))
        self.assertEqual(capture.result(0).observed_bytes, 5)
        self.assertEqual(capture.result(1).data, b'45')

    def test_discovery_reuses_snapshot_and_failed_scan_invalidates(self):
        clock = [1.0]
        discovery = _Discovery(lambda: clock[0])
        a = discovery.snapshot()
        self.assertIs(discovery.snapshot(), a)
        with patch('pathlib.Path.iterdir', side_effect=OSError('scan failure')):
            with self.assertRaises(OSError):
                discovery.snapshot(fresh=True)
        self.assertIsNone(discovery.value)


class IsolationTests(unittest.TestCase):
    def test_fixed_isolation_and_explicit_roots(self):
        spec = LaunchSpec(('/tools/program', 'arg'), (Mount('/usr', '/tools'),), sys.executable,
                          sha256(Path(sys.executable).read_bytes()).hexdigest(), (('LANG', 'C'),))
        command = spec.command(status_fd=7, scratch_bytes=2048)
        self.assertIn('--unshare-all', command)
        self.assertIn('--clearenv', command)
        self.assertNotIn('--share-net', command)
        self.assertIn('--ro-bind', command)
        self.assertEqual(command[-3:], ('--', '/tools/program', 'arg'))

    def test_paths_metadata_and_profiles(self):
        for target in ('/', '/proc/self', '/tmp', '/dev/null', '/x/../tmp', '//proc'):
            with self.assertRaises(ValueError):
                Mount('/usr', target)
        with self.assertRaises(ValueError):
            LaunchSpec(('/x', 'x' * 16385), (), '/usr/bin/false', '0' * 64)
        with self.assertRaises(ValueError):
            LaunchSpec(('/x',), (Mount('/usr', '/a'), Mount('/usr', '/a/b')), '/usr/bin/false', '0' * 64)
        with self.assertRaises(ValueError):
            LaunchSpec(('/x',), (), '/usr/bin/false', '0' * 64, profile='host')

    def test_bad_launcher_never_falls_back(self):
        spec = LaunchSpec(('/x',), (), '/usr/bin/false', '0' * 64)
        with patch('subprocess.Popen', side_effect=AssertionError('host fallback')):
            result = _launch(spec, ResourceLimits(1000, 128, 4096, 4096))
        self.assertEqual(result.outcome, Outcome.SETUP_FAILURE)

    def test_preparation_charges_wall_time_and_cleans_only_its_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / 'evidence'
            spec = LaunchSpec(('/x',), (), '/usr/bin/false', '0' * 64)
            def command(*args, **kwargs):
                time.sleep(.02)
                return ('/usr/bin/true',)
            with patch.object(LaunchSpec, 'command', command):
                result = _launch(spec, ResourceLimits(10, 128, 4096, 4096), evidence_path=fifo)
            self.assertEqual((result.outcome, result.limit), (Outcome.RESOURCE_LIMIT, 'wall'))
            self.assertFalse(fifo.exists())
            fifo.write_bytes(b'existing')
            result = _launch(spec, ResourceLimits(10, 128, 4096, 4096), evidence_path=fifo)
            self.assertEqual(result.outcome, Outcome.SETUP_FAILURE)
            self.assertEqual(fifo.read_bytes(), b'existing')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.executor = Executor(self.directory, workers=2, compiler_slots=1, compiler_memory_mib=128, progress_capacity=1)
        self.addCleanup(self.executor.drain)

    def start(self, execute=success, *, inv=None, grant=None):
        inv = inv or invocation()
        workspace = self.directory / inv.original_execution_id
        workspace.mkdir()
        self.executor.start(inv, execute, grant=grant or CapacityGrant(inv), workspace=workspace)
        return inv

    def complete(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if result := self.executor.poll():
                return result
            time.sleep(.005)
        self.fail('No completion')

    def test_worker_receives_frozen_request_and_restricted_context(self):
        seen = []
        def work(request, context):
            seen.append((request, context))
            self.assertFalse(hasattr(context, 'start'))
            self.assertFalse(hasattr(context, 'db'))
            return success(request, context)
        inv = self.start(work)
        done, = self.complete()
        self.assertIs(seen[0][0], inv.request)
        self.assertIs(seen[0][1].limits, inv.request.job.limits)
        self.assertEqual(done.result.termination, Termination.TERMINATED)
        with self.assertRaises(WorkerStopped):
            seen[0][1].check()

    def test_explicit_transport_uncertainty_survives_callable_return(self):
        inv = self.start(lambda request, context: replace(success(request, context), termination=Termination.UNKNOWN),
                         inv=invocation(kind=JobKind.MODEL))
        done, = self.complete()
        self.assertEqual(done.result.termination, Termination.UNKNOWN)
        self.assertIn(inv.original_execution_id, self.executor.state().unconfirmed)
        with self.assertRaises(ValueError):
            self.executor.reconcile_termination(inv.original_execution_id, Termination.UNKNOWN)
        self.assertTrue(self.executor.reconcile_termination(inv.original_execution_id, Termination.TERMINATED))
        self.assertFalse(self.executor.state().unconfirmed)

    def test_capacity_grant_matches_full_invocation(self):
        inv = invocation()
        for changed in (replace(inv, reservation_ref=reference()), replace(inv, session=Session(str(uuid4()), str(uuid4()), 2))):
            with self.assertRaises(ValueError):
                CapacityGrant(inv).validate_for(changed)

    def test_running_cancel_and_drain_do_not_claim_early_termination(self):
        entered, release = threading.Event(), threading.Event()
        def work(request, context):
            entered.set()
            release.wait(3)
            return success(request, context)
        inv = self.start(work)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.executor.cancel(inv.original_execution_id))
        self.assertFalse(self.executor.drain(timeout=.01))
        self.assertEqual(self.executor.state().compiler_slots, 1)
        self.assertEqual(self.executor.poll(), ())
        release.set()
        done, = self.complete()
        self.assertEqual((done.result.status, done.result.termination), (ResultStatus.CANCELLED, Termination.TERMINATED))
        self.assertTrue(self.executor.drain(timeout=1))

    def test_compiler_capacity_rejects_without_a_second_queue(self):
        entered, release = threading.Event(), threading.Event()
        def work(request, context):
            entered.set()
            release.wait(2)
            return success(request, context)
        self.start(work)
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaisesRegex(ValueError, 'Compiler capacity'):
                self.start()
            # Noncompiler work can still use the remaining thread.
            self.start(inv=invocation(kind=JobKind.MODEL))
            self.complete()
        finally:
            release.set()
        self.complete()

    def test_progress_backpressure_is_cancelable_and_bounded(self):
        waiting = threading.Event()
        def work(request, context):
            context.emit('case_started')
            waiting.set()
            context.emit('case_finished')
            return success(request, context)
        inv = self.start(work)
        self.assertTrue(waiting.wait(1))
        self.assertEqual(self.executor._progress.qsize(), 1)
        self.executor.cancel(inv.original_execution_id)
        done, = self.complete()
        self.assertEqual(done.result.status, ResultStatus.CANCELLED)
        message, = self.executor.poll_progress()
        self.assertEqual((message.invocation_id, message.sequence, message.kind), (inv.original_execution_id, 0, 'case_started'))
        with self.assertRaises(ValueError):
            WorkerProgress(inv.original_execution_id, 0, 'x' * 129)

    def test_exceptions_and_mismatched_results_are_normalized(self):
        def broken(request, context):
            raise OSError('failed execution')
        self.start(broken)
        result, = self.complete()
        self.assertEqual(result.result.status, ResultStatus.FAILED)
        self.assertEqual(result.result.delivery_certainty, DeliveryCertainty.UNKNOWN)
        self.assertIn('failed execution', result.detail)
        self.start(lambda request, context: replace(success(request, context), invocation_id=str(uuid4())))
        result, = self.complete()
        self.assertEqual(result.result.status, ResultStatus.FAILED)
        self.assertIn('differs', result.detail)

    def test_cancellation_keeps_returned_artifacts_and_known_usage(self):
        output = reference(b'partial output')
        def work(request, context):
            context.cancellation.set()
            return replace(success(request, context), staged_artifacts=(output,), output=output, usage=Usage(10, 0, 5, 20))
        self.start(work)
        done, = self.complete()
        self.assertEqual(done.result.status, ResultStatus.CANCELLED)
        self.assertEqual(done.result.output, output)
        self.assertEqual(done.result.usage, Usage(10, 0, 5, 20))

    def test_sequential_import_cases_share_one_retained_parent(self):
        from midas.executor.process import Diagnostic
        created, observed = [], []
        class FakeWorker:
            def __init__(self, recipe, directory, limits):
                self.id, self.key, self.memory_mib = str(uuid4()), recipe.key(limits), limits.memory_mib
                self.closed, self.termination = False, Termination.TERMINATED
                self.process = self
                created.append(self)
            def poll(self):
                return None
            def close(self):
                self.closed = True
            def run(self, recipe, workspace, limits, cancel):
                observed.append(limits.evidence_bytes)
                return replace(failure(Outcome.COMPLETED, ''), returncode=0, evidence=Diagnostic(b'e' * 100, 100, False))
        def work(request, context):
            context.run_import(recipe())
            context.run_import(recipe(b'changed source'))
            return success(request, context)
        with patch('midas.executor.workers._ImportWorker', FakeWorker):
            self.start(work)
            done, = self.complete()
        self.assertEqual(done.result.status, ResultStatus.SUCCEEDED, done.detail)
        self.assertEqual(len(created), 1)
        self.assertEqual(observed, [4096, 3996])

    def test_recursive_submission_and_thread_escape_rejected(self):
        # Use a matching grant so the recursive-submission guard is reached.
        original = self.executor.start
        def work(request, context):
            child = invocation()
            with self.assertRaisesRegex(ValueError, 'recursively'):
                original(child, success, grant=CapacityGrant(child), workspace=context.workspace)
            seen = []
            def other():
                try:
                    context.check()
                except WorkerStopped:
                    seen.append(True)
            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
            self.assertEqual(seen, [True])
            return success(request, context)
        self.start(work)
        done, = self.complete()
        self.assertEqual(done.result.status, ResultStatus.SUCCEEDED, done.detail)

    def test_process_calls_share_cumulative_limits(self):
        observed = []
        from midas.executor.process import Diagnostic
        spec = recipe().launch
        def launch(spec, limits, **kwargs):
            observed.append(limits)
            return replace(failure(Outcome.COMPLETED, ''), returncode=0,
                           stdout=Diagnostic(b'x' * 3000, 3000, False))
        def work(request, context):
            context.run_process(spec)
            context.run_process(spec)
            stopped = context.run_process(spec)
            self.assertEqual((stopped.outcome, stopped.limit), (Outcome.RESOURCE_LIMIT, 'output'))
            return success(request, context)
        with patch('midas.executor.workers._launch', side_effect=launch):
            self.start(work, inv=invocation(limits=ResourceLimits(2000, 128, 4096, 4096, 12345)))
            done, = self.complete()
        self.assertEqual(done.result.status, ResultStatus.SUCCEEDED, done.detail)
        self.assertEqual([x.output_bytes for x in observed], [4096, 1096])
        self.assertEqual([x.max_heartbeat for x in observed], [12345, 12345])
        self.assertLessEqual(observed[1].wall_ms, observed[0].wall_ms)

    def test_unconsumed_completions_bound_dispatch(self):
        self.start(inv=invocation(kind=JobKind.MODEL))
        self.start(inv=invocation(kind=JobKind.MODEL))
        with self.assertRaisesRegex(ValueError, 'poll completions'):
            self.start(inv=invocation(kind=JobKind.MODEL))

    def test_dispatch_failure_is_a_never_sent_completion(self):
        with patch.object(self.executor._pool, 'submit', side_effect=RuntimeError('no thread')):
            self.start()
        done, = self.complete()
        self.assertEqual((done.result.status, done.result.delivery_certainty, done.result.termination),
                         (ResultStatus.FAILED, DeliveryCertainty.NOT_SENT, Termination.TERMINATED))
        self.assertIn('no thread', done.detail)

    def test_unknown_termination_keeps_capacity_after_poll(self):
        result = replace(failure(Outcome.SETUP_FAILURE, 'unknown tree'), termination=Termination.UNKNOWN)
        def work(request, context):
            context.run_process(recipe().launch)
            return success(request, context)
        with patch('midas.executor.workers._launch', return_value=result):
            inv = self.start(work)
            done, = self.complete()
        self.assertEqual(done.result.termination, Termination.UNKNOWN)
        state = self.executor.state()
        self.assertEqual((state.compiler_slots, state.compiler_memory_mib, state.unconfirmed),
                         (1, 128, (inv.original_execution_id,)))
        with self.assertRaises(ValueError):
            self.start()
        self.assertFalse(self.executor.drain(timeout=0))

    def test_retained_transfer_eviction_and_drain_use_one_capacity_account(self):
        created = []
        class FakeWorker:
            def __init__(self, recipe, directory, limits):
                self.id, self.key, self.memory_mib = str(uuid4()), recipe.key(limits), limits.memory_mib
                self.closed = False
                self.termination = Termination.TERMINATED
                self.process = self
                created.append(self)
            def poll(self):
                return 0 if self.closed else None
            def close(self):
                self.closed = True
            def run(self, recipe, workspace, limits, cancel):
                return replace(failure(Outcome.COMPLETED, ''), returncode=0)
        def work(request, context):
            run = context.run_import(recipe())
            self.assertEqual(run.result.outcome, Outcome.COMPLETED)
            return success(request, context)
        with patch('midas.executor.workers._ImportWorker', FakeWorker):
            self.start(work)
            first, = self.complete()
            self.assertEqual(first.result.status, ResultStatus.SUCCEEDED, first.detail)
            retained, = self.executor.state().retained
            self.assertEqual(retained.memory_mib, 128)
            inv = invocation()
            self.start(work, inv=inv, grant=CapacityGrant(inv, retained.id))
            second, = self.complete()
            self.assertEqual(first.retained_worker_id, second.retained_worker_id)
            self.assertEqual(len(created), 1)
            self.start(work)  # Explicit fresh grant evicts the idle footprint.
            self.complete()
            self.assertTrue(created[0].closed)
            self.assertEqual(len(created), 2)
        self.assertTrue(self.executor.drain())
        self.assertTrue(created[1].closed)
        self.assertEqual(self.executor.state().retained, ())

    def test_fallback_keeps_remaining_bytes_and_stops_on_limits(self):
        from midas.executor.process import Diagnostic
        observations = []
        class Unsupported:
            def __init__(self, recipe, directory, limits):
                self.key = recipe.key(limits)
                self.termination = Termination.TERMINATED
            def close(self):
                pass
            def run(self, recipe, workspace, limits, cancel):
                time.sleep(.01)
                return replace(failure(Outcome.SETUP_FAILURE, 'unsupported'),
                               stdout=Diagnostic(b'x' * 1000, 1000, False),
                               evidence=Diagnostic(b'y' * 100, 100, False))
        def launch(spec, limits, **kwargs):
            observations.append(limits)
            return failure(Outcome.COMPLETED, '')
        def work(request, context):
            run = context.run_import(recipe(), fresh=recipe().launch)
            self.assertEqual(len(run.attempts), 2)
            self.assertIsNone(run.runtime_sha256)
            return success(request, context)
        with patch('midas.executor.workers._ImportWorker', Unsupported), patch('midas.executor.workers._launch', side_effect=launch):
            self.start(work)
            done, = self.complete()
        self.assertEqual(done.result.status, ResultStatus.SUCCEEDED, done.detail)
        self.assertEqual((observations[0].output_bytes, observations[0].evidence_bytes), (3096, 3996))
        self.assertLess(observations[0].wall_ms, 2000)

        def limited(*args):
            return failure(Outcome.RESOURCE_LIMIT, '', limit='memory')
        with patch.object(Unsupported, 'run', limited), patch('midas.executor.workers._ImportWorker', Unsupported), patch('midas.executor.workers._launch', side_effect=AssertionError('fallback on resource limit')):
            def work(request, context):
                run = context.run_import(recipe(), fresh=recipe().launch)
                self.assertEqual(len(run.attempts), 1)
                return success(request, context)
            self.start(work)
            done, = self.complete()
            self.assertEqual(done.result.status, ResultStatus.SUCCEEDED, done.detail)


class ImportTests(unittest.TestCase):
    def test_import_worker_platform_requirements(self):
        for system, machine in (('Darwin', 'x86_64'), ('Linux', 'aarch64')):
            with self.subTest(system=system, machine=machine), \
                    patch('midas.executor.imports.platform.system', return_value=system), \
                    patch('midas.executor.imports.platform.machine', return_value=machine), \
                    patch('midas.executor.imports.ctypes.CDLL') as library:
                self.assertFalse(import_worker_platform_supported())
                library.assert_not_called()

    def test_import_worker_landlock_capability_without_toolchain_input(self):
        with patch('midas.executor.imports.platform.system', return_value='Linux'), \
                patch('midas.executor.imports.platform.machine', return_value='x86_64'), \
                patch('midas.executor.imports.ctypes.CDLL') as library:
            for abi, expected in ((-1, False), (5, False), (6, True), (7, True)):
                with self.subTest(abi=abi):
                    library.return_value.syscall.return_value = abi
                    self.assertEqual(import_worker_platform_supported(), expected)
                    library.return_value.syscall.assert_called_with(444, 0, 0, 1)

    def test_recipe_bindings_and_flat_runtime_resource(self):
        first, second = recipe(b'one'), recipe(b'two')
        limits = ResourceLimits(1000, 128, 4096, 4096)
        self.assertEqual(first.key(limits), second.key(limits))
        dependency = ImportInput('Dependency.olean', 'a' * 64, 10)
        self.assertNotEqual(first.key(limits), replace(first, inputs=(*first.inputs, dependency)).key(limits))
        for field in ('environment_id', 'runtime_sha256'):
            value = 'env_' + '4' * 64 if field == 'environment_id' else '4' * 64
            self.assertNotEqual(first.key(limits), replace(first, **{field: value}).key(limits))
        self.assertIn(b'PTRACE_O_TRACEFORK', import_runtime_source())

    def test_transport_reuse_and_hash_validation_without_payload_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'job'
            source.mkdir()
            data = b'x' * (1024 * 1024)
            (source / 'Request.bin').write_bytes(data)
            # A fake bootstrap exercises real pipe/FIFO transport, not isolation.
            def command(spec, *, status_fd, scratch_bytes):
                workspace = next(m.source for m in spec.mounts if m.target == '/midas-inputs')
                program = ('import os,sys,json\n'
                    f'os.write({status_fd},b\'{"{\"child-pid\":1}\\n"}\')\n'
                    f'control=os.open({workspace!r}+"/.midas-control",os.O_WRONLY)\n'
                    f'evidence=os.open({workspace!r}+"/.midas-evidence",os.O_WRONLY)\n'
                    'for line in sys.stdin:\n'
                    ' os.write(evidence,b"evidence")\n'
                    ' os.write(1,b"output")\n'
                    ' os.write(control,b"MIDAS_WORKER_END "+json.dumps({"path":line.strip(),"exit":0}).encode()+b"\\n")\n')
                return (sys.executable, '-u', '-c', program)
            limits = ResourceLimits(2000, 128, 4096, 4096)
            with patch.object(LaunchSpec, 'command', command):
                worker = _ImportWorker(recipe(data), root, limits)
                try:
                    for _ in range(2):
                        result = worker.run(recipe(data), source, limits, threading.Event())
                        self.assertEqual(result.outcome, Outcome.COMPLETED, result.detail)
                        self.assertEqual(result.evidence.data, b'evidence')
                    (source / 'Request.bin').write_bytes(b'z' * len(data))
                    result = worker.run(recipe(data), source, limits, threading.Event())
                    self.assertEqual(result.outcome, Outcome.SETUP_FAILURE)
                    self.assertTrue(worker.closed)
                finally:
                    worker.close()


if __name__ == '__main__':
    unittest.main()
