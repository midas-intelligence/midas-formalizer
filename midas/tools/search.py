"""Bounded frozen-source and hosted discovery; no task scheduling."""
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
from .protocol import (ENDPOINT, LEANSEARCH_ENDPOINT, REQUEST_BYTES, SEARCH_BYTES, Search, Stage, pack, read,
                       source_selector, strings, text)
from .bm25 import DETAILS, FILE_BYTES, TOTAL, corpus_key, database, documents, ranked, stopped
from .bm25_score import token_text

HOSTED_BYTES = 5_000_000
_HOSTED_FIELD_CHARS = 16_384
_HOSTED_STOP = frozenset(('a', 'an', 'as', 'at', 'be', 'by', 'if', 'in', 'is', 'it',
                          'of', 'on', 'or', 'so', 'to', 'find', 'lemma', 'theorem', 'prove', 'show'))


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
        """Normalize path hints and known module aliases, without requiring existence."""
        resolved = []
        for raw in prefixes:
            stem = source_selector(raw).removesuffix('.lean')
            hint = stem if stem.split('/')[0] in self.roots else source_selector(stem.replace('.', '/'))
            if hint.split('/')[0] in self.roots:
                matches = [hint]
            else:
                # Known modules may live below a package's srcDir. Otherwise
                # qualify using the top namespace, independent of descendants.
                entries = self.by_module.get(hint.replace('/', '.'), ())
                matches = [entry.path for entry in entries]
                aliased_roots = {entry.path.split('/')[0] for entry in entries}
                top = hint.split('/')[0]
                roots = [r for r in self.roots if r+'/'+top in self.paths or r+'/'+top+'.lean' in self.by_path]
                if not roots and not matches:
                    roots = self.roots
                matches.extend(r+'/'+hint for r in roots if r not in aliased_roots)
            for match in matches:
                if match+'.lean' in self.by_path:
                    match += '.lean'
                if match not in resolved:
                    resolved.append(match)
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
            if (row.path is None) != (stage.action.backend.name != 'frozen-source'):
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


def source_search(stage, index, reader, cancel, deadline, *, cache_directory=None, prepared_path=None,
                  context=None):
    if index is None or index.environment_id != stage.scope.environment_id:
        raise ValueError('Source index differs from search environment')
    action = stage.action
    prefixes = index.resolve(action.backend.prefixes)
    if context is None:
        with database(index, reader, cancel, deadline, cache_directory, prepared_path=prepared_path) as db:
            hits = ranked(db, action.query, prefixes, limit=action.limit,
                          check=lambda: stopped(cancel, deadline))
    else:
        from .search_process import search_hits
        hits = search_hits(index, reader, action, prefixes, context,
                          cache_directory=cache_directory, prepared_path=prepared_path)
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


def _hosted_terms(value):
    return set(token_text(value).split()) - _HOSTED_STOP


def _hosted_relevant(query_terms, row):
    """Reject multi-term hints supported only by an isolated keyword collision.

    LeanExplore exposes no relevance scores. Its informalization can supply
    query wording absent from the Lean name (e.g. forward difference/fwdDiff).
    Sparse or oversized metadata cannot establish a mismatch; retain those hints.
    """
    if len(query_terms) < 2:
        return True
    found = query_terms & _hosted_terms(row['name'])
    described = incomplete = False
    for key in ('informalization', 'docstring', 'source_text'):
        if len(found) >= 2:
            return True
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        described = True
        incomplete |= len(value) > _HOSTED_FIELD_CHARS
        value = value[:_HOSTED_FIELD_CHARS]
        if key == 'source_text':
            # Proof references and neighbouring commands are not evidence that
            # this declaration addresses the query. Reuse the source index's
            # lexical header extraction without loading any frozen sources.
            declaration = next(documents(value), None)
            value = declaration[-1] if declaration is not None else ''
        found.update(query_terms & _hosted_terms(value))
    return len(found) >= 2 or not described or incomplete


def hosted(action, cancel, deadline, *, opener=None):
    if not isinstance(action, Search) or action.backend.name == 'frozen-source':
        raise ValueError('Expected hosted search')
    stopped(cancel, deadline)
    headers = {'Accept': 'application/json', 'User-Agent': 'midas'}
    if action.backend.name == 'leanexplore':
        url = ENDPOINT + '?' + urlencode({'q': action.query, 'limit': action.limit, 'packages': 'Mathlib'})
        request = Request(url, headers=headers)
    else:
        # Only the already bounded query and result count are serialized.
        # Response bodies use the incremental external wire decoder below.
        body = codec.canonical({'query': [action.query], 'num_results': action.limit})
        request = Request(LEANSEARCH_ENDPOINT, data=body,
                          headers={**headers, 'Content-Type': 'application/json'})
    opener = opener or build_opener(_NoRedirect()).open
    with opener(request,
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
    if action.backend.name == 'leansearch':
        if type(value) is not list or len(value) != 1 or type(value[0]) is not list:
            raise ValueError('Invalid LeanSearch envelope')
        result = []
        for row in value[0][:action.limit]:
            stopped(cancel, deadline)
            if type(row) is not dict or type(row.get('result')) is not dict:
                raise ValueError('Invalid LeanSearch declaration')
            parts = row['result'].get('name')
            if (type(parts) is not list or not parts
                    or any(type(p) is not str or not p or len(p) > 1024 for p in parts)
                    or sum(map(len, parts)) + len(parts) - 1 > 1024):
                raise ValueError('Invalid LeanSearch declaration name')
            result.append(Match(('.'.join(parts),)))
        stopped(cancel, deadline)
        return tuple(result)
    if type(value) is not dict or type(value.get('results')) is not list:
        raise ValueError('Invalid hosted search envelope')
    result, query_terms = [], _hosted_terms(action.query)
    for row in value['results'][:action.limit]:
        stopped(cancel, deadline)
        if type(row) is not dict:
            raise ValueError('Invalid hosted declaration')
        match = Match((row.get('name'),))
        if _hosted_relevant(query_terms, row):
            result.append(match)
    stopped(cancel, deadline)
    return tuple(result)


def execute_search(stage, reader, cancel, deadline, *, index=None, transport=hosted,
                   cache_directory=None, prepared_path=None, context=None):
    if not isinstance(stage.action, Search):
        raise ValueError('Expected search stage')
    stopped(cancel, deadline)
    if stage.action.backend.name == 'frozen-source':
        rows = source_search(stage, index, reader, cancel, deadline, cache_directory=cache_directory,
                             prepared_path=prepared_path, context=context)
    elif context is not None and transport is hosted:
        from .search_process import hosted_search
        rows = hosted_search(stage, context)
    else:
        # Custom transports own their isolation; the built-in hosted transport
        # runs in the same bounded worker as frozen-source search.
        rows = transport(stage.action, cancel, deadline)
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
                                    index=self.index, transport=self.transport, context=context, cache_directory=self.cache_directory,
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
