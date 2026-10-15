"""Binary evidence validation. Syntax alone never authorizes a proof."""
from hashlib import sha256

from ..domain import Signature, validate_signature
from ..domain import encoding as codec
from ..domain.identity import CLOSED, bounded_text, validate_imports

ROOT = ['str', ['anonymous'], 'MidasCandidateResult']
PROOF = 'midas-source-proof-v1'
INTERFACE = 'midas-elaborated-interface-v1'
NATIVE = b'MIDAS_NATIVE_V2\n'


def closed(value):
    validate_signature(value, _validated_graph=True)
    if value['context'] != CLOSED and value['context'][0] != 'declaration-context-v2':
        raise ValueError('Expected a closed signature')
    return Signature(*(codec.encode(value[k]) for k in ('target', 'universes', 'context')))


def decode_output(payload, *, maximum):
    if type(payload) is not bytes or not 0 < len(payload) <= maximum:
        raise ValueError('Missing or oversized compiler evidence')
    if not payload.startswith(NATIVE):
        return codec.decode(payload, limit=maximum), ()
    pos = len(NATIVE)
    if len(payload) < pos + 8:
        raise ValueError('Truncated native frame')
    size = int.from_bytes(payload[pos:pos+8], 'little')
    pos += 8
    if size > len(payload) - pos:
        raise ValueError('Truncated native header')
    header = codec.decode(payload[pos:pos+size], limit=maximum)
    pos += size
    codec.fields(header, ('format', 'proof', 'artifacts'))
    if header['format'] != 'midas-native-declarations-v2':
        raise ValueError('Unknown native frame')
    rows = header['artifacts']
    if type(rows) is not list or not 1 <= len(rows) <= 256:
        raise ValueError('Invalid native batch')
    result, seen, total = [], set(), 0
    view = memoryview(payload)
    for row in rows:
        codec.fields(row, ('id', 'bytes'))
        name, size = row['id'], row['bytes']
        if name != 'producer':
            from ..domain.records import identifier
            identifier(name, '')
        if name in seen or type(size) is not int or not 0 < size <= min(8 * 1024**2, len(payload) - pos):
            raise ValueError('Invalid native artifact')
        total += size
        if total > 8 * 1024**2:
            raise ValueError('Native batch exceeds 8 MiB')
        seen.add(name)
        # Native cache blobs are opaque. Retain a view until materialization,
        # instead of copying up to 8 MiB merely to split the evidence frame.
        result.append((name, view[pos:pos+size]))
        pos += size
    if pos != len(payload):
        raise ValueError('Trailing native bytes')
    return header['proof'], tuple(result)


def certificate(value, digest):
    if type(value) is not dict:
        raise ValueError('Expected a certificate record')
    extra = ('imports',) if 'imports' in value else ()
    validate_imports(value.get('imports', []))
    if value.get('format') == INTERFACE:
        codec.fields(value, ('format', 'request', 'purpose', 'statement', 'signature', *extra))
        if value['request'] != digest or value['purpose'] != 'interface':
            raise ValueError('Interface binding differs')
        bounded_text(value['statement'], 'bound statement', 1024**2)
        closed(value['signature'])
        return value
    if 'assumptions' in value:
        extra += ('assumptions',)
    codec.fields(value, ('format', 'request', 'purpose', 'signature', 'declarations', *extra))
    if value['format'] != PROOF or value['request'] != digest or value['purpose'] != 'proof':
        raise ValueError('Proof certificate binding differs')
    closed(value['signature'])
    assumptions = value.get('assumptions', [])
    if type(assumptions) is not list or len(assumptions) > 128 or 'assumptions' in value and not assumptions:
        raise ValueError('Invalid conditional assumptions')
    names = set()
    for row in assumptions:
        codec.fields(row, ('parameter', 'signature'))
        codec.name(row['parameter'])
        key = codec.encode(row['parameter'])
        if row['parameter'] == ['anonymous'] or key in names:
            raise ValueError('Anonymous or duplicate assumption')
        names.add(key)
        closed(row['signature'])
    rows = value['declarations']
    if type(rows) is not list or not 1 <= len(rows) <= 4096 or assumptions and len(rows) != 1:
        raise ValueError('Invalid certificate declarations')
    seen = set()
    for row in rows:
        codec.fields(row, ('name', 'signature', 'source', 'requires'))
        codec.name(row['name'])
        key = codec.encode(row['name'])
        if row['name'] == ['anonymous'] or key in seen:
            raise ValueError('Duplicate or anonymous proof declaration')
        closed(row['signature'])
        bounded_text(row['source'], 'proof source', 1024**2)
        if type(row['requires']) is not list or len(row['requires']) > 4096:
            raise ValueError('Invalid declaration dependencies')
        for dependency in row['requires']:
            codec.name(dependency)
            if codec.encode(dependency) not in seen:
                raise ValueError('Proof declarations are not in dependency order')
        seen.add(key)
    if rows[-1]['name'] != ROOT:
        raise ValueError('Missing final proof declaration')
    return value


def proof_for(value, packet, digest):
    certificate(value, digest)
    if value['format'] != PROOF or value.get('imports', []) != packet.get('imports', []):
        raise ValueError('Unexpected proof kind or imports')
    expected = packet.get('statement_interface', {}).get('signature', packet['signature'])
    if codec.encode(value['signature']) != codec.encode(expected):
        raise ValueError('Proof subject differs from request')
    assumptions = ([{'parameter': d['parameter'], 'signature': d['interface']['signature']}
                    for d in packet['dependencies']]
                   if packet.get('dependency_protocol') == 'polymorphic-lemmas-v1' else [])
    if codec.encode(value.get('assumptions', [])) != codec.encode(assumptions):
        raise ValueError('Proof assumptions differ from request')
    code = packet['candidate']
    if len(value['declarations']) != 1 or value['declarations'][0]['source'] != code:
        raise ValueError('Produced source differs from submitted candidate')
    return value


