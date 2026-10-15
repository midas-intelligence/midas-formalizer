"""Fenced graph/state applications. Every operation is atomic even if caught."""
from functools import wraps

from ..domain.records import instance
from . import codec
from .artifacts import Durable
from .protocol import Changes, Conflict, FrozenRun, StorageError


def atomic(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        self._check()
        self._db.execute('SAVEPOINT operation')
        try:
            before = self._db.total_changes
            value = method(self, *args, **kwargs)
            self._db.execute('RELEASE operation')
            if method.__name__ != 'register' and self._db.total_changes != before:
                self._state_changed = True
            return value
        except BaseException:
            self._db.execute('ROLLBACK TO operation')
            self._db.execute('RELEASE operation')
            raise
    return call


class StateTransaction:
    def __init__(self, writer):
        self._writer, self._db = writer, writer._db
        self._state_changed = False

    def _check(self):
        self._writer._check()
        if self._writer._tx is not self or not self._db.in_transaction:
            raise StorageError('Transaction is no longer active')

    def _ref(self, ref):
        if self._writer.artifact(ref.id) != ref:
            raise Conflict('Artifact reference differs from committed bytes')
        return ref.id

    def expect(self, owners):
        self._check()
        for owner in owners:
            row = self._db.execute('SELECT generation FROM nodes WHERE id=?', (owner.obligation_id,)).fetchone()
            if row != (owner.generation,):
                raise Conflict('Owner generation changed')

    @atomic
    def register(self, durable):
        instance(durable, Durable)
        if durable.authority is not self._writer._authority:
            raise Conflict('Artifact was not finalized by this writer')
        r = durable.reference
        previous = self._db.execute('SELECT digest,size FROM artifacts WHERE id=?', (r.id,)).fetchone()
        if previous is not None:
            if previous != (r.sha256, r.byte_count):
                raise Conflict('Artifact ID conflicts with immutable bytes')
            return r
        if durable.inline is None:
            segment, offset, size = durable.location
            self._db.execute('''INSERT INTO segments VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET
                size=excluded.size,stamp=excluded.stamp WHERE excluded.size>=segments.size''',
                (segment, offset+size, durable.stamp))
        else:
            segment = offset = size = None
        self._db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)',
                         (r.id, r.sha256, r.byte_count, durable.inline, segment, offset, size))
        return r

    @atomic
    def freeze(self, frozen):
        instance(frozen, FrozenRun)
        row = self._db.execute('SELECT environment_id FROM run').fetchone()
        if row[0] is not None:
            if self._writer.frozen_run() != frozen:
                raise Conflict('Frozen run inputs cannot change')
            return False
        self._db.execute('UPDATE run SET environment_id=?,inputs=?,configuration=?,parent_id=?,parent_revision=?',
            (frozen.environment_id, self._ref(frozen.inputs), self._ref(frozen.configuration), frozen.parent_run_id, frozen.parent_revision))
        return True

    def _record(self, change):
        row = self._db.execute('SELECT revision FROM records WHERE scope=? AND key=?', (change.scope, change.key)).fetchone()
        if (row[0] if row else None) != change.expected:
            raise Conflict('Record revision changed')
        revision = row[0]+1 if row else 0
        artifact = self._ref(change.artifact) if change.artifact else None
        self._db.execute('INSERT INTO record_history VALUES (?,?,?,?)', (change.scope, change.key, revision, artifact))
        self._db.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(scope,key) DO UPDATE SET revision=excluded.revision',
                         (change.scope, change.key, revision))

    def _counter(self, change):
        if self._writer.counter(change.scope, change.key) != change.expected:
            raise Conflict('Counter changed')
        self._db.execute('INSERT INTO counters VALUES (?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value',
                         (change.scope, change.key, change.value))

    def _intent(self, intent):
        request, job = intent.request, intent.request.job
        if self._writer.frozen_run().environment_id != request.environment_id:
            raise Conflict('Intent environment differs from frozen run')
        payload = codec.pack(codec.request(request))
        previous = self._db.execute('SELECT payload,priority,not_before FROM intents WHERE key=?', (job.semantic_key,)).fetchone()
        if previous is not None:
            if previous != (payload, intent.priority, intent.not_before_ms):
                raise Conflict('Intent semantic key conflicts with immutable inputs')
            return
        self.expect(job.expected_versions)
        self._db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,'PENDING',NULL)",
            (job.semantic_key, job.owner.obligation_id, job.owner.generation, job.kind.value, payload, intent.priority, intent.not_before_ms))
        refs = {*job.input_refs, request.operation_payload, request.request_ref}
        for ref in refs:
            self._db.execute('INSERT INTO intent_artifacts VALUES (?,?)', (job.semantic_key, self._ref(ref)))
        self._db.executemany('INSERT INTO intent_owners VALUES (?,?,?)',
            ((job.semantic_key, o.obligation_id, o.generation) for o in job.expected_versions))
        self._db.executemany('INSERT INTO intent_dependencies VALUES (?,?)', ((job.semantic_key, d) for d in job.depends_on))

    def _nodes(self, changes):
        env = self._writer.frozen_run().environment_id
        affected = set()
        for change in changes:
            node, expected = change.node, change.expected
            key = node.owner.obligation_id
            previous = self._db.execute('SELECT generation,revision FROM nodes WHERE id=?', (key,)).fetchone()
            if previous != ((expected.owner.generation, expected.revision) if expected else None):
                raise Conflict('Node revision changed')
            if node.environment_id != env:
                raise Conflict('Node environment differs from frozen run')
            revision = previous[1]+1 if previous else 0
            self._db.execute('INSERT INTO node_history VALUES (?,?,?)', (key, revision, codec.pack(codec.node(node))))
            for ref in set(codec.node_refs(node)):
                self._db.execute('INSERT INTO node_artifacts VALUES (?,?,?)', (key, revision, self._ref(ref)))
            self._db.execute('''INSERT INTO nodes VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                generation=excluded.generation,revision=excluded.revision,state=excluded.state,
                interface=excluded.interface,selected=excluded.selected''',
                (key, node.owner.generation, revision, node.state.value, node.interface.id,
                 node.selected.candidate.id if node.selected else None))
            if previous and previous[0] != node.owner.generation:
                self._invalidate_owner(node.owner)
            affected.add(key)
        for change in changes:
            node, key = change.node, change.node.owner.obligation_id
            self._db.execute('DELETE FROM edges WHERE parent=?', (key,))
            if node.selected:
                for child in {d.owner.obligation_id for d in node.selected.candidate.dependencies}:
                    self._db.execute('INSERT INTO edges VALUES (?,?)', (key, child))
            affected.update(row[0] for row in self._db.execute('SELECT parent FROM edges WHERE child=?', (key,)))
        # Check the touched nodes and direct consumers, never decode all history.
        for key in affected:
            node = self._writer.node(key).node
            if node.selected is None:
                continue
            children = {}
            for dep in node.selected.candidate.dependencies:
                child = self._writer.node(dep.owner.obligation_id).node
                if (child.owner, child.interface, child.environment_id) != (dep.owner, dep.interface, node.environment_id):
                    raise Conflict('Selected dependency binding changed')
                children[dep.owner.obligation_id] = child
            if node.assembly is not None:
                actual = tuple(children[d.owner.obligation_id].proof for d in node.selected.candidate.logical_dependencies)
                if actual != node.assembly.assembled_from:
                    raise Conflict('Assembly differs from current children')
        for change in changes:
            key = change.node.owner.obligation_id
            if self._db.execute('''WITH RECURSIVE reachable(id) AS (
                SELECT child FROM edges WHERE parent=? UNION SELECT e.child FROM edges e JOIN reachable r ON e.parent=r.id)
                SELECT 1 FROM reachable WHERE id=? LIMIT 1''', (key, key)).fetchone():
                raise Conflict('Selected dependencies contain a cycle')

    @atomic
    def apply(self, key, changes, *, receipt=None, disposition='APPLIED'):
        """Commit validated effects and finish their receipt in the same transaction."""
        from .protocol import name
        name(key)
        name(disposition)
        instance(changes, Changes)
        # Include disposition and exact original receipt in duplicate comparison.
        payload = codec.pack([codec.receipt(receipt) if receipt else None, disposition]) + codec.changes(changes)
        prior = self._db.execute('SELECT payload FROM applications WHERE key=?', (key,)).fetchone()
        if prior is not None:
            if prior[0] != payload:
                raise Conflict('Conflicting application replay')
            return False
        if receipt is not None:
            self._accepting(receipt)
        self.expect(changes.expected)
        if changes.graph_revision is not None:
            if self._db.execute('SELECT graph_revision FROM run').fetchone()[0] != changes.graph_revision:
                raise Conflict('Graph revision changed')
        self._nodes(changes.nodes)
        for change in changes.records:
            self._record(change)
        for change in changes.counters:
            self._counter(change)
        for intent in changes.intents:
            self._intent(intent)
        for intent in changes.intents:
            key_ = intent.request.job.semantic_key
            if self._db.execute('''WITH RECURSIVE dependencies(key) AS (
                SELECT dependency FROM intent_dependencies WHERE intent=? UNION
                SELECT d.dependency FROM intent_dependencies d JOIN dependencies p ON d.intent=p.key)
                SELECT 1 FROM dependencies WHERE key=? LIMIT 1''', (key_, key_)).fetchone():
                raise Conflict('Intent dependencies contain a cycle')
        if changes.roots is not None:
            self._db.execute('DELETE FROM roots')
            self._db.executemany('INSERT INTO roots VALUES (?,?)', enumerate(changes.roots))
        if changes.nodes or changes.roots is not None:
            self._db.execute('UPDATE run SET graph_revision=graph_revision+1')
        self._db.execute('INSERT INTO applications VALUES (?,?,?)', (key, payload, receipt.original_execution_id if receipt else None))
        if receipt is not None:
            self._finish(receipt, key, disposition)
        return True
