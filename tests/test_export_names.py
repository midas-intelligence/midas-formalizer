"""Readable naming without changing checked identities or authored fragments."""
import unittest

from midas.export.names import ExportNames
from midas.source import SourceFile, lean_name, parse_identifier
from midas.source.text import Builder
from midas.domain import ByteSpan


def result(letter):
    return 'MidasCandidateResult.s' + letter*64


def dependencies(letter):
    return 'MidasCandidateResult.dependencies' + letter*64


def declaration(name, source='by trivial', requires=()):
    return dict(name=parse_identifier(name), source=source,
                requires=[parse_identifier(n) for n in requires], signature={'universes': []})


def generated(text):
    writer = Builder()
    writer.authored(SourceFile('authored', text.encode()))
    return writer.finish()


def section_sources():
    """Repeated sibling contexts leave both empty and occupied export sections."""
    from midas.domain import encoding as codec
    from midas.domain.identity import make_context
    from midas.source import CheckedDeclaration, declarations
    from tests.test_source import signature

    body = b'by\n  /- section\nend -/\n  let marker := "section\\nend"\n  exact y'
    rows, checked = [], []
    for letter in 'abc':
        row = declaration(result(letter), body.decode())
        rows.append(row)
        context = make_context(letter, [{'name': 'x', 'type': 'Nat', 'value': '1'},
                                        {'name': 'y', 'type': 'Nat', 'value': 'x + 1'}])
        checked.append(CheckedDeclaration(codec.encode(row['name']),
            signature(['const', parse_identifier('Nat'), []], context=context), SourceFile(letter, body)))
    spans, sections, contexts = [], [], {}
    source = declarations(tuple(checked), spans=spans, sections=sections, emitted=contexts)
    aliases = [[parse_identifier('Midas.' + old), parse_identifier('Midas.' + new)]
               for old, new in (('x_2', 'x'), ('y_2', 'y'), ('x_3', 'x'), ('y_3', 'y'),
                                ('proof_2', 'proof'), ('answer', 'proof'))]

    def names(equivalences):
        value = ExportNames(b'', equivalences)
        value.add(rows, rows[-1]['name'], 'answer')
        value.contexts(contexts.values())
        return value

    original = names(()).rewrite(source, maximum=10000)
    reduced = names(aliases).rewrite(source, maximum=10000, spans=spans, sections=sections)
    # In a later batch every declaration can already have a representative.
    aliases = [[old, parse_identifier('Midas.previous_' + lean_name(new, readable=True).removeprefix('Midas.'))]
               for old, new in aliases]
    aliases += [[parse_identifier('Midas.' + old), parse_identifier('Midas.previous_' + old)]
                for old in ('x', 'y', 'proof')]
    empty = names(aliases).rewrite(source, maximum=10000, spans=spans, sections=sections)
    return original, reduced, empty