def binding_for(value, packet, digest):
    codec.fields(value, ('format', 'request', 'parameters', 'certificate'))
    if value['format'] != 'midas-binding-result-v2' or value['request'] != digest:
        raise ValueError('Binding result differs from request')
    from ..source import parse_identifier
    if value['parameters'] != [parse_identifier(n) for n in packet['parameters']]:
        raise ValueError('Resolved binding names differ')
    cert = value['certificate']
    if packet['interface'] is None:
        if cert is not None:
            raise ValueError('Unexpected statement interface')
    else:
        certificate(cert, digest)
        if (cert['format'] != INTERFACE or cert['statement'] != packet['interface']['statement']
                or cert.get('imports', []) != packet.get('imports', [])
                or codec.encode(cert['signature']['context']) != codec.encode(packet['interface']['statement_context'])):
            raise ValueError('Bound interface differs from request')
    return value


def replay_receipt(cert, digest):
    return {'format': 'midas-proof-replay-receipt-v1', 'request': digest,
            'certificate_sha256': sha256(codec.encode(cert)).hexdigest()}


def lookup_for(value, packet, digest):
    codec.fields(value, ('format', 'request', 'records'))
    source = packet.get('body_format') == 'source'
    expected = 'midas-lookup-result-source-v1' if source else 'midas-lookup-result-context-v1'
    if value['format'] != expected or value['request'] != digest or len(value['records']) != len(packet['names']):
        raise ValueError('Lookup result differs from request')
    for query, row in zip(packet['names'], value['records']):
        codec.fields(row, ('query', 'status', 'constant', 'kind', 'type', 'body', 'error',
                           *(('source_range',) if source else ())))
        if row['query'] != query or row['status'] not in ('available', 'unavailable'):
            raise ValueError('Lookup query differs')
        if row['status'] == 'unavailable':
            if type(row['error']) is not str or any(row[k] is not None for k in ('constant', 'kind', 'type', 'body')):
                raise ValueError('Invalid unavailable lookup')
        else:
            codec.name(row['constant'])
            if (row['kind'] not in ('theorem', 'def', 'inductive', 'constructor', 'recursor', 'parameter')
                    or row['error'] is not None or type(row['type']) is not str):
                raise ValueError('Invalid available lookup')
            if row['body'] is not None and (source or not packet['include_body'] or row['kind'] != 'def'
                                            or type(row['body']) is not str):
                raise ValueError('Unrequested lookup body')
        if source and row['source_range'] is not None:
            span = row['source_range']
            codec.fields(span, ('module', 'start', 'end'))
            if row['status'] != 'available' or span['module'] is not None and type(span['module']) is not str:
                raise ValueError('Invalid lookup source module')
            for pos in (span['start'], span['end']):
                if type(pos) is not list or len(pos) != 2 or any(type(n) is not int or n < 0 for n in pos) or pos[0] == 0:
                    raise ValueError('Invalid lookup source range')
            if span['end'] < span['start']:
                raise ValueError('Reversed lookup source range')
    return value


def probe_for(value, case, digest):
    codec.fields(value, ('format', 'request', 'row'))
    if value['format'] != 'midas-probe-case-v1' or value['request'] != digest:
        raise ValueError('Probe result differs from request')
    codec.fields(value['row'], ('result', 'messages'))
    result = value['row']['result']
    if result.get('id') != case['id'] or result.get('operation') != case['operation']:
        raise ValueError('Probe case differs from request')
    if result.get('status') not in ('open', 'closed_exploratory', 'inspected', 'contains_sorry', 'error', 'interrupted'):
        raise ValueError('Unknown probe status')
    if 'diagnostic' in result:
        codec.fields(result, ('id', 'operation', 'status', 'diagnostic'))
        if result['status'] not in ('error', 'interrupted') or type(result['diagnostic']) is not str:
            raise ValueError('Invalid probe diagnostic')
    else:
        codec.fields(result, ('id', 'operation', 'status', 'goals', 'contains_sorry', 'unresolved_metavariables', 'inspection'))
        if (type(result['goals']) is not list or any(type(g) is not str for g in result['goals'])
                or type(result['contains_sorry']) is not bool or type(result['unresolved_metavariables']) is not bool
                or type(result['inspection']) is not dict):
            raise ValueError('Invalid probe goal state')
        if result['inspection']:
            inspection = result['inspection']
            codec.fields(inspection, ('expression', 'inferred_type', 'contains_sorry', 'unresolved_metavariables'))
            if (any(type(inspection[k]) is not str for k in ('expression', 'inferred_type'))
                    or any(type(inspection[k]) is not bool for k in ('contains_sorry', 'unresolved_metavariables'))):
                raise ValueError('Invalid expression inspection')
    if type(value['row']['messages']) is not list:
        raise ValueError('Invalid probe messages')
    for message in value['row']['messages']:
        codec.fields(message, ('severity', 'file', 'line', 'column', 'text'))
        if (message['severity'] not in ('information', 'warning', 'error')
                or any(type(message[k]) is not str for k in ('file', 'text'))
                or any(type(message[k]) is not int or message[k] < 0 for k in ('line', 'column'))):
            raise ValueError('Invalid probe diagnostic message')
    return value
