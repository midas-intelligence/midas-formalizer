"""Checked source certificates at the source-rendering boundary."""
from hashlib import sha256

from ..domain import AdmittedCheck, ByteSpan, Signature
from ..domain import encoding as codec
from ..source import (CheckedDeclaration, Edit, GeneratedSource, RenderPolicy, SourceFile,
                      add_imports, apply_edits, declarations, lean_name, merge_imports, parse_identifier, proof_application)
from . import evidence as ev


def bind_definitions(selection, index, bindings, packet, *, available):
    """Replay accepted earlier definition sources in their original declaration scopes."""
    edits, imports, seen = [], tuple(packet['imports']), set()
    for dependency, child, certificate, admitted, interface in bindings:
        if (not isinstance(admitted, AdmittedCheck) or admitted.candidate.owner != dependency.owner
                or admitted.evidence != dependency.certificate
                or admitted.candidate.environment_id != selection.environment_id
                or admitted.candidate.joint_submission_id != dependency.submission_id
                or admitted.candidate.logical_dependencies):
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
        # A checker-produced closed proof has one source root. Composed exports
        # are rendered by export using the shared declarations renderer.
        if len(certificate['declarations']) != 1:
            raise ValueError('Expected one checked definition source root')
        declaration = certificate['declarations'][0]
        digest = sha256(codec.encode(certificate)).hexdigest()
        root = codec.encode(['str', ['anonymous'], 'MidasChecked_' + digest])
        sig = ev.closed(declaration['signature'])
        policy = RenderPolicy(computable=b'noncomputable' not in
            selection.source[target.declaration.start:target.replacement.start])
        helper = declarations((CheckedDeclaration(root, sig, SourceFile('definition-proof', declaration['source'].encode())),), policy=policy)
        proof = proof_application(root, sig, policy=policy)
        prefix = b':= ' if selection.source[target.replacement.start:target.replacement.end].lstrip().startswith(b':=') else b''
        edits.extend((Edit(ByteSpan(target.declaration.start, target.declaration.start), helper),
                      Edit(target.replacement, GeneratedSource(prefix + proof.data + b'\n'))))
        imports = merge_imports(imports, tuple(certificate.get('imports', [])))
        packet['candidate'] = ('let ' + lean_name(parse_identifier(dependency.alias)) + ' := ' +
                               lean_name(interface['constant'], absolute=True) + ';\n' + packet['candidate'])
    source = apply_edits(SourceFile(selection.selected.request.source, selection.source), tuple(edits))
    source = add_imports(source, imports, available=available,
                         current_module=selection.selected.request.source[:-5].replace('/', '.'))
    packet.update(source=source.data.decode(), imports=list(imports),
        offset=selection.selected.targets[index].declaration.start + len(source.data) - len(selection.source))
