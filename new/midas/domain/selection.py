"""Target selection over explicit Lean-produced evidence and admitted input bindings.

No Lean text/name parsing occurs here. The extractor must provide canonical
selectors, structural constant names, declaration spans, and complete references.
"""

from dataclasses import dataclass, field
from functools import cached_property
from hashlib import sha256
from pathlib import PurePosixPath
from struct import Struct
from types import MappingProxyType
from typing import Mapping

from .records import identifier, natural
from . import encoding as codec
from .identity import ObligationIdentity, validate_signature


class SelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceBinding:
    source: str
    sha256: str
    byte_count: int

    def __post_init__(self):
        SelectionRequest(self.source, all_unfinished=True)
        identifier(self.sha256, '')
        natural(self.byte_count, 'Source byte count')


@dataclass(frozen=True, slots=True)
class SelectionInputs:
    """Admitted environment hash and authorized source metadata, without a manifest."""
    environment_id: str
    sources: tuple[SourceBinding, ...]
    sources_by_path: Mapping[str, SourceBinding] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        identifier(self.environment_id, 'env_')
        if type(self.sources) is not tuple or any(not isinstance(item, SourceBinding) for item in self.sources):
            raise SelectionError('Expected immutable authorized source bindings')
        index = {item.source: item for item in self.sources}
        if len(index) != len(self.sources):
            raise SelectionError('Duplicate authorized source path')
        object.__setattr__(self, 'sources_by_path', MappingProxyType(index))


@dataclass(frozen=True)
class ByteSpan:
    start: int
    end: int


@dataclass(frozen=True)
class Declaration:
    selector: str                 # Canonical fully qualified, escaped Lean name.
    constant: bytes               # Structural Lean Name; never a display alias.
    kind: str
    declaration: ByteSpan
    replacement: ByteSpan | None
    unfinished: bool              # Explicit sorry in this declaration's own value.
    type_has_sorry: bool = False
    synthetic_sorry: bool = False
    supported: bool = True        # Extractor validated an independently replaceable body.
    type_references: tuple[bytes, ...] = ()
    value_references: tuple[bytes, ...] = ()
    source_authored: bool = True


@dataclass(frozen=True)
class SelectionRequest:
    source: str                   # One project-relative POSIX Lean source path.
    names: tuple[str, ...] = ()
    all_unfinished: bool = False

    def __post_init__(self):
        if not isinstance(self.source, str):
            raise SelectionError("source must be a canonical project-relative .lean path")
        path = PurePosixPath(self.source)
        if (not self.source or "\\" in self.source or "\x00" in self.source
                or path.is_absolute() or path.as_posix() != self.source
                or ".." in path.parts or path.suffix != ".lean"):
            raise SelectionError("source must be one canonical project-relative .lean path")
        if not isinstance(self.names, tuple) or any(not isinstance(n, str) or not n for n in self.names):
            raise SelectionError("target names must be an immutable tuple of canonical Lean selectors")
        if len(set(self.names)) != len(self.names):
            raise SelectionError("duplicate target selector")
        if type(self.all_unfinished) is not bool or bool(self.names) == self.all_unfinished:
            raise SelectionError("choose explicit targets or all_unfinished, exclusively")


@dataclass(frozen=True)
class SelectedTargets:
    request: SelectionRequest
    source_sha256: str
    targets: tuple[Declaration, ...]     # Source order, independent of request order.
    remaining_unfinished: tuple[str, ...]


