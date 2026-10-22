"""One event loop: durable admission, bounded progress, receipt application and drain."""
from collections import deque
from dataclasses import fields, replace
from hashlib import sha256
import shutil
import threading
import time
from uuid import uuid4
import weakref

from ..domain import (CapacityGrant, DeliveryCertainty, JobKind, ResultStatus,
                      Termination, Usage, WorkerResult)
from ..policy import BacklogState, Capacity, Failure, ProviderGate, RecoveryConfig, Services
from ..storage import Changes, Conflict, CounterChange, RecordChange
from . import records as saved
from .protocol import Accepted, Effect, Operations, Tick
from .allowances import owner_usage as stored_owner_usage

_OWNERS = weakref.WeakKeyDictionary()
_COMPILER = frozenset(JobKind) - {JobKind.MODEL, JobKind.SEARCH, JobKind.NETWORK_SEARCH}


class Controller:
    """Application supplies finite operations and a bounded planning projection.

    Only step/run/drain touch storage. request_stop may be called from another
    thread. A planner(reader, policy, limit) returns (application_key, Effect)
    pairs; reducers and planners use storage read APIs and own no transactions.
    """
    def __init__(self, writer, executor, policy, operations: Operations, *,
                 planner=None, services=Services(), owner_usage=None,
                 clock=time.monotonic, wall_clock=time.time, batch=64):
        if type(batch) is not int or not 1 <= batch <= 1000:
            raise ValueError('Controller batch must be 1 through 1000')
        if writer in _OWNERS and _OWNERS[writer]() is not None:
            raise ValueError('Writer already has a controller')
        if writer.frozen_run() is None or writer.session is None:
            raise ValueError('Controller requires frozen inputs and a writer session')
        self.writer, self.executor, self.policy, self.operations = writer, executor, policy, operations
        self.session = writer.session
        self.planner, self.services = planner, services
        self.owner_usage = owner_usage or stored_owner_usage
        self.clock, self.wall_clock, self.batch = clock, wall_clock, batch
        self._began, self._elapsed = clock(), writer.counter(saved.SCOPE, 'elapsed_ms')
        self._stop = threading.Event()
        self._active, self._retained_files = {}, {}
        self._progress_seen = {}
        self._pending_progress = deque()
        self._completed = deque()
        self._ready_cursor = self._receipt_cursor = None
        self._planned_revision = None
        self._backlog = BacklogState()
        self._closed = self._stepping = False
        self._recovering = True
        self._deadline = False
        self._draining = False
        self._resuming = False
        self._waiting_reasons = {}
        if writer.counter(saved.SCOPE, 'stopped'):
            self._stop.set()
        _OWNERS[writer] = weakref.ref(self)

    @property
    def closed(self):
        return self._closed

    @property
    def elapsed_ms(self):
        return self._elapsed + max(0, int((self.clock()-self._began)*1000))

    def now_ms(self):
        return max(0, int(self.wall_clock()*1000))

    def request_stop(self):
        self._resuming = False
        self._stop.set()

    def _check(self):
        if self._closed or self.writer.session != self.session:
            raise RuntimeError('Controller is closed or its session changed')

    def _checkpoint(self, tx):
        before = self.writer.counter(saved.SCOPE, 'elapsed_ms')
        elapsed = max(before, self.elapsed_ms)
        if elapsed != before:
            tx.apply('clock:'+str(uuid4()), Changes(counters=(CounterChange(saved.SCOPE, 'elapsed_ms', before, elapsed),)))

    def _publish_record(self, scope, key, value):
        durable = self.writer.publish(value, max_bytes=saved.META)
        return durable, saved.pointer(self.writer, scope, key, durable.reference)

    def _budget(self, execution_id, budget, *, exceeded=False):
        data, counters = saved.budget_changes(self.writer, execution_id, budget, exceeded=exceeded)
        durable, pointer = self._publish_record('controller.budget', execution_id, data)
        return durable, Changes(records=(pointer,), counters=counters)

    def _prepare_effect(self, effect):
        if not isinstance(effect, Effect):
            raise ValueError('Reducer/planner must return an Effect')
        artifacts = [self.writer.publish(a.source, reference=a.reference, max_bytes=a.reference.byte_count)
                     for a in effect.artifacts]
        records = list(effect.changes.records)
        for work in effect.work:
            data = saved.pack([saved.admission(work.admission),
                               [getattr(self.policy.config.recovery, f.name) for f in fields(RecoveryConfig)]])
            durable = self.writer.publish(data, max_bytes=saved.META)
            artifacts.append(durable)
            records.append(RecordChange('controller.admission', work.intent.request.job.semantic_key,
                                        None, durable.reference))
        return artifacts, replace(effect.changes, records=tuple(records), intents=tuple(w.intent for w in effect.work))

    def _apply(self, key, effect, *, receipt=None, adopt=False):
        artifacts, changes = self._prepare_effect(effect)
        with self.writer.transaction(self.session) as tx:
            new_count = sum(self.writer.intent_state(w.intent.request.job.semantic_key) is None for w in effect.work)
            if sum(n for _, n in self.writer.status_snapshot().intents)+new_count > self.policy.config.run.max_intents:
                raise ValueError('Lifetime intent allowance exhausted')
            for artifact in artifacts:
                tx.register(artifact)
            if adopt:
                tx.adopt_receipt(receipt.original_execution_id)
            applied = tx.apply(key, changes, receipt=receipt, disposition=effect.disposition)
            self._checkpoint(tx)
        if applied:
            # Storage invalidates all intents fenced by retired generations.
            for invocation, _, _ in self._active.values():
                if any(o in invocation.request.job.expected_versions for o in effect.cancel):
                    self.executor.cancel(invocation.original_execution_id)
            self._cancel_stale()
        return applied

    def publish(self, key, effect):
        self._check()
        return self._apply(key, effect)

    def _cancel_stale(self):
        for invocation, _, _ in self._active.values():
            current = self.writer.intent_state(invocation.request.job.semantic_key)
            if current != ('RUNNING', invocation.original_execution_id):
                self.executor.cancel(invocation.original_execution_id)

    def recover(self):
        """Reconcile a bounded batch. Saved receipts are admitted by step afterward."""
        self._check()
        keys = self.writer.unreconciled(self.session, limit=self.batch)
        # Each prepared reservation is known not to have crossed dispatch. Its
        # projected budget can be released in the same recovery transaction.
        for key in keys:
            state = self.writer.execution(key)
            budget = None
            if state.phase == 'PREPARED' and state.invocation.request.job.kind == JobKind.MODEL:
                durable, changes = self._budget(key, Usage(0, 0, 0, 0))
                reservation, _, _ = saved.load_reservation(self.writer, state.invocation)
                changes = replace(changes, counters=(*changes.counters,
                    saved.delta(self.writer, 'downstream', -reservation.request.downstream)))
                budget = durable, changes
            with self.writer.transaction(self.session) as tx:
                if budget:
                    tx.register(durable)
                    tx.apply('recover-budget:'+key, changes)
                tx.reconcile(limit=1)
        self._recovering = len(keys) == self.batch
        return len(keys)

    def resume(self):
        """Explicitly clear a persisted stop; retain original retry/usage history."""
        self._check()
        before = self.writer.counter(saved.SCOPE, 'stopped')
        if before:
            with self.writer.transaction(self.session) as tx:
                tx.apply('resume:'+str(uuid4()), Changes(counters=(CounterChange(saved.SCOPE, 'stopped', before, 0),)))
        self._stop.clear()
        self._resuming = True

    def _gate(self, route):
        return ProviderGate(*saved.record(self.writer, 'controller.gate', route, [0, False])) if route else ProviderGate()

    def _note_provider(self, operation, invocation, accepted, receipt):
        if operation.route is None or accepted.failure not in (Failure.TRANSPORT, Failure.RATE_LIMIT, Failure.TIMEOUT):
            return
        key = invocation.original_execution_id
        if self.writer.record('controller.failure', key) is not None:
            return
        route = operation.route(invocation.request, self.writer)
        _, recovery, _ = saved.load_reservation(self.writer, invocation)
        now = self.now_ms()
        scope = 'provider:'+route
        count = self.writer.record_count(scope, after=f'{max(0, now-recovery.failure_window_ms):020d}',
                                         before=f'{now:020d}:\uffff')
        gate = self.policy.provider_gate(now_ms=now, recent_failures=count+1,
            rate_limited=accepted.failure == Failure.RATE_LIMIT, retry_after_ms=accepted.retry_after_ms,
            previous=self._gate(route), saved_recovery=recovery)
        durable, pointer = self._publish_record('controller.gate', route,
                                                saved.pack([gate.not_before_ms, gate.unavailable]))
        with self.writer.transaction(self.session) as tx:
            tx.register(durable)
            tx.apply('provider-failure:'+key, Changes(records=(pointer,
                RecordChange(scope, f'{now:020d}:'+key, None, receipt.result_ref),
                RecordChange('controller.failure', key, None, receipt.result_ref))))

    def reset_provider(self, route):
        """Explicit recovery command after the application checks provider health."""
        self._check()
        durable, pointer = self._publish_record('controller.gate', route, saved.pack([0, False]))
        with self.writer.transaction(self.session) as tx:
            tx.register(durable)
            tx.apply('provider-reset:'+str(uuid4()), Changes(records=(pointer,)))

    def report_usage(self, execution_id, report_id, usage):
        """Correlated cumulative usage updates the original call, even after retry."""
        self._check()
        state = self.writer.execution(execution_id)
        cumulative = Usage(*(getattr(usage, k) if getattr(usage, k) is not None else getattr(state.usage, k)
                             for k in saved.FIELDS))
        projection, exceeded = saved.projection(state, cumulative)
        durable, changes = self._budget(execution_id, projection, exceeded=exceeded)
        with self.writer.transaction(self.session) as tx:
            changed = tx.report_usage(state.invocation, report_id, usage)
            if changed:
                tx.register(durable)
                tx.apply('usage:'+execution_id+':'+sha256(report_id.encode()).hexdigest(), changes)
        return changed

    def reconcile_termination(self, execution_id, observe):
        """Application's OS recovery observer must confirm exit before release.

        Observation does not grant proof authority or change the saved receipt.
        Unknown dispatch retries still require the frozen policy's opt-in.
        """
        self._check()
        state = self.writer.execution(execution_id)
        if not state.held:
            return True
        if observe(state.invocation) != Termination.TERMINATED:
            return False
        reservation, _, _ = saved.load_reservation(self.writer, state.invocation)
        budget = None
        if state.charged:
            projection, exceeded = saved.projection(state, state.usage, terminated=True)
            durable, changes = self._budget(execution_id, projection, exceeded=exceeded)
            budget = durable, replace(changes, counters=(*changes.counters,
                saved.delta(self.writer, 'downstream', -reservation.request.downstream)))
        with self.writer.transaction(self.session) as tx:
            tx.release_capacity(state.invocation, Termination.TERMINATED)
            if budget:
                tx.register(budget[0])
                tx.apply('terminated:'+execution_id, budget[1])
        self.executor.reconcile_termination(execution_id, Termination.TERMINATED)
        if execution_id in self._active:
            self._retire(state.invocation)
        if state.phase == 'UNKNOWN' and not self._stopping():
            result = WorkerResult(execution_id, state.invocation.request.request_hash, ResultStatus.FAILED,
                (), None, None, state.usage, DeliveryCertainty.UNKNOWN, Termination.TERMINATED)
            self._retry(state.invocation, result, Accepted(failure=Failure.TRANSPORT))
        return True

    def _stopping(self):
        self._deadline = self.elapsed_ms >= self.policy.config.run.runtime_ms
        return self._stop.is_set() or self._deadline or self._draining

    def _progress(self):
        count = 0
        # Keep at most one batch until publication and commit acknowledge it.
        # An I/O failure must retain both this message and the rest of the batch.
        if not self._pending_progress:
            self._pending_progress.extend(self.executor.poll_progress(limit=self.batch))
        while self._pending_progress:
            message = self._pending_progress[0]
            owned = self._active.get(message.invocation_id)
            if owned is None:
                self._pending_progress.popleft()
                continue
            invocation, staging, _ = owned
            operation = self.operations.for_kind(invocation.request.job.kind)
            try:
                if operation.progress is None:
                    raise ValueError('Operation does not emit progress')
                artifacts = [self.writer.publish_staged(staging, ref) for ref in message.artifacts]
                with self.writer.transaction(self.session) as tx:
                    for artifact in artifacts:
                        tx.register(artifact)
                operation.progress(invocation, message, self.writer)
                with self.writer.transaction(self.session) as tx:
                    tx.record_progress(invocation, message)
                count += 1
            except (ValueError, KeyError, Conflict):
                # A blocked emitter wakes through cooperative cancellation.
                self.executor.cancel(message.invocation_id)
            # Durable messages and explicit rejections both release the worker's
            # completion fence. Exceptions above leave the message for replay.
            self._progress_seen[message.invocation_id] = message.sequence+1
            self._pending_progress.popleft()
        return count

    def _retire(self, invocation, retained_id=None):
        owned = self._active.get(invocation.original_execution_id)
        if owned is None:
            return
        _, staging, workspace = owned
        self._progress_seen.pop(invocation.original_execution_id, None)
        staging.close()
        if retained_id is not None:
            self._retained_files.setdefault(retained_id, []).append(workspace)
        else:
            shutil.rmtree(workspace)
        self._active.pop(invocation.original_execution_id)

    def _collect_retired(self):
        alive = {worker.id for worker in self.executor.state().retained}
        for key in tuple(self._retained_files):
            if key not in alive:
                for workspace in self._retained_files.pop(key):
                    shutil.rmtree(workspace)

    def _receive(self):
        self._completed.extend(self.executor.poll())
        count = 0
        for _ in range(min(len(self._completed), self.batch)):
            completion = self._completed[0]
            invocation, result = completion.invocation, completion.result
            previous = self.writer.receipt(invocation.original_execution_id)
            if previous is not None:
                if not self.writer.verify_receipt(previous, invocation, result):
                    raise ValueError('Conflicting executor completion replay')
                if result.termination == Termination.TERMINATED:
                    self._retire(invocation, completion.retained_worker_id)
                self._completed.popleft()
                count += 1
                continue
            if completion.progress_count > self._progress_seen.get(invocation.original_execution_id, 0):
                self._completed.rotate(-1)
                continue
            owned = self._active.get(invocation.original_execution_id)
            if owned is None or owned[0] != invocation:
                raise ValueError('Executor completion is not owned by this controller')
            artifacts = []
            for ref in result.staged_artifacts:
                try:
                    known = self.writer.artifact(ref.id)
                except KeyError:
                    known = None
                if known != ref:
                    artifacts.append(self.writer.publish_staged(owned[1], ref))
            receipt, receipt_artifacts = self.writer.prepare_receipt(invocation, result)
            reservation, _, _ = saved.load_reservation(self.writer, invocation)
            settlement = self.policy.settle(reservation, invocation, receipt, result)
            budget = self._budget(invocation.original_execution_id, settlement.budget,
                exceeded=bool(settlement.exceeded_bounds)) if invocation.request.job.kind == JobKind.MODEL else None
            if budget and result.termination == Termination.TERMINATED:
                budget = budget[0], replace(budget[1], counters=(*budget[1].counters,
                    saved.delta(self.writer, 'downstream', -reservation.request.downstream)))
            with self.writer.transaction(self.session) as tx:
                for artifact in (*artifacts, *receipt_artifacts):
                    tx.register(artifact)
                received = tx.receive(invocation, result, receipt)
                if received and budget is not None:
                    tx.register(budget[0])
                    tx.apply('settle:'+invocation.original_execution_id, budget[1])
                self._checkpoint(tx)
            if result.termination == Termination.TERMINATED:
                self._retire(invocation, completion.retained_worker_id)
            self._completed.popleft()
            count += 1
        return count

    def _retry(self, invocation, result, accepted, *, resume=False):
        reservation, recovery, first_ms = saved.load_reservation(self.writer, invocation)
        history = saved.history(self.writer, invocation.request.job.semantic_key, first_ms, accepted.retry_after_ms)
        if result.termination != Termination.TERMINATED and not self.writer.execution(invocation.original_execution_id).held:
            result = replace(result, termination=Termination.TERMINATED)
        decision = self.policy.retry(reservation, invocation, result, accepted.failure, history,
            saved.run_usage(self.writer, self.elapsed_ms), now_ms=self.now_ms(), saved_recovery=recovery, resume=resume)
        if not decision.eligible:
            return False
        durable, pointer = self._publish_record('controller.retry', invocation.request.job.semantic_key,
                                                saved.retried(history, decision))
        with self.writer.transaction(self.session) as tx:
            tx.register(durable)
            tx.retry(invocation.original_execution_id, not_before_ms=decision.earliest_ms)
            tx.apply('retry:'+invocation.original_execution_id, Changes(records=(pointer,)))
        return True

    def _accept(self):
        keys = self.writer.pending_receipts(after=self._receipt_cursor or '', limit=self.batch)
        self._receipt_cursor = keys[-1] if len(keys) == self.batch else None
        applied = 0
        for key in keys:
            state = self.writer.execution(key)
            invocation, receipt = state.invocation, state.receipt
            result = self.writer.result(key)
            if not self.writer.verify_receipt(receipt, invocation, result):
                raise ValueError('Saved receipt authentication failed')
            operation = self.operations.for_kind(invocation.request.job.kind)
            if operation is None:
                continue
            try:
                accepted = operation.admit(invocation, result, receipt, self.writer, self.writer)
                if not isinstance(accepted, Accepted):
                    raise ValueError('Operation returned invalid admission')
            except (ValueError, KeyError):
                accepted = Accepted(failure=Failure.PROTOCOL)
            if operation.observe is not None:
                observation = operation.observe(self.writer, invocation, result)
                if observation is not None:
                    self._apply('observe:'+key, observation)
            if accepted.provider and accepted.request_id:
                with self.writer.transaction(self.session) as tx:
                    tx.bind_provider(invocation, accepted.provider, accepted.request_id)
            self._note_provider(operation, invocation, accepted, receipt)
            if state.held:
                continue
            if accepted.failure == Failure.CANCELLED and self._stopping():
                continue  # Durable, unapplied receipt can be resumed explicitly.
            try:
                if (accepted.failure is not None and not self._stopping()
                        and (operation.retry_allowed is None or operation.retry_allowed(self.writer, invocation)) and self._retry(
                        invocation, result, accepted, resume=self._resuming or invocation.session != self.session)):
                    continue
                effect = operation.reduce(self.writer, invocation, accepted)
                applied += self._apply('receipt:'+key, effect, receipt=receipt,
                    adopt=invocation.session != self.session and state.adopted_session_id != self.session.session_id)
            except Conflict:
                # A later tick reads current versions; no external call is repeated.
                continue
            except (ValueError, KeyError):
                effect = Effect(disposition='REJECTED')
                applied += self._apply('receipt:'+key, effect, receipt=receipt,
                    adopt=invocation.session != self.session and state.adopted_session_id != self.session.session_id)
        return applied

    def _capacity(self, retained_id=None):
        a = self.writer.status_snapshot().accounting
        state = self.executor.state()
        pending = sum(n for kind, n in self.writer.queue_counts() if JobKind(kind) in _COMPILER)
        projected = self.writer.counter(saved.SCOPE, 'downstream')
        return Capacity(a.models, a.compilers, a.locals, a.compiler_memory_mib,
            sum(w.memory_mib for w in state.retained if w.id != retained_id), pending+projected, networks=a.networks)

    def _dispatch(self):
        now = self.now_ms()
        ready = self.writer.ready(now, after=self._ready_cursor, limit=self.batch)
        self._ready_cursor = ((ready[-1].priority, ready[-1].not_before_ms, ready[-1].request.job.semantic_key)
                              if len(ready) == self.batch else None)
        waiting, dispatched = [], 0
        for intent in ready:
            if self._stopping():
                break
            frozen, job = intent.request, intent.request.job
            operation = self.operations.for_kind(job.kind)
            if operation is None:
                waiting.append((job.semantic_key, 'operation_unavailable'))
                continue
            data = saved.record(self.writer, 'controller.admission', job.semantic_key)
            if data is None:
                raise ValueError('Intent lacks controller admission metadata')
            admission = saved.load_admission(job, data[0])
            recovery = RecoveryConfig(*data[1])
            retained_id = None
            runtime = operation.retained(frozen) if operation.retained else None
            for worker in self.executor.state().retained:
                if (runtime, frozen.environment_id, job.limits.memory_mib) == (
                        worker.runtime_sha256, worker.environment_id, worker.memory_mib):
                    retained_id = worker.id
                    break
            capacity = self._capacity(retained_id)
            gate = self._gate(operation.route(frozen, self.writer) if operation.route else None)
            capacity = replace(capacity, provider_not_before_ms=gate.not_before_ms, provider_unavailable=gate.unavailable)
            # Evict incompatible idle workers before policy admission; memory is
            # released only when executor reports actual process exit.
            if job.kind in _COMPILER and capacity.compiler_memory_mib+capacity.retained_memory_mib+job.limits.memory_mib > self.policy.config.scheduling.compiler_memory_mib:
                for worker in self.executor.state().retained:
                    if worker.id != retained_id:
                        self.executor.evict(worker.id)
                capacity = self._capacity(retained_id)
            usage = self.owner_usage(self.writer, job.owner)
            run = saved.run_usage(self.writer, self.elapsed_ms)
            decision = self.policy.admit(admission, run, usage, capacity, self.services,
                                         now_ms=now, backlog_state=self._backlog, reserved_allowances=True)
            if not decision.allowed:
                waiting.append((job.semantic_key, decision.reason))
                continue
            first = saved.record(self.writer, 'controller.retry', job.semantic_key, [now])[0]
            durable = self.writer.publish(saved.reservation(decision.reservation, recovery, first),
                                          max_bytes=saved.META)
            initial_history = None
            if self.writer.record('controller.retry', job.semantic_key) is None:
                initial_history = self._publish_record('controller.retry', job.semantic_key, saved.pack([first, 0, 0, 0]))
            execution_id = str(uuid4())
            budget = self._budget(execution_id, admission.bounds) if job.kind == JobKind.MODEL else None
            if budget:
                budget = budget[0], replace(budget[1], counters=(*budget[1].counters,
                    saved.delta(self.writer, 'downstream', admission.downstream)))
            workspace = self.executor.directory / execution_id
            staging, committed = None, False
            try:
                # Complete local setup before reservation. Reservation and the
                # dispatch marker can then share one durable commit before I/O.
                workspace.mkdir(mode=0o700)
                staging = self.writer.staging(max_bytes=job.limits.output_bytes+job.limits.evidence_bytes+65536)
                reader = self.writer.artifact_reader((*job.input_refs, frozen.operation_payload, frozen.request_ref))
                with self.writer.transaction(self.session) as tx:
                    # Current reads and admission are repeated under the write fence.
                    decision = self.policy.admit(admission, saved.run_usage(self.writer, self.elapsed_ms),
                        self.owner_usage(self.writer, job.owner), replace(self._capacity(retained_id),
                            provider_not_before_ms=gate.not_before_ms, provider_unavailable=gate.unavailable), self.services,
                        now_ms=now, backlog_state=self._backlog, reserved_allowances=True)
                    if not decision.allowed or self._stopping():
                        continue
                    tx.register(durable)
                    if initial_history:
                        tx.register(initial_history[0])
                        tx.apply('retry-origin:'+execution_id, Changes(records=(initial_history[1],)))
                    invocation = tx.reserve(job.semantic_key, durable.reference,
                        expected_revision=self.writer.status_snapshot().revision, bounds=admission.bounds,
                        now_ms=now, execution_id=execution_id)
                    if budget:
                        tx.register(budget[0])
                        tx.apply('reserve-budget:'+execution_id, budget[1])
                    tx.mark_dispatched(invocation)
                    self._checkpoint(tx)
                committed = True
            except Conflict:
                continue
            except BaseException:
                # A commit may have succeeded before its acknowledgement failed.
                # No worker was called, so retain a not-sent completion for replay.
                if self.writer.intent_state(job.semantic_key) == ('RUNNING', execution_id):
                    from ..executor import Completion
                    committed = True
                    invocation = self.writer.invocation(execution_id)
                    self._active[execution_id] = invocation, staging, workspace
                    result = WorkerResult(execution_id, frozen.request_hash, ResultStatus.FAILED, (), None, None,
                        Usage(0, 0, 0, 0), DeliveryCertainty.NOT_SENT, Termination.TERMINATED)
                    self._completed.append(Completion(invocation, result))
                raise
            finally:
                if not committed:
                    if staging is not None:
                        staging.close()
                    if workspace.exists():
                        shutil.rmtree(workspace)
            self._active[execution_id] = invocation, staging, workspace
            try:
                if self._stopping():
                    raise RuntimeError('Stopped before worker launch')
                self.executor.start(invocation, operation.execute, grant=CapacityGrant(invocation, retained_id),
                                    workspace=workspace, reader=reader, staging=staging)
            except Exception:
                # Executor rejects before submission. The durable dispatch is
                # closed by a normal, authenticated not-sent receipt.
                from ..executor import Completion
                result = WorkerResult(execution_id, frozen.request_hash, ResultStatus.FAILED, (), None, None,
                                      Usage(0, 0, 0, 0), DeliveryCertainty.NOT_SENT, Termination.TERMINATED)
                self._completed.append(Completion(invocation, result))
            dispatched += 1
            self._waiting_reasons.pop(job.semantic_key, None)
        self._waiting_reasons.update(waiting)
        return dispatched, tuple(waiting)

    def step(self):
        self._check()
        if self._stepping:
            raise RuntimeError('Recursive controller step')
        self._stepping = True
        try:
            if self._recovering:
                self.recover()
            stopping = self._stopping()
            if stopping:
                if self._stop.is_set() and not self.writer.counter(saved.SCOPE, 'stopped'):
                    with self.writer.transaction(self.session) as tx:
                        tx.apply('stop:'+str(uuid4()), Changes(counters=(CounterChange(saved.SCOPE, 'stopped', 0, 1),)))
                        self._checkpoint(tx)
                self.executor.cancel_owned_work()
            self._cancel_stale()
            progress = self._progress()
            received = self._receive()
            # Completed workers retain staging until their last queued progress
            # sequence has passed through _progress, including across ticks.
            status = self.writer.status_snapshot()
            applied = self._accept() if dict(status.intents).get('RUNNING', 0) else 0
            self._collect_retired()
            if stopping or self._recovering:
                return Tick(progress, received, applied, stopped=stopping)
            revision = self.writer.status_snapshot().revision
            if self.planner is not None and revision != self._planned_revision:
                proposals = iter(self.planner(self.writer, self.policy, self.batch))
                exhausted = False
                for _ in range(self.batch):
                    proposal = next(proposals, None)
                    if proposal is None:
                        exhausted = True
                        break
                    self._apply(*proposal)
                self._planned_revision = self.writer.status_snapshot().revision if exhausted else revision
            if not dict(self.writer.status_snapshot().intents).get('PENDING', 0):
                return Tick(progress, received, applied)
            self._backlog = self.policy.tick(self._capacity().backlog, self._backlog)
            dispatched, waiting = self._dispatch()
            return Tick(progress, received, applied, dispatched, waiting)
        finally:
            self._stepping = False

    def run(self, progress, *, poll_interval=.01, observe=None):
        """Use the application's current root/export projection for completion."""
        if not 0 < poll_interval <= 1:
            raise ValueError('Invalid poll interval')
        while True:
            self.step()
            view = progress(self.writer)
            state = self.executor.state()
            committed = self.writer.status_snapshot()
            if self.planner is not None and self._planned_revision == committed.revision:
                # The planner exhausted this exact state. Application readiness
                # hints (such as unfinished submissions) cannot imply more local
                # work until something changes; only queued jobs remain ready.
                view = replace(view, ready=min(view.ready, dict(committed.intents).get('PENDING', 0)))
            if not state.running and view.ready and self._waiting_reasons:
                self._waiting_reasons = {key: reason for key, reason in self._waiting_reasons.items()
                    if (self.writer.intent_state(key) or (None,))[0] == 'PENDING'}
                pending = sum(n for _, n in self.writer.queue_counts())
                reasons = tuple(dict.fromkeys(self._waiting_reasons.values()))
                if pending and pending == len(self._waiting_reasons) and all(
                        not self.policy.transient_admission(reason) for reason in reasons):
                    view = replace(view, ready=max(0, view.ready-pending), reasons=reasons[:20])
            view = replace(view, elapsed_ms=self.elapsed_ms,
                stopped=self._stop.is_set(), active=state.running+state.pending_completions+len(self._completed),
                recovery_required=view.recovery_required or bool(state.unconfirmed) or self.writer.recovery_pending())
            completion = self.policy.completion(view)
            if observe is not None:
                observe(view, completion)
            if completion.exit_code is not None:
                if not self.drain(timeout=0):
                    return self.policy.completion(replace(view, recovery_required=True))
                return completion
            self._stop.wait(poll_interval) if not self._stop.is_set() else time.sleep(poll_interval)

    def drain(self, *, timeout=5):
        """Keep polling progress while cancelling. False retains ownership/capacity."""
        self._check()
        if not 0 <= timeout <= 60:
            raise ValueError('Drain timeout must be 0 through 60 seconds')
        self._draining = True
        until = time.monotonic()+timeout
        while True:
            self.step()
            state = self.executor.state()
            if not state.running and not state.pending_completions:
                ended = self.executor.drain(timeout=0)
                self._collect_retired()
                if ended and not self._completed and not self._active and not self._pending_progress:
                    self._closed = True
                    _OWNERS.pop(self.writer, None)
                    return True
            if time.monotonic() >= until:
                return False
            time.sleep(.005)
