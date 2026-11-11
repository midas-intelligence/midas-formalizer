"""Original invocations, cumulative usage, receipts and explicit recovery actions."""
from uuid import uuid4

from ..domain import (DeliveryCertainty, Invocation, JobKind, Termination, Usage, WorkerProgress)
from ..domain.records import instance, natural
from . import codec
from .protocol import Conflict, StaleSession, USAGE_FIELDS, name, page_limit
from .transaction import StateTransaction, atomic


def resource(kind):
    return 'NETWORK' if kind == JobKind.NETWORK_SEARCH else 'MODEL' if kind == JobKind.MODEL else 'LOCAL' if kind == JobKind.SEARCH else 'COMPILER'


class Transaction(StateTransaction):
    def _execution(self, invocation):
        if self._writer.invocation(invocation.original_execution_id) != invocation:
            raise Conflict('Invocation differs from its immutable request/session')
        return self._db.execute('''SELECT intent,phase,charged,held,resource,memory,bounds,session,adopted_session
            FROM executions WHERE id=?''', (invocation.original_execution_id,)).fetchone()

    def _current(self, invocation, *, adopted=False):
        row = self._execution(invocation)
        session = self._writer.session
        if row[7] != session.session_id and not (adopted and row[8] == session.session_id):
            raise StaleSession('Invocation belongs to an obsolete session; explicitly adopt its saved receipt')
        current = self._db.execute('SELECT current_execution,state FROM intents WHERE key=?', (row[0],)).fetchone()
        if current != (invocation.original_execution_id, 'RUNNING'):
            raise Conflict('Invocation was superseded or its intent retired')
        self.expect(invocation.request.job.expected_versions)
        return row

    @atomic
    def reserve(self, key, reservation, *, expected_revision, bounds=Usage(), now_ms=0, execution_id=None):
        """Caller evaluates policy against this transaction's current read view.

        The revision prevents reusing an admission decision from another commit.
        Reservation bytes retain the policy decision; storage owns no limits.
        """
        natural(expected_revision, 'Admission revision')
        natural(now_ms, 'Clock')
        instance(bounds, Usage)
        if expected_revision != self._db.execute('SELECT revision FROM run').fetchone()[0]:
            raise Conflict('Admission snapshot changed')
        intent = self._writer.intent(key)
        request, job = intent.request, intent.request.job
        self.expect(job.expected_versions)
        if self._db.execute('SELECT state,not_before FROM intents WHERE key=?', (key,)).fetchone()[0] != 'PENDING' or intent.not_before_ms > now_ms:
            raise Conflict('Intent is not ready')
        if self._db.execute('''SELECT 1 FROM intent_dependencies d LEFT JOIN intents p ON p.key=d.dependency
            WHERE d.intent=? AND (p.state IS NULL OR p.state!='DONE') LIMIT 1''', (key,)).fetchone():
            raise Conflict('Intent prerequisites are incomplete')
        invocation = Invocation(execution_id or str(uuid4()), self._writer.session, request, reservation)
        ref = self._ref(reservation)
        kind = resource(job.kind)
        charged = int(kind == 'MODEL')
        amounts = [getattr(bounds, k) for k in USAGE_FIELDS]
        self._db.execute("INSERT INTO executions VALUES (?,?,?,NULL,?,?,'PREPARED',?,1,?,?,NULL,NULL)",
            (invocation.original_execution_id, key, invocation.session.session_id, ref, codec.pack(amounts), charged, kind, job.limits.memory_mib))
        self._db.execute("UPDATE intents SET state='RUNNING',current_execution=? WHERE key=?", (invocation.original_execution_id, key))
        self._db.execute('INSERT INTO execution_usage VALUES (?,NULL,NULL,NULL,NULL)', (invocation.original_execution_id,))
        self._db.execute('UPDATE accounting SET turns=turns+?', (charged,))
        for field, amount in zip(USAGE_FIELDS, amounts):
            self._db.execute(f'UPDATE accounting SET {field}_unknown={field}_unknown+?,{field}_reserved={field}_reserved+?', (charged, amount or 0))
        column = {'MODEL': 'models', 'COMPILER': 'compilers', 'LOCAL': 'locals', 'NETWORK': 'networks'}[kind]
        self._db.execute(f'UPDATE accounting SET {column}={column}+1,compiler_memory=compiler_memory+?',
                         (job.limits.memory_mib if kind == 'COMPILER' else 0,))
        return invocation

    def _provider(self, execution_id, provider, request_id):
        if provider is None and request_id is None:
            return
        name(provider)
        if request_id is not None:
            name(request_id)
        old = self._db.execute('SELECT provider,provider_request FROM executions WHERE id=?', (execution_id,)).fetchone()
        if old[0] not in (None, provider) or request_id is not None and old[1] not in (None, request_id):
            raise Conflict('Provider request identity changed')
        self._db.execute('UPDATE executions SET provider=coalesce(provider,?),provider_request=coalesce(provider_request,?) WHERE id=?',
                         (provider, request_id, execution_id))

    @atomic
    def bind_provider(self, invocation, provider, request_id):
        row = self._execution(invocation)
        if row[1] not in ('DISPATCHED', 'UNKNOWN', 'RECEIVED'):
            raise Conflict('Provider identity requires a dispatched invocation')
        self._provider(invocation.original_execution_id, provider, request_id)

    @atomic
    def mark_dispatched(self, invocation, *, provider=None, request_id=None):
        row = self._current(invocation)
        if row[1] not in ('PREPARED', 'DISPATCHED') or not row[3]:
            raise Conflict('Invocation cannot be dispatched')
        self._provider(invocation.original_execution_id, provider, request_id)
        self._db.execute("UPDATE executions SET phase='DISPATCHED' WHERE id=?", (invocation.original_execution_id,))

    def _release(self, execution_id):
        held, kind, memory, bounds = self._db.execute('SELECT held,resource,memory,bounds FROM executions WHERE id=?', (execution_id,)).fetchone()
        if not held:
            return False
        column = {'MODEL': 'models', 'COMPILER': 'compilers', 'LOCAL': 'locals', 'NETWORK': 'networks'}[kind]
        self._db.execute(f'UPDATE accounting SET {column}={column}-1,compiler_memory=compiler_memory-?', (memory if kind == 'COMPILER' else 0,))
        for field, amount in zip(USAGE_FIELDS, codec.unpack(bounds)):
            self._db.execute(f'UPDATE accounting SET {field}_reserved={field}_reserved-?', (amount or 0,))
        self._db.execute('UPDATE executions SET held=0 WHERE id=?', (execution_id,))
        return True

    @atomic
    def release_capacity(self, invocation, termination):
        row = self._execution(invocation)
        if row[1] == 'PREPARED':
            raise Conflict('Release undispatched reservations with not_sent')
        if termination != Termination.TERMINATED:
            raise Conflict('Capacity release requires observed termination')
        return self._release(invocation.original_execution_id)

    def _uncharge(self, execution_id):
        charged = self._db.execute('SELECT charged FROM executions WHERE id=?', (execution_id,)).fetchone()[0]
        usage = self._db.execute('SELECT input_tokens,cache_tokens,output_tokens,cost_microusd FROM execution_usage WHERE execution=?', (execution_id,)).fetchone()
        if not charged or any(v not in (None, 0) for v in usage):
            return False
        self._db.execute('UPDATE accounting SET turns=turns-1')
        for field, value in zip(USAGE_FIELDS, usage):
            self._db.execute(f'UPDATE accounting SET {field}_unknown={field}_unknown-?', (int(value is None),))
        self._db.execute('UPDATE executions SET charged=0 WHERE id=?', (execution_id,))
        return True

    def _not_sent(self, execution_id):
        self._uncharge(execution_id)
        self._release(execution_id)
        self._db.execute("UPDATE executions SET phase='NOT_SENT' WHERE id=?", (execution_id,))

    @atomic
    def not_sent(self, invocation):
        row = self._current(invocation)
        if row[1] != 'PREPARED':
            raise Conflict('Only undispatched work can be released without a receipt')
        self._not_sent(invocation.original_execution_id)
        self._db.execute("UPDATE intents SET state='PENDING',current_execution=NULL WHERE key=?", (row[0],))

    @atomic
    def report_usage(self, invocation, report_id, usage):
        """Cumulative reports attach only to the original call, including late calls."""
        name(report_id)
        instance(usage, Usage)
        row = self._execution(invocation)
        if row[1] not in ('DISPATCHED', 'UNKNOWN', 'RECEIVED') or not row[2]:
            raise Conflict('Usage requires an original charged model invocation')
        values = [getattr(usage, k) for k in USAGE_FIELDS]
        payload = codec.pack(values)
        key = invocation.original_execution_id
        old_report = self._db.execute('SELECT payload FROM usage_reports WHERE execution=? AND key=?', (key, report_id)).fetchone()
        if old_report:
            if old_report[0] != payload:
                raise Conflict('Conflicting duplicate usage report')
            return False
        before = self._db.execute('SELECT input_tokens,cache_tokens,output_tokens,cost_microusd FROM execution_usage WHERE execution=?', (key,)).fetchone()
        after = [old if new is None else new for old, new in zip(before, values)]
        for field, old, new in zip(USAGE_FIELDS, before, after):
            if old is not None and new < old:
                raise Conflict('Cumulative usage cannot decrease')
            if new is not None:
                self._db.execute(f'UPDATE accounting SET {field}={field}+?,{field}_unknown={field}_unknown-?',
                                 (new-(old or 0), int(old is None)))
        self._db.execute('INSERT INTO usage_reports VALUES (?,?,?)', (key, report_id, payload))
        self._db.execute('UPDATE execution_usage SET input_tokens=?,cache_tokens=?,output_tokens=?,cost_microusd=? WHERE execution=?', (*after, key))
        return True

    @atomic
    def receive(self, invocation, result, receipt):
        """Retain an authenticated result without changing proof state."""
        row = self._execution(invocation)
        result.validate_for(invocation)
        if row[1] not in ('DISPATCHED', 'UNKNOWN', 'RECEIVED'):
            raise Conflict('Receipt requires an original dispatched invocation')
        self._writer._verify_seal(receipt, invocation, result)
        previous = self._writer.receipt(invocation.original_execution_id)
        if previous is not None:
            if previous != receipt:
                raise Conflict('Conflicting receipt replay')
            return False
        refs = (receipt.request_ref, receipt.result_ref, receipt.seal_ref, *result.staged_artifacts)
        for ref in refs:
            self._ref(ref)
        key = invocation.original_execution_id
        self._db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,?)',
            (key, receipt.request_ref.id, receipt.result_ref.id, receipt.seal_ref.id,
             result.status.value, result.delivery_certainty.value, result.termination.value))
        self._db.executemany('INSERT INTO receipt_artifacts VALUES (?,?)', ((key, r.id) for r in set(refs)))
        self._db.execute("UPDATE executions SET phase='RECEIVED' WHERE id=?", (key,))
        if row[2]:
            self.report_usage(invocation, 'receipt', result.usage)
        elif any(getattr(result.usage, k) not in (None, 0) for k in USAGE_FIELDS):
            raise Conflict('Non-model work reported billable usage')
        if result.delivery_certainty == DeliveryCertainty.NOT_SENT and result.termination == Termination.TERMINATED:
            self._uncharge(key)
        if result.termination == Termination.TERMINATED:
            self._release(key)
        return True

    @atomic
    def record_progress(self, invocation, message):
        instance(message, WorkerProgress)
        row = self._execution(invocation)
        if message.invocation_id != invocation.original_execution_id or row[1] not in ('DISPATCHED', 'UNKNOWN', 'RECEIVED'):
            raise Conflict('Progress differs from a dispatched invocation')
        payload = codec.pack([codec.ref(r) for r in message.artifacts])
        key = (message.invocation_id, message.sequence)
        prior = self._db.execute('SELECT kind,payload FROM progress WHERE execution=? AND sequence=?', key).fetchone()
        if prior is not None:
            if prior != (message.kind, payload):
                raise Conflict('Conflicting progress replay')
            return False
        self._db.execute('INSERT INTO progress VALUES (?,?,?,?)', (*key, message.kind, payload))
        self._db.executemany('INSERT INTO progress_artifacts VALUES (?,?,?)',
                            ((*key, self._ref(r)) for r in set(message.artifacts)))
        return True

    def _accepting(self, receipt):
        invocation = self._writer.invocation(receipt.original_execution_id)
        row = self._current(invocation, adopted=True)
        if row[1] != 'RECEIVED' or self._writer.receipt(receipt.original_execution_id) != receipt:
            raise Conflict('No matching durable receipt to apply')
        return invocation

    def _finish(self, receipt, application, disposition):
        key = receipt.original_execution_id
        intent = self._db.execute('SELECT intent FROM executions WHERE id=?', (key,)).fetchone()[0]
        self._db.execute('INSERT INTO dispositions VALUES (?,?,?)', (key, application, disposition))
        self._db.execute("UPDATE intents SET state='DONE' WHERE key=?", (intent,))

    @atomic
    def adopt_receipt(self, execution_id):
        """Adopt application authority, preserving original invocation and accounting."""
        invocation = self._writer.invocation(execution_id)
        self._execution(invocation)
        self.expect(invocation.request.job.expected_versions)
        if self._writer.receipt(execution_id) is None:
            raise Conflict('Only saved receipts can be adopted')
        current = self._db.execute('SELECT current_execution,state FROM intents WHERE key=?', (invocation.request.job.semantic_key,)).fetchone()
        if current != (execution_id, 'RUNNING'):
            raise Conflict('Receipt has been superseded or applied')
        self._db.execute('UPDATE executions SET adopted_session=? WHERE id=?', (self._writer.session.session_id, execution_id))
        return invocation

    @atomic
    def retry(self, execution_id, *, not_before_ms):
        """Apply the caller's recovery/retry decision; no automatic free retries."""
        natural(not_before_ms, 'Retry time')
        invocation = self._writer.invocation(execution_id)
        row = self._execution(invocation)
        self.expect(invocation.request.job.expected_versions)
        current = self._db.execute('SELECT current_execution,state FROM intents WHERE key=?', (row[0],)).fetchone()
        if row[1] not in ('UNKNOWN', 'RECEIVED') or row[3] or current != (execution_id, 'RUNNING'):
            raise Conflict('Retry requires a current completed/unknown invocation with released capacity')
        if self._db.execute('SELECT 1 FROM dispositions WHERE execution=?', (execution_id,)).fetchone():
            raise Conflict('An applied receipt cannot be retried')
        self._db.execute("UPDATE intents SET state='PENDING',current_execution=NULL,not_before=? WHERE key=?", (not_before_ms, row[0]))

    def _invalidate_owner(self, owner):
        after = ''
        while True:
            keys = self._db.execute('''SELECT i.key,i.current_execution FROM intents i JOIN intent_owners f ON f.intent=i.key
                WHERE f.owner=? AND f.generation!=? AND i.key>? AND i.state IN ('PENDING','RUNNING','BLOCKED')
                ORDER BY i.key LIMIT 1000''', (owner.obligation_id, owner.generation, after)).fetchall()
            if not keys:
                break
            for key, execution in keys:
                if execution:
                    phase = self._db.execute('SELECT phase FROM executions WHERE id=?', (execution,)).fetchone()[0]
                    if phase == 'PREPARED':
                        self._not_sent(execution)
                    elif phase == 'DISPATCHED':
                        self._db.execute("UPDATE executions SET phase='UNKNOWN' WHERE id=?", (execution,))
                self._db.execute("UPDATE intents SET state='CANCELLED' WHERE key=?", (key,))
            after = keys[-1][0]

    @atomic
    def reconcile(self, *, limit=100):
        """One bounded recovery batch. Unknown dispatch keeps its charge/capacity."""
        rows = self._db.execute('''SELECT id,intent,phase FROM executions
            WHERE phase IN ('PREPARED','DISPATCHED') AND session!=? ORDER BY id LIMIT ?''',
            (self._writer.session.session_id, page_limit(limit))).fetchall()
        for execution, intent, phase in rows:
            if phase == 'PREPARED':
                self._not_sent(execution)
                self._db.execute("UPDATE intents SET state='PENDING',current_execution=NULL WHERE key=? AND current_execution=? AND state='RUNNING'", (intent, execution))
            else:
                self._db.execute("UPDATE executions SET phase='UNKNOWN' WHERE id=?", (execution,))
        return len(rows)
