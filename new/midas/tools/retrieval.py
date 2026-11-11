"""Per-query backend merging and cross-query declaration deduplication."""
from dataclasses import dataclass, replace
from functools import lru_cache

from ..source import lean_name, parse_identifier
from .bm25 import TOTAL
from .search import Match


@lru_cache(maxsize=4096)
def canonical(name):
    parsed = parse_identifier(name)
    return lean_name(parsed, readable=True) if parsed is not None else None


def allowed_names(forbidden):
    denied = {canonical(name) for name in forbidden}
    def allowed(name):
        key = canonical(name)
        return key is not None and key not in denied and not key.startswith('Midas')
    return allowed


@dataclass(frozen=True, slots=True)
class Hit:
    key: tuple
    name: str | None
    match: Match
    rank: int


@dataclass(frozen=True, slots=True)
class QueryHits:
    query: str
    local: bool
    hits: tuple[Hit, ...]
    errors: tuple[tuple[str, str], ...]


def _source_key(hit):
    match = hit.match
    return (not bool(match.source), match.path is None, match.path or '',
            match.line, hit.name or '', match.source)


def merge_searches(searches, *, allowed=lambda name: True):
    """Keep the best backend rank, a useful source, and at most 50 unique hits."""
    queries = {}
    for result, search in searches:
        hits, errors, local = queries.setdefault(search.query, ({}, [], [False]))
        local[0] |= search.backend.name == 'frozen-source'
        if result.error:
            errors.append((search.backend.name, result.error))
            continue
        found = set()
        for match in result.records:
            for name in match.names or (None,):
                identity = canonical(name) if name is not None else None
                if name is not None and (identity is None or not allowed(name)):
                    continue
                key = ('name', identity) if name is not None else ('source', match.path, match.line)
                if key in found:
                    continue
                rank = len(found)
                found.add(key)
                hit = Hit(key, name, replace(match, names=(name,)) if name is not None else match, rank)
                previous = hits.get(key)
                if previous is not None:
                    preferred = min((previous, hit), key=_source_key)
                    hit = replace(preferred, rank=min(previous.rank, rank))
                hits[key] = hit
    return tuple(QueryHits(query, local[0],
        tuple(sorted(hits.values(), key=lambda hit: (hit.rank, hit.key))[:TOTAL]), tuple(errors))
        for query, (hits, errors, local) in queries.items())


def shared_hits(queries):
    """Return each declaration in multiple query lists once, with query indices."""
    hits, memberships = {}, {}
    for index, query in enumerate(queries):
        for hit in query.hits:
            if hit.name is None:
                continue
            memberships.setdefault(hit.key, []).append(index)
            previous = hits.get(hit.key)
            if previous is None:
                hits[hit.key] = hit
            else:
                hits[hit.key] = replace(min((previous, hit), key=_source_key),
                                        rank=min(previous.rank, hit.rank))
    return tuple((hit, tuple(memberships[hit.key])) for hit in
        sorted(hits.values(), key=lambda hit: (hit.rank, hit.key)) if len(memberships[hit.key]) > 1)
