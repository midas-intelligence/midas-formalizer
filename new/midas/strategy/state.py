"""Immutable planning/acceptance values. Storage owns transactions and publication."""
from dataclasses import dataclass, field
from hashlib import sha256

from ..domain import (AdmittedCheck, AdmittedReview, ArtifactRef, Candidate, CheckKind,
                      FrozenRequest, JobKind, OwnerVersion, ProofGraph, ReceiptBinding)
from ..domain.encoding import load_record
from ..domain.records import identifier, instance, items, natural
from ..policy import OwnerUsage, Policy, RunUsage, Services
from ..prompt import WireRequest
from ..tools import EvidenceChange, Page, Plan
from .candidates import Context, Draft, MAX_BYTES
from .memory import Memory, pack, text


def candidate_hash(candidate):
    if candidate is None:
        return None
    return sha256(pack([candidate.id, candidate.owner.obligation_id, candidate.owner.generation,
        candidate.environment_id, candidate.source.to_dict(), candidate.revision_of,
        candidate.joint_submission_id, [[d.owner.obligation_id, d.owner.generation, d.alias,
        d.interface.to_dict(), d.origin.value, d.certificate.to_dict() if d.certificate else None,
        d.submission_id] for d in candidate.dependencies]], MAX_BYTES)).hexdigest()


@dataclass(frozen=True, slots=True)
class Version:
    owner: OwnerVersion
    revision: int
    context: ArtifactRef
    memory: int = 0
    evidence: int = 0

    def __post_init__(self):
        instance(self.owner, OwnerVersion)
        instance(self.context, ArtifactRef)
        for name in ('revision', 'memory', 'evidence'):
            natural(getattr(self, name), name)

    def value(self):
        return [self.owner.obligation_id, self.owner.generation, self.revision,
                self.context.to_dict(), self.memory, self.evidence]

    @classmethod
    def load(cls, value):
        if type(value) is not list or len(value) != 6:
            raise ValueError('Invalid strategy version')
        return cls(OwnerVersion(*value[:2]), value[2], ArtifactRef.from_dict(value[3]), *value[4:])


@dataclass(frozen=True, slots=True)
class Attempt:
    candidate: Candidate
    bound: bool = False
    checked: AdmittedCheck | None = None
    review: AdmittedReview | None = None
    binding_only: bool = False

    def __post_init__(self):
        instance(self.candidate, Candidate)
        if type(self.bound) is not bool or type(self.binding_only) is not bool:
            raise ValueError('Expected binding flag')
        if self.checked is not None:
            instance(self.checked, AdmittedCheck)
            if (not self.bound or self.binding_only or self.checked.candidate != self.candidate
                    or self.checked.kind == CheckKind.ASSEMBLY):
                raise ValueError('Pending check differs from bound candidate')
        if self.review is not None:
            instance(self.review, AdmittedReview)
            if (self.checked is None or self.checked.kind != CheckKind.CONDITIONAL
                    or self.review.candidate != self.candidate or not self.review.approved):
                raise ValueError('Pending review differs from checked composition')


