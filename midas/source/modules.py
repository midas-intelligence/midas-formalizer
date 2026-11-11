"""Flat generated Lean module layouts, independent of export storage and auditing."""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from .context import _insert_imports, module_name
from .names import plain_identifier
from .text import GeneratedSource, MAX_BYTES, byte_limit


def generated_module(identity: str):
    if not isinstance(identity, str) or not identity or len(identity.encode()) > 1024:
        raise ValueError('Invalid generated module identity')
    return 'Midas_' + sha256(identity.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Module:
    name: str
    body: GeneratedSource
    imports: tuple[str, ...] = ()

    def __post_init__(self):
        module_name(self.name)
        if not plain_identifier(self.name):
            raise ValueError('Generated module names must be flat identifiers')
        if not isinstance(self.body, GeneratedSource) or type(self.imports) is not tuple:
            raise ValueError('Expected source and immutable module imports')
        if len(set(self.imports)) != len(self.imports):
            raise ValueError('Duplicate module imports')
        for name in self.imports:
            module_name(name)


@dataclass(frozen=True, slots=True)
class SourceTree:
    entry: str
    files: Mapping[str, GeneratedSource]
    build_order: tuple[str, ...]


def module_tree(modules: tuple[Module, ...], entry: str, *, available=frozenset(), maximum=MAX_BYTES):
    """Validate reachability and cycles, then return raw files in dependency order."""
    maximum = byte_limit(maximum)
    if (type(modules) is not tuple or not 1 <= len(modules) <= 10000
            or any(not isinstance(module, Module) for module in modules) or type(available) is not frozenset):
        raise ValueError('Invalid generated module inventory')
    by_name = {module.name: module for module in modules}
    if len({name.casefold() for name in by_name}) != len(modules) or entry not in by_name:
        raise ValueError('Duplicate module names or missing entry')
    graph = {}
    for name, module in by_name.items():
        if name in available:
            raise ValueError('Generated module shadows an admitted module')
        if set(module.imports) - by_name.keys() - available:
            raise ValueError('Unavailable module import')
        graph[name] = set(module.imports) & by_name.keys()
    reachable, pending = set(), [entry]
    while pending:
        name = pending.pop()
        if name not in reachable:
            reachable.add(name)
            pending.extend(graph[name])
    if reachable != by_name.keys():
        raise ValueError('Unreachable generated module')
    try:
        order = tuple(TopologicalSorter({name: sorted(graph[name]) for name in sorted(graph)}).static_order())
    except CycleError as error:
        raise ValueError('Cyclic generated modules') from error
    files, size = {}, 0
    for name in order:
        module = by_name[name]
        source = _insert_imports(module.body, module.imports, maximum - size)
        files[name + '.lean'] = source
        size += len(source.data)
    return SourceTree(entry + '.lean', MappingProxyType(files), order)
