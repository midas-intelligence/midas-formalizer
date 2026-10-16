"""Application pointers and frozen job bindings. Large values live in artifacts."""
from dataclasses import replace

from ..controller import Effect, Planned, artifact
from ..domain import ArtifactRef, FrozenRequest, Job, Usage
from ..policy import AdmissionRequest
from ..storage import Changes, Intent, NodeChange, RecordChange
from ..storage.codec import ref, unref, owner, candidate, uncandidate, check, uncheck, receipt, unreceipt

from .metadata import DATA, META, SCOPE, Inputs, Owner, owner_state, pack, read, value


def wire_artifact(data, key):
    """Distinct turn references preserve identical wire bytes and cache prefixes."""
    from uuid import UUID, uuid5
    value = artifact(data)
    return replace(value, reference=replace(value.reference,
        id=str(uuid5(UUID('cab38953-5b47-437d-a2d8-b7636fb6d5d1'), key))))


def pointer(reader, scope, key, reference):
    saved = reader.record(scope, key)
    return RecordChange(scope, key, saved.revision if saved else None, reference)


def save(reader, scope, key, data):
    item = artifact(data)
    return Effect(Changes(records=(pointer(reader, scope, key, item.reference),)), artifacts=(item,))


def record(reader, scope, key, data):
    return save(reader, scope, key, pack(data))


def merge(*effects):
    """Compose disjoint proposals; reject accidental double updates before commit."""
    changes = [e.changes for e in effects]
    revisions = {c.graph_revision for c in changes if c.graph_revision is not None}
    roots = [c.roots for c in changes if c.roots is not None]
    if len(revisions) > 1 or len(roots) > 1:
        raise ValueError('Conflicting graph proposals')
    return Effect(Changes(expected=tuple(dict.fromkeys(o for c in changes for o in c.expected)),
        nodes=tuple(n for c in changes for n in c.nodes), records=tuple(r for c in changes for r in c.records),
        counters=tuple(n for c in changes for n in c.counters), roots=roots[0] if roots else None,
        graph_revision=next(iter(revisions), None)),
        work=tuple(w for e in effects for w in e.work),
        artifacts=tuple({a.reference: a for e in effects for a in e.artifacts}.values()),
        cancel=tuple(dict.fromkeys(o for e in effects for o in e.cancel)))


def graph_changes(reader, before, after):
    previous = {n.owner.obligation_id: n for n in before.nodes}
    nodes = tuple(NodeChange(reader.node(n.owner.obligation_id).version if n.owner.obligation_id in previous else None, n)
                  for n in after.nodes if previous.get(n.owner.obligation_id) != n)
    return Effect(Changes(nodes=nodes, graph_revision=before.revision if nodes else None))


def job(reader, key, kind, owner_version, environment_id, payload, inputs, expected, limits,
        binding, *, bounds=Usage(), helper=None, downstream=0):
    operation = artifact(payload)
    job = Job(key, kind, owner_version, inputs, tuple(dict.fromkeys((owner_version, *expected))), limits)
    # The receipt binds the entire job, including limits and version fences.
    envelope = artifact(pack(['midas-application-request-v1', key, kind.value, owner(owner_version),
        environment_id, ref(operation.reference), [ref(r) for r in inputs],
        [owner(o) for o in job.expected_versions],
        [limits.wall_ms, limits.memory_mib, limits.output_bytes, limits.evidence_bytes,
         limits.max_heartbeat, limits.max_rec_depth]]))
    request = FrozenRequest(job, environment_id, operation.reference, envelope.reference)
    return merge(record(reader, 'application.jobs', key, binding),
        Effect(work=(Planned(Intent(request), AdmissionRequest(job, bounds=bounds, helper=helper,
                                                             downstream=downstream)),),
               artifacts=(operation, envelope)))


def save_owner(reader, key, state):
    return save(reader, 'application.owners', key, state.payload)
