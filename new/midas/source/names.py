"""Advisory declaration hints from ordinary Lean namespace/section commands.

This is a lexical index, not Lean elaboration. Macro-generated names and private
compiler names are not inferred; every hint still needs checked prefix lookup.
"""
import re

from ..domain.encoding import name as validate_name

_RESERVED = frozenset('''_ Type Sort Prop fun forall let in by do if then else match with
where have show from return yield for while repeat try catch finally theorem def
abbrev example axiom opaque inductive structure class instance namespace section
end open import export variable universe set_option attribute syntax macro elab
deriving mutual private protected public noncomputable unsafe partial nonrec module'''.split())
# Lean's isIdFirst/isIdRest (Init/Meta/Defs.lean), not Python's Unicode
# identifier rules. In particular, subscripts and !/? may continue a name.
_LETTER_LIKE = (r"\u03b1-\u03ba\u03bc-\u03c9\u0391-\u039f\u03a1-\u03a2\u03a4-\u03a9"
                r"\u03ca-\u03fb\u1f00-\u1ffe\u2100-\u214f\U0001d49c-\U0001d59f"
                r"\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0100-\u017f")
_FIRST = 'A-Za-z_' + _LETTER_LIKE
_REST = _FIRST + r"0-9'!?\u2080-\u2089\u2090-\u209c\u1d62-\u1d6a\u2c7c"
_IDENTIFIER = f'[{_FIRST}][{_REST}]*'
_PLAIN = re.compile(_IDENTIFIER)


def plain_identifier(text):
    return isinstance(text, str) and bool(_PLAIN.fullmatch(text)) and text not in _RESERVED


def lean_name(value, *, absolute=False, readable=False):
    """Render a structural Lean name iteratively, including private user aliases."""
    validate_name(value)
    parts = []
    while value != ['anonymous']:
        parts.append((value[0], value[2]))
        value = value[1]
    parts.reverse()
    if parts and parts[0] == ('str', '_private'):
        scope = next((i for i, (kind, _) in enumerate(parts) if kind == 'num'), None)
        if scope is not None:
            parts = parts[scope + 1:]
    if any(kind != 'str' or not text or any(c in text for c in ('«', '»', '\n', '\r', '\x00'))
           for kind, text in parts):
        raise ValueError('Unsupported source spelling for Lean name')
    result = '.'.join(text if readable and plain_identifier(text) else '«' + text + '»'
                      for _, text in parts)
    return ('_root_.' if absolute and result else '') + result


def parse_identifier(text):
    """Exact spelling for advisory alias lookup; expressions are never queries."""
    if not isinstance(text, str) or not text or len(text.encode()) > 1024:
        return None
    parts, cursor = [], 0
    while cursor < len(text):
        if text[cursor] == '«':
            end = text.find('»', cursor + 1)
            if end < 0:
                return None
            part = text[cursor + 1:end]
            if not part or any(c in part for c in ('«', '\n', '\r', '\x00')):
                return None
            cursor = end + 1
        else:
            end = text.find('.', cursor)
            end = len(text) if end < 0 else end
            part = text[cursor:end]
            if not plain_identifier(part):
                return None
            cursor = end
        parts.append(part)
        if cursor < len(text):
            if text[cursor] != '.' or cursor == len(text) - 1:
                return None
            cursor += 1
    if parts[0] == '_root_':
        parts.pop(0)
    if not parts:
        return None
    value = ['anonymous']
    for part in parts:
        value = ['str', value, part]
    return value

_COMPONENT = rf'(?:«[^»\n]+»|{_IDENTIFIER})'
NAME = rf'{_COMPONENT}(?:\.{_COMPONENT})*'
LEXEME = re.compile(r'/-|--[^\n]*|"(?:\\.|[^"\\])*"|«[^»]*»')
COMMENT = re.compile(r'/-|-/')
_TOKENS = re.compile(r'/-|--[^\n]*|r(?P<raw>\#*)"|"|\'(?:\\.|[^\\\'\n])\'|' + NAME)
SCOPE = re.compile(r'^(?:(?:noncomputable|public)\s+)?(namespace|section|end)(?:\s+(' + NAME + r'))?\s*$')
DECL = re.compile(r'^(?P<mod>(?:(?:private|protected|public|noncomputable|unsafe|partial|nonrec)\s+)*)'
                  r'(?:theorem|lemma|def|abbrev|instance|class|structure|inductive|opaque|axiom)\s+'
                  r'(?P<name>' + NAME + r')(?=\s|:|\(|\{|\[|\.\{|$)')


