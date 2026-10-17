from dataclasses import replace
from io import BytesIO
import unittest
from unittest.mock import patch

from midas.domain.encoding import load_metadata
from midas.environment import File, ModulePin, Modules
from midas.environment.inventory import chunks
from midas.environment.options import (MAGIC, module_setup,
                                       option_rows, read_options, saved_options)


class CompilerOptionsTests(unittest.TestCase):
    def test_typed_settings_roundtrip_without_inventory_json(self):
        values = (('autoImplicit', False), ('maxSynthPendingDepth', 3), ('traceLabel', 'a"\\b\n'))
        rows = [(b'M', b'A', b'pkg'), *option_rows(values), (b'M', b'B', b'pkg'), *option_rows(values)]
        with patch('json.dumps', side_effect=AssertionError('inventory JSON')):
            result = read_options(BytesIO(b''.join(chunks(rows, MAGIC))), {'A': 'pkg', 'B': 'pkg'})
        self.assertEqual(result['A'], values)
        self.assertIs(result['A'], result['B'])
        setup = load_metadata(module_setup('A', 'pkg', values))
        self.assertEqual(setup['options'], dict(values))
        self.assertEqual(setup['importArts'], {})
        self.assertEqual(saved_options([list(p) for p in values]), values)

    def test_boolean_and_natural_are_distinct_in_profiles_and_identity(self):
        rows = [(b'M', b'A', b'pkg'), *option_rows((('flag', False),)),
                (b'M', b'B', b'pkg'), *option_rows((('flag', 0),))]
        result = read_options(BytesIO(b''.join(chunks(rows, MAGIC))), {'A': 'pkg', 'B': 'pkg'})
        self.assertIs(type(result['A'][0][1]), bool)
        self.assertIs(type(result['B'][0][1]), int)
        source = File('A.lean', 0, False, '0'*64)
        module = ModulePin('A', 'pkg', source, (), '.', 'pkg', lean_options=result['A'])
        first = Modules('0'*64, ('A',), (module,), (), ())
        second = replace(first, modules=(replace(module, lean_options=result['B']),))
        self.assertNotEqual(first.id, second.id)

    def test_invalid_or_large_profiles_are_rejected_before_json(self):
        cases = ([], (('a', -1),), (('a', 2**64),), (('a', 1.0),), (('a', None),),
                 (('a', 'x'*1025),), (('a', '\0'),), (('a', []),),
                 (('a', 1), ('a', 2)), (('b', 1), ('a', 2)),
                 (('bad option', 1),), tuple((f'v{i:02}', 0) for i in range(65)),
                 (('a', 'x'*1024), ('b', 'x'*1024)))
        for options in cases:
            with self.subTest(options=str(options)[:80]), patch('json.dumps', side_effect=AssertionError('JSON called')):
                with self.assertRaises(ValueError):
                    module_setup('A', 'pkg', options)

    def test_option_stream_requires_exact_modules_and_valid_records(self):
        cases = ([], [(b'M', b'B', b'pkg')], [(b'M', b'A', b'other')],
                 [(b'M', b'A', b'pkg'), (b'M', b'A', b'pkg')],
                 [(b'O', b'x', b'n', b'1')],
                 [(b'M', b'A', b'pkg'), (b'O', b'x', b'n', b'01')],
                 [(b'M', b'A', b'pkg'), (b'O', b'x', b'b', b'2')],
                 [(b'M', b'A', b'pkg'), (b'O', b'x', b'n', b'1'), (b'O', b'x', b's', b'text')],
                 [(b'M', b'A', b'pkg'), (b'O', b'x', b'n', b'1'), (b'O', b'x', b'n', b'2')])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                read_options(BytesIO(b''.join(chunks(rows, MAGIC))), {'A': 'pkg'})


if __name__ == '__main__':
    unittest.main()
