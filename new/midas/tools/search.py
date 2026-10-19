"""Bounded frozen-source discovery and one hosted search; no task scheduling."""
from dataclasses import dataclass, field
from math import isfinite
import time
from types import MappingProxyType
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

from ..domain import (ArtifactRef, DeliveryCertainty, ResultStatus, Termination, Usage, WorkerResult)
from ..domain import encoding as codec
from ..domain.records import identifier, items
from ..domain.wire import decode
from .protocol import (ENDPOINT, REQUEST_BYTES, SEARCH_BYTES, Search, Stage, pack, read,
                       source_selector, strings, text)
from .bm25 import DETAILS, FILE_BYTES, TOTAL, corpus_key, database, ranked, stopped

HOSTED_BYTES = 5_000_000


@dataclass(frozen=True, slots=True)
class SourceEntry:
    path: str
    artifact: ArtifactRef
    module: str | None = None

    def __post_init__(self):
        # Project and installed sources support both lookup and search.
        # Private checker/runtime sources are not admitted here.
        if (source_selector(self.path) != self.path or '/' not in self.path
                or not self.path.endswith('.lean') or not isinstance(self.artifact, ArtifactRef)):
            raise ValueError('Invalid frozen-source entry')
        if self.module is not None:
            text(self.module, 'source module', 1024)
            if (any(c in self.module for c in ('/', '\\', '\x00'))
                    or not self.path.endswith('/'+self.module.replace('.', '/')+'.lean')):
                raise ValueError('Source module differs from its path')


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """Application supplies admitted frozen source refs once per environment hash."""
    environment_id: str
    entries: tuple[SourceEntry, ...]
    paths: frozenset[str] = field(init=False, repr=False)
    roots: tuple[str, ...] = field(init=False, repr=False)
    by_path: object = field(init=False, repr=False, compare=False)
    by_module: object = field(init=False, repr=False, compare=False)
    search_key: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        identifier(self.environment_id, 'env_')
        items(self.entries, SourceEntry)
        if len(self.entries) > 200_000 or len({e.path for e in self.entries}) != len(self.entries):
            raise ValueError('Duplicate or excessive source entries')
        paths, roots, modules = set(), set(), {}
        for entry in self.entries:
            parts = entry.path.split('/')
            roots.add(parts[0])
            paths.update('/'.join(parts[:i]) for i in range(1, len(parts)+1))
            if entry.module is not None:
                modules.setdefault(entry.module, []).append(entry)
        object.__setattr__(self, 'paths', frozenset(paths))
        object.__setattr__(self, 'roots', tuple(sorted(roots)))
        object.__setattr__(self, 'by_path', MappingProxyType({e.path: e for e in self.entries}))
        object.__setattr__(self, 'by_module', MappingProxyType({name: tuple(rows) for name, rows in modules.items()}))
        object.__setattr__(self, 'search_key', corpus_key(self))

    def resolve(self, prefixes):
        resolved = []
        for raw in prefixes:
            selector = source_selector(raw)
            forms = [selector]
            if not selector.endswith('.lean'):
                forms.append(selector + '.lean')
            stem = selector.removesuffix('.lean')
            if '.' in stem:
                module = source_selector(stem.replace('.', '/'))
                forms.extend([module + '.lean'] if selector.endswith('.lean') else [module, module + '.lean'])
            matches = []
            for form in dict.fromkeys(forms):
                candidates = [form] if form.split('/')[0] in self.roots else [r+'/'+form for r in self.roots]
                matches = [p for p in candidates if p in self.paths]
                if len(matches) > 1:
                    raise ValueError('Ambiguous source selector: ' + raw + '; qualify its source root')
                if matches:
                    break
            if not matches:
                # Module names can live below a package's srcDir rather than
                # immediately below the frozen root.
                entries = self.by_module.get(stem.replace('/', '.'), ())
                if len(entries) > 1:
                    raise ValueError('Ambiguous source selector: ' + raw + '; qualify its source root')
                if not entries:
                    raise ValueError('Source selector absent from frozen inputs: ' + raw)
                matches = [entries[0].path]
            if matches[0] not in resolved:
                resolved.append(matches[0])
        return tuple(resolved)


