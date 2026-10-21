"""Offline controller integration: real storage, deterministic workers and threads."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from midas.controller import (Accepted, Artifact, Controller, Effect, Operation, Operations, Planned,
                              checker_operation, model_operation)
from midas.controller import records
from midas.domain import (DeliveryCertainty, FrozenRequest, Job, JobKind, OwnerVersion, ProofNode,
                          ResourceLimits, ResultStatus, Termination, Usage, WorkerProgress, WorkerResult)
from midas.executor import Completion, Executor, ExecutorState, RetainedWorker
from midas.policy import AdmissionRequest, Config, Failure, Policy, Progress, Stage
from midas.storage import (Changes, Conflict, CounterChange, FrozenRun, Intent, NodeChange,
                           RecordChange, claim_writer, create)

ENV = 'env_'+'a'*64


class ManualExecutor:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir()
        self.active, self.completed, self.messages = {}, [], []
        self.unconfirmed, self.started = set(), []
        self.fail_start = False

    def start(self, invocation, execute, *, grant, workspace, reader, staging):
        grant.validate_for(invocation)
        if self.fail_start:
            raise ValueError('launch refused')
        self.active[invocation.original_execution_id] = (invocation, staging)
        self.started.append(invocation)

    def finish(self, invocation=None, *, status=ResultStatus.SUCCEEDED, usage=Usage(10, 0, 5, 100),
               delivery=DeliveryCertainty.DELIVERED, termination=Termination.TERMINATED, payload=b'ok', progress=0):
        invocation = invocation or self.started[-1]
        _, staging = self.active.pop(invocation.original_execution_id)
        output = staging.write_bounded(payload, max_bytes=len(payload))
        if invocation.request.job.kind != JobKind.MODEL:
            usage = Usage(0, 0, 0, 0)
        result = WorkerResult(invocation.original_execution_id, invocation.request.request_hash,
            status, (output,), output, None, usage, delivery, termination)
        self.completed.append(Completion(invocation, result, progress_count=progress))
        if termination == Termination.UNKNOWN:
            self.unconfirmed.add(invocation.original_execution_id)
        return result

    def poll(self):
        values, self.completed = tuple(self.completed), []
        return values

    def poll_progress(self, *, limit=64):
        values, self.messages = self.messages[:limit], self.messages[limit:]
        return tuple(values)

    def state(self):
        return ExecutorState(len(self.active), len(self.completed), 0, 0, (), tuple(self.unconfirmed))

    def cancel(self, key):
        if key in self.active:
            self.finish(self.active[key][0], status=ResultStatus.CANCELLED)
            return True
        return False

    def cancel_owned_work(self):
        for key in tuple(self.active):
            self.cancel(key)

    def drain(self, *, timeout=None):
        return not self.active and not self.unconfirmed

    def evict(self, worker_id):
        return False

    def reconcile_termination(self, key, termination):
        self.unconfirmed.discard(key)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.writer = create(self.root/'run')
        self.ref = self.writer.put(b'inputs', max_bytes=100)
        self.owner = OwnerVersion('obl_'+'b'*64, 0)
        self.node = ProofNode(self.owner, ENV, self.ref)
        with self.writer.transaction(self.writer.session) as tx:
            tx.freeze(FrozenRun(ENV, self.ref, self.ref))
            tx.apply('initial', Changes(nodes=(NodeChange(None, self.node),), roots=(self.owner.obligation_id,), graph_revision=0))
        self.executor = ManualExecutor(self.root/'work')
        self.time = 100.
        self.policy = Policy(replace(Config(), model=replace(Config().model, output_bytes=1_000_000)))
        self.controller = None
        self.admitted = []

    def tearDown(self):
        if self.controller and not self.controller._closed:
            self.controller.drain(timeout=.1)
        self.writer.close()
        self.temp.cleanup()

    def operation(self, *, failure=None, reducer=None, progress=None):
        def admit(invocation, result, receipt, reader, authority):
            self.assertTrue(authority.verify_receipt(receipt, invocation, result))
            self.admitted.append(invocation.original_execution_id)
            return Accepted(result.output, Failure.CANCELLED if result.status == ResultStatus.CANCELLED else failure)
        def reduce(reader, invocation, accepted):
            before = reader.counter('test', 'applied')
            return Effect(Changes(counters=(CounterChange('test', 'applied', before, before+1),)))
        return Operation(lambda *args: None, admit, reducer or reduce, progress)

    def build(self, operation=None, **kwargs):
        op = operation or self.operation()
        self.controller = Controller(self.writer, self.executor, self.policy,
            Operations(model=op, search=op, binding=op, proof=op, probe=op),
            clock=lambda: self.time, wall_clock=lambda: self.time, **kwargs)
        return self.controller

    def planned(self, key='job', *, kind=JobKind.MODEL, depends=(), bounds=Usage(100, 10, 50, 1000),
                downstream=0, priority=0, not_before=0):
        job = Job(key, kind, self.owner, (self.ref,), (self.owner,), ResourceLimits(1000, 128, 1_000_000, 1_000_000, 1000), depends)
        frozen = FrozenRequest(job, ENV, self.ref, self.ref)
        return Planned(Intent(frozen, priority, not_before), AdmissionRequest(job, bounds, downstream))

    def publish(self, *work):
        work = work or (self.planned(),)
        self.controller.publish('plan:'+work[0].intent.request.job.semantic_key, Effect(work=work))

    def test_full_lifecycle_receipt_and_effect_are_idempotent(self):
        c = self.build()
        self.publish()
        self.assertEqual(c.step().dispatched, 1)
        invocation = self.executor.started[0]
        self.assertEqual(self.writer.execution(invocation.original_execution_id).phase, 'DISPATCHED')
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(100, 10, 50, 1000))
        self.executor.finish()
        tick = c.step()
        self.assertEqual((tick.received, tick.applied), (1, 1))
        self.assertEqual(self.writer.counter('test', 'applied'), 1)
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 1)
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(10, 0, 5, 100))
        self.assertEqual(c.step().applied, 0)
        self.assertFalse(tuple(self.executor.directory.iterdir()))
        self.writer.validate()

    def test_atomic_downstream_work_and_prerequisites(self):
        def reduce(reader, invocation, accepted):
            if invocation.request.job.semantic_key == 'first':
                return Effect(work=(self.planned('second', kind=JobKind.SEARCH, depends=('first',)),))
            return Effect()
        c = self.build(self.operation(reducer=reduce))
        self.publish(self.planned('first'))
        c.step()
        self.executor.finish()
        self.assertEqual(c.step().dispatched, 1)
        self.assertEqual([i.request.job.semantic_key for i in self.executor.started], ['first', 'second'])

    def test_policy_capacity_and_budget_include_pending_reservations(self):
        self.policy = Policy(replace(self.policy.config, run=replace(Config().run, max_input_tokens=150)))
        c = self.build()
        self.publish(self.planned('a'), self.planned('b'))
        tick = c.step()
        self.assertEqual(tick.dispatched, 1)
        self.assertIn(('b', 'usage_budget_exhausted:input_tokens'), tick.waiting)
        self.executor.finish()
        self.assertEqual(c.step().dispatched, 1)

    def test_unbounded_unknown_usage_blocks_finite_budget(self):
        self.policy = Policy(replace(self.policy.config, run=replace(Config().run, max_input_tokens=150)))
        c = self.build()
        self.publish(self.planned(bounds=Usage()))
        self.assertEqual(c.step().waiting, (('job', 'unknown_usage_bound:input_tokens'),))

    def test_bound_overrun_blocks_following_calls(self):
        c = self.build()
        self.publish()
        c.step()
        self.executor.finish(usage=Usage(1000, 0, 5, 100))
        c.step()
        self.publish(self.planned('next'))
        self.assertEqual(c.step().waiting, (('next', 'usage_bound_exceeded'),))

    def test_fair_paging_passes_a_blocked_high_priority_job(self):
        c = self.build(batch=1)
        self.publish(self.planned('a', bounds=Usage(), downstream=9999), self.planned('b', kind=JobKind.SEARCH))
        self.assertEqual(c.step().dispatched, 0)
        self.assertEqual(c.step().dispatched, 1)
        self.assertEqual(self.executor.started[-1].request.job.semantic_key, 'b')

    def test_retry_preserves_wire_and_original_turn_usage(self):
        c = self.build(self.operation(failure=Failure.TRANSPORT))
        self.publish()
        c.step()
        first = self.executor.started[-1]
        self.executor.finish(status=ResultStatus.FAILED, delivery=DeliveryCertainty.NOT_SENT, usage=Usage())
        c.step()
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 0)
        self.assertEqual(self.writer.counter('test', 'applied'), 0)
        self.time += 3
        c.step()
        second = self.executor.started[-1]
        self.assertNotEqual(first.original_execution_id, second.original_execution_id)
        self.assertEqual(first.request, second.request)
        self.assertIsNotNone(self.writer.receipt(first.original_execution_id))

    def test_unknown_delivery_needs_frozen_retry_opt_in(self):
        c = self.build(self.operation(failure=Failure.TRANSPORT))
        self.publish()
        c.step()
        self.executor.finish(status=ResultStatus.FAILED, delivery=DeliveryCertainty.UNKNOWN)
        c.step()
        self.time += 5
        c.step()
        self.assertEqual(len(self.executor.started), 1)
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 1)

    def test_launch_rejection_refunds_only_provably_not_sent(self):
        c = self.build(self.operation(failure=Failure.PERMANENT))
        self.executor.fail_start = True
        self.publish()
        c.step()
        c.step()
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 0)
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(0, 0, 0, 0))
        self.assertEqual(self.writer.counter('test', 'applied'), 1)

    def test_publication_failure_retries_completion_without_reexecution(self):
        c = self.build()
        self.publish()
        c.step()
        self.executor.finish()
        with patch.object(self.writer, 'publish_staged', side_effect=OSError('disk')), self.assertRaises(OSError):
            c.step()
        self.assertEqual(len(c._completed), 1)
        self.assertEqual(c.step().applied, 1)
        self.assertEqual(len(self.executor.started), 1)

    def test_receipt_commit_acknowledgement_failure_does_not_settle_twice(self):
        c = self.build()
        self.publish(self.planned(downstream=2))
        c.step()
        self.executor.finish()
        def crash(stage):
            if stage == 'after_commit':
                raise OSError('lost commit acknowledgement')
        with patch('midas.storage.artifacts.checkpoint', crash), self.assertRaises(OSError):
            c.step()
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 0)
        self.assertEqual(c.step().applied, 1)
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 1)
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(10, 0, 5, 100))

    def test_dispatch_commit_acknowledgement_failure_is_provably_not_sent(self):
        c = self.build(self.operation(failure=Failure.PERMANENT))
        self.publish(self.planned(downstream=2))
        def crash(stage):
            if stage == 'after_commit':
                raise OSError('lost dispatch acknowledgement')
        with patch('midas.storage.artifacts.checkpoint', crash), self.assertRaises(OSError):
            c.step()
        self.assertFalse(self.executor.started)
        self.assertEqual(len(c._completed), 1)
        c.step()
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 0)
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 0)

    def test_local_setup_failure_leaves_no_reservation_or_workspace(self):
        c = self.build()
        self.publish()
        with patch.object(self.writer, 'staging', side_effect=OSError('disk full')), self.assertRaises(OSError):
            c.step()
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 0)
        self.assertFalse(tuple(self.executor.directory.iterdir()))
        self.assertEqual(c.step().dispatched, 1)

    def test_rate_limit_gate_survives_retry_and_is_route_scoped(self):
        operation = replace(self.operation(failure=Failure.RATE_LIMIT), route=lambda *args: 'route-a')
        c = self.build(operation)
        self.publish()
        c.step()
        self.executor.finish(status=ResultStatus.FAILED)
        c.step()
        self.time += 3
        self.assertEqual(c.step().waiting, (('job', 'provider_cooldown'),))
        self.assertEqual(c._gate('route-b').not_before_ms, 0)
        self.time += 60
        self.assertEqual(c.step().dispatched, 1)

    def test_same_session_explicit_resume_retries_cancelled_receipt(self):
        c = self.build()
        self.publish()
        c.step()
        c.request_stop()
        c.step()
        c.resume()
        self.assertEqual(c.step().dispatched, 1)
        self.assertEqual(len(self.executor.started), 2)
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 2)

    def test_owner_retirement_cancels_after_commit_and_saves_historical_receipt(self):
        c = self.build()
        self.publish()
        c.step()
        invocation = self.executor.started[-1]
        old = self.writer.node(self.owner.obligation_id)
        owner = replace(self.owner, generation=1)
        c.publish('retire', Effect(Changes(nodes=(NodeChange(old.version, replace(self.node, owner=owner)),),
            graph_revision=1), cancel=(self.owner,)))
        c.step()
        self.assertIsNotNone(self.writer.receipt(invocation.original_execution_id))
        self.assertEqual(self.writer.counter('test', 'applied'), 0)
        self.assertEqual(self.writer.node(owner.obligation_id).node.owner, owner)

    def test_unknown_termination_holds_capacity_until_observed(self):
        c = self.build()
        self.publish(self.planned(downstream=2))
        c.step()
        invocation = self.executor.started[-1]
        self.executor.finish(termination=Termination.UNKNOWN)
        c.step()
        self.assertEqual(self.writer.status_snapshot().accounting.models, 1)
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 2)
        self.assertFalse(c.reconcile_termination(invocation.original_execution_id, lambda _: Termination.UNKNOWN))
        self.assertTrue(c.reconcile_termination(invocation.original_execution_id, lambda _: Termination.TERMINATED))
        self.assertEqual(self.writer.status_snapshot().accounting.models, 0)
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 0)

    def test_late_cumulative_usage_updates_original_budget_once(self):
        c = self.build()
        self.publish()
        c.step()
        invocation = self.executor.started[-1]
        self.executor.finish(usage=Usage())
        c.step()
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(100, 10, 50, 1000))
        self.assertTrue(c.report_usage(invocation.original_execution_id, 'late', Usage(20, 2, 10, 200)))
        self.assertFalse(c.report_usage(invocation.original_execution_id, 'late', Usage(20, 2, 10, 200)))
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(20, 2, 10, 200))
        with self.assertRaises(Conflict):
            c.report_usage(invocation.original_execution_id, 'decreasing', Usage(10, 0, 5, 100))

    def test_completion_waits_for_all_queued_progress_across_batches(self):
        c = self.build(self.operation(progress=lambda *args: None), batch=1)
        self.publish(self.planned(kind=JobKind.PROBE))
        c.step()
        invocation = self.executor.started[-1]
        for index in range(3):
            self.executor.messages.append(WorkerProgress(invocation.original_execution_id, index, 'probe'))
        self.executor.finish(progress=3)
        self.assertEqual(c.step().received, 0)
        self.assertEqual(c.step().received, 0)
        self.assertEqual(c.step().received, 1)
        self.assertEqual(len(self.writer.progress(invocation.original_execution_id)), 3)

    def finish_with_progress(self):
        invocation = self.executor.started[-1]
        staging = self.executor.active[invocation.original_execution_id][1]
        messages = tuple(WorkerProgress(invocation.original_execution_id, index, 'probe',
            (staging.write_bounded(f'case {index}'.encode(), max_bytes=100),)) for index in range(3))
        self.executor.messages.extend(messages)
        self.executor.finish(progress=len(messages))
        return invocation, messages

    def test_progress_publication_failure_replays_rest_of_batch(self):
        c = self.build(self.operation(progress=lambda *args: None), batch=3)
        self.publish(self.planned(kind=JobKind.PROBE))
        c.step()
        invocation, messages = self.finish_with_progress()
        publish = self.writer.publish_staged
        calls = 0
        def interrupted(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('progress publication interrupted')
            return publish(*args)
        with patch.object(self.writer, 'publish_staged', interrupted), self.assertRaises(OSError):
            c.step()
        self.assertEqual(self.writer.progress(invocation.original_execution_id), messages[:1])
        self.assertEqual(c._progress_seen[invocation.original_execution_id], 1)
        self.assertEqual(tuple(c._pending_progress), messages[1:])
        tick = c.step()
        self.assertEqual((tick.progress, tick.received, tick.applied), (2, 1, 1))
        self.assertEqual(self.writer.progress(invocation.original_execution_id), messages)
        self.assertEqual(len(self.executor.started), 1)
        self.assertTrue(c.drain(timeout=0))

    def test_progress_commit_failure_replays_without_duplicate_records(self):
        c = self.build(self.operation(progress=lambda *args: None), batch=2)
        for failure in ('before_commit', 'after_commit'):
            with self.subTest(failure=failure):
                self.publish(self.planned(failure, kind=JobKind.PROBE))
                c.step()
                invocation, messages = self.finish_with_progress()
                commits = 0
                def interrupted(stage):
                    nonlocal commits
                    if stage == failure:
                        commits += 1
                        # First publish artifact metadata, then record progress.
                        if commits == 2:
                            raise OSError('progress commit interrupted')
                with patch('midas.storage.artifacts.checkpoint', interrupted), self.assertRaises(OSError):
                    c.step()
                self.assertEqual(len(self.writer.progress(invocation.original_execution_id)),
                                 int(failure == 'after_commit'))
                self.assertEqual(c._progress_seen.get(invocation.original_execution_id, 0), 0)
                self.assertEqual(tuple(c._pending_progress), messages[:2])
                self.assertEqual(c.step().received, 0)
                self.assertEqual(c.step().received, 1)
                self.assertEqual(self.writer.progress(invocation.original_execution_id), messages)
                self.assertFalse(c._pending_progress)
        self.assertEqual(len(self.executor.started), 2)
        self.assertEqual(self.writer.counter('test', 'applied'), 2)
        self.assertTrue(c.drain(timeout=0))

    def test_duplicate_completion_with_progress_does_not_wait_on_retired_staging(self):
        c = self.build(self.operation(progress=lambda *args: None))
        self.publish(self.planned(kind=JobKind.PROBE))
        c.step()
        invocation = self.executor.started[-1]
        self.executor.messages.append(WorkerProgress(invocation.original_execution_id, 0, 'probe'))
        result = self.executor.finish(progress=1)
        c.step()
        self.executor.completed.append(Completion(invocation, result, progress_count=1))
        c.step()
        self.assertFalse(c._completed)
        self.assertEqual(self.writer.counter('test', 'applied'), 1)

    def test_stop_keeps_cancelled_receipt_for_resume(self):
        c = self.build()
        self.publish()
        c.step()
        c.request_stop()
        tick = c.step()
        self.assertTrue(tick.stopped)
        self.assertEqual(tick.applied, 0)
        self.assertEqual(self.writer.counter(records.SCOPE, 'stopped'), 1)
        self.assertEqual(len(self.writer.pending_receipts()), 1)

    def test_run_reports_recovery_when_retained_worker_cannot_drain(self):
        c = self.build()
        worker = RetainedWorker('retained', 128, ENV, 'a'*64)
        workspace = self.root/'retained-workspace'
        workspace.mkdir()
        c._retained_files[worker.id] = [workspace]
        state = ExecutorState(0, 0, 1, 128, (worker,))
        complete = Progress(roots=1, verified_roots=1, integration=Stage.VERIFIED,
                            export_verification=Stage.VERIFIED)
        with patch.object(self.executor, 'state', return_value=state), \
                patch.object(self.executor, 'drain', return_value=False):
            result = c.run(lambda reader: complete)
            self.assertEqual((result.outcome, result.exit_code), ('recovery_required', 2))
            self.assertIn('recovery_required', result.reasons)
            self.assertFalse(c._closed)
            self.assertTrue(workspace.exists())
            self.assertEqual(self.executor.state().compiler_memory_mib, 128)
            with self.assertRaises(ValueError):
                Controller(self.writer, self.executor, self.policy, Operations())
        self.assertTrue(c.drain(timeout=0))
        self.assertFalse(workspace.exists())

    def test_run_returns_success_after_confirmed_drain(self):
        c = self.build()
        result = c.run(lambda reader: Progress(roots=1, verified_roots=1,
            integration=Stage.VERIFIED, export_verification=Stage.VERIFIED))
        self.assertEqual((result.outcome, result.exit_code), ('export_verified', 0))
        self.assertTrue(c._closed)

    def test_reducer_rejection_is_bounded_and_does_not_stop_other_receipts(self):
        def reject(*args):
            raise ValueError('untrusted response')
        c = self.build(self.operation(reducer=reject))
        self.publish(self.planned('a'), self.planned('b'))
        c.step()
        for invocation in tuple(self.executor.started):
            self.executor.finish(invocation)
        self.assertEqual(c.step().applied, 2)
        for invocation in self.executor.started:
            self.assertEqual(self.writer.execution(invocation.original_execution_id).disposition, 'REJECTED')

    def test_version_conflict_revalidates_without_repeating_external_work(self):
        count = 0
        def reduce(reader, invocation, accepted):
            nonlocal count
            count += 1
            return Effect(Changes(counters=(CounterChange('test', 'applied', 1 if count == 1 else 0, 1),)))
        c = self.build(self.operation(reducer=reduce))
        self.publish()
        c.step()
        self.executor.finish()
        self.assertEqual(c.step().applied, 0)
        self.assertEqual(c.step().applied, 1)
        self.assertEqual(len(self.executor.started), 1)

    def test_large_result_and_effect_avoid_json(self):
        c = self.build()
        self.publish()
        c.step()
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            self.executor.finish(payload=b'p'*900_000)
            self.assertEqual(c.step().applied, 1)

    def test_single_controller_per_writer(self):
        self.build()
        with self.assertRaises(ValueError):
            Controller(self.writer, self.executor, self.policy, Operations())

    def test_candidate_allowance_closes_once_and_refunds_protocol_failure(self):
        from midas.controller import owner_usage, reserve_candidate, settle_candidate
        from midas.strategy import AttemptDecision
        from uuid import uuid4
        c = self.build()
        candidate = str(uuid4())
        c.publish('candidate', reserve_candidate(self.writer, self.policy, self.owner, candidate, 'direct'))
        self.assertTrue(owner_usage(self.writer, self.owner).pending_candidate)
        self.assertEqual(owner_usage(self.writer, self.owner).direct, 1)
        with self.assertRaises(ValueError):
            reserve_candidate(self.writer, self.policy, self.owner, str(uuid4()), 'direct')
        decision = AttemptDecision(candidate, 'exception', 0, 'protocol')
        c.publish('candidate-result', settle_candidate(self.writer, self.owner, decision))
        usage = owner_usage(self.writer, self.owner)
        self.assertEqual((usage.direct, usage.protocol, usage.pending_candidate), (0, 1, False))
        with self.assertRaises(ValueError):
            settle_candidate(self.writer, self.owner, decision)

    def test_helper_allowance_is_shared_by_all_continuation_stages(self):
        from midas.controller import owner_usage, reserve_helper
        from midas.tools import HelperRequest, Retrieve, Scope, plan_helper
        c = self.build()
        helper = HelperRequest(Scope(self.owner, ENV, self.ref), Retrieve(names=('True',)), 'prover')
        plan = plan_helper(helper, self.policy, owner_usage(self.writer, self.owner), c.services)
        c.publish('helper', reserve_helper(self.writer, self.policy, self.owner, plan, c.services))
        c.publish('duplicate-helper', reserve_helper(self.writer, self.policy, self.owner, plan, c.services))
        c.publish('continuation', reserve_helper(self.writer, self.policy, self.owner, replace(plan, charge=None), c.services))
        self.assertEqual(owner_usage(self.writer, self.owner).retrieval, 1)

    def test_reserved_research_uses_its_own_resource_profile(self):
        from midas.controller import owner_usage, reserve_helper
        from midas.policy import Services
        from midas.tools import HelperRequest, Research, Scope, plan_helper
        config = self.policy.config
        self.policy = Policy(replace(config, helpers=replace(config.helpers, research=1),
            model=replace(config.model, output_bytes=10000),
            research_model=replace(config.research_model, output_bytes=1_000_000)))
        c = self.build(services=Services(research=True, research_web_calls=1))
        helper = HelperRequest(Scope(self.owner, ENV, self.ref), Research('Find an approach', web_calls=1), 'prover')
        plan = plan_helper(helper, self.policy, owner_usage(self.writer, self.owner), c.services)
        work = self.planned()
        work = replace(work, admission=replace(work.admission, helper='research'))
        effect = reserve_helper(self.writer, self.policy, self.owner, plan, c.services)
        c.publish('research', replace(effect, work=(work,)))
        self.assertEqual(owner_usage(self.writer, self.owner).research, 1)
        self.assertEqual(c.step().dispatched, 1)

    def test_scripted_provider_flows_through_real_strategy_and_atomic_publication(self):
        from midas.controller import artifact
        from midas.domain import ProofGraph
        from midas.prompt import Options, Section, Snapshot as Display
        from midas.provider import Limits, ModelOutput, ModelRequest, Provider, Route, ScriptedTransport
        from midas.strategy import Context, Dispatch, Snapshot, Strategy, Subject, Version
        from tests.test_strategy import proof, response
        from uuid import uuid4
        self.executor = Executor(self.root, workers=1)
        ctx = Context('sel_'+'a'*64, (Subject('target', 'goal'),))
        ctx_ref = self.writer.put(ctx.payload, max_bytes=len(ctx.payload))
        snapshot = Snapshot(Version(self.owner, 0, ctx_ref), ctx, ENV,
            ProofGraph(1, (self.owner.obligation_id,), (self.node,)), workflow='scripted')
        strategy = Strategy()
        display = Display((Section('subject', 'goal', b'Goal: True'),), (ctx_ref,), (self.owner,))
        prepared = strategy.prepare_model(snapshot, self.policy, strategy.plan(snapshot, self.policy).work[0],
                                          display, Options('azure_responses', 'model'))
        wire = self.writer.put(prepared.wire.wire, max_bytes=len(prepared.wire.wire))
        route = Route('azure', 'model', 'azure_responses', 'test', 'https://example.test/responses')
        limits = replace(Limits.from_config(self.policy.config.model), output_bytes=100000,
                         frame_bytes=200000, event_bytes=500000)
        request = ModelRequest(route, 'prover', limits, wire)
        req = self.writer.put(request.payload, max_bytes=len(request.payload))
        job = Job('scripted', JobKind.MODEL, self.owner, request.inputs, (self.owner,),
                  ResourceLimits(2000, 128, 100000, 600000))
        frozen = FrozenRequest(job, ENV, req, req)
        dispatch = Dispatch.bind(prepared, frozen)
        provider = Provider(((route, ScriptedTransport({wire.sha256: ModelOutput(raw=response(proof()),
                                                                              usage=Usage(10, 0, 5, 100))})),))
        def reduce(reader, invocation, accepted):
            proposed = strategy.on_model(snapshot, self.policy, dispatch, accepted.value, reader, submission_id=str(uuid4()))
            draft = artifact(proposed.draft.payload)
            return Effect(Changes(records=(RecordChange('test', 'draft', None, draft.reference),)), artifacts=(draft,))
        c = self.build(model_operation(provider, reduce))
        self.publish(Planned(Intent(frozen), AdmissionRequest(job)))
        until = time.monotonic()+3
        while self.writer.record('test', 'draft') is None and time.monotonic() < until:
            c.step()
            time.sleep(.001)
        record = self.writer.record('test', 'draft')
        self.assertIsNotNone(record)
        from midas.strategy import Draft
        draft = Draft.load(self.writer.read_artifact(record.artifact, max_bytes=8*1024**2))
        self.assertEqual(draft.mode, 'direct')
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 1)
        self.assertIsNone(self.writer.node(self.owner.obligation_id).node.selected)

    def reopen(self, operation=None):
        # Simulate process death after completed worker publication. The previous
        # controller cannot be called after its writer has closed.
        self.controller._closed = True
        self.writer.close()
        self.writer = claim_writer(self.root/'run')
        self.executor = ManualExecutor(self.root/('work-'+str(self.writer.session.epoch)))
        return self.build(operation)

    def test_resume_adopts_receipt_without_new_charge_or_call(self):
        c = self.build()
        self.publish()
        c.step()
        invocation = self.executor.started[-1]
        self.executor.finish()
        c._receive()
        c = self.reopen()
        tick = c.step()
        self.assertEqual((tick.applied, tick.dispatched), (1, 0))
        state = self.writer.execution(invocation.original_execution_id)
        self.assertEqual(state.adopted_session_id, self.writer.session.session_id)
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 1)

    def test_recovery_preserves_unknown_dispatch_and_budget(self):
        c = self.build()
        self.publish(self.planned(downstream=3))
        c.step()
        invocation = self.executor.started[-1]
        c = self.reopen()
        c.step()
        state = self.writer.execution(invocation.original_execution_id)
        self.assertEqual(state.phase, 'UNKNOWN')
        self.assertTrue(state.held)
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 3)
        self.assertEqual(len(self.executor.started), 0)

    def test_prepared_recovery_refunds_budget_atomically(self):
        from midas.policy import Reservation
        c = self.build()
        work = self.planned(downstream=3)
        self.publish(work)
        reservation = self.writer.publish(records.reservation(Reservation(work.admission, 1),
            self.policy.config.recovery, c.now_ms()), max_bytes=records.META)
        from uuid import uuid4
        key = str(uuid4())
        budget, changes = c._budget(key, work.admission.bounds)
        with self.writer.transaction(self.writer.session) as tx:
            tx.register(reservation)
            tx.register(budget)
            tx.reserve('job', reservation.reference, expected_revision=self.writer.status_snapshot().revision,
                       bounds=work.admission.bounds, now_ms=c.now_ms(), execution_id=key)
            tx.apply('prepared-budget', replace(changes, counters=(*changes.counters,
                CounterChange(records.SCOPE, 'downstream', 0, 3))))
        c = self.reopen()
        def crash(stage):
            if stage == 'before_commit':
                raise OSError('recovery interrupted')
        with patch('midas.storage.artifacts.checkpoint', crash), self.assertRaises(OSError):
            c.recover()
        self.assertEqual(self.writer.execution(key).phase, 'PREPARED')
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 3)
        c.recover()
        self.assertEqual(self.writer.execution(key).phase, 'NOT_SENT')
        self.assertEqual(self.writer.status_snapshot().accounting.turns, 0)
        self.assertEqual(records.run_usage(self.writer, 0).budget, Usage(0, 0, 0, 0))
        self.assertEqual(self.writer.counter(records.SCOPE, 'downstream'), 0)

    def test_real_executor_progress_backpressure_is_drained(self):
        self.executor = Executor(self.root, workers=1, progress_capacity=1)
        def execute(frozen, context):
            for index in range(8):
                context.emit('case')
            ref = context.staging.write_bounded(b'ok', max_bytes=2)
            return WorkerResult(context.invocation_id, frozen.request_hash, ResultStatus.SUCCEEDED,
                (ref,), ref, None, Usage(0, 0, 0, 0), DeliveryCertainty.DELIVERED, Termination.TERMINATED)
        op = replace(self.operation(progress=lambda *args: None), execute=execute)
        c = self.build(op, batch=1)
        self.publish(self.planned(kind=JobKind.SEARCH))
        until = time.monotonic()+3
        while time.monotonic() < until and not self.writer.counter('test', 'applied'):
            c.step()
            time.sleep(.001)
        self.assertEqual(self.writer.counter('test', 'applied'), 1)
        key = self.writer.executions('RECEIVED')[0]
        self.assertEqual(len(self.writer.progress(key)), 8)


class CheckerOperationTests(unittest.TestCase):
    def test_resource_failures_preserve_limits_and_recursion_diagnostics(self):
        from midas.checker import Checker, prepare_proof, worker_payload
        from midas.domain import Candidate, ReceiptBinding
        from midas.domain.encoding import load_record
        from midas.executor import Diagnostic, LaunchSpec, Outcome, ProcessResult
        from tests.test_checker import Artifacts, issue, selection
        from uuid import uuid4

        artifacts, selected = Artifacts(), selection()
        owner = OwnerVersion(selected.identities[0].id, 0)
        candidate = Candidate(str(uuid4()), owner, selected.environment_id, artifacts.save(b'by trivial'))
        request = prepare_proof(selected, 0, candidate, artifacts)
        invocation = issue(request, artifacts)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = SimpleNamespace(id=selected.environment_id, materialize=lambda *a, **k: None)
            runtime = SimpleNamespace(id='4'*64, environment_id=environment.id, retained=False,
                launch=lambda *a, **k: LaunchSpec(('/compiler',), (), '/usr/bin/false', '0'*64))
            checker = Checker(environment, root, runtime=runtime, retained=False)
            operation = checker_operation(checker, lambda *args: Effect())
            for limit, failure, stderr in (
                    ('memory', Failure.MEMORY, b''), ('wall', Failure.TIMEOUT, b''),
                    ('output', Failure.TIMEOUT, b''), ('evidence', Failure.TIMEOUT, b''),
                    (None, Failure.RECURSION_DEPTH, b'MIDAS_RECURSION_DEPTH\nmaximum recursion depth reached\n'),
                    (None, Failure.REJECTION, b'MIDAS_CHECK_REJECTED\nmaximum recursion depth reached\n'),
                    (None, Failure.CRASH, b'maximum recursion depth reached\n')):
                with self.subTest(limit=limit):
                    process = ProcessResult(Outcome.RESOURCE_LIMIT if limit else Outcome.COMPILER_ERROR,
                        -9 if limit else 1, 1, 100, 100, limit,
                        Diagnostic(b'', 0, False), Diagnostic(stderr, len(stderr), False))
                    context = SimpleNamespace(workspace=root, invocation_id=invocation.original_execution_id,
                        request_hash=invocation.request.request_hash, reader=artifacts, staging=artifacts,
                        check=lambda: None, run_process=lambda *a, **k: process)
                    result = checker.execute(invocation.request, context)
                    self.assertEqual(result.status, ResultStatus.FAILED)
                    report = load_record(artifacts.read_artifact(result.output, max_bytes=65536), limit=65536)
                    self.assertEqual(report['phases'][-1]['limit'], limit)
                    self.assertEqual(artifacts.read_artifact(result.diagnostics, max_bytes=65536), stderr)
                    for status, expected in ((result.status, failure), (ResultStatus.CANCELLED, Failure.CANCELLED)):
                        reported = replace(result, status=status)
                        receipt = ReceiptBinding(invocation.original_execution_id, invocation.request.request_ref,
                            artifacts.save(worker_payload(reported)), artifacts.save(b'seal'))
                        accepted = operation.admit(invocation, reported, receipt, artifacts,
                            SimpleNamespace(verify_receipt=lambda *a: True))
                        self.assertEqual(accepted.failure, expected)


if __name__ == '__main__':
    unittest.main()
