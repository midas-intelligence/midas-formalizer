"""Pure admission, accounting, retry and completion rules over bounded snapshots.

Storage supplies indexed totals including pending reservations. The controller
rechecks decisions and commits them atomically with their exact requests. This
module never scans run history, reads artifacts, starts workers or performs I/O.
"""

from dataclasses import dataclass, field, fields
from enum import Enum

from ..domain import (
    DeliveryCertainty, DomainError, Invocation, Job, JobKind, ReceiptBinding, ResourceLimits,
    ResultStatus, Termination, Usage, WorkerResult,
)
from ..domain.records import instance, natural
from .config import Config, Prices, RecoveryConfig


def _counts(value, *, flags=(), skip=()):
    for field in fields(value):
        item = getattr(value, field.name)
        if field.name in skip:
            continue
        if field.name in flags:
            if type(item) is not bool:
                raise DomainError(f'{field.name} must be boolean')
        else:
            natural(item, field.name)


@dataclass(frozen=True, slots=True)
class OwnerUsage:
    """Lifetime consumed plus pending units; repair_remaining is a saved grant.

    Failed transport attempts do not consume direct/decomposition allowances.
    A repair grant's saved allowance is not enlarged by a later config increase.
    """
    direct: int = 0
    decomposition: int = 0
    research: int = 0
    retrieval: int = 0
    probe: int = 0
    backtracks: int = 0
    timeout: int = 0
    memory: int = 0
    elaboration: int = 0
    protocol: int = 0
    pending_candidate: bool = False
    repair_open: bool = False
    repair_suspended: bool = False
    repair_remaining: int = 0

    def __post_init__(self):
        _counts(self, flags=('pending_candidate', 'repair_open', 'repair_suspended'))
        if not self.repair_open and (self.repair_remaining or self.repair_suspended):
            raise DomainError('Repair capacity requires an open grant')


@dataclass(frozen=True, slots=True)
class RunUsage:
    turns: int = 0
    intents: int = 0
    elapsed_ms: int = 0
    backtracks: int = 0
    budget: Usage = Usage(0, 0, 0, 0)
    bound_exceeded: bool = False

    def __post_init__(self):
        _counts(self, flags=('bound_exceeded',), skip=('budget',))
        instance(self.budget, Usage)


@dataclass(frozen=True, slots=True)
class Services:
    compiler: bool = True
    research: bool = False
    research_web_calls: int = 0

    def __post_init__(self):
        _counts(self, flags=('compiler', 'research'))


@dataclass(frozen=True, slots=True)
class Capacity:
    """Includes prepared work; retained memory excludes active job memory.

    For a job reusing a retained worker, remove that worker from retained memory
    in this projection and include its full footprint in the new reservation.
    """
    models: int = 0
    compilers: int = 0
    locals: int = 0
    compiler_memory_mib: int = 0
    retained_memory_mib: int = 0
    backlog: int = 0
    provider_not_before_ms: int = 0
    provider_unavailable: bool = False
    networks: int = 0

    def __post_init__(self):
        _counts(self, flags=('provider_unavailable',))


@dataclass(frozen=True, slots=True)
class BacklogState:
    new_closed: bool = False
    repair_closed: bool = False
    ramping: bool = False
    model_allowance: int | None = None

    def __post_init__(self):
        _counts(self, flags=('new_closed', 'repair_closed', 'ramping'), skip=('model_allowance',))
        if self.model_allowance is not None:
            natural(self.model_allowance, 'model_allowance')


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    job: Job
    bounds: Usage = Usage()
    downstream: int = 0
    repair_model: bool = False
    new_intent: bool = True
    dependencies_ready: bool = True
    candidate_mode: str | None = None
    helper: str | None = None
    helper_units: int = 1

    def __post_init__(self):
        instance(self.job, Job)
        instance(self.bounds, Usage)
        _counts(self, flags=('repair_model', 'new_intent', 'dependencies_ready'),
                skip=('job', 'bounds', 'candidate_mode', 'helper'))
        if self.candidate_mode not in (None, 'direct', 'decomposition'):
            raise DomainError('Unknown candidate mode')
        if self.helper not in (None, 'research', 'retrieve', 'probe'):
            raise DomainError('Unknown helper mode')
        if self.helper_units == 0:
            raise DomainError('Helper units must be positive')
        if self.helper is not None:
            kinds = {'research': (JobKind.MODEL,),
                     'retrieve': (JobKind.SEARCH, JobKind.NETWORK_SEARCH, JobKind.LOOKUP),
                     'probe': (JobKind.PROBE,)}
            if self.job.kind not in kinds[self.helper]:
                raise DomainError('Helper differs from job kind')
        if self.job.kind != JobKind.MODEL and (self.downstream or self.repair_model):
            raise DomainError('Only model work reserves downstream compiler backlog')


