"""Readable export names recovered from checked dependency aliases.

Certificates retain their exact identities. This table only projects generated
source, which the integration and independent audit subsequently check.
"""
import re
from heapq import merge

from ..source.names import declaration_names, identifiers, lean_name, parse_identifier, rewrite_identifiers

_ROOT = 'MidasCandidateResult'
_RESULT = re.compile(r's[0-9a-f]{64}')
_DEPENDENCIES = re.compile(r'dependencies[0-9a-f]{64}')


def _parts(name):
    parts = []
    while name != ['anonymous']:
        if name[0] != 'str':
            raise ValueError('Unsupported generated source name')
        parts.append(name[2])
        name = name[1]
    return tuple(reversed(parts))


def _name(parts):
    result = ['anonymous']
    for part in parts:
        result = ['str', result, part]
    return result


def _forward(row):
    """Recognize a generated alias, never infer names from arbitrary proof text."""
    if len(row['requires']) != 1:
        return None
    text = row['source'].strip()
    if not text.startswith('@'):
        return None
    reference = row['requires'][0]
    for readable in (False, True):
        spelling = '@' + lean_name(reference, absolute=True, readable=readable)
        universes = row['signature']['universes']
        if universes:
            spelling += '.{' + ', '.join(lean_name(u, readable=readable) for u in universes) + '}'
        if text == spelling:
            return _parts(reference)
    return None


