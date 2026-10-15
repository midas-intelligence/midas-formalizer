"""Small immutable transport contracts; large content travels as artifact references."""
from dataclasses import dataclass, fields
from hashlib import sha256
from urllib.parse import urlsplit
import re

from ..domain import ArtifactRef, DeliveryCertainty, DomainError, JobKind, Termination, Usage
from ..domain import encoding as codec
from ..domain.records import instance, natural, text

META_BYTES = 16 * 1024
WIRE_BYTES = 8 * 1024**2
ERRORS = frozenset(('timeout', 'rate_limit', 'transient', 'certificate', 'authentication',
    'configuration', 'unsupported_retry_control', 'malformed_output', 'output_limit', 'cancelled', 'crash', 'refusal'))


def bounded_text(value, label, maximum=256):
    if type(value) is not str or len(value) > maximum:
        raise DomainError(f'Invalid {label}')
    text(value, label)
    if len(value.encode()) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DomainError(f'Invalid {label}')


def pack(value):
    return b''.join(codec.record_parts(value, limit=META_BYTES))


def read(reader, ref, maximum):
    return ref.verify(reader.read_artifact(ref, max_bytes=maximum), max_bytes=maximum)


@dataclass(frozen=True, slots=True)
class Route:
    provider: str
    model: str
    transport: str
    account: str | None = None
    endpoint: str | None = None
    credential_env: str | None = None
    prompt_cache: str | None = None

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None or f.name in ('provider', 'model', 'transport'):
                bounded_text(value, f.name, 2048 if f.name == 'endpoint' else 256)
                if value != value.strip():
                    raise DomainError('Route values cannot have surrounding whitespace')
        if self.provider not in ('openai', 'azure', 'amazon-bedrock-runtime'):
            raise DomainError('Unsupported provider')
        if self.transport not in ('codex', 'azure_responses', 'bedrock_responses'):
            raise DomainError('Unsupported transport')
        if self.credential_env is not None and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.credential_env):
            raise DomainError('Invalid credential variable name')
        if self.prompt_cache not in (None, 'explicit', 'disabled'):
            raise DomainError('Invalid prompt cache mode')
        if self.transport == 'codex':
            if self.endpoint is not None or self.prompt_cache is not None:
                raise DomainError('App-server endpoints/cache controls belong to its pinned configuration')
        else:
            expected = {'azure_responses': 'azure', 'bedrock_responses': 'amazon-bedrock-runtime'}
            if self.provider != expected[self.transport]:
                raise DomainError('Provider/transport mismatch')
            u = urlsplit(self.endpoint or '')
            if (u.scheme != 'https' or not u.hostname or u.username is not None or u.password is not None
                    or u.query or u.fragment or u.port == 0 or any(c.isspace() for c in self.endpoint)):
                raise DomainError('Responses endpoint must be HTTPS without credentials, query or fragment')
            if self.transport == 'bedrock_responses' and not re.fullmatch(
                    r'https://bedrock-runtime\.[a-z]{2}(?:-[a-z]+)+-\d+\.amazonaws\.com(?:\.cn)?/openai/v1/responses', self.endpoint):
                raise DomainError('Invalid Bedrock Responses endpoint')

    @classmethod
    def from_config(cls, config):
        """Application projection; no import of policy or credentials."""
        transport = config.transport or {'openai': 'codex', 'azure': 'azure_responses',
                                         'amazon-bedrock-runtime': 'bedrock_responses'}.get(config.provider)
        return cls(**{f.name: transport if f.name == 'transport' else getattr(config, f.name) for f in fields(cls)})

    def record(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @property
    def scope(self):
        return sha256(pack(self.record())).hexdigest()


@dataclass(frozen=True, slots=True)
class Limits:
    """Frozen transport bounds from policy, including Codex's internal retries."""
    invocation_ms: int
    connect_ms: int
    write_ms: int
    read_ms: int
    pool_ms: int
    startup_ms: int
    shutdown_ms: int
    output_bytes: int
    frame_bytes: int
    event_bytes: int
    web_calls: int = 0
    request_max_retries: int = 0
    stream_max_retries: int = 0

    def __post_init__(self):
        for f in fields(self):
            natural(getattr(self, f.name), f.name,
                    positive=f.name not in ('web_calls', 'request_max_retries', 'stream_max_retries'))
        if (not self.output_bytes <= self.frame_bytes <= self.event_bytes <= 4 * 1024**2
                or max(self.invocation_ms, self.startup_ms) > 7_200_000 or self.web_calls > 12):
            raise DomainError('Invalid transport limits')

    @classmethod
    def from_config(cls, config, *, web_calls=0):
        return cls(**{f.name: web_calls if f.name == 'web_calls' else getattr(config, f.name) for f in fields(cls)})

    def record(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class ModelRequest:
    route: Route
    role: str
    limits: Limits
    wire: ArtifactRef
    setup: ArtifactRef | None = None

    def __post_init__(self):
        instance(self.route, Route)
        instance(self.limits, Limits)
        instance(self.wire, ArtifactRef)
        if self.role not in ('prover', 'reviewer', 'research'):
            raise DomainError('Invalid model role')
        if self.role == 'research' and (self.route.transport, self.route.provider) != ('codex', 'openai'):
            raise DomainError('Research requires the OpenAI app-server')
        if self.role != 'research' and self.limits.web_calls:
            raise DomainError('Only research can use web tools')
        if self.route.transport == 'codex':
            instance(self.setup, ArtifactRef)
        elif self.setup is not None:
            raise DomainError('Only app-server requests have thread setup bytes')
        if any(ref.byte_count > WIRE_BYTES for ref in self.inputs):
            raise DomainError('Wire request exceeds byte limit')

    @property
    def inputs(self):
        return (self.wire,) if self.setup is None else (self.wire, self.setup)

    @property
    def payload(self):
        return pack({'format': 'midas-provider-request-v1', 'route': self.route.record(), 'role': self.role,
                     'limits': self.limits.record(), 'wire': self.wire.to_dict(),
                     'setup': self.setup.to_dict() if self.setup else None})

    @classmethod
    def load(cls, payload):
        v = codec.load_record(payload, limit=META_BYTES)
        codec.fields(v, ('format', 'route', 'role', 'limits', 'wire', 'setup'))
        if v['format'] != 'midas-provider-request-v1':
            raise DomainError('Invalid provider request version')
        return cls(Route(**v['route']), v['role'], Limits(**v['limits']), ArtifactRef.from_dict(v['wire']),
                   ArtifactRef.from_dict(v['setup']) if v['setup'] is not None else None)

    def validate_for(self, frozen, reader):
        if (frozen.job.kind != JobKind.MODEL or frozen.job.input_refs != self.inputs
                or read(reader, frozen.operation_payload, META_BYTES) != self.payload
                or self.limits.output_bytes > frozen.job.limits.output_bytes
                or self.limits.event_bytes + META_BYTES > frozen.job.limits.evidence_bytes):
            raise DomainError('Provider request differs from reserved inputs or bounds')


@dataclass(frozen=True, slots=True)
class ModelOutput:
    """Ephemeral adapter value. Persistence uses raw bytes and small binary metadata."""
    raw: bytes = b''
    error: str | None = None
    phase: str = 'terminal'
    delivery: DeliveryCertainty = DeliveryCertainty.DELIVERED
    usage: Usage = Usage()
    cache_write_tokens: int | None = None
    request_id: str | None = None
    response_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    retry_after_ms: int = 0
    events: bytes = b''
    diagnostics: tuple[str, ...] = ()
    termination: Termination = Termination.TERMINATED

    def __post_init__(self):
        if type(self.raw) is not bytes or type(self.events) is not bytes:
            raise DomainError('Provider output must be raw bytes')
        instance(self.usage, Usage)
        instance(self.delivery, DeliveryCertainty)
        instance(self.termination, Termination)
        if self.error not in ERRORS | {None} or self.phase not in ('preparing', 'connecting', 'sending', 'reading', 'terminal'):
            raise DomainError('Invalid provider outcome')
        if self.error is None and (self.phase != 'terminal' or self.delivery != DeliveryCertainty.DELIVERED):
            raise DomainError('Success requires terminal delivery')
        natural(self.retry_after_ms, 'retry delay')
        if self.cache_write_tokens is not None:
            natural(self.cache_write_tokens, 'cache write tokens')
        for key in ('request_id', 'response_id', 'thread_id', 'turn_id'):
            if getattr(self, key) is not None:
                bounded_text(getattr(self, key), key, 1024)
        if type(self.diagnostics) is not tuple or len(self.diagnostics) > 8:
            raise DomainError('Invalid diagnostics')
        for diagnostic in self.diagnostics:
            bounded_text(diagnostic, 'diagnostic')
        if self.delivery == DeliveryCertainty.NOT_SENT and (self.raw or self.response_id or self.turn_id
                or any(x not in (None, 0) for x in (self.usage.input_tokens, self.usage.cache_tokens,
                    self.usage.output_tokens, self.usage.cost_microusd, self.cache_write_tokens))):
            raise DomainError('Never-sent result contains evidence of model work')

    def validate_limits(self, limits):
        if len(self.raw) > limits.output_bytes or len(self.events) > limits.event_bytes:
            raise DomainError('Provider result exceeds saved limits')
        self.raw.decode('utf-8')