def identifiers(source):
    """Yield identifier spans outside comments, ordinary/raw strings and chars."""
    cursor = 0
    while match := _TOKENS.search(source, cursor):
        token, cursor = match.group(), match.end()
        if token == '/-':
            depth = 1
            for boundary in COMMENT.finditer(source, cursor):
                depth += 1 if boundary.group() == '/-' else -1
                if not depth:
                    cursor = boundary.end()
                    break
            else:
                return
        elif token.startswith('--') or token.startswith("'"):
            continue
        elif token == '"' or match.group('raw') is not None:
            if match.group('raw') is not None:
                ending = '"' + match.group('raw')
                end = source.find(ending, cursor)
                cursor = len(source) if end < 0 else end + len(ending)
            else:
                while cursor < len(source):
                    char = source[cursor]
                    cursor += 1
                    if char == '"':
                        break
                    if char == '\\':
                        cursor += 1
        else:
            yield match.start(), match.end(), token


def rewrite_identifiers(source, rename, *, maximum=64*1024**2):
    """Rename exact tokens in one pass; untouched bytes retain source mappings."""
    from ..domain import ByteSpan
    from .text import Builder, SourceMapEntry
    text = source.data.decode()
    writer, cursor, character, mapping = Builder(maximum), 0, 0, 0
    view = memoryview(source.data)

    def copy(end):
        nonlocal mapping
        start = writer.size
        writer.append(view[cursor:end])
        while mapping < len(source.mappings):
            row = source.mappings[mapping]
            if row.generated.start >= end:
                break
            left, right = max(cursor, row.generated.start), min(end, row.generated.end)
            if left < right:
                origin = row.authored.start + left - row.generated.start
                writer.mappings.append(SourceMapEntry(
                    ByteSpan(start + left - cursor, start + right - cursor), row.original,
                    ByteSpan(origin, origin + right - left)))
            if row.generated.end > end:
                break
            mapping += 1

    for start, end, token in identifiers(text):
        replacement = rename(token)
        if replacement is None or replacement == token:
            continue
        left = cursor + len(text[character:start].encode())
        right = left + len(token.encode())
        copy(left)
        writer.append(replacement.encode())
        cursor, character = right, end
    if not cursor:
        return source
    copy(len(source.data))
    return writer.finish()


def code_only(source):
    """Blank comments and string literals without moving line boundaries."""
    chunks, offset, cursor = [], 0, 0
    while match := LEXEME.search(source, cursor):
        start, end = match.span()
        token = match.group()
        if token.startswith('«'):
            cursor = end
            continue
        if token == '/-':
            depth = 1
            end = len(source)
            for boundary in COMMENT.finditer(source, match.end()):
                depth += 1 if boundary.group() == '/-' else -1
                if not depth:
                    end = boundary.end()
                    break
        chunks.extend((source[offset:start], re.sub(r'[^\n]', ' ', source[start:end])))
        offset = cursor = end
    chunks.append(source[offset:])
    return ''.join(chunks)


def declaration_names(source):
    """Return zero-based line -> qualified candidate names for source excerpts."""
    clean = code_only(source)
    # Attributes may precede a declaration on the same line or span lines.
    clean = re.sub(r'@\[[^\]]*\]', lambda m: re.sub(r'[^\n]', ' ', m.group()), clean)
    namespace, scopes, result = '', [], {}
    for number, line in enumerate(clean.splitlines()):
        line = line.strip()
        if scope := SCOPE.fullmatch(line):
            command, name = scope.groups()
            if command == 'end':
                if scopes:
                    namespace = scopes.pop()
            else:
                scopes.append(namespace)
                if command == 'namespace' and name:
                    namespace = (name.removeprefix('_root_.') if name.startswith('_root_.')
                                 else '.'.join(filter(None, (namespace, name))))
            continue
        if match := DECL.match(line):
            if 'private' in match['mod'].split():
                continue
            name = match['name']
            result[number] = (name.removeprefix('_root_.') if name.startswith('_root_.')
                              else '.'.join(filter(None, (namespace, name))))
    return result
