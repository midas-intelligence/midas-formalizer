"""Immutable helper inputs. Context/source payloads travel by artifact reference."""
from dataclasses import dataclass, field
from hashlib import sha256
import re
from textwrap import indent
from urllib.parse import urlsplit

from ..domain import ArtifactRef, Job, JobKind, OwnerVersion
from ..domain import encoding as codec
from ..domain.records import identifier, instance, items, natural
from ..source import parse_identifier
from ..source.names import code_only

REQUEST_BYTES = 256 * 1024
RESULT_BYTES = 8 * 1024**2
SEARCH_BYTES = 512 * 1024
ENDPOINT = 'https://www.leanexplore.com/api/v2/search'
BACKENDS = ('frozen-source', 'leanexplore')


def pack(value, limit=REQUEST_BYTES):
    return b''.join(codec.record_parts(value, limit=limit))


def read(reader, reference, limit=RESULT_BYTES):
    return reference.verify(reader.read_artifact(reference, max_bytes=limit), max_bytes=limit)


def text(value, label, limit, *, empty=False):
    if (type(value) is not str or len(value) > limit or len(value.encode()) > limit
            or not empty and not value.strip()):
        raise ValueError('Invalid ' + label)


def strings(value, count, limit, label, *, names=False):
    if type(value) is not tuple or len(value) > count:
        raise ValueError('Invalid ' + label)
    for item in value:
        text(item, label, limit)
        if item != item.strip() or names and parse_identifier(item) is None:
            raise ValueError('Invalid ' + label)
    if len(set(value)) != len(value):
        raise ValueError('Duplicate ' + label)


def public_url(value):
    text(value, 'research URL', 8000)
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('Research URLs require HTTP(S) without credentials')
    return value


def source_selector(value):
    text(value, 'source selector', 1024)
    path = value.strip().replace('\\', '/')
    if path.startswith('/') or ':' in path or any(ord(c) < 32 for c in path) or '..' in path.split('/'):
        raise ValueError('Source selector must be a relative frozen-source path')
    path = '/'.join(p for p in path.split('/') if p not in ('', '.'))
    if not path or path.split('/')[0].split('.')[0] in ('checker', 'runtime'):
        raise ValueError('Search only frozen project/library/toolchain sources')
    return path


@dataclass(frozen=True, slots=True)
class Scope:
    owner: OwnerVersion
    environment_id: str
    context: ArtifactRef

    def __post_init__(self):
        instance(self.owner, OwnerVersion)
        identifier(self.environment_id, 'env_')
        instance(self.context, ArtifactRef)

    def value(self):
        return [self.owner.obligation_id, self.owner.generation, self.environment_id, self.context.to_dict()]

    @classmethod
    def load(cls, value):
        if type(value) is not list or len(value) != 4:
            raise ValueError('Invalid helper scope')
        return cls(OwnerVersion(*value[:2]), value[2], ArtifactRef.from_dict(value[3]))


@dataclass(frozen=True, slots=True)
class Backend:
    name: str
    prefixes: tuple[str, ...] = ()

    def __post_init__(self):
        if self.name not in BACKENDS:
            raise ValueError('Unknown search backend')
        strings(self.prefixes, 16, 1024, 'source prefixes')
        if bool(self.prefixes) != (self.name == 'frozen-source'):
            raise ValueError('Only frozen-source search requires prefixes')
        for prefix in self.prefixes:
            source_selector(prefix)

    def value(self):
        return [self.name, list(self.prefixes)]


@dataclass(frozen=True, slots=True)
class Search:
    query: str
    backend: Backend
    limit: int = 50

    def __post_init__(self):
        text(self.query, 'search query', 2048)
        instance(self.backend, Backend)
        if type(self.limit) is not int or not 1 <= self.limit <= 50:
            raise ValueError('Search limit must be 1 through 50')


@dataclass(frozen=True, slots=True)
class Lookup:
    names: tuple[str, ...]
    inspect: bool = False

    def __post_init__(self):
        strings(self.names, 16, 1024, 'lookup names', names=True)
        if not self.names or type(self.inspect) is not bool:
            raise ValueError('Expected lookup names and inspection flag')


