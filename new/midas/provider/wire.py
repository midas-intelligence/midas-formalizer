"""Bounded protocol parsing without whole-document JSON decoding/serialization.

The prompt owner produces exact wire bytes. This parser validates their protocol
shape and incoming frames. Strings are scanned in C and unescaped in bounded
pieces; large plain text never passes through a JSON library.
"""
import codecs
import math
import re
from ..domain import DomainError

_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
_NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_SPACE = re.compile(rb'[ \t\r\n]*')
_ESCAPES = {34: b'"', 92: b'\\', 47: b'/', 98: b'\b', 102: b'\f', 110: b'\n', 114: b'\r', 116: b'\t'}


def decode(data, *, limit):
    if type(data) is not bytes or not 0 < len(data) <= limit:
        raise DomainError('Protocol frame exceeds byte limit')
    pos = count = 0

    def string():
        nonlocal pos
        pos += 1
        parts = []
        start = pos
        while True:
            match = _SPECIAL.search(data, pos)
            if match is None:
                raise DomainError('Unterminated protocol string')
            end = match.start()
            marker = data[end]
            if end > start:
                # Incremental UTF-8 validation bounds every decoding operation.
                decoder = codecs.getincrementaldecoder('utf-8')()
                for offset in range(start, end, 8192):
                    parts.append(decoder.decode(data[offset:min(end, offset + 8192)]))
                parts.append(decoder.decode(b'', final=True))
            pos = end + 1
            if marker == 34:
                return ''.join(parts)
            if marker != 92 or pos >= len(data):
                raise DomainError('Invalid protocol string')
            escape = data[pos]
            pos += 1
            if escape in _ESCAPES:
                parts.append(_ESCAPES[escape].decode())
            elif escape == 117:
                def unit():
                    nonlocal pos
                    chunk = data[pos:pos+4]
                    if len(chunk) != 4 or any(c not in b'0123456789abcdefABCDEF' for c in chunk):
                        raise DomainError('Invalid Unicode escape')
                    pos += 4
                    return int(chunk, 16)
                code = unit()
                if 0xD800 <= code <= 0xDBFF:
                    if data[pos:pos+2] != b'\\u':
                        raise DomainError('Unpaired Unicode surrogate')
                    pos += 2
                    low = unit()
                    if not 0xDC00 <= low <= 0xDFFF:
                        raise DomainError('Unpaired Unicode surrogate')
                    code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                elif 0xDC00 <= code <= 0xDFFF:
                    raise DomainError('Unpaired Unicode surrogate')
                parts.append(chr(code))
            else:
                raise DomainError('Invalid protocol escape')
            start = pos

    def value(depth):
        nonlocal pos, count
        count += 1
        if depth > 16 or count > 100_000:
            raise DomainError('Protocol structure exceeds limit')
        pos = _SPACE.match(data, pos).end()
        if pos >= len(data):
            raise DomainError('Truncated protocol value')
        tag = data[pos]
        if tag == 34:
            return string()
        if tag in (123, 91):
            mapping = tag == 123
            result = {} if mapping else []
            close = 125 if mapping else 93
            pos = _SPACE.match(data, pos+1).end()
            if pos < len(data) and data[pos] == close:
                pos += 1
                return result
            while True:
                if mapping:
                    pos = _SPACE.match(data, pos).end()
                    if pos >= len(data) or data[pos] != 34:
                        raise DomainError('Expected protocol object key')
                    key = string()
                    if key in result:
                        raise DomainError('Duplicate protocol key')
                    pos = _SPACE.match(data, pos).end()
                    if pos >= len(data) or data[pos] != 58:
                        raise DomainError('Expected protocol colon')
                    pos += 1
                    result[key] = value(depth+1)
                else:
                    result.append(value(depth+1))
                pos = _SPACE.match(data, pos).end()
                if pos >= len(data):
                    raise DomainError('Truncated protocol container')
                marker = data[pos]
                pos += 1
                if marker == close:
                    return result
                if marker != 44:
                    raise DomainError('Expected protocol separator')
        for word, item in ((b'true', True), (b'false', False), (b'null', None)):
            if data.startswith(word, pos):
                pos += len(word)
                return item
        match = _NUMBER.match(data, pos)
        if match is None or match.end() - pos > 128:
            raise DomainError('Invalid protocol number')
        raw = match.group()
        pos = match.end()
        number = float(raw) if any(c in raw for c in b'.eE') else int(raw)
        if type(number) is float and not math.isfinite(number):
            raise DomainError('Nonfinite protocol number')
        return number

    result = value(0)
    if _SPACE.match(data, pos).end() != len(data):
        raise DomainError('Trailing protocol bytes')
    return result


