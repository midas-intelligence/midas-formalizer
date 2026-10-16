"""Durable submission stages: bind leaves, check proofs, review and activate."""
from dataclasses import replace
from uuid import UUID, uuid5

from ..controller import Effect, artifact, reserve_candidate, settle_candidate
from ..domain import (Candidate, CheckKind, Dependency, DependencyOrigin, OwnerVersion,
    ProofNode, accept_checked_proof, accept_joint,
    joint_targets, load_identity, load_submission, resolve_entry)
from ..domain.identity import entry_context, CLOSED
from ..domain import encoding as codec
from ..storage import Changes, CounterChange, NodeChange
from ..strategy import AttemptDecision, Context, Draft, child_context
from . import records as r


def identity(reader, state, environment_id):
    return load_identity(r.read(reader, state.identity, r.DATA), environment_id=environment_id)


def prerequisites(reader, context, environment_id):
    for prerequisite in context.prerequisites:
        reference = prerequisite.reference
        row = submission(reader, reference.submission_id)['rows'][reference.key]
        child = reader.node(row['owner']).node
        if child.owner != OwnerVersion(*row['version']) or child.binding is None:
            raise ValueError('Prerequisite is no longer bound')
        _, state = r.owner_state(reader, child.owner.obligation_id)
        yield prerequisite.parameter, child, identity(reader, state, environment_id)


def submission(reader, key):
    return r.value(reader, 'application.submissions', key)


def entries(reader, saved):
    draft = Draft.load(r.read(reader, r.unref(saved['draft']), 17 * 1024**2))
    value = load_submission(draft.submission)
    return draft, value, {e['key']: e for e in value['entries']}


def begin(flow, reader, owner, draft, *, supplied=False, state=None):
    _, current = r.owner_state(reader, owner.obligation_id)
    state = state or current
    if current.submission is not None:
        raise ValueError('Another candidate is pending for this owner')
    value = load_submission(draft.submission)
    key = value['submission_id']
    if submission(reader, key) is not None:
        raise ValueError('Submission identity was already used')
    attempt = str(uuid5(UUID(key), 'attempt'))
    data = artifact(draft.payload)
    saved = dict(owner=r.owner(owner), draft=r.ref(data.reference), supplied=supplied, attempt=attempt,
                 order=[k for k, _ in draft.checks()], rows={}, phase='binding', current=None)
    return r.merge(reserve_candidate(reader, flow.policy, owner, attempt, draft.mode),
        r.record(reader, 'application.submissions', key, saved),
        r.save_owner(reader, owner.obligation_id, replace(state, base=data.reference, submission=key, reason=None)),
        Effect(artifacts=(data,)))


def _index(flow, entry):
    subject = entry['subject']
    name = subject.get('scope_target', subject['name'])
    return next(i for i, t in enumerate(flow.selection.selected.targets) if t.selector == name)


