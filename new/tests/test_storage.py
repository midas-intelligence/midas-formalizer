"""Offline storage contracts and fault injection. No Lean or external providers."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from midas.domain import (AdmittedCheck, AdmittedReview, ArtifactRef, Candidate, CheckKind,
    DeliveryCertainty, Dependency, FrozenRequest, Job, JobKind, OwnerVersion, ProofNode,
    ResourceLimits, ResultStatus, Session, Termination, Usage, WorkerProgress, WorkerResult)
from midas.storage import (ArtifactError, Changes, Conflict, CounterChange, FrozenRun, Intent,
    NodeChange, RecordChange, RunLocked, StaleSession, StorageError, claim_writer, create, open_reader,
    worker_payload)
from midas.storage import artifacts

ENV = 'env_'+'a'*64


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'run'
        self.w = create(self.path)
        self.ref = self.w.put(b'frozen inputs', max_bytes=100)
        self.owner = OwnerVersion('obl_'+'b'*64, 0)
        self.node = ProofNode(self.owner, ENV, self.ref)
        with self.w.transaction(self.w.session) as tx:
            tx.freeze(FrozenRun(ENV, self.ref, self.ref))
            tx.apply('initial', Changes(nodes=(NodeChange(None, self.node),), roots=(self.owner.obligation_id,), graph_revision=0))

    def tearDown(self):
        if self.w._db is not None:
            self.w.close()
        self.temp.cleanup()

    def intent(self, key='job', *, kind=JobKind.MODEL, owner=None, depends_on=()):
        owner = owner or self.owner
        request = FrozenRequest(Job(key, kind, owner, (self.ref,), (owner,),
            ResourceLimits(1000, 128, 1_000_000, 1_000_000), depends_on), ENV, self.ref, self.ref)
        return Intent(request)

    def reserve(self, key='job', *, dispatch=True, bounds=Usage(), kind=JobKind.MODEL):
        with self.w.transaction(self.w.session) as tx:
            tx.apply('intent:'+key, Changes(intents=(self.intent(key, kind=kind),)))
            invocation = tx.reserve(key, self.ref, expected_revision=self.w.status_snapshot().revision, bounds=bounds)
            if dispatch:
                tx.mark_dispatched(invocation)
        return invocation

    def result(self, invocation, *, usage=Usage(10, 2, 5, 100), status=ResultStatus.SUCCEEDED,
               delivery=DeliveryCertainty.DELIVERED, termination=Termination.TERMINATED):
        result = WorkerResult(invocation.original_execution_id, invocation.request.request_hash, status,
            (self.ref,), self.ref, None, usage, delivery, termination)
        return result, self.w.seal_result(invocation, result)

    def receive(self, invocation, **kwargs):
        result, receipt = self.result(invocation, **kwargs)
        with self.w.transaction(self.w.session) as tx:
            tx.receive(invocation, result, receipt)
        return result, receipt

    def reopen(self):
        self.w.close()
        self.w = claim_writer(self.path)

    def archive_path(self, ref):
        segment = self.w._db.execute('SELECT segment FROM artifacts WHERE id=?', (ref.id,)).fetchone()[0]
        return self.path/'artifacts'/(segment+'.seg')

    def test_create_never_reuses_directory_and_writer_is_exclusive(self):
        with self.assertRaises(FileExistsError):
            create(self.path)
        with self.assertRaises(RunLocked):
            claim_writer(self.path)
        with open_reader(self.path) as reader:
            self.assertEqual(reader.frozen_run(), self.w.frozen_run())

    def test_frozen_inputs_immutable_and_no_environment_inventory(self):
        with self.w.transaction(self.w.session) as tx:
            self.assertFalse(tx.freeze(FrozenRun(ENV, self.ref, self.ref)))
            with self.assertRaises(Conflict):
                tx.freeze(FrozenRun('env_'+'c'*64, self.ref, self.ref))
        self.assertEqual(self.w.frozen_run().environment_id, ENV)

    def test_session_thread_process_and_transaction_lifetimes(self):
        with self.assertRaises(StaleSession), self.w.transaction(Session(self.w.run_id, str(uuid4()), 1)):
            pass
        with self.w.transaction(self.w.session) as tx:
            with self.assertRaises(StorageError), self.w.transaction(self.w.session):
                pass
            with self.assertRaises(StorageError):
                self.w.put(b'x', max_bytes=1)
        with self.assertRaises(StorageError):
            tx.expect((self.owner,))
        with ThreadPoolExecutor(1) as pool:
            with self.assertRaises(StorageError):
                pool.submit(self.w.status_snapshot).result()
        with patch('os.getpid', return_value=os.getpid()+1), self.assertRaises(StorageError):
            self.w.status_snapshot()

    def test_old_session_rejected_after_reopen(self):
        old = self.w.session
        self.reopen()
        self.assertEqual(self.w.session.epoch, old.epoch+1)
        with self.assertRaises(StaleSession), self.w.transaction(old):
            pass

    def test_unsupported_schema_rejected_without_new_session(self):
        self.w.close()
        with sqlite3.connect(self.path/'run.sqlite3') as db:
            epoch = db.execute('SELECT epoch FROM run').fetchone()
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(StorageError):
            claim_writer(self.path)
        with sqlite3.connect(self.path/'run.sqlite3') as db:
            self.assertEqual(db.execute('SELECT epoch FROM run').fetchone(), epoch)

    def test_symlinks_and_nonregular_archives_rejected(self):
        alias = Path(self.temp.name)/'alias'
        alias.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(StorageError):
            open_reader(alias)
        ref = self.w.put(b'x'*20000, max_bytes=20000)
        archive = self.archive_path(ref)
        archive.unlink()
        archive.symlink_to(self.path/'receipt.key')
        with self.assertRaises(OSError):
            self.w.read_artifact(ref, max_bytes=20000)

    def test_inline_and_large_streams_without_json(self):
        data = (b'proof\n'*1000 + os.urandom(1000))*800
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            ref = self.w.put(io.BytesIO(data), max_bytes=len(data))
            self.assertEqual(self.w.read_artifact(ref, max_bytes=len(data)), data)
            self.w.validate(force_artifacts=True)
        self.assertEqual(len(list((self.path/'artifacts').glob('*.seg'))), 1)
        self.assertEqual(self.w.put(data, max_bytes=len(data)), ref)

    def test_short_reads_exact_limit_empty_and_wrong_reference(self):
        class Short(io.BytesIO):
            def read(self, n):
                return super().read(min(n, 13))
        data = b'x'*18000
        ref = self.w.put(Short(data), max_bytes=len(data))
        with self.assertRaises(ArtifactError):
            self.w.put(Short(data), max_bytes=len(data)-1)
        with self.assertRaises(ArtifactError):
            self.w.read_artifact(ref, max_bytes=len(data)-1)
        with self.assertRaises(ArtifactError):
            self.w.read_artifact(replace(ref, sha256='f'*64), max_bytes=len(data))
        empty = self.w.put(b'', max_bytes=0)
        self.assertEqual(self.w.read_artifact(empty, max_bytes=0), b'')

    def test_inline_id_conflict_and_unpublished_capability(self):
        pending = self.w.publish(b'pending', max_bytes=7)
        with self.assertRaises(ArtifactError):
            self.w.read_artifact(pending.reference, max_bytes=7)
        with self.w.transaction(self.w.session) as tx:
            tx.register(pending)
        fake = replace(pending, authority=object())
        with self.w.transaction(self.w.session) as tx, self.assertRaises(Conflict):
            tx.register(fake)
        with self.assertRaises(ValueError):
            self.w.put(b'changed', max_bytes=7, reference=pending.reference)

    def test_artifact_reader_is_thread_safe_and_scoped(self):
        reader = self.w.artifact_reader((self.ref,))
        extra = self.w.put(b'other', max_bytes=5)
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(list(pool.map(lambda _: reader.read_artifact(self.ref, max_bytes=100), range(8))), [b'frozen inputs']*8)
        with self.assertRaises(ArtifactError):
            reader.read_artifact(extra, max_bytes=100)

    def test_staging_quotas_scope_thread_and_publication(self):
        staging = self.w.staging(max_bytes=21000, max_artifacts=3)
        with ThreadPoolExecutor(1) as pool:
            ref = pool.submit(staging.write_bounded, b'p'*20000, max_bytes=20000).result()
        with self.assertRaises(ArtifactError):
            staging.write_bounded(b'x', max_bytes=1)
        durable = self.w.publish_staged(staging, ref)
        with self.w.transaction(self.w.session) as tx:
            tx.register(durable)
        staging.close()
        self.assertEqual(self.w.read_artifact(ref, max_bytes=20000), b'p'*20000)
        s = self.w.staging(max_bytes=3, max_artifacts=1)
        with self.assertRaises(ArtifactError):
            s.write_bounded(b'four', max_bytes=4)
        s.write_bounded(b'abc', max_bytes=3)
        with self.assertRaises(ArtifactError):
            s.write_bounded(b'', max_bytes=0)
        s.close()

    def test_archive_corruption_truncation_and_tail_rejected(self):
        ref = self.w.put(b'q'*30000, max_bytes=30000)
        path = self.archive_path(ref)
        original = path.read_bytes()
        for corrupt in (original[:-1], b'badmagic'+original[8:]):
            path.write_bytes(corrupt)
            with self.assertRaises(ArtifactError):
                self.w.read_artifact(ref, max_bytes=30000)
            with self.assertRaises(StorageError):
                self.w.validate()
        path.write_bytes(original)
        self.w.validate()
        path.write_bytes(original+b'uncommitted tail')
        self.assertEqual(self.w.read_artifact(ref, max_bytes=30000), b'q'*30000)
        self.reopen()
        self.assertEqual(path.read_bytes(), original)

    def test_orphan_recovery_and_failure_before_reference_commit(self):
        orphan = self.w.publish(b'o'*20000, max_bytes=20000)
        scope = self.w.staging(max_bytes=20000)
        scope.write_bounded(b's'*20000, max_bytes=20000)
        self.reopen()
        self.assertFalse(tuple((self.path/'artifacts').iterdir()))
        self.assertFalse(tuple((self.path/'staging').iterdir()))
        self.assertEqual(self.w.read_artifact(self.ref, max_bytes=100), b'frozen inputs')

    def test_faults_at_publication_and_sql_boundaries(self):
        for stage in ('file_synced', 'directory_synced'):
            def fail(current):
                if current == stage:
                    raise OSError('injected crash')
            with patch.object(artifacts, 'checkpoint', side_effect=fail), self.assertRaises(OSError):
                self.w.put(os.urandom(18000), max_bytes=18000)
            self.reopen()
            self.assertEqual(self.w.artifacts(), (self.ref,))
        pending = self.w.publish(b'pending', max_bytes=7)
        with patch.object(artifacts, 'checkpoint', side_effect=lambda s: (_ for _ in ()).throw(OSError()) if s == 'before_commit' else None):
            with self.assertRaises(OSError), self.w.transaction(self.w.session) as tx:
                tx.register(pending)
        with self.assertRaises(KeyError):
            self.w.artifact(pending.reference.id)

    def test_commit_uncertainty_is_idempotent(self):
        changes = Changes(counters=(CounterChange('run', 'turns', 0, 1),))
        with patch.object(artifacts, 'checkpoint', side_effect=lambda s: (_ for _ in ()).throw(OSError()) if s == 'after_commit' else None):
            with self.assertRaises(OSError), self.w.transaction(self.w.session) as tx:
                tx.apply('once', changes)
        with self.w.transaction(self.w.session) as tx:
            self.assertFalse(tx.apply('once', changes))
        self.assertEqual(self.w.counter('run', 'turns'), 1)

    def test_atomic_effects_and_conflicting_replay(self):
        changes = Changes(expected=(self.owner,), records=(RecordChange('memory', self.owner.obligation_id, None, self.ref),),
            counters=(CounterChange('owner', 'attempts', 0, 1),), intents=(self.intent(),))
        with self.w.transaction(self.w.session) as tx:
            self.assertTrue(tx.apply('change', changes))
            self.assertFalse(tx.apply('change', changes))
            with self.assertRaises(Conflict):
                tx.apply('change', replace(changes, counters=()))
            with self.assertRaises(Conflict):
                tx.apply('bad', Changes(records=(RecordChange('other', 'first', None, self.ref),),
                    counters=(CounterChange('owner', 'attempts', 0, 2),)))
        self.assertIsNone(self.w.record('other', 'first'))
        self.assertEqual(self.w.counter('owner', 'attempts'), 1)
        self.assertEqual(self.w.ready(0), (self.intent(),))

    def test_records_retain_history_and_indexed_pages(self):
        with self.w.transaction(self.w.session) as tx:
            tx.apply('a', Changes(records=tuple(RecordChange('evidence', str(i), None, self.ref) for i in range(5))))
            tx.apply('b', Changes(records=(RecordChange('evidence', '0', 0, None),)))
        self.assertIsNone(self.w.record('evidence', '0').artifact)
        self.assertEqual(self.w.record('evidence', '0', revision=0).artifact, self.ref)
        self.assertEqual([r.key for r in self.w.records('evidence', after='1', limit=2)], ['2', '3'])
        with self.assertRaises(ValueError):
            self.w.nodes(limit=1001)

    def test_consistent_read_snapshot_and_checkpoint_copy(self):
        with open_reader(self.path) as reader, reader.snapshot():
            before = reader.status_snapshot()
            with self.w.transaction(self.w.session) as tx:
                tx.apply('new', Changes(counters=(CounterChange('run', 'count', 0, 1),)))
            self.assertEqual(reader.status_snapshot(), before)
            self.assertEqual(reader.counter('run', 'count'), 0)
            with create(Path(self.temp.name)/'child') as child:
                child.copy_artifact(reader, self.ref)
                with child.transaction(child.session) as tx:
                    tx.freeze(FrozenRun(ENV, self.ref, self.ref, reader.run_id, before.revision))
                self.assertEqual(child.frozen_run().parent_run_id, self.w.run_id)
        with open_reader(self.path) as reader:
            self.assertEqual(reader.counter('run', 'count'), 1)

    def test_ready_dependencies_cycle_and_semantic_key_conflicts(self):
        a, b = self.intent('a'), self.intent('b', depends_on=('a',))
        with self.w.transaction(self.w.session) as tx:
            tx.apply('jobs', Changes(intents=(b, a)))
            with self.assertRaises(Conflict):
                tx.apply('conflict', Changes(intents=(replace(a, priority=4),)))
            with self.assertRaises(Conflict):
                tx.reserve('b', self.ref, expected_revision=self.w.status_snapshot().revision)
        self.assertEqual([i.request.job.semantic_key for i in self.w.ready(0)], ['a'])
        with self.w.transaction(self.w.session) as tx, self.assertRaises(Conflict):
            tx.apply('cycle', Changes(intents=(self.intent('c', depends_on=('d',)), self.intent('d', depends_on=('c',)))))
        with self.assertRaises(KeyError):
            self.w.intent('c')

    def test_admission_revision_and_not_before(self):
        before = self.w.status_snapshot().revision
        with self.w.transaction(self.w.session) as tx:
            tx.apply('job', Changes(intents=(replace(self.intent(), not_before_ms=50),)))
        with self.w.transaction(self.w.session) as tx:
            with self.assertRaises(Conflict):
                tx.reserve('job', self.ref, expected_revision=before, now_ms=50)
            with self.assertRaises(Conflict):
                tx.reserve('job', self.ref, expected_revision=self.w.status_snapshot().revision)
        self.assertFalse(self.w.ready(49))
        self.assertEqual(len(self.w.ready(50)), 1)

    def test_prepared_release_and_recovery_charges(self):
        self.reserve('prepared', dispatch=False, bounds=Usage(20, 5, 10, 100))
        sent = self.reserve('sent', bounds=Usage(20, 5, 10, 100))
        self.reopen()
        with self.w.transaction(self.w.session) as tx:
            self.assertEqual(tx.reconcile(limit=1), 1)
            self.assertEqual(tx.reconcile(limit=1), 1)
            self.assertEqual(tx.reconcile(limit=1), 0)
        a = self.w.status_snapshot().accounting
        self.assertEqual((a.turns, a.models, a.reserved.input_tokens), (1, 1, 20))
        self.assertEqual(self.w.executions('UNKNOWN'), (sent.original_execution_id,))
        self.assertEqual([i.request.job.semantic_key for i in self.w.ready(0)], ['prepared'])
        self.w.validate()

    def test_usage_monotonic_unknown_partial_duplicate_and_overflow(self):
        inv = self.reserve()
        with self.w.transaction(self.w.session) as tx:
            self.assertTrue(tx.report_usage(inv, 'first', Usage(10)))
            self.assertFalse(tx.report_usage(inv, 'first', Usage(10)))
            with self.assertRaises(Conflict):
                tx.report_usage(inv, 'first', Usage(11))
            with self.assertRaises(Conflict):
                tx.report_usage(inv, 'decrease', Usage(9))
            tx.report_usage(inv, 'next', Usage(None, 2, 5, 20))
        a = self.w.status_snapshot().accounting
        self.assertEqual(a.actual, Usage(10, 2, 5, 20))
        second = self.reserve('second')
        with self.w.transaction(self.w.session) as tx, self.assertRaises(sqlite3.IntegrityError):
            tx.report_usage(second, 'overflow', Usage(2**63-1))
        self.assertEqual(self.w.status_snapshot().accounting.known.input_tokens, 10)
        self.w.validate()

    def test_provider_request_identity_is_unique_and_retained(self):
        a, b = self.reserve('a'), self.reserve('b')
        with self.w.transaction(self.w.session) as tx:
            tx.bind_provider(a, 'provider', 'request')
            with self.assertRaises(sqlite3.IntegrityError):
                tx.bind_provider(b, 'provider', 'request')
            with self.assertRaises(Conflict):
                tx.bind_provider(a, 'provider', 'other')

    def test_receipt_authentication_and_separate_application(self):
        inv = self.reserve()
        result, receipt = self.result(inv)
        self.assertFalse(self.w.verify_receipt(receipt, inv, result))
        with self.w.transaction(self.w.session) as tx:
            self.assertTrue(tx.receive(inv, result, receipt))
            self.assertFalse(tx.receive(inv, result, receipt))
        self.assertEqual(self.w.node(self.owner.obligation_id).node.state.value, 'UNRESOLVED')
        self.assertTrue(self.w.verify_receipt(receipt, inv, result))
        self.assertFalse(self.w.verify_receipt(receipt, inv, replace(result, usage=Usage(20))))
        with self.w.transaction(self.w.session) as tx:
            self.assertTrue(tx.apply('accept', Changes(), receipt=receipt))
            self.assertFalse(tx.apply('accept', Changes(), receipt=receipt))
            with self.assertRaises(Conflict):
                tx.apply('accept-again', Changes(), receipt=receipt)
        self.w.validate()

    def test_not_sent_receipt_releases_turn_but_unknown_does_not(self):
        a, b = self.reserve('a'), self.reserve('b')
        self.receive(a, usage=Usage(), status=ResultStatus.FAILED, delivery=DeliveryCertainty.NOT_SENT)
        self.receive(b, usage=Usage(), status=ResultStatus.FAILED, delivery=DeliveryCertainty.UNKNOWN, termination=Termination.UNKNOWN)
        usage = self.w.status_snapshot().accounting
        self.assertEqual((usage.turns, usage.models, usage.unknown.input_tokens), (1, 1, 1))
        self.w.validate()

    def test_contradictory_not_sent_usage_cannot_refund_turn(self):
        inv = self.reserve()
        self.receive(inv, usage=Usage(5), status=ResultStatus.FAILED, delivery=DeliveryCertainty.NOT_SENT)
        self.assertEqual(self.w.status_snapshot().accounting.turns, 1)

    def test_explicit_adoption_keeps_original_invocation_and_usage(self):
        inv = self.reserve()
        result, receipt = self.receive(inv)
        self.reopen()
        with self.w.transaction(self.w.session) as tx:
            with self.assertRaises(StaleSession):
                tx.apply('accept', Changes(), receipt=receipt)
            self.assertEqual(tx.adopt_receipt(inv.original_execution_id), inv)
            tx.apply('accept', Changes(), receipt=receipt)
        self.assertEqual(self.w.status_snapshot().accounting.turns, 1)
        self.assertTrue(self.w.verify_receipt(receipt, inv, result))

    def test_explicit_retry_retains_original_charge_and_late_usage(self):
        inv = self.reserve()
        self.reopen()
        with self.w.transaction(self.w.session) as tx:
            tx.reconcile()
            with self.assertRaises(Conflict):
                tx.retry(inv.original_execution_id, not_before_ms=0)
            tx.release_capacity(inv, Termination.TERMINATED)
            tx.retry(inv.original_execution_id, not_before_ms=10)
            again = tx.reserve('job', self.ref, expected_revision=self.w.status_snapshot().revision, now_ms=10)
            tx.mark_dispatched(again)
            tx.report_usage(inv, 'late', Usage(50, 1, 10, 100))
        _, receipt = self.receive(inv, usage=Usage(50, 1, 10, 100))
        with self.w.transaction(self.w.session) as tx, self.assertRaises(Conflict):
            tx.adopt_receipt(inv.original_execution_id)
        self.assertEqual(self.w.status_snapshot().accounting.turns, 2)
        self.assertEqual(self.w.status_snapshot().accounting.known.input_tokens, 50)

    def test_progress_exact_replay_and_case_pages(self):
        inv = self.reserve(kind=JobKind.PROBE)
        message = WorkerProgress(inv.original_execution_id, 1, 'probe_completed:case', (self.ref,))
        with self.w.transaction(self.w.session) as tx:
            self.assertTrue(tx.record_progress(inv, message))
            self.assertFalse(tx.record_progress(inv, message))
            with self.assertRaises(Conflict):
                tx.record_progress(inv, replace(message, kind='other'))
        self.assertEqual(self.w.progress(inv.original_execution_id), (message,))
        self.assertEqual(self.w.progress(inv.original_execution_id, after=1), ())

    def test_generation_retirement_fences_pending_work_and_late_proof(self):
        prepared, sent = self.reserve('prepared', dispatch=False), self.reserve('sent')
        version = self.w.node(self.owner.obligation_id).version
        with self.w.transaction(self.w.session) as tx:
            tx.apply('retire', Changes(nodes=(NodeChange(version, replace(self.node, owner=replace(self.owner, generation=1))),), graph_revision=1))
        self.assertEqual(self.w.status_snapshot().accounting.turns, 1)
        self.assertFalse(self.w.ready(0))
        self.assertEqual(self.w.executions('NOT_SENT'), (prepared.original_execution_id,))
        _, receipt = self.receive(sent)
        with self.w.transaction(self.w.session) as tx, self.assertRaises(Conflict):
            tx.apply('late', Changes(), receipt=receipt)
        self.w.validate()

    def test_proof_node_roundtrip_and_dependency_binding(self):
        inv = self.reserve()
        _, receipt = self.receive(inv)
        candidate = Candidate(str(uuid4()), self.owner, ENV, self.ref)
        checked = AdmittedCheck(candidate, CheckKind.DIRECT, receipt, self.ref)
        node = replace(self.node, selected=checked)
        version = self.w.node(self.owner.obligation_id).version
        with self.w.transaction(self.w.session) as tx:
            tx.apply('proof', Changes(nodes=(NodeChange(version, node),), graph_revision=1), receipt=receipt)
        self.assertEqual(self.w.node(self.owner.obligation_id).node, node)
        self.assertEqual(self.w.status_snapshot().nodes, (('VERIFIED', 1),))
        self.reopen()
        self.assertEqual(self.w.node(self.owner.obligation_id).node, node)

    def test_graph_cycle_and_child_retirement_rejected_atomically(self):
        inv = self.reserve()
        _, receipt = self.receive(inv)
        child_owner = OwnerVersion('obl_'+'c'*64, 0)
        child = ProofNode(child_owner, ENV, self.ref)
        def conditional(node, dependency):
            candidate = Candidate(str(uuid4()), node.owner, ENV, self.ref, (Dependency(dependency.owner, 'lemma', self.ref),))
            return replace(node, selected=AdmittedCheck(candidate, CheckKind.CONDITIONAL, receipt, self.ref),
                           review=AdmittedReview(candidate, receipt, True))
        parent = conditional(self.node, child)
        with self.w.transaction(self.w.session) as tx:
            tx.apply('graph', Changes(nodes=(NodeChange(None, child), NodeChange(self.w.node(self.owner.obligation_id).version, parent)), graph_revision=1))
        self.assertEqual(self.w.consumers(child_owner.obligation_id), (self.owner.obligation_id,))
        version = self.w.node(child_owner.obligation_id).version
        with self.w.transaction(self.w.session) as tx:
            with self.assertRaises(Conflict):
                tx.apply('cycle', Changes(nodes=(NodeChange(version, conditional(child, parent)),), graph_revision=2))
            with self.assertRaises(Conflict):
                tx.apply('stale-child', Changes(nodes=(NodeChange(version, replace(child, owner=replace(child_owner, generation=1))),), graph_revision=2))
        self.assertEqual(self.w.node(child_owner.obligation_id).node, child)

    def test_worker_formats_match_existing_services(self):
        from midas.checker import worker_payload as checker_payload
        from midas.provider import worker_payload as provider_payload
        from midas.tools.search import worker_payload as search_payload
        inv = self.reserve()
        result, _ = self.result(inv)
        self.assertEqual(worker_payload(result), checker_payload(result))
        self.assertEqual(worker_payload(result), provider_payload(result))
        self.assertEqual(worker_payload(result), search_payload(result))

    def test_accounting_corruption_is_detected_before_recovery(self):
        self.reserve()
        self.w.close()
        with sqlite3.connect(self.path/'run.sqlite3') as db:
            db.execute('UPDATE accounting SET turns=2')
        with self.assertRaises(StorageError):
            claim_writer(self.path)

    def test_recovery_views_reconstruct_original_results(self):
        inv = self.reserve()
        result, receipt = self.receive(inv)
        with self.w.transaction(self.w.session) as tx:
            tx.bind_provider(inv, 'offline', 'req-1')
        self.reopen()
        self.assertEqual(self.w.result(inv.original_execution_id), result)
        self.assertEqual(self.w.pending_receipts(), (inv.original_execution_id,))
        state = self.w.execution(inv.original_execution_id)
        self.assertEqual((state.invocation, state.receipt, state.usage, state.held), (inv, receipt, result.usage, False))
        self.assertEqual(self.w.provider_invocation('offline', 'req-1'), inv)
        self.assertEqual(self.w.node_history(self.owner.obligation_id)[0].node, self.node)

    def test_large_receipt_metadata_needs_no_file_io_inside_receive(self):
        inv = self.reserve()
        durable = tuple(self.w.publish(str(i).encode(), max_bytes=4) for i in range(110))
        with self.w.transaction(self.w.session) as tx:
            refs = tuple(tx.register(d) for d in durable)
        result = WorkerResult(inv.original_execution_id, inv.request.request_hash, ResultStatus.SUCCEEDED,
            refs, refs[0], None, Usage(1, 0, 1, 1), DeliveryCertainty.DELIVERED, Termination.TERMINATED)
        receipt = self.w.seal_result(inv, result)
        self.assertGreater(receipt.result_ref.byte_count, 16384)
        with patch('midas.storage.queries.stream_file', side_effect=AssertionError('File I/O in transaction')):
            with self.w.transaction(self.w.session) as tx:
                tx.receive(inv, result, receipt)
        self.assertTrue(self.w.verify_receipt(receipt, inv, result))

    def test_staging_mutation_cannot_change_published_bytes(self):
        staging = self.w.staging(max_bytes=50000)
        ref = staging.write_bounded(b'a'*20000, max_bytes=20000)
        durable = self.w.publish_staged(staging, ref)
        with self.w.transaction(self.w.session) as tx:
            tx.register(durable)
        (staging.directory/(ref.id+'.raw')).write_bytes(b'b'*20000)
        self.assertEqual(self.w.read_artifact(ref, max_bytes=20000), b'a'*20000)
        with self.assertRaises(ArtifactError):
            self.w.publish_staged(staging, ref)
        staging.close()

    def test_truncated_segment_cannot_be_used_for_more_publication(self):
        ref = self.w.put(b'a'*20000, max_bytes=20000)
        self.archive_path(ref).write_bytes(b'')
        with self.assertRaises(ArtifactError):
            self.w.put(b'b'*20000, max_bytes=20000)

    def test_node_projection_corruption_detected_on_open(self):
        self.w.close()
        with sqlite3.connect(self.path/'run.sqlite3') as db:
            db.execute("UPDATE nodes SET state='VERIFIED'")
        with self.assertRaisesRegex(StorageError, 'projection'):
            claim_writer(self.path)

    def test_dispatch_requires_capacity_and_prepared_release_is_explicit(self):
        inv = self.reserve(dispatch=False)
        with self.w.transaction(self.w.session) as tx:
            with self.assertRaises(Conflict):
                tx.release_capacity(inv, Termination.TERMINATED)
            tx.mark_dispatched(inv)
            tx.release_capacity(inv, Termination.TERMINATED)
            with self.assertRaises(Conflict):
                tx.mark_dispatched(inv)

    def test_status_cache_observes_external_commits_and_pinned_snapshots(self):
        with open_reader(self.path) as reader:
            first = reader.status_snapshot()
            self.assertEqual(reader.status_snapshot(), first)
            with self.w.transaction(self.w.session) as tx:
                tx.apply('advance', Changes(counters=(CounterChange('run', 'count', 0, 1),)))
            self.assertGreater(reader.status_snapshot().revision, first.revision)
            with reader.snapshot():
                pinned = reader.status_snapshot()
                with self.w.transaction(self.w.session) as tx:
                    tx.apply('advance-again', Changes(counters=(CounterChange('run', 'count', 1, 2),)))
                self.assertEqual(reader.status_snapshot(), pinned)
            self.assertGreater(reader.status_snapshot().revision, pinned.revision)

    def test_not_sent_without_termination_keeps_reservation(self):
        inv = self.reserve()
        self.receive(inv, status=ResultStatus.FAILED, delivery=DeliveryCertainty.NOT_SENT,
                     termination=Termination.UNKNOWN, usage=Usage())
        a = self.w.status_snapshot().accounting
        self.assertEqual((a.turns, a.models, a.unknown.input_tokens), (1, 1, 1))

    def test_resource_projection_matches_reserved_worker_kinds(self):
        for kind in JobKind:
            self.reserve(kind.value, kind=kind)
        a = self.w.status_snapshot().accounting
        self.assertEqual((a.models, a.locals, a.compilers, a.compiler_memory_mib), (1, 1, len(JobKind)-2, 128*(len(JobKind)-2)))
        self.w.validate()

    def test_deep_graph_uses_flat_rows_and_bounded_pages(self):
        inv = self.reserve()
        _, receipt = self.receive(inv)
        child = self.node
        changes = []
        for index in range(1100):
            owner = OwnerVersion('obl_'+sha256(str(index).encode()).hexdigest(), 0)
            candidate = Candidate(str(uuid4()), owner, ENV, self.ref, (Dependency(child.owner, 'child', self.ref),))
            child = ProofNode(owner, ENV, self.ref,
                selected=AdmittedCheck(candidate, CheckKind.CONDITIONAL, receipt, self.ref),
                review=AdmittedReview(candidate, receipt, True))
            changes.append(NodeChange(None, child))
            if len(changes) == 100 or index == 1099:
                with self.w.transaction(self.w.session) as tx:
                    tx.apply('batch:'+str(index), Changes(nodes=tuple(changes), graph_revision=self.w.status_snapshot().graph_revision))
                changes.clear()
        self.w.validate()
        first = self.w.nodes(limit=1000)
        second = self.w.nodes(after=first[-1].version.owner.obligation_id, limit=1000)
        self.assertEqual((len(first), len(second)), (1000, 101))


if __name__ == '__main__':
    unittest.main()
