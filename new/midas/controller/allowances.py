"""Translate policy allowances to atomic owner counters and immutable pointers."""
from dataclasses import fields
from hashlib import sha256
from uuid import UUID, uuid5

from ..domain import ArtifactRef
from ..policy import OwnerUsage
from ..storage import Changes, CounterChange, RecordChange
from . import records
from .protocol import Artifact, Effect


def artifact(data):
    digest = sha256(data).hexdigest()
    key = uuid5(UUID('25bc07e4-bbde-413b-baf7-d87d3cd28547'), digest)
    return Artifact(ArtifactRef(str(key), digest, len(data)), data)


def scope(owner):
    # Mathematics counters are lifetime counters across owner generations.
    return 'owner:'+owner.obligation_id


def owner_usage(reader, owner):
    flags = ('pending_candidate', 'repair_open', 'repair_suspended', 'retrieval_disabled')
    return OwnerUsage(**{f.name: bool(value) if f.name in flags else value for f in fields(OwnerUsage)
                        for value in (reader.counter(scope(owner), f.name),)})


def _changes(reader, owner, **deltas):
    return tuple(CounterChange(scope(owner), key, reader.counter(scope(owner), key),
                               reader.counter(scope(owner), key)+value) for key, value in deltas.items())


def reserve_candidate(reader, policy, owner, candidate_id, mode):
    """Compose this effect with the first candidate checks, then commit once."""
    usage = owner_usage(reader, owner)
    decision = policy.candidate(mode, usage)
    if not decision.allowed:
        raise ValueError(decision.reason)
    repair = getattr(usage, mode) >= getattr(policy.config.mathematics, mode)
    data = artifact(records.pack([candidate_id, mode, repair]))
    deltas = {mode: 1, 'pending_candidate': 1}
    if repair:
        deltas['repair_remaining'] = -1
    return Effect(Changes(expected=(owner,), counters=_changes(reader, owner, **deltas),
        records=(records.pointer(reader, scope(owner), 'candidate', data.reference),)), artifacts=(data,))


def settle_candidate(reader, owner, decision):
    """Close the exact pending candidate; only mathematical outcomes keep its charge."""
    from ..strategy import AttemptDecision
    if not isinstance(decision, AttemptDecision):
        raise ValueError('Expected a strategy attempt decision')
    pending = records.record(reader, scope(owner), 'candidate')
    if pending is None or pending[0] != decision.candidate_id:
        raise ValueError('Attempt decision differs from reserved candidate')
    deltas = {'pending_candidate': -1}
    if not decision.charge:
        deltas[pending[1]] = -1
        if pending[2]:
            deltas['repair_remaining'] = 1
    if decision.exception:
        deltas[decision.exception] = 1
    return Effect(Changes(expected=(owner,), counters=_changes(reader, owner, **deltas),
        records=(records.pointer(reader, scope(owner), 'candidate', None),)))


def reserve_helper(reader, policy, owner, plan, services):
    """One request charge shared by all stages; continuation plans charge nothing."""
    if plan.charge is None:
        return Effect()
    if plan.request.scope.owner != owner:
        raise ValueError('Helper allowance owner differs from plan')
    data = artifact(plan.request.payload)
    key = 'helper:'+plan.request.id
    existing = reader.record(scope(owner), key)
    if existing is not None:
        if existing.artifact != data.reference:
            raise ValueError('Helper request identity changed')
        return Effect()
    decision = policy.helper(plan.charge, owner_usage(reader, owner), services, units=plan.units)
    if not decision.allowed:
        raise ValueError(decision.reason)
    counter = 'research' if plan.charge == 'research' else 'probe' if plan.charge == 'probe' else 'retrieval'
    return Effect(Changes(expected=(owner,), counters=_changes(reader, owner, **{counter: plan.units}),
        records=(RecordChange(scope(owner), key, None, data.reference),)), artifacts=(data,))
