"""Shared candidate, probe, declaration, composition, and in-place source rendering.

Callers supply checked bindings and proof dependencies. These functions neither
admit certificates nor decide which proof or export policy should be selected.
"""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from ..domain import ByteSpan, Signature
from ..domain import encoding as graph
from ..domain.cache import Memo
from ..domain.identity import CLOSED, extends, validate_signature
from .context import Context, _render_context, add_imports, context_opens
from .names import lean_name, parse_identifier
from .terms import RenderPolicy, _Renderer
from .text import Builder, Edit, GeneratedSource, MAX_BYTES, SourceFile, apply_edits

_SIGNATURES = Memo(max_entries=128, max_bytes=16*1024**2)


def _signature(signature):
    def build():
        value = {key: graph.decode(getattr(signature, key)) for key in ('target', 'universes', 'context')}
        validate_signature(value, _validated_graph=True)
        return value
    # Private decoded values are read-only throughout this module. Reuse the
    # same closed type across declaration rendering and its proof application.
    return _SIGNATURES.get(b'', signature, build)


def _target(renderer, signature):
    """Close a flat local context without constructing a nested expression tree."""
    context = signature['context']
    if context[0] != 'local-context':
        return renderer.expr(signature['target'])
    locals_, parts, scope, size = context[1], [], (), 0
    instances = {len(locals_) - 1 - item[2][1] for item in context[2]}
    for index, row in enumerate(locals_):
        name = renderer.binder(row[1], scope)
        if row[0] == 'local':
            binder = 'instance' if index in instances else row[2]
            opening, closing = {'explicit': ('(', ')'), 'implicit': ('{', '}'),
                                'strictImplicit': ('⦃', '⦄'), 'instance': ('[', ']')}[binder]
            part = renderer.join('(∀ ', opening, name, ' : ', renderer.expr(row[4], scope), closing, ', ')
        else:
            part = renderer.join('(let ', name, ' : ', renderer.expr(row[3], scope), ' := ',
                                 renderer.expr(row[4], scope), '; ')
        size += part.size + 1
        if size > renderer.maximum:
            raise ValueError('Rendered Lean source exceeds byte limit')
        parts.append(part)
        scope = (*scope, name)
    return renderer.join(*parts, renderer.expr(signature['target'], scope), ')' * len(locals_))


def _binders(signature):
    context = signature['context']
    if context[0] == 'local-context':
        for row in context[1]:
            if row[0] == 'local':
                yield row[1]
    target = signature['target']
    while target[0] in ('forall', 'lam', 'let'):
        if target[0] != 'let':
            yield target[1]
        target = target[4]


def present_subject(signature: Signature, *, policy=RenderPolicy(readable=True), maximum=MAX_BYTES):
    """Present a closed telescope, retaining named-definition context separately."""
    if signature.presentation:
        if len(signature.presentation) > maximum:
            raise ValueError('Rendered Lean source exceeds byte limit')
        return signature.presentation
    value = _signature(signature)
    renderer = _Renderer(policy, maximum)
    return renderer.finish(_target(renderer, value))


def _named_context(signature):
    context = signature['context']
    return CLOSED if context and context[0] == 'local-context' else context


