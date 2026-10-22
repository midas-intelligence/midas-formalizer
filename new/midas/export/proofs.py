"""Closed source composition and integration over admitted, versioned proofs."""
from hashlib import sha256

from ..checker import evidence as ev
from ..checker.proofs import prefix_proof as _prefix_proof
from ..checker.requests import read
from ..domain import ByteSpan, DependencyOrigin, ProofReference, ProofState, Signature
from ..domain import encoding as codec
from ..source import (CheckedDeclaration, Edit, GeneratedSource, RenderPolicy, RootReplacement, SourceFile,
                      add_imports, apply_edits, declarations, integrate, lean_name, merge_imports,
                      parse_identifier, proof_application)
from .protocol import NodeSnapshot


def same(left, right):
    """Compare DAGs without serializing them or expanding shared branches."""
    pending, seen = [(left, right)], set()
    while pending:
        a, b = pending.pop()
        if a is b:
            continue
        if type(a) is not type(b):
            return False
        if type(a) in (dict, list):
            key = id(a), id(b)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(a, dict):
                if a.keys() != b.keys():
                    return False
                pending.extend((v, b[k]) for k, v in a.items())
            else:
                if len(a) != len(b):
                    return False
                pending.extend(zip(a, b))
        elif a != b:
            return False
    return True


def name_key(name):
    parts = []
    while name != ['anonymous']:
        parts.append((name[0], name[2]))
        name = name[1]
    return tuple(parts)


def bind_peers(selection, graph, candidate, packet, reader, *, available):
    """Replace earlier selected definition holes before replaying dependent proofs."""
    by_constant = {t.constant: (i, t) for i, t in enumerate(selection.selected.targets)}
    parent = by_constant[codec.canonical(packet['constant'])][1]
    edits, imports, contexts, seen = [], tuple(packet['imports']), {}, set()
    for dependency in candidate.dependencies:
        if dependency.origin not in (DependencyOrigin.FROZEN_PEER, DependencyOrigin.CLOSED_DEFINITION):
            continue
        interface = codec.decode(read(reader, dependency.interface))
        # Names, unlike signatures/source, are bounded selection metadata.
        target = by_constant.get(codec.canonical(interface['constant']))
        if target is None or target[1].kind != 'def':
            continue
        target = target[1]
        if target.declaration.start >= parent.declaration.start or target.constant in seen:
            raise ValueError('Assembly definition must be a distinct earlier selected target')
        seen.add(target.constant)
        reference = graph.node(dependency.owner).proof
        if reference is None or dependency.certificate is not None and reference.evidence != dependency.certificate:
            raise ValueError('Assembly definition proof changed')
        proof = load(reader, reference.evidence)
        if not same(proof['signature'], interface['signature']):
            raise ValueError('Assembly definition signature changed')
        namespace = 'MidasReplay_' + reference.evidence.sha256
        rows, root = _prefix_proof(proof, namespace)
        policy = RenderPolicy(computable=b'noncomputable' not in
            selection.source[target.declaration.start:target.replacement.start])
        helpers = declarations(rows, policy=policy, emitted=contexts)
        invocation = proof_application(root, ev.closed(proof['signature']), policy=policy)
        prefix = b':= ' if selection.source[target.replacement.start:target.replacement.end].lstrip().startswith(b':=') else b''
        edits.extend((Edit(ByteSpan(target.declaration.start, target.declaration.start), helpers),
                      Edit(target.replacement, GeneratedSource(prefix + invocation.data + b'\n'))))
        imports = merge_imports(imports, tuple(proof.get('imports', ())))
    if not edits:
        return
    source = apply_edits(SourceFile(selection.selected.request.source, selection.source), tuple(edits))
    source = add_imports(source, imports, available=available,
                         current_module=selection.selected.request.source[:-5].replace('/', '.'))
    # Recover the original parent offset before candidate import/definition edits.
    packet.update(source=source.data.decode(), imports=list(imports),
                  offset=parent.declaration.start + len(source.data) - len(selection.source))


def load(reader, reference, *, closed=True):
    value = codec.decode(read(reader, reference))
    ev.certificate(value, value['request'])
    if value['format'] != ev.PROOF or closed and 'assumptions' in value:
        raise ValueError('Expected a closed source proof')
    return value


def named(proof):
    root = proof['declarations'][-1]
    digest = sha256(codec.encode([{k: v for k, v in root.items() if k != 'name'},
                                 proof.get('imports', []), proof.get('assumptions', [])])).hexdigest()
    name = ['str', ev.ROOT, 's' + digest]
    return [*proof['declarations'][:-1], dict(root, name=name)], name


def application(name, universes):
    return '@' + lean_name(name, absolute=True) + (
        '.{' + ', '.join(lean_name(u) for u in universes) + '}' if universes else '')


