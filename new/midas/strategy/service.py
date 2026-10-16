"""Plan, interpret and reduce. No SQL, compiler, provider, worker or timer calls."""
from dataclasses import replace
from hashlib import sha256
import re

from ..domain import (AdmittedBinding, AdmittedCheck, AdmittedReview, CheckKind, ProofState,
                      ReceiptBinding, ResultStatus, accept_binding, accept_checked_proof, accept_joint)
from ..domain.records import instance
from ..policy import Policy
from ..prompt import Section, Snapshot as Display, build, prover, reviewer
from ..tools import (Backend, HelperRequest, Lookup, Probe, Research, Retrieve, Scope, Search,
                     finish_helper, plan_helper, recall)
from .candidates import expand, revise, supplied
from .memory import Observation, pack, update_memory
from .protocol import RESPONSE_BYTES, interpret
from .state import (ArtifactData, Attempt, AttemptDecision, Changes, Dispatch, Objection,
                    PreparedModel, Snapshot, Turn, Work, candidate_hash)


def _work(snapshot, operation, *, detail=b''):
    return Work(operation, snapshot.version.owner,
                snapshot.attempt.candidate.id if snapshot.attempt else None, detail)


def _resume(snapshot, turn):
    if snapshot.stopped or snapshot.graph.node(snapshot.version.owner).state != ProofState.UNRESOLVED:
        return ()
    return (_work(snapshot, turn.operation),)