def select_targets(request: SelectionRequest, source: bytes,
                   declarations: tuple[Declaration, ...], *,
                   imported_constants: frozenset[bytes] = frozenset()) -> SelectedTargets:
    """Resolve one complete source inventory; never widen the requested scope.

    Inventory completeness and imported-environment trust are extractor/preflight
    obligations. External references must be explicitly classified as pinned imports.
    """
    if not isinstance(request, SelectionRequest):
        raise SelectionError('Expected a selection request')
    if not isinstance(source, bytes):
        raise SelectionError("source must be immutable snapshot bytes")
    try:
        source.decode("utf-8")
    except UnicodeError as error:
        raise SelectionError("source is not valid UTF-8") from error
    if not isinstance(declarations, tuple):
        raise SelectionError("declaration inventory must be immutable")
    if not isinstance(imported_constants, frozenset) or any(
        not isinstance(c, bytes) or not c for c in imported_constants
    ):
        raise SelectionError("imported constants must be immutable structural names")

    def valid_span(span):
        if (not isinstance(span, ByteSpan)
                or type(span.start) is not int or type(span.end) is not int
                or not 0 <= span.start < span.end <= len(source)):
            raise SelectionError("invalid source byte span")
        for offset in (span.start, span.end):
            if offset < len(source) and source[offset] & 0xC0 == 0x80:
                raise SelectionError("source span splits a UTF-8 character")

    by_name, by_constant = {}, {}
    for decl in declarations:
        if not isinstance(decl, Declaration):
            raise SelectionError('Expected declaration evidence')
        if not isinstance(decl.selector, str) or not decl.selector or not isinstance(decl.constant, bytes) or not decl.constant:
            raise SelectionError("declaration is missing its selector or structural name")
        if any(type(flag) is not bool for flag in (
            decl.unfinished, decl.type_has_sorry, decl.synthetic_sorry, decl.supported, decl.source_authored
        )):
            raise SelectionError("declaration evidence flags must be explicit booleans")
        if decl.constant in by_constant or decl.constant in imported_constants:
            raise SelectionError("duplicate structural declaration name")
        valid_span(decl.declaration)
        if decl.replacement is not None:
            valid_span(decl.replacement)
            if not decl.declaration.start < decl.replacement.start < decl.replacement.end <= decl.declaration.end:
                raise SelectionError("replacement span escapes declaration")
        for refs in (decl.type_references, decl.value_references):
            if not isinstance(refs, tuple) or any(not isinstance(r, bytes) or not r for r in refs):
                raise SelectionError("references must be immutable structural names")
        if decl.synthetic_sorry or decl.type_has_sorry:
            raise SelectionError(f"invalid extracted input: {decl.selector} has a recovered error or type hole")
        by_name.setdefault(decl.selector, []).append(decl)
        by_constant[decl.constant] = decl

    names = request.names if not request.all_unfinished else tuple(
        d.selector for d in declarations if d.unfinished and d.source_authored)
    if not names:
        raise SelectionError("no unfinished declarations selected")
    selected = []
    for name in names:
        matches = by_name.get(name, [])
        if not matches:
            raise SelectionError(f"target not found in selected source: {name}")
        if len(matches) != 1:
            raise SelectionError(f"ambiguous source selector: {name}")
        decl = matches[0]
        if not decl.unfinished:
            raise SelectionError(f"target is already complete: {name}")
        if (not decl.source_authored or decl.kind not in ("theorem", "def")
                or not decl.supported or decl.replacement is None):
            raise SelectionError(f"unsupported target declaration form: {name}")
        selected.append(decl)
    selected.sort(key=lambda d: d.declaration.start)
    for left, right in zip(selected, selected[1:]):
        if left.declaration.end > right.declaration.start:
            raise SelectionError("selected declarations overlap; grouped declarations are unsupported")

    chosen = {d.constant for d in selected}
    missing = set()
    visited = set()
    # Candidate bodies of selected targets are replaced, so their old references
    # do not grant prerequisites. Fixed helper values remain part of the context.
    pending = [r for d in selected for r in d.type_references]
    while pending:
        constant = pending.pop()
        if constant in visited:
            continue
        visited.add(constant)
        dep = by_constant.get(constant)
        if dep is None:
            if constant not in imported_constants:
                raise SelectionError("incomplete extraction: unclassified constant reference")
            continue
        if dep.unfinished and constant not in chosen:
            if not dep.source_authored:
                raise SelectionError(f"unfinished generated prerequisite is unsupported: {dep.selector}")
            missing.add(dep.selector)
        pending.extend(dep.type_references)
        if not dep.unfinished:
            pending.extend(dep.value_references)
    if missing:
        raise SelectionError("select required unfinished prerequisites explicitly: " + ", ".join(sorted(missing)))

    remaining = tuple(d.selector for d in sorted(declarations, key=lambda d: d.declaration.start)
                      if d.unfinished and d.source_authored and d.constant not in chosen)
    return SelectedTargets(request, sha256(source).hexdigest(), tuple(selected), remaining)


