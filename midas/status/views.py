"""Read-only projections. Application supplies its workflow and export facts."""
from dataclasses import dataclass

from ..policy import Completion, Progress, Stage
from ..storage import Status


@dataclass(frozen=True, slots=True)
class Inspection:
    run_id: str
    workflow: str
    status: Status
    completion: Completion
    progress: Progress
    environment_id: str
    turn_limit: int
    runtime_ms: int
    model_slots: int
    compiler_slots: int
    node_counts: tuple | None = None
    discarded_nodes: int = 0


@dataclass(frozen=True, slots=True)
class NodePage:
    revision: int
    items: tuple
    after: str | None
    details: 'NodeDetails | None' = None


@dataclass(frozen=True, slots=True)
class NodeDetails:
    id: str
    stopped: bool
    reason: str | None
    submission: str | None
    artifacts: tuple
    cause: str | None = None


def progress(reader, *, committed=None, integration=Stage.PENDING, verification=Stage.PENDING,
             local_ready=0, reasons=(), allow_sorry=False):
    committed = committed or reader.status_snapshot()
    roots = dict(reader.root_counts())
    counts = dict(committed.intents)
    usage = committed.accounting
    return Progress(sum(roots.values()), roots.get('VERIFIED', 0), integration, verification,
        active=usage.models+usage.compilers+usage.locals+usage.networks, ready=counts.get('PENDING', 0)+local_ready,
        waiting=counts.get('RUNNING', 0), elapsed_ms=reader.counter('controller', 'elapsed_ms'),
        stopped=bool(reader.counter('controller', 'stopped')), recovery_required=reader.recovery_pending(),
        reasons=tuple(dict.fromkeys(reasons))[:20], allow_sorry=allow_sorry)


def project(reader, policy, *, workflow, progress, committed=None, completion=None, node_counts=None):
    config = policy.config
    committed = committed or reader.status_snapshot()
    discarded = sum(n for _, n in committed.nodes)-sum(n for _, n in node_counts) if node_counts is not None else 0
    return Inspection(reader.run_id, workflow, committed,
        completion or policy.completion(progress), progress, reader.frozen_run().environment_id,
        config.run.turns, config.run.runtime_ms,
        config.scheduling.model_concurrency, config.scheduling.compiler_concurrency, node_counts, discarded)


def nodes(reader, *, after='', limit=100, state=None, owner=None, roots=False):
    with reader.snapshot():
        rows = reader.node_summaries(after=after, limit=limit, state=state, owner=owner, roots=roots)
        cursor = None
        if rows and owner is None and reader.node_summaries(after=rows[-1].owner.obligation_id,
                limit=1, state=state, roots=roots):
            cursor = rows[-1].owner.obligation_id
        return NodePage(reader.status_snapshot().revision, rows, cursor)
