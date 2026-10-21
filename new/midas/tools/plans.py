"""Helper planning and reduction over explicit values; no workers or database."""
from dataclasses import dataclass, replace
from functools import lru_cache
from math import lcm

from ..domain import ArtifactRef, ReceiptBinding, ResultStatus
from ..domain import encoding as codec
from ..domain.records import identifier, instance, items, natural
from ..policy import AdmissionRequest
from ..source import lean_name, parse_identifier
from .protocol import (HelperRequest, Lookup, Probe, Research, Retrieve, Scope, Search, Stage,
                       REQUEST_BYTES, RESULT_BYTES, action_value, read, text)
from .search import SearchResult

# The complete bounded rank domain has one exact denominator. Integer addition
# preserves Fraction ordering without allocating/reducing a fraction per hit.
_DENOMINATOR = lcm(*range(60, 1060))
_RANK_WEIGHT = tuple(_DENOMINATOR // rank for rank in range(60, 1060))


@lru_cache(maxsize=4096)
def _canonical(name):
    parsed = parse_identifier(name)
    return lean_name(parsed, readable=True) if parsed is not None else None


def _allowed(forbidden):
    denied = {_canonical(n) for n in forbidden}
    def allowed(name):
        canonical = _canonical(name)
        return canonical is not None and canonical not in denied and not canonical.startswith('Midas')
    return allowed


@dataclass(frozen=True, slots=True)
class Plan:
    request: HelperRequest
    stages: tuple[Stage, ...]
    charge: str | None
    units: int = 1

    def __post_init__(self):
        instance(self.request, HelperRequest)
        items(self.stages, Stage)
        if (any(s.helper_id != self.request.id or s.scope != self.request.scope for s in self.stages)
                or len({s.key for s in self.stages}) != len(self.stages)):
            raise ValueError('Plan stages differ from helper')
        if self.charge not in (None, action_value(self.request.action)[0]):
            raise ValueError('Helper charge differs from request')
        expected = len(self.request.action.cases) if isinstance(self.request.action, Probe) else 1
        if type(self.units) is not int or self.units != expected:
            raise ValueError('Helper units differ from request')

    def admission(self, ordinal, limits):
        """Charge only the initial stage; controller reserves initial stages atomically.

        Compiler/model owners replace the job inputs after lowering, preserving
        this helper charge. Continuations and retries carry no new helper charge.
        """
        natural(ordinal, 'Stage ordinal')
        if ordinal >= len(self.stages):
            raise ValueError('Stage ordinal outside plan')
        stage = self.stages[ordinal]
        return AdmissionRequest(stage.job(limits), helper=self.charge if ordinal == 0 else None,
                                helper_units=self.units)


def _stages(request, actions, start=0):
    return tuple(Stage(request.id, n, request.scope, action) for n, action in enumerate(actions, start))


def search_stages(request):
    action = request.action
    if not isinstance(action, Retrieve):
        raise ValueError('Expected retrieval request')
    return _stages(request, (Search(q, backend) for q in action.queries for backend in action.backends))


def plan_helper(request, policy, usage, services):
    """Policy remains the single owner of helper allowances and service availability."""
    mode = action_value(request.action)[0]
    units = len(request.action.cases) if isinstance(request.action, Probe) else 1
    decision = policy.helper(mode, usage, services, units=units)
    if not decision.allowed:
        raise ValueError(decision.reason)
    if isinstance(request.action, Research):
        bound = min(request.action.web_calls, policy.capabilities(usage, services).web_calls)
        request = replace(request, action=replace(request.action, web_calls=bound))
    if isinstance(request.action, Retrieve):
        stages = search_stages(request)
        if not stages:
            stages = lookup_stages(request, fuse(request, ()))
            if not stages:
                raise ValueError('Retrieval contains no allowed names')
    else:
        stages = _stages(request, (request.action,))
    return Plan(request, stages, mode, units)


@dataclass(frozen=True, slots=True)
class Fusion:
    ranked: tuple[str, ...]
    selected: tuple[str, ...]
    excluded: tuple[str, ...]
    provenance: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    errors: tuple[tuple[int, str], ...]


def fuse(request, reports):
    stages = search_stages(request)
    if type(reports) is not tuple or len(reports) != len(stages):
        raise ValueError('Retrieval requires every exact search stage outcome')
    scores, provenance, excluded, errors = {}, {}, {}, []
    allowed = _allowed(request.forbidden)
    for stage, report in zip(stages, reports):
        instance(report, SearchResult)
        report.validate_for(stage)
        if report.error is not None:
            errors.append((stage.ordinal, report.error))
            continue
        names = dict.fromkeys(name for row in report.records for name in row.names)
        for rank, name in enumerate(names):
            if not allowed(name):
                excluded[name] = None
                continue
            scores[name] = scores.get(name, 0) + _RANK_WEIGHT[rank]
            provenance.setdefault(name, []).append((stage.ordinal, rank))
    ranked = tuple(sorted(scores, key=lambda n: (-scores[n], n)))
    action = request.action
    names = dict.fromkeys((*action.names, *ranked[:12]))
    selected = []
    for name in names:
        if allowed(name):
            selected.append(name)
        else:
            excluded[name] = None
    return Fusion(ranked, tuple(selected), tuple(excluded),
                  tuple((n, tuple(provenance[n])) for n in ranked), tuple(errors))


def lookup_stages(request, fusion):
    actions = (Lookup(fusion.selected[i:i+12]) for i in range(0, len(fusion.selected), 12))
    return _stages(request, actions, start=len(search_stages(request)))


def continue_retrieval(plan, reports, scope):
    if scope != plan.request.scope or plan.stages != search_stages(plan.request):
        raise ValueError('Stale scope or unexpected retrieval stage')
    fusion = fuse(plan.request, reports)
    return Plan(plan.request, lookup_stages(plan.request, fusion), None), fusion


@dataclass(frozen=True, slots=True)
class StageEvidence:
    """Controller handoff AFTER checker/provider receipt admission and projection.

    Construction checks bindings, not authenticity. Search uses SearchService's
    receipt admission. No helper handoff establishes a proof or checked interface.
    """
    stage: Stage
    request_hash: str
    receipt: ReceiptBinding
    status: ResultStatus
    artifacts: tuple[ArtifactRef, ...]
    available: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self):
        instance(self.stage, Stage)
        identifier(self.request_hash, '')
        instance(self.receipt, ReceiptBinding)
        instance(self.status, ResultStatus)
        items(self.artifacts, ArtifactRef)
        if self.receipt.request_ref.sha256 != self.request_hash:
            raise ValueError('Helper evidence differs from admitted receipt request')
        if len(self.artifacts) > 16 or not self.artifacts:
            raise ValueError('Expected bounded helper evidence references')
        if type(self.available) is not tuple or type(self.unavailable) is not tuple:
            raise ValueError('Expected immutable lookup classifications')
        names = self.available + self.unavailable
        if isinstance(self.stage.action, Lookup):
            expected = self.stage.action.names
            if (self.status == ResultStatus.SUCCEEDED and (set(names) != set(expected) or len(names) != len(expected))
                    or self.status != ResultStatus.SUCCEEDED and names):
                raise ValueError('Lookup classification differs from exact request')
        elif names:
            raise ValueError('Only checked lookup may classify name availability')
        if (self.error is None) != (self.status == ResultStatus.SUCCEEDED):
            raise ValueError('Helper failure requires a diagnostic')
        if self.error is not None:
            text(self.error, 'helper diagnostic', 2048)