def plan(flow, reader, key, saved):
    """A submission advances one ready stage at a time; stages survive restart."""
    if saved['phase'] in ('done', 'failed'):
        return None
    if saved['current']:
        return None
    draft, value, by_key = entries(reader, saved)
    coordinator = OwnerVersion(*saved['owner'])
    for entry_key in saved['order']:
        if entry_key in saved['rows']:
            continue
        entry = resolve_entry(value, by_key[entry_key])
        index = _index(flow, entry)
        scope_owner = reader.node(flow.selection.identities[index].id).node.owner
        request = flow.checker.prepare('binding', flow.selection, index, scope_owner,
            statement=entry['subject'].get('statement'),
            parameters=tuple(d['parameter'] for d in entry['dependencies']),
            context=entry['subject'].get('declaration_context', CLOSED),
            imports=tuple(entry.get('imports', ())))
        job_key = f'candidate:{key}:binding:{entry_key}'
        return r.merge(flow.compiler_job(reader, job_key, request, (coordinator,),
            ['binding', key, entry_key]), r.record(reader, 'application.submissions', key,
                dict(saved, current=job_key)))
    # All identities exist before any candidate or dependency is constructed.
    for entry_key in saved['order']:
        row = saved['rows'][entry_key]
        if row.get('done'):
            continue
        if entry_key in draft.unproved:
            return _row(reader, key, saved, entry_key, dict(row, done=True))
        checked = r.uncheck(row.get('checked'))
        if checked:
            if checked.kind == CheckKind.CONDITIONAL:
                if saved['supplied']:
                    # Supplied approval is explicit input. Its provenance is the
                    # compiler receipt binding the exact saved candidate source.
                    from ..domain import AdmittedReview
                    review = AdmittedReview(checked.candidate, checked.receipt, True)
                    return activate(flow, reader, key, saved, entry_key, checked, review)
                return flow.review_job(reader, key, saved, entry_key, checked)
            return activate(flow, reader, key, saved, entry_key, checked)
        entry = resolve_entry(value, by_key[entry_key])
        node = reader.node(row['owner']).node
        _, state = r.owner_state(reader, node.owner.obligation_id)
        ident = identity(reader, state, flow.selection.environment_id)
        deps, identities, closed = [], [], []
        for dep in entry['dependencies']:
            origin = dep['entry']
            previous = saved if origin['submission_id'] == key else submission(reader, origin['submission_id'])
            if previous is None or origin['key'] not in previous['rows']:
                raise ValueError('Unknown checked dependency entry')
            child_row = previous['rows'][origin['key']]
            child = reader.node(child_row['owner']).node
            if child.owner != OwnerVersion(*child_row['version']):
                raise ValueError('Dependency entry has been retired')
            _, child_state = r.owner_state(reader, child.owner.obligation_id)
            identities.append(identity(reader, child_state, flow.selection.environment_id))
            kind, certificate, joint = DependencyOrigin.ABSTRACTED_LEMMA, None, None
            if origin['submission_id'] == key and origin['key'] in draft.closed_definitions:
                prior = r.uncheck(child_row.get('checked'))
                if prior is None:
                    return None
                kind, certificate, joint = DependencyOrigin.CLOSED_DEFINITION, prior.evidence, key
                closed.append(prior)
            elif child.owner.obligation_id in {i.id for i in flow.selection.identities}:
                kind = DependencyOrigin.FROZEN_PEER
            deps.append(Dependency(child.owner, dep['parameter'], child.interface, kind, certificate, joint))
        source = artifact(entry['proof'].encode())
        candidate = Candidate(str(uuid5(UUID(key), entry_key)), node.owner, flow.selection.environment_id,
            source.reference, tuple(deps), entry['revision_of'], key if joint_targets(flow.selection) else None)
        request = flow.checker.prepare('proof', flow.selection, state.index, candidate,
            flow.overlay(reader, (source,)), identity=ident, statement=entry['subject'].get('statement'),
            dependencies=tuple(identities), closed_definitions=tuple(closed),
            context=entry_context(entry), imports=tuple(entry.get('imports', ())))
        job_key = f'candidate:{key}:proof:{entry_key}'
        next_row = dict(row, candidate=r.candidate(candidate))
        updated = dict(saved, current=job_key, phase='proof', rows=dict(saved['rows'], **{entry_key: next_row}))
        return r.merge(flow.compiler_job(reader, job_key, request, (coordinator,), ['proof', key, entry_key]),
            r.record(reader, 'application.submissions', key, updated), Effect(artifacts=(source,)))
    # A joint definition/property group becomes visible in one graph commit.
    graph_effect = Effect()
    if joint_targets(flow.selection):
        ordered = [saved['rows'][e['key']] for e in value['entries']]
        checks = tuple(r.uncheck(row['checked']) for row in ordered)
        graph = flow.graph(reader)
        graph_effect = r.graph_changes(reader, graph, accept_joint(graph, flow.selection, key, checks))
    return r.merge(finish(flow, reader, key, saved, 'success'), graph_effect)


def _row(reader, key, saved, entry_key, row):
    return r.record(reader, 'application.submissions', key,
        dict(saved, current=None, rows=dict(saved['rows'], **{entry_key: row})))