def compose(parent, children, dependencies):
    """Share named declarations, discharging each exact dependency once.

    Signatures remain binary DAGs; neither proof bodies nor transitive graphs are
    expanded into nested JSON. Both frozen peers and polymorphic lemmas retain
    the original checker's application rules.
    """
    if len(children) != len(dependencies) or not children:
        raise ValueError('Assembly requires every logical dependency')
    output, seen = [], {}
    imports = tuple(parent.get('imports', ()))

    def append(row):
        key = name_key(row['name'])
        if key in seen:
            if not same(seen[key], row):
                raise ValueError('Conflicting shared proof declaration')
        else:
            if len(output) >= 4096:
                raise ValueError('Assembly exceeds declaration limit')
            if len(row['source'].encode()) > 1024**2:
                raise ValueError('Assembly proof source exceeds byte limit')
            seen[key] = row
            output.append(row)

    resolved = []
    for child, dependency in zip(children, dependencies):
        if (child['format'] != ev.PROOF or 'assumptions' in child
                or not same(child['signature'], dependency['interface']['signature'])):
            raise ValueError('Assembly child differs from checked interface')
        rows, root = named(child)
        resolved.append((child, rows, root))
        imports = merge_imports(imports, tuple(child.get('imports', ())))
    if 'assumptions' in parent:
        expected = [{'parameter': d['parameter'], 'signature': d['interface']['signature']}
                    for d in dependencies]
        if (any(d['origin'] != 'abstracted_lemma' for d in dependencies)
                or not same(parent['assumptions'], expected)):
            raise ValueError('Conditional assumption manifest differs')
        original = parent['declarations'][0]
        material = dict(original, requires=[root for _, _, root in resolved])
        digest = sha256(codec.encode([material, expected, list(imports)])).hexdigest()
        root = ['str', ev.ROOT, 's' + digest]
        namespace = ['str', ev.ROOT, 'dependencies' + digest]
        aliases = []
        for dependency, (child, rows, child_root) in zip(dependencies, resolved):
            for row in rows:
                append(row)
            parts, parameter = [], dependency['parameter']
            while parameter != ['anonymous']:
                parts.append((parameter[0], parameter[2]))
                parameter = parameter[1]
            alias = namespace
            for tag, part in reversed(parts):
                alias = [tag, alias, part]
            append(dict(name=alias, signature=child['signature'],
                source=application(child_root, child['signature']['universes']), requires=[child_root]))
            aliases.append(alias)
        append(dict(original, name=root, requires=aliases,
            source='open ' + lean_name(namespace, absolute=True) + ' in\n' + original['source']))
        value, requires = application(root, parent['signature']['universes']), [root]
    else:
        rows, root = named(parent)
        for row in rows:
            append(row)
        value, requires = application(root, parent['signature']['universes']), [root]
        universe_keys = {codec.encode(u) for u in parent['signature']['universes']}
        for child, rows, child_root in resolved:
            if any(codec.encode(u) not in universe_keys for u in child['signature']['universes']):
                raise ValueError('Assembly dependency introduces unbound universes')
            for row in rows:
                append(row)
            value += ' (' + application(child_root, child['signature']['universes']) + ')'
            requires.append(child_root)
    append(dict(name=ev.ROOT, signature=parent['signature'], source=value, requires=requires))
    result = {k: v for k, v in parent.items() if k != 'assumptions'}
    result.update(declarations=output, imports=list(imports))
    # Inputs have already passed certificate admission. The compiler boundary
    # validates the bounded, generated certificate again before kernel replay.
    return result


def roots(selection, graph):
    if graph.roots != tuple(dict.fromkeys(i.id for i in selection.identities)):
        raise ValueError('Export roots differ from frozen selection')
    nodes = {n.owner.obligation_id: n for n in graph.nodes}
    result = tuple(nodes[i.id] for i in selection.identities)
    if any(n.environment_id != selection.environment_id or n.state != ProofState.VERIFIED for n in result):
        raise ValueError('Export requires current verified roots')
    return tuple(n.proof for n in result)


def snapshot(selection, graph):
    """Flat, bounded metadata for exactly the nodes consumed by partial rendering."""
    if graph.roots != tuple(dict.fromkeys(i.id for i in selection.identities)):
        raise ValueError('Export roots differ from frozen selection')
    nodes = {n.owner.obligation_id: n for n in graph.nodes}
    pending, seen, result = list(reversed(graph.roots)), set(), []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        node = nodes[key]
        if node.environment_id != selection.environment_id:
            raise ValueError('Export node environment differs')
        proof = node.proof
        if node.state == ProofState.CONDITIONAL:
            proof = ProofReference(node.owner, node.selected.receipt, node.selected.evidence)
            pending.extend(d.owner.obligation_id for d in reversed(node.selected.candidate.logical_dependencies))
        result.append(NodeSnapshot(node.owner, node.interface, node.state, proof))
        if len(result) > 10000:
            raise ValueError('Partial export exceeds node limit')
    return tuple(result)


