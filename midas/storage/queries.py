"""Indexed, bounded read views and worker capabilities without SQLite access."""
from contextlib import contextmanager
import os
import threading

from ..domain import ArtifactRef, Invocation, OwnerVersion, ReceiptBinding, Session, Usage
from ..domain.records import natural, uuid
from . import codec
from .artifacts import ArtifactError, stream_file
from .protocol import (Accounting, Conflict, ExecutionState, FrozenRun, Intent, NodeSummary, NodeVersion, SavedNode, SavedRecord,
                       Status, StorageError, WorkSnapshot, name, page_limit)


class ArtifactAccess:
    """An explicit allowlist of committed inputs, safe to pass to worker threads."""
    def __init__(self, directory, values):
        self._directory, self._values = directory, values

    def read_artifact(self, reference, *, max_bytes):
        natural(max_bytes, 'Artifact read limit')
        if reference.byte_count > max_bytes:
            raise ArtifactError('Artifact exceeds read limit')
        return b''.join(self.stream_artifact(reference, max_bytes=max_bytes))

    def stream_artifact(self, reference, *, max_bytes):
        natural(max_bytes, 'Artifact read limit')
        saved = self._values.get(reference.id)
        if saved is None or saved[0] != reference or reference.byte_count > max_bytes:
            raise ArtifactError('Artifact is outside this read capability')
        if saved[1] is not None:
            yield reference.verify(saved[1], max_bytes=max_bytes)
        else:
            segment, offset, size = saved[2]
            yield from stream_file(self._directory / 'artifacts' / (segment+'.seg'), reference, offset=offset, stored_size=size)


