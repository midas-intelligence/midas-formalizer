"""Bounded compiler feedback: error headers first, then complete line excerpts."""
from collections import deque
import re

from .protocol import MAX_BYTES, clean_diagnostics


_MESSAGE = re.compile(
    r'(?m)^(?:(?:(?P<file>[^\r\n]+):(?P<line>\d+):\d+: )?'
    r'(?P<severity>error|warning|information|info)(?:\((?P<code>[^\r\n()]+)\))?:'
    r'(?P<message>[^\r\n]*)'
    r'|(?P<internal>unexpected internal error:)[^\r\n]*'
    r'|(?P<axiom>unapproved axiom:)[^\r\n]*|MIDAS_CHECK_[^\r\n]*)')
_DETAIL_NAMES = re.compile(r'''`[^`]*`|'[^']*'|"[^"]*"|\b\d+\b''')


def _excerpt(value):
    """Five leading/trailing lines; do not allocate all expanded context lines."""
    head_end = 0
    for _ in range(5):
        boundary = value.find('\n', head_end)
        if boundary < 0:
            return value
        head_end = boundary + 1
    tail_start = len(value) - int(value.endswith('\n'))
    for _ in range(5):
        boundary = value.rfind('\n', head_end, tail_start)
        if boundary < 0:
            return value
        tail_start = boundary
    tail_start += 1
    omitted = value.count('\n', head_end, tail_start)
    return value[:head_end] + f'... [{omitted} lines omitted] ...\n' + value[tail_start:]


def _clip(value, maximum):
    # Encode only a bounded prefix, even when one diagnostic line is enormous.
    raw = value[:maximum].encode()
    if len(value) <= maximum and len(raw) <= maximum:
        return value
    suffix = '\n... [text truncated]'
    return raw[:maximum-len(suffix)].decode('utf-8', errors='ignore') + suffix


def _blocks(value):
    previous = None
    for match in _MESSAGE.finditer(value):
        if previous is not None:
            yield previous, match.start()
        previous = match
    if previous is not None:
        yield previous, len(value)


def _error_kind(match):
    if match['code'] is not None:
        return match['code']
    if match['internal'] is not None:
        return 'internal error'
    if match['axiom'] is not None:
        return 'unapproved axiom'
    heading = match['message'][:512].partition(':')[0].strip().casefold()
    # Unknown names/numerals are details; different failed tactics are useful
    # distinct kinds even though Lean quotes their names in the heading.
    return heading if heading.startswith('tactic `') else _DETAIL_NAMES.sub('_', heading)


def _detail_order(rows):
    """Cycle kinds, and distinct file/line locations within each kind, in O(n)."""
    locations, kinds = {}, {}
    for index, (_, _, _, location, kind) in enumerate(rows):
        key = kind, location
        if key not in locations:
            locations[key] = deque()
            kinds.setdefault(kind, deque()).append(key)
        locations[key].append(index)
    active = deque(kinds)
    while active:
        kind = active.popleft()
        key = kinds[kind].popleft()
        yield locations[key].popleft()
        if locations[key]:
            kinds[kind].append(key)
        if kinds[kind]:
            active.append(kind)


def compiler_feedback(data, *, max_bytes=8000):
    """Reserve error headers before spending the observation's UTF-8 byte budget.

    Each detailed block keeps its first/last five lines, including the header.
    Excerpts are chosen across kinds and file/line locations; oversized ones
    do not prevent smaller later excerpts from fitting. Display order stays
    unchanged. Warnings are silent; full diagnostics remain in the evidence.
    """
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise ValueError('Compiler diagnostics exceed byte limit')
    if type(max_bytes) is not int or not 256 <= max_bytes <= 8000:
        raise ValueError('Invalid compiler feedback byte limit')
    value = clean_diagnostics(data).decode(errors='replace').strip()
    # Short omission notices, whose counts are bounded by the input size.
    allowance = max_bytes - 192
    rows, used, errors = [], 0, 0
    first, fallback = 0, []
    headers_full = False
    for match, end in _blocks(value):
        kind = match['severity']
        if kind != 'error' and match['internal'] is None and match['axiom'] is None:
            if not errors:
                if first == 0:
                    fallback.append(value[:match.start()])
                if kind != 'warning':
                    fallback.append(value[match.start():end])
                first = end
            continue
        errors += 1
        if headers_full:
            continue
        header = _clip(value[match.start():match.end()], allowance)
        size = len(header.encode()) + (2 if rows else 0)
        if used + size > allowance:
            headers_full = True
            continue
        location = match['file'], (match['line'].lstrip('0') or '0') if match['line'] else None
        rows.append((header, match.start(), end, location, _error_kind(match)))
        used += size
    if not errors:
        # Timeouts, crashes and other non-Lean failures may have no error header.
        return _clip(_excerpt(''.join(fallback).strip() if first else value), max_bytes)

    rendered, compact = [row[0] for row in rows], 0
    for index in _detail_order(rows):
        header, start, end, _, _ = rows[index]
        block = value[start:end].rstrip()
        excerpt = _excerpt(block)
        available = allowance - used + len(header.encode())
        if len(excerpt) <= available and len(excerpt.encode()) <= available:
            rendered[index] = excerpt
            used += len(excerpt.encode()) - len(header.encode())
        else:
            compact += int('\n' in block)
    if compact:
        rendered.append(f'{compact} error(s) shown without details (feedback limit).')
    if errors > len(rows):
        rendered.append(f'{errors-len(rows)} further error(s) omitted (feedback limit).')
    return '\n\n'.join(rendered)
