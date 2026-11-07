"""Explicit source contexts and imports; no storage, environment admission, or I/O."""

from dataclasses import dataclass
from collections import OrderedDict
import os
import re
from threading import Lock

from ..domain import ByteSpan, FrozenSelection, Signature
from ..domain import encoding as graph
from ..domain.identity import CLOSED, validate_context, validate_imports
from .names import lean_name, parse_identifier
from .text import Builder, GeneratedSource, MAX_BYTES, SourceFile, byte_limit

class _ContextSources:
    """Bounded immutable output reuse with conservative, source-specific sizing."""

    def __init__(self, max_entries=128, max_bytes=16 * 1024**2):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.lock = Lock()
        self.entries = OrderedDict()
        self.bytes = 0

    def reset(self):
        with self.lock:
            self.entries.clear()
            self.bytes = 0

    def get(self, key, build):
        with self.lock:
            found = self.entries.get(key)
            if found is not None:
                self.entries.move_to_end(key)
                return found[0]
        value = build()
        # Include retained key/output bytes, mapping objects, labels, and the
        # maximum lazy line-index allocation. No recursive object-graph walk.
        size = 1024 + len(key[0]) + len(value.data)
        for row in value.mappings:
            size += (1024 + len(row.original.path.encode()) * 4 + len(row.original.data)
                     + 40 * (row.original.data.count(b'\n') + 1))
        if size <= self.max_bytes and self.max_entries:
            with self.lock:
                if key not in self.entries:
                    while self.entries and (len(self.entries) >= self.max_entries or self.bytes + size > self.max_bytes):
                        _, (_, removed) = self.entries.popitem(last=False)
                        self.bytes -= removed
                    self.entries[key] = value, size
                    self.bytes += size
        return value


_CONTEXT_SOURCES = _ContextSources()


def _reset_context_sources_after_fork():
    # A worker must not inherit a lock held by another thread at fork time.
    _CONTEXT_SOURCES.lock = Lock()
    _CONTEXT_SOURCES.entries = OrderedDict()
    _CONTEXT_SOURCES.bytes = 0


os.register_at_fork(after_in_child=_reset_context_sources_after_fork)


@dataclass(frozen=True, slots=True)
class CheckedAlias:
    """Checker-supplied interface; construction does not authenticate a receipt."""
    name: str
    signature: Signature

    def __post_init__(self):
        if parse_identifier(self.name) is None or not isinstance(self.signature, Signature):
            raise ValueError('Invalid checked alias')


@dataclass(frozen=True, slots=True)
class Context:
    source: SourceFile
    prefix_end: int
    signature: Signature
    aliases: tuple[CheckedAlias, ...] = ()

    def __post_init__(self):
        from .text import check_span
        check_span(self.source.data, ByteSpan(0, self.prefix_end))
        if not isinstance(self.signature, Signature):
            raise ValueError('Expected a signature')
        _aliases(self.aliases)


@dataclass(frozen=True, slots=True)
class LookupContext:
    source: SourceFile
    prefix_end: int
    aliases: tuple[CheckedAlias, ...]


def _aliases(aliases):
    if type(aliases) is not tuple or any(not isinstance(a, CheckedAlias) for a in aliases):
        raise ValueError('Expected immutable checked aliases')
    keys = [lean_name(parse_identifier(a.name)) for a in aliases]
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate checked alias')


def _source(selection, target_index):
    if (not isinstance(selection, FrozenSelection) or type(target_index) is not int
            or not 0 <= target_index < len(selection.selected.targets)):
        raise ValueError('Invalid selected source target')
    return (SourceFile(selection.selected.request.source, selection.source),
            selection.selected.targets[target_index].declaration.start)


def declaration_context(selection, target_index, *, signature=None, aliases=()):
    """Use an admitted selection and, for a derived goal, its exact checked signature."""
    source, offset = _source(selection, target_index)
    if signature is None:
        identity = selection.identities[target_index]
        signature = Signature(identity.target, identity.universes, identity.context)
    return Context(source, offset, signature, aliases)


def lookup_context(selection, target_index, queries, *, aliases=()):
    """Keep only requested checked aliases. Never decode any goal or alias type."""
    source, offset = _source(selection, target_index)
    _aliases(aliases)
    if type(queries) is not tuple or any(not isinstance(q, str) for q in queries):
        raise ValueError('Expected immutable lookup queries')
    by_name = {lean_name(parse_identifier(a.name)): a for a in aliases}
    result, seen = [], set()
    for query in queries:
        name = parse_identifier(query)
        key = lean_name(name) if name is not None else None
        if key in by_name and key not in seen:
            seen.add(key)
            result.append(by_name[key])
    return LookupContext(source, offset, tuple(result))


def render_context(context: bytes, *, maximum=MAX_BYTES):
    """Render sequential named definitions from a compact domain context."""
    byte_limit(maximum)
    if type(context) is not bytes or len(context) > MAX_BYTES:
        raise ValueError('Expected bounded binary context bytes')
    def build():
        writer = Builder(maximum)
        _render_context(graph.decode(context), writer, {})
        return writer.finish()
    # Rendering depends on these exact bytes only, not an environment.
    return _CONTEXT_SOURCES.get((context, maximum), build)


