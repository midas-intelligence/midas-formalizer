"""Small JSON metadata, shallow binary records, and binary expression graphs."""

import json
import re

from struct import Struct

from .cache import Memo


OBLIGATION_FORMAT = "midas-obligation-envelope-v3"
LEAN_SERIALIZER = "lean-structural-v1"
ENVIRONMENT_FORMAT = "midas-environment-v1"
LAUNCH_PROFILE = "linux-bwrap-direct-lean-v1"
ARTIFACT_VERSION = 2
SEGMENT_MAGIC = b"MIDASSEG"
CHUNK_MAGIC = b"MIDASCHK"
SEGMENT_HEADER = Struct("<8sII16s")
CHUNK_HEADER = Struct("<8sB7sQQ32s")
INLINE_LIMIT = 16 * 1024
CHUNK_LIMIT = 4 * 1024 * 1024
SEGMENT_LIMIT = 256 * 1024 * 1024




JSON_LIMIT = 16 * 1024
JSON_DEPTH = 16
_JSON_ESCAPES = re.compile(r'["\\\x00-\x1f]')
_JSON_ADMISSIONS = Memo(max_entries=2048, max_bytes=32 * 1024**2)


def canonical(value, *, ascii=False):
    """Encode small metadata only; reject large values before invoking JSON."""
    _metadata_budget(value, ascii=ascii)
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=ascii, allow_nan=False).encode('utf-8')


def _metadata_budget(value, *, ascii=False):
    pending = [(value, 0)]
    size = 0
    while pending:
        item, depth = pending.pop()
        if depth > JSON_DEPTH or type(item) in (list, dict) and depth >= JSON_DEPTH:
            raise ValueError('JSON metadata exceeds depth limit')
        if type(item) is str:
            if len(item) > JSON_LIMIT:
                raise ValueError('JSON metadata exceeds byte budget')
            encoded_size = len(item.encode('utf-8'))
            if ascii and not item.isascii():
                encoded_size = sum(1 if ord(c) < 128 else 6 if ord(c) <= 0xffff else 12 for c in item)
            size += 2 + encoded_size
            size += sum(1 if m[0] in '"\\\b\f\n\r\t' else 5 for m in _JSON_ESCAPES.finditer(item))
        elif type(item) is list:
            size += 2 + len(item)
            if size <= JSON_LIMIT:
                pending.extend((child, depth + 1) for child in item)
        elif type(item) is dict:
            size += 2 + 2 * len(item)
            if size <= JSON_LIMIT:
                if any(type(key) is not str for key in item):
                    raise ValueError('JSON metadata keys must be strings')
                pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif type(item) is int:
            if item.bit_length() > 4096:
                raise ValueError('JSON metadata integer exceeds limit')
            size += len(str(item))
        elif item is None or type(item) in (bool, float):
            size += 32
        else:
            raise ValueError('Unsupported JSON metadata value')
        if size > JSON_LIMIT:
            raise ValueError('JSON metadata exceeds byte budget')


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('Invalid persisted object fields')


def load_json(payload, *, ascii=False, limit=JSON_LIMIT):
    limit = min(limit, JSON_LIMIT)
    if not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
        raise ValueError('Persisted input exceeds byte limit or is empty')
    # Only admission of these exact bytes is reusable. Never retain a decoded
    # dict/list: callers may mutate it, and copying large trees is expensive.
    # Check each caller's byte limit above, including on a warm admission.
    decoded = []
    def admit():
        decoded.append(_load_json(payload, ascii=ascii))
        return True
    _JSON_ADMISSIONS.get(b'', (payload, bool(ascii)), admit)
    # Admitted bytes are already known to be UTF-8; avoid encoding detection.
    return decoded[0] if decoded else json.loads(payload.decode('utf-8'))


def load_metadata(payload):
    """Read ordinary small JSON documents with the same size/shape admission."""
    if type(payload) is not bytes or not 0 < len(payload) <= JSON_LIMIT:
        raise ValueError('JSON metadata exceeds byte limit or is empty')
    return _load_json(payload, ascii=False, exact=False)