def admit_checked(stage, admitted, compiler_hash, reader):
    """Project a checker-admitted advisory result onto its planned helper stage.

    Controller binds its saved CompilerRequest to this stage before dispatch and
    supplies that request's payload hash here. Checker already authenticated the
    receipt and validated the exact source/context, names, cases and signatures.
    This boundary cannot accept a proof result or infer availability from search.
    """
    identifier(compiler_hash, '')
    operation = 'lookup' if isinstance(stage.action, Lookup) else 'probe' if isinstance(stage.action, Probe) else None
    if operation is None or admitted.operation != operation:
        raise ValueError('Checker advisory differs from helper operation')
    payload = read(reader, admitted.evidence, RESULT_BYTES)
    available = unavailable = ()
    if operation == 'lookup':
        value = codec.decode(payload)
        if (value.get('request') != compiler_hash or type(value.get('records')) is not list
                or tuple(row.get('query') for row in value['records']) != stage.action.names
                or any(row.get('status') not in ('available', 'unavailable') for row in value['records'])):
            raise ValueError('Checked lookup differs from planned names/request')
        available = tuple(row['query'] for row in value['records'] if row['status'] == 'available')
        unavailable = tuple(row['query'] for row in value['records'] if row['status'] == 'unavailable')
    else:
        # Preserve checker-owned per-case progress and exact archived case refs.
        # Neither probe closure nor a diagnostic can promote proof state.
        refs = codec.load_record(payload, limit=RESULT_BYTES)
        if type(refs) is not list or len(refs) != len(stage.action.cases):
            raise ValueError('Probe batch differs from planned cases')
        remaining = RESULT_BYTES-len(payload)
        for case_ref, raw_ref in zip(stage.action.cases, refs):
            reference = ArtifactRef.from_dict(raw_ref)
            case = codec.load_record(read(reader, case_ref, REQUEST_BYTES), limit=REQUEST_BYTES)
            data = read(reader, reference, remaining)
            remaining -= len(data)
            value = codec.decode(data)
            result = value.get('row', {}).get('result', {})
            if (value.get('request') != compiler_hash or type(case) is not dict
                    or result.get('id') != case.get('id') or not isinstance(case.get('id'), str)
                    or result.get('operation') != case.get('operation', 'tactic')):
                raise ValueError('Probe evidence differs from planned case/request')
    return StageEvidence(stage, admitted.receipt.request_ref.sha256, admitted.receipt,
                         ResultStatus.SUCCEEDED, (admitted.evidence,), available, unavailable)


@dataclass(frozen=True, slots=True)
class EvidenceChange:
    scope: Scope
    helper_id: str
    continuation: str
    evidence: tuple[StageEvidence, ...]
    searches: tuple[SearchResult, ...] = ()
    fusion: Fusion | None = None

    @property
    def authority(self):
        return 'advisory'


def finish_helper(plan, evidence, scope, *, searches=()):
    """Propose scoped evidence and original continuation; controller commits once."""
    if scope != plan.request.scope:
        raise ValueError('Helper scope is stale')
    if type(evidence) is not tuple or any(not isinstance(e, StageEvidence) for e in evidence):
        raise ValueError('Expected admitted stage evidence')
    fusion = None
    if isinstance(plan.request.action, Retrieve):
        fusion = fuse(plan.request, searches)
        expected = lookup_stages(plan.request, fusion)
    else:
        if searches:
            raise ValueError('Unexpected search stages')
        expected = _stages(plan.request, (plan.request.action,))
    if tuple(e.stage for e in evidence) != expected or plan.stages != expected:
        raise ValueError('Missing, duplicate, reordered or substituted helper stage')
    return EvidenceChange(scope, plan.request.id, plan.request.continuation, evidence, searches, fusion)