def _candidate(context, proof, statement, name, policy, maximum):
    if not isinstance(context, Context) or not isinstance(proof, SourceFile):
        raise ValueError('Expected a source context and authored proof')
    signature = _signature(context.signature)
    signatures = {context.signature: signature}
    renderer, writer = _Renderer(policy, maximum), Builder(maximum)
    writer.authored(context.source, ByteSpan(0, context.prefix_end))
    writer.append(b'\n')
    _render_context(_named_context(signature), writer, {})
    parameters = []
    local_names = {lean_name(name) for name in _binders(signature)}
    for alias in context.aliases:
        structural = parse_identifier(alias.name)
        if len(structural) != 3 or structural[1] != ['anonymous']:
            raise ValueError('Candidate aliases must be local names')
        alias_name = lean_name(structural)
        if alias_name in local_names:
            raise ValueError('Dependency alias collides with a local binder')
        if alias.signature not in signatures:
            signatures[alias.signature] = _signature(alias.signature)
        value = signatures[alias.signature]
        # Earlier checked definition prefixes retain the same generated names.
        if not extends(_named_context(signature), _named_context(value)):
            raise ValueError('Alias definition context differs from candidate context')
        parameters.append((alias_name, renderer.finish(_target(renderer, value))))
    writer.append(context_opens(_named_context(signature)))
    writer.append(b'set_option autoImplicit true in\n')
    if name is None:
        writer.append(b'example' if policy.computable else b'noncomputable example')
    else:
        rendered_name = lean_name(graph.decode(name), absolute=True, readable=policy.readable)
        if not rendered_name:
            raise ValueError('Candidate declaration needs a name')
        writer.append((('def ' if policy.computable else 'noncomputable def ')
                       + rendered_name).encode())
    for alias, type_source in parameters:
        writer.append((' (' + alias + ' : ').encode())
        writer.append(type_source)
        writer.append(b')')
    writer.append(b' : (\n')
    if statement is None:
        writer.append(renderer.finish(_target(renderer, signature)))
    else:
        if not isinstance(statement, SourceFile):
            raise ValueError('Expected an authored statement')
        writer.authored(statement)
    writer.append(b'\n) := (\n')
    writer.authored(proof)
    writer.append(b'\n)\n')
    return writer.finish()


def candidate(context, proof, *, statement=None, name=None, imports=(), available=frozenset(),
              current_module=None, policy=RenderPolicy(), maximum=MAX_BYTES):
    if current_module is None and context.source.path.endswith('.lean'):
        current_module = context.source.path[:-5].replace('/', '.')
    result = _candidate(context, proof, statement, name, policy, maximum)
    return add_imports(result, imports, available=available, current_module=current_module, maximum=maximum)


def probe(context, case, *, statement=None, imports=(), available=frozenset(), current_module=None,
          policy=RenderPolicy(), maximum=MAX_BYTES):
    """One probe case uses exactly the same source rules as a candidate."""
    return candidate(context, case, statement=statement, imports=imports, available=available,
                     current_module=current_module, policy=policy, maximum=maximum)


@dataclass(frozen=True, slots=True)
class CheckedDeclaration:
    """Named, closed proof source supplied by checker/export after receipt admission."""
    name: bytes
    signature: Signature
    proof: SourceFile
    requires: tuple[bytes, ...] = ()

    def __post_init__(self):
        if not lean_name(graph.decode(self.name)):
            raise ValueError('Proof declaration needs a name')
        if not isinstance(self.signature, Signature) or not isinstance(self.proof, SourceFile):
            raise ValueError('Expected a signature and authored proof')
        if type(self.requires) is not tuple or any(type(name) is not bytes for name in self.requires):
            raise ValueError('Expected immutable declaration dependencies')
        if len(set(self.requires)) != len(self.requires):
            raise ValueError('Duplicate declaration dependency')


def _references(expression):
    pending, visited, found = [expression], set(), set()
    while pending:
        item = pending.pop()
        if not isinstance(item, list) or id(item) in visited:
            continue
        visited.add(id(item))
        if item and item[0] == 'const':
            found.add(graph.encode(item[1]))
        else:
            pending.extend(item)
    return found


def declarations(values: tuple[CheckedDeclaration, ...], *, external=frozenset(),
                 policy=RenderPolicy(), maximum=MAX_BYTES, emitted=None):
    """Emit each context and named declaration once, with dependencies first."""
    if type(values) is not tuple or type(external) is not frozenset:
        raise ValueError('Expected immutable declarations and external names')
    by_name, signatures, decoded = {}, {}, {}
    for declaration in values:
        if not isinstance(declaration, CheckedDeclaration):
            raise ValueError('Expected checked declarations')
        previous = by_name.get(declaration.name)
        if previous is not None:
            if (previous.signature != declaration.signature or previous.proof.data != declaration.proof.data
                    or previous.requires != declaration.requires):
                raise ValueError('Conflicting shared declaration')
            continue
        by_name[declaration.name] = declaration
        if declaration.signature not in decoded:
            decoded[declaration.signature] = _signature(declaration.signature)
        signatures[declaration.name] = decoded[declaration.signature]
    dependencies = {}
    for name, declaration in by_name.items():
        missing = set(declaration.requires) - by_name.keys() - external
        if missing:
            raise ValueError('Missing checked declaration dependency')
        references = _references(signatures[name]['target']) | _references(signatures[name]['context'])
        dependencies[name] = (set(declaration.requires) | references) & by_name.keys()
    try:
        order = tuple(TopologicalSorter({name: sorted(dependencies[name]) for name in sorted(by_name)}).static_order())
    except CycleError as error:
        raise ValueError('Cyclic source declarations') from error
    writer = Builder(maximum)
    emitted = {} if emitted is None else emitted
    for name in order:
        _render_context(_named_context(signatures[name]), writer, emitted)
    for name in order:
        declaration, signature = by_name[name], signatures[name]
        renderer = _Renderer(policy, maximum)
        writer.append(context_opens(_named_context(signature)))
        writer.append(b'set_option autoImplicit true in\n')
        writer.append((('def ' if policy.computable else 'noncomputable def ')
                       + lean_name(graph.decode(name), absolute=True, readable=policy.readable) + ' : ').encode())
        writer.append(renderer.finish(_target(renderer, signature)))
        writer.append(b' := (\n')
        writer.authored(declaration.proof)
        writer.append(b'\n)\n\n')
    return writer.finish()


