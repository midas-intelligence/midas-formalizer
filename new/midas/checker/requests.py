"""Pure compiler preparation. Requests contain hashes, references and binary DAGs."""
from dataclasses import dataclass
from hashlib import sha256
import re

from ..domain import (ArtifactRef, Candidate, DependencyOrigin, FrozenSelection, JobKind,
                      ObligationIdentity, OwnerVersion, Signature, validate_identity, validate_signature)
from ..domain import encoding as codec
from ..domain.cache import Memo
from ..domain.identity import CLOSED, bounded_text, extends, validate_context
from ..source import GeneratedSource, add_imports, lean_name, lookup_context, parse_identifier

MAX_BYTES = 64 * 1024**2
TERM_BYTES = 1024**2
FORMAT = 'midas-compiler-request-v1'
OPERATIONS = {'binding': JobKind.CHECK_BINDING, 'proof': JobKind.CHECK_PROOF,
              'lookup': JobKind.LOOKUP, 'probe': JobKind.PROBE, 'audit': JobKind.EXPORT_VERIFY,
              'replay': JobKind.ASSEMBLE, 'integrate': JobKind.INTEGRATE}
_SIGNATURES = Memo(max_entries=128, max_bytes=16 * 1024**2)
_REQUESTS = Memo(max_entries=128, max_bytes=32 * 1024**2)


def signature(value):
    key = tuple(getattr(value, k) for k in ('target', 'universes', 'context'))
    def build():
        result = {k: codec.decode(v) for k, v in zip(('target', 'universes', 'context'), key)}
        validate_signature(result, _validated_graph=True)
        return codec.encode(result)
    # Retain compact immutable bytes only; callers own their private decoded DAG.
    return codec.decode(_SIGNATURES.get(b'', key, build))


def read(reader, reference, maximum=MAX_BYTES):
    return reference.verify(reader.read_artifact(reference, max_bytes=maximum), max_bytes=maximum)


def _candidate(value):
    return {'id': value.id, 'owner': [value.owner.obligation_id, value.owner.generation],
            'environment': value.environment_id, 'source': value.source.to_dict(),
            'dependencies': [[d.owner.obligation_id, d.owner.generation, d.alias, d.interface.to_dict(),
                              d.origin.value, d.certificate.to_dict() if d.certificate else None,
                              d.submission_id] for d in value.dependencies],
            'revision': value.revision_of, 'joint_submission': value.joint_submission_id}


@dataclass(frozen=True, slots=True)
class CompilerRequest:
    payload: bytes

    def __post_init__(self):
        if type(self.payload) is not bytes:
            raise ValueError('Expected immutable compiler request bytes')
        _REQUESTS.get(b'', self.payload, self._validate)

    def _validate(self):
        value = codec.decode(self.payload)
        codec.fields(value, ('format', 'operation', 'environment', 'selection', 'owner',
                             'candidate', 'inputs', 'packet', *(('export',) if 'export' in value else ())))
        if value['format'] != FORMAT or value['operation'] not in OPERATIONS:
            raise ValueError('Unknown compiler request')
        from ..domain.records import identifier
        identifier(value['environment'], 'env_')
        identifier(value['selection'], 'sel_')
        OwnerVersion(*value['owner'])
        if 'allow_sorry' in value['packet'] and (value['operation'] not in ('audit', 'integrate')
                or type(value['packet']['allow_sorry']) is not bool):
            raise ValueError('Sorry is an explicit source-audit option only')
        refs = tuple(ArtifactRef.from_dict(v) for v in value['inputs'])
        maximum = 40016 if 'export' in value else 20000 if value['operation'] == 'audit' else 256
        if len(refs) > maximum or len(set(refs)) != len(refs):
            raise ValueError('Invalid compiler inputs')
        if 'modules' in value['packet']:
            raise ValueError('Inline module inventories are not supported')
        if 'inventory' in value['packet']:
            if (value['operation'] not in ('audit', 'integrate')
                    or ArtifactRef.from_dict(value['packet']['inventory']) not in refs):
                raise ValueError('Module inventory must be an admitted audit input')
        return True

    @property
    def sha256(self):
        return sha256(self.payload).hexdigest()

    def decode(self):
        return codec.decode(self.payload)

    def validate_for(self, frozen, reader):
        value = self.decode()
        if (read(reader, frozen.operation_payload) != self.payload
                or frozen.environment_id != value['environment']
                or frozen.job.kind != OPERATIONS[value['operation']]
                or frozen.job.owner != OwnerVersion(*value['owner'])
                or frozen.job.input_refs != tuple(ArtifactRef.from_dict(v) for v in value['inputs'])):
            raise ValueError('Compiler request differs from reserved job')
        candidate = value['candidate']
        bound_owner = value['packet'].get('bound_owner')
        if bound_owner is not None and (value['operation'] != 'binding'
                or OwnerVersion(*bound_owner) not in frozen.job.expected_versions):
            raise ValueError('Rebound subject version is not fenced')
        if candidate is not None:
            for d in candidate['dependencies']:
                if OwnerVersion(d[0], d[1]) not in frozen.job.expected_versions:
                    raise ValueError('Compiler dependency version is not fenced')
        return value


