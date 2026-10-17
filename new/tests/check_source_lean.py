"""Opt-in compiler smoke checks. Requires permission under the root AGENTS.md.

Run: python3 -B -m tests.check_source_lean /absolute/path/to/toolchain/bin/lean
The compiler runs in bubblewrap with no network and only a temporary writable root.
"""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from midas.domain import encoding as graph
from midas.domain.identity import make_context
from midas.source import (
    CheckedAlias, CheckedDeclaration, Context, GeneratedSource, Module, RenderPolicy,
    RootReplacement, SourceFile, candidate, declarations, integrate, lean_name, module_tree,
    parse_identifier, proof_application, term,
)
from tests.test_source import context, name, signature
from tests.test_domain_contracts import selected_problem


def main():
    compiler = str(Path(sys.argv[1]).resolve(strict=True))
    snippets = []
    for text in ('plain α', 'quote " slash \\ newline\n\t\r', '\x00\x01\x1f\x7f'):
        snippets.append(b'example : String := ' + term(graph.encode(['string', text])) + b'\n')
    expr = ['lam', name('n'), 'explicit', ['const', name('Nat'), []], ['bvar', 0]]
    snippets.append(b'example : Nat -> Nat := ' + term(graph.encode(expr)) + b'\n')
    snippets.append(candidate(context(), SourceFile('proof', b'by trivial')).data)
    alias_context = Context(SourceFile('aliases', b''), 0, signature(),
                            (CheckedAlias('h', signature()), CheckedAlias('n', signature(['const', name('Nat'), []]))))
    snippets.append(candidate(alias_context, SourceFile('proof', b'by exact h')).data)
    definitions = make_context('scope', [{'name': 'x', 'type': 'Nat', 'value': '1'},
                                         {'name': 'y', 'kind': 'abbrev', 'type': 'Nat', 'value': 'x + 1'}])
    sig = signature(['const', name('Nat'), []], context=definitions)
    snippets.append(candidate(context(sig), SourceFile('proof', b'by exact y')).data)
    nat = ['const', name('Nat'), []]
    local = ['local-context', [['local', name('n'), 'explicit', 'default', nat]], []]
    equality = ['app', ['app', ['app', ['const', name('Eq'), [['succ', ['zero']]]], nat], ['bvar', 0]], ['bvar', 0]]
    snippets.append(candidate(context(signature(equality, context=local)),
                              SourceFile('local-proof', b'by intro n; rfl')).data)
    universe = ['param', name('u')]
    polymorphic = signature(['forall', name('α'), 'explicit', ['sort', universe], ['const', name('True'), []]],
                            universes=[name('u')])
    snippets.append(candidate(context(polymorphic), SourceFile('polymorphic-proof', 'by intro α; trivial'.encode())).data)
    child_name, parent_name = graph.encode(name('child')), graph.encode(name('parent'))
    child = CheckedDeclaration(child_name, signature(), SourceFile('child', b'by trivial'))
    parent = CheckedDeclaration(parent_name, signature(), SourceFile('parent', b'by exact child'), (child_name,))
    snippets.append(declarations((parent, child)).data)
    # Closed signatures include both authored header locals and any binders
    # still inside the target. Exercise each boundary and renamed-local lookup.
    wrapper_sig = signature(['forall', name('n'), 'explicit', nat,
        ['forall', name('h'), 'explicit', ['const', name('True'), []], ['const', name('True'), []]]])
    wrapper_name = graph.encode(name('wrapperResult'))
    snippets.append(declarations((CheckedDeclaration(wrapper_name, wrapper_sig,
        SourceFile('wrapper', b'by intro n h; exact h')),)).data)
    wrapper = proof_application(wrapper_name, wrapper_sig).data
    for header in (b'example (n : Nat) (h : True) : True := ',
                   b'example (n : Nat) : True -> True := ',
                   b'example : Nat -> True -> True := ',
                   b'example (renamed : Nat) (witness : True) : True := '):
        snippets.append(header + wrapper)
    _, _, _, selection = selected_problem()
    integrated = integrate(selection, (RootReplacement('witness', GeneratedSource(b'0')),
                                       RootReplacement('property', GeneratedSource(b'by rfl'))))
    snippets.append(integrated.data)
    modules = (Module('Leaf', GeneratedSource(b'def leaf : Nat := 1\n')),
               Module('Branch', GeneratedSource(b'def branch : Nat := leaf\n'), ('Leaf',)),
               Module('Main', GeneratedSource(b'example : branch = 1 := rfl\n'), ('Branch',)))
    tree = module_tree(modules, 'Main')
    with TemporaryDirectory(prefix='midas-source-lean-') as directory:
        root = Path(directory)
        identifiers = ['namespace IdentifierRegression']
        for component in ('ne₁₂_of_not_collinear', "x₀₉ₐₜᵢⱼ'", 'getElem!', 'find?',
                          'αβ', 'Ω', 'ℝ', '𝒜', 'é', 'Ā'):
            parsed = parse_identifier(component)
            assert parsed is not None, component
            spelling = lean_name(parsed, readable=True)
            assert spelling == component, spelling
            identifiers.extend((f'def {spelling} : Nat := 0', f'example : {spelling} = 0 := rfl'))
        identifiers.append('end IdentifierRegression')
        (root / 'Identifiers.lean').write_text('\n'.join(identifiers) + '\n')
        (root / 'Examples.lean').write_bytes(b'\n'.join(snippets))
        scoped = Context(SourceFile('Scoped.lean', b'namespace Scoped\nsection\n'), 25, signature())
        (root / 'Scoped.lean').write_bytes(candidate(scoped, SourceFile('proof', b'by trivial')).data)
        header = module_tree((Module('Header', GeneratedSource(b'module\nexample : True := by trivial\n'), ('Std',)),),
                             'Header', available=frozenset({'Std'}))
        (root / 'Header.lean').write_bytes(header.files['Header.lean'].data)
        for path, source in tree.files.items():
            (root / path).write_bytes(source.data)
        command = ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
                   '--dev', '/dev', '--proc', '/proc', '--bind', directory, directory,
                   '--chdir', directory, '--setenv', 'LEAN_PATH', directory, compiler]
        for module in ('Identifiers', 'Examples', 'Scoped', 'Header', *tree.build_order):
            result = subprocess.run([*command, '-o', module + '.olean', module + '.lean'],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                print(result.stdout + result.stderr)
                raise SystemExit(result.returncode)
            print(module + ': compiled')


if __name__ == '__main__':
    main()
