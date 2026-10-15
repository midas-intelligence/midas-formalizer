"""Pure Python source contracts. This suite does not invoke Lean or bubblewrap."""

import ast
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from midas.domain import ByteSpan, Signature
from midas.domain import encoding as graph
from midas.domain.identity import make_context
from midas.source import (
    CheckedAlias, CheckedDeclaration, Context, Edit, GeneratedSource, Module,
    RenderPolicy, RootReplacement, SourceFile, SourceMapEntry, add_imports,
    apply_edits, candidate, code_only, declaration_context, declaration_names,
    declarations, generated_module, integrate, lean_name, level, lookup_context,
    merge_imports, module_tree, parse_identifier, present_subject, probe,
    proof_application, render_context, string_literal, term,
)
from midas.source.text import Builder
from tests.test_domain_contracts import name, selected_problem


def signature(target=None, *, universes=None, context=None):
    return Signature(*(graph.encode(value) for value in (
        target if target is not None else ['const', name('True'), []],
        universes if universes is not None else [],
        context if context is not None else ['closed-telescope'])))


def context(sig=None):
    return Context(SourceFile('Main.lean', b''), 0, sig or signature())


class NameTests(unittest.TestCase):
    def test_structural_names_and_private_aliases(self):
        self.assertEqual(lean_name(name('match'), readable=True), '«match»')
        self.assertEqual(lean_name(name('α'), absolute=True, readable=True), '_root_.α')
        private = ['str', ['num', ['str', name('_private'), 'Main'], 42], 'visible']
        self.assertEqual(lean_name(private), '«visible»')
        with self.assertRaises(ValueError):
            lean_name(['num', name('A'), 3])
        with self.assertRaises(ValueError):
            lean_name(name('bad\nname'))

    def test_exact_identifier_lookup(self):
        expected = ['str', name('A'), 'x.y']
        self.assertEqual(parse_identifier('_root_.A.«x.y»'), expected)
        for invalid in ('A.', 'A..B', '«a»b', '«', 'x + y', 'a\nb', '«»', 'by'):
            self.assertIsNone(parse_identifier(invalid), invalid)

    def test_lexical_hints_ignore_comments_strings_and_private(self):
        text = ('namespace A\n/- namespace Fake /- nested -/ -/\n'
                'def text := "namespace Wrong"\nsection\n@[simp]\n'
                'theorem «x.y» : True := by trivial\nend\n'
                'private theorem hidden : True := by trivial\nend A\n'
                'theorem _root_.visible : True := by trivial\n')
        hints = declaration_names(text)
        self.assertEqual(list(hints.values()), ['A.text', 'A.«x.y»', 'visible'])
        self.assertEqual(code_only(text).count('\n'), text.count('\n'))


class TextTests(unittest.TestCase):
    def test_unicode_indentation_and_generated_scaffolding(self):
        source = SourceFile('proof', 'by\n  exact α\n'.encode())
        writer = Builder()
        writer.append(b'example : True :=\n')
        writer.indented(source, 4)
        writer.append(b'-- generated\n')
        result = writer.finish()
        offset = result.data.index('α'.encode())
        mapped = result.authored_location(offset)
        self.assertEqual((mapped.path, mapped.line, mapped.column), ('proof', 2, 8))
        self.assertIsNone(result.authored_location(0))
        self.assertIsNone(result.authored_location(result.data.index(b'    ')))
        self.assertIsNone(result.authored_location(result.data.index(b'--')))
        with self.assertRaises(ValueError):
            result.authored_location(offset + 1)

    def test_byte_splicing_maps_only_authored_segments(self):
        original = SourceFile('Main.lean', 'aαbc\n'.encode())
        proof = SourceFile('proof', b'XYZ')
        writer = Builder()
        writer.authored(proof)
        result = apply_edits(original, (Edit(ByteSpan(3, 4), writer.finish()),))
        self.assertEqual(result.data, 'aαXYZc\n'.encode())
        self.assertEqual(result.authored_location(4).path, 'proof')
        self.assertEqual(result.authored_location(6).byte_offset, 4)
        with self.assertRaises(ValueError):
            apply_edits(original, (Edit(ByteSpan(2, 3), GeneratedSource(b'')),))

    def test_reject_overlap_forged_mapping_and_limits(self):
        source = SourceFile('x', b'abcdef')
        with self.assertRaises(ValueError):
            apply_edits(source, (Edit(ByteSpan(1, 4), GeneratedSource(b'x')),
                                 Edit(ByteSpan(3, 5), GeneratedSource(b'y'))))
        with self.assertRaises(ValueError):
            GeneratedSource(b'xxx', (SourceMapEntry(ByteSpan(0, 3), source, ByteSpan(0, 3)),))
        with self.assertRaises(ValueError):
            apply_edits(source, (), maximum=5)

    def test_coordinate_units_and_line_endings(self):
        source = SourceFile('x', 'α\r\nβ\n'.encode())
        self.assertEqual(source.offset(1, 1), 2)
        self.assertEqual(source.offset(2, 1), 6)
        self.assertEqual(source.location(4).line, 2)
        self.assertIsNone(source.offset(2, 2))
        self.assertIsNone(source.offset(True, 0))