class RunReader:
    def __init__(self, directory, connection):
        self.directory, self._db = directory, connection
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._status_cache = None
        self.run_id = connection.execute('SELECT id FROM run WHERE singleton=1').fetchone()[0]

    def _check(self):
        if self._db is None or os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise StorageError('Storage handle is closed or belongs to another process/thread')

    @property
    def cache_identity(self):
        """Location and database inode for revision-bound read projections."""
        self._check()
        return getattr(self, '_pool_key', None), self.run_id

    @contextmanager
    def snapshot(self):
        """Pin a WAL snapshot for consistent paginated reads or a fork checkpoint."""
        self._check()
        if self._db.in_transaction:
            yield self
            return
        self._db.execute('BEGIN')
        try:
            self._db.execute('SELECT revision FROM run').fetchone()  # Establish snapshot now.
            yield self
        finally:
            self._db.rollback()

    def artifact(self, artifact_id):
        self._check()
        row = self._db.execute('SELECT digest,size FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return ArtifactRef(artifact_id, *row)

    def artifact_reader(self, references):
        self._check()
        if len(references) > 1000:
            raise ValueError('Artifact capability exceeds batch allowance')
        values = {}
        for ref in references:
            row = self._db.execute('SELECT digest,size,inline,segment,offset,stored_size FROM artifacts WHERE id=?', (ref.id,)).fetchone()
            if row is None or row[:2] != (ref.sha256, ref.byte_count):
                raise ArtifactError('Unknown or mismatched committed artifact')
            if row[3] is not None:
                uuid(row[3])
            values[ref.id] = (ref, row[2], row[3:])
        return ArtifactAccess(self.directory, values)

    def read_artifact(self, reference, *, max_bytes):
        natural(max_bytes, 'Artifact read limit')
        if reference.byte_count > max_bytes:
            raise ArtifactError('Artifact exceeds read limit')
        return self.artifact_reader((reference,)).read_artifact(reference, max_bytes=max_bytes)

    def stream_artifact(self, reference, *, max_bytes):
        return self.artifact_reader((reference,)).stream_artifact(reference, max_bytes=max_bytes)

    def artifacts(self, *, after='', limit=100):
        self._check()
        return tuple(ArtifactRef(*row) for row in self._db.execute(
            'SELECT id,digest,size FROM artifacts WHERE id>? ORDER BY id LIMIT ?', (after, page_limit(limit))))

    def frozen_run(self):
        self._check()
        row = self._db.execute('SELECT environment_id,inputs,configuration,parent_id,parent_revision FROM run').fetchone()
        if row[0] is None:
            raise StorageError('Run initialization has not committed')
        return FrozenRun(row[0], self.artifact(row[1]), self.artifact(row[2]), *row[3:])

    def node(self, owner_id):
        self._check()
        row = self._db.execute('''SELECT n.generation,n.revision,h.payload FROM nodes n
            JOIN node_history h ON h.owner=n.id AND h.revision=n.revision WHERE n.id=?''', (owner_id,)).fetchone()
        if row is None:
            raise KeyError(owner_id)
        return SavedNode(NodeVersion(OwnerVersion(owner_id, row[0]), row[1]), codec.unnode(codec.unpack(row[2])))

    def node_history(self, owner_id, *, after=-1, limit=100):
        self._check()
        rows = self._db.execute('SELECT revision,payload FROM node_history WHERE owner=? AND revision>? ORDER BY revision LIMIT ?',
                                (owner_id, after, page_limit(limit)))
        result = []
        for revision, payload in rows:
            node = codec.unnode(codec.unpack(payload))
            result.append(SavedNode(NodeVersion(node.owner, revision), node))
        return tuple(result)

    def root_counts(self):
        self._check()
        return tuple(self._db.execute('''SELECT n.state,count(*) FROM roots r
            JOIN nodes n ON n.id=r.owner GROUP BY n.state'''))

    def current_session(self):
        """The recorded owner, which may need recovery after a process crash."""
        self._check()
        row = self._db.execute("SELECT id,epoch FROM sessions WHERE state='ACTIVE'").fetchone()
        return Session(self.run_id, *row) if row else None

    def node_summaries(self, *, after='', limit=100, state=None, owner=None, roots=False):
        """Read compact current columns, without decoding proof history/payloads."""
        self._check()
        page_limit(limit)
        if state is not None and state not in ('UNRESOLVED', 'CONDITIONAL', 'VERIFIED'):
            raise ValueError('Unknown proof state')
        clauses, params = ['n.id>?'], [after]
        for column, value in (('n.state', state), ('n.id', owner)):
            if value is not None:
                clauses.append(column+'=?')
                params.append(value)
        if roots:
            clauses.append('r.ordinal IS NOT NULL')
        rows = self._db.execute('''SELECT n.id,n.generation,n.revision,n.state,
            a.id,a.digest,a.size,r.ordinal FROM nodes n JOIN artifacts a ON a.id=n.interface
            LEFT JOIN roots r ON r.owner=n.id WHERE '''+' AND '.join(clauses)+
            ' ORDER BY n.id LIMIT ?', (*params, limit))
        return tuple(NodeSummary(OwnerVersion(*row[:2]), row[2], row[3],
                                 ArtifactRef(*row[4:7]), row[7]) for row in rows)

    def nodes(self, *, after='', limit=100, state=None):
        self._check()
        if state is not None and state not in ('UNRESOLVED', 'CONDITIONAL', 'VERIFIED'):
            raise ValueError('Unknown proof state')
        with self.snapshot():
            if state is None:
                rows = self._db.execute('SELECT id FROM nodes WHERE id>? ORDER BY id LIMIT ?', (after, page_limit(limit)))
            else:
                rows = self._db.execute('SELECT id FROM nodes WHERE state=? AND id>? ORDER BY id LIMIT ?', (state, after, page_limit(limit)))
            return tuple(self.node(row[0]) for row in rows)

    def display_nodes(self, *, extra_roots=(), after='', limit=100):
        """Page roots, running owners and their dependencies, plus caller-supplied roots."""
        self._check()
        page_limit(limit)
        if len(extra_roots) > 1000:
            raise ValueError('Display roots exceed batch allowance')
        extra = ' UNION SELECT id FROM nodes WHERE id IN ('+','.join('?' for _ in extra_roots)+')' if extra_roots else ''
        rows = self._db.execute('''WITH RECURSIVE current(id) AS (
            SELECT owner FROM roots
            UNION SELECT i.owner FROM intents i JOIN nodes n ON n.id=i.owner AND n.generation=i.generation
                WHERE i.state='RUNNING'
            '''+extra+'''
            UNION SELECT e.child FROM edges e JOIN current c ON e.parent=c.id
        ) SELECT n.id,n.generation,n.revision,n.state,a.id,a.digest,a.size,r.ordinal
            FROM current c JOIN nodes n ON n.id=c.id JOIN artifacts a ON a.id=n.interface
            LEFT JOIN roots r ON r.owner=n.id WHERE n.id>? ORDER BY n.id LIMIT ?''',
            (*extra_roots, after, limit))
        return tuple(NodeSummary(OwnerVersion(*row[:2]), row[2], row[3], ArtifactRef(*row[4:7]), row[7])
                     for row in rows)

    def consumers(self, owner_id, *, after='', limit=100):
        self._check()
        return tuple(row[0] for row in self._db.execute(
            'SELECT parent FROM edges WHERE child=? AND parent>? ORDER BY parent LIMIT ?', (owner_id, after, page_limit(limit))))

    def roots(self, *, after=-1, limit=100):
        self._check()
        return tuple(self._db.execute('SELECT ordinal,owner FROM roots WHERE ordinal>? ORDER BY ordinal LIMIT ?', (after, page_limit(limit))))

    def record(self, scope, key, *, revision=None):
        self._check()
        name(scope)
        name(key)
        if revision is None:
            row = self._db.execute('''SELECT h.revision,h.artifact FROM records r JOIN record_history h
                USING(scope,key,revision) WHERE r.scope=? AND r.key=?''', (scope, key)).fetchone()
        else:
            natural(revision, 'Record revision')
            row = self._db.execute('SELECT revision,artifact FROM record_history WHERE scope=? AND key=? AND revision=?', (scope, key, revision)).fetchone()
        return SavedRecord(scope, key, row[0], self.artifact(row[1]) if row[1] else None) if row else None

    def records(self, scope, *, after='', limit=100):
        self._check()
        name(scope)
        return tuple(self.record(scope, row[0]) for row in self._db.execute(
            'SELECT key FROM records WHERE scope=? AND key>? ORDER BY key LIMIT ?', (scope, after, page_limit(limit))))

    def counter(self, scope, key):
        self._check()
        name(scope)
        name(key)
        row = self._db.execute('SELECT value FROM counters WHERE scope=? AND key=?', (scope, key)).fetchone()
        return row[0] if row else 0

    def status_snapshot(self):
        self._check()
        cache_key = None
        if not self._db.in_transaction:
            cache_key = (self._db.execute('PRAGMA data_version').fetchone()[0], self._db.total_changes)
            if self._status_cache is not None and self._status_cache[0] == cache_key:
                return self._status_cache[1]
        # One SQLite statement is one consistent snapshot, even for a live reader.
        # Start with indexed running work, not the potentially large unresolved graph.
        rows = self._db.execute('''SELECT r.revision,r.graph_revision,a.*,c.category,c.state,c.value,
            (SELECT count(DISTINCT i.owner) FROM intents i
             CROSS JOIN nodes n ON n.id=i.owner AND n.generation=i.generation
             WHERE i.state='RUNNING' AND n.state='UNRESOLVED')
            FROM run r JOIN accounting a ON a.singleton=r.singleton LEFT JOIN counts c ON c.value>0
            ORDER BY c.category,c.state''').fetchall()
        revision, graph = rows[0][:2]
        a = rows[0][3:-4]
        counts = {k: [] for k in ('nodes', 'intents', 'executions')}
        for row in rows:
            category, state, value = row[-4:-1]
            if category is not None:
                counts[category].append((state, value))
        result = Status(revision, graph, *(tuple(counts[k]) for k in ('nodes', 'intents', 'executions')),
                        Accounting(a[0], Usage(*a[1:5]), Usage(*a[5:9]), Usage(*a[9:13]), *a[13:]), rows[0][-1])
        if cache_key is not None:
            self._status_cache = (cache_key, result)
        return result

    def work_snapshot(self, owners=(), *, records=(), counters=()):
        if any(len(v) > 1000 for v in (owners, records, counters)):
            raise ValueError('Snapshot exceeds batch allowance')
        with self.snapshot():
            nodes = tuple(self.node(owner.obligation_id) for owner in owners)
            if any(n.version.owner != o for n, o in zip(nodes, owners)):
                raise Conflict('Snapshot owner generation changed')
            return WorkSnapshot(self.status_snapshot(), nodes, tuple(self.record(*r) for r in records),
                                tuple(self.counter(*c) for c in counters))

    def intent(self, key):
        self._check()
        row = self._db.execute('SELECT payload,priority,not_before FROM intents WHERE key=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return Intent(codec.unrequest(codec.unpack(row[0])), *row[1:])

    def ready(self, now_ms, *, limit=100, after=None):
        self._check()
        natural(now_ms, 'Clock')
        cursor = (-1, -1, '') if after is None else after
        if type(cursor) is not tuple or len(cursor) != 3:
            raise ValueError('Invalid ready cursor')
        if after is not None:
            natural(cursor[0], 'Ready priority')
            natural(cursor[1], 'Ready time')
            name(cursor[2])
        rows = self._db.execute('''SELECT i.key FROM intents i
            WHERE i.state='PENDING' AND i.not_before<=?
            AND (i.priority,i.not_before,i.key)>(?,?,?)
            AND NOT EXISTS (SELECT 1 FROM intent_owners f JOIN nodes n ON n.id=f.owner
                WHERE f.intent=i.key AND f.generation!=n.generation)
            AND NOT EXISTS (SELECT 1 FROM intent_dependencies d JOIN intents p ON p.key=d.dependency
                WHERE d.intent=i.key AND p.state!='DONE')
            ORDER BY i.priority,i.not_before,i.key LIMIT ?''', (now_ms, *cursor, page_limit(limit)))
        return tuple(self.intent(row[0]) for row in rows)

    def intent_state(self, key):
        self._check()
        row = self._db.execute('SELECT state,current_execution FROM intents WHERE key=?', (key,)).fetchone()
        return row

    def record_count(self, scope, *, after='', before='\uffff'):
        self._check()
        name(scope)
        return self._db.execute('SELECT count(*) FROM records WHERE scope=? AND key>? AND key<=?',
                                (scope, after, before)).fetchone()[0]

    def recovery_pending(self):
        self._check()
        return self._db.execute("""SELECT 1 FROM executions e JOIN intents i ON i.current_execution=e.id
            WHERE e.phase='UNKNOWN' AND i.state='RUNNING' LIMIT 1""").fetchone() is not None

    def queue_counts(self):
        """Fixed-size aggregates over the pending queue, never execution history."""
        self._check()
        return tuple(self._db.execute("SELECT kind,count(*) FROM intents WHERE state='PENDING' GROUP BY kind"))

    def unreconciled(self, session, *, limit=100):
        self._check()
        return tuple(row[0] for row in self._db.execute('''SELECT id FROM executions
            WHERE phase IN ('PREPARED','DISPATCHED') AND session!=? ORDER BY id LIMIT ?''',
            (session.session_id, page_limit(limit))))

    def invocation(self, execution_id):
        self._check()
        row = self._db.execute('''SELECT e.intent,e.session,s.epoch,e.reservation FROM executions e
            JOIN sessions s ON s.id=e.session WHERE e.id=?''', (execution_id,)).fetchone()
        if row is None:
            from .receipts import imported
            saved = imported(self, execution_id)
            if saved:
                return saved[0]
            raise KeyError(execution_id)
        return Invocation(execution_id, Session(self.run_id, row[1], row[2]), self.intent(row[0]).request, self.artifact(row[3]))

    def receipt(self, execution_id):
        self._check()
        row = self._db.execute('SELECT request,result,seal FROM receipts WHERE execution=?', (execution_id,)).fetchone()
        if row:
            return ReceiptBinding(execution_id, *(self.artifact(r) for r in row))
        from .receipts import imported
        saved = imported(self, execution_id)
        return saved[1] if saved else None

    def verify_receipt(self, receipt, invocation, result):
        from .receipts import verify
        return verify(self, receipt, invocation, result)

    def artifacts(self, *, after='', limit=100):
        self._check()
        return tuple(ArtifactRef(*row) for row in self._db.execute(
            'SELECT id,digest,size FROM artifacts WHERE id>? ORDER BY id LIMIT ?', (after, page_limit(limit))))

    def receipt_ids(self, *, after='', limit=100):
        self._check()
        return tuple(row[0] for row in self._db.execute('''SELECT execution FROM receipts WHERE execution>?
            UNION SELECT execution FROM imported_receipts WHERE execution>? ORDER BY execution LIMIT ?''',
            (after, after, page_limit(limit))))

    def execution(self, execution_id):
        self._check()
        with self.snapshot():
            invocation = self.invocation(execution_id)
            row = self._db.execute('''SELECT e.phase,e.charged,e.held,e.bounds,u.input_tokens,u.cache_tokens,
                u.output_tokens,u.cost_microusd,d.value,e.provider,e.provider_request,e.adopted_session
                FROM executions e JOIN execution_usage u ON u.execution=e.id
                LEFT JOIN dispositions d ON d.execution=e.id WHERE e.id=?''', (execution_id,)).fetchone()
            return ExecutionState(invocation, row[0], bool(row[1]), bool(row[2]), Usage(*codec.unpack(row[3])),
                                  Usage(*row[4:8]), self.receipt(execution_id), *row[8:])

    def result(self, execution_id):
        receipt = self.receipt(execution_id)
        if receipt is None:
            raise KeyError(execution_id)
        result = codec.load_result(self.read_artifact(receipt.result_ref, max_bytes=65536))
        result.validate_for(self.invocation(execution_id))
        return result

    def pending_receipts(self, *, after='', limit=100):
        self._check()
        return tuple(r[0] for r in self._db.execute('''SELECT e.id FROM executions e
            JOIN intents i ON i.current_execution=e.id
            WHERE e.phase='RECEIVED' AND e.id>? AND i.state='RUNNING'
            AND NOT EXISTS (SELECT 1 FROM intent_owners f JOIN nodes n ON n.id=f.owner
                WHERE f.intent=i.key AND f.generation!=n.generation)
            ORDER BY e.id LIMIT ?''', (after, page_limit(limit))))

    def provider_invocation(self, provider, request_id):
        self._check()
        row = self._db.execute('SELECT id FROM executions WHERE provider=? AND provider_request=?', (provider, request_id)).fetchone()
        return self.invocation(row[0]) if row else None

    def executions(self, phase, *, after='', limit=100):
        self._check()
        return tuple(row[0] for row in self._db.execute(
            'SELECT id FROM executions WHERE phase=? AND id>? ORDER BY id LIMIT ?', (phase, after, page_limit(limit))))

    def progress(self, execution_id, *, after=-1, limit=100):
        self._check()
        from ..domain import WorkerProgress
        return tuple(WorkerProgress(execution_id, r[0], r[1], tuple(codec.unref(v) for v in codec.unpack(r[2])))
            for r in self._db.execute('SELECT sequence,kind,payload FROM progress WHERE execution=? AND sequence>? ORDER BY sequence LIMIT ?',
                                      (execution_id, after, page_limit(limit))))

    def close(self):
        if self._db is not None:
            self._check()
            if getattr(self, '_pool_key', None) is not None:
                from .readers import release
                release(self._pool_key, self._db)
            else:
                self._db.close()
            self._db = None

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_):
        self.close()