@dataclass(frozen=True, slots=True)
class Retrieve:
    queries: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    backends: tuple[Backend, ...] = ()
    offset: int = 0
    inspect: bool = False

    def __post_init__(self):
        strings(self.queries, 3, 2048, 'retrieval queries')
        strings(self.names, 12, 1024, 'retrieval names', names=True)
        items(self.backends, Backend)
        if (not self.queries and not self.names or bool(self.backends) != bool(self.queries)
                or len(self.backends) > 2 or len({b.name for b in self.backends}) != len(self.backends)
                or type(self.offset) is not int or not 0 <= self.offset <= 200
                or type(self.inspect) is not bool):
            raise ValueError('Invalid retrieval query/backends/offset/inspection')


@dataclass(frozen=True, slots=True)
class Probe:
    """References to case records; checker owns code/operation validation."""
    cases: tuple[ArtifactRef, ...]

    def __post_init__(self):
        items(self.cases, ArtifactRef)
        if not 1 <= len(self.cases) <= 3 or len(set(self.cases)) != len(self.cases):
            raise ValueError('Expected 1 through 3 distinct probe cases')


def expand_probe_bodies(cases):
    """Lower model-authored TacticM bodies into bounded internal checker cases."""
    if type(cases) is not list or not 1 <= len(cases) <= 3:
        raise ValueError('Expected 1 through 3 probe cases')
    ids, expanded = set(), []
    for case in cases:
        codec.fields(case, ('id', 'code'))
        if (type(case['id']) is not str or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', case['id']) is None
                or case['id'] in ids):
            raise ValueError('Invalid or duplicate probe case ID')
        ids.add(case['id'])
        text(case['code'], 'probe body', 16000)
        body = code_only(case['code'])
        if not body.strip() or re.match(r'\s*(?:by|run_tac|run_elab|do)\b', body):
            raise ValueError('Probe code must contain only the body after run_tac do; omit the wrapper')
        code = 'run_tac do\n'+indent(case['code'], '  ')
        if len(code.encode()) > 16000:
            raise ValueError('Generated probe code exceeds 16000 UTF-8 bytes')
        expanded.append(dict(id=case['id'], code=code, operation='tactic', setup='', expected=None))
    return tuple(expanded)


@dataclass(frozen=True, slots=True)
class Research:
    question: str
    urls: tuple[str, ...] = ()
    web_calls: int = 12

    def __post_init__(self):
        text(self.question, 'research question', 8000)
        strings(self.urls, 6, 8000, 'research URLs')
        for url in self.urls:
            public_url(url)
        if type(self.web_calls) is not int or not 1 <= self.web_calls <= 12:
            raise ValueError('Invalid research web-call bound')


def action_value(action):
    if isinstance(action, Search):
        return ['search', action.query, action.backend.value(), action.limit]
    if isinstance(action, Lookup):
        return ['lookup', list(action.names), action.inspect]
    if isinstance(action, Retrieve):
        return ['retrieve', list(action.queries), list(action.names), [b.value() for b in action.backends],
                action.offset, action.inspect]
    if isinstance(action, Probe):
        return ['probe', [c.to_dict() for c in action.cases]]
    if isinstance(action, Research):
        return ['research', action.question, list(action.urls), action.web_calls]
    raise ValueError('Unknown helper action')


def load_action(value):
    if type(value) is not list or not value:
        raise ValueError('Invalid helper action')
    def backend(row):
        if type(row) is not list or len(row) != 2 or type(row[1]) is not list:
            raise ValueError('Invalid backend')
        return Backend(row[0], tuple(row[1]))
    def array(row):
        if type(row) is not list:
            raise ValueError('Invalid helper array')
        return tuple(row)
    kind = value[0]
    if kind == 'search' and len(value) == 4:
        return Search(value[1], backend(value[2]), value[3])
    if kind == 'lookup' and len(value) == 3:
        return Lookup(array(value[1]), value[2])
    if kind == 'retrieve' and len(value) == 6:
        return Retrieve(array(value[1]), array(value[2]), tuple(backend(b) for b in array(value[3])), *value[4:])
    if kind == 'probe' and len(value) == 2:
        return Probe(tuple(ArtifactRef.from_dict(c) for c in array(value[1])))
    if kind == 'research' and len(value) == 4:
        return Research(value[1], array(value[2]), value[3])
    raise ValueError('Unknown helper action or fields')