def _partial_sources(selection, graph, reader):
    """Compose bottom-up once per node; retain shared declarations, never proof trees.

    The temporary source records containing sorry are rendering inputs only. They
    never enter proof admission or change the graph's verification state.
    """
    nodes = {n.owner.obligation_id: n for n in graph.nodes}
    resolved, interfaces, emitted = {}, {}, {}
    for identity in selection.identities:
        pending, fresh = [(identity.id, False)], []
        while pending:
            key, ready = pending.pop()
            if key in resolved:
                continue
            node = nodes[key]
            deps = node.selected.candidate.logical_dependencies if node.state == ProofState.CONDITIONAL else ()
            if deps and not ready:
                pending.append((key, True))
                pending.extend((d.owner.obligation_id, False) for d in reversed(deps))
                continue
            interface = codec.decode(read(reader, node.interface))
            codec.fields(interface, ('constant', 'signature'))
            interfaces[key] = interface
            if node.state == ProofState.UNRESOLVED:
                proof = {'format': ev.PROOF, 'request': '0'*64, 'purpose': 'proof',
                    'signature': interface['signature'], 'declarations': [dict(name=ev.ROOT,
                    signature=interface['signature'], source='sorry', requires=[])]}
            else:
                reference = node.proof if node.state == ProofState.VERIFIED else node.selected
                proof = load(reader, reference.evidence, closed=node.state == ProofState.VERIFIED)
                if not same(proof['signature'], interface['signature']):
                    raise ValueError('Export proof differs from node interface')
                if deps:
                    dependencies = [{'parameter': parse_identifier(d.alias),
                        'origin': d.origin.value, 'interface': interfaces[d.owner.obligation_id]} for d in deps]
                    proof = compose(proof, [resolved[d.owner.obligation_id] for d in deps], dependencies)
            rows, _ = named(proof)
            for row in rows:
                name = name_key(row['name'])
                if name in emitted:
                    if not same(emitted[name], row):
                        raise ValueError('Conflicting partial proof declaration')
                else:
                    emitted[name] = row
                    fresh.append(row)
            # Parents reference these already-emitted helpers via this small root.
            resolved[key] = dict(proof, declarations=[proof['declarations'][-1]])
        proof = resolved[identity.id]
        _, root = named(proof)
        yield proof, identity.id, fresh, root


def render_partial(selection, graph, reader, *, available=frozenset(), maximum=64*1024**2):
    return _render(selection, _partial_sources(selection, graph, reader), available=available, maximum=maximum)


def render(selection, references, reader, *, available=frozenset(), maximum=64*1024**2):
    if selection.selected.remaining_unfinished:
        raise ValueError('Cannot integrate unselected unfinished declarations')
    if len(references) != len(selection.identities):
        raise ValueError('Integration root count differs')
    def sources():
        for identity, reference in zip(selection.identities, references):
            if reference.owner.obligation_id != identity.id:
                raise ValueError('Integration root owner differs')
            proof = load(reader, reference.evidence)
            rows, root = named(proof)
            yield proof, reference.evidence.id, rows, root
    return _render(selection, sources(), available=available, maximum=maximum)


def _render(selection, sources, *, available, maximum):
    replacements, imports, emitted, contexts = [], (), {}, {}
    signatures = {}
    def closed(value):
        found = signatures.get(id(value))
        if found is None:
            found = (value, ev.closed(value))
            signatures[id(value)] = found
        return found[1]
    for target, identity, (proof, path, rows, root) in zip(selection.selected.targets, selection.identities, sources):
        root_signature = Signature(*(codec.encode(proof['signature'][key]) for key in ('target', 'universes', 'context')))
        if any(getattr(root_signature, key) != getattr(identity, key) for key in ('target', 'universes', 'context')):
            raise ValueError('Integration proof signature differs from selected target')
        signatures[id(proof['signature'])] = (proof['signature'], root_signature)
        fresh = []
        for row in rows:
            key = codec.encode(row['name'])
            if key in emitted:
                if not same(emitted[key], row):
                    raise ValueError('Conflicting integrated proof declaration')
                continue
            emitted[key] = row
            fresh.append(CheckedDeclaration(key, closed(row['signature']),
                SourceFile(path, row['source'].encode()),
                tuple(dict.fromkeys(map(codec.encode, row['requires'])))))
        policy = RenderPolicy(computable=target.kind == 'def' and b'noncomputable' not in
            selection.source[target.declaration.start:target.replacement.start])
        helpers = declarations(tuple(fresh), external=frozenset(emitted), policy=policy,
                               maximum=maximum, emitted=contexts)
        invocation = proof_application(codec.encode(root), closed(proof['signature']), policy=policy,
                                       maximum=maximum)
        replacements.append(RootReplacement(target.selector, invocation, helpers))
        imports = merge_imports(imports, tuple(proof.get('imports', ())))
    return integrate(selection, tuple(replacements), imports=imports, available=available, maximum=maximum)