class TermTests(unittest.TestCase):
    def test_binders_shadowing_applications_and_levels(self):
        n = name('x')
        expr = ['lam', n, 'explicit', ['sort', ['zero']],
                ['lam', n, 'implicit', ['sort', ['zero']], ['bvar', 1]]]
        output = term(graph.encode(expr), policy=RenderPolicy(readable=True)).decode()
        self.assertEqual(output, '(fun (x : (Sort 0)) => (fun {x_1 : (Sort 0)} => x))')
        app = ['app', ['app', ['const', name('f'), []], ['nat', 1]], ['nat', 2]]
        self.assertEqual(term(graph.encode(app)), b'(@_root_.\xc2\xabf\xc2\xbb (nat_lit 1) (nat_lit 2))')
        self.assertEqual(level(graph.encode(['max', ['zero'], ['succ', ['zero']]])), b'(max 0 (0 + 1))')

    def test_let_projection_and_invalid_bound_variable(self):
        expr = ['let', name('x'), ['const', name('Nat'), []], ['nat', 2], ['bvar', 0], False]
        self.assertIn(b'; _midas0)', term(graph.encode(expr)))
        self.assertTrue(term(graph.encode(['proj', name('Pair'), 1, ['const', name('p'), []]])).endswith(b').2'))
        with self.assertRaises(ValueError):
            term(graph.encode(['bvar', 0]))

    def test_large_strings_never_call_json(self):
        value = 'α\\"\n\x00' * 100000
        payload = graph.encode(['string', value])
        with patch('json.dumps', side_effect=AssertionError('JSON serialization')), patch('json.loads', side_effect=AssertionError('JSON parsing')):
            output = term(payload)
            self.assertEqual(output, string_literal(value).encode())
            self.assertIn(b'\\x00', output)
        with self.assertRaises(ValueError):
            string_literal('\x00' * 100, maximum=200)

    def test_shared_exponential_graph_fails_before_expansion(self):
        expr = ['nat', 0]
        for _ in range(50):
            expr = ['app', ['app', ['const', name('f'), []], expr], expr]
        payload = graph.encode(expr)
        self.assertLess(len(payload), 4000)
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            term(payload, maximum=4096)

    def test_local_context_is_closed_with_instance_binders(self):
        ctx = ['local-context', [['local', name('n'), 'explicit', 'default', ['const', name('Nat'), []]]], []]
        sig = signature(['bvar', 0], context=ctx)
        self.assertEqual(present_subject(sig), '(∀ (n : @_root_.Nat), n)'.encode())

    def test_flat_local_context_does_not_build_a_deep_tree(self):
        ctx = ['local-context', [['local', name('x' + str(i)), 'explicit', 'default',
                                 ['const', name('Nat'), []]] for i in range(1100)], []]
        rendered = present_subject(signature(['const', name('True'), []], context=ctx))
        self.assertEqual(rendered.count('∀'.encode()), 1100)

    def test_local_let_and_instance_context(self):
        ctx = ['local-context', [
            ['local', name('n'), 'explicit', 'default', ['const', name('Nat'), []]],
            ['local-let', name('m'), 'default', ['const', name('Nat'), []], ['bvar', 0], False]], []]
        self.assertIn(b'(let m : @_root_.Nat := n; m)', present_subject(signature(['bvar', 0], context=ctx)))
        ctx = ['local-context', [['local', name('i'), 'explicit', 'default', ['const', name('True'), []]]],
               [['local-instance', name('i'), ['bvar', 0]]]]
        self.assertIn(b'[i :', present_subject(signature(context=ctx)))


