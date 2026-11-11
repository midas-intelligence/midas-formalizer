"""Exact Lean structural identities, independent of environment admission and I/O.

Only the hash of the admitted environment crosses this boundary. Toolchain and
package data stay with the environment service. Structural validation does not
establish kernel proof correctness.
"""

import re

from copy import copy
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256

from . import encoding as codec, encoding as graph
from .cache import Memo
from .encoding import LEAN_SERIALIZER, OBLIGATION_FORMAT, fields, hash_field, load_json, name
from .records import identifier


IDENTITY_REFERENCE_FORMAT = "midas-obligation-reference-v4"
_IDENTITY_CACHE = Memo(max_entries=128, max_bytes=64 * 1024**2)


@dataclass(frozen=True)
class ObligationIdentity:
    target: bytes
    universes: bytes
    context: bytes
    environment_id: str
    prerequisite_interfaces: tuple[bytes, ...] = ()
    serializer: str = LEAN_SERIALIZER

    def __post_init__(self):
        identifier(self.environment_id, 'env_')
        for field in (self.target, self.universes, self.context):
            if not isinstance(field, bytes) or not field:
                raise ValueError("identity components must be nonempty immutable bytes")
        if not isinstance(self.prerequisite_interfaces, tuple):
            raise ValueError("prerequisite interfaces must be an immutable ordered tuple")
        if any(not isinstance(item, bytes) or not item
               for item in self.prerequisite_interfaces):
            raise ValueError("interfaces must be nonempty immutable bytes")
        if not isinstance(self.serializer, str) or not self.serializer:
            raise ValueError("serializer version is required")

        # Components arrive as bounded binary DAGs. Avoid JSON normalization or
        # a second encode: the binary decoder already checks canonical bytes.
        for payload in (self.target, self.universes, self.context, *self.prerequisite_interfaces):
            if not payload.startswith(graph.MAGIC):
                raise ValueError('Identity components require canonical binary graphs')
            graph.decode(payload)

    def payload(self) -> bytes:
        return self._payload

    @cached_property
    def _payload(self) -> bytes:
        return self._envelope(self.environment_id)

    def _components(self):
        return dict(serializer=self.serializer,
                    **{key: graph.decode(getattr(self, key)) for key in ('target', 'universes', 'context')},
                    prerequisite_interfaces=[graph.decode(item) for item in self.prerequisite_interfaces])

    def _envelope(self, environment: str) -> bytes:
        return graph.encode(dict(format=OBLIGATION_FORMAT, environment=environment, **self._components()))

    def reference_payload(self) -> bytes:
        return self._reference_payload

    @cached_property
    def _reference_payload(self) -> bytes:
        return graph.encode(dict(format=IDENTITY_REFERENCE_FORMAT, obligation=self.id,
                              environment=self.environment_id, **self._components()))

    @cached_property
    def id(self) -> str:
        return 'obl_' + sha256(self._payload).hexdigest()


