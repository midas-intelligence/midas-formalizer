"""Bounded storage values. Policy and semantic acceptance remain with callers."""
from dataclasses import dataclass

from ..domain import ArtifactRef, FrozenRequest, Invocation, OwnerVersion, ProofNode, ReceiptBinding, Usage
from ..domain.records import identifier, instance, items, natural, text, uuid

PAGE_LIMIT = 1000
META_LIMIT = 256 * 1024
USAGE_FIELDS = ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')


class StorageError(RuntimeError):
    pass


class RunLocked(StorageError):
    pass


class StaleSession(StorageError):
    pass


class Conflict(StorageError):
    pass


def name(value):
    text(value, 'Storage key')
    if len(value.encode()) > 1024:
        raise ValueError('Storage key exceeds 1024 bytes')


def page_limit(value):
    if type(value) is not int or not 1 <= value <= PAGE_LIMIT:
        raise ValueError(f'Page limit must be 1 through {PAGE_LIMIT}')
    return value


@dataclass(frozen=True, slots=True)
class FrozenRun:
    environment_id: str
    inputs: ArtifactRef
    configuration: ArtifactRef
    parent_run_id: str | None = None
    parent_revision: int | None = None

    def __post_init__(self):
        identifier(self.environment_id, 'env_')
        instance(self.inputs, ArtifactRef)
        instance(self.configuration, ArtifactRef)
        if (self.parent_run_id is None) != (self.parent_revision is None):
            raise ValueError('Lineage requires both parent run and revision')
        if self.parent_run_id is not None:
            uuid(self.parent_run_id)
            natural(self.parent_revision, 'Parent revision')


@dataclass(frozen=True, slots=True)
class NodeVersion:
    owner: OwnerVersion
    revision: int

    def __post_init__(self):
        instance(self.owner, OwnerVersion)
        natural(self.revision, 'Node revision')


@dataclass(frozen=True, slots=True)
class SavedNode:
    version: NodeVersion
    node: ProofNode


@dataclass(frozen=True, slots=True)
class NodeSummary:
    owner: OwnerVersion
    revision: int
    state: str
    interface: ArtifactRef
    target: int | None


@dataclass(frozen=True, slots=True)
class NodeChange:
    expected: NodeVersion | None
    node: ProofNode

    def __post_init__(self):
        instance(self.node, ProofNode)
        if self.expected is not None:
            instance(self.expected, NodeVersion)
            before, after = self.expected.owner, self.node.owner
            if before.obligation_id != after.obligation_id or after.generation not in (before.generation, before.generation + 1):
                raise ValueError('Node change must preserve identity and advance at most one generation')
        elif self.node.owner.generation != 0:
            raise ValueError('A new node starts at generation zero')


@dataclass(frozen=True, slots=True)
class RecordChange:
    """Versioned artifact pointer; None removes the current value, retaining history."""
    scope: str
    key: str
    expected: int | None
    artifact: ArtifactRef | None

    def __post_init__(self):
        name(self.scope)
        name(self.key)
        if self.expected is not None:
            natural(self.expected, 'Record revision')
        if self.artifact is not None:
            instance(self.artifact, ArtifactRef)


@dataclass(frozen=True, slots=True)
class SavedRecord:
    scope: str
    key: str
    revision: int
    artifact: ArtifactRef | None


@dataclass(frozen=True, slots=True)
class CounterChange:
    scope: str
    key: str
    expected: int
    value: int

    def __post_init__(self):
        name(self.scope)
        name(self.key)
        natural(self.expected, 'Expected counter')
        natural(self.value, 'New counter')


@dataclass(frozen=True, slots=True)
class Intent:
    request: FrozenRequest
    priority: int = 0
    not_before_ms: int = 0

    def __post_init__(self):
        instance(self.request, FrozenRequest)
        name(self.request.job.semantic_key)
        natural(self.priority, 'Priority')
        natural(self.not_before_ms, 'Not before')
        if max(len(self.request.job.depends_on), len(self.request.job.input_refs),
               len(self.request.job.expected_versions)) > PAGE_LIMIT:
            raise ValueError('Intent exceeds metadata allowance')
        for key in self.request.job.depends_on:
            name(key)


@dataclass(frozen=True, slots=True)
class Changes:
    """One atomic, deduplicated application of already validated effects."""
    expected: tuple[OwnerVersion, ...] = ()
    nodes: tuple[NodeChange, ...] = ()
    records: tuple[RecordChange, ...] = ()
    counters: tuple[CounterChange, ...] = ()
    intents: tuple[Intent, ...] = ()
    roots: tuple[str, ...] | None = None
    graph_revision: int | None = None

    def __post_init__(self):
        for value, cls in ((self.expected, OwnerVersion), (self.nodes, NodeChange),
                           (self.records, RecordChange), (self.counters, CounterChange), (self.intents, Intent)):
            items(value, cls)
            if len(value) > PAGE_LIMIT:
                raise ValueError('Application exceeds batch allowance')
        for keys in ([o.obligation_id for o in self.expected],
                     [c.node.owner.obligation_id for c in self.nodes],
                     [(r.scope, r.key) for r in self.records],
                     [(c.scope, c.key) for c in self.counters],
                     [i.request.job.semantic_key for i in self.intents]):
            if len(keys) != len(set(keys)):
                raise ValueError('Duplicate application key')
        if self.roots is not None:
            items(self.roots, str)
            if len(self.roots) > PAGE_LIMIT or len(set(self.roots)) != len(self.roots):
                raise ValueError('Invalid graph roots')
            for root in self.roots:
                identifier(root, 'obl_')
        if self.nodes or self.roots is not None:
            natural(self.graph_revision, 'Expected graph revision')


@dataclass(frozen=True, slots=True)
class Accounting:
    turns: int
    known: Usage
    unknown: Usage
    reserved: Usage
    models: int
    compilers: int
    locals: int
    compiler_memory_mib: int
    networks: int = 0

    @property
    def actual(self):
        return Usage(*(None if getattr(self.unknown, k) else getattr(self.known, k) for k in USAGE_FIELDS))


@dataclass(frozen=True, slots=True)
class Status:
    revision: int
    graph_revision: int
    nodes: tuple[tuple[str, int], ...]
    intents: tuple[tuple[str, int], ...]
    executions: tuple[tuple[str, int], ...]
    accounting: Accounting


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    status: Status
    nodes: tuple[SavedNode, ...]
    records: tuple[SavedRecord | None, ...]
    counters: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ExecutionState:
    invocation: Invocation
    phase: str
    charged: bool
    held: bool
    bounds: Usage
    usage: Usage
    receipt: ReceiptBinding | None
    disposition: str | None
    provider: str | None
    provider_request: str | None
    adopted_session_id: str | None