class ContextTests(unittest.TestCase):
    def test_lookup_does_not_decode_any_type_or_identity(self):
        _, _, _, selection = selected_problem()
        aliases = (CheckedAlias('wanted', signature()), CheckedAlias('other', signature(['nat', 123])))
        with patch.object(graph, 'decode', side_effect=AssertionError('Type expansion')):
            lookup = lookup_context(selection, 1, ('wanted', 'wanted', 'x + y'), aliases=aliases)
            declaration = declaration_context(selection, 0)
        self.assertEqual(lookup.aliases, aliases[:1])
        self.assertIs(lookup.source.data, selection.source)
        self.assertEqual(lookup.prefix_end, selection.selected.targets[1].declaration.start)
        self.assertIs(declaration.signature.target, selection.identities[0].target)

    def test_context_definitions_use_raw_text_and_prefix_names(self):
        definitions = [{'name': 'match', 'type': 'Nat', 'value': '0'},
                       {'name': 'b', 'type': 'Nat', 'value': 'match'}]
        ctx = make_context('scope', definitions)
        with patch('json.dumps', side_effect=AssertionError('JSON')):
            result = render_context(graph.encode(ctx))
        self.assertIn('«match»'.encode(), result.data)
        self.assertIn(('open ' + ctx[2][0]['namespace'] + ' in').encode(), result.data)
        self.assertEqual(result.authored_location(result.data.index(b'Nat')).path, ctx[2][0]['namespace'] + '/type')

    def test_import_header_and_maps(self):
        source = SourceFile('Main.lean', b'/- header /- nested -/ -/\nmodule\n-- note\ntheorem t : True := by trivial\n')
        writer = Builder()
        writer.authored(source)
        result = add_imports(writer.finish(), ('Std',), available=frozenset({'Std'}))
        self.assertIn(b'module\nimport Std\n', result.data)
        self.assertIsNone(result.authored_location(result.data.index(b'import')))
        self.assertEqual(result.authored_location(result.data.index(b'theorem')).line, 4)
        for modules, available, current in [(('Unknown',), frozenset(), None), (('Main',), frozenset({'Main'}), 'Main'),
                                             (('MidasCheck',), frozenset({'MidasCheck'}), None)]:
            with self.assertRaises(ValueError):
                add_imports(writer.finish(), modules, available=available, current_module=current)

    def test_import_deduplication_and_unclosed_comment(self):
        self.assertEqual(merge_imports(('Std',), ('Std', 'Mathlib')), ('Std', 'Mathlib'))
        with self.assertRaises(ValueError):
            add_imports(GeneratedSource(b'/- unclosed'), ('Std',), available=frozenset({'Std'}))

    def test_context_cache_is_bounded_and_does_not_skip_changed_inputs(self):
        from midas.source.context import _CONTEXT_SOURCES
        _CONTEXT_SOURCES.reset()
        ctx = make_context('scope', [{'name': 'x', 'type': 'Nat', 'value': '1'}])
        first = render_context(graph.encode(ctx))
        with patch.object(graph, 'decode', side_effect=AssertionError('Repeated expansion')):
            self.assertIs(render_context(graph.encode(ctx)), first)
        ctx[2][0]['value'] = '2'
        with self.assertRaises(ValueError):
            render_context(graph.encode(ctx))
        with self.assertRaises(ValueError):
            render_context(graph.encode(make_context('scope', [])), maximum=-1)
        empty = graph.encode(make_context('scope', []))
        render_context(empty, maximum=1)
        with self.assertRaises(ValueError):
            render_context(empty, maximum=True)

    def test_context_cache_accounts_for_lazy_line_indices_and_evicts(self):
        from midas.source.context import _ContextSources
        value = render_context(graph.encode(make_context('scope', [{'name': 'x', 'type': 'Nat', 'value': '1'}])))
        cache = _ContextSources(max_entries=1, max_bytes=100000)
        cache.get((b'a', 1), lambda: value)
        cache.get((b'b', 1), lambda: value)
        self.assertEqual(tuple(cache.entries), ((b'b', 1),))
        self.assertLessEqual(cache.bytes, cache.max_bytes)
        cache.reset()
        self.assertEqual(cache.bytes, 0)
        tiny = _ContextSources(max_bytes=1)
        self.assertIs(tiny.get((b'a', 1), lambda: value), value)
        self.assertFalse(tiny.entries)


