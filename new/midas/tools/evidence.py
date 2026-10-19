"""Advisory archive preparation, indexed paging, and frozen-source excerpts."""
from dataclasses import dataclass, field
from hashlib import sha256
import re

from ..domain import ArtifactRef
from ..domain import encoding as codec
from ..domain.records import identifier, instance, items
from ..source import SourceFile, code_only
from .protocol import RESULT_BYTES, Scope, pack, read, text
from .search import FILE_BYTES, SearchResult
from .bm25 import DETAILS

PAGE_BYTES = 16000
MAX_PAGES = 1024
INDEX_BYTES = 256 * 1024
_ALIAS = re.compile(r'\s*(?:@\[[^\]]*\]\s*)*'
                    r'(?:(?:private|protected|public|noncomputable|unsafe|partial|nonrec)\s+)*alias\b')


@dataclass(frozen=True, slots=True)
class Archive:
    scope: Scope
    helper_id: str
    provenance: tuple[ArtifactRef, ...]
    pages: tuple[ArtifactRef, ...]
    id: str = field(init=False)

    def __post_init__(self):
        instance(self.scope, Scope)
        identifier(self.helper_id, '')
        items(self.provenance, ArtifactRef)
        items(self.pages, ArtifactRef)
        if (not 1 <= len(self.pages) <= MAX_PAGES or not 1 <= len(self.provenance) <= 64
                or any(p.byte_count > PAGE_BYTES for p in self.pages)
                or sum(p.byte_count for p in self.pages) > RESULT_BYTES):
            raise ValueError('Invalid advisory archive bounds')
        object.__setattr__(self, 'id', sha256(self.payload).hexdigest())

    @property
    def payload(self):
        return pack(['midas-advisory-archive-v1', self.scope.value(), self.helper_id,
                     [r.to_dict() for r in self.provenance], [p.to_dict() for p in self.pages]], INDEX_BYTES)

    @classmethod
    def load(cls, payload):
        v = codec.load_record(payload, limit=INDEX_BYTES)
        if (type(v) is not list or len(v) != 5 or v[0] != 'midas-advisory-archive-v1'
                or any(type(v[i]) is not list for i in (3, 4))):
            raise ValueError('Invalid advisory archive')
        return cls(Scope.load(v[1]), v[2], tuple(ArtifactRef.from_dict(r) for r in v[3]),
                   tuple(ArtifactRef.from_dict(r) for r in v[4]))


def _fragments(records):
    """Iterative, shallow leaf projection; text offsets count UTF-8 bytes."""
    count = total = 0
    for index, record in enumerate(records):
        if index >= 10000:
            raise ValueError('Too many evidence records')
        pending = [(record, (str(index),))]
        while pending:
            value, path = pending.pop()
            count += 1
            if len(path) > 9 or count > 100000:
                raise ValueError('Evidence exceeds structural allowance')
            path_size = sum(len(p.encode()) + 9 for p in path)
            if path_size > 2048:
                raise ValueError('Evidence field path exceeds allowance')
            if type(value) is dict and value:
                if len(value) > 10000 or any(type(k) is not str for k in value):
                    raise ValueError('Invalid evidence record keys')
                yield ['object', list(path), len(value)]
                pending.extend((value[k], (*path, k)) for k in reversed(sorted(value)))
            elif type(value) is list and value:
                if len(value) > 10000:
                    raise ValueError('Too many evidence values')
                yield ['array', list(path), len(value)]
                pending.extend((v, (*path, str(i))) for i, v in reversed(list(enumerate(value))))
            elif type(value) is str:
                if len(value) > RESULT_BYTES:
                    raise ValueError('Evidence string exceeds allowance')
                data = value.encode()
                total += len(data)
                if total > RESULT_BYTES:
                    raise ValueError('Evidence exceeds byte allowance')
                offset = 0
                # Leave room for the full scope and page envelope. Boundaries are
                # explicit, so fragments cannot be mistaken for complete source.
                chunk = PAGE_BYTES - path_size - 2048
                while offset < len(data):
                    end = min(len(data), offset+chunk)
                    while end < len(data) and data[end] & 0xC0 == 0x80:
                        end -= 1
                    yield ['text', list(path), offset, len(data), data[offset:end].decode()]
                    offset = end
                if not data:
                    yield ['text', list(path), 0, 0, '']
            else:
                # Only scalar/empty containers reach here; no nested copies.
                codec.record_size(value, limit=PAGE_BYTES)
                yield ['value', list(path), value]