def _load_json(payload, *, ascii, exact=True):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate persisted JSON key')
            result[key] = value
        return result
    try:
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
                if depth > JSON_DEPTH:
                    raise ValueError('JSON metadata exceeds depth limit')
            elif byte in (93, 125):
                depth -= 1
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=pairs)
        pending = [(value, 0)]
        nodes = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > JSON_DEPTH or nodes > JSON_LIMIT:
                raise ValueError('Persisted input exceeds structural limits')
            children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
            pending.extend((child, depth + 1) for child in children)
        normalized = canonical(value, ascii=ascii)
        if exact and normalized != payload:
            raise ValueError('Noncanonical persisted JSON')
        return value
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid persisted JSON encoding or depth') from error


def array(value):
    if not isinstance(value, list):
        raise ValueError('Expected array')
    return value


def natural(value):
    if type(value) is not int or value < 0:
        raise ValueError('Expected natural number')


def name(value):
    seen = set()
    while True:
        array(value)
        if id(value) in seen:
            raise ValueError('Cyclic structural Lean name')
        seen.add(id(value))
        if value == ['anonymous']:
            return
        if len(value) != 3 or value[0] not in ('str', 'num'):
            raise ValueError('Invalid structural Lean name')
        if value[0] == 'str':
            if not isinstance(value[2], str):
                raise ValueError('Invalid name string')
            value[2].encode('utf-8')
        else:
            natural(value[2])
        value = value[1]


MAGIC = b'MIDASDG\x01'
MAX_BYTES = 64 * 1024**2
MAX_NODES = 1_000_000
MAX_EDGES = 4_000_000
MAX_DEPTH = 128


class GraphLimit(ValueError):
    pass


def _uint(n):
    out = bytearray()
    while n >= 128:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def encode(value, *, limit=MAX_BYTES):
    limit = min(limit, MAX_BYTES)
    nodes, intern, objects, active, heights = [], {}, {}, set(), []
    edges = 0
    total = len(MAGIC) + 2
    def visit(v, depth=0):
        nonlocal edges, total
        if depth > MAX_DEPTH:
            raise GraphLimit('Certificate graph depth limit exceeded')
        container = isinstance(v, (list, dict))
        if container:
            if id(v) in active:
                raise ValueError('Cyclic certificate graph')
            if id(v) in objects:
                index = objects[id(v)]
                if depth + heights[index] > MAX_DEPTH:
                    raise GraphLimit('Certificate graph depth limit exceeded')
                return index
            active.add(id(v))
        if v is None: key = (0,)
        elif type(v) is bool: key = (2 if v else 1,)
        elif type(v) is int and v >= 0:
            text = str(v).encode('ascii')
            if len(text)>4096: raise ValueError('Oversized graph natural')
            key = (3, text)
        elif isinstance(v, str): key = (4, v.encode('utf-8'))
        elif isinstance(v, list): key = (5, tuple(visit(x, depth+1) for x in v))
        elif isinstance(v, dict):
            if any(not isinstance(k, str) for k in v):
                raise ValueError('Certificate object keys must be strings')
            key = (6, tuple(i for k in sorted(v) for i in (visit(k,depth+1),visit(v[k],depth+1))))
        else: raise ValueError('Unsupported certificate graph value')
        index = intern.get(key)
        if index is None:
            index = len(nodes)
            if index >= MAX_NODES: raise GraphLimit('Certificate graph node limit exceeded')
            tag = key[0]
            record = bytes([tag])
            if tag in (3,4): record += _uint(len(key[1])) + key[1]
            elif tag in (5,6):
                refs = key[1]; edges += len(refs)
                if edges > MAX_EDGES: raise GraphLimit('Certificate graph edge limit exceeded')
                record += _uint(len(refs)) + b''.join(_uint(i) for i in refs)
            total += len(record)
            if total > limit: raise GraphLimit('Certificate graph byte limit exceeded')
            heights.append(1+max((heights[i] for i in key[1]),default=-1) if tag in (5,6) else 0)
            nodes.append(record); intern[key] = index
        if container:
            active.remove(id(v)); objects[id(v)] = index
        return index
    root = visit(value)
    result = MAGIC + _uint(len(nodes)) + b''.join(nodes) + _uint(root)
    if len(result) > limit: raise GraphLimit('Certificate graph byte limit exceeded')
    return result


