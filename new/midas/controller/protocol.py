"""Small controller handoffs; semantic algorithms stay with their owning services."""
from dataclasses import dataclass
from typing import Callable

from ..domain import ArtifactRef, JobKind, OwnerVersion
from ..domain.records import instance, items, natural
from ..policy import AdmissionRequest, Failure
from ..storage import Changes, Intent


@dataclass(frozen=True, slots=True)
class Artifact:
    reference: ArtifactRef
    source: object  # Immutable bytes or a bounded binary stream, never JSON.


@dataclass(frozen=True, slots=True)
class Planned:
    intent: Intent
    admission: AdmissionRequest

    def __post_init__(self):
        instance(self.intent, Intent)
        instance(self.admission, AdmissionRequest)
        if self.admission.job != self.intent.request.job:
            raise ValueError('Admission differs from frozen intent')
        # Candidate/helper allowances belong to the atomic strategy proposal,
        # not individual transport attempts or retrieval continuation stages.
        if self.admission.candidate_mode:
            raise ValueError('Reserve candidate allowances in the proposal')


@dataclass(frozen=True, slots=True)
class Effect:
    changes: Changes = Changes()
    work: tuple[Planned, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    cancel: tuple[OwnerVersion, ...] = ()
    disposition: str = 'APPLIED'

    def __post_init__(self):
        instance(self.changes, Changes)
        for value, cls in ((self.work, Planned), (self.artifacts, Artifact), (self.cancel, OwnerVersion)):
            items(value, cls)
            if len(value) > 1000:
                raise ValueError('Controller proposal exceeds batch limit')
        if self.changes.intents:
            raise ValueError('Controller intents require saved admission metadata')
        if not self.disposition or len(self.disposition) > 256:
            raise ValueError('Invalid disposition')


@dataclass(frozen=True, slots=True)
class Accepted:
    value: object = None
    failure: Failure | None = None
    retry_after_ms: int = 0
    provider: str | None = None
    request_id: str | None = None

    def __post_init__(self):
        if self.failure is not None:
            instance(self.failure, Failure)
        natural(self.retry_after_ms, 'Retry after')


@dataclass(frozen=True, slots=True)
class Operation:
    """Trusted composition functions, installed once; no registration mechanism.

    admit(invocation, result, receipt, reader, authority) authenticates semantics.
    reduce(reader, invocation, accepted) returns an Effect using current versions.
    execute receives only the executor's restricted WorkerContext.
    """
    execute: Callable
    admit: Callable
    reduce: Callable
    progress: Callable | None = None
    retained: Callable | None = None
    route: Callable | None = None

    def __post_init__(self):
        for value in (self.execute, self.admit, self.reduce):
            if not callable(value):
                raise ValueError('Operation requires execution, admission and reduction')


@dataclass(frozen=True, slots=True)
class Operations:
    model: Operation | None = None
    binding: Operation | None = None
    proof: Operation | None = None
    lookup: Operation | None = None
    probe: Operation | None = None
    search: Operation | None = None
    assemble: Operation | None = None
    integrate: Operation | None = None
    export_verify: Operation | None = None

    def for_kind(self, kind):
        names = ('model', 'binding', 'proof', 'lookup', 'probe', 'search',
                 'assemble', 'integrate', 'export_verify')
        return getattr(self, dict(zip(JobKind, names))[kind])


@dataclass(frozen=True, slots=True)
class Tick:
    progress: int = 0
    received: int = 0
    applied: int = 0
    dispatched: int = 0
    waiting: tuple[tuple[str, str], ...] = ()
    stopped: bool = False