def validate_signature(value, *, _validated_graph=False):
    """Validate the frozen structural grammar, binding depth and universe scope."""
    from .encoding import fields, array, natural, name
    if not _validated_graph:
        graph.encode(value)  # Reject cyclic, oversized or unsupported caller-owned graphs.
    fields(value, ('target', 'universes', 'context'))
    universes = array(value['universes'])
    for item in universes:
        name(item)
    # Universe names arrive inside binary graphs and may contain large strings.
    # Compare their binary keys without projecting them through JSON.
    scope = {graph.encode(item) for item in universes}
    if len(scope) != len(universes):
        raise ValueError('Duplicate universe parameter')

    checked_levels = set()
    def level(v):
        if id(v) in checked_levels:
            return
        level_impl(v)
        checked_levels.add(id(v))

    def level_impl(v):
        array(v)
        if v == ['zero']:
            return
        if len(v) == 2 and v[0] == 'param':
            name(v[1])
            if graph.encode(v[1]) not in scope:
                raise ValueError('Unbound universe parameter')
        elif len(v) == 2 and v[0] == 'succ':
            level(v[1])
        elif len(v) == 3 and v[0] in ('max', 'imax'):
            level(v[1]); level(v[2])
        else:
            raise ValueError('Unsupported Lean level')

    def binder(v):
        if v not in ('explicit', 'implicit', 'strictImplicit', 'instance'):
            raise ValueError('Invalid binder')

    def kind(v):
        if v not in ('default', 'implementation-detail', 'auxiliary'):
            raise ValueError('Invalid local kind')

    checked_expressions = set()
    def expr(v, depth):
        key = (id(v), depth)
        if key in checked_expressions:
            return
        expr_impl(v, depth)
        checked_expressions.add(key)

    def expr_impl(v, depth):
        array(v)
        if not v:
            raise ValueError('Empty Lean expression')
        tag = v[0]
        if tag == 'bvar' and len(v) == 2:
            natural(v[1])
            if v[1] >= depth:
                raise ValueError('Loose bound variable')
        elif tag == 'sort' and len(v) == 2:
            level(v[1])
        elif tag == 'const' and len(v) == 3:
            name(v[1])
            for u in array(v[2]):
                level(u)
        elif tag == 'app' and len(v) == 3:
            expr(v[1], depth); expr(v[2], depth)
        elif tag in ('lam', 'forall') and len(v) == 5:
            name(v[1]); binder(v[2]); expr(v[3], depth); expr(v[4], depth + 1)
        elif tag == 'let' and len(v) == 6 and type(v[5]) is bool:
            name(v[1]); expr(v[2], depth); expr(v[3], depth); expr(v[4], depth + 1)
        elif tag == 'nat' and len(v) == 2:
            natural(v[1])
        elif tag == 'string' and len(v) == 2 and isinstance(v[1], str):
            pass
        elif tag == 'proj' and len(v) == 4:
            name(v[1]); natural(v[2]); expr(v[3], depth)
        else:
            raise ValueError('Unsupported Lean expression')

    context = array(value['context'])
    depth = 0
    if context and context[0] == TAG:
        validate_context(context)
    elif context != ['closed-telescope']:
        if len(context) != 3 or context[0] != 'local-context':
            raise ValueError('Unsupported Lean context')
        for entry in array(context[1]):
            array(entry)
            if len(entry) == 5 and entry[0] == 'local':
                name(entry[1]); binder(entry[2]); kind(entry[3]); expr(entry[4], depth)
            elif len(entry) == 6 and entry[0] == 'local-let' and type(entry[5]) is bool:
                name(entry[1]); kind(entry[2]); expr(entry[3], depth); expr(entry[4], depth)
            else:
                raise ValueError('Invalid local declaration')
            depth += 1
        for entry in array(context[2]):
            array(entry)
            if len(entry) != 3 or entry[0] != 'local-instance':
                raise ValueError('Invalid local instance')
            name(entry[1]); expr(entry[2], depth)
            if entry[2][0] != 'bvar':
                raise ValueError('Local instance must reference a local')
    expr(value['target'], depth)


def validate_identity(identity):
    from .encoding import fields, name
    if not isinstance(identity, ObligationIdentity) or identity.serializer != LEAN_SERIALIZER:
        raise ValueError('Unsupported persisted identity version')
    validate_signature({k: graph.decode(getattr(identity, k)) for k in ('target', 'universes', 'context')}, _validated_graph=True)
    for interface in identity.prerequisite_interfaces:
        item = graph.decode(interface)
        fields(item, ('constant', 'signature'))
        name(item['constant'])
        validate_signature(item['signature'], _validated_graph=True)