def prepare_archive(scope, helper_id, records, provenance, staging):
    """Stage immutable pages. Controller publishes refs and commits their index."""
    instance(scope, Scope)
    identifier(helper_id, '')
    pages, rows, size, total = [], [], 0, 0
    def flush():
        nonlocal rows, size, total
        payload = pack(['midas-advisory-page-v1', scope.value(), helper_id, len(pages), rows], PAGE_BYTES)
        total += len(payload)
        if total > RESULT_BYTES or len(pages) >= MAX_PAGES:
            raise ValueError('Evidence archive exceeds allowance')
        pages.append(staging.write_bounded(payload, max_bytes=PAGE_BYTES))
        rows, size = [], 0
    for fragment in _fragments(records):
        length = codec.record_size(fragment, limit=PAGE_BYTES)
        if rows and size+length > PAGE_BYTES-1024:
            flush()
        rows.append(fragment)
        size += length
    if rows or not pages:
        flush()
    return Archive(scope, helper_id, provenance, tuple(pages))


@dataclass(frozen=True, slots=True)
class Page:
    archive_id: str
    ordinal: int
    payload: bytes
    next_cursor: str | None

    @property
    def delivery_key(self):
        return f'{self.archive_id}:{self.ordinal}'


def recall(archive, scope, reader, *, cursor=None, delivered=lambda key: False):
    """Read one indexed page. Storage supplies one indexed duplicate-delivery test.

    The controller rechecks this delivery key and scope in its commit transaction.
    No complete historical result or previous pages are read or deserialized.
    """
    if archive.scope != scope:
        raise ValueError('Evidence recall scope differs')
    ordinal = 0
    if cursor is not None:
        text(cursor, 'evidence cursor', 80)
        parts = cursor.split(':')
        if (len(parts) != 2 or parts[0] != archive.id or not parts[1].isascii()
                or not parts[1].isdigit() or str(int(parts[1])) != parts[1]):
            raise ValueError('Invalid evidence cursor')
        ordinal = int(parts[1])
    if not 0 <= ordinal < len(archive.pages):
        raise ValueError('Evidence cursor outside archive')
    if delivered(f'{archive.id}:{ordinal}'):
        return None
    payload = read(reader, archive.pages[ordinal], PAGE_BYTES)
    v = codec.load_record(payload, limit=PAGE_BYTES)
    if (type(v) is not list or len(v) != 5 or v[:4] != [
            'midas-advisory-page-v1', scope.value(), archive.helper_id, ordinal] or type(v[4]) is not list):
        raise ValueError('Evidence page binding differs')
    for row in v[4]:
        if (type(row) is not list or not row or row[0] not in ('text', 'value', 'object', 'array')
                or len(row) != (5 if row[0] == 'text' else 3) or type(row[1]) is not list
                or not 1 <= len(row[1]) <= 9 or any(type(p) is not str for p in row[1])):
            raise ValueError('Invalid evidence fragment')
        if row[0] == 'text' and (type(row[2]) is not int or type(row[3]) is not int or type(row[4]) is not str
                or not 0 <= row[2] <= row[3] <= RESULT_BYTES or row[2]+len(row[4].encode()) > row[3]):
            raise ValueError('Invalid evidence text fragment')
        if row[0] == 'value' and type(row[2]) in (dict, list) and row[2]:
            raise ValueError('Nested evidence fragment')
        if row[0] in ('object', 'array') and (type(row[2]) is not int or not 1 <= row[2] <= 10000):
            raise ValueError('Invalid evidence container descriptor')
    following = f'{archive.id}:{ordinal+1}' if ordinal+1 < len(archive.pages) else None
    return Page(archive.id, ordinal, payload, following)


def source_excerpt(data, location):
    """Lean positions use Unicode columns; source owns the shared coordinate map."""
    codec.fields(location, ('module', 'start', 'end'))
    for pos in (location['start'], location['end']):
        if type(pos) is not list or len(pos) != 2 or any(type(n) is not int for n in pos) or pos[0] < 1 or pos[1] < 0:
            raise ValueError('Invalid source range')
    if location['start'] >= location['end']:
        raise ValueError('Invalid source range ordering')
    source = SourceFile('lookup', data)
    start, end = (source.offset(*location[k]) for k in ('start', 'end'))
    if start is None or end is None:
        raise ValueError('Source range outside frozen file')
    return data[start:end].decode() if end-start <= 256*1024 else None


