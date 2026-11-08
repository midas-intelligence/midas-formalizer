"""All policy settings in one place; one shallow, bounded config document.

This module contains values, not provider construction or environment admission.
Prices are integer micro-USD per million tokens, avoiding float rounding.
"""

from dataclasses import dataclass, fields, replace
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from ..domain import DomainError, ResourceLimits
from ..domain.encoding import JSON_LIMIT, canonical
from ..domain.records import natural


FORMAT = "midas-config-v1"
MAX_CONFIG_BYTES = JSON_LIMIT
MIB = 1024**2
MAX_MODEL_DOWNSTREAM = 16
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _integers(value, *, zero=(), optional=()):
    for field in fields(value):
        item = getattr(value, field.name)
        if item is None and field.name in optional:
            continue
        natural(item, field.name, positive=field.name not in zero)


@dataclass(frozen=True, slots=True)
class RunConfig:
    turns: int = 400
    runtime_ms: int = 7_200_000
    max_intents: int = 10_000
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_microusd: int | None = None

    def __post_init__(self):
        budgets = ('max_input_tokens', 'max_output_tokens', 'max_cost_microusd')
        _integers(self, zero=('turns', *budgets), optional=budgets)


@dataclass(frozen=True, slots=True)
class SchedulingConfig:
    model_concurrency: int = 2
    network_concurrency: int = 2
    compiler_concurrency: int = 1  # Shared CPU slots for compiler and local search jobs.
    compiler_memory_mib: int = 4096
    backlog_low: int = 4
    backlog_new_stop: int | None = None
    backlog_all_stop: int | None = None
    reopen_per_tick: int = 1

    def __post_init__(self):
        natural(self.model_concurrency, 'model_concurrency', positive=True)
        for name, base in (('backlog_new_stop', 20), ('backlog_all_stop', 40)):
            if getattr(self, name) is None:
                object.__setattr__(self, name, base + MAX_MODEL_DOWNSTREAM * self.model_concurrency)
        _integers(self)
        if max(self.model_concurrency, self.compiler_concurrency, self.network_concurrency) > 1024:
            raise DomainError('Concurrency exceeds 1024')
        if not self.backlog_low < self.backlog_new_stop < self.backlog_all_stop:
            raise DomainError('Backlog thresholds must increase strictly')


@dataclass(frozen=True, slots=True)
class MathematicsConfig:
    direct: int = 7
    decomposition: int = 7
    stop_requests: int = 3
    backtracks: int = 12
    owner_backtracks: int = 3
    repair: int = 2
    timeout: int = 2
    memory: int = 0
    elaboration: int = 2
    protocol: int = 2

    def __post_init__(self):
        _integers(self, zero=tuple(f.name for f in fields(self)))
        if not 1 <= self.repair <= 10 or max(self.timeout, self.memory, self.elaboration) > 5:
            raise DomainError('Invalid repair or exception allowance')


@dataclass(frozen=True, slots=True)
class HelperConfig:
    research: int = 7
    web_search: int = 7
    retrieval: int = 10
    probe: int = 6
    web_calls: int = 12

    def __post_init__(self):
        _integers(self, zero=tuple(f.name for f in fields(self)))
        if self.web_calls > 12:
            raise DomainError('Web-search call limit exceeds 12')


@dataclass(frozen=True, slots=True)
class ProcessConfig:
    wall_ms: int = 90_000
    memory_mib: int = 4096
    output_bytes: int = 8 * MIB
    evidence_bytes: int = 64 * MIB
    max_heartbeat: int = 2_000_000
    max_rec_depth: int = 1000

    def __post_init__(self):
        _integers(self, zero=('max_heartbeat',))
        for name, cap in (('wall_ms', 86_400_000), ('memory_mib', 1_048_576),
                          ('output_bytes', 8 * MIB), ('evidence_bytes', 64 * MIB)):
            if getattr(self, name) > cap:
                raise DomainError(f'{name} exceeds process profile')

    def limits(self):
        return ResourceLimits(self.wall_ms, self.memory_mib, self.output_bytes, self.evidence_bytes,
                              self.max_heartbeat, self.max_rec_depth)


