"""Raw UTF-8 source, bounded assembly, and exact authored coordinate mapping."""

from bisect import bisect_right
from dataclasses import dataclass
from functools import cached_property

from ..domain import ByteSpan

MAX_BYTES = 64 * 1024**2


def byte_limit(value):
    if type(value) is not int or not 0 <= value <= MAX_BYTES:
        raise ValueError('Invalid source byte limit')
    return value


def utf8(value):
    if type(value) is not bytes or len(value) > MAX_BYTES:
        raise ValueError('Expected bounded immutable source bytes')
    value.decode('utf-8')
    return value


def check_span(data, span):
    if (not isinstance(span, ByteSpan) or type(span.start) is not int
            or type(span.end) is not int or not 0 <= span.start <= span.end <= len(data)):
        raise ValueError('Invalid source byte span')
    for offset in (span.start, span.end):
        if offset < len(data) and data[offset] & 0xC0 == 0x80:
            raise ValueError('Source span splits a UTF-8 character')


@dataclass(frozen=True)
class SourceFile:
    """A source path or fragment label and its exact bytes; no filesystem access."""
    path: str
    data: bytes

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path or len(self.path.encode()) > 1024:
            raise ValueError('Invalid source label')
        utf8(self.data)

    @cached_property
    def line_starts(self):
        import re
        return (0, *(match.end() for match in re.finditer(b'\n', self.data)))

    def offset(self, line, column):
        """One-based line, zero-based Unicode code-point column (not byte/UTF-16)."""
        if (type(line) is not int or type(column) is not int
                or not 1 <= line <= len(self.line_starts) or column < 0):
            return None
        start = self.line_starts[line - 1]
        end = self.line_starts[line] - 1 if line < len(self.line_starts) else len(self.data)
        text = self.data[start:end].decode()
        if column > len(text):
            return None
        return start + len(text[:column].encode())

    def location(self, offset):
        check_span(self.data, ByteSpan(offset, offset))
        index = bisect_right(self.line_starts, offset) - 1
        return Location(self.path, index + 1,
                        len(self.data[self.line_starts[index]:offset].decode()), offset)


@dataclass(frozen=True, slots=True)
class Location:
    path: str
    line: int
    column: int
    byte_offset: int


@dataclass(frozen=True, slots=True)
class SourceMapEntry:
    generated: ByteSpan
    original: SourceFile
    authored: ByteSpan


@dataclass(frozen=True)
class GeneratedSource:
    data: bytes
    mappings: tuple[SourceMapEntry, ...] = ()

    def __post_init__(self):
        utf8(self.data)
        if type(self.mappings) is not tuple:
            raise ValueError('Expected immutable source mappings')
        end = 0
        view = memoryview(self.data)
        for row in self.mappings:
            if not isinstance(row, SourceMapEntry) or not isinstance(row.original, SourceFile):
                raise ValueError('Invalid source mapping')
            check_span(self.data, row.generated)
            check_span(row.original.data, row.authored)
            if row.generated.start < end or row.generated.start == row.generated.end:
                raise ValueError('Overlapping or empty source mapping')
            if view[row.generated.start:row.generated.end] != memoryview(row.original.data)[row.authored.start:row.authored.end]:
                raise ValueError('Source map does not preserve authored bytes')
            end = row.generated.end

    @cached_property
    def _starts(self):
        return tuple(row.generated.start for row in self.mappings)

    def authored_location(self, offset):
        check_span(self.data, ByteSpan(offset, offset))
        index = bisect_right(self._starts, offset) - 1
        if index < 0:
            return None
        row = self.mappings[index]
        if offset >= row.generated.end:
            return None
        return row.original.location(row.authored.start + offset - row.generated.start)


class Builder:
    """Private bounded byte accumulator; generated scaffolding has no source map."""

    def __init__(self, maximum=MAX_BYTES):
        self.maximum = byte_limit(maximum)
        self.size = 0
        self.parts = []
        self.mappings = []

    def append(self, data):
        if not isinstance(data, (bytes, memoryview)):
            raise ValueError('Expected source bytes')
        if self.size + len(data) > self.maximum:
            raise ValueError('Generated source exceeds byte limit')
        self.parts.append(data)
        self.size += len(data)

    def authored(self, source, span=None):
        span = ByteSpan(0, len(source.data)) if span is None else span
        check_span(source.data, span)
        start = self.size
        self.append(memoryview(source.data)[span.start:span.end])
        if start != self.size:
            self.mappings.append(SourceMapEntry(ByteSpan(start, self.size), source, span))

    def generated(self, source):
        start = self.size
        self.append(source.data)
        self.mappings.extend(SourceMapEntry(ByteSpan(start + m.generated.start, start + m.generated.end),
                                           m.original, m.authored) for m in source.mappings)

    def slice(self, source, span):
        check_span(source.data, span)
        start = self.size
        self.append(memoryview(source.data)[span.start:span.end])
        for row in source.mappings:
            left, right = max(row.generated.start, span.start), min(row.generated.end, span.end)
            if left < right:
                origin = row.authored.start + left - row.generated.start
                self.mappings.append(SourceMapEntry(
                    ByteSpan(start + left - span.start, start + right - span.start),
                    row.original, ByteSpan(origin, origin + right - left)))

    def indented(self, source, indent):
        if type(indent) is not int or not 0 <= indent <= 64:
            raise ValueError('Invalid source indentation')
        for index, start in enumerate(source.line_starts):
            if start == len(source.data):
                break
            end = source.line_starts[index + 1] if index + 1 < len(source.line_starts) else len(source.data)
            self.append(b' ' * indent)
            self.authored(source, ByteSpan(start, end))
            if source.data[end - 1] != 10:
                self.append(b'\n')

    def finish(self):
        return GeneratedSource(b''.join(self.parts), tuple(self.mappings))


@dataclass(frozen=True, slots=True)
class Edit:
    span: ByteSpan
    replacement: GeneratedSource


def apply_edits(source: SourceFile, edits: tuple[Edit, ...], *, maximum=MAX_BYTES):
    """Apply non-overlapping byte edits in one pass, preserving untouched bytes."""
    if type(edits) is not tuple or any(not isinstance(edit, Edit) for edit in edits):
        raise ValueError('Expected immutable source edits')
    writer, cursor = Builder(maximum), 0
    for edit in sorted(edits, key=lambda edit: (edit.span.start, edit.span.end)):
        check_span(source.data, edit.span)
        if edit.span.start < cursor:
            raise ValueError('Overlapping source edits')
        writer.authored(source, ByteSpan(cursor, edit.span.start))
        writer.generated(edit.replacement)
        cursor = edit.span.end
    writer.authored(source, ByteSpan(cursor, len(source.data)))
    return writer.finish()