class RenderTests(unittest.TestCase):
    def test_candidate_and_probe_share_rendering_and_authored_maps(self):
        proof = SourceFile('proof', b'by\n  trivial')
        statement = SourceFile('statement', b'True')
        rendered = candidate(context(), proof, statement=statement)
        self.assertEqual(rendered, probe(context(), proof, statement=statement))
        self.assertEqual(rendered.authored_location(rendered.data.index(b'trivial')).path, 'proof')
        self.assertEqual(rendered.authored_location(rendered.data.index(b'True')).path, 'statement')
        self.assertIsNone(rendered.authored_location(rendered.data.index(b'example')))

    def test_alias_binding_and_shadow_rejection(self):
        ctx = replace(context(), aliases=(CheckedAlias('h', signature()),))
        self.assertIn('(«h» :'.encode(), candidate(ctx, SourceFile('proof', b'by exact h')).data)
        sig = signature(['forall', name('h'), 'explicit', ['const', name('True'), []], ['const', name('True'), []]])
        with self.assertRaisesRegex(ValueError, 'collides'):
            candidate(replace(ctx, signature=sig), SourceFile('proof', b'by trivial'))

    def test_candidate_rejects_self_import_without_a_separate_module_argument(self):
        with self.assertRaisesRegex(ValueError, 'self-importing'):
            candidate(context(), SourceFile('proof', b'by trivial'), imports=('Main',), available=frozenset({'Main'}))

    def test_alias_can_use_an_inherited_definition_prefix(self):
        definitions = [{'name': 'x', 'type': 'Nat', 'value': '1'}, {'name': 'y', 'type': 'Nat', 'value': 'x'}]
        parent = signature(context=make_context('scope', definitions))
        inherited = signature(context=make_context('scope', definitions[:1]))
        ctx = replace(context(parent), aliases=(CheckedAlias('h', inherited),))
        self.assertIn('(«h» :'.encode(), candidate(ctx, SourceFile('proof', b'by exact h')).data)
        unrelated = signature(context=make_context('different-scope', definitions[:1]))
        with self.assertRaises(ValueError):
            candidate(replace(ctx, aliases=(CheckedAlias('h', unrelated),)), SourceFile('proof', b'by exact h'))

    def test_repeated_signatures_are_expanded_once_per_bundle(self):
        sig = signature()
        values = tuple(CheckedDeclaration(graph.encode(name('p' + str(i))), sig, SourceFile('p', b'by trivial'))
                       for i in range(20))
        from midas.source import render
        with patch.object(render, '_signature', wraps=render._signature) as decode:
            declarations(values)
        self.assertEqual(decode.call_count, 1)

    def test_shared_declarations_are_emitted_once_in_dependency_order(self):
        child_name, parent_name = graph.encode(name('child')), graph.encode(name('parent'))
        child = CheckedDeclaration(child_name, signature(), SourceFile('child-proof', b'by trivial'))
        parent = CheckedDeclaration(parent_name, signature(), SourceFile('parent-proof', b'by exact child'), (child_name,))
        result = declarations((parent, child, child))
        self.assertEqual(result.data.count('def _root_.«child»'.encode()), 1)
        self.assertLess(result.data.index('«child»'.encode()), result.data.index('«parent»'.encode()))
        with self.assertRaises(ValueError):
            declarations((parent,))
        with self.assertRaises(ValueError):
            declarations((child, replace(child, proof=SourceFile('other', b'by assumption'))))
        with self.assertRaises(ValueError):
            declarations((parent, replace(child, requires=(parent_name,))))

    def test_type_dependencies_and_definition_contexts_precede_proofs(self):
        ctx = make_context('scope', [{'name': 'x', 'type': 'Nat', 'value': '1'}])
        a_name, b_name = graph.encode(name('a')), graph.encode(name('b'))
        first = CheckedDeclaration(a_name, signature(['sort', ['zero']], context=ctx), SourceFile('a', b'True'))
        second = CheckedDeclaration(b_name, signature(['const', name('a'), []], context=ctx), SourceFile('b', b'by trivial'))
        result = declarations((second, first))
        self.assertEqual(result.data.count(b'noncomputable def _root_.MidasContext_'), 1)
        self.assertLess(result.data.index('«a»'.encode()), result.data.index('«b»'.encode()))

    def test_integration_preserves_unselected_source_and_exact_roots(self):
        original, _, _, selection = selected_problem()
        roots = (RootReplacement('property', GeneratedSource(b'by rfl')),
                 RootReplacement('witness', GeneratedSource(b'0')))
        result = integrate(selection, roots)
        self.assertNotIn(b'sorry', result.data)
        self.assertIn(b'theorem property : witness = witness := by rfl', result.data)
        self.assertEqual(result.authored_location(result.data.index(b'theorem')).byte_offset, original.index(b'theorem'))
        with self.assertRaises(ValueError):
            integrate(selection, roots[:1])
        with self.assertRaises(ValueError):
            integrate(selection, roots + roots[:1])

    def test_proof_application_uses_telescope(self):
        sig = signature(['forall', name('x'), 'explicit', ['const', name('Nat'), []], ['const', name('True'), []]])
        result = proof_application(graph.encode(name('result')), sig)
        self.assertEqual(result.data.count(b'| exact @'), 2)
        self.assertIn('exact «x»'.encode(), result.data)


