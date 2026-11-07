"""Checked source certificates at the source-rendering boundary."""
from hashlib import sha256
import re

from ..domain import AdmittedCheck, ByteSpan, CheckKind, Signature
from ..domain import encoding as codec
from ..source import (CheckedDeclaration, Edit, GeneratedSource, RenderPolicy, SourceFile,
                      add_imports, apply_edits, code_only, declarations, lean_name, merge_imports, parse_identifier, proof_application)
from . import evidence as ev


def prefix_proof(proof, namespace):
    """Relocate reserved proof names while preserving sharing and authored strings."""
    def rename(name):
        parts, current = [], name
        while current != ev.ROOT and current != ['anonymous']:
            parts.append((current[0], current[2]))
            current = current[1]
        if current != ev.ROOT:
            return name
        result = ['str', ['anonymous'], namespace]
        for tag, part in reversed(parts):
            result = [tag, result, part]
        return result
    memo = {}
    def expression(value):
        pending = [(value, False)]
        while pending:
            item, done = pending.pop()
            if not isinstance(item, (list, dict)) or id(item) in memo:
                continue
            if not done:
                pending.append((item, True))
                pending.extend((v, False) for v in (item.values() if isinstance(item, dict) else item))
            elif isinstance(item, dict):
                memo[id(item)] = {k: memo.get(id(v), v) for k, v in item.items()}
            else:
                result = [memo.get(id(v), v) for v in item]
                if result and result[0] in ('const', 'proj'):
                    result[1] = rename(item[1])
                memo[id(item)] = result
        return memo.get(id(value), value)
    pattern = re.compile(r"(?<![\w'«])(?:«MidasCandidateResult»|MidasCandidateResult)(?![\w'»])")
    def source(text):
        parts, offset = [], 0
        for match in pattern.finditer(code_only(text)):
            parts.extend((text[offset:match.start()], '«' + namespace + '»'))
            offset = match.end()
        parts.append(text[offset:])
        return SourceFile(namespace, ''.join(parts).encode())
    result = []
    for row in proof['declarations']:
        result.append(CheckedDeclaration(codec.encode(rename(row['name'])), ev.closed(expression(row['signature'])),
            source(row['source']), tuple(codec.encode(rename(n)) for n in row['requires']),
            source(row['type_source']) if 'type_source' in row else None))
    return tuple(result), codec.encode(['str', ['anonymous'], namespace])


def bind_definitions(selection, index, bindings, packet, *, available):
    """Replay accepted earlier definition sources in their original declaration scopes."""
    edits, imports, seen = [], tuple(packet['imports']), set()
    for dependency, child, certificate, admitted, interface in bindings:
        if (not isinstance(admitted, AdmittedCheck) or admitted.candidate.owner != dependency.owner
                or admitted.evidence != dependency.certificate
                or admitted.candidate.environment_id != selection.environment_id
                or admitted.candidate.joint_submission_id != dependency.submission_id
                or admitted.candidate.logical_dependencies and admitted.kind != CheckKind.ASSEMBLY):
            raise ValueError('Definition source lacks matching closed proof admission')
        ev.certificate(certificate, certificate['request'])
        if certificate['format'] != ev.PROOF or 'assumptions' in certificate:
            raise ValueError('Definition requires a closed source certificate')
        expected = {k: codec.decode(getattr(child, k)) for k in ('target', 'universes', 'context')}
        if codec.encode(certificate['signature']) != codec.encode(expected):
            raise ValueError('Definition source has a different signature')
        constant = codec.canonical(interface['constant'])
        matches = [(i, target) for i, target in enumerate(selection.selected.targets) if target.constant == constant]
        if len(matches) != 1 or matches[0][0] >= index or matches[0][1].kind != 'def' or constant in seen:
            raise ValueError('Definition is not a distinct earlier selected definition')
        seen.add(constant)
        target = matches[0][1]
        if target.replacement is None:
            raise ValueError('Definition source is not replaceable')
        digest = sha256(codec.encode(certificate)).hexdigest()
        rows, root = prefix_proof(certificate, 'MidasChecked_' + digest)
        sig = ev.closed(certificate['signature'])
        policy = RenderPolicy(computable=b'noncomputable' not in
            selection.source[target.declaration.start:target.replacement.start])
        helper = declarations(rows, policy=policy)
        proof = proof_application(root, sig, policy=policy)
        prefix = b':= ' if selection.source[target.replacement.start:target.replacement.end].lstrip().startswith(b':=') else b''
        edits.extend((Edit(ByteSpan(target.declaration.start, target.declaration.start), helper),
                      Edit(target.replacement, GeneratedSource(prefix + proof.data + b'\n'))))
        imports = merge_imports(imports, tuple(certificate.get('imports', [])))
        if packet.get('candidate') is not None:
            packet['candidate'] = ('let ' + lean_name(parse_identifier(dependency.alias)) + ' := ' +
                                   lean_name(interface['constant'], absolute=True) + ';\n' + packet['candidate'])
    source = apply_edits(SourceFile(selection.selected.request.source, selection.source), tuple(edits))
    source = add_imports(source, imports, available=available,
                         current_module=selection.selected.request.source[:-5].replace('/', '.'))
    packet.update(source=source.data.decode(), imports=list(imports),
        offset=selection.selected.targets[index].declaration.start + len(source.data) - len(selection.source))