@dataclass(frozen=True, slots=True)
class Match:
    names: tuple[str, ...]
    path: str | None = None
    line: int = 0
    start_line: int = 0
    source: str = ''
    score: float = 0

    def __post_init__(self):
        # Hosted names remain hints. Invalid Lean spelling is excluded before lookup.
        strings(self.names, 20, 1024, 'discovered names')
        if (any(type(n) is not int for n in (self.line, self.start_line))
                or type(self.score) not in (int, float) or not isfinite(self.score) or self.score < 0):
            raise ValueError('Invalid search coordinates/score')
        if self.path is None:
            if len(self.names) != 1 or self.line != 0 or self.start_line != 0 or self.source or self.score != 0:
                raise ValueError('Invalid hosted hint')
        elif (source_selector(self.path) != self.path or not self.path.endswith('.lean')
                or type(self.line) is not int or type(self.start_line) is not int
                or not 1 <= self.start_line <= self.line):
            raise ValueError('Invalid source match')
        text(self.source, 'search excerpt', 6000, empty=True)

    def value(self):
        # Binary records have no float tag. Hex preserves every score bit without
        # quantization or normalization and does not involve JSON serialization.
        return [list(self.names), self.path, self.line, self.start_line, self.source, float(self.score).hex()]


@dataclass(frozen=True, slots=True)
class SearchResult:
    request_hash: str
    records: tuple[Match, ...] = ()
    error: str | None = None

    def __post_init__(self):
        identifier(self.request_hash, '')
        items(self.records, Match)
        if len(self.records) > TOTAL or self.error is not None and self.records:
            raise ValueError('Invalid search result')
        if self.error is not None:
            text(self.error, 'search error', 2048)

    def validate_for(self, stage):
        if not isinstance(stage.action, Search) or self.request_hash != stage.sha256:
            raise ValueError('Search result differs from stage')
        if len(self.records) > stage.action.limit:
            raise ValueError('Search result exceeds requested count')
        for row in self.records:
            if (row.path is None) != (stage.action.backend.name == 'leanexplore'):
                raise ValueError('Search result differs from backend')
        return self

    @property
    def payload(self):
        return pack(['midas-search-result-v2', self.request_hash, [r.value() for r in self.records], self.error], SEARCH_BYTES)

    @classmethod
    def load(cls, payload):
        v = codec.load_record(payload, limit=SEARCH_BYTES)
        if type(v) is not list or len(v) != 4 or v[0] != 'midas-search-result-v2' or type(v[2]) is not list:
            raise ValueError('Invalid search result encoding')
        rows = []
        for row in v[2]:
            if (type(row) is not list or len(row) != 6 or type(row[0]) is not list
                    or type(row[5]) is not str or len(row[5]) > 32):
                raise ValueError('Invalid search row encoding')
            rows.append(Match(tuple(row[0]), *row[1:5], float.fromhex(row[5])))
        return cls(v[1], tuple(rows), v[3])


def source_search(stage, index, reader, cancel, deadline, *, cache_directory=None, prepared_path=None):
    if index is None or index.environment_id != stage.scope.environment_id:
        raise ValueError('Source index differs from search environment')
    action = stage.action
    prefixes = index.resolve(action.backend.prefixes)
    with database(index, reader, cancel, deadline, cache_directory, prepared_path=prepared_path) as db:
        hits = ranked(db, action.query, prefixes, limit=action.limit)
    result, loaded = [], {}
    for rank, (name, path, line, start_line, start, end, score) in enumerate(hits):
        stopped(cancel, deadline)
        source = ''
        if rank < DETAILS or not name:
            if path not in loaded:
                loaded[path] = read(reader, index.by_path[path].artifact, FILE_BYTES)
            source = loaded[path][start:min(end, start+6000)].decode('utf-8', errors='ignore').rstrip()
        result.append(Match((name,) if name else (), path, line, start_line, source, score))
    return tuple(result)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Hosted search redirects are unsupported')