def search_summary(result, search):
    """Twelve detailed hits and a compact name tail, without a score cutoff.

    If full excerpts would push names off the first page, lead with the ranked
    name list and page the details after it. Anonymous hits always need source.
    """
    instance(result, SearchResult)
    rows = [f'Search ({search.backend.name}): {search.query}']
    if result.error:
        rows.append('Search failed: '+result.error)
    elif not result.records:
        rows.append('No matches.')
    details, names = [], []
    compact = len(result.records) > DETAILS
    if compact:
        detail = f'; first {DETAILS} detailed' if search.backend.name == 'frozen-source' else ''
        rows.append(f'{len(result.records)} results{detail}. '
                    'Use lookup with a name for its type or inspect=true for source.')
    for rank, match in enumerate(result.records, 1):
        name = '\n'.join(match.names) or 'Source match (no declaration name)'
        label = f'{rank}. ' if compact else ''
        names.append(label+name if match.names else f'{label}{name}: {match.path}:{match.line}')
        detailed = rank <= DETAILS or not match.names
        if match.path is not None and detailed:
            name += f'\nSource: {match.path}:{match.line}'
            if match.source:
                name += f'\nExcerpt starts at line {match.start_line}:\n'+match.source
            details.append(label+name)
        rows.append(label+name)
    if compact and details and sum(codec.record_size(f, limit=PAGE_BYTES)
                                   for f in _fragments(rows)) > PAGE_BYTES-1024:
        return (*rows[:2], 'Ranked names (details follow; use page for further excerpts):\n'+'\n'.join(names),
                *details)
    return tuple(rows)


def probe_summary(case, outcome):
    """Render an admitted case with exact generated code and diagnostic locations."""
    result = outcome['result']
    rows = [f"Probe {case['id']} ({case['operation']}): {result['status']} (advisory)"]
    for field in ('setup', 'code', 'expected'):
        if case.get(field):
            rows.append(f"{case['id']}.{field} (line 1 below):\n"+case[field])
    if 'diagnostic' in result:
        rows.append('Diagnostic:\n'+result['diagnostic'])
    for message in outcome['messages']:
        rows.append(f"{message['severity']} at {message['file']}:{message['line']}:{message['column']}:\n"
                    +message['text'])
    if 'goals' in result:
        rows.extend(f'Goal {i}:\n'+goal for i, goal in enumerate(result['goals'], 1))
        if not result['goals']:
            rows.append('No remaining goals (exploratory only).')
    for field, label in (('contains_sorry', 'Contains sorry'),
                         ('unresolved_metavariables', 'Unresolved metavariables')):
        if field in result:
            rows.append(f'{label}: '+str(result[field]).lower())
    inspection = result.get('inspection')
    if inspection:
        rows.extend(('Inspected expression:\n'+inspection['expression'],
                     'Inferred type:\n'+inspection['inferred_type'],
                     'Expression contains sorry: '+str(inspection['contains_sorry']).lower(),
                     'Expression unresolved metavariables: '+str(inspection['unresolved_metavariables']).lower()))
    return '\n\n'.join(rows)


def lookup_summary(records):
    """Render admitted declaration signatures and lookup failures as plain text."""
    if type(records) is not tuple or len(records) > 16:
        raise ValueError('Expected bounded admitted lookup records')
    result = []
    for row in records:
        if row['status'] != 'available':
            result.append(row['query']+': '+(row.get('error') or 'Declaration unavailable.'))
            continue
        kind = row.get('kind')
        declaration = (kind+' ' if kind in ('def', 'theorem', 'inductive') else '')+row['query']
        if row.get('type'):
            declaration += ' : '+row['type']
        result.append(declaration)
    return tuple(result)


def lookup_sources(records, *, environment_id, index, prefix, reader):
    """Return authored declarations, retaining checked types for alias commands.

    prefix is the exact checked SourceFile prefix. Installed modules are resolved
    only against the admitted index, and bytes are verified on every read.
    """
    if index.environment_id != environment_id or not isinstance(prefix, SourceFile):
        raise ValueError('Lookup source scope differs')
    result, loaded = [], {}
    for row, summary in zip(records, lookup_summary(records)):
        location = row.get('source_range')
        if row['status'] != 'available':
            result.append(summary)
            continue
        data, path, note = prefix.data, prefix.path, None
        if location is None:
            note = 'Original declaration source unavailable: no declaration range.'
        elif location['module'] is not None:
            module = location['module']
            if type(module) is not str or not module or any(c in module for c in ('/', '\\', '\x00')):
                raise ValueError('Invalid lookup source module')
            candidates = index.by_module.get(module, ())
            if len(candidates) != 1:
                note = 'Original declaration source unavailable: missing or ambiguous frozen module.'
            elif candidates[0].artifact.byte_count > FILE_BYTES:
                note = 'Original declaration source unavailable: file exceeds byte allowance.'
            else:
                entry = candidates[0]
                path = entry.path
                if path not in loaded:
                    loaded[path] = read(reader, entry.artifact, FILE_BYTES)
                data = loaded[path]
        if note is None:
            source = None
            try:
                source = source_excerpt(data, location)
            except (ValueError, UnicodeError):
                note = 'Original declaration source unavailable: range differs from frozen source.'
            if source is None:
                note = note or 'Original declaration source unavailable: declaration exceeds byte allowance.'
            else:
                if 'alias' in source and _ALIAS.match(code_only(source)):
                    source = summary + '\n\n' + source
                result.append(source)
                continue
        result.append(summary+'\n-- '+note)
    return tuple(result)