def small_json(value):
    """JSON is permitted only for fixed-size transport control values."""
    import json
    from ..domain.encoding import record_size
    record_size(value, limit=16 * 1024)
    payload = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    if len(payload) > 16 * 1024:
        raise DomainError('Control message exceeds byte limit')
    return payload


def extend_object(raw, fields):
    """Append small invocation controls to previously validated exact params bytes."""
    suffix = small_json(fields)
    end = len(raw.rstrip()) - 1
    return raw[:end] + (b',' if raw[1:end].strip() else b'') + suffix[1:] + raw[end+1:]


def validate_wire(request, raw, setup=None):
    from .protocol import WIRE_BYTES
    w = decode(raw, limit=WIRE_BYTES)
    if not isinstance(w, dict):
        raise DomainError('Expected wire object')
    route = request.route
    if route.transport == 'codex':
        if set(w) != {'input'} or type(w['input']) is not list or not w['input']:
            raise DomainError('Expected frozen turn input')
        for block in w['input']:
            if type(block) is not dict or set(block) != {'type', 'text'} or block['type'] != 'text' or type(block['text']) is not str:
                raise DomainError('Only text turn inputs are supported')
        config = decode(setup, limit=WIRE_BYTES)
        if type(config) is not dict or set(config) != {'baseInstructions'} or type(config['baseInstructions']) is not str:
            raise DomainError('Expected complete frozen base instructions')
    else:
        allowed = {'model', 'input', 'stream', 'store', 'reasoning', 'max_output_tokens', 'text',
                   'prompt_cache_key', 'prompt_cache_options'}
        if (set(w) - allowed or w.get('model') != route.model or w.get('stream') is not True
                or w.get('store') is not False or type(w.get('input')) is not list or not w['input']):
            raise DomainError('Expected stateless streaming Responses request for saved model')
        text_config = w.get('text')
        if type(text_config) is not dict or set(text_config) != {'format'}:
            raise DomainError('Responses requires an explicit JSON output format')
        output_format = text_config['format']
        if type(output_format) is not dict:
            raise DomainError('Invalid Responses output format')
        if output_format.get('type') == 'json_object':
            if set(output_format) != {'type'}:
                raise DomainError('Invalid JSON object output format')
        elif output_format.get('type') == 'json_schema':
            if (set(output_format) - {'type', 'name', 'schema', 'strict', 'description'}
                    or output_format.get('strict') is not True
                    or type(output_format.get('schema')) is not dict
                    or type(output_format.get('name')) is not str
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', output_format['name']) is None
                    or 'description' in output_format and type(output_format['description']) is not str):
                raise DomainError('Expected a named strict JSON schema from prompt')
        else:
            raise DomainError('Responses output must use json_schema or json_object')
        for message in w['input']:
            if (type(message) is not dict or set(message) != {'role', 'content'}
                    or message['role'] not in ('system', 'developer', 'user', 'assistant')):
                raise DomainError('Unsupported Responses input message')
            content = message['content']
            if type(content) is not str:
                if type(content) is not list or not content:
                    raise DomainError('Invalid message content')
                for part in content:
                    if (type(part) is not dict or set(part) != {'type', 'text'}
                            or part['type'] not in ('input_text', 'output_text') or type(part['text']) is not str):
                        raise DomainError('Only text content is supported')
        if 'max_output_tokens' in w and (type(w['max_output_tokens']) is not int or w['max_output_tokens'] <= 0):
            raise DomainError('Invalid output token bound')
        if route.prompt_cache == 'disabled' and any(k in w for k in ('prompt_cache_key', 'prompt_cache_options')):
            raise DomainError('Cache controls supplied for disabled cache')
        if 'reasoning' in w and (type(w['reasoning']) is not dict or set(w['reasoning']) - {'effort', 'summary'}
                or any(type(v) is not str or len(v) > 128 for v in w['reasoning'].values())):
            raise DomainError('Invalid reasoning controls')
        for k in ('prompt_cache_key', 'prompt_cache_options'):
            if k in w:
                small_json({k: w[k]})