SELECTION_FORMAT = 'midas-selection-v3'
SELECTION_MAGIC = b'MIDASSEL\x03'
MAX_BYTES = 64 * 1024**2
NAME_BYTES = 16 * 1024
MAX_RECORDS = 1_000_000
_U64 = Struct('<Q')


@dataclass(frozen=True, slots=True)
class Signature:
    """Flat references to canonical binary expression graphs; no decoded tree retained."""
    target: bytes
    universes: bytes
    context: bytes

    def __post_init__(self):
        for value in (self.target, self.universes, self.context):
            if type(value) is not bytes or not value.startswith(codec.MAGIC) or len(value) > MAX_BYTES:
                raise SelectionError('Signatures require bounded binary graph components')


@dataclass(frozen=True)
class FrozenSelection:
    environment_id: str
    source: bytes
    declarations: tuple[Declaration, ...]
    imported_constants: tuple[bytes, ...]
    signatures: tuple[Signature, ...]  # Aligned with declarations, never a nested map.
    selected: SelectedTargets
    identities: tuple[ObligationIdentity, ...]

    @cached_property
    def id(self):
        # Hash the canonical field stream without materializing a saved file.
        digest = sha256()
        for part in _encoded_parts(self):
            digest.update(part)
        return 'sel_' + digest.hexdigest()


def _constant(payload):
    # Structural names are small metadata, with an explicit cap even on cache hits.
    value = codec.load_json(payload, limit=NAME_BYTES)
    codec.name(value)
    return value


def _build_selection(request, source, declarations, imported_constants, signatures, inputs):
    """Shared typed admission for construction and loading; performs no selection I/O."""
    if not isinstance(inputs, SelectionInputs):
        raise SelectionError('Expected an environment hash and authorized source bindings')
    if type(source) is not bytes or len(source) > MAX_BYTES:
        raise SelectionError('Source exceeds selection byte limit')
    selected = select_targets(request, source, declarations, imported_constants=imported_constants)
    if any(len(items) > MAX_RECORDS for items in (request.names, declarations, imported_constants)):
        raise SelectionError('Selection record count exceeds limit')
    record = inputs.sources_by_path.get(request.source)
    if record is None or record.byte_count != len(source) or record.sha256 != selected.source_sha256:
        raise SelectionError('Selection source differs from authorized input')
    if len(selected.targets) > 64:
        raise SelectionError('Frozen selection supports at most 64 targets')
    if type(signatures) is not tuple or len(signatures) != len(declarations):
        raise SelectionError('Signature inventory differs from declarations')

    imports = tuple(sorted(imported_constants))
    for constant in imports:
        _constant(constant)
    encoded_interfaces = {}
    selected_constants = {target.constant for target in selected.targets}
    for decl, signature in zip(declarations, signatures):
        if not isinstance(signature, Signature):
            raise SelectionError('Expected a typed binary signature')
        constant = _constant(decl.constant)
        if decl.kind not in ('theorem', 'def', 'other'):
            raise SelectionError('Invalid declaration kind')
        for references in (decl.type_references, decl.value_references):
            if len(references) > MAX_RECORDS:
                raise SelectionError('Selection reference count exceeds limit')
            if len(set(references)) != len(references):
                raise SelectionError('Duplicate constant reference')
            for reference in references:
                _constant(reference)
        decoded = {key: codec.decode(getattr(signature, key)) for key in ('target', 'universes', 'context')}
        if decoded['context'] != ['closed-telescope']:
            raise SelectionError('Source declaration must have a closed signature')
        validate_signature(decoded, _validated_graph=True)
        pending, visited, used = [decoded['target']], set(), set()
        while pending:
            item = pending.pop()
            if isinstance(item, list) and id(item) not in visited:
                visited.add(id(item))  # Visit shared graph nodes once, never expand a DAG.
                if len(item) == 3 and item[0] == 'const':
                    codec.require_tree_budget(item[1], limit=NAME_BYTES)
                    used.add(codec.canonical(item[1]))
                pending.extend(child for child in item if isinstance(child, list))
        if used != set(decl.type_references):
            raise SelectionError('Signature constant references disagree with inventory')
        if decl.constant in selected_constants:
            encoded_interfaces[decl.constant] = codec.encode({'constant': constant, 'signature': decoded})

    # Peer interfaces are encoded once and shared by all roots.
    by_constant = dict(zip((decl.constant for decl in declarations), signatures))
    identities = []
    retained_bytes = 0
    for target in selected.targets:
        signature = by_constant[target.constant]
        interfaces = tuple(encoded_interfaces[peer.constant] for peer in selected.targets
                           if peer.constant != target.constant)
        identity = ObligationIdentity(signature.target, signature.universes, signature.context,
                                      inputs.environment_id, interfaces)
        retained_bytes += 512 + 2 * sum(map(len, (signature.target, signature.universes,
                                                signature.context, *interfaces)))
        if retained_bytes > MAX_BYTES:
            raise SelectionError('Selection identity graphs exceed byte limit')
        identities.append(identity)
    result = FrozenSelection(inputs.environment_id, source, declarations, imports, signatures,
                             selected, tuple(identities))
    # Check the persisted size without serializing source or allocating an envelope.
    for _ in _encoded_parts(result):
        pass
    return result


