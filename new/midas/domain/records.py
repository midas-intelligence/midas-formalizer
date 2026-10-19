"""Immutable shared identities, candidates, and execution protocol records."""

import re

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Protocol
from uuid import UUID


class DomainError(ValueError):
    """A value or proposed transition violates a domain invariant."""


def natural(value: int, label: str, *, positive: bool = False) -> None:
    if type(value) is not int or not (1 if positive else 0) <= value < 2**63:
        raise DomainError(f"{label} must be a {'positive' if positive else 'nonnegative'} signed-64-bit integer")


def text(value: str, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise DomainError(f"{label} must be a nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise DomainError(f"{label} must be valid UTF-8") from error


def identifier(value: str, prefix: str) -> None:
    if type(value) is not str or re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", value) is None:
        raise DomainError(f"Expected a {prefix or 'SHA-256 '}identity")


def uuid(value: str) -> None:
    try:
        valid = type(value) is str and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise DomainError("Expected a canonical UUID")


def instance(value: object, expected: type) -> None:
    if not isinstance(value, expected):
        raise DomainError(f"Expected {expected.__name__}")


def items(value: tuple, expected: type) -> None:
    if type(value) is not tuple or any(not isinstance(item, expected) for item in value):
        raise DomainError(f"Expected an immutable tuple of {expected.__name__}")


def obligation_id(canonical_envelope: bytes) -> str:
    """Hash an existing canonical obligation envelope.

    The caller supplies the admitted Lean envelope bytes. This function does not
    parse Lean, canonicalize source text, or establish that a string is a Prop.
    """
    if type(canonical_envelope) is not bytes or not canonical_envelope:
        raise DomainError("Expected nonempty canonical obligation envelope bytes")
    return "obl_" + sha256(canonical_envelope).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    id: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        uuid(self.id)
        identifier(self.sha256, "")
        natural(self.byte_count, "Artifact byte count")

    def verify(self, payload: bytes, *, max_bytes: int) -> bytes:
        natural(max_bytes, "Artifact read limit")
        if (type(payload) is not bytes or self.byte_count > max_bytes
                or len(payload) != self.byte_count or sha256(payload).hexdigest() != self.sha256):
            raise DomainError("Artifact bytes differ from the reference or exceed the read limit")
        return payload

    def to_dict(self) -> dict:
        # Preserve the established artifact-reference wire shape.
        return {"id": self.id, "sha256": self.sha256, "bytes": self.byte_count}

    @classmethod
    def from_dict(cls, value: dict) -> "ArtifactRef":
        if type(value) is not dict or set(value) != {"id", "sha256", "bytes"}:
            raise DomainError("Invalid artifact reference fields")
        return cls(value["id"], value["sha256"], value["bytes"])


@dataclass(frozen=True, slots=True)
class OwnerVersion:
    obligation_id: str
    generation: int

    def __post_init__(self) -> None:
        identifier(self.obligation_id, "obl_")
        natural(self.generation, "Owner generation")


def versions(value: tuple[OwnerVersion, ...]) -> None:
    items(value, OwnerVersion)
    if len({owner.obligation_id for owner in value}) != len(value):
        raise DomainError("Duplicate expected owner version")


@dataclass(frozen=True, slots=True)
class Session:
    run_id: str
    session_id: str
    epoch: int

    def __post_init__(self) -> None:
        uuid(self.run_id)
        uuid(self.session_id)
        natural(self.epoch, "Session epoch", positive=True)


@dataclass(frozen=True, slots=True)
class Usage:
    """One invocation's cumulative usage; None is unknown, including after failure."""

    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None

    def __post_init__(self) -> None:
        for label in ("input_tokens", "cache_tokens", "output_tokens", "cost_microusd"):
            value = getattr(self, label)
            if value is not None:
                natural(value, label)


class DependencyOrigin(str, Enum):
    ABSTRACTED_LEMMA = 'abstracted_lemma'
    FROZEN_PEER = 'frozen_peer'
    CLOSED_DEFINITION = 'closed_definition'


@dataclass(frozen=True, slots=True)
class Dependency:
    owner: OwnerVersion
    alias: str
    interface: ArtifactRef
    origin: DependencyOrigin = DependencyOrigin.ABSTRACTED_LEMMA
    certificate: ArtifactRef | None = None
    submission_id: str | None = None

    def __post_init__(self) -> None:
        instance(self.owner, OwnerVersion)
        text(self.alias, "Dependency alias")
        instance(self.interface, ArtifactRef)
        instance(self.origin, DependencyOrigin)
        if self.origin == DependencyOrigin.CLOSED_DEFINITION:
            instance(self.certificate, ArtifactRef)
            uuid(self.submission_id)
        elif self.certificate is not None or self.submission_id is not None:
            raise DomainError('Only closed definitions carry submission-scoped certificates')


@dataclass(frozen=True, slots=True)
class Candidate:
    """An admitted candidate's exact inputs; its existence says nothing about validity.

    Source/name syntax and external submission decoding belong at their own
    boundaries. Dependencies retain their authored order and checked interfaces.
    """

    id: str
    owner: OwnerVersion
    environment_id: str
    source: ArtifactRef
    dependencies: tuple[Dependency, ...] = ()
    revision_of: str | None = None
    joint_submission_id: str | None = None

    def __post_init__(self) -> None:
        uuid(self.id)
        instance(self.owner, OwnerVersion)
        identifier(self.environment_id, "env_")
        instance(self.source, ArtifactRef)
        items(self.dependencies, Dependency)
        if self.joint_submission_id is not None:
            uuid(self.joint_submission_id)
        aliases = set()
        owners = {}
        for dependency in self.dependencies:
            key = dependency.owner.obligation_id
            if key == self.owner.obligation_id:
                raise DomainError("A candidate cannot depend on itself")
            if dependency.alias in aliases:
                raise DomainError("Duplicate dependency alias")
            aliases.add(dependency.alias)
            binding = (dependency.owner, dependency.interface, dependency.origin,
                       dependency.certificate, dependency.submission_id)
            if key in owners and owners[key] != binding:
                raise DomainError("One dependency has conflicting versions or interfaces")
            owners[key] = binding
            if (dependency.origin == DependencyOrigin.CLOSED_DEFINITION
                    and dependency.submission_id != self.joint_submission_id):
                raise DomainError('Definition certificate belongs to another submission')
        if self.revision_of is not None:
            uuid(self.revision_of)
            if self.revision_of == self.id:
                raise DomainError("A candidate cannot revise itself")

    @property
    def logical_dependencies(self) -> tuple[Dependency, ...]:
        return tuple(dep for dep in self.dependencies if dep.origin != DependencyOrigin.CLOSED_DEFINITION)

    def validate_revision(self, previous: "Candidate") -> None:
        instance(previous, Candidate)
        if (self.revision_of != previous.id or self.owner != previous.owner
                or self.environment_id != previous.environment_id):
            raise DomainError("Revision differs from the prior candidate's owner or environment")


class JobKind(str, Enum):
    MODEL = "MODEL"
    CHECK_BINDING = "CHECK_BINDING"
    CHECK_PROOF = "CHECK_PROOF"
    LOOKUP = "LOOKUP"
    PROBE = "PROBE"
    SEARCH = "SEARCH"
    ASSEMBLE = "ASSEMBLE"
    INTEGRATE = "INTEGRATE"
    EXPORT_VERIFY = "EXPORT_VERIFY"
    NETWORK_SEARCH = "NETWORK_SEARCH"


class ResultStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DeliveryCertainty(str, Enum):
    NOT_SENT = "NOT_SENT"
    DELIVERED = "DELIVERED"
    UNKNOWN = "UNKNOWN"


class Termination(str, Enum):
    TERMINATED = "TERMINATED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    wall_ms: int
    memory_mib: int
    output_bytes: int
    evidence_bytes: int
    max_heartbeat: int = 2_000_000
    max_rec_depth: int = 1000

    def __post_init__(self) -> None:
        for field in ("wall_ms", "memory_mib", "output_bytes", "evidence_bytes"):
            natural(getattr(self, field), field, positive=True)
        natural(self.max_heartbeat, "max_heartbeat")
        natural(self.max_rec_depth, "max_rec_depth", positive=True)


@dataclass(frozen=True, slots=True)
class Job:
    semantic_key: str
    kind: JobKind
    owner: OwnerVersion
    input_refs: tuple[ArtifactRef, ...]
    expected_versions: tuple[OwnerVersion, ...]
    limits: ResourceLimits
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        text(self.semantic_key, "Job semantic key")
        instance(self.kind, JobKind)
        instance(self.owner, OwnerVersion)
        items(self.input_refs, ArtifactRef)
        versions(self.expected_versions)
        if self.owner not in self.expected_versions:
            raise DomainError("Job must fence its owner version")
        instance(self.limits, ResourceLimits)
        items(self.depends_on, str)
        for key in self.depends_on:
            text(key, "Prerequisite job key")
        if len(set(self.depends_on)) != len(self.depends_on) or self.semantic_key in self.depends_on:
            raise DomainError("Duplicate or self-dependent job")


@dataclass(frozen=True, slots=True)
class FrozenRequest:
    job: Job
    environment_id: str
    operation_payload: ArtifactRef
    request_ref: ArtifactRef

    def __post_init__(self) -> None:
        instance(self.job, Job)
        identifier(self.environment_id, "env_")
        instance(self.operation_payload, ArtifactRef)
        instance(self.request_ref, ArtifactRef)

    @property
    def request_hash(self) -> str:
        return self.request_ref.sha256


@dataclass(frozen=True, slots=True)
class Invocation:
    original_execution_id: str
    session: Session
    request: FrozenRequest
    reservation_ref: ArtifactRef

    def __post_init__(self) -> None:
        uuid(self.original_execution_id)
        instance(self.session, Session)
        instance(self.request, FrozenRequest)
        instance(self.reservation_ref, ArtifactRef)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    invocation_id: str
    request_hash: str
    status: ResultStatus
    staged_artifacts: tuple[ArtifactRef, ...]
    output: ArtifactRef | None
    diagnostics: ArtifactRef | None
    usage: Usage
    delivery_certainty: DeliveryCertainty
    termination: Termination

    def __post_init__(self) -> None:
        uuid(self.invocation_id)
        identifier(self.request_hash, "")
        instance(self.status, ResultStatus)
        items(self.staged_artifacts, ArtifactRef)
        if len({ref.id for ref in self.staged_artifacts}) != len(self.staged_artifacts):
            raise DomainError("Duplicate staged artifact ID")
        for ref in (self.output, self.diagnostics):
            if ref is not None:
                instance(ref, ArtifactRef)
                if ref not in self.staged_artifacts:
                    raise DomainError("Worker output must name an exact staged artifact")
        instance(self.usage, Usage)
        instance(self.delivery_certainty, DeliveryCertainty)
        instance(self.termination, Termination)
        if self.status == ResultStatus.SUCCEEDED and self.delivery_certainty != DeliveryCertainty.DELIVERED:
            raise DomainError("Successful work requires confirmed delivery")

    def validate_for(self, invocation: Invocation) -> None:
        instance(invocation, Invocation)
        if (self.invocation_id != invocation.original_execution_id
                or self.request_hash != invocation.request.request_hash):
            raise DomainError("Worker result differs from its invocation or exact request")


@dataclass(frozen=True, slots=True)
class ReceiptBinding:
    """Exact receipt provenance supplied by trusted admission, never by a model.

    Construction only validates shape. The checker/export admission boundary
    must verify the controller's seal and exact request/result bytes before
    constructing an admitted semantic result that carries this value.
    """

    original_execution_id: str
    request_ref: ArtifactRef
    result_ref: ArtifactRef
    seal_ref: ArtifactRef

    def __post_init__(self) -> None:
        uuid(self.original_execution_id)
        for ref in (self.request_ref, self.result_ref, self.seal_ref):
            instance(ref, ArtifactRef)


class ArtifactReader(Protocol):
    """A bounded, verified read capability; deliberately has no write methods."""

    def read_artifact(self, reference: ArtifactRef, *, max_bytes: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CapacityGrant:
    """Controller handoff after reservation commit; construction is not authority.

    A retained worker transfers its existing footprint into this invocation's
    reservation. Policy admission excludes that worker from retained memory.
    """
    invocation: Invocation
    retained_worker_id: str | None = None

    def __post_init__(self):
        instance(self.invocation, Invocation)
        if self.retained_worker_id is not None:
            uuid(self.retained_worker_id)

    def validate_for(self, invocation: Invocation):
        if self.invocation != invocation:
            raise DomainError('Capacity grant differs from the committed invocation')


@dataclass(frozen=True, slots=True)
class WorkerProgress:
    """Small progress metadata; evidence travels only as bounded artifact refs."""
    invocation_id: str
    sequence: int
    kind: str
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self):
        uuid(self.invocation_id)
        natural(self.sequence, 'Progress sequence')
        text(self.kind, 'Progress kind')
        if len(self.kind) > 128:
            raise DomainError('Progress kind exceeds 128 characters')
        items(self.artifacts, ArtifactRef)
        if len(self.artifacts) > 16:
            raise DomainError('Progress has too many artifact references')


class ArtifactStaging(Protocol):
    """Private, bounded worker writes; publication remains controller/storage-owned."""
    def write_bounded(self, data: bytes, *, max_bytes: int) -> ArtifactRef: ...
