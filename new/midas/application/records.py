"""Application pointers and frozen job bindings. Large values live in artifacts."""
from dataclasses import dataclass, replace

from ..controller import Effect, Planned, artifact
from ..domain import ArtifactRef, FrozenRequest, Job, Usage
from ..domain.encoding import load_record, record_parts
from ..policy import AdmissionRequest
from ..storage import Changes, Intent, NodeChange, RecordChange
from ..storage.codec import ref, unref, owner, candidate, uncandidate, check, uncheck, receipt, unreceipt

META = 256 * 1024
DATA = 64 * 1024**2
SCOPE = 'application'


def pack(value, limit=META):
    return b''.join(record_parts(value, limit=limit))


def wire_artifact(data, key):
    """Distinct turn references preserve identical wire bytes and cache prefixes."""
    from uuid import UUID, uuid5
    value = artifact(data)
    return replace(value, reference=replace(value.reference,
        id=str(uuid5(UUID('cab38953-5b47-437d-a2d8-b7636fb6d5d1'), key))))


def read(reader, reference, limit=META):
    return reader.read_artifact(reference, max_bytes=limit)


def value(reader, scope, key):
    saved = reader.record(scope, key)
    return load_record(read(reader, saved.artifact), limit=META) if saved and saved.artifact else None


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


@dataclass(frozen=True, slots=True)
class Owner:
    context: ArtifactRef
    identity: ArtifactRef
    index: int
    base: ArtifactRef | None = None
    submission: str | None = None
    memory: ArtifactRef | None = None
    stopped: bool = False
    reason: str | None = None
    evidence: tuple[ArtifactRef, ...] = ()

    @property
    def payload(self):
        return pack([ref(self.context), ref(self.identity), self.index, ref(self.base), self.submission,
                     ref(self.memory), self.stopped, self.reason, [ref(r) for r in self.evidence]])

    @classmethod
    def load(cls, data):
        v = load_record(data, limit=META)
        if type(v) is not list or len(v) != 9:
            raise ValueError('Invalid application owner')
        return cls(unref(v[0]), unref(v[1]), v[2], unref(v[3]), v[4], unref(v[5]), v[6], v[7], tuple(unref(r) for r in v[8]))


def owner_state(reader, key):
    saved = reader.record('application.owners', key)
    return saved, Owner.load(read(reader, saved.artifact))


def save_owner(reader, key, state):
    return save(reader, 'application.owners', key, state.payload)


@dataclass(frozen=True, slots=True)
class Inputs:
    selection: ArtifactRef
    package: str
    workflow: str
    instructions: tuple[tuple[str, ArtifactRef], ...] = ()

    def __post_init__(self):
        if self.workflow not in ('live', 'scripted', 'supplied'):
            raise ValueError('Unknown application workflow')
        if type(self.package) is not str or not self.package or len(self.package.encode()) > 4096:
            raise ValueError('Invalid package locator')

    @property
    def payload(self):
        return pack(['midas-application-inputs-v1', ref(self.selection), self.package, self.workflow,
                     [[role, ref(reference)] for role, reference in self.instructions]])

    @classmethod
    def load(cls, payload):
        v = load_record(payload, limit=META)
        if type(v) is not list or len(v) != 5 or v[0] != 'midas-application-inputs-v1':
            raise ValueError('Invalid application inputs')
        return cls(unref(v[1]), v[2], v[3], tuple((role, unref(r)) for role, r in v[4]))