def proof_application(name: bytes, signature: Signature, *, policy=RenderPolicy(), maximum=MAX_BYTES):
    """Apply a checked result to the authored declaration's existing telescope."""
    value = _signature(signature)
    writer = Builder(maximum)
    invocation = '@' + lean_name(graph.decode(name), absolute=True, readable=policy.readable)
    if value['universes']:
        invocation += '.{' + ', '.join(lean_name(u, readable=policy.readable) for u in value['universes']) + '}'
    writer.append(b'by\n  first\n')
    writer.append(('  | exact ' + invocation + '\n').encode())
    # Resolve authored locals before unifying the result with the goal. Nested
    # `by` arguments are postponed by Lean; unification of a dependent result
    # can then unfold enormous expressions while its arguments are still holes.
    # The telescope may also include binders still inside the goal, so try each
    # prefix. Keep tactic lookup for unavailable/renamed/anonymous source locals.
    direct = invocation
    for binder in _binders(value):
        if binder[0] != 'str':
            break
        local = lean_name(binder, readable=policy.readable)
        if not local:
            break
        direct += ' ' + local
        writer.append(('  | exact ' + direct + '\n').encode())
    for binder in _binders(value):
        local = lean_name(binder, readable=policy.readable) if binder[0] == 'str' else '_'
        invocation += ' (by first | exact ' + (local or '_') + ' | assumption)'
        writer.append(('  | exact ' + invocation + '\n').encode())
    return writer.finish()


@dataclass(frozen=True, slots=True)
class RootReplacement:
    selector: str
    proof: GeneratedSource
    declarations: GeneratedSource = GeneratedSource(b'')


def integrate(selection, roots, *, imports=(), available=frozenset(), current_module=None, maximum=MAX_BYTES):
    """Replace exactly the selected bodies; preserve authored scopes and attributes."""
    if type(roots) is not tuple or any(not isinstance(root, RootReplacement) for root in roots):
        raise ValueError('Expected immutable checked root replacements')
    by_name = {root.selector: root for root in roots}
    if len(by_name) != len(roots) or set(by_name) != {target.selector for target in selection.selected.targets}:
        raise ValueError('Integration roots differ from selected targets')
    source, edits = SourceFile(selection.selected.request.source, selection.source), []
    if current_module is None:
        current_module = selection.selected.request.source[:-5].replace('/', '.')
    for target in selection.selected.targets:
        root = by_name[target.selector]
        if target.replacement is None or not root.proof.data.strip():
            raise ValueError('Missing replaceable target proof')
        if root.declarations.data:
            edits.append(Edit(ByteSpan(target.declaration.start, target.declaration.start), root.declarations))
        writer = Builder(maximum)
        original = source.data[target.replacement.start:target.replacement.end]
        if original.lstrip().startswith(b':='):
            writer.append(b':= ')
        writer.generated(root.proof)
        # Protect the source suffix from a candidate's trailing line comment.
        writer.append(b'\n')
        edits.append(Edit(target.replacement, writer.finish()))
    result = apply_edits(source, tuple(edits), maximum=maximum)
    return add_imports(result, imports, available=available, current_module=current_module, maximum=maximum)