@dataclass(frozen=True, slots=True)
class Snapshot:
    version: Version
    context: Context
    environment_id: str
    graph: ProofGraph
    memory: Memory = Memory()
    usage: OwnerUsage = OwnerUsage()
    run: RunUsage = RunUsage()
    services: Services = Services()
    workflow: str = 'live'
    base: Draft | None = None
    attempt: Attempt | None = None
    busy: bool = False
    stopped: bool = False
    allowed_pins: tuple[str, ...] = ()
    forbidden_names: tuple[str, ...] = ()
    cursors: tuple[str, ...] = ()
    stop_request: 'StopRequest | None' = None

    def __post_init__(self):
        instance(self.version, Version)
        instance(self.context, Context)
        identifier(self.environment_id, 'env_')
        self.version.context.verify(self.context.payload, max_bytes=MAX_BYTES)
        instance(self.graph, ProofGraph)
        node = self.graph.node(self.version.owner)
        if node.environment_id != self.environment_id:
            raise ValueError('Strategy environment hash differs from graph')
        instance(self.memory, Memory)
        if self.memory.version != self.version.memory:
            raise ValueError('Strategy memory version differs')
        for value, cls in ((self.usage, OwnerUsage), (self.run, RunUsage), (self.services, Services)):
            instance(value, cls)
        if self.workflow not in ('live', 'scripted', 'supplied'):
            raise ValueError('Unknown strategy workflow')
        if type(self.busy) is not bool or type(self.stopped) is not bool:
            raise ValueError('Expected snapshot state flags')
        if self.base is not None:
            instance(self.base, Draft)
        if self.stop_request is not None:
            instance(self.stop_request, StopRequest)
            if self.stop_request.owner != self.version.owner or self.attempt is not None:
                raise ValueError('Stop request differs from idle current owner')
        if self.attempt is not None:
            instance(self.attempt, Attempt)
            candidate = self.attempt.candidate
            if candidate.owner != self.version.owner or candidate.environment_id != self.environment_id:
                raise ValueError('Pending candidate differs from snapshot')
            for dependency in candidate.dependencies:
                child = self.graph.node(dependency.owner)
                if child.environment_id != self.environment_id or child.interface != dependency.interface:
                    raise ValueError('Pending dependency differs from graph')
        for values in (self.allowed_pins, self.forbidden_names, self.cursors):
            items(values, str)
            if len(values) > 4096 or len(set(values)) != len(values):
                raise ValueError('Invalid snapshot names')
            for value in values:
                text(value, 1024)


@dataclass(frozen=True, slots=True)
class Work:
    operation: str
    owner: OwnerVersion
    candidate_id: str | None = None
    detail: bytes = b''

    def __post_init__(self):
        if self.operation not in ('generate', 'revise', 'review', 'review_stop', 'binding', 'proof', 'assemble',
                                   'children', 'admit_candidate', 'page', 'activate', 'joint'):
            raise ValueError('Unknown strategy work')
        instance(self.owner, OwnerVersion)
        if self.candidate_id is not None:
            from ..domain.records import uuid
            uuid(self.candidate_id)
        if type(self.detail) is not bytes or len(self.detail) > MAX_BYTES:
            raise ValueError('Work detail exceeds limit')


@dataclass(frozen=True, slots=True)
class Turn:
    """Saved alongside the exact wire; identities are supplied by the controller."""
    version: Version
    operation: str
    contract: str
    schema_hash: str
    wire_hash: str
    setup_hash: str | None
    base_id: str | None
    candidate_hash: str | None
    environment_id: str
    graph_revision: int
    stop_request_hash: str | None = None
    id: str = field(init=False)

    def __post_init__(self):
        instance(self.version, Version)
        identifier(self.environment_id, 'env_')
        natural(self.graph_revision, 'Dispatched graph revision')
        if self.operation not in ('generate', 'revise', 'review', 'review_stop'):
            raise ValueError('Invalid model operation')
        text(self.contract, 128)
        identifier(self.schema_hash, '')
        identifier(self.wire_hash, '')
        for value in (self.setup_hash, self.base_id, self.candidate_hash, self.stop_request_hash):
            if value is not None:
                identifier(value, '')
        if (self.operation == 'review') != (self.candidate_hash is not None):
            raise ValueError('Review requires exact candidate binding')
        if (self.operation == 'review_stop') != (self.stop_request_hash is not None):
            raise ValueError('Stop review requires exact request binding')
        if self.operation == 'revise' and self.base_id is None:
            raise ValueError('Revision requires an exact dispatched base')
        object.__setattr__(self, 'id', sha256(self.payload).hexdigest())

    @property
    def role(self):
        return 'reviewer' if self.operation in ('review', 'review_stop') else 'prover'

    @property
    def payload(self):
        return pack(['midas-strategy-turn-v2', self.version.value(), self.operation, self.contract,
                     self.schema_hash, self.wire_hash, self.setup_hash, self.base_id, self.candidate_hash,
                     self.environment_id, self.graph_revision, self.stop_request_hash])

    @classmethod
    def load(cls, payload):
        v = load_record(payload, limit=65536)
        if type(v) is not list or len(v) != 12 or v[0] != 'midas-strategy-turn-v2':
            raise ValueError('Invalid saved model turn')
        return cls(Version.load(v[1]), *v[2:])

    def validate_for(self, snapshot, *, continuation=False):
        if self.environment_id != snapshot.environment_id:
            raise ValueError('Dispatched environment hash changed')
        if continuation:
            if (self.version.owner, self.version.context) != (snapshot.version.owner, snapshot.version.context):
                raise ValueError('Stale continuation owner/context')
        elif self.version != snapshot.version:
            raise ValueError('Stale dispatched strategy snapshot')
        if self.base_id != (snapshot.base.id if snapshot.base else None):
            raise ValueError('Dispatched candidate base changed')
        current = candidate_hash(snapshot.attempt.candidate) if snapshot.attempt and self.operation == 'review' else None
        if current != self.candidate_hash:
            raise ValueError('Dispatched review candidate/interfaces changed')
        pending = snapshot.stop_request.id if snapshot.stop_request else None
        if pending != self.stop_request_hash:
            raise ValueError('Dispatched stop request changed')


