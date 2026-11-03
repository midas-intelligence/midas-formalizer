"""Default prompt contracts and bounded external decoding; no proof authority."""
from dataclasses import dataclass
from functools import lru_cache

from ..domain import DomainError
from ..domain.wire import decode, decode_object
from ..prompt import prover, reviewer
from .memory import Update, text

RESPONSE_BYTES = 8 * 1024**2
CONTRACTS = {'prover': 'prover-action-v3', 'reviewer': 'review-verdict-v1'}


def _validator(schema, root):
    """Compile the small default schemas once. Large responses are visited once.

    This deliberately supports the vocabulary of the default contracts only.
    A replacement contract supplies its own interpreter alongside its renderer.
    """
    if '$ref' in schema:
        return _validator(root['$defs'][schema['$ref'].removeprefix('#/$defs/')], root)
    if 'anyOf' in schema:
        branches = schema['anyOf']
        validators = [_validator(branch, root) for branch in branches]
        modes = {b['properties']['mode']['enum'][0]: v for b, v in zip(branches, validators)
                 if b.get('type') == 'object' and 'mode' in b.get('properties', {})}
        def union(value):
            if len(modes) == len(branches):
                if type(value) is not dict or type(value.get('mode')) is not str or value['mode'] not in modes:
                    raise DomainError('Unknown strategy action')
                return modes[value['mode']](value)
            for validate in validators:
                try:
                    validate(value)
                    return
                except ValueError:
                    pass
            raise DomainError('Response differs from nullable contract')
        return union
    kind = schema['type']
    if kind == 'object':
        members = {k: _validator(v, root) for k, v in schema['properties'].items()}
        def obj(value):
            if type(value) is not dict or value.keys() != members.keys():
                raise DomainError('Response fields differ from dispatched contract')
            for key, validate in members.items():
                validate(value[key])
        return obj
    if kind == 'array':
        item = _validator(schema['items'], root)
        def array(value):
            if type(value) is not list or not schema.get('minItems', 0) <= len(value) <= schema['maxItems']:
                raise DomainError('Response array exceeds contract')
            for v in value:
                item(v)
        return array
    def scalar(value):
        types = {'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
        if type(value) is not types[kind]:
            raise DomainError('Response scalar differs from contract')
        if kind == 'string':
            text(value, schema.get('maxLength', 8000), empty=True)
        if 'enum' in schema and value not in schema['enum']:
            raise DomainError('Unknown contract value')
        if kind == 'integer' and not schema['minimum'] <= value <= schema['maximum']:
            raise DomainError('Response integer exceeds contract')
    return scalar


@lru_cache(maxsize=2)
def _default(role):
    raw = {'prover': prover.SCHEMA, 'reviewer': reviewer.SCHEMA}[role]
    schema = decode(raw, limit=16384)
    return _validator(schema, schema)


@dataclass(frozen=True, slots=True)
class Response:
    """Short-lived decoded response. Reducers never retain its mutable value."""
    mode: str
    value: dict
    memory: Update | None = None


def interpret(payload, *, role, contract):
    if CONTRACTS.get(role) != contract:
        raise DomainError('No interpreter for dispatched response contract')
    value = decode_object(payload, limit=RESPONSE_BYTES)
    discarded_queries = False
    action = value.get('action')
    if role == 'prover' and type(action) is dict and action.get('mode') == 'retrieve':
        queries = action.get('queries')
        if type(queries) is list:
            action['queries'] = [query for query in queries if type(query) is str]
            discarded_queries = len(action['queries']) != len(queries)
    _default(role)(value)
    if role == 'prover':
        action = value['action']
        if action['mode'] == 'retrieve':
            action['names'] = list(dict.fromkeys(action['names']))
        # With no usable search queries, retain a valid names-only lookup.
        if discarded_queries and not action['queries'] and action['names']:
            action['backends'] = []
        text(action['rationale'])
        return Response(action['mode'], action, Update.from_value(value['memory_update']))
    verdict = value['verdict']
    text(value['rationale'])
    if verdict == 'reject':
        if not value['findings']:
            raise DomainError('Review rejection requires findings')
        for finding in value['findings']:
            text(finding['issue'])
    elif value['findings']:
        raise DomainError('Accept/helper review requires empty findings')
    if verdict in ('research', 'web_search'):
        text(value['question'])
        if verdict == 'research' and value['urls']:
            raise DomainError('Research does not accept URLs; use web_search for references')
    elif value['question'] or value['urls']:
        raise DomainError('Helper fields require a research or web_search verdict')
    return Response('review', value)
