"""Binary evidence validation. Syntax alone never authorizes a proof."""
from hashlib import sha256
from heapq import merge

from ..domain import Signature, validate_signature
from ..domain import encoding as codec
from ..domain.identity import CLOSED, bounded_text, validate_imports

ROOT = ['str', ['anonymous'], 'MidasCandidateResult']
PROOF = 'midas-source-proof-v1'
INTERFACE = 'midas-elaborated-interface-v1'
NATIVE = b'MIDAS_NATIVE_V2\n'


def source_aliases(value, generated):
    """Validate the bounded, ordered names in an admitted kernel comparison."""
    if type(generated) is not list or len(generated) > 100000:
        raise ValueError('Invalid generated declaration list')
    for name in generated:
        codec.name(name)
    if type(value) is not list or len(value) > len(generated):
        raise ValueError('Invalid source alias table')
    positions = {codec.encode(n): i for i, n in enumerate(generated)}
    if len(positions) != len(generated):
        raise ValueError('Duplicate generated source name')
    removed, targets = set(), set()
    for row in value:
        if type(row) is not list or len(row) != 2:
            raise ValueError('Invalid source alias')
        for n in row:
            codec.name(n)
        old, new = map(codec.encode, row)
        if (old not in positions or new not in positions or positions[new] >= positions[old]
                or old in removed):
            raise ValueError('Source alias is not an earlier generated declaration')
        removed.add(old)
        targets.add(new)
    if removed & targets:
        raise ValueError('Source aliases must refer directly to retained declarations')
    return value


def source_regions(regions, source, calls):
    size = len(source.encode())
    for spans in (regions, calls):
        if type(spans) is not list or len(spans) > 10000:
            raise ValueError('Invalid generated source regions')
        end = 0
        for span in spans:
            if (type(span) is not list or len(span) != 2 or any(type(n) is not int for n in span)
                    or not end <= span[0] < span[1] <= size):
                raise ValueError('Invalid generated source region')
            end = span[1]
    end = 0
    for span in merge(regions, calls):
        if span[0] < end:
            raise ValueError('Overlapping generated source regions')
        end = span[1]


def source_layout(layout, packet):
    source_regions(packet['regions'], packet['source'], packet['calls'])
    if type(layout) is not list or len(layout) != len(packet['share']):
        raise ValueError('Invalid generated source layout')
    region, end, count = 0, 0, 0
    def span(value, left, right):
        if (type(value) is not list or len(value) != 2 or any(type(n) is not int for n in value)
                or not left <= value[0] < value[1] <= right):
            raise ValueError('Invalid checked source span')
    def pairs(value):
        if type(value) is not list or len(value) > 100000:
            raise ValueError('Invalid checked open names')
        for pair in value:
            if type(pair) is not list or len(pair) != 2:
                raise ValueError('Invalid checked open alias')
            for name in pair:
                codec.name(name)
    for row, name in zip(layout, packet['share']):
        codec.fields(row, ('name', 'namespace', 'span', 'declaration_start', 'name_span',
                           'references', 'opens', 'inline', 'sequences'))
        codec.name(row['namespace'])
        if codec.encode(row['name']) != codec.encode(name):
            raise ValueError('Checked source declaration differs')
        span(row['span'], end, packet['regions'][-1][1] if packet['regions'] else 0)
        left, right = row['span']
        while region < len(packet['regions']) and packet['regions'][region][1] <= left:
            region += 1
        if region == len(packet['regions']):
            raise ValueError('Checked declaration is outside generated source')
        span(row['span'], *packet['regions'][region])
        span(row['name_span'], left, right)
        if type(row['declaration_start']) is not int or not left <= row['declaration_start'] < row['name_span'][0]:
            raise ValueError('Invalid checked declaration start')
        pairs(row['opens'])
        if type(row['sequences']) is not list:
            raise ValueError('Invalid checked tactic sequences')
        count += len(row['sequences'])
        previous = left - 1
        for item in row['sequences']:
            if (type(item) is not list or len(item) != 3 or any(type(n) is not int for n in item)
                    or not left <= item[0] < item[1] < item[1] + 3 < item[2] <= right
                    or item[1] <= previous):
                raise ValueError('Invalid checked tactic sequence')
            previous = item[1]
        for key in ('references', 'inline'):
            if type(row[key]) is not list:
                raise ValueError('Invalid checked source bindings')
            count += len(row[key])
            if count > 1000000:
                raise ValueError('Checked source binding limit exceeded')
            previous = left
            for item in row[key]:
                if type(item) is not list or len(item) != 3:
                    raise ValueError('Invalid checked source binding')
                span(item[:2], previous, right)
                (codec.name if key == 'references' else pairs)(item[2])
                previous = item[1]
        end = right