@dataclass(frozen=True, slots=True)
class PreparedModel:
    turn: Turn
    wire: WireRequest

    def __post_init__(self):
        instance(self.turn, Turn)
        instance(self.wire, WireRequest)
        if (self.turn.role != self.wire.role or self.turn.contract != self.wire.contract
                or self.turn.wire_hash != sha256(self.wire.wire).hexdigest()
                or self.turn.setup_hash != (sha256(self.wire.setup).hexdigest() if self.wire.setup is not None else None)):
            raise ValueError('Prepared turn differs from rendered wire')


@dataclass(frozen=True, slots=True)
class Dispatch:
    turn: Turn
    request: ArtifactRef

    def __post_init__(self):
        instance(self.turn, Turn)
        instance(self.request, ArtifactRef)

    @classmethod
    def bind(cls, prepared, frozen):
        """Controller calls after saving provider request and exact input artifacts."""
        instance(prepared, PreparedModel)
        instance(frozen, FrozenRequest)
        turn, refs = prepared.turn, frozen.job.input_refs
        expected = (turn.wire_hash,) + ((turn.setup_hash,) if turn.setup_hash is not None else ())
        if (frozen.job.kind != JobKind.MODEL or frozen.job.owner != turn.version.owner
                or tuple(r.sha256 for r in refs) != expected
                or frozen.environment_id != turn.environment_id
                or frozen.job.expected_versions != prepared.wire.snapshot.owners):
            raise ValueError('Frozen provider request differs from prepared strategy turn')
        return cls(turn, frozen.request_ref)


@dataclass(frozen=True, slots=True)
class AttemptDecision:
    candidate_id: str
    outcome: str
    charge: int
    exception: str | None = None

    def __post_init__(self):
        from ..domain.records import uuid
        uuid(self.candidate_id)
        if type(self.charge) is not int or self.charge != Policy.mathematical_charge(self.outcome):
            raise ValueError('Attempt charge differs from outcome')
        if (self.outcome == 'exception') != (self.exception in ('timeout', 'memory', 'elaboration', 'protocol')):
            raise ValueError('Invalid attempt exception')


@dataclass(frozen=True, slots=True)
class Objection:
    owner: OwnerVersion
    category: str
    evidence: str
    source: ArtifactRef

    def __post_init__(self):
        instance(self.owner, OwnerVersion)
        instance(self.source, ArtifactRef)
        if self.category not in ('suspected_false', 'missing_assumption', 'context_mismatch'):
            raise ValueError('Unknown statement objection')
        text(self.evidence)