def _request(operation, selection, owner, packet, candidate=None, inputs=()):
    return CompilerRequest(codec.encode({'format': FORMAT, 'operation': operation,
        'environment': selection.environment_id, 'selection': selection.id,
        'owner': [owner.obligation_id, owner.generation],
        'candidate': _candidate(candidate) if candidate else None,
        'inputs': [r.to_dict() for r in dict.fromkeys(inputs)], 'packet': packet}))


def _base(selection, index, imports, available):
    if not isinstance(selection, FrozenSelection) or type(index) is not int or not 0 <= index < len(selection.identities):
        raise ValueError('Invalid selected compiler target')
    identity, target = selection.identities[index], selection.selected.targets[index]
    data = selection.source
    offset = target.declaration.start
    generated = add_imports(GeneratedSource(data), imports, available=available,
                           current_module=selection.selected.request.source[:-5].replace('/', '.'))
    offset += len(generated.data) - len(data)
    packet = {'source': generated.data.decode(), 'logical': selection.selected.request.source,
              'offset': offset, 'constant': codec.load_json(target.constant),
              'signature': signature(identity), 'imports': list(imports),
              'prerequisite_interfaces': [codec.decode(v) for v in identity.prerequisite_interfaces],
              'statement': None, 'candidate': None, 'dependencies': [], 'certificate': None}
    return packet


def prepare_binding(selection, index, owner, *, statement=None, parameters=(),
                    context=None, imports=(), available=frozenset(), subject_owner=None):
    packet = _base(selection, index, imports, available)
    if owner.obligation_id != selection.identities[index].id:
        raise ValueError('Binding job must be scoped to the selected parent')
    if type(parameters) is not tuple or len(parameters) > 128:
        raise ValueError('Expected at most 128 parameter names')
    parsed = [parse_identifier(n) for n in parameters]
    if any(n is None for n in parsed) or len({codec.encode(n) for n in parsed}) != len(parsed):
        raise ValueError('Invalid or duplicate parameter names')
    context = CLOSED if context is None else context
    validate_context(context)
    if context != CLOSED and context[1] != selection.selected.targets[index].selector:
        raise ValueError('Binding context belongs to another source scope')
    packet.update(parameters=list(parameters), binding_protocol='elaborated-bindings-v3', interface=None)
    if subject_owner is not None:
        if not isinstance(subject_owner, OwnerVersion):
            raise ValueError('Expected a rebound subject owner')
        packet['bound_owner'] = [subject_owner.obligation_id, subject_owner.generation]
    if statement is not None:
        bounded_text(statement, 'statement', TERM_BYTES)
        packet['interface'] = dict(packet, purpose='interface', mode='produce', statement=statement,
            statement_protocol='elaborated-statements-v1', statement_context=context, declaration_context=context)
        for key in ('parameters', 'binding_protocol', 'interface'):
            packet['interface'].pop(key)
    return _request('binding', selection, owner, packet)