def _load_identity(payload, environment_id):
    from .encoding import fields, array
    if not isinstance(payload, bytes) or not payload.startswith(graph.MAGIC):
        raise ValueError('Persisted identity requires a canonical binary graph')
    identifier(environment_id, 'env_')
    value = graph.decode(payload)
    if not isinstance(value, dict) or value.get('format') not in (IDENTITY_REFERENCE_FORMAT, OBLIGATION_FORMAT):
        raise ValueError('Unsupported persisted identity version')
    reference = value['format'] == IDENTITY_REFERENCE_FORMAT
    fields(value, ('format', 'serializer', 'target', 'universes', 'context',
                   'environment', 'prerequisite_interfaces') + (('obligation',) if reference else ()))
    if value['environment'] != environment_id:
        raise ValueError('Referenced identity environment mismatch')
    identity = ObligationIdentity(*(graph.encode(value[k]) for k in ('target', 'universes', 'context')),
        environment_id, tuple(graph.encode(v) for v in array(value['prerequisite_interfaces'])), value['serializer'])
    validate_identity(identity)
    if reference and value['obligation'] != identity.id:
        raise ValueError('Identity hash mismatch')
    identity.id
    # Do not retain expanded envelopes in the bounded admission cache.
    identity.__dict__.pop('_payload', None)
    if reference:
        object.__setattr__(identity, '_reference_payload', payload)
    return identity


def load_identity(payload, *, expected_id=None, environment_id=None):
    identifier(environment_id, 'env_')
    if isinstance(payload, bytes):
        private = _IDENTITY_CACHE.get(environment_id.encode('ascii'), payload,
                                      lambda: _load_identity(payload, environment_id))
        identity = copy(private)
    else:
        identity = _load_identity(payload, environment_id)
    if expected_id is not None and identity.id != expected_id:
        raise ValueError('Identity hash mismatch')
    return identity




TAG = 'declaration-context-v2'
CLOSED = ['closed-telescope']


def bounded_text(value, label, limit=256):
    if (not isinstance(value, str) or len(value) > limit or not value.strip()
            or len(value.encode('utf-8')) > limit):
        raise ValueError(f'Invalid or oversized {label}')


def simple_name(value):
    bounded_text(value, 'local name')
    if value == '_' or not re.fullmatch(r"[^\W\d]\w*'*", value):
        raise ValueError('Expected a simple Lean local name')


def validate_definitions(value):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError('Expected at most 64 local definitions')
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('Expected context definition object')
        fields(item, {'name', 'type', 'value'} | ({'purpose', 'kind'} & item.keys()))
        if item.get('kind', 'def') not in ('def', 'abbrev'):
            raise ValueError('Expected def or abbrev')
        simple_name(item['name'])
        if item['name'] in seen:
            raise ValueError('Duplicate local definition')
        seen.add(item['name'])
        for key in ('type', 'value'):
            bounded_text(item[key], 'definition ' + key, 131072)
        if 'purpose' in item:
            bounded_text(item['purpose'], 'definition purpose', 8000)


def make_context(scope, definitions):
    bounded_text(scope, 'declaration source scope', 1024**2)
    validate_definitions(definitions)
    digest = sha256(b'MidasContext\x02')
    hash_field(digest, scope.encode('utf-8'))
    rows = []
    for definition in definitions:
        item = {key: definition[key] for key in ('name', 'type', 'value')}
        item['kind'] = definition.get('kind', 'def')
        for key in ('name', 'kind', 'type', 'value'):
            hash_field(digest, item[key].encode('utf-8'))
        namespace = 'MidasContext_' + digest.hexdigest()
        rows.append(dict(item, namespace=namespace))
    return [TAG, scope, rows] if rows else list(CLOSED)


def validate_context(context):
    if context == CLOSED:
        return context
    if (not isinstance(context, list) or len(context) != 3 or context[0] != TAG
            or not isinstance(context[2], list)):
        raise ValueError('Invalid declaration context')
    definitions = []
    for row in context[2]:
        fields(row, ('name', 'kind', 'type', 'value', 'namespace'))
        definitions.append({k: v for k, v in row.items() if k != 'namespace'})
    if make_context(context[1], definitions) != context:
        raise ValueError('Declaration context identity mismatch')
    return context


