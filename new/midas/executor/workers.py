"""Bounded dispatch under committed grants and one compiler capacity account."""
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
import queue
import threading
import time

from ..domain import (CapacityGrant, DeliveryCertainty, Invocation, JobKind,
                      ResourceLimits, ResultStatus, Termination, Usage,
                      WorkerProgress, WorkerResult)
from ..domain.records import instance, natural
from .imports import ImportRecipe, _ImportWorker
from .isolation import LaunchSpec, _launch
from .process import Outcome, ProcessResult, failure

_LOCAL = threading.local()
_COMPILER_KINDS = frozenset(JobKind) - {JobKind.MODEL, JobKind.SEARCH}


class WorkerStopped(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetainedWorker:
    id: str
    memory_mib: int
    environment_id: str
    runtime_sha256: str
    termination: Termination = Termination.UNKNOWN


@dataclass(frozen=True, slots=True)
class ExecutorState:
    running: int
    pending_completions: int
    compiler_slots: int
    compiler_memory_mib: int
    retained: tuple[RetainedWorker, ...]
    unconfirmed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Completion:
    invocation: Invocation
    result: WorkerResult
    detail: str = ''
    retained_worker_id: str | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    attempts: tuple[ProcessResult, ...]
    runtime_sha256: str | None

    @property
    def result(self):
        return self.attempts[-1]


class WorkerContext:
    """Restricted invocation capabilities, with no scheduler or database handle.

    Python callables are trusted and cancellation is cooperative. Compiler code
    runs only through isolated processes. Callables must use their deadline for
    external I/O; an uncooperative callable remains active and holds its capacity.
    """
    def __init__(self, invocation, grant, workspace, reader, staging, progress, retained, parent):
        self.invocation_id = invocation.original_execution_id
        self.request_hash = invocation.request.request_hash
        self.capacity_grant = grant
        self.workspace = workspace
        self.reader = reader
        self.staging = staging
        self.cancellation = threading.Event()
        self.deadline = time.monotonic() + invocation.request.job.limits.wall_ms / 1000
        self._limits = invocation.request.job.limits
        self._environment_id = invocation.request.environment_id
        self._compiler = invocation.request.job.kind in _COMPILER_KINDS
        self._progress = progress
        self._sequence = 0
        self._output = self._evidence = 0
        self._termination = Termination.TERMINATED
        self._operation = threading.Lock()
        self._active = True
        self._retained = retained
        self._parent = parent
        self._thread_id = None

    @property
    def limits(self):
        """The immutable limits saved with this invocation's capacity grant."""
        return self._limits

    def check(self):
        if self._thread_id != threading.get_ident():
            raise WorkerStopped('Worker context belongs to its execution thread')
        if not self._active:
            raise WorkerStopped('Worker context is closed')
        if self.cancellation.is_set():
            raise WorkerStopped('Cancellation requested')
        if time.monotonic() >= self.deadline:
            raise WorkerStopped('Wall budget exhausted')

    def emit(self, kind, artifacts=()):
        message = WorkerProgress(self.invocation_id, self._sequence, kind, artifacts)
        while True:
            self.check()
            try:
                self._progress.put(message, timeout=min(0.02, max(0, self.deadline - time.monotonic())))
                self._sequence += 1
                return
            except queue.Full:
                pass

    def _remaining(self):
        self.check()
        if self._termination != Termination.TERMINATED:
            raise WorkerStopped('Process termination is unconfirmed')
        if self._output >= self._limits.output_bytes or self._evidence >= self._limits.evidence_bytes:
            return None
        return ResourceLimits(max(1, int((self.deadline - time.monotonic()) * 1000)),
                              self._limits.memory_mib, self._limits.output_bytes - self._output,
                              self._limits.evidence_bytes - self._evidence, self._limits.max_heartbeat)

    def _record(self, result):
        self._output += result.stdout.observed_bytes + result.stderr.observed_bytes
        self._evidence += result.evidence.observed_bytes if result.evidence is not None else 0
        if result.termination != Termination.TERMINATED:
            self._termination = Termination.UNKNOWN
        return result

    def _discard(self):
        if self._retained is not None:
            self._retained.close()
            if self._retained.termination != Termination.TERMINATED:
                self._termination = Termination.UNKNOWN
            self._retained = None

    def _begin(self):
        self.check()
        if not self._compiler:
            raise ValueError('This invocation has no compiler capacity grant')
        if not self._operation.acquire(blocking=False):
            raise ValueError('A capacity grant permits only one process operation at a time')

    def _exhausted(self):
        return failure(Outcome.RESOURCE_LIMIT, 'Invocation byte budget exhausted',
                       limit='output' if self._output >= self._limits.output_bytes else 'evidence')

    def run_process(self, spec, *, evidence_path=None):
        instance(spec, LaunchSpec)
        if evidence_path is not None:
            evidence_path = Path(evidence_path).absolute()
            if not evidence_path.parent.resolve().is_relative_to(self.workspace):
                raise ValueError('Evidence FIFO must be in the private workspace')
        self._begin()
        try:
            self._discard()  # A retained parent and a fresh process share one slot.
            limits = self._remaining()
            if limits is None:
                return self._exhausted()
            return self._record(_launch(spec, limits, cancel=self.cancellation, evidence_path=evidence_path))
        finally:
            self._operation.release()

    def run_import(self, recipe, *, fresh=None, evidence_path=None):
        """Use an admitted optimization and optional explicitly authorized fallback.

        All attempts share wall/output/evidence budgets. The final attempt's
        evidence stays separate from earlier diagnostics and failed evidence.
        """
        instance(recipe, ImportRecipe)
        if recipe.environment_id != self._environment_id:
            raise ValueError('Import recipe differs from the invocation environment hash')
        if fresh is not None:
            instance(fresh, LaunchSpec)
        if evidence_path is not None:
            evidence_path = Path(evidence_path).absolute()
            if not evidence_path.parent.resolve().is_relative_to(self.workspace):
                raise ValueError('Evidence FIFO must be in the private workspace')
        self._begin()
        attempts = []
        runtime = None
        try:
            for attempt in range(2):
                limits = self._remaining()
                if limits is None:
                    attempts.append(self._exhausted())
                    break
                if self._retained is not None and self._retained.key != recipe.key(self._limits):
                    self._discard()
                try:
                    if self._retained is None:
                        self._retained = _ImportWorker(recipe, self._parent, self._limits)
                    # Startup time belongs to the same invocation.
                    limits = self._remaining()
                    result = self._retained.run(recipe, self.workspace, limits, self.cancellation)
                except (OSError, ValueError) as error:
                    result = failure(Outcome.SETUP_FAILURE, str(error))
                attempts.append(self._record(result))
                runtime = recipe.runtime_sha256
                if result.outcome in (Outcome.COMPLETED, Outcome.COMPILER_ERROR):
                    break
                self._discard()
                if (attempt == 0 and result.returncode == 2
                        and b'non-append import change' in result.stdout.data):
                    continue
                break
            if (fresh is not None and attempts[-1].outcome not in
                    (Outcome.COMPLETED, Outcome.COMPILER_ERROR, Outcome.RESOURCE_LIMIT, Outcome.CANCELLED)):
                self._discard()
                limits = self._remaining()
                attempts.append(self._exhausted() if limits is None else
                                self._record(_launch(fresh, limits, cancel=self.cancellation, evidence_path=evidence_path)))
                runtime = None
            return ImportResult(tuple(attempts), runtime)
        finally:
            self._operation.release()


class Executor:
    """One bounded worker pool. Reject over-dispatch; never create another scheduler.

    Controller/policy already admitted the job. These ceilings guard dispatch
    mistakes and account for retained memory until actual process exit. Results
    and workspaces are handed back for controller-owned validation/publication.
    """
    def __init__(self, directory, *, workers=4, compiler_slots=1, compiler_memory_mib=4096,
                 progress_capacity=64):
        for name, value in (('workers', workers), ('compiler_slots', compiler_slots),
                            ('compiler_memory_mib', compiler_memory_mib), ('progress_capacity', progress_capacity)):
            natural(value, name, positive=True)
        if workers > 1024 or compiler_slots > workers or progress_capacity > 4096:
            raise ValueError('Executor capacity is out of range')
        self.directory = Path(directory).absolute()
        if self.directory.resolve(strict=True) != self.directory or not self.directory.is_dir():
            raise ValueError('Executor directory must exist without symlinks')
        self._workers, self._slots, self._memory = workers, compiler_slots, compiler_memory_mib
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='midas-worker')
        self._lock = threading.RLock()
        self._entries = {}
        self._unconfirmed = {}
        self._retained = {}
        self._progress = queue.Queue(progress_capacity)
        self._closed = False

    def _capacity(self):
        contexts = [ctx for _, ctx, _ in self._entries.values() if ctx._active and ctx._compiler]
        contexts.extend(ctx for ctx in self._unconfirmed.values() if ctx._compiler)
        return len(contexts), sum(ctx._limits.memory_mib for ctx in contexts)

    def start(self, invocation, execute, *, grant, workspace, reader=None, staging=None):
        instance(invocation, Invocation)
        instance(grant, CapacityGrant)
        grant.validate_for(invocation)
        if getattr(_LOCAL, 'inside', False):
            raise ValueError('Workers cannot recursively submit work')
        if not callable(execute):
            raise ValueError('Expected a worker callable')
        workspace = Path(workspace).absolute()
        if (workspace.resolve(strict=True) != workspace or not workspace.is_dir()
                or not workspace.is_relative_to(self.directory) or workspace == self.directory):
            raise ValueError('Expected a private workspace inside the executor directory')
        compiler = invocation.request.job.kind in _COMPILER_KINDS
        memory = invocation.request.job.limits.memory_mib
        with self._lock:
            if self._closed:
                raise RuntimeError('Executor is closed')
            key = invocation.original_execution_id
            if key in self._entries or key in self._unconfirmed or len(self._entries) + len(self._unconfirmed) >= self._workers:
                raise ValueError('Duplicate invocation or worker capacity exhausted; poll completions first')
            if any(ctx.workspace == workspace for _, ctx, _ in self._entries.values()):
                raise ValueError('Workspace is already owned by another invocation')
            retained = self._retained.get(grant.retained_worker_id)
            if grant.retained_worker_id is not None and (retained is None or retained.closed or not compiler or retained.memory_mib != memory):
                raise ValueError('Retained worker transfer differs from the capacity grant')
            slots, active_memory = self._capacity()
            if compiler and (slots >= self._slots or active_memory + memory > self._memory):
                raise ValueError('Compiler capacity grant exceeds executor capacity')
            if retained is not None:
                del self._retained[retained.id]
            if compiler:
                # Eviction is termination, not another reservation/waiting queue.
                while (slots + 1 + len(self._retained) > self._slots or
                       active_memory + memory + sum(w.memory_mib for w in self._retained.values()) > self._memory):
                    if not self.evict(next(iter(self._retained))):
                        if retained is not None:
                            self._retained[retained.id] = retained
                        raise ValueError('Retained worker termination is unconfirmed')
            context = WorkerContext(invocation, grant, workspace, reader, staging, self._progress, retained, self.directory)
            self._entries[key] = (invocation, context, None)
            try:
                future = self._pool.submit(self._execute, invocation, execute, context)
                self._entries[key] = (invocation, context, future)
            except Exception as error:
                context._discard()
                context._active = False
                future = Future()
                future.set_result(Completion(invocation, WorkerResult(key, invocation.request.request_hash,
                    ResultStatus.FAILED, (), None, None, Usage(), DeliveryCertainty.NOT_SENT, Termination.TERMINATED),
                    (type(error).__name__ + ': ' + str(error))[:4096]))
                self._entries[key] = (invocation, context, future)

    def _execute(self, invocation, execute, context):
        _LOCAL.inside = True
        context._thread_id = threading.get_ident()
        began = False
        detail = ''
        valid_result = None
        try:
            context.check()
            began = True
            result = execute(invocation.request, context)
            instance(result, WorkerResult)
            result.validate_for(invocation)
            valid_result = result
            context.check()
        except BaseException as error:
            detail = (type(error).__name__ + ': ' + str(error))[:4096]
            status = ResultStatus.CANCELLED if context.cancellation.is_set() else ResultStatus.FAILED
            if valid_result is not None:
                result = replace(valid_result, status=status)
            else:
                result = WorkerResult(invocation.original_execution_id, invocation.request.request_hash,
                    status, (), None, None, Usage(),
                    DeliveryCertainty.UNKNOWN if began else DeliveryCertainty.NOT_SENT, Termination.UNKNOWN)
        finally:
            _LOCAL.inside = False
        with self._lock:
            # The callable has returned. No context operation can start afterward.
            context._active = False
            if (self._closed or context.cancellation.is_set() or detail or
                    context._termination != Termination.TERMINATED):
                context._discard()
            retained_id = None
            if context._retained is not None:
                worker = context._retained
                if worker.closed or worker.process.poll() is not None:
                    context._discard()
                else:
                    self._retained[worker.id] = worker
                    retained_id = worker.id
                    context._retained = None
            result = replace(result, termination=context._termination)
            if result.termination != Termination.TERMINATED:
                # An unknown process tree is never turned into free capacity.
                # Recovery must reconcile OS ownership before a new executor.
                self._unconfirmed[invocation.original_execution_id] = context
            return Completion(invocation, result, detail, retained_id)

    def poll(self):
        with self._lock:
            ready = [key for key, (_, _, future) in self._entries.items() if future is not None and future.done()]
            return tuple(self._entries.pop(key)[2].result() for key in ready)

    def poll_progress(self, *, limit=64):
        natural(limit, 'Progress poll limit', positive=True)
        if limit > 4096:
            raise ValueError('Progress poll limit exceeds 4096')
        messages = []
        for _ in range(limit):
            try:
                messages.append(self._progress.get_nowait())
            except queue.Empty:
                break
        return tuple(messages)

    def cancel(self, invocation_id):
        with self._lock:
            entry = self._entries.get(invocation_id)
            if entry is None:
                return False
            entry[1].cancellation.set()
            return True

    def cancel_owned_work(self):
        with self._lock:
            for _, context, _ in self._entries.values():
                context.cancellation.set()

    def state(self):
        with self._lock:
            slots, memory = self._capacity()
            retained = tuple(RetainedWorker(w.id, w.memory_mib, w.key[1], w.key[2],
                w.termination if w.closed else Termination.UNKNOWN) for w in self._retained.values())
            active = sum(ctx._active for _, ctx, _ in self._entries.values())
            return ExecutorState(active, len(self._entries) - active, slots, memory, retained, tuple(self._unconfirmed))

    def evict(self, worker_id):
        with self._lock:
            worker = self._retained.get(worker_id)
            if worker is not None:
                worker.close()
                if worker.termination != Termination.TERMINATED:
                    return False
                del self._retained[worker_id]
                return True
            return False

    def drain(self, *, timeout=None):
        """Cancel and join. False means work still lives and capacity stays held."""
        if timeout is not None and (type(timeout) not in (int, float) or not 0 <= timeout <= 86400):
            raise ValueError('Invalid drain timeout')
        if getattr(_LOCAL, 'inside', False):
            raise ValueError('Workers cannot drain their executor')
        with self._lock:
            self._closed = True
            self.cancel_owned_work()
            for worker_id in tuple(self._retained):
                self.evict(worker_id)
            futures = [future for _, _, future in self._entries.values() if future is not None]
        _, remaining = wait(futures, timeout=timeout)
        self._pool.shutdown(wait=not remaining)
        return not remaining and not self._unconfirmed and not self._retained

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.drain()