def decode(payload, *, limit=MAX_BYTES):
    if not isinstance(payload, bytes): raise ValueError('Expected certificate bytes')
    limit = min(limit, MAX_BYTES)
    if not payload.startswith(MAGIC):
        raise ValueError('Expected binary certificate graph')
    if not 0 < len(payload) <= limit: raise GraphLimit('Certificate graph byte limit exceeded')
    pos = len(MAGIC)
    def uint():
        nonlocal pos
        n = 0
        for shift in range(0,35,7):
            if pos >= len(payload): raise ValueError('Truncated certificate graph')
            b = payload[pos]; pos += 1; n |= (b & 127) << shift
            if b < 128:
                if shift and b == 0: raise ValueError('Noncanonical graph integer')
                return n
        raise ValueError('Oversized graph integer')
    count = uint()
    if not 0 < count <= MAX_NODES: raise GraphLimit('Certificate graph node limit exceeded')
    nodes, depths = [], []
    edges = 0
    for i in range(count):
        if pos >= len(payload): raise ValueError('Truncated certificate graph')
        tag = payload[pos]; pos += 1; depth = 0
        if tag <= 2: v = (None,False,True)[tag]
        elif tag in (3,4):
            size = uint()
            if size > len(payload)-pos: raise ValueError('Truncated graph scalar')
            b = payload[pos:pos+size]; pos += size
            if tag == 3:
                if not b or not b.isdigit() or len(b)>1 and b[0]==48 or len(b)>4096:
                    raise ValueError('Invalid graph natural')
                v = int(b)
            else: v = b.decode('utf-8')
        elif tag in (5,6):
            size = uint(); edges += size
            if edges > MAX_EDGES: raise GraphLimit('Certificate graph edge limit exceeded')
            if tag == 6 and size % 2: raise ValueError('Invalid graph object')
            refs = []
            for _ in range(size):
                ref = uint()
                if ref >= i: raise ValueError('Forward or cyclic graph reference')
                refs.append(ref)
            depth = 1 + max((depths[r] for r in refs),default=-1)
            if depth > MAX_DEPTH: raise GraphLimit('Certificate graph depth limit exceeded')
            v = [nodes[r] for r in refs]
            if tag == 6:
                keys = v[::2]
                if any(not isinstance(k,str) for k in keys) or keys != sorted(set(keys)):
                    raise ValueError('Noncanonical graph object keys')
                v = dict(zip(keys,v[1::2]))
        else: raise ValueError('Unknown certificate graph tag')
        nodes.append(v); depths.append(depth)
    root = uint()
    if root != count-1 or pos != len(payload): raise ValueError('Invalid graph root or trailing bytes')
    value = nodes[root]
    # Reject unused/duplicate nodes and alternative numbering. Also ensures the
    # two implementations agree on one canonical wire representation.
    if encode(value,limit=limit) != payload: raise ValueError('Noncanonical certificate graph')
    return value


def require_tree_budget(value, *, limit=MAX_BYTES):
    """Check a tree projection's size without expanding shared graph nodes."""
    memo, active = {}, set()
    def size(v):
        key=id(v)
        if key in memo: return memo[key]
        if key in active: raise ValueError('Cyclic certificate graph')
        active.add(key)
        if isinstance(v,list): result=2+sum(1+size(x) for x in v)
        elif isinstance(v,dict): result=2+sum(2+size(k)+size(x) for k,x in v.items())
        elif isinstance(v,str): result=2+6*len(v.encode('utf-8'))
        elif type(v) is int: result=len(str(v))
        else: result=5
        active.remove(key)
        if result>limit: raise GraphLimit('Expanded source/identity projection exceeds byte limit')
        memo[key]=result
        return result
    size(value)


_U64 = Struct('<Q')
_RECORD_HEADER = Struct('<BQ')
RECORD_DEPTH = 8
RECORD_VALUES = 100_000