def prepare_proof(selection, index, candidate, reader, *, identity=None, statement=None,
                  dependencies=(), closed_definitions=(), context=None, imports=(), available=frozenset()):
    if not isinstance(candidate, Candidate) or candidate.environment_id != selection.environment_id:
        raise ValueError('Candidate environment differs from selection')
    identity = selection.identities[index] if identity is None else identity
    validate_identity(identity)
    if identity.id != candidate.owner.obligation_id or identity.environment_id != selection.environment_id:
        raise ValueError('Candidate owner differs from checked identity')
    if identity.prerequisite_interfaces != selection.identities[index].prerequisite_interfaces:
        raise ValueError('Checked identity belongs to another source scope')
    packet = _base(selection, index, imports, available)
    code = read(reader, candidate.source, TERM_BYTES).decode()
    bounded_text(code, 'candidate', TERM_BYTES)
    if (type(dependencies) is not tuple or len(dependencies) != len(candidate.dependencies)
            or len(dependencies) > 128):
        raise ValueError('Dependency identities differ from candidate')
    if type(closed_definitions) is not tuple:
        raise ValueError('Expected immutable closed definition admissions')
    definitions = {a.evidence: a for a in closed_definitions}
    if len(definitions) != len(closed_definitions):
        raise ValueError('Duplicate definition admission')
    refs, bindings, closed, peer_order = [candidate.source], [], [], -1
    for dep, child in zip(candidate.dependencies, dependencies):
        validate_identity(child)
        if child.id != dep.owner.obligation_id or child.environment_id != selection.environment_id:
            raise ValueError('Dependency identity differs from candidate')
        iface = codec.decode(read(reader, dep.interface, TERM_BYTES))
        codec.fields(iface, ('constant', 'signature'))
        codec.name(iface['constant'])
        if codec.encode(iface['signature']) != codec.encode(signature(child)):
            raise ValueError('Dependency signature differs from its identity')
        scope = iface['signature']['context']
        if scope != CLOSED and scope[1] != selection.selected.targets[index].selector:
            raise ValueError('Dependency definitions belong to another source scope')
        parameter = parse_identifier(dep.alias)
        if parameter is None:
            raise ValueError('Invalid dependency alias')
        if dep.origin == DependencyOrigin.FROZEN_PEER:
            try:
                ordinal = identity.prerequisite_interfaces.index(codec.encode(iface))
            except ValueError as error:
                raise ValueError('Unauthorized frozen peer') from error
            if ordinal <= peer_order:
                raise ValueError('Frozen peer order differs')
            peer_order = ordinal
        elif dep.origin == DependencyOrigin.CLOSED_DEFINITION:
            admitted = definitions.pop(dep.certificate, None)
            if admitted is None:
                raise ValueError('Definition dependency requires an admitted closed proof')
            cert = codec.decode(read(reader, dep.certificate))
            closed.append((dep, child, cert, admitted, iface))
            refs.extend((dep.interface, dep.certificate))
            continue
        else:
            # A checked obligation can be shared under different local aliases.
            # Its exact signature/identity and saved interface were checked above;
            # the parameter is a binder in this candidate, not a global constant.
            iface = dict(iface, constant=parameter)
        bindings.append({'parameter': parameter, 'interface': iface, 'origin': dep.origin.value})
        refs.append(dep.interface)
    work_context = codec.decode(identity.context) if context is None else context
    subject_context = codec.decode(identity.context)
    if not extends(work_context, subject_context):
        raise ValueError('Candidate does not inherit subject definitions')
    if work_context != CLOSED and work_context[1] != selection.selected.targets[index].selector:
        raise ValueError('Candidate context belongs to another source scope')
    packet.update(purpose='proof', candidate=code, dependencies=bindings,
                  declaration_context=work_context, statement_context=subject_context)
    if statement is not None:
        bounded_text(statement, 'statement', TERM_BYTES)
        packet.update(statement=statement, statement_interface={'signature': signature(identity)})
    elif identity.id != selection.identities[index].id:
        raise ValueError('Derived obligation requires its bound statement')
    if bindings and all(d['origin'] == 'abstracted_lemma' for d in bindings):
        packet['dependency_protocol'] = 'polymorphic-lemmas-v1'
    if definitions:
        raise ValueError('Unrequested definition admissions')
    if closed:
        from .proofs import bind_definitions
        bind_definitions(selection, index, closed, packet, available=available)
    if bindings and 'dependency_protocol' not in packet:
        for dependency in reversed(bindings):
            packet['candidate'] = ('(fun ' + lean_name(dependency['parameter']) + ' =>\n'
                                   + packet['candidate'] + '\n)')
    return _request('proof', selection, candidate.owner, packet, candidate, refs)