class ExportNames:
    def __init__(self, source, aliases=()):
        self.names, self.used, self.reserved = {}, set(), set()
        # Only the admitted integration audit supplies these equivalences.
        self.aliases = {_parts(old): _parts(new) for old, new in aliases}
        self.opens = {}
        self.suffixes = {}
        # Avoid names already mentioned by authored source, including namespaces.
        if b'Midas' not in source:
            return
        text = source.decode()
        for _, _, token in identifiers(text):
            name = parse_identifier(token)
            if name is not None:
                parts = _parts(name)
                self.used.update(parts[:i] for i in range(1, len(parts) + 1))
        for token in declaration_names(text).values():
            name = parse_identifier(token)
            if name is not None:
                parts = _parts(name)
                self.used.update(parts[:i] for i in range(1, len(parts) + 1))

    def _allocate(self, namespace, preferred):
        base = (*namespace, preferred)
        name, suffix = base, self.suffixes.get(base, 2)
        while name in self.used or name != base and name in self.reserved:
            name = (*namespace, preferred + '_' + str(suffix))
            suffix += 1
        self.suffixes[base] = suffix
        self.used.add(name)
        return name

    def add(self, rows, root, label):
        parsed = parse_identifier(label)
        label = _parts(parsed)[-1] if parsed else 'proof'
        by_name = {_parts(row['name']): row for row in rows}
        labels = {_parts(root): label}
        groups = {}
        for name, row in by_name.items():
            if len(name) >= 3 and name[0] == _ROOT and _DEPENDENCIES.fullmatch(name[1]):
                groups.setdefault(name[:2], []).append(name)
                child = _forward(row)
                if child is not None:
                    labels.setdefault(child, name[-1])
            elif not (len(name) == 2 and name[0] == _ROOT and _RESULT.fullmatch(name[1])):
                self.used.update(name[:i] for i in range(1, len(name) + 1))
        pending = list(labels)
        for name in pending:
            row = by_name.get(name)
            child = _forward(row) if row else None
            if child is not None and child not in labels:
                labels[child] = labels[name]
                pending.append(child)
        self.reserved.update(('Midas', label) for label in labels.values())
        self.reserved.update(('Midas', name[-1]) for group in groups.values() for name in group)
        # Checked rows are in dependency order. Give the proof its readable name
        # before its forwarding wrappers, which the kernel audit may remove.
        # Keep authored suggestions ahead of unlabelled fallbacks and aliases.
        for name in (*[name for name in by_name if name in labels], *by_name):
            if name in self.names:
                continue
            if len(name) == 2 and name[0] == _ROOT and _RESULT.fullmatch(name[1]):
                self.names[name] = self._allocate(('Midas',), labels.get(name, 'proof'))
        for namespace, members in groups.items():
            for name in members:
                if name not in self.names:
                    self.names[name] = self._allocate(('Midas',), name[-1])
            self._open(namespace, members)

    def _open(self, namespace, members):
        """Keep the old scope's short bindings without opening unrelated names.

        Lean resolves local binders before these aliases. Replacing unqualified
        tokens instead would also rename shadowing binders in authored code.
        """
        pairs = [(self._project(name)[-1], name[-1]) for name in members]
        def spelling(part):
            return lean_name(_name((part,)), readable=True)
        if all(new == old for new, old in pairs):
            body = '(' + ' '.join(spelling(new) for new, _ in pairs) + ')'
        else:
            body = 'renaming ' + ', '.join(spelling(new) + ' → ' + spelling(old)
                                          for new, old in pairs)
        self.opens[namespace] = '_root_.Midas ' + body

    def contexts(self, rows):
        rows = tuple(rows)
        self.reserved.update(('Midas', row['name']) for row in rows)
        for row in rows:
            old = (row['namespace'], row['name'])
            if old not in self.names:
                self.names[old] = self._allocate(('Midas',), row['name'])
                self._open(old[:1], (old,))

    def _rename(self, token):
        if 'Midas' not in token:
            return None
        name = parse_identifier(token)
        if name is None:
            return None
        parts = _parts(name)
        if parts in self.opens:
            return self.opens[parts]
        for size in range(len(parts), 0, -1):
            prefix = parts[:size]
            replacement = self.names.get(prefix)
            if replacement is not None:
                replacement = self.aliases.get(replacement, replacement)
                return lean_name(_name((*replacement, *parts[size:])),
                                 absolute=token.startswith('_root_.'), readable=True)
        return None

    def _project(self, name):
        flat = self.names.get(name, name)
        return self.aliases.get(flat, flat)

    def rewrite(self, source, *, maximum, spans=(), sections=(), generated=None):
        from ..domain import ByteSpan
        from ..source.text import Builder, SourceMapEntry
        removed, retained = [], []
        for name, span in spans:
            flat = self.names.get(_parts(name), _parts(name))
            if flat in self.aliases:
                removed.append(span)
            else:
                if sections:
                    retained.append(span)
                if generated is not None:
                    generated.append(_name(flat))
        if removed and sections:
            # Renderer-owned sections are flat and contain only opens and the
            # recorded declarations. Walk their spans, never parse proof text.
            empty, index = [], 0
            for section in sections:
                while index < len(retained) and retained[index].end <= section.start:
                    index += 1
                if index == len(retained) or retained[index].start >= section.end:
                    empty.append(section)
            if empty:
                combined = []
                for span in merge(removed, empty, key=lambda span: span.start):
                    if combined and span.start <= combined[-1].end:
                        previous = combined[-1]
                        combined[-1] = ByteSpan(previous.start, max(previous.end, span.end))
                    else:
                        combined.append(span)
                removed = combined
        if removed:
            writer, cursor, mapping = Builder(maximum), 0, 0
            view = memoryview(source.data)
            # Walk both ordered sequences once, including mappings in deleted
            # declarations. Repeated Builder.slice calls would be quadratic.
            for span in (*removed, ByteSpan(len(source.data), len(source.data))):
                start = writer.size
                writer.append(view[cursor:span.start])
                while mapping < len(source.mappings):
                    row = source.mappings[mapping]
                    if row.generated.start >= span.start:
                        break
                    left, right = max(cursor, row.generated.start), min(span.start, row.generated.end)
                    if left < right:
                        origin = row.authored.start + left - row.generated.start
                        writer.mappings.append(SourceMapEntry(
                            ByteSpan(start + left - cursor, start + right - cursor), row.original,
                            ByteSpan(origin, origin + right - left)))
                    if row.generated.end > span.start:
                        break
                    mapping += 1
                cursor = span.end
            source = writer.finish()
        return rewrite_identifiers(source, self._rename, maximum=maximum)