@dataclass(frozen=True, slots=True)
class StopRequest:
    owner: OwnerVersion
    rationale: str
    source: ArtifactRef
    objection: Objection | None = None

    def __post_init__(self):
        instance(self.owner, OwnerVersion)
        instance(self.source, ArtifactRef)
        text(self.rationale)
        if self.objection is not None:
            instance(self.objection, Objection)
            if self.objection.owner != self.owner or self.objection.source != self.source:
                raise ValueError('Stop objection differs from request')

    @property
    def payload(self):
        return pack(['midas-stop-request-v1', self.owner.obligation_id, self.owner.generation,
            self.rationale, self.source.to_dict(),
            [self.objection.category, self.objection.evidence] if self.objection else None])

    @property
    def id(self):
        return sha256(self.payload).hexdigest()

    @classmethod
    def load(cls, payload):
        v = load_record(payload, limit=65536)
        if type(v) is not list or len(v) != 6 or v[0] != 'midas-stop-request-v1':
            raise ValueError('Invalid stop request')
        owner, source = OwnerVersion(*v[1:3]), ArtifactRef.from_dict(v[4])
        if v[5] is not None and (type(v[5]) is not list or len(v[5]) != 2):
            raise ValueError('Invalid stop objection')
        objection = Objection(owner, *v[5], source) if v[5] is not None else None
        return cls(owner, v[3], source, objection)


@dataclass(frozen=True, slots=True)
class ArtifactData:
    """Bytes proposed for controller publication, without filesystem side effects."""
    data: bytes
    reference: ArtifactRef = field(init=False)

    def __post_init__(self):
        from uuid import UUID, uuid5
        if type(self.data) is not bytes or len(self.data) > MAX_BYTES:
            raise ValueError('Strategy artifact exceeds limit')
        digest = sha256(self.data).hexdigest()
        key = str(uuid5(UUID('25bc07e4-bbde-413b-baf7-d87d3cd28547'), digest))
        object.__setattr__(self, 'reference', ArtifactRef(key, digest, len(self.data)))


@dataclass(frozen=True, slots=True)
class Changes:
    """One atomic proposal. None means no replacement, never deletion.

    An AttemptDecision closes the pending attempt. Controller checks expected,
    publishes artifacts, records application identity once and commits all effects
    and downstream intents together. It cancels only after committing graph edits.
    """
    expected: Version
    work: tuple[Work, ...] = ()
    reason: str | None = None
    draft: Draft | None = None
    memory: Memory | None = None
    attempt: Attempt | None = None
    decision: AttemptDecision | None = None
    graph: ProofGraph | None = None
    graph_revision: int | None = None
    helper: Plan | None = None
    artifacts: tuple[ArtifactData, ...] = ()
    evidence: EvidenceChange | None = None
    page: Page | None = None
    objection: Objection | None = None
    repair_allowance: int | None = None
    revision_charge: str | None = None
    cancel_after_commit: tuple[OwnerVersion, ...] = ()
    stopped: bool | None = None
    stop_request: StopRequest | None = None

    def __post_init__(self):
        instance(self.expected, Version)
        items(self.work, Work)
        items(self.artifacts, ArtifactData)
        items(self.cancel_after_commit, OwnerVersion)
        if self.stopped is not None and type(self.stopped) is not bool:
            raise ValueError('Invalid stop change')
        if self.stop_request is not None:
            instance(self.stop_request, StopRequest)
            if self.stop_request.owner != self.expected.owner or self.stopped is not None:
                raise ValueError('Invalid pending stop change')
        if self.revision_charge not in (None, 'timeout', 'memory', 'elaboration', 'protocol'):
            raise ValueError('Invalid revision charge')
        if self.repair_allowance is not None:
            natural(self.repair_allowance, 'Repair allowance', positive=True)
            if self.graph is None:
                raise ValueError('Repair grant requires atomic graph retirement')
        if self.graph is not None and self.graph_revision is None:
            raise ValueError('Graph changes require an expected revision')