def prepare_lookup(selection, index, owner, names, *, aliases=(), include_body=False,
                   source_ranges=False, imports=(), available=frozenset()):
    if (type(names) is not tuple or not 1 <= len(names) <= 16 or len(set(names)) != len(names)
            or any(parse_identifier(n) is None for n in names)
            or type(include_body) is not bool or type(source_ranges) is not bool
            or source_ranges and not include_body):
        raise ValueError('Expected 1 through 16 distinct lookup identifiers')
    # Do not decode the selected goal, nor elaborate its statement or proof.
    scope = lookup_context(selection, index, names, aliases=aliases)
    prefix = scope.source.data[:scope.prefix_end] + b'\n'
    source = add_imports(GeneratedSource(prefix), imports, available=available)
    packet = {'format': 'midas-lookup-packet-v3', 'source': source.data.decode() + 'set_option autoImplicit false\n',
        'logical': scope.source.path, 'offset': len(source.data), 'names': list(names),
        'include_body': include_body, 'lookup_aliases': [
            {'parameter': parse_identifier(a.name), 'signature': signature(a.signature)} for a in scope.aliases]}
    if source_ranges:
        packet['body_format'] = 'source'
    return _request('lookup', selection, owner, packet)


@dataclass(frozen=True, slots=True)
class ProbeCase:
    id: str
    code: str
    operation: str = 'tactic'
    setup: str = ''
    expected: str | None = None

    def __post_init__(self):
        if type(self.id) is not str or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,47}', self.id):
            raise ValueError('Invalid probe case ID')
        if self.operation not in ('tactic', 'term', 'synth'):
            raise ValueError('Invalid probe operation')
        bounded_text(self.code, 'probe code', 16000)
        if type(self.setup) is not str or len(self.setup.encode()) > 16000:
            raise ValueError('Invalid probe setup')
        if self.expected is not None:
            bounded_text(self.expected, 'probe expected type', 16000)


def prepare_probe(selection, index, owner, cases, *, goal=None, aliases=(),
                  imports=(), available=frozenset()):
    if (type(cases) is not tuple or not 1 <= len(cases) <= 3
            or any(not isinstance(c, ProbeCase) for c in cases)
            or len({c.id for c in cases}) != len(cases)):
        raise ValueError('Expected 1 through 3 distinct probe cases')
    packet = _base(selection, index, imports, available)
    if goal is not None or aliases:
        packet['helper_signature'] = signature(goal or selection.identities[index])
        packet['helper_dependencies'] = [dict(parameter=parse_identifier(a.name), origin='abstracted_lemma',
            interface={'constant': parse_identifier(a.name), 'signature': signature(a.signature)}) for a in aliases]
    packet['cases'] = [{'id': c.id, 'operation': c.operation, 'code': c.code,
                        'setup': c.setup, 'expected': c.expected} for c in cases]
    return _request('probe', selection, owner, packet)


def prepare_audit(selection, owner, source, reader, *, available=frozenset(), allow_sorry=False):
    """Prepare independent whole-source kernel auditing for the export owner."""
    data = read(reader, source).decode()
    packet = {'source': data, 'logical': selection.selected.request.source, 'offset': 0,
              'targets': [{'constant': codec.load_json(target.constant), 'signature': signature(identity)}
                          for target, identity in zip(selection.selected.targets, selection.identities)]}
    if type(allow_sorry) is not bool:
        raise ValueError('Expected a boolean sorry option')
    if allow_sorry:
        packet['allow_sorry'] = True
    return _request('audit', selection, owner, packet, inputs=(source,))
