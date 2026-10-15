"""Streaming structural checks; semantic receipt admission stays with services."""
from ..domain import OwnerVersion
from . import codec
from .protocol import StorageError


def _acyclic(db, table, key, edges, parent, child):
    # Disk-backed temporary rows keep even a deep graph out of Python memory.
    # Identifiers here are fixed implementation constants, never caller values.
    db.execute('SAVEPOINT graph_validation')
    try:
        db.execute('CREATE TEMP TABLE graph_degrees (id TEXT PRIMARY KEY, degree INTEGER NOT NULL) WITHOUT ROWID')
        db.execute('CREATE INDEX graph_ready ON graph_degrees(degree,id)')
        db.execute(f'''INSERT INTO graph_degrees SELECT n.{key},count(e.{child}) FROM {table} n
            LEFT JOIN {edges} e ON e.{parent}=n.{key} GROUP BY n.{key}''')
        while True:
            ready = db.execute('SELECT id FROM graph_degrees WHERE degree=0 LIMIT 1000').fetchall()
            if not ready:
                break
            for (value,) in ready:
                db.execute(f'''UPDATE graph_degrees SET degree=degree-1 WHERE id IN
                    (SELECT {parent} FROM {edges} WHERE {child}=?)''', (value,))
                db.execute('DELETE FROM graph_degrees WHERE id=?', (value,))
        if db.execute('SELECT 1 FROM graph_degrees LIMIT 1').fetchone():
            raise StorageError('Persisted dependency graph contains a cycle')
        db.execute('DROP TABLE graph_degrees')
        db.execute('RELEASE graph_validation')
    except BaseException:
        db.execute('ROLLBACK TO graph_validation')
        db.execute('RELEASE graph_validation')
        raise


def validate_views(reader):
    db = reader._db
    frozen = db.execute('SELECT environment_id FROM run').fetchone()[0]
    for key, generation, revision, state, interface, selected in db.execute('SELECT * FROM nodes'):
        saved = reader.node(key)
        node = saved.node
        actual = (node.owner, node.state.value, node.interface.id, node.selected.candidate.id if node.selected else None)
        if actual != (OwnerVersion(key, generation), state, interface, selected) or node.environment_id != frozen:
            raise StorageError('Node projection differs from its saved domain record')
        refs = set(codec.node_refs(node))
        for ref in refs:
            if reader.artifact(ref.id) != ref:
                raise StorageError('Node artifact binding differs')
        indexed = {row[0] for row in db.execute('SELECT artifact FROM node_artifacts WHERE owner=? AND revision=?', (key, revision))}
        if indexed != {ref.id for ref in refs}:
            raise StorageError('Node artifact index differs')
        children = {}
        if node.selected:
            for dep in node.selected.candidate.dependencies:
                child = reader.node(dep.owner.obligation_id).node
                if (child.owner, child.interface, child.environment_id) != (dep.owner, dep.interface, node.environment_id):
                    raise StorageError('Persisted dependency binding differs')
                children[dep.owner.obligation_id] = child
            if node.assembly:
                actual = tuple(children[d.owner.obligation_id].proof for d in node.selected.candidate.logical_dependencies)
                if actual != node.assembly.assembled_from:
                    raise StorageError('Persisted assembly differs from current children')
        if set(children) != {r[0] for r in db.execute('SELECT child FROM edges WHERE parent=?', (key,))}:
            raise StorageError('Selected dependency index differs')
    for key, owner, generation, kind, payload in db.execute('SELECT key,owner,generation,kind,payload FROM intents'):
        request = codec.unrequest(codec.unpack(payload))
        j = request.job
        if (key, owner, generation, kind, frozen) != (j.semantic_key, j.owner.obligation_id, j.owner.generation, j.kind.value, request.environment_id):
            raise StorageError('Intent projection differs from frozen request')
        refs = {*j.input_refs, request.operation_payload, request.request_ref}
        for ref in refs:
            if reader.artifact(ref.id) != ref:
                raise StorageError('Intent artifact binding differs')
        if {r.id for r in refs} != {r[0] for r in db.execute('SELECT artifact FROM intent_artifacts WHERE intent=?', (key,))}:
            raise StorageError('Intent artifact index differs')
        if {(o.obligation_id, o.generation) for o in j.expected_versions} != set(db.execute('SELECT owner,generation FROM intent_owners WHERE intent=?', (key,))):
            raise StorageError('Intent owner fences differ')
        if set(j.depends_on) != {r[0] for r in db.execute('SELECT dependency FROM intent_dependencies WHERE intent=?', (key,))}:
            raise StorageError('Intent dependency index differs')
    if db.execute('''SELECT 1 FROM intents i LEFT JOIN executions e ON e.id=i.current_execution
        WHERE (i.state='RUNNING' AND e.id IS NULL) OR (e.id IS NOT NULL AND e.intent!=i.key) LIMIT 1''').fetchone():
        raise StorageError('Intent current execution differs')
    if db.execute('''SELECT 1 FROM executions e LEFT JOIN execution_usage u ON u.execution=e.id
        WHERE u.execution IS NULL LIMIT 1''').fetchone():
        raise StorageError('Missing execution usage record')
    _acyclic(db, 'nodes', 'id', 'edges', 'parent', 'child')
    _acyclic(db, 'intents', 'key', 'intent_dependencies', 'intent', 'dependency')
