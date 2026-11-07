"""Projection contracts; compiler fixtures here do not launch Lean."""
from copy import deepcopy
import re
import unittest

from midas.checker.evidence import source_layout
from midas.export.projection import project
from midas.source import lean_name, parse_identifier


def fake_layout(packet):
    """Locations for the simple generated definitions used by fake compilers."""
    data, rows = packet['source'].encode(), []
    for name in packet['share']:
        spelling = lean_name(name, absolute=True, readable=True).encode()
        name_start = data.index(spelling, rows[-1]['span'][1] if rows else 0)
        name_end = name_start + len(spelling)
        start = data.rfind(b'\n', 0, name_start) + 1
        declaration_start = start
        option = b'set_option autoImplicit true in\n'
        if data[max(0, start - len(option)):start] == option:
            start -= len(option)
        end = data.index(b'\n)\n\n', name_end) + 2
        inlines = []
        for match in re.finditer(rb'open _root_\.Midas (\([^\n]*\)|renaming [^\n]*) in\n', data[name_end:end]):
            spec = match[1].decode()
            pairs = [part.strip().split(' → ') for part in spec.removeprefix('renaming ').split(',')] if spec.startswith('renaming ') else [[x, x] for x in spec[1:-1].split()]
            pairs = [[parse_identifier(alias), parse_identifier('Midas.' + target)] for target, alias in pairs]
            inlines.append([name_end + match.start(), name_end + match.start(1) + len(match[1]), pairs])
        references = [[name_end + m.start(), name_end + m.end(), parse_identifier(m[0].decode())]
                      for m in re.finditer(rb'_root_\.Midas\.[A-Za-z_][A-Za-z_0-9]*', data[name_end:end])]
        rows.append(dict(name=name, namespace=['anonymous'], span=[start, end], declaration_start=declaration_start,
                         name_span=[name_start, name_end],
                         references=references, opens=[], inline=inlines))
    return rows


class ProjectionTests(unittest.TestCase):
    def test_root_enclosing_and_opened_constants_are_qualified_but_binders_are_not(self):
        prefix = b'namespace Authored\n'
        body = (b'section\nopen _root_.Midas (helper)\n'
                b'noncomputable def _root_.Midas.count : Nat := (\n'
                b'by\n  let helper := 1\n  exact count + opened + localCount + helper\n)\n\nend\n\n')
        data = prefix + body + b'end Authored\n'
        packet = dict(source=data.decode(), share=[parse_identifier('Midas.count')],
                      regions=[[len(prefix), len(prefix + body)]], calls=[])
        layout = fake_layout(packet)
        layout[0]['namespace'] = parse_identifier('Authored')
        references = []
        for spelling, target in ((b'count', 'count'), (b'opened', 'Imported.opened'), (b'localCount', 'Authored.localCount')):
            start = data.index(spelling, data.index(b'exact'))
            references.append([start, start + len(spelling), parse_identifier(target)])
        layout[0]['references'] = references
        source_layout(layout, packet)
        result = project(data, packet['regions'], layout, []).data
        self.assertIn(b'with_weak_namespace _root_.Midas noncomputable def count', result)
        self.assertIn(b'def count : Nat', result)
        self.assertIn(b'let helper := 1', result)
        self.assertIn(b'exact _root_.count + _root_.Imported.opened + _root_.Authored.localCount + helper', result)
        self.assertNotIn(b'open ', result)
        self.assertTrue(result.endswith(b'end Authored\n'))
        for field, value in (('span', [0, len(data)]), ('name_span', [0, 1]), ('references', [[0, 1, parse_identifier('x')]])):
            changed = deepcopy(layout)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                source_layout(changed, packet)
        for references in (list(reversed(references)), [references[0], references[0]]):
            changed = deepcopy(layout)
            changed[0]['references'] = references
            with self.assertRaises(ValueError):
                source_layout(changed, packet)

    def test_dedup_updates_calls_and_drops_a_region_with_only_duplicates(self):
        first = b'def _root_.Midas.helper : Nat := (\n1\n)\n\n'
        second = b'section\nopen _root_.Midas (helper)\ndef _root_.Midas.copy : Nat := (\nhelper\n)\n\nend\n\n'
        middle = b'example : Nat := @_root_.Midas.helper\n'
        suffix = (b'example : Nat := @_root_.Midas.copy\n'
                  b'-- @_root_.Midas.copy\n'
                  b'def text := "@_root_.Midas.copy"\n')
        data = first + middle + second + suffix
        regions = [[0, len(first)], [len(first + middle), len(first + middle + second)]]
        packet = dict(source=data.decode(), share=[parse_identifier('Midas.' + n) for n in ('helper', 'copy')],
                      regions=regions, calls=[[len(first), len(first + middle)],
                          [len(first + middle + second), len(first + middle + second) + suffix.index(b'--')]])
        layout = fake_layout(packet)
        source_layout(layout, packet)
        aliases = [[packet['share'][1], packet['share'][0]]]
        result = project(data, regions, layout, aliases, calls=packet['calls']).data
        self.assertEqual(result.count(b'namespace Midas'), 1)
        self.assertEqual(result.count(b'example : Nat := @_root_.Midas.helper'), 2)
        self.assertNotIn(b'section', result)
        self.assertNotIn(b'open ', result)
        self.assertNotIn(b'def copy', result)
        self.assertTrue(result.endswith(b'-- @_root_.Midas.copy\ndef text := "@_root_.Midas.copy"\n'))
        changed = dict(packet, calls=[regions[0]])
        with self.assertRaises(ValueError):
            source_layout(layout, changed)


if __name__ == '__main__':
    unittest.main()