class Strategy:
    def __init__(self, *, interpreter=interpret):
        if not callable(interpreter):
            raise ValueError('Expected response interpreter')
        self.interpreter = interpreter

    def plan(self, snapshot, policy):
        instance(snapshot, Snapshot)
        instance(policy, Policy)
        result = lambda reason=None, work=(): Changes(snapshot.version, work, reason)
        if snapshot.busy:
            return result('work_pending')
        if snapshot.version.owner.obligation_id not in snapshot.graph.reachable():
            return result('unreachable_owner')
        node = snapshot.graph.node(snapshot.version.owner)
        if node.state == ProofState.VERIFIED:
            return result('verified')
        if snapshot.stopped:
            return result('strategy_stopped')
        attempt = snapshot.attempt
        if attempt is not None:
            if attempt.candidate.id in snapshot.graph.retired_candidates:
                return result('retired_candidate')
            if not attempt.bound:
                return result(work=(_work(snapshot, 'binding'),))
            if attempt.binding_only:
                return result('binding_complete')
            if attempt.checked is None:
                return result(work=(_work(snapshot, 'proof'),))
            if attempt.candidate.joint_submission_id is not None:
                return result(work=(_work(snapshot, 'joint'),))
            if attempt.checked.kind == CheckKind.CONDITIONAL and attempt.review is None:
                return result('supplied_review_required') if snapshot.workflow == 'supplied' else result(
                    work=(_work(snapshot, 'review'),))
            return result(work=(_work(snapshot, 'activate'),))
        if node.state == ProofState.CONDITIONAL:
            dependencies = node.selected.candidate.logical_dependencies
            children = tuple(dict.fromkeys(d.owner for d in dependencies
                if snapshot.graph.node(d.owner).state != ProofState.VERIFIED))
            if children:
                return result(work=(Work('children', node.owner, node.selected.candidate.id,
                    pack([[o.obligation_id, o.generation] for o in children])),))
            return result(work=(Work('assemble', node.owner, node.selected.candidate.id),))
        if snapshot.workflow == 'supplied':
            return result('supplied_candidate_required')
        if not any(policy.candidate(mode, snapshot.usage).allowed for mode in ('direct', 'decomposition')):
            return result('mathematical_allowance_exhausted')
        return result(work=(_work(snapshot, 'revise' if snapshot.base else 'generate'),))

    def prepare_model(self, snapshot, policy, work, display, options, *, renderer=None):
        """Render once and bind its exact schema/wire to the dispatched snapshot."""
        if work not in self.plan(snapshot, policy).work or work.operation not in ('generate', 'revise', 'review'):
            raise ValueError('Model work is not ready in this snapshot')
        instance(display, Display)
        if snapshot.version.owner not in display.owners or snapshot.version.context not in display.artifacts:
            raise ValueError('Presentation lacks exact owner/context bindings')
        if work.operation == 'review':
            candidate = snapshot.attempt.candidate
            if (candidate.source not in display.artifacts or any(d.interface not in display.artifacts
                    or d.owner not in display.owners for d in candidate.dependencies)):
                raise ValueError('Review presentation lacks exact candidate/dependency bindings')
        capabilities = policy.capabilities(snapshot.usage, snapshot.services)
        modes = [mode for mode in ('direct', 'decomposition') if policy.candidate(mode, snapshot.usage).allowed]
        details = ('Candidate modes: ' + ', '.join(modes) + '\nHelper modes: ' +
            ', '.join(capabilities.available_modes) + '\n' + '\n'.join(
            f'{name}: {getattr(capabilities, name)}' for name in (
                'direct_remaining', 'decomposition_remaining', 'repair_remaining', 'research_remaining',
                'retrieval_remaining', 'probe_remaining', 'web_calls')))
        sections = tuple(s for s in display.sections if s.kind != 'capabilities')
        name = 'strategy-capabilities'
        while name in {s.name for s in sections}:
            name += '-current'
        display = replace(display, sections=sections + (Section('capabilities', name, details.encode()),))
        role = 'reviewer' if work.operation == 'review' else 'prover'
        default = reviewer if role == 'reviewer' else prover
        prompt = (renderer or default.render)(display)
        if prompt.role != role:
            raise ValueError('Renderer role differs from planned work')
        if self.interpreter is interpret and (prompt.contract != {'prover': 'prover-action-v1',
                'reviewer': 'review-verdict-v1'}[role] or prompt.schema != default.SCHEMA):
            raise ValueError('Replacement schema/contract requires its matching interpreter')
        wire = build(display, options, renderer=lambda _: prompt)
        turn = Turn(snapshot.version, work.operation, prompt.contract, sha256(prompt.schema).hexdigest(),
            sha256(wire.wire).hexdigest(), sha256(wire.setup).hexdigest() if wire.setup is not None else None,
            snapshot.base.id if snapshot.base else None,
            candidate_hash(snapshot.attempt.candidate) if role == 'reviewer' else None,
            snapshot.environment_id, snapshot.graph.revision)
        return PreparedModel(turn, wire)

    def on_model(self, snapshot, policy, dispatch, admitted, reader, *, submission_id=None):
        """Provider admits authenticity first; raw/scripted outputs use this path.

        admitted is Provider.AdmittedOutput, passed structurally to preserve the
        dependency direction. Partial, refused, cancelled and failed transport
        outputs are never interpreted, even when their bytes contain valid JSON.
        """
        instance(dispatch, Dispatch)
        dispatch.turn.validate_for(snapshot)
        instance(admitted.receipt, ReceiptBinding)
        if admitted.receipt.request_ref != dispatch.request:
            raise ValueError('Model receipt differs from dispatched request')
        if admitted.status != ResultStatus.SUCCEEDED or admitted.outcome.error is not None:
            return Changes(snapshot.version, reason=admitted.outcome.error or admitted.status.value.lower())
        raw = admitted.raw.verify(reader.read_artifact(admitted.raw, max_bytes=RESPONSE_BYTES), max_bytes=RESPONSE_BYTES)
        response = self.interpreter(raw, role=dispatch.turn.role, contract=dispatch.turn.contract)
        if dispatch.turn.role == 'reviewer':
            if response.mode != 'review' or response.memory is not None:
                raise ValueError('Reviewer cannot submit candidates or update prover memory')
            if response.value['verdict'] == 'research':
                return self._helper(snapshot, policy, dispatch.turn, 'research', dict(
                    question=response.value['question'], urls=response.value['urls']))
            review = AdmittedReview(snapshot.attempt.candidate, admitted.receipt,
                                    response.value['verdict'] == 'accept')
            return self.on_review(snapshot, policy, review, evidence=admitted.raw,
                                  diagnostics=(response.value['rationale'],))
        action, mode = response.value, response.mode
        # Compute memory changes only after the complete semantic action validates.
        if mode in ('proof', 'decomposition', 'candidate', 'revise'):
            if snapshot.attempt is not None:
                raise ValueError('Another mathematical candidate is pending')
            if mode == 'revise':
                if snapshot.base is None:
                    raise ValueError('Revision requires an exact dispatched base')
                draft = revise(action, snapshot.base, snapshot.context, submission_id)
            else:
                draft = expand(action, snapshot.context, submission_id)
            decision = policy.candidate(draft.mode, snapshot.usage)
            if not decision.allowed:
                raise ValueError(decision.reason)
            result = Changes(snapshot.version, (_work(snapshot, 'admit_candidate'),), draft=draft)
        elif mode in ('search', 'lookup', 'retrieve', 'probe', 'research'):
            result = self._helper(snapshot, policy, dispatch.turn, mode, action)
        elif mode in ('recall', 'page'):
            decision = policy.helper(mode, snapshot.usage, snapshot.services)
            if not decision.allowed:
                raise ValueError(decision.reason)
            key = action['result_id'] if mode == 'recall' else action['cursor']
            if key not in (snapshot.allowed_pins if mode == 'recall' else snapshot.cursors):
                raise ValueError('Evidence result/cursor was not advertised in this snapshot')
            result = Changes(snapshot.version, (_work(snapshot, mode, detail=pack([key, dispatch.turn.id])),))
        elif mode == 'stop':
            objection = action['objection']
            report = Objection(snapshot.version.owner, objection['category'], objection['evidence'], admitted.raw) if objection else None
            result = Changes(snapshot.version, reason=action['rationale'], objection=report, stopped=True)
        else:
            raise ValueError('Unsupported prover action')
        memory = update_memory(snapshot.memory, response.memory, allowed_pins=snapshot.allowed_pins)
        return replace(result, memory=memory if memory != snapshot.memory else None)

    def submit(self, snapshot, policy, payload, *, guidance):
        if snapshot.attempt is not None:
            raise ValueError('Another mathematical candidate is pending')
        draft = supplied(payload, snapshot.context, guidance=guidance)
        decision = policy.candidate(draft.mode, snapshot.usage)
        if not decision.allowed:
            raise ValueError(decision.reason)
        return Changes(snapshot.version, (_work(snapshot, 'admit_candidate'),), draft=draft)

    def _helper(self, snapshot, policy, turn, mode, action):
        artifacts = ()
        if mode == 'search':
            helper = Search(action['query'], Backend(action['backend']['name'], tuple(action['backend']['prefixes'])))
        elif mode == 'lookup':
            helper = Lookup(tuple(action['names']), action['inspect'])
        elif mode == 'retrieve':
            helper = Retrieve(tuple(action['queries']), tuple(action['names']), tuple(
                Backend(b['name'], tuple(b['prefixes'])) for b in action['backends']), action['offset'], action['inspect'])
        elif mode == 'research':
            helper = Research(action['question'], tuple(action['urls']))
        elif mode == 'probe':
            cases = action['cases']
            ids = set()
            for case in cases:
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', case['id']) is None or case['id'] in ids:
                    raise ValueError('Invalid or duplicate probe case ID')
                ids.add(case['id'])
                if not case['code'].strip() or case['expected'] is not None and (
                        case['operation'] != 'term' or not case['expected'].strip()):
                    raise ValueError('Invalid probe code/expected type')
            artifacts = tuple(ArtifactData(pack(case)) for case in cases)
            helper = Probe(tuple(a.reference for a in artifacts))
        else:
            raise ValueError('Unknown helper mode')
        request = HelperRequest(Scope(snapshot.version.owner, snapshot.environment_id, snapshot.version.context),
            helper, turn.id, snapshot.forbidden_names)
        plan = plan_helper(request, policy, snapshot.usage, snapshot.services)
        return Changes(snapshot.version, helper=plan, artifacts=artifacts)

    def on_helper(self, snapshot, turn, plan, evidence, *, searches=()):
        """Resume the original prover/reviewer operation, without a helper charge."""
        turn.validate_for(snapshot, continuation=True)
        if plan.request.continuation != turn.id:
            raise ValueError('Helper belongs to another strategy continuation')
        scope = Scope(snapshot.version.owner, snapshot.environment_id, snapshot.version.context)
        change = finish_helper(plan, evidence, scope, searches=searches)
        return Changes(snapshot.version, _resume(snapshot, turn), evidence=change)

    def on_recall(self, snapshot, turn, work, archive, reader, *, delivered=lambda key: False):
        """Read one tools-validated page and propose its delivery with continuation."""
        from ..domain.encoding import load_record
        turn.validate_for(snapshot, continuation=True)
        if work.operation not in ('recall', 'page') or work.owner != turn.version.owner:
            raise ValueError('Unexpected evidence continuation')
        value = load_record(work.detail, limit=65536)
        if type(value) is not list or len(value) != 2 or value[1] != turn.id:
            raise ValueError('Evidence continuation belongs to another turn')
        key = value[0]
        if (work.operation == 'recall' and key != archive.id
                or work.operation == 'page' and not key.startswith(archive.id + ':')):
            raise ValueError('Evidence archive differs from requested result')
        page = recall(archive, Scope(snapshot.version.owner, snapshot.environment_id, snapshot.version.context), reader,
                      cursor=key if work.operation == 'page' else None, delivered=delivered)
        return Changes(snapshot.version, _resume(snapshot, turn), page=page,
                       reason='already_delivered' if page is None else None)

    def on_binding(self, snapshot, binding):
        instance(binding, AdmittedBinding)
        if snapshot.attempt is None or binding.owner != snapshot.attempt.candidate.owner:
            raise ValueError('Binding differs from pending candidate')
        graph = accept_binding(snapshot.graph, binding)
        if snapshot.attempt.binding_only:
            return Changes(snapshot.version, reason='binding_complete', attempt=replace(snapshot.attempt, bound=True),
                           graph=graph, graph_revision=snapshot.graph.revision)
        return Changes(snapshot.version, (_work(snapshot, 'proof'),),
            attempt=replace(snapshot.attempt, bound=True), graph=graph, graph_revision=snapshot.graph.revision)

    def on_check(self, snapshot, policy, checked):
        instance(checked, AdmittedCheck)
        pending = snapshot.attempt
        if pending is None or not pending.bound or pending.binding_only or checked.candidate != pending.candidate:
            raise ValueError('Proof check differs from bound pending candidate')
        pending = replace(pending, checked=checked)
        if checked.candidate.joint_submission_id is not None:
            return Changes(snapshot.version, (_work(snapshot, 'joint'),), attempt=pending)
        if checked.kind == CheckKind.CONDITIONAL:
            return Changes(snapshot.version,
                () if snapshot.workflow == 'supplied' else (_work(snapshot, 'review'),),
                reason='supplied_review_required' if snapshot.workflow == 'supplied' else None, attempt=pending)
        graph = accept_checked_proof(snapshot.graph, checked)
        return Changes(snapshot.version, decision=AttemptDecision(checked.candidate.id, 'success',
            policy.mathematical_charge('success')), graph=graph, graph_revision=snapshot.graph.revision)

    def on_review(self, snapshot, policy, review, *, evidence, diagnostics=()):
        instance(review, AdmittedReview)
        pending = snapshot.attempt
        if (pending is None or pending.checked is None or pending.checked.kind != CheckKind.CONDITIONAL
                or review.candidate != pending.candidate):
            raise ValueError('Review differs from checked pending composition')
        if not review.approved:
            return self.on_failure(snapshot, policy, 'rejection', evidence=evidence, diagnostics=diagnostics, source='review')
        graph = accept_checked_proof(snapshot.graph, pending.checked, review)
        return Changes(snapshot.version, decision=AttemptDecision(review.candidate.id, 'success',
            policy.mathematical_charge('success')), graph=graph, graph_revision=snapshot.graph.revision)

    def activate(self, snapshot, policy):
        """Recover selection from already-admitted saved check/review handoffs."""
        pending = snapshot.attempt
        if pending is None or pending.checked is None:
            raise ValueError('Activation requires an admitted proof check')
        graph = accept_checked_proof(snapshot.graph, pending.checked, pending.review)
        return Changes(snapshot.version, decision=AttemptDecision(pending.candidate.id, 'success',
            policy.mathematical_charge('success')), graph=graph, graph_revision=snapshot.graph.revision)

    def on_joint(self, snapshot, policy, selection, submission_id, checks):
        """The controller gathers all admitted root checks for one atomic effect."""
        if snapshot.attempt is None or snapshot.attempt.candidate.joint_submission_id != submission_id:
            raise ValueError('Joint submission differs from pending candidate')
        if snapshot.attempt.candidate not in tuple(c.candidate for c in checks):
            raise ValueError('Joint acceptance omits the pending candidate')
        graph = accept_joint(snapshot.graph, selection, submission_id, checks)
        return Changes(snapshot.version, decision=AttemptDecision(snapshot.attempt.candidate.id, 'success',
            policy.mathematical_charge('success')), graph=graph, graph_revision=snapshot.graph.revision)

    def on_failure(self, snapshot, policy, kind, *, evidence, diagnostics=(), source=None):
        """Controller supplies an admitted outcome, never arbitrary model claims.

        Provider retries stay in policy/controller. Exceptions have separate
        revision counters and do not spend a mathematical attempt.
        """
        if kind not in ('rejection', 'recursion_depth', 'timeout', 'memory', 'elaboration', 'protocol',
                        'cancelled', 'stale', 'infrastructure'):
            raise ValueError('Unknown strategy failure')
        observation = Observation.select(source or ('checker' if snapshot.attempt else 'protocol'), kind, evidence, diagnostics)
        memory = update_memory(snapshot.memory, observation=observation)
        exception = ('elaboration' if kind == 'recursion_depth' else
                     kind if kind in ('timeout', 'memory', 'elaboration', 'protocol') else None)
        outcome = 'exception' if exception else kind
        decision = AttemptDecision(snapshot.attempt.candidate.id, outcome,
            policy.mathematical_charge(outcome), exception) if snapshot.attempt else None
        if kind in ('cancelled', 'stale', 'infrastructure'):
            return Changes(snapshot.version, reason=kind, memory=memory, decision=decision)
        if exception and not policy.revision(exception, snapshot.usage).allowed:
            return Changes(snapshot.version, reason=exception + '_revision_allowance_exhausted',
                           memory=memory, decision=decision)
        if snapshot.workflow == 'supplied':
            return Changes(snapshot.version, reason='supplied_candidate_required', memory=memory, decision=decision)
        return Changes(snapshot.version, (_work(snapshot, 'revise' if snapshot.base else 'generate'),),
                       memory=memory, decision=decision, revision_charge=exception)