@dataclass(frozen=True, slots=True)
class ExportConfig:
    timeout_ms: int = 90_000
    comparator_timeout_ms: int = 1_800_000
    deadline_ms: int = 7_200_000
    memory_mib: int = 4096
    max_bytes: int = 16 * 1024**3
    output_bytes: int = 8 * MIB
    evidence_bytes: int = 64 * MIB
    max_heartbeat: int = 2_000_000
    max_rec_depth: int = 1000

    def __post_init__(self):
        _integers(self, zero=('max_heartbeat',))
        ProcessConfig(self.timeout_ms, self.memory_mib, self.output_bytes, self.evidence_bytes)
        if (self.comparator_timeout_ms > 7_200_000 or self.deadline_ms > 86_400_000
                or self.max_bytes > 1024**4):
            raise DomainError('Export limits exceed profile')

    def limits(self, *, comparator=False):
        return ResourceLimits(self.comparator_timeout_ms if comparator else self.timeout_ms,
                              self.memory_mib, self.output_bytes, self.evidence_bytes,
                              self.max_heartbeat, self.max_rec_depth)


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Invocation bounds, including Codex retries within a single turn."""
    invocation_ms: int = 600_000
    preparation_ms: int = 10_000
    connect_ms: int = 30_000
    write_ms: int = 30_000
    read_ms: int = 60_000
    pool_ms: int = 10_000
    startup_ms: int = 30_000
    shutdown_ms: int = 3000
    output_bytes: int = 8 * MIB
    frame_bytes: int = 16 * MIB
    event_bytes: int = 64 * MIB
    request_max_retries: int = 0
    stream_max_retries: int = 0

    def __post_init__(self):
        _integers(self, zero=('request_max_retries', 'stream_max_retries'))
        if not self.output_bytes <= self.frame_bytes <= self.event_bytes <= 64 * MIB:
            raise DomainError('Invalid model byte bounds')
        if max(self.invocation_ms, self.startup_ms) > 7_200_000:
            raise DomainError('Model timeout exceeds profile')


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    transport_retries: int = 2
    unknown_retries: int = 0
    local_retries: int = 2
    deadline_ms: int = 1_920_000
    backoff_ms: int = 2000
    backoff_cap_ms: int = 60_000
    retry_after_cap_ms: int = 300_000
    failure_window_ms: int = 600_000
    failure_threshold: int = 3
    cooldown_ms: int = 60_000

    def __post_init__(self):
        _integers(self, zero=('transport_retries', 'unknown_retries', 'local_retries'))
        if (self.transport_retries > 15 or self.unknown_retries > self.transport_retries
                or self.backoff_ms > self.backoff_cap_ms or self.deadline_ms > 7_200_000):
            raise DomainError('Invalid recovery allowance or deadline')


@dataclass(frozen=True, slots=True)
class Prices:
    input: int = 0
    cached_input: int = 0
    output: int = 0
    cache_write: int | None = None

    def __post_init__(self):
        _integers(self, zero=('input', 'cached_input', 'output', 'cache_write'), optional=('cache_write',))


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """One role's non-secret provider settings; no credentials or client objects.

    Provider/model identifiers are opaque. The provider component owns supported
    transports and their combinations, authentication and model availability.
    """
    provider: str | None = None
    model: str | None = None
    transport: str | None = None
    account: str | None = None
    endpoint: str | None = None
    credential_env: str | None = None
    prompt_cache: str | None = None
    prices: Prices | None = None
    input_token_budget: int | None = None
    reasoning_effort: str | None = None

    def __post_init__(self):
        for field in fields(self):
            if field.name in ('prices', 'input_token_budget'):
                continue
            value = getattr(self, field.name)
            if value is None:
                continue
            maximum = 2048 if field.name == 'endpoint' else 256
            if (type(value) is not str or not value.strip() or len(value) > maximum
                    or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise DomainError(f'Invalid model route {field.name}')
            try:
                if len(value.encode('utf-8')) > maximum:
                    raise DomainError(f'Model route {field.name} exceeds byte limit')
            except UnicodeError as error:
                raise DomainError(f'Invalid model route {field.name} encoding') from error
        if (self.provider is None) != (self.model is None):
            raise DomainError('A model route requires both provider and model')
        if self.prices is not None and type(self.prices) is not Prices:
            raise DomainError('Invalid model prices')
        if self.input_token_budget is not None:
            natural(self.input_token_budget, 'input_token_budget', positive=True)
        if self.prompt_cache not in (None, 'explicit', 'disabled'):
            raise DomainError('prompt_cache must be explicit or disabled')
        if self.reasoning_effort not in (None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise DomainError('Invalid reasoning_effort')
        if not self.configured and any(getattr(self, name) is not None for name in (
                'transport', 'account', 'endpoint', 'credential_env', 'prompt_cache', 'prices', 'input_token_budget',
                'reasoning_effort')):
            raise DomainError('Model route settings require provider and model')
        if self.credential_env is not None and not _ENV_NAME.fullmatch(self.credential_env):
            raise DomainError('credential_env must name an environment variable')
        if self.endpoint is not None:
            try:
                url = urlsplit(self.endpoint)
                port = url.port  # Reject invalid or out-of-range ports without I/O.
                valid = (url.scheme in ('http', 'https') and bool(url.hostname)
                         and url.username is None and url.password is None
                         and not url.query and not url.fragment and port != 0
                         and not any(c.isspace() for c in self.endpoint))
            except ValueError as error:
                raise DomainError('Invalid model endpoint') from error
            if not valid:
                raise DomainError('Model endpoint must be an HTTP(S) URL without credentials, query or fragment')

    @property
    def configured(self):
        return self.provider is not None and self.model is not None

    def record(self):
        value = {f.name: getattr(self, f.name) for f in fields(self) if f.name != 'prices'}
        value['prices'] = None if self.prices is None else {
            f.name: getattr(self.prices, f.name) for f in fields(self.prices)}
        return value


@dataclass(frozen=True, slots=True)
class Config:
    run: RunConfig = RunConfig()
    scheduling: SchedulingConfig = SchedulingConfig()
    mathematics: MathematicsConfig = MathematicsConfig()
    helpers: HelperConfig = HelperConfig()
    checker: ProcessConfig = ProcessConfig()
    local: ProcessConfig = ProcessConfig(wall_ms=60_000)
    export: ExportConfig = ExportConfig()
    model: ModelConfig = ModelConfig()
    research_model: ModelConfig = ModelConfig(invocation_ms=900_000)
    web_search_model: ModelConfig = ModelConfig(invocation_ms=900_000)
    recovery: RecoveryConfig = RecoveryConfig()
    prover: ModelRoute = ModelRoute()
    reviewer: ModelRoute = ModelRoute()
    research: ModelRoute = ModelRoute()
    web_search: ModelRoute = ModelRoute()

    def __post_init__(self):
        for name, cls in _SECTIONS.items():
            value = getattr(self, name)
            if type(value) is not cls:
                raise DomainError(f'Invalid {name} config')
        if max(self.checker.memory_mib, self.export.memory_mib) > self.scheduling.compiler_memory_mib:
            raise DomainError('Compiler job memory exceeds pool')
        if self.research_model.invocation_ms > 900_000:
            raise DomainError('Research invocation exceeds 900000 ms')
        if self.web_search_model.invocation_ms > 900_000:
            raise DomainError('Web-search invocation exceeds 900000 ms')

    def record(self):
        return dict(format=FORMAT, **{
            name: section.record() if isinstance(section := getattr(self, name), ModelRoute) else
            {f.name: getattr(section, f.name) for f in fields(section)} for name in _SECTIONS})

    def encode(self):
        return canonical(self.record())

    @property
    def id(self):
        return 'policy_' + sha256(self.encode()).hexdigest()

    def increase(self, changes):
        """Prospective live increases; callers persist audit and expected version.

        Frozen requests retain their old bounds. Usage and deadlines' origins do
        not reset. Helper invocation timeouts may increase; routes, retry controls
        and helper allowances stay frozen.
        """
        _object(changes, ('run', 'mathematics', 'checker', 'local', 'export', 'scheduling',
                         'research_model', 'web_search_model'))
        sections = {}
        changed = False
        for name, patch in changes.items():
            original = getattr(self, name)
            allowed = (('turns', 'runtime_ms') if name == 'run' else
                ('invocation_ms',) if name in ('research_model', 'web_search_model') else
                ('model_concurrency', 'compiler_concurrency', 'network_concurrency', 'compiler_memory_mib')
                if name == 'scheduling' else tuple(f.name for f in fields(original)))
            _object(patch, allowed)
            for field, value in patch.items():
                natural(value, field)
                before = getattr(original, field)
                # Lean interprets zero heartbeats as unlimited.
                decreases = ((before == 0 and value != 0) or (value != 0 and value < before)
                             if field == 'max_heartbeat' else value < before)
                if decreases:
                    raise DomainError(f'Live limits cannot decrease: {name}.{field}')
                changed |= value != before
            sections[name] = replace(original, **patch)
        if not changed:
            raise DomainError('At least one live limit must increase')
        return replace(self, **sections)


_SECTIONS = {
    'run': RunConfig, 'scheduling': SchedulingConfig, 'mathematics': MathematicsConfig,
    'helpers': HelperConfig, 'checker': ProcessConfig, 'local': ProcessConfig,
    'export': ExportConfig, 'model': ModelConfig, 'research_model': ModelConfig, 'web_search_model': ModelConfig,
    'recovery': RecoveryConfig, 'prover': ModelRoute, 'reviewer': ModelRoute,
    'research': ModelRoute, 'web_search': ModelRoute,
}


def _object(value, allowed):
    if type(value) is not dict or value.keys() - set(allowed):
        raise DomainError('Unknown config fields or expected object')


def load_config(payload: bytes) -> Config:
    """Load ordinary UTF-8 JSON. Bound bytes and depth before JSON parsing."""
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_CONFIG_BYTES:
        raise DomainError('Config must be 1–16384 bytes')
    depth = 0
    quoted = escaped = False
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 3:
                raise DomainError('Config nesting exceeds the role/prices depth limit')
        elif byte in (93, 125):
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DomainError('Duplicate config key')
            result[key] = value
        return result

    def constant(_):
        raise DomainError('Nonfinite config number')

    try:
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as error:
        raise DomainError('Invalid config encoding') from error
    _object(value, ('format', *_SECTIONS))
    if value.get('format') != FORMAT:
        raise DomainError('Unsupported config format')
    defaults = Config()
    sections = {}
    for name, section in value.items():
        if name == 'format':
            continue
        cls = _SECTIONS[name]
        _object(section, (f.name for f in fields(cls)))
        if cls is ModelRoute and section.get('prices') is not None:
            prices = section['prices']
            _object(prices, (f.name for f in fields(Prices)))
            section = dict(section, prices=Prices(**prices))
        # Scheduling derives omitted backlog windows from the configured pool.
        if name == 'scheduling':
            sections[name] = cls(**section)
        else:
            sections[name] = replace(getattr(defaults, name), **section)
    return replace(defaults, **sections)