def hash_field(digest, value):
    """Hash one raw field with its length, without building a combined payload."""
    digest.update(_U64.pack(len(value)))
    digest.update(value)


def record_parts(value, *, limit, _sizes_only=False):
    """Flat token stream for shallow records; strings are raw UTF-8 fields.

    Tags 0/1/2 denote null/false/true, 3 an unsigned integer, 4 a string,
    5 a list, and 6 a map. Nontrivial tags carry a u64 value/length/count.
    Maps contain sorted string keys. Depth and byte limits apply before saving.
    """
    pending = [(value, 0)]
    total = count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > RECORD_DEPTH or count > RECORD_VALUES:
            raise ValueError('Binary record exceeds structural limits')
        data = None
        if item is None:
            header = 1 if _sizes_only else b'\x00'
        elif type(item) is bool:
            header = 1 if _sizes_only else b'\x02' if item else b'\x01'
        elif type(item) is int and 0 <= item < 2**63:
            header = 9 if _sizes_only else _RECORD_HEADER.pack(3, item)
        elif type(item) is str:
            if len(item) > limit:
                raise ValueError('Binary record exceeds byte limit')
            data = item.encode('utf-8')
            header = 9 if _sizes_only else _RECORD_HEADER.pack(4, len(data))
        elif type(item) in (list, dict):
            children = len(item) * (2 if type(item) is dict else 1)
            if count + len(pending) + children > RECORD_VALUES:
                raise ValueError('Binary record exceeds value limit')
            header = 9 if _sizes_only else _RECORD_HEADER.pack(6 if type(item) is dict else 5, len(item))
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise ValueError('Binary record keys must be strings')
                for key in sorted(item, reverse=True):
                    pending.append((item[key], depth + 1))
                    pending.append((key, depth + 1))
            else:
                pending.extend((child, depth + 1) for child in reversed(item))
        else:
            raise ValueError('Unsupported binary record value')
        size = (header if _sizes_only else len(header)) + (len(data) if data is not None else 0)
        total += size
        if total > limit:
            raise ValueError('Binary record exceeds byte limit')
        if _sizes_only:
            yield size
        else:
            yield header
            if data is not None:
                yield data


def record_size(value, *, limit):
    """Validate shallow values and count raw UTF-8 bytes without binary framing."""
    return sum(record_parts(value, limit=limit, _sizes_only=True))


def load_record(payload, *, limit):
    """Read a bounded, shallow binary record without a JSON or graph fallback."""
    if type(payload) is not bytes or not 0 < len(payload) <= limit:
        raise ValueError('Expected bounded binary record bytes')
    position = count = 0

    def read(depth):
        nonlocal position, count
        count += 1
        if depth > RECORD_DEPTH or count > RECORD_VALUES:
            raise ValueError('Binary record exceeds structural limits')
        if position >= len(payload):
            raise ValueError('Truncated binary record')
        tag = payload[position]
        position += 1
        if tag <= 2:
            return (None, False, True)[tag]
        if tag > 6 or position + _U64.size > len(payload):
            raise ValueError('Invalid or truncated binary record tag')
        size = _U64.unpack_from(payload, position)[0]
        position += _U64.size
        if tag == 3:
            if size >= 2**63:
                raise ValueError('Oversized binary record integer')
            return size
        if tag == 4:
            end = position + size
            if end > len(payload):
                raise ValueError('Truncated binary record string')
            result = payload[position:end].decode('utf-8')
            position = end
            return result
        children = size * (2 if tag == 6 else 1)
        if count + children > RECORD_VALUES or children > len(payload) - position:
            raise ValueError('Binary record count exceeds remaining values')
        if tag == 5:
            return [read(depth + 1) for _ in range(size)]
        result, previous = {}, None
        for _ in range(size):
            key = read(depth + 1)
            if type(key) is not str or previous is not None and key <= previous:
                raise ValueError('Binary record keys must be unique and sorted')
            result[key] = read(depth + 1)
            previous = key
        return result

    result = read(0)
    if position != len(payload):
        raise ValueError('Trailing binary record bytes')
    return result
