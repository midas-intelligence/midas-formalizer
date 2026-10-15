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
_PLAIN = re.compile(r"[A-Za-z_\u03b1-\u03c9][A-Za-z_0-9\u03b1-\u03c9']*")


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
            if not part.replace("'", '_').isidentifier() or part in _RESERVED:
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

NAME = r"(?:«[^»\n]+»|[\w']+)(?:\.(?:«[^»\n]+»|[\w']+))*"
LEXEME = re.compile(r'/-|--[^\n]*|"(?:\\.|[^"\\])*"|«[^»]*»')
COMMENT = re.compile(r'/-|-/')
SCOPE = re.compile(r'^(?:(?:noncomputable|public)\s+)?(namespace|section|end)(?:\s+(' + NAME + r'))?\s*$')
DECL = re.compile(r'^(?P<mod>(?:(?:private|protected|public|noncomputable|unsafe|partial|nonrec)\s+)*)'
                  r'(?:theorem|lemma|def|abbrev|instance|class|structure|inductive|opaque)\s+'
                  r'(?P<name>' + NAME + r')(?=\s|:|\(|\{|\[|\.\{|$)')


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