@dataclass(frozen=True, slots=True)
class Reservation:
    request: AdmissionRequest
    turns: int
    uses_repair: bool = False

    def __post_init__(self):
        instance(self.request, AdmissionRequest)
        if type(self.turns) is not int or self.turns != int(self.request.job.kind == JobKind.MODEL):
            raise DomainError('Turn reservation differs from job kind')
        if type(self.uses_repair) is not bool or self.uses_repair and self.request.candidate_mode is None:
            raise DomainError('Repair reservation requires a candidate')


@dataclass(frozen=True, slots=True)
class Decision:
    reason: str | None = None
    reservation: Reservation | None = None

    @property
    def allowed(self):
        return self.reason is None


@dataclass(frozen=True, slots=True)
class Capabilities:
    available_modes: tuple[str, ...]
    direct_remaining: int
    decomposition_remaining: int
    repair_remaining: int
    research_remaining: int
    retrieval_remaining: int
    probe_remaining: int
    web_calls: int


@dataclass(frozen=True, slots=True)
class Settlement:
    reservation: Reservation
    receipt: ReceiptBinding
    result: WorkerResult
    turns: int
    usage: Usage
    budget: Usage
    release_capacity: bool
    exceeded_bounds: tuple[str, ...]


def cost_microusd(prices: Prices | None, usage: Usage, *, cache_write_tokens: int | None = None):
    """Partition input/read/write tokens and round up once. Unknown stays unknown."""
    if prices is None:
        return None
    instance(prices, Prices)
    instance(usage, Usage)
    if cache_write_tokens is not None:
        natural(cache_write_tokens, 'cache_write_tokens')
    inputs, outputs = usage.input_tokens, usage.output_tokens
    if inputs is None or outputs is None:
        return None
    cached = usage.cache_tokens
    if cached is None:
        if inputs and prices.input != prices.cached_input:
            return None
        cached = 0
    if cached > inputs:
        return None
    writes = 0 if prices.cache_write is None else cache_write_tokens
    write_rate = prices.input if prices.cache_write is None else prices.cache_write
    if writes is None:
        if inputs > cached and write_rate != prices.input:
            return None
        writes = 0
    if cached + writes > inputs:
        return None
    numerator = ((inputs - cached - writes) * prices.input + cached * prices.cached_input
                 + writes * write_rate + outputs * prices.output)
    cost = (numerator + 999_999) // 1_000_000
    return cost if cost < 2**63 else None


class Failure(str, Enum):
    TRANSPORT = 'transport'
    RATE_LIMIT = 'rate_limit'
    TIMEOUT = 'timeout'
    MEMORY = 'memory'
    CRASH = 'crash'
    CANCELLED = 'cancelled'
    REJECTION = 'rejection'
    RECURSION_DEPTH = 'recursion_depth'
    PROTOCOL = 'protocol'
    PERMANENT = 'permanent'


@dataclass(frozen=True, slots=True)
class RetryHistory:
    """Counts for this exact saved request, not a run-wide invocation scan."""
    first_ms: int = 0
    transport_retries: int = 0
    unknown_retries: int = 0
    local_retries: int = 0
    retry_after_ms: int = 0

    def __post_init__(self):
        _counts(self)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    eligible: bool
    reason: str | None = None
    earliest_ms: int | None = None
    turn_charge: int = 0
    transport_charge: int = 0
    unknown_charge: int = 0
    local_charge: int = 0


@dataclass(frozen=True, slots=True)
class ProviderGate:
    not_before_ms: int = 0
    unavailable: bool = False

    def __post_init__(self):
        _counts(self, flags=('unavailable',))


