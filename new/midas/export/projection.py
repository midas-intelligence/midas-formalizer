"""Namespace projection using source locations and bindings supplied by Lean."""
import re

from ..source import lean_name, parse_identifier
from ..source.names import rewrite_identifiers
from ..source.text import Builder, GeneratedSource
from .names import _name, _parts

_IN = re.compile(rb'\s+in\b[ \t]*\n?')


def project(data, regions, layout, aliases, *, calls=(), maximum=64*1024**2):
    aliases = {_parts(old): _parts(new) for old, new in aliases}
    generated = {_parts(row['name']) for row in layout}
    writer, cursor, index, call = Builder(maximum), 0, 0, 0
    view = memoryview(data)

    def canonical(name):
        return aliases.get(name, name)

    def outside(start, stop):
        nonlocal call
        # Generated calls in the selected declarations also need the retained
        # name after deduplication. Only absolute helper references can change;
        # comments, strings and all other authored syntax stay intact.
        if not aliases:
            writer.append(view[start:stop])
            return
        def rename(token):
            if not token.startswith('_root_.Midas.'):
                return None
            name = parse_identifier(token)
            target = aliases.get(_parts(name)) if name is not None else None
            return lean_name(_name(target), absolute=True, readable=True) if target else None
        while call < len(calls) and calls[call][0] < stop:
            left, right = calls[call]
            if not start <= left < right <= stop:
                raise ValueError('Generated call overlaps a helper region')
            writer.append(view[start:left])
            source = GeneratedSource(data[left:right])
            writer.append(rewrite_identifiers(source, rename, maximum=maximum).data)
            start, call = right, call + 1
        writer.append(view[start:stop])

    def opens(pairs):
        changes = []
        for alias, target in pairs:
            alias, target = _parts(alias), canonical(_parts(target))
            if target != ('Midas', *alias):
                changes.append(lean_name(_name(target[1:]), readable=True) + ' → ' +
                               lean_name(_name(alias), readable=True))
        return ('open _root_.Midas renaming ' + ', '.join(changes) + ' in\n').encode() if changes else b''

    for start, stop in regions:
        outside(cursor, start)
        opened = False
        while index < len(layout) and layout[index]['span'][0] < stop:
            row = layout[index]
            index += 1
            name = _parts(row['name'])
            if name in aliases:
                continue
            namespace = _parts(row['namespace'])
            if not opened and not namespace:
                writer.append(b'namespace Midas\n\n')
                opened = True
            writer.append(opens(row['opens']))
            left, right = row['span']
            short = name[1:] if name[:1] == ('Midas',) else ('_root_', *name)
            edits = [(*row['name_span'], lean_name(_name(short), readable=True).encode())]
            if namespace and namespace != ('Midas',):
                # Keep this inside command-local `set_option ... in` prefixes:
                # Lean otherwise applies the namespace only to the option.
                position = row['declaration_start']
                edits.append((position, position, b'with_weak_namespace _root_.Midas '))
            prefixes = []
            for a, b, pairs in row['inline']:
                match = _IN.match(data, b, right)
                if match is None:
                    raise ValueError('Checked open prefix differs from source')
                prefixes.append((a, match.end()))
                edits.append((a, match.end(), opens(pairs)))
            prefixes.sort()
            for a, op, b in row['sequences']:
                if data[op:op+3] != b'<;>':
                    raise ValueError('Checked tactic operator differs from source')
                edits.extend(((a, a, b'('), (op, op+3, b';'), (b, b, b')')))
            prefix = 0
            for a, b, reference in row['references']:
                while prefix < len(prefixes) and prefixes[prefix][1] <= a:
                    prefix += 1
                if prefix < len(prefixes) and prefixes[prefix][0] <= a:
                    continue
                original = _parts(reference)
                target = canonical(original)
                token = parse_identifier(data[a:b].decode())
                # Retain short helper names only when they still denote the
                # same constant. External names are absolute, including root
                # constants and names from enclosing or opened namespaces.
                if (original in generated and target[:1] == ('Midas',) and token is not None
                        and _parts(token) == target[1:]):
                    continue
                replacement = lean_name(_name(target), absolute=True, readable=True).encode()
                edits.append((a, b, replacement))
            for a, b, replacement in sorted(edits):
                if a < left:
                    raise ValueError('Overlapping checked source edits')
                writer.append(view[left:a])
                writer.append(replacement)
                left = b
            writer.append(view[left:right])
            writer.append(b'\n\n')
        if opened:
            writer.append(b'end Midas\n\n')
        cursor = stop
    outside(cursor, len(data))
    return writer.finish()
