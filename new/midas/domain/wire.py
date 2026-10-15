"""Bounded external wire decoding shared by providers and advisory tools.

No whole-document JSON library calls; strings decode in bounded UTF-8 pieces.
"""
import codecs
import math
import re
from .records import DomainError

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

