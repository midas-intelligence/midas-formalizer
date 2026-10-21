"""Lower strategy objections and retirements into one fenced controller effect."""
from dataclasses import replace

from ..controller import Effect, artifact, settle_candidate
from ..domain import OwnerVersion
from ..storage import Changes, CounterChange
from ..strategy import AttemptDecision, Memory, Observation, Objection, Repair, consumers, reopen, update_memory
from . import candidates, records as r


def report(flow, reader, objection):
    data = [r.owner(objection.owner), objection.category, objection.evidence, r.ref(objection.source)]
    effects = [r.record(reader, 'application.objections', objection.owner.obligation_id, data)]
    for repair in consumers(flow.graph(reader), objection):
        effects.append(r.record(reader, 'application.repairs', repair.owner.obligation_id,
                                [r.owner(repair.owner), repair.candidate_id, data]))
    return r.merge(*effects)


def plan(flow, reader, owner):
    saved = r.value(reader, 'application.repairs', owner.obligation_id)
    if saved is None:
        return None
    old_owner, candidate, report = saved
    objection = Objection(OwnerVersion(*report[0]), report[1], report[2], r.unref(report[3]))
    repair = Repair(OwnerVersion(*old_owner), candidate, objection)
    snapshot = flow.snapshot(reader, owner)
    proposal = reopen(snapshot, flow.policy, repair)
    if proposal.reason == 'mathematical_candidate_pending':
        return None
    effects = [r.record(reader, 'application.repairs', owner.obligation_id, None)]
    if proposal.graph is None:
        return r.merge(*effects)
    effects.append(r.graph_changes(reader, snapshot.graph, proposal.graph))
    _, state = r.owner_state(reader, owner.obligation_id)
    memory = Memory.load(r.read(reader, state.memory)) if state.memory else Memory()
    memory = update_memory(memory, observation=Observation.select('objection', objection.category,
        objection.source, (objection.evidence,)))
    data = artifact(memory.payload)
    effects.extend((Effect(artifacts=(data,)), r.save_owner(reader, owner.obligation_id,
        replace(state, memory=data.reference, stopped=False, reason=None))))
    for old in proposal.cancel_after_commit:
        _, child = r.owner_state(reader, old.obligation_id)
        if child.submission:
            submission = candidates.submission(reader, child.submission)
            effects.extend((settle_candidate(reader, old, AttemptDecision(submission['attempt'], 'abandoned', 0)),
                r.record(reader, 'application.submissions', child.submission, dict(submission, phase='failed', current=None))))
        effects.extend((r.save_owner(reader, old.obligation_id, replace(child, submission=None, stopped=True)),
                        r.record(reader, 'application.pending', old.obligation_id, None)))
    scope = 'owner:'+owner.obligation_id
    changes = []
    for key, amount in (('backtracks', 1), ('repair_open', 1), ('repair_remaining', proposal.repair_allowance)):
        before = reader.counter(scope, key)
        changes.append(CounterChange(scope, key, before, before+amount))
    before = reader.counter(r.SCOPE, 'backtracks')
    changes.append(CounterChange(r.SCOPE, 'backtracks', before, before+1))
    effects.extend((Effect(Changes(counters=tuple(changes)), cancel=proposal.cancel_after_commit),
                    r.record(reader, r.SCOPE, 'retired', list(proposal.graph.retired_candidates))))
    return r.merge(*effects)
