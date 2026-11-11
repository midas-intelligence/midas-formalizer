"""Local source comparisons against v2; no Lean, database, or network calls."""

import argparse
from pathlib import Path
from statistics import median
import sys
from timeit import repeat

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from midas_formalizer import assembly as old_terms
from midas_formalizer import candidate_imports as old_imports
from midas_formalizer import certificate_codec as old_graph
from midas_formalizer import declaration_context as old_context
from midas_formalizer import source_names as old_names
from midas_formalizer import lookup_context as old_lookup
from midas_formalizer.persisted import canonical as old_json
from midas.domain import ByteSpan
from midas.domain import encoding as graph
from midas.domain.identity import make_context
from midas.source import Edit, GeneratedSource, SourceFile, add_imports, apply_edits, lean_name, render_context, term
from midas.source import declaration_names, lookup_context, parse_identifier
from midas.source.context import _CONTEXT_SOURCES
from tests.test_source import name
from tests.test_domain_contracts import selected_problem
from types import SimpleNamespace


def measure(old, new, number):
    old(); new()
    times = ([], [])
    for index in range(7):
        for side in (index % 2, 1 - index % 2):
            times[side].append(repeat((old, new)[side], number=number, repeat=1)[0] / number)
    previous, current = (median(values) for values in times)
    return previous * 1e6, current * 1e6, current / previous


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    structural = ['anonymous']
    for index in range(32):
        structural = ['str', structural, 'n' + str(index)]
    identifiers = ('Nat.add', 'EuclideanGeometry.angle_add_angle_add_angle_eq_pi', "A.α'", 'A.«x.y»')
    assert [old_lookup._identifier(n) for n in identifiers] == [parse_identifier(n) for n in identifiers]
    source = ('namespace A\n' + 'theorem x : True := by trivial\n' * 1000 + 'end\n')
    header = '/- ' + 'long comment ' * 20000 + '-/\nmodule\n' + source
    header_source = GeneratedSource(header.encode())
    shared = ['app', ['const', name('f'), []], ['nat', 1]]
    for _ in range(9):
        shared = ['app', ['app', ['const', name('f'), []], shared], shared]
    payload = graph.encode(shared)
    definitions = [{'name': 'x' + str(i), 'type': 'Nat', 'value': '1'} for i in range(32)]
    old_ctx, new_ctx = old_context.make('scope', definitions), graph.encode(make_context('scope', definitions))
    old_ctx_bytes = old_graph.encode(old_ctx)
    def cold_context():
        _CONTEXT_SOURCES.reset()
        return render_context(new_ctx)
    data = SourceFile('Main.lean', b'-- padding\n' * 200000)
    spans = tuple(ByteSpan(index, index + 5) for index in range(0, len(data.data), 22000))
    edits = tuple(Edit(span, GeneratedSource(b'-- new')) for span in spans)
    def old_splice():
        result = data.data
        for span in reversed(spans):
            result = result[:span.start] + b'-- new' + result[span.end:]
        return result
    _, _, _, selection = selected_problem()
    old_selection = SimpleNamespace(payload=old_json({'source': selection.source.hex()}), selected=selection.selected)
    cases = {
        'name': (lambda: old_terms.lean_name(structural), lambda: lean_name(structural), 1000),
        'parse_identifier': (lambda: [old_lookup._identifier(n) for n in identifiers],
                             lambda: [parse_identifier(n) for n in identifiers], 1000),
        'source_hints': (lambda: old_names.declaration_names(source), lambda: declaration_names(source), 30),
        'imports': (lambda: old_imports.augment(header, ['Std']),
                    lambda: add_imports(header_source, ('Std',), available=frozenset({'Std'})), 30),
        'shared_term': (lambda: old_terms.term(old_graph.decode(payload), group_sorts=True).encode(), lambda: term(payload), 20),
        'contexts': (lambda: old_context.render(old_ctx).encode(), lambda: render_context(new_ctx), 20),
        'contexts_cold': (lambda: old_context.render(old_graph.decode(old_ctx_bytes)).encode(), cold_context, 20),
        'source_edits': (old_splice, lambda: apply_edits(data, edits), 10),
        'lookup': (lambda: old_lookup.prefix(old_selection, 1), lambda: lookup_context(selection, 1, ()), 1000),
    }
    for case, (old, new, number) in cases.items():
        if args.case and case not in args.case:
            continue
        previous, current, ratio = measure(old, new, number)
        print(f'{case}: v2={previous:.2f} us new={current:.2f} us ratio={ratio:.3f}')


if __name__ == '__main__':
    main()