def on_binding(flow, reader, key, saved, entry_key, bound):
    draft, value, by_key = entries(reader, saved)
    entry = by_key[entry_key]
    ident, admitted = bound.identity, bound.admitted
    index = _index(flow, entry)
    try:
        previous = reader.node(ident.id)
    except KeyError:
        previous = None
    owner = previous.node.owner if previous else admitted.owner
    if owner.generation != admitted.owner.generation:
        resolved = resolve_entry(value, entry)
        scope_owner = reader.node(flow.selection.identities[index].id).node.owner
        request = flow.checker.prepare('binding', flow.selection, index, scope_owner,
            statement=resolved['subject'].get('statement'),
            parameters=tuple(d['parameter'] for d in resolved['dependencies']),
            context=resolved['subject'].get('declaration_context', CLOSED),
            imports=tuple(resolved.get('imports', ())), subject_owner=owner)
        job_key = f'candidate:{key}:binding:{entry_key}:generation:{owner.generation}'
        return r.merge(flow.compiler_job(reader, job_key, request, (OwnerVersion(*saved['owner']), owner),
            ['binding', key, entry_key]), r.record(reader, 'application.submissions', key, dict(saved, current=job_key)))
    if previous and previous.node.binding:
        if previous.node.interface.sha256 != admitted.interface.sha256:
            raise ValueError('Shared obligation has a different checked interface')
        node = previous.node
    else:
        node = ProofNode(owner, ident.environment_id, admitted.interface, admitted,
                         previous.node.selected if previous else None,
                         previous.node.review if previous else None,
                         previous.node.assembly if previous else None)
    effects = []
    if previous is None or previous.node != node:
        effects.append(Effect(Changes(nodes=(NodeChange(previous.version if previous else None, node),),
            graph_revision=reader.status_snapshot().graph_revision)))
    if previous is None or (previous.node.binding is None and entry['subject']['kind'] == 'lemma'):
        _, parent = r.owner_state(reader, saved['owner'][0])
        context = child_context(draft, entry_key, Context.load(r.read(reader, parent.context, 8*1024**2)))
        context_data, identity_data = artifact(context.payload), artifact(ident.reference_payload())
        effects.extend((Effect(artifacts=(context_data, identity_data)),
            r.save_owner(reader, ident.id, r.Owner(context_data.reference, identity_data.reference, index))))
        guidance = codec.load_record(draft.guidance, limit=8*1024**2)['children'].get(entry_key)
        if guidance:
            effects.append(r.save(reader, 'application.guidance', ident.id,
                (guidance['proof']+'\n'+guidance['usefulness']).encode()))
    row = dict(owner=ident.id, version=r.owner(owner), candidate=None, checked=None, done=False)
    effects.append(_row(reader, key, saved, entry_key, row))
    # A stable entry index makes later external prerequisites a bounded read.
    return r.merge(*effects)


def on_check(flow, reader, key, saved, entry_key, checked):
    row = dict(saved['rows'][entry_key], checked=r.check(checked))
    return _row(reader, key, saved, entry_key, row)


def activate(flow, reader, key, saved, entry_key, checked, review=None):
    graph_effect = Effect()
    if checked.candidate.joint_submission_id is None:
        graph = flow.graph(reader)
        graph_effect = r.graph_changes(reader, graph, accept_checked_proof(graph, checked, review))
    return r.merge(graph_effect, _row(reader, key, saved, entry_key,
        dict(saved['rows'][entry_key], done=True)))


def finish(flow, reader, key, saved, outcome, *, exception=None, reason=None, evidence=None, diagnostics=()):
    owner = OwnerVersion(*saved['owner'])
    _, state = r.owner_state(reader, owner.obligation_id)
    decision = AttemptDecision(saved['attempt'], outcome, flow.policy.mathematical_charge(outcome), exception)
    effects = []
    if outcome == 'success' and reader.counter('owner:'+owner.obligation_id, 'repair_open'):
        scope = 'owner:'+owner.obligation_id
        effects.append(Effect(Changes(counters=tuple(CounterChange(scope, name, reader.counter(scope, name), 0)
            for name in ('repair_open', 'repair_remaining', 'repair_suspended')))))
    if evidence:
        from ..strategy import Memory, Observation, update_memory
        memory = Memory.load(r.read(reader, state.memory)) if state.memory else Memory()
        memory = update_memory(memory, observation=Observation.select('checker', reason or outcome, evidence, diagnostics))
        data = artifact(memory.payload)
        effects.append(Effect(artifacts=(data,)))
        state = replace(state, memory=data.reference)
    stop = state.stopped or bool(exception and not flow.policy.revision(exception,
        flow.owner_usage(reader, owner)).allowed)
    return r.merge(*effects, settle_candidate(reader, owner, decision),
        r.save_owner(reader, owner.obligation_id, replace(state, submission=None, reason=reason, stopped=stop)),
        r.record(reader, 'application.submissions', key,
                 dict(saved, phase='done' if outcome == 'success' else 'failed', current=None)))
