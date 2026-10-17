"""Opt-in real namespace/Lean/native-runtime checks; AGENTS.md requires permission.

Usage: python3 -B -m tests.check_executor_lean /path/to/toolchain/bin/lean
Builds a small independent harness around the production C isolation runtime.
It tests execution/termination, not the future checker's proof admission.
"""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import time

from midas.domain import CapacityGrant, ResourceLimits, ResultStatus, Termination
from midas.executor import (Executor, ImportInput, ImportRecipe, LaunchSpec, LINK_FLAGS,
                            Mount, Outcome, import_runtime_source, import_worker_platform_supported)
from tests.test_executor import invocation, success


HARNESS = r'''import Lean
open Lean

@[extern "midas_isolate"] opaque isolate : IO UInt32
@[extern "midas_child_exit"] opaque exitChild (code : UInt32) : IO Unit
@[extern "midas_control_open"] opaque openControl : IO Unit
@[extern "midas_control_write"] opaque writeControl (line : @& String) : IO Unit
@[extern "test_cpu_work"] opaque cpuWork : IO Unit

def main : IO UInt32 := do
  openControl
  let input ← IO.getStdin
  repeat
    let line ← input.getLine
    if line.isEmpty then break
    let path := line.trimAscii.toString
    let result ← isolate
    if result == 0 then
      let request ← IO.FS.readFile path
      if request == "sleep" then IO.sleep 10000
      if request == "cpu" then cpuWork
      if request == "forge" then
        let denied ← try
          IO.FS.writeFile "/midas-inputs/.midas-control" "forged\n"
          pure false
        catch _ => pure true
        unless denied do exitChild 1
      IO.FS.writeFile "/midas-inputs/.midas-evidence" request
      IO.println ("handled " ++ request)
      exitChild (if request == "reject" then 1 else 0)
    let code := result - 1
    writeControl ("MIDAS_WORKER_END " ++ (Json.mkObj [
      ("path", toJson path), ("exit", toJson code.toNat)]).compress ++ "\n")
    if code > 1 then return 2
  return 0
'''


