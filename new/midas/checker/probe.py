"""Bounded syntax repair candidates; Lean must parse a candidate before using it."""
import re


_LITERAL = re.compile(r"/-|--[^\n]*|r(?P<raw>\#*)\"|\"|'(?:\\.|[^\\'\n])'|«[^»]*»")
_COMMENT = re.compile(r'/-|-/')
_QUOTE = re.compile(r'`\(tactic\|')
_CALL = re.compile(r'(?<![\w.])(?:Lean\.Elab\.Tactic\.)?evalTactic\s*$')
_TACTIC = re.compile(r'(?:have|let|intro|intros|skip|constructor|exact|apply|refine|rw|simp|simpa|'
                     r'rcases|obtain|suffices|calc|clear|change|revert|subst|done)\b|·')
MAX_CODE = 16000
MAX_REPAIRS = 16


def _masked(code):
    """Keep character offsets, hiding nested comments, literals and quoted names."""
    parts, cursor, copied = [], 0, 0
    while match := _LITERAL.search(code, cursor):
        start, end = match.span()
        token = match.group()
        if token == '/-':
            depth = 1
            for boundary in _COMMENT.finditer(code, end):
                depth += 1 if boundary.group() == '/-' else -1
                if not depth:
                    end = boundary.end()
                    break
            else:
                return None
        elif token == '"' or match['raw'] is not None:
            if match['raw'] is not None:
                closing = '"' + match['raw']
                stop = code.find(closing, end)
                if stop < 0:
                    return None
                end = stop + len(closing)
            else:
                interpolated = start > 0 and code[start - 1] == '!'
                braces = 0
                while end < len(code):
                    char = code[end]
                    end += 1
                    if char == '"':
                        # Nested interpolation expressions need Lean's lexer.
                        # Decline rather than treating their strings as code.
                        if braces:
                            return None
                        break
                    if char == '\\':
                        end += 1
                    elif interpolated and char in '{}':
                        if not braces and code[end:end + 1] == char:
                            end += 1
                        else:
                            braces += 1 if char == '{' else -1
                else:
                    return None
        parts.extend((code[copied:start], re.sub(r'[^\n]', ' ', code[start:end])))
        copied = cursor = end
    parts.append(code[copied:])
    return ''.join(parts)


def repair_candidates(code):
    """Repair quote sequence layout, inline bullets and a missing evalTactic closer.

    The original is always parsed first. These are alternatives, not a rewrite of
    valid Lean. Keep edits within complete, static tactic quotations; leave other
    delimiter mistakes, antiquotations and nested quotations to Lean's diagnostic.
    """
    if type(code) is not str or not code or len(code.encode()) > MAX_CODE:
        return ()
    clean = _masked(code)
    if clean is None:
        return ()
    stack, pairs, parents = [], {}, {}
    for index, char in enumerate(clean):
        if char in '([{':
            parents[index] = stack[-1] if stack else None
            stack.append(index)
        elif char in ')]}':
            if not stack or clean[stack[-1]] != {')': '(', ']': '[', '}': '{'}[char]:
                return ()
            pairs[stack.pop()] = index
    closers, layouts = [], []
    quotes = list(_QUOTE.finditer(clean))
    quote_openings = {quote.start() + 1 for quote in quotes}
    for quote in quotes:
        opening = quote.start() + 1
        closing = pairs.get(opening)
        if closing is None:
            continue
        content = clean[quote.end():closing]
        if '$' in content or '`(' in content:
            continue
        parent = parents[opening]
        ancestor = parent
        while ancestor is not None and ancestor not in quote_openings:
            ancestor = parents[ancestor]
        if ancestor is not None:
            continue
        if (parent in stack and re.fullmatch(r'\(\s*←\s*', clean[parent:quote.start()])
                and _CALL.search(clean[:parent])):
            # Insert immediately after the quote, before a following do statement.
            closers.append((closing + 1, closing + 1, ')'))
        lines = [(m.start() + quote.end(), m.group()) for m in re.finditer(r'[^\n]+', content)
                 if m.group().strip()]
        if len(lines) < 2:
            continue
        first, text = lines[0]
        start = first + len(text) - len(text.lstrip())
        line_start = clean.rfind('\n', 0, start) + 1
        inline = line_start <= quote.start()
        following = [(pos, line, len(line) - len(line.lstrip())) for pos, line in lines[1:]]
        indent = min(n for _, _, n in following)
        if inline and clean[start] == '·':
            body = start + 1
            while body < closing and clean[body] in ' \t':
                body += 1
            if body < closing and clean[body] != '\n' and _TACTIC.match(clean, body):
                layouts.append((start + 1, body, '\n' + ' ' * indent))
        elif inline:
            # A leading constructor/skip followed by goals is a raw sequence.
            # Do not reindent an inline have/let with its own nested proof.
            if text.strip() in ('constructor', 'skip'):
                layouts.append((quote.end(), start, ' first |\n' + ' ' * indent))
        else:
            column = start - line_start
            siblings = [(pos, line) for pos, line, n in following
                        if n == column and _TACTIC.match(line.lstrip())]
            if siblings and not any(line.rstrip().endswith(';') for _, line in lines):
                layouts.append((quote.end(), quote.end(), ' first |'))

    def apply(edits):
        cursor, parts = 0, []
        for start, end, replacement in sorted(edits):
            if start < cursor:
                return None
            parts.extend((code[cursor:start], replacement))
            cursor = end
        parts.append(code[cursor:])
        repaired = ''.join(parts)
        return repaired if repaired != code and len(repaired.encode()) <= MAX_CODE else None

    candidates = []
    # Try fewer edits first; a combined candidate handles several bad quotations.
    edits = ([closers] if closers else []) + [closers + [layout] for layout in layouts]
    if len(layouts) > 1:
        edits.append(closers + layouts)
    if len(edits) > MAX_REPAIRS:
        return ()
    for changes in edits:
        candidate = apply(changes)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)