def freeze_selection(request, source, declarations, imported_constants, signatures, inputs):
    """Validate explicit values once. Saving is a separate encode_selection call."""
    if not isinstance(signatures, Mapping):
        raise SelectionError('Expected signatures indexed by structural constant')
    if type(declarations) is not tuple or any(not isinstance(decl, Declaration) for decl in declarations):
        raise SelectionError('Expected immutable declaration inventory')
    if len(signatures) != len(declarations):
        raise SelectionError('Signature inventory differs from declarations')
    try:
        ordered = tuple(signatures[decl.constant] for decl in declarations)
    except KeyError as error:
        raise SelectionError('Missing declaration signature') from error
    return _build_selection(request, source, declarations, imported_constants, ordered, inputs)


def _number(value):
    natural(value, 'Selection integer')
    return _U64.pack(value)


def _fields(selection):
    """Flat, length-delimited fields. Source and graph bytes are copied only on save."""
    request = selection.selected.request
    yield selection.environment_id.encode('ascii')
    yield request.source.encode('utf-8')
    yield bytes((request.all_unfinished,))
    yield _number(len(request.names))
    for name in request.names:
        yield name.encode('utf-8')
    yield selection.source
    yield _number(len(selection.imported_constants))
    yield from selection.imported_constants
    yield _number(len(selection.declarations))
    for decl, signature in zip(selection.declarations, selection.signatures):
        yield decl.selector.encode('utf-8')
        yield decl.constant
        yield decl.kind.encode('ascii')
        yield _number(decl.declaration.start)
        yield _number(decl.declaration.end)
        yield bytes((decl.replacement is not None,))
        if decl.replacement is not None:
            yield _number(decl.replacement.start)
            yield _number(decl.replacement.end)
        yield bytes((decl.unfinished, decl.type_has_sorry, decl.synthetic_sorry,
                     decl.supported, decl.source_authored))
        for references in (decl.type_references, decl.value_references):
            yield _number(len(references))
            yield from references
        yield signature.target
        yield signature.universes
        yield signature.context


def _encoded_parts(selection):
    yield SELECTION_MAGIC
    size = len(SELECTION_MAGIC)
    for value in _fields(selection):
        size += _U64.size + len(value)
        if size > MAX_BYTES:
            raise SelectionError('Saved selection exceeds byte limit')
        yield _U64.pack(len(value))
        yield value