def main():
    compiler = Path(sys.argv[1]).resolve(strict=True)
    toolchain = compiler.parent.parent
    if not import_worker_platform_supported() or '4.35.0-rc3' not in (toolchain / 'include/lean/version.h').read_text():
        raise SystemExit('Native smoke check requires Lean 4.35.0-rc3, Linux x86-64 and Landlock ABI 6')
    launcher = str(Path(shutil.which('bwrap')).resolve())
    launcher_hash = sha256(Path(launcher).read_bytes()).hexdigest()
    with TemporaryDirectory(prefix='midas-executor-lean-') as directory:
        root = Path(directory)
        build = root / 'build'
        build.mkdir()
        (build / 'Worker.lean').write_text(HARNESS)
        (build / 'import_runtime.c').write_bytes(import_runtime_source() + br'''
#include <time.h>
LEAN_EXPORT lean_obj_res test_cpu_work(lean_obj_arg world) {
    (void)world;
    struct timespec start, now;
    volatile unsigned long work = 1;
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &start)) die("CPU clock");
    do {
        for (unsigned i = 0; i < 1000000; i++) work = work * 1664525 + 1013904223;
        if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &now)) die("CPU clock");
    } while (now.tv_sec - start.tv_sec < 17);
    return lean_io_result_mk_ok(lean_box(0));
}
''')
        (build / 'Good.lean').write_text('example : True := by trivial\n')
        (build / 'Bad.lean').write_text('example : False := by trivial\n')
        # Explicit host runtime roots for this test only. Production recipes
        # receive environment-owned frozen roots and their admitted loader.
        runtime = tuple(Mount(path, path) for path in ('/usr', '/lib', '/lib64', '/bin') if Path(path).exists())
        mounts = (*runtime, Mount(str(toolchain), '/toolchain'), Mount(str(build), '/project', True))
        variables = (('PATH', '/toolchain/bin:/usr/bin:/bin'), ('HOME', '/tmp'), ('TMPDIR', '/tmp'),
                     ('LC_ALL', 'C'), ('LEAN_SYSROOT', '/toolchain'), ('LEAN_MAIN_USE_THREAD', '0'))
        limits = ResourceLimits(120000, 4096, 8 * 1024 * 1024, 64 * 1024 * 1024)
        with Executor(root, workers=2, compiler_slots=1, compiler_memory_mib=4096) as executor:
            def launch(argv, *, readonly=False):
                actual = tuple(replace(m, writable=False) if readonly else m for m in mounts)
                return LaunchSpec(tuple(argv), actual, launcher, launcher_hash, variables)

            def execute(work, *, grant_id=None, job_limits=limits, expected=ResultStatus.SUCCEEDED):
                inv = invocation(limits=job_limits)
                workspace = root / inv.original_execution_id
                workspace.mkdir()
                executor.start(inv, work, grant=CapacityGrant(inv, grant_id), workspace=workspace)
                while True:
                    done = executor.poll()
                    if done:
                        completion, = done
                        if completion.result.status != expected:
                            raise AssertionError(completion.detail)
                        if completion.result.termination != Termination.TERMINATED:
                            raise AssertionError('Unconfirmed termination')
                        return completion
                    time.sleep(.01)

            def process(argv, expected=Outcome.COMPLETED):
                def work(request, context):
                    result = context.run_process(launch(argv))
                    if result.outcome != expected:
                        raise AssertionError(str(result)[:12000])
                    return success(request, context)
                execute(work)

            process(('/toolchain/bin/lean', '/project/Good.lean'))
            process(('/toolchain/bin/lean', '/project/Bad.lean'), Outcome.COMPILER_ERROR)
            print('fresh Lean success and rejection: passed', flush=True)
            process(('/usr/bin/test', '!', '-e', '/etc/passwd'))
            process(('/usr/bin/sh', '-c', 'touch /toolchain/SHOULD_NOT_EXIST'), Outcome.COMPILER_ERROR)
            print('explicit roots and read-only toolchain: passed', flush=True)
            process(('/toolchain/bin/lean', '--c=/project/Worker.c', '/project/Worker.lean'))
            process((str(Path(shutil.which('cc')).resolve()), '-O2', '-fPIC', '-Wall', '-Wextra', '-Werror',
                     '-I/toolchain/include', '-c', '/project/import_runtime.c', '-o', '/project/isolation.o'))
            process(('/toolchain/bin/leanc', '-O2', '-rdynamic', '-o', '/project/worker',
                     '/project/Worker.c', '/project/isolation.o', *LINK_FLAGS))
            print('source-bound native harness build: passed', flush=True)
            native_hash = sha256((build / 'worker').read_bytes()).hexdigest()
            native_launch = launch(('/project/worker',), readonly=True)
            retained_id = None
            for label in ('normal', 'reject', 'cpu', 'normal-again'):
                data = label.encode()
                def work(request, context, data=data):
                    (context.workspace / 'Request.bin').write_bytes(data)
                    recipe = ImportRecipe(native_launch, request.environment_id, native_hash, 'Request.bin',
                                          (ImportInput('Request.bin', sha256(data).hexdigest(), len(data)),))
                    run = context.run_import(recipe)
                    expected = Outcome.COMPILER_ERROR if data == b'reject' else Outcome.COMPLETED
                    if run.result.outcome != expected or run.result.evidence.data != data:
                        raise AssertionError(str(run)[:12000])
                    return success(request, context)
                completion = execute(work, grant_id=retained_id)
                if retained_id is not None and retained_id != completion.retained_worker_id:
                    raise AssertionError('Worker was not reused')
                retained_id = completion.retained_worker_id
                if retained_id is None:
                    raise AssertionError('Native parent was not retained')
                print('retained native ' + label + ': passed', flush=True)

            def forge(request, context):
                data = b'forge'
                (context.workspace / 'Request.bin').write_bytes(data)
                recipe = ImportRecipe(native_launch, request.environment_id, native_hash, 'Request.bin',
                                      (ImportInput('Request.bin', sha256(data).hexdigest(), len(data)),))
                run = context.run_import(recipe, fresh=launch(('/toolchain/bin/lean', '/project/Good.lean')))
                if (len(run.attempts) != 2 or run.attempts[0].returncode != 125
                        or run.result.outcome != Outcome.COMPLETED or run.runtime_sha256 is not None):
                    raise AssertionError(str(run))
                return success(request, context)
            execute(forge, grant_id=retained_id)
            print('control FIFO protected; authorized isolated fallback: passed', flush=True)

            def timeout(request, context):
                data = b'sleep'
                (context.workspace / 'Request.bin').write_bytes(data)
                recipe = ImportRecipe(native_launch, request.environment_id, native_hash, 'Request.bin',
                                      (ImportInput('Request.bin', sha256(data).hexdigest(), len(data)),))
                run = context.run_import(recipe, fresh=launch(('/toolchain/bin/lean', '/project/Good.lean')))
                assert len(run.attempts) == 1, run
                assert (run.result.outcome, run.result.limit, run.result.termination) == (
                    Outcome.RESOURCE_LIMIT, 'wall', Termination.TERMINATED), run
                return success(request, context)
            timed = execute(timeout, job_limits=replace(limits, wall_ms=1000), expected=ResultStatus.FAILED)
            assert timed.detail.startswith('WorkerStopped:'), timed
            print('native wall limit without fallback: passed', flush=True)

            def cancel(request, context):
                data = b'sleep'
                (context.workspace / 'Request.bin').write_bytes(data)
                recipe = ImportRecipe(native_launch, request.environment_id, native_hash, 'Request.bin',
                                      (ImportInput('Request.bin', sha256(data).hexdigest(), len(data)),))
                timer = threading.Timer(.15, context.cancellation.set)
                timer.start()
                try:
                    run = context.run_import(recipe)
                    if (run.result.outcome, run.result.termination) != (Outcome.CANCELLED, Termination.TERMINATED):
                        raise AssertionError(str(run))
                finally:
                    timer.join()
                # The executor itself must report this invocation as cancelled.
                return success(request, context)
            inv = invocation(limits=limits)
            workspace = root / inv.original_execution_id
            workspace.mkdir()
            executor.start(inv, cancel, grant=CapacityGrant(inv), workspace=workspace)
            while not (done := executor.poll()):
                time.sleep(.01)
            if (done[0].result.status, done[0].result.termination) != (ResultStatus.CANCELLED, Termination.TERMINATED):
                raise AssertionError(str(done[0]))
            if not done[0].detail.startswith('WorkerStopped:'):
                raise AssertionError(done[0].detail)
            if executor.state().retained:
                raise AssertionError('Cancelled native parent retained capacity')
            print('native cancellation and confirmed tree exit: passed', flush=True)


if __name__ == '__main__':
    main()