class Stage(str, Enum):
    PENDING = 'pending'
    VERIFIED = 'verified'
    FAILED = 'failed'


@dataclass(frozen=True, slots=True)
class Progress:
    roots: int
    verified_roots: int = 0
    integration: Stage = Stage.PENDING
    export_verification: Stage = Stage.PENDING
    active: int = 0
    ready: int = 0
    waiting: int = 0
    elapsed_ms: int = 0
    stopped: bool = False
    recovery_required: bool = False
    setup_failed: bool = False
    reasons: tuple[str, ...] = ()
    allow_sorry: bool = False

    def __post_init__(self):
        _counts(self, flags=('stopped', 'recovery_required', 'setup_failed', 'allow_sorry'),
                skip=('integration', 'export_verification', 'reasons'))
        instance(self.integration, Stage)
        instance(self.export_verification, Stage)
        if self.roots == 0 or self.verified_roots > self.roots:
            raise DomainError('Invalid root progress')
        if (self.integration == Stage.VERIFIED and self.verified_roots != self.roots and not self.allow_sorry
                or self.export_verification == Stage.VERIFIED and self.integration != Stage.VERIFIED):
            raise DomainError('Completion stages lack their prerequisites')
        if type(self.reasons) is not tuple or len(self.reasons) > 20 or any(
                type(reason) is not str or not reason or len(reason) > 256 for reason in self.reasons):
            raise DomainError('Expected at most 20 bounded completion reasons')


@dataclass(frozen=True, slots=True)
class Completion:
    proof_state: str
    integration_state: Stage
    export_verification_state: Stage
    outcome: str
    reasons: tuple[str, ...]
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class ExportUsage:
    elapsed_ms: int = 0
    captured_bytes: int = 0
    artifact_bytes: int = 0
    cancelled: bool = False

    def __post_init__(self):
        _counts(self, flags=('cancelled',))