class ModuleTests(unittest.TestCase):
    def test_flat_shared_dag_modules_and_order(self):
        empty = GeneratedSource(b'')
        values = (Module('Main', empty, ('Left', 'Right')), Module('Left', empty, ('Leaf',)),
                  Module('Right', empty, ('Leaf',)), Module('Leaf', empty, ('Std',)))
        tree = module_tree(values, 'Main', available=frozenset({'Std'}))
        self.assertEqual(tree.build_order, ('Leaf', 'Left', 'Right', 'Main'))
        self.assertEqual(len(tree.files), 4)
        self.assertTrue(all('/' not in path for path in tree.files))
        with self.assertRaises(TypeError):
            tree.files['new'] = empty
        self.assertNotIn('.', generated_module('some/very/deep/proof'))
        with self.assertRaises(ValueError):
            Module('Deep.Path', empty)

    def test_module_header_precedes_imports(self):
        tree = module_tree((Module('Main', GeneratedSource(b'module\n'), ('Std',)),),
                           'Main', available=frozenset({'Std'}))
        self.assertTrue(tree.files['Main.lean'].data.startswith(b'module\nimport Std\n'))

    def test_invalid_module_graphs_and_total_limits(self):
        empty = GeneratedSource(b'')
        for values in ((Module('Main', empty, ('Main',)),),
                       (Module('Main', empty), Module('Unused', empty)),
                       (Module('Main', empty), Module('main', empty)),
                       (Module('Main', empty, ('Missing',)),)):
            with self.assertRaises(ValueError):
                module_tree(values, 'Main')
        with self.assertRaises(ValueError):
            module_tree((Module('Main', GeneratedSource(b'123456')),), 'Main', maximum=5)
        with self.assertRaises(ValueError):
            module_tree((Module('Main', empty),), 'Main', available=frozenset({'Main'}))

    def test_deep_proof_chain_does_not_create_deep_paths(self):
        values = tuple(Module('M' + str(i), GeneratedSource(b''), ('M' + str(i - 1),) if i else ())
                       for i in range(1200))
        tree = module_tree(values, 'M1199')
        self.assertEqual(len(tree.files), 1200)
        self.assertEqual(tree.build_order[0], 'M0')


class BoundaryTests(unittest.TestCase):
    def test_large_source_flow_has_no_json_operations(self):
        _, _, _, selection = selected_problem()
        large = SourceFile('proof', b'by\n' + b'  -- commentary\n' * 70000 + b'  trivial')
        with patch('json.dumps', side_effect=AssertionError('JSON serialization')), patch('json.loads', side_effect=AssertionError('JSON parsing')):
            generated = candidate(context(), large)
            self.assertGreater(len(generated.data), 1024**2)
            integrated = integrate(selection, (RootReplacement('witness', GeneratedSource(b'0')),
                                                RootReplacement('property', GeneratedSource(b'by rfl'))))
            tree = module_tree((Module('Main', integrated),), 'Main')
            self.assertIs(tree.files['Main.lean'], integrated)

    def test_source_imports_only_domain_and_standard_library(self):
        import sys
        directory = Path(__file__).parents[1] / 'midas' / 'source'
        for path in directory.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name.split('.')[0] in sys.stdlib_module_names for alias in node.names), path)
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 2:
                        self.assertTrue(node.module == 'domain' or node.module.startswith('domain.'), path)
                    elif node.level == 0:
                        self.assertIn(node.module.split('.')[0], sys.stdlib_module_names, path)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ('open', 'exec', 'eval'), path)


if __name__ == '__main__':
    unittest.main()