def hosted(action, cancel, deadline, *, opener=None):
    if not isinstance(action, Search) or action.backend.name != 'leanexplore':
        raise ValueError('Expected hosted search')
    stopped(cancel, deadline)
    url = ENDPOINT + '?' + urlencode({'q': action.query, 'limit': action.limit, 'packages': 'Mathlib'})
    opener = opener or build_opener(_NoRedirect()).open
    with opener(Request(url, headers={'Accept': 'application/json', 'User-Agent': 'midas'}),
                timeout=max(.001, min(15, deadline-time.monotonic()))) as response:
        data = bytearray()
        while True:
            stopped(cancel, deadline)
            chunk = response.read1(min(65536, HOSTED_BYTES+1-len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > HOSTED_BYTES:
                raise ValueError('Hosted search response exceeds byte allowance')
    stopped(cancel, deadline)
    value = decode(bytes(data), limit=HOSTED_BYTES)
    if type(value) is not dict or type(value.get('results')) is not list:
        raise ValueError('Invalid hosted search envelope')
    result = []
    for row in value['results'][:action.limit]:
        if type(row) is not dict:
            raise ValueError('Invalid hosted declaration')
        result.append(Match((row.get('name'),)))
    stopped(cancel, deadline)
    return tuple(result)


def execute_search(stage, reader, cancel, deadline, *, index=None, transport=hosted,
                   cache_directory=None, prepared_path=None):
    if not isinstance(stage.action, Search):
        raise ValueError('Expected search stage')
    stopped(cancel, deadline)
    rows = (source_search(stage, index, reader, cancel, deadline, cache_directory=cache_directory,
                          prepared_path=prepared_path) if stage.action.backend.name == 'frozen-source'
            else transport(stage.action, cancel, deadline))
    stopped(cancel, deadline)
    return SearchResult(stage.sha256, rows).validate_for(stage)


def worker_payload(result):
    """Exact bounded worker metadata for controller-issued receipts."""
    return pack({'invocation': result.invocation_id, 'request': result.request_hash,
        'status': result.status.value, 'artifacts': [r.to_dict() for r in result.staged_artifacts],
        'output': result.output.to_dict() if result.output else None,
        'diagnostics': result.diagnostics.to_dict() if result.diagnostics else None,
        'usage': [getattr(result.usage, k) for k in ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')],
        'delivery': result.delivery_certainty.value, 'termination': result.termination.value}, 16*1024)


class SearchService:
    def __init__(self, *, index=None, transport=hosted, source_reader=None,
                 cache_directory=None, prepared_path=None):
        self.index, self.transport, self.source_reader = index, transport, source_reader
        self.cache_directory = cache_directory
        self.prepared_path = prepared_path

    def execute(self, frozen, context):
        invocation = context.capacity_grant.invocation
        context.capacity_grant.validate_for(invocation)
        if frozen != invocation.request:
            raise ValueError('Search differs from capacity grant')
        stage = Stage.load(read(context.reader, frozen.operation_payload, REQUEST_BYTES))
        stage.validate_for(frozen, context.reader)
        status = ResultStatus.SUCCEEDED
        try:
            result = execute_search(stage, self.source_reader or context.reader, context.cancellation, context.deadline,
                                    index=self.index, transport=self.transport, cache_directory=self.cache_directory,
                                    prepared_path=self.prepared_path)
        except Exception as exc:
            status = ResultStatus.CANCELLED if context.cancellation.is_set() else ResultStatus.FAILED
            message = (type(exc).__name__ + ': ' + str(exc)).encode()[:2048].decode('utf-8', errors='ignore')
            result = SearchResult(stage.sha256, error=message)
        limit = min(SEARCH_BYTES, frozen.job.limits.output_bytes, frozen.job.limits.evidence_bytes)
        output = context.staging.write_bounded(result.payload, max_bytes=limit)
        return WorkerResult(invocation.original_execution_id, frozen.request_hash, status, (output,), output, None,
                            Usage(0, 0, 0, 0), DeliveryCertainty.DELIVERED, Termination.TERMINATED)

    def admit_receipt(self, stage, invocation, result, receipt, reader, authority):
        result.validate_for(invocation)
        stage.validate_for(invocation.request, reader)
        if (receipt.original_execution_id != invocation.original_execution_id
                or receipt.request_ref != invocation.request.request_ref
                or read(reader, receipt.result_ref, 16*1024) != worker_payload(result)
                or authority.verify_receipt(receipt, invocation, result) is not True):
            raise ValueError('Unauthenticated search receipt')
        if (result.output is None or result.staged_artifacts != (result.output,) or result.diagnostics is not None
                or result.termination != Termination.TERMINATED
                or result.delivery_certainty != DeliveryCertainty.DELIVERED or result.usage != Usage(0, 0, 0, 0)):
            raise ValueError('Invalid search worker outcome')
        limits = invocation.request.job.limits
        output = SearchResult.load(read(reader, result.output, min(SEARCH_BYTES, limits.output_bytes, limits.evidence_bytes)))
        output.validate_for(stage)
        if result.status == ResultStatus.SUCCEEDED and output.error is not None:
            raise ValueError('Failed search reported success')
        if result.status != ResultStatus.SUCCEEDED and output.error is None:
            # Stop/deadline may win after the worker finished. Do not admit its hints.
            return SearchResult(stage.sha256, error='Search interrupted after completion')
        return output