def encode_selection(selection: FrozenSelection) -> bytes:
    """Serialize one selection to the v3 binary format at the persistence boundary."""
    if not isinstance(selection, FrozenSelection):
        raise SelectionError('Expected frozen selection')
    return b''.join(_encoded_parts(selection))


class _Reader:
    def __init__(self, payload):
        if type(payload) is not bytes or len(payload) > MAX_BYTES:
            raise SelectionError('Expected bounded binary selection bytes')
        if not payload.startswith(SELECTION_MAGIC):
            raise SelectionError('Expected midas-selection-v3 binary bytes')
        self.payload = payload
        self.position = len(SELECTION_MAGIC)

    def take(self, limit=MAX_BYTES):
        if self.position + _U64.size > len(self.payload):
            raise SelectionError('Truncated selection field')
        size = _U64.unpack_from(self.payload, self.position)[0]
        self.position += _U64.size
        end = self.position + size
        if size > limit or end > len(self.payload):
            raise SelectionError('Truncated or oversized selection field')
        result = self.payload[self.position:end]
        self.position = end
        return result

    def number(self):
        value = self.take(_U64.size)
        if len(value) != _U64.size:
            raise SelectionError('Invalid selection integer')
        result = _U64.unpack(value)[0]
        natural(result, 'Selection integer')
        return result

    def count(self):
        result = self.number()
        if result > MAX_RECORDS or result > (len(self.payload) - self.position) // _U64.size:
            raise SelectionError('Selection count exceeds remaining fields')
        return result

    def flags(self, count):
        value = self.take(count)
        if len(value) != count or any(flag not in (0, 1) for flag in value):
            raise SelectionError('Invalid selection boolean flags')
        return tuple(bool(flag) for flag in value)


def load_selection(payload, inputs: SelectionInputs, *, expected_id=None):
    """Decode once, then use the same typed admission as freeze_selection."""
    if not isinstance(inputs, SelectionInputs):
        raise SelectionError('Expected an environment hash and authorized source bindings')
    reader = _Reader(payload)
    if reader.take(68).decode('ascii') != inputs.environment_id:
        raise SelectionError('Selection environment mismatch')
    source_path = reader.take().decode('utf-8')
    all_unfinished, = reader.flags(1)
    names = tuple(reader.take().decode('utf-8') for _ in range(reader.count()))
    request = SelectionRequest(source_path, names, all_unfinished)
    source = reader.take()
    imports = tuple(reader.take(NAME_BYTES) for _ in range(reader.count()))
    if imports != tuple(sorted(set(imports))):
        raise SelectionError('Imported constants must be unique and sorted')
    declarations, signatures = [], []
    for _ in range(reader.count()):
        selector = reader.take().decode('utf-8')
        constant = reader.take(NAME_BYTES)
        kind = reader.take(7).decode('ascii')
        declaration = ByteSpan(reader.number(), reader.number())
        replacement = ByteSpan(reader.number(), reader.number()) if reader.flags(1)[0] else None
        unfinished, type_hole, synthetic, supported, authored = reader.flags(5)
        type_refs = tuple(reader.take(NAME_BYTES) for _ in range(reader.count()))
        value_refs = tuple(reader.take(NAME_BYTES) for _ in range(reader.count()))
        signatures.append(Signature(reader.take(), reader.take(), reader.take()))
        declarations.append(Declaration(selector, constant, kind, declaration, replacement, unfinished,
                                        type_hole, synthetic, supported, type_refs, value_refs, authored))
    if reader.position != len(payload):
        raise SelectionError('Trailing selection bytes')
    result = _build_selection(request, source, tuple(declarations), frozenset(imports), tuple(signatures), inputs)
    saved_id = 'sel_' + sha256(payload).hexdigest()
    if expected_id is not None and expected_id != saved_id:
        raise SelectionError('Selection hash mismatch')
    # The flat grammar has exactly one representation for the admitted records.
    object.__setattr__(result, 'id', saved_id)
    return result