class ReadableNames(unittest.TestCase):
    def test_dedup_removes_empty_sections_and_preserves_live_scopes_and_maps(self):
        original, reduced, empty = section_sources()
        # Six generated sections become two; section-like proof text is intact.
        self.assertEqual(original.data.count(b'\nsection\n'), 6)
        self.assertEqual(reduced.data.count(b'\nsection\n'), 2)
        self.assertNotRegex(reduced.data, rb'(?m)^section\n(?:open [^\n]*\n|\n)*end\n')
        self.assertIn(b'/- section\nend -/', reduced.data)
        self.assertIn(b'"section\\nend"', reduced.data)
        self.assertIn(b'open _root_.Midas (y)\n', reduced.data)
        self.assertEqual(reduced.authored_location(reduced.data.index(b'exact y')).path, 'a')
        self.assertEqual(reduced.authored_location(reduced.data.index(b'exact y')).byte_offset,
                         original.authored_location(original.data.index(b'exact y')).byte_offset)
        self.assertEqual(empty.data, b'')
        self.assertEqual(empty.mappings, ())

    def test_admitted_aliases_remove_declarations_and_preserve_source_maps(self):
        a, b = result('a'), result('b')
        aliases = [[parse_identifier('Midas.copy'), parse_identifier('Midas.original')]]
        names = ExportNames(b'', aliases)
        names.add([declaration(a)], parse_identifier(a), 'original')
        names.add([declaration(b)], parse_identifier(b), 'copy')
        first = 'def ' + a + ' := 1\n'
        second = 'def ' + b + ' := 1\n'
        suffix = 'example := ' + b + '\n'
        source = generated(first + second + suffix)
        spans = [(parse_identifier(a), ByteSpan(0, len(first))),
                 (parse_identifier(b), ByteSpan(len(first), len(first + second)))]
        emitted = []
        actual = names.rewrite(source, maximum=10000, spans=spans, generated=emitted)
        self.assertEqual(actual.data.decode(), 'def Midas.original := 1\nexample := Midas.original\n')
        self.assertEqual(emitted, [parse_identifier('Midas.original')])
        location = actual.authored_location(actual.data.index(b'example'))
        self.assertEqual(location.byte_offset, len(first + second))

    def test_model_alias_names_reach_shared_helpers_and_forwarding_results(self):
        leaf, wrapper, parent = result('a'), result('b'), result('c')
        scope = dependencies('c')
        rows = [declaration(leaf), declaration(wrapper, '@_root_.' + leaf, (leaf,)),
                declaration(scope + '.model_lemma', '@_root_.' + wrapper, (wrapper,)),
                declaration(parent, 'open _root_.' + scope + ' in\nby exact model_lemma',
                            (scope + '.model_lemma',))]
        names = ExportNames(b'')
        names.add(rows, parse_identifier(parent), 'Target.answer')
        text = '\n'.join((leaf, wrapper, parent, scope + '.model_lemma'))
        actual = names.rewrite(generated(text), maximum=10000).data.decode()
        self.assertEqual(actual.splitlines(), [
            'Midas.model_lemma', 'Midas.model_lemma_2',
            'Midas.answer', 'Midas.model_lemma_3'])
        self.assertEqual(names.rewrite(generated(rows[-1]['source']), maximum=10000).data.decode(),
                         'open _root_.Midas renaming model_lemma_3 → model_lemma in\nby exact model_lemma')
        # A later selected root may share this helper under another local alias.
        other = result('d')
        names.add([rows[0], declaration(other)], parse_identifier(other), 'other_answer')
        self.assertEqual(names.rewrite(generated(leaf), maximum=10000).data.decode(), actual.splitlines()[0])
        self.assertEqual(rows[0]['name'], parse_identifier(leaf))

    def test_deduplicated_forwarders_keep_the_lemma_name_and_only_needed_renames(self):
        leaf, wrapper, parent = (result(letter) for letter in 'abc')
        scope, other_scope = dependencies('c'), dependencies('d')
        rows = [declaration(leaf), declaration(wrapper, '@_root_.' + leaf, (leaf,)),
                declaration(scope + '.lemma', '@_root_.' + wrapper, (wrapper,)),
                declaration(other_scope + '.other', '@_root_.' + wrapper, (wrapper,)),
                declaration(parent)]
        for authored, canonical, removed in (
                (b'', 'lemma', ('lemma_2', 'lemma_3', 'other')),
                (b'namespace Midas\ndef lemma := 99\nend Midas\n',
                 'lemma_2', ('lemma_3', 'lemma_4', 'other'))):
            with self.subTest(authored=authored):
                aliases = [[parse_identifier('Midas.' + old), parse_identifier('Midas.' + canonical)]
                           for old in removed]
                names = ExportNames(authored, aliases)
                names.add(rows, parse_identifier(parent), 'answer')
                chunks, spans, offset = [], [], 0
                for row in rows:
                    text = 'def ' + lean_name(row['name'], readable=True) + ' := ' + row['source'] + '\n'
                    chunks.append(text)
                    spans.append((row['name'], ByteSpan(offset, offset + len(text.encode()))))
                    offset += len(text.encode())
                chunks.append('open ' + scope + ' in\nby exact lemma\n')
                chunks.append('open ' + other_scope + ' in\nby exact other\n')
                actual = names.rewrite(generated(''.join(chunks)), maximum=10000, spans=spans).data.decode()
                self.assertEqual(actual.count('def '), 2)
                self.assertIn('def Midas.' + canonical + ' := by trivial\n', actual)
                self.assertNotIn('_proof', actual)
                local = '(lemma)' if canonical == 'lemma' else 'renaming lemma_2 → lemma'
                self.assertIn('open _root_.Midas ' + local + ' in\nby exact lemma', actual)
                self.assertIn('open _root_.Midas renaming ' + canonical + ' → other in\nby exact other', actual)

    def test_collisions_reserve_authored_names_and_keep_distinct_contexts(self):
        a, b, c, parent = (result(letter) for letter in 'abcd')
        scope = dependencies('d')
        rows = [declaration(n) for n in (a, b, c)]
        rows += [declaration(scope + '.' + label, '@_root_.' + child, (child,))
                 for label, child in (('same', a), ('same_2', b))]
        rows += [declaration(dependencies('c') + '.same', '@_root_.' + c, (c,)), declaration(parent)]
        names = ExportNames(b'namespace Midas\ndef same := 1\nend Midas\n')
        names.add(rows, parse_identifier(parent), 'answer')
        actual = names.rewrite(generated('\n'.join((a, b, c))), maximum=10000).data.decode().splitlines()
        self.assertEqual(actual, ['Midas.same_3', 'Midas.same_2',
                                  'Midas.same_4'])
        contexts = [{'namespace': 'MidasContext_' + x*64, 'name': 'localDef'} for x in 'ab']
        names.contexts(contexts)
        names.contexts(contexts)
        actual = names.rewrite(generated('\n'.join(row['namespace'] + '.«localDef»' for row in contexts)),
                               maximum=10000).data.decode()
        self.assertEqual(actual, 'Midas.localDef\nMidas.localDef_2')
        opens = '\n'.join('open ' + row['namespace'] for row in contexts)
        self.assertEqual(names.rewrite(generated(opens), maximum=10000).data.decode(),
                         'open _root_.Midas (localDef)\nopen _root_.Midas renaming localDef_2 → localDef')

    def test_context_and_dependency_collisions_preserve_authored_local_names(self):
        root, scope = result('a'), dependencies('a')
        alias = scope + '.x'
        names = ExportNames(b'namespace Midas\ndef x := 99\nend Midas\n')
        names.add([declaration(alias), declaration(scope + '.y'), declaration(root)],
                  parse_identifier(root), 'x')
        context = {'namespace': 'MidasContext_' + 'a'*64, 'name': 'x'}
        names.contexts([context])
        source = ('open ' + context['namespace'] + '\nopen ' + scope + ' in\n'
                  'by\n  have x := 7\n  exact x -- keep the local binder\n')
        self.assertEqual(names.rewrite(generated(source), maximum=10000).data.decode(),
            'open _root_.Midas renaming x_4 → x\n'
            'open _root_.Midas renaming x_3 → x, y → y in\n'
            'by\n  have x := 7\n  exact x -- keep the local binder\n')

    def test_quoted_names_unicode_comments_strings_and_source_maps(self):
        old = result('a')
        names = ExportNames(b'')
        names.add([declaration(old)], parse_identifier(old), '«résultat.α»')
        quoted = '_root_.«MidasCandidateResult».«s' + 'a'*64 + '»'
        text = ('-- ' + old + '\n/- nested /- ' + old + ' -/ comment -/\n'
                '"' + old + '"\nr##"a "' + old + '" b"##\n\'λ\'\n'
                'α ' + quoted + '\nunchanged ' + old + 'Extra\nOther.' + old + '\n')
        source = generated(text)
        actual = names.rewrite(source, maximum=10000)
        self.assertEqual(actual.data.decode(), text.replace(quoted, '_root_.Midas.«résultat.α»'))
        position = actual.data.index(b'unchanged')
        location = actual.authored_location(position)
        self.assertEqual(location.path, 'authored')
        self.assertEqual(location.byte_offset, text.encode().index(b'unchanged'))
        self.assertIsNone(actual.authored_location(actual.data.index(b'_root_')))

    def test_universes_in_forwarding_aliases_and_unlabelled_fallbacks(self):
        child, root = result('a'), result('b')
        row = declaration(root, '@_root_.' + child + '.{u}', (child,))
        row['signature']['universes'] = [parse_identifier('u')]
        names = ExportNames(b'')
        names.add([declaration(child), row, declaration(result('c'))], parse_identifier(root), 'answer')
        actual = names.rewrite(generated('\n'.join((child, root, result('c')))), maximum=10000).data.decode()
        self.assertEqual(actual, 'Midas.answer\nMidas.answer_2\nMidas.proof')

    def test_forwarding_chains_keep_names_short(self):
        a, b, c = (result(letter) for letter in 'abc')
        names = ExportNames(b'')
        names.add([declaration(a), declaration(b, '@_root_.' + a, (a,)),
                   declaration(c, '@_root_.' + b, (b,))], parse_identifier(c), 'answer')
        text = names.rewrite(generated('\n'.join((a, b, c))), maximum=10000).data.decode()
        self.assertEqual(text, 'Midas.answer\nMidas.answer_2\nMidas.answer_3')

    def test_authored_proof_suffix_is_preserved(self):
        a, b = result('a'), result('b')
        names = ExportNames(b'')
        names.add([declaration(a), declaration(b, '@_root_.' + a, (a,))], parse_identifier(b), 'answer_proof')
        actual = names.rewrite(generated(a + '\n' + b), maximum=10000).data.decode()
        self.assertEqual(actual, 'Midas.answer_proof\nMidas.answer_proof_2')


if __name__ == '__main__':
    unittest.main()
