"""Explicit statement objections reopen only their current direct consumers."""
from dataclasses import dataclass

from ..domain import DomainError, OwnerVersion, ProofState, retire_plan
from ..domain.records import instance, uuid
from .memory import pack
from .state import Changes, Objection, Work


@dataclass(frozen=True, slots=True)
class Repair:
    owner: OwnerVersion
    candidate_id: str
    report: Objection

    def __post_init__(self):
        instance(self.owner, OwnerVersion)
        uuid(self.candidate_id)
        instance(self.report, Objection)


def consumers(graph, report):
    """Snapshot fan-out, not a subscription for future plans or grandparents."""
    instance(report, Objection)
    try:
        graph.node(report.owner)
    except DomainError:
        return ()
    live = graph.reachable()
    if report.owner.obligation_id not in live:
        return ()
    return tuple(Repair(node.owner, node.selected.candidate.id, report) for node in graph.nodes
        if node.owner.obligation_id in live and node.state == ProofState.CONDITIONAL
        and any(dep.owner == report.owner for dep in node.selected.candidate.logical_dependencies))


def reopen(snapshot, policy, repair):
    """Propose retirement, one policy-sized repair grant and fresh work atomically."""
    instance(repair, Repair)
    if repair.owner != snapshot.version.owner:
        raise ValueError('Repair differs from current owner')
    if repair not in consumers(snapshot.graph, repair.report):
        return Changes(snapshot.version, reason='obsolete_consumer_plan')
    if snapshot.attempt is not None or snapshot.busy:
        return Changes(snapshot.version, reason='mathematical_candidate_pending')
    decision = policy.backtrack(snapshot.usage, snapshot.run)
    if not decision.allowed:
        return Changes(snapshot.version, reason=decision.reason)
    if snapshot.run.intents >= policy.config.run.max_intents:
        return Changes(snapshot.version, reason='lifetime_intent_allowance_exhausted')
    retirement = retire_plan(snapshot.graph, repair.owner, repair.candidate_id)
    detail = pack([repair.report.source.to_dict(), repair.report.category, repair.report.evidence])
    work = () if snapshot.workflow == 'supplied' else (Work('revise' if snapshot.base else 'generate',
                                                        repair.owner, detail=detail),)
    return Changes(snapshot.version, work, reason='supplied_candidate_required' if not work else None,
        graph=retirement.graph, graph_revision=retirement.expected_revision,
        repair_allowance=policy.config.mathematics.repair,
        cancel_after_commit=retirement.cancel_after_commit, stopped=False)