class _ContextScope:
    """Reuse open namespace prefixes within one flat, temporary section."""

    def __init__(self, writer, *, sections=None):
        self.writer, self.namespaces = writer, ()
        self.sections, self.start = sections, 0

    def open(self, namespaces):
        namespaces = tuple(namespaces)
        if namespaces == self.namespaces:
            return
        if namespaces[:len(self.namespaces)] != self.namespaces:
            self.close()
        if namespaces and not self.namespaces:
            self.start = self.writer.size
            self.writer.append(b'section\n')
        self.writer.append(''.join('open ' + ns + '\n'
                                   for ns in namespaces[len(self.namespaces):]).encode())
        self.namespaces = namespaces

    def close(self):
        if self.namespaces:
            self.writer.append(b'end\n\n')
            if self.sections is not None:
                self.sections.append(ByteSpan(self.start, self.writer.size))
            self.namespaces = ()


def _render_context(context, writer, emitted, *, scope=None, spans=None):
    validate_context(context)
    if context == CLOSED:
        return
    namespaces = []
    for row in context[2]:
        key = row['namespace'], row['name']
        previous = emitted.get(key)
        if previous is not None and previous != row:
            raise ValueError('Conflicting context declaration')
        if previous is None:
            emitted[key] = row
            if scope is None:
                writer.append(''.join('open ' + ns + ' in\n' for ns in namespaces).encode())
            else:
                scope.open(namespaces)
            start = writer.size
            writer.append(('noncomputable ' + row['kind'] + ' _root_.' + row['namespace'] + '.'
                           + lean_name(['str', ['anonymous'], row['name']]) + ' : (\n').encode())
            writer.authored(SourceFile(row['namespace'] + '/type', row['type'].encode()))
            writer.append(b'\n) := (\n')
            writer.authored(SourceFile(row['namespace'] + '/value', row['value'].encode()))
            writer.append(b'\n)\n\n')
            if spans is not None:
                name = ['str', ['str', ['anonymous'], row['namespace']], row['name']]
                spans.append((name, ByteSpan(start, writer.size)))
        namespaces.append(row['namespace'])


def _context_namespaces(context):
    if context == CLOSED:
        return ()
    if context[0] != 'declaration-context-v2':
        raise ValueError('Source declarations require a closed or named definition context')
    return tuple(row['namespace'] for row in context[2])


def context_opens(context):
    return ''.join('open ' + ns + ' in\n' for ns in _context_namespaces(context)).encode()


def merge_imports(*groups):
    for group in groups:
        if type(group) is not tuple:
            raise ValueError('Expected immutable imports')
        validate_imports(list(group))
    result = tuple(dict.fromkeys(module for group in groups for module in group))
    validate_imports(list(result))
    return result


_MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')


def module_name(value):
    if not isinstance(value, str) or len(value) > 256 or not _MODULE.fullmatch(value):
        raise ValueError('Invalid Lean module name')
    return value


def check_imports(imports, available, *, current_module=None):
    imports = merge_imports(imports)
    if type(available) is not frozenset:
        raise ValueError('Expected an admitted module-name set')
    if current_module is not None:
        module_name(current_module)
    for module in imports:
        if module not in available or module == current_module:
            raise ValueError('Unavailable or self-importing module: ' + module)
    return imports


_TRIVIA = re.compile(rb'\s+|--[^\n]*(?:\n|$)')
_COMMENT = re.compile(rb'/-|-/')
_MODULE_HEADER = re.compile(rb'module\b')


def _import_position(data):
    offset = 0
    while offset < len(data):
        match = _TRIVIA.match(data, offset)
        if match:
            offset = match.end()
        elif data.startswith(b'/-', offset):
            depth = 1
            for boundary in _COMMENT.finditer(data, offset + 2):
                depth += 1 if boundary[0] == b'/-' else -1
                if not depth:
                    offset = boundary.end()
                    break
            if depth:
                raise ValueError('Unclosed source header comment')
        else:
            break
    header = _MODULE_HEADER.match(data, offset)
    return (header.end(), b'\n') if header else (offset, b'')


def add_imports(source: GeneratedSource, imports, *, available, current_module=None, maximum=MAX_BYTES):
    imports = check_imports(imports, available, current_module=current_module)
    return _insert_imports(source, imports, maximum)


def _insert_imports(source, imports, maximum):
    byte_limit(maximum)
    if not imports:
        if len(source.data) > maximum:
            raise ValueError('Generated source exceeds byte limit')
        return source
    position, newline = _import_position(source.data)
    writer = Builder(maximum)
    writer.slice(source, ByteSpan(0, position))
    writer.append(newline + ''.join('import ' + name + '\n' for name in imports).encode())
    writer.slice(source, ByteSpan(position, len(source.data)))
    return writer.finish()