@dataclass(frozen=True, slots=True)
class HelperRequest:
    scope: Scope
    action: Search | Lookup | Retrieve | Probe | Research
    continuation: str
    forbidden: tuple[str, ...] = ()
    id: str = field(init=False)

    def __post_init__(self):
        instance(self.scope, Scope)
        action_value(self.action)
        text(self.continuation, 'strategy continuation', 1024)
        strings(self.forbidden, 1024, 1024, 'forbidden names', names=True)
        digest = sha256()
        for part in codec.record_parts(self.value(), limit=REQUEST_BYTES):
            digest.update(part)
        object.__setattr__(self, 'id', digest.hexdigest())

    def value(self):
        return ['midas-helper-v1', self.scope.value(), action_value(self.action), self.continuation,
                list(self.forbidden)]

    @property
    def payload(self):
        return pack(self.value())

    @classmethod
    def load(cls, payload):
        value = codec.load_record(payload, limit=REQUEST_BYTES)
        if (type(value) is not list or len(value) != 5 or value[0] != 'midas-helper-v1'
                or type(value[4]) is not list):
            raise ValueError('Invalid helper request')
        return cls(Scope.load(value[1]), load_action(value[2]), value[3], tuple(value[4]))


@dataclass(frozen=True, slots=True)
class Stage:
    helper_id: str
    ordinal: int
    scope: Scope
    action: Search | Lookup | Probe | Research
    sha256: str = field(init=False)
    _payload: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        identifier(self.helper_id, '')
        natural(self.ordinal, 'Helper stage')
        instance(self.scope, Scope)
        if not isinstance(self.action, (Search, Lookup, Probe, Research)):
            raise ValueError('Invalid helper stage action')
        payload = pack(['midas-helper-stage-v1', self.helper_id, self.ordinal, self.scope.value(), action_value(self.action)])
        object.__setattr__(self, '_payload', payload)
        object.__setattr__(self, 'sha256', sha256(payload).hexdigest())

    @property
    def key(self):
        return f'helper:{self.helper_id}:{self.ordinal}'

    @property
    def kind(self):
        if isinstance(self.action, Search) and self.action.backend.name == 'leanexplore':
            return JobKind.NETWORK_SEARCH
        return {Search: JobKind.SEARCH, Lookup: JobKind.LOOKUP, Probe: JobKind.PROBE,
                Research: JobKind.MODEL}[type(self.action)]

    @property
    def inputs(self):
        return (self.scope.context, *self.action.cases) if isinstance(self.action, Probe) else (self.scope.context,)

    @property
    def payload(self):
        return self._payload

    def job(self, limits):
        """Checker/model owners lower this description to their frozen request."""
        return Job(self.key, self.kind, self.scope.owner, self.inputs, (self.scope.owner,), limits)

    def validate_for(self, frozen, reader):
        if (self.kind not in (JobKind.SEARCH, JobKind.NETWORK_SEARCH) or frozen.job != self.job(frozen.job.limits)
                or frozen.environment_id != self.scope.environment_id
                or read(reader, frozen.operation_payload, REQUEST_BYTES) != self.payload):
            raise ValueError('Search stage differs from frozen request')

    @classmethod
    def load(cls, payload):
        v = codec.load_record(payload, limit=REQUEST_BYTES)
        if type(v) is not list or len(v) != 5 or v[0] != 'midas-helper-stage-v1':
            raise ValueError('Invalid helper stage')
        return cls(v[1], v[2], Scope.load(v[3]), load_action(v[4]))