@dataclass(frozen=True, slots=True)
class Policy:
    config: Config = Config()
    _checker: ResourceLimits = field(init=False, repr=False)
    _local: ResourceLimits = field(init=False, repr=False)
    _export: ResourceLimits = field(init=False, repr=False)
    _comparator: ResourceLimits = field(init=False, repr=False)

    def __post_init__(self):
        instance(self.config, Config)
        # Construct resource values once, not for every candidate admission.
        for name, limits in (('_checker', self.config.checker.limits()), ('_local', self.config.local.limits()),
                             ('_export', self.config.export.limits()), ('_comparator', self.config.export.limits(comparator=True))):
            object.__setattr__(self, name, limits)

    def tick(self, backlog: int, state: BacklogState = BacklogState()) -> BacklogState:
        """Explicit hysteresis state; call once per controller tick, not per job."""
        natural(backlog, 'backlog')
        limits = self.config.scheduling
        new, repair, ramping = state.new_closed, state.repair_closed, state.ramping
        allowance = limits.model_concurrency if state.model_allowance is None else state.model_allowance
        if backlog >= limits.backlog_all_stop:
            new = repair = True
        elif backlog >= limits.backlog_new_stop:
            new = True
        elif backlog < limits.backlog_low:
            if new or repair:
                ramping, allowance = True, 0
            new = repair = False
        if ramping:
            allowance = min(limits.model_concurrency, allowance + limits.reopen_per_tick)
            ramping = allowance < limits.model_concurrency
        return BacklogState(new, repair, ramping, allowance)

    def candidate(self, mode: str, owner: OwnerUsage) -> Decision:
        if mode not in ('direct', 'decomposition'):
            raise DomainError('Unknown mathematical mode')
        if owner.pending_candidate:
            return Decision('mathematical_candidate_pending')
        if getattr(owner, mode) < getattr(self.config.mathematics, mode):
            return Decision()
        if owner.repair_open and not owner.repair_suspended and owner.repair_remaining:
            return Decision()
        return Decision(mode + '_allowance_exhausted')

    def helper(self, mode: str, owner: OwnerUsage, services: Services, *, units=1) -> Decision:
        natural(units, 'helper units', positive=True)
        if mode in ('recall', 'page'):
            return Decision()
        if mode not in ('research', 'retrieve', 'probe'):
            raise DomainError('Unknown helper mode')
        if mode in ('retrieve', 'probe') and not services.compiler:
            return Decision('compiler_not_configured')
        if mode == 'research' and (not services.research or not min(
                services.research_web_calls, self.config.helpers.web_calls)):
            return Decision('missing_research_configuration')
        field = 'retrieval' if mode == 'retrieve' else mode
        if getattr(owner, field) + units > getattr(self.config.helpers, field):
            return Decision(field + '_allowance_exhausted')
        return Decision()

    def capabilities(self, owner: OwnerUsage, services: Services) -> Capabilities:
        modes = tuple(mode for mode in ('research', 'retrieve', 'probe', 'recall', 'page')
                      if self.helper(mode, owner, services).allowed)
        repair = owner.repair_remaining if owner.repair_open and not owner.repair_suspended else 0
        return Capabilities(modes,
            max(0, self.config.mathematics.direct - owner.direct),
            max(0, self.config.mathematics.decomposition - owner.decomposition), repair,
            max(0, self.config.helpers.research - owner.research),
            max(0, self.config.helpers.retrieval - owner.retrieval),
            max(0, self.config.helpers.probe - owner.probe),
            min(services.research_web_calls, self.config.helpers.web_calls) if services.research else 0)

    def backtrack(self, owner: OwnerUsage, run: RunUsage) -> Decision:
        if owner.repair_open:
            return Decision('repair_grant_open')
        if owner.pending_candidate:
            return Decision('mathematical_candidate_pending')
        if run.backtracks >= self.config.mathematics.backtracks:
            return Decision('run_backtrack_limit')
        if owner.backtracks >= self.config.mathematics.owner_backtracks:
            return Decision('owner_backtrack_limit')
        return Decision()

    def revision(self, kind: str, owner: OwnerUsage) -> Decision:
        if kind not in ('timeout', 'memory', 'elaboration', 'protocol'):
            raise DomainError('Unknown revision allowance')
        return Decision(kind + '_revision_allowance_exhausted' if
                        getattr(owner, kind) >= getattr(self.config.mathematics, kind) else None)

    @staticmethod
    def transient_admission(reason):
        """These waits can clear without a new command or changed frozen inputs."""
        return reason in ('model_capacity', 'compiler_capacity', 'network_capacity',
                          'provider_cooldown', 'compiler_backlog', 'projected_compiler_backlog',
                          'waiting_dependency')

    @staticmethod
    def mathematical_charge(outcome: str) -> int:
        if outcome not in ('success', 'rejection', 'stale', 'cancelled', 'abandoned', 'infrastructure', 'exception'):
            raise DomainError('Unknown mathematical outcome')
        return int(outcome in ('success', 'rejection'))

    def _budget_reason(self, bounds: Usage, run: RunUsage):
        if run.bound_exceeded:
            return 'usage_bound_exceeded'
        if run.turns >= self.config.run.turns:
            return 'model_turn_budget'
        for field in ('input_tokens', 'output_tokens', 'cost_microusd'):
            maximum = getattr(self.config.run, 'max_' + field)
            if maximum is None:
                continue
            current, requested = getattr(run.budget, field), getattr(bounds, field)
            if current is None or requested is None:
                return 'unknown_usage_bound:' + field
            if current + requested > maximum:
                return 'usage_budget_exhausted:' + field
        return None

    def admit(self, request: AdmissionRequest, run: RunUsage, owner: OwnerUsage,
              capacity: Capacity, services: Services, *, now_ms=0,
              backlog_state: BacklogState = BacklogState(), reserved_allowances=False) -> Decision:
        natural(now_ms, 'now_ms')
        if type(reserved_allowances) is not bool:
            raise DomainError('Invalid allowance reservation flag')
        job, limits = request.job, self.config.scheduling
        if run.elapsed_ms >= self.config.run.runtime_ms:
            return Decision('run_deadline')
        if request.new_intent and run.intents >= self.config.run.max_intents:
            return Decision('lifetime_intent_allowance_exhausted')
        if not request.dependencies_ready:
            return Decision('waiting_dependency')
        if request.candidate_mode and not reserved_allowances:
            decision = self.candidate(request.candidate_mode, owner)
            if not decision.allowed:
                return decision
        if request.helper and not reserved_allowances:
            decision = self.helper(request.helper, owner, services, units=request.helper_units)
            if not decision.allowed:
                return decision
        if job.kind == JobKind.MODEL:
            model = self.config.research_model if request.helper == 'research' else self.config.model
            if (job.limits.wall_ms > model.invocation_ms or job.limits.output_bytes > model.output_bytes
                    or job.limits.evidence_bytes > model.event_bytes + 16384):
                return Decision('model_resource_limit')
            reason = self._budget_reason(request.bounds, run)
            if reason:
                return Decision(reason)
            if capacity.provider_unavailable:
                return Decision('provider_unavailable')
            if now_ms < capacity.provider_not_before_ms:
                return Decision('provider_cooldown')
            threshold = limits.backlog_all_stop if request.repair_model else limits.backlog_new_stop
            if request.downstream >= threshold:
                return Decision('impossible_downstream')
            allowance = limits.model_concurrency if backlog_state.model_allowance is None else min(
                limits.model_concurrency, backlog_state.model_allowance)
            if capacity.models >= allowance:
                return Decision('model_capacity')
            if backlog_state.repair_closed or (backlog_state.new_closed and not request.repair_model):
                return Decision('compiler_backlog')
            if capacity.backlog + request.downstream >= threshold:
                return Decision('projected_compiler_backlog')
        else:
            network = job.kind == JobKind.NETWORK_SEARCH
            local = job.kind in (JobKind.SEARCH, JobKind.NETWORK_SEARCH)
            export = job.kind in (JobKind.ASSEMBLE, JobKind.INTEGRATE, JobKind.EXPORT_VERIFY)
            profile = (self._local if local else self._comparator if job.kind == JobKind.EXPORT_VERIFY
                       else self._export if export else self._checker)
            if not local and not services.compiler:
                return Decision('compiler_not_configured')
            if not local and job.limits.memory_mib > limits.compiler_memory_mib:
                return Decision('impossible_memory')
            if (job.limits.wall_ms > profile.wall_ms or job.limits.memory_mib > profile.memory_mib
                    or job.limits.output_bytes > profile.output_bytes or job.limits.evidence_bytes > profile.evidence_bytes
                    or not local and job.limits.max_rec_depth > profile.max_rec_depth
                    or not local and profile.max_heartbeat and (
                        job.limits.max_heartbeat == 0 or job.limits.max_heartbeat > profile.max_heartbeat)):
                return Decision('process_resource_limit')
            if network and capacity.networks >= limits.network_concurrency:
                return Decision('network_capacity')
            if not network and capacity.compilers + capacity.locals >= limits.compiler_concurrency:
                return Decision('compiler_capacity')
            if (not local and capacity.compiler_memory_mib
                    + capacity.retained_memory_mib + job.limits.memory_mib > limits.compiler_memory_mib):
                return Decision('compiler_capacity')
        uses_repair = bool(request.candidate_mode and getattr(owner, request.candidate_mode)
                          >= getattr(self.config.mathematics, request.candidate_mode))
        return Decision(reservation=Reservation(request, int(job.kind == JobKind.MODEL), uses_repair))

    @staticmethod
    def settle(reservation: Reservation, invocation: Invocation, receipt: ReceiptBinding,
               result: WorkerResult, *, previous: Settlement | None = None) -> Settlement | None:
        """Return one ledger change, or None for an exact duplicate report.

        Receipt seals and saved reservation bytes must be admitted by the caller.
        Capacity remains held until the executor confirms process termination.
        """
        result.validate_for(invocation)
        if (reservation.request.job != invocation.request.job
                or receipt.original_execution_id != invocation.original_execution_id
                or receipt.request_ref != invocation.request.request_ref):
            raise DomainError('Accounting differs from its exact invocation')
        if previous is not None:
            if (previous.reservation, previous.receipt, previous.result) != (reservation, receipt, result):
                raise DomainError('Conflicting duplicate accounting report')
            return None
        not_sent = result.delivery_certainty == DeliveryCertainty.NOT_SENT
        terminated = result.termination == Termination.TERMINATED
        refunded = not_sent and terminated
        names = ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')
        if not_sent and any(getattr(result.usage, name) not in (None, 0) for name in names):
            raise DomainError('Not-sent result reports consumed usage')
        usage = Usage(0, 0, 0, 0) if refunded else result.usage
        budget, exceeded = {}, []
        for name in names:
            actual, bound = getattr(usage, name), getattr(reservation.request.bounds, name)
            # A process that has not terminated may still increase its usage.
            budget[name] = (actual if actual is not None else bound) if terminated else (
                None if bound is None else max(actual or 0, bound))
            if actual is not None and bound is not None and actual > bound:
                exceeded.append(name)
        return Settlement(reservation, receipt, result, 0 if refunded else reservation.turns,
                          usage, Usage(**budget), terminated,
                          tuple(exceeded))

    def retry(self, reservation: Reservation, invocation: Invocation, result: WorkerResult, failure: Failure,
              history: RetryHistory, run: RunUsage, *, now_ms: int,
              saved_recovery: RecoveryConfig, resume=False, jitter=1.0) -> RetryDecision:
        """Retry the saved request; revisions need a new request and allowance.

        The caller admits the saved reservation and recovery config. A
        cancellation resumes without spending failure retries, while
        its old turn and usage remain charged. Unknown failures require opt-in.
        """
        natural(now_ms, 'now_ms')
        result.validate_for(invocation)
        request = reservation.request
        if request.job != invocation.request.job:
            raise DomainError('Retry differs from its saved request')
        instance(failure, Failure)
        instance(saved_recovery, RecoveryConfig)
        if type(resume) is not bool or type(jitter) not in (int, float) or not 0 <= jitter <= 1:
            raise DomainError('Invalid retry controls')
        if now_ms < history.first_ms:
            return RetryDecision(False, 'clock_reconciliation_required')
        if result.status == ResultStatus.SUCCEEDED:
            return RetryDecision(False, 'already_succeeded')
        if (failure == Failure.CANCELLED) != (result.status == ResultStatus.CANCELLED):
            raise DomainError('Cancellation classification differs from result')
        if result.termination != Termination.TERMINATED:
            return RetryDecision(False, 'termination_unconfirmed')
        if run.elapsed_ms >= self.config.run.runtime_ms:
            return RetryDecision(False, 'run_deadline')
        rules = saved_recovery
        if now_ms - history.first_ms >= rules.deadline_ms:
            return RetryDecision(False, 'retry_deadline')
        if failure in (Failure.REJECTION, Failure.PROTOCOL):
            return RetryDecision(False, 'revision_required')
        if failure == Failure.PERMANENT:
            return RetryDecision(False, 'permanent_failure')
        model = request.job.kind == JobKind.MODEL
        if model:
            reason = self._budget_reason(request.bounds, run)
            if reason:
                return RetryDecision(False, reason)
        if failure == Failure.CANCELLED:
            if not resume:
                return RetryDecision(False, 'cancelled')
            return RetryDecision(True, earliest_ms=now_ms, turn_charge=int(model))
        if history.retry_after_ms > rules.retry_after_cap_ms:
            return RetryDecision(False, 'provider_retry_after_limit')
        if model:
            if failure not in (Failure.TRANSPORT, Failure.RATE_LIMIT, Failure.TIMEOUT):
                return RetryDecision(False, 'permanent_failure')
            if history.transport_retries >= rules.transport_retries:
                return RetryDecision(False, 'provider_retry_exhausted')
            uncertain = result.delivery_certainty != DeliveryCertainty.NOT_SENT and not (
                failure == Failure.RATE_LIMIT and result.delivery_certainty == DeliveryCertainty.DELIVERED)
            if uncertain and history.unknown_retries >= rules.unknown_retries:
                return RetryDecision(False, 'unknown_outcome_policy')
            count = history.transport_retries
        else:
            if history.local_retries >= rules.local_retries:
                return RetryDecision(False, 'local_retry_exhausted')
            count, uncertain = history.local_retries, False
        delay = max(int(jitter * min(rules.backoff_cap_ms, rules.backoff_ms * 2**min(count, 63))),
                    history.retry_after_ms)
        earliest = now_ms + delay
        if earliest >= history.first_ms + rules.deadline_ms:
            return RetryDecision(False, 'retry_deadline')
        if run.elapsed_ms + delay >= self.config.run.runtime_ms:
            return RetryDecision(False, 'run_deadline')
        return RetryDecision(True, earliest_ms=earliest, turn_charge=int(model),
                             transport_charge=int(model), unknown_charge=int(uncertain), local_charge=int(not model))

    def provider_gate(self, *, now_ms: int, recent_failures: int, rate_limited=False,
                      probe_failed=False, retry_after_ms=0, previous=ProviderGate(),
                      saved_recovery: RecoveryConfig) -> ProviderGate:
        """Caller supplies failures already restricted to failure_window_ms."""
        for name, value in (('now_ms', now_ms), ('recent_failures', recent_failures), ('retry_after_ms', retry_after_ms)):
            natural(value, name)
        rules = saved_recovery
        instance(rules, RecoveryConfig)
        if type(rate_limited) is not bool or type(probe_failed) is not bool:
            raise DomainError('Invalid provider gate flags')
        if previous.unavailable or probe_failed or retry_after_ms > rules.retry_after_cap_ms:
            return ProviderGate(previous.not_before_ms, True)
        if rate_limited or recent_failures >= rules.failure_threshold:
            return ProviderGate(max(previous.not_before_ms, now_ms + rules.cooldown_ms, now_ms + retry_after_ms))
        return previous

    def export_limits(self, usage: ExportUsage, *, comparator=False):
        """Residual whole-export budget; executor supplies cumulative byte counts.

        Call at each stage and during streaming. Artifact limits are enforced
        before publication, and process-tree termination remains executor-owned.
        """
        limits = self.config.export
        if usage.cancelled or usage.elapsed_ms >= limits.deadline_ms:
            raise DomainError('Export cancelled or exceeded deadline')
        if usage.artifact_bytes >= limits.max_bytes or usage.captured_bytes >= limits.output_bytes:
            raise DomainError('Export byte budget exhausted')
        return ResourceLimits(min(limits.comparator_timeout_ms if comparator else limits.timeout_ms,
                              limits.deadline_ms - usage.elapsed_ms), limits.memory_mib,
                              limits.output_bytes - usage.captured_bytes,
                              min(limits.evidence_bytes, limits.max_bytes-usage.artifact_bytes),
                              limits.max_heartbeat, limits.max_rec_depth)

    def completion(self, progress: Progress) -> Completion:
        proof = 'verified' if progress.verified_roots == progress.roots else 'incomplete'
        reasons = progress.reasons
        if progress.active:
            outcome, reason, exit_code = 'waiting', 'active_workers', None
        elif progress.recovery_required:
            outcome, reason, exit_code = 'recovery_required', 'recovery_required', 2
        elif progress.export_verification == Stage.VERIFIED:
            outcome, reason, exit_code = 'export_verified', None, 0
            reasons = ()
        elif progress.setup_failed:
            outcome, reason, exit_code = 'setup_failed', 'initialization_incomplete', 2
        elif progress.stopped:
            outcome, reason, exit_code = 'stopped', 'external_stop', 130
        elif Stage.FAILED in (progress.integration, progress.export_verification):
            outcome, reason, exit_code = 'finalization_failed', 'finalization_failed', 1
        elif progress.elapsed_ms >= self.config.run.runtime_ms:
            outcome, reason, exit_code = 'search_blocked', 'run_deadline', 1
        elif progress.ready or progress.waiting:
            outcome, reason, exit_code = ('running', None, None) if progress.ready else ('waiting', 'waiting_dependency', None)
        elif proof == 'verified':
            outcome, reason, exit_code = 'finalization_pending', 'finalization_pending', 1
        else:
            outcome, reason, exit_code = 'search_blocked', None if reasons else 'no_ready_work', 1
        if reason and reason not in reasons:
            reasons = (*reasons[:19], reason)
        return Completion(proof, progress.integration, progress.export_verification, outcome, reasons, exit_code)