def extends(context, parent):
    validate_context(context)
    validate_context(parent)
    return parent == CLOSED or (context != CLOSED and context[1] == parent[1]
                                and context[2][:len(parent[2])] == parent[2])


def entry_context(entry):
    return entry.get('declaration_context', entry['subject'].get('declaration_context', CLOSED))


def set_entry_context(entry, context):
    if context == entry['subject'].get('declaration_context', CLOSED):
        entry.pop('declaration_context', None)
    else:
        entry['declaration_context'] = context


_IMPORT_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')


def validate_imports(imports):
    if (not isinstance(imports, list) or len(imports) > 32
            or any(not isinstance(module, str) or len(module) > 256 or not _IMPORT_NAME.fullmatch(module)
                   for module in imports) or len(set(imports)) != len(imports)):
        raise ValueError('Expected at most 32 distinct canonical candidate imports')
    if any(module == 'MidasCheck' or module.startswith(('MidasCheck.', 'MidasDecl_')) for module in imports):
        raise ValueError('Candidate cannot import private checker modules')
    return imports


def entry_imports(value, entry):
    return validate_imports(list(dict.fromkeys(
        module for imports in (value.get('imports', []), entry.get('imports', []))
        for module in validate_imports(imports))))




INTERFACE_LIMIT = 1024 * 1024


def encode_interface(value):
    return codec.encode(value, limit=INTERFACE_LIMIT)


def decode_interface(payload):
    return codec.decode(payload, limit=INTERFACE_LIMIT)


@dataclass(frozen=True, slots=True)
class BoundDependency:
    identity: ObligationIdentity
    parameter: bytes
    interface: bytes
    origin: str = 'abstracted_lemma'


def validate_dependencies(owner, dependencies):
    """Match exact Lean signatures, names, environment, and frozen peer order."""
    validate_identity(owner)
    if type(dependencies) is not tuple or not 1 <= len(dependencies) <= 128:
        raise ValueError('Conditional plans require 1 through 128 dependencies')
    parameters = set()
    peer_order = -1
    for dep in dependencies:
        if not isinstance(dep, BoundDependency):
            raise ValueError('Expected bound dependency')
        validate_identity(dep.identity)
        parameter = load_json(dep.parameter, limit=16384)
        name(parameter)
        key = dep.parameter  # The metadata loader already checked canonical bytes.
        if parameter == ['anonymous'] or key in parameters:
            raise ValueError('Anonymous or duplicate dependency parameter')
        parameters.add(key)
        if dep.identity.id == owner.id:
            raise ValueError('Obligation depends on itself')
        if dep.identity.environment_id != owner.environment_id:
            raise ValueError('Dependency environment differs')
        interface = decode_interface(dep.interface)
        fields(interface, ('constant', 'signature'))
        name(interface['constant'])
        validate_signature(interface['signature'], _validated_graph=True)
        expected = {key: codec.decode(getattr(dep.identity, key)) for key in ('target', 'universes', 'context')}
        if codec.encode(interface['signature']) != codec.encode(expected):
            raise ValueError('Dependency interface does not match obligation')
        if dep.origin == 'frozen_peer':
            frozen = codec.encode(interface)
            try:
                ordinal = owner.prerequisite_interfaces.index(frozen)
            except ValueError as error:
                raise ValueError('Peer is not permitted by the frozen identity') from error
            if ordinal <= peer_order:
                raise ValueError('Frozen peer dependency order differs')
            peer_order = ordinal
        elif dep.origin == 'abstracted_lemma':
            if interface['constant'] != parameter:
                raise ValueError('Abstracted interface parameter differs')
        else:
            raise ValueError('Unknown dependency origin')
    return dependencies