def _closed(value):
    validate_signature(value, _validated_graph=True)
    if value['context'] != CLOSED and value['context'][0] != 'declaration-context-v2':
        raise ValueError('Expected a closed signature')


def closed(value):
    _closed(value)
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
    checked_signatures = {}
    def check_signature(sig):
        if checked_signatures.get(id(sig)) is not sig:
            _closed(sig)
            checked_signatures[id(sig)] = sig
    extra = ('imports',) if 'imports' in value else ()
    validate_imports(value.get('imports', []))
    if value.get('format') == INTERFACE:
        codec.fields(value, ('format', 'request', 'purpose', 'statement', 'signature', *extra))
        if value['request'] != digest or value['purpose'] != 'interface':
            raise ValueError('Interface binding differs')
        bounded_text(value['statement'], 'bound statement', 1024**2)
        check_signature(value['signature'])
        return value
    if 'assumptions' in value:
        extra += ('assumptions',)
    if 'type_source' in value:
        extra += ('type_source',)
        bounded_text(value['type_source'], 'proof type source', 1024**2)
    codec.fields(value, ('format', 'request', 'purpose', 'signature', 'declarations', *extra))
    if value['format'] != PROOF or value['request'] != digest or value['purpose'] != 'proof':
        raise ValueError('Proof certificate binding differs')
    check_signature(value['signature'])
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
        check_signature(row['signature'])
    rows = value['declarations']
    if type(rows) is not list or not 1 <= len(rows) <= 4096 or assumptions and len(rows) != 1:
        raise ValueError('Invalid certificate declarations')
    seen = set()
    for row in rows:
        codec.fields(row, ('name', 'signature', 'source', 'requires',
                           *(('type_source',) if 'type_source' in row else ())))
        codec.name(row['name'])
        key = codec.encode(row['name'])
        if row['name'] == ['anonymous'] or key in seen:
            raise ValueError('Duplicate or anonymous proof declaration')
        check_signature(row['signature'])
        bounded_text(row['source'], 'proof source', 1024**2)
        if 'type_source' in row:
            bounded_text(row['type_source'], 'declaration type source', 1024**2)
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
    if ('type_source' in value and packet.get('statement') is not None
            and value['type_source'] != packet['statement']):
        raise ValueError('Produced type source differs from submitted statement')
    row = value['declarations'][0]
    if ('type_source' in row and 'type_source' in value
            and (not packet['dependencies'] or assumptions)
            and row['type_source'] != value['type_source']):
        raise ValueError('Produced declaration type differs from subject type source')
    return value


def binding_for(value, packet, digest):
    codec.fields(value, ('format', 'request', 'parameters', 'certificate', 'presentation'))
    if value['format'] != 'midas-binding-result-v3' or value['request'] != digest:
        raise ValueError('Binding result differs from request')
    bounded_text(value['presentation'], 'type presentation', 1024**2)
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
    codec.fields(value['row'], ('result', 'messages',
                               *(key for key in ('code', 'sources') if key in value['row'])))
    if 'code' in value['row']:
        from .probe import repair_candidates
        code = value['row']['code']
        if (case['operation'] != 'tactic' or type(code) is not str
                or code not in repair_candidates(case['code'])):
            raise ValueError('Probe code differs from allowed syntax repairs')
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
    if 'sources' in value['row']:
        sources = value['row']['sources']
        if type(sources) is not list or not sources:
            raise ValueError('Invalid probe input sources')
        seen = set()
        for source in sources:
            codec.fields(source, ('file', 'code'))
            label = source['file']
            parent, _, number = label.rpartition('.input') if type(label) is str else ('', '', '')
            if (type(label) is not str or type(source['code']) is not str or label in seen
                    or parent not in (case['id'] + '.' + stage for stage in ('setup', 'code', 'expected'))
                    or not number.isascii() or not number.isdigit() or number.startswith('0')):
                raise ValueError('Invalid probe input source')
            seen.add(label)
    return value
