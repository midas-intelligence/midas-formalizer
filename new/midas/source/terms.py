"""Bounded Lean syntax rendering. Binary graphs are expanded only inside here."""

from dataclasses import dataclass
import re

from ..domain import encoding as graph
from .names import lean_name, plain_identifier
from .text import MAX_BYTES, byte_limit


@dataclass(frozen=True, slots=True)
class RenderPolicy:
    readable: bool = False
    raw_naturals: bool = True
    group_sorts: bool = True
    computable: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in
               (self.readable, self.raw_naturals, self.group_sorts, self.computable)):
            raise ValueError('Renderer options must be booleans')


@dataclass(frozen=True, slots=True)
class _Doc:
    parts: tuple
    size: int


class _Renderer:
    """Size-checked ropes preserve DAG sharing until the final bounded output."""

    def __init__(self, policy, maximum):
        self.policy, self.maximum = policy, byte_limit(maximum)
        self.terms, self.levels, self.names = {}, {}, {}

    def join(self, *parts):
        parts = tuple(part.encode() if isinstance(part, str) else part for part in parts)
        size = sum(part.size if isinstance(part, _Doc) else len(part) for part in parts)
        if size > self.maximum:
            raise ValueError('Rendered Lean source exceeds byte limit')
        return _Doc(parts, size)

    def finish(self, doc):
        pending, chunks = [doc], []
        while pending:
            item = pending.pop()
            if isinstance(item, _Doc):
                pending.extend(reversed(item.parts))
            elif item:
                chunks.append(item)
        return b''.join(chunks)

    def name(self, value, *, absolute=False):
        key = id(value), absolute
        if key not in self.names:
            self.names[key] = (value, lean_name(value, absolute=absolute, readable=self.policy.readable))
        return self.names[key][1]

    def level(self, value):
        key = id(value)
        if key in self.levels:
            return self.levels[key][1]
        if value == ['zero']:
            result = self.join('0')
        elif len(value) == 2 and value[0] == 'param':
            result = self.join(self.name(value[1]))
        elif len(value) == 2 and value[0] == 'succ':
            result = self.join('(', self.level(value[1]), ' + 1)')
        elif len(value) == 3 and value[0] in ('max', 'imax'):
            result = self.join('(', value[0], ' ', self.level(value[1]), ' ', self.level(value[2]), ')')
        else:
            raise ValueError('Unsupported Lean level')
        self.levels[key] = value, result
        return result

    def binder(self, value, scope):
        if not self.policy.readable:
            base = '_midas' + str(len(scope))
        else:
            base = value[2] if (len(value) == 3 and value[:2] == ['str', ['anonymous']]
                                and plain_identifier(value[2])) else 'x'
        candidate, index = base, 1
        while candidate in scope:
            candidate, index = base + '_' + str(index), index + 1
        return candidate

    def expr(self, value, scope=()):
        key = id(value), scope
        if key in self.terms:
            return self.terms[key][1]
        if not isinstance(value, list) or not value:
            raise ValueError('Expected a Lean expression')
        tag = value[0]
        if tag == 'bvar' and len(value) == 2:
            if type(value[1]) is not int or not 0 <= value[1] < len(scope):
                raise ValueError('Loose bound variable')
            result = self.join(scope[-1 - value[1]])
        elif tag == 'const' and len(value) == 3:
            levels = []
            for level in value[2]:
                if levels:
                    levels.append(', ')
                levels.append(self.level(level))
            suffix = self.join('.{', *levels, '}') if levels else b''
            result = self.join('@', self.name(value[1], absolute=True), suffix)
        elif tag == 'sort' and len(value) == 2:
            result = self.join('Sort ', self.level(value[1]))
            if self.policy.group_sorts:
                result = self.join('(', result, ')')
        elif tag == 'app' and len(value) == 3:
            args, head = [], value
            while isinstance(head, list) and len(head) == 3 and head[0] == 'app':
                args.append(head[2])
                head = head[1]
            parts = ['(', self.expr(head, scope)]
            for arg in reversed(args):
                parts.extend((' ', self.expr(arg, scope)))
            result = self.join(*parts, ')')
        elif tag in ('lam', 'forall') and len(value) == 5:
            name = self.binder(value[1], scope)
            delimiters = {'explicit': ('(', ')'), 'implicit': ('{', '}'),
                          'strictImplicit': ('⦃', '⦄'), 'instance': ('[', ']')}
            if value[2] not in delimiters:
                raise ValueError('Unsupported Lean binder')
            opening, closing = delimiters[value[2]]
            result = self.join('(fun ' if tag == 'lam' else '(∀ ', opening, name, ' : ',
                               self.expr(value[3], scope), closing, ' => ' if tag == 'lam' else ', ',
                               self.expr(value[4], (*scope, name)), ')')
        elif tag == 'let' and len(value) == 6 and type(value[5]) is bool:
            name = self.binder(value[1], scope)
            result = self.join('(let ', name, ' : ', self.expr(value[2], scope), ' := ',
                               self.expr(value[3], scope), '; ', self.expr(value[4], (*scope, name)), ')')
        elif tag == 'proj' and len(value) == 4:
            graph.name(value[1])
            graph.natural(value[2])
            result = self.join('(', self.expr(value[3], scope), ').', str(value[2] + 1))
        elif tag == 'nat' and len(value) == 2:
            graph.natural(value[1])
            result = self.join('(nat_lit ', str(value[1]), ')') if self.policy.raw_naturals else self.join(str(value[1]))
        elif tag == 'string' and len(value) == 2 and isinstance(value[1], str):
            result = self.join(string_literal(value[1], maximum=self.maximum))
        else:
            raise ValueError('Unsupported Lean expression')
        self.terms[key] = value, result
        return result


_ESCAPES = re.compile(r'["\\\x00-\x1f\x7f]')
_REPLACEMENTS = {'"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t'}


def string_literal(value, *, maximum=MAX_BYTES):
    """Lean string syntax without JSON serialization, including large literals."""
    maximum = byte_limit(maximum)
    if not isinstance(value, str) or len(value) + 2 > maximum:
        raise ValueError('Lean string exceeds byte limit')
    size = len(value.encode()) + 2
    for match in _ESCAPES.finditer(value):
        size += len(_REPLACEMENTS.get(match[0], '\\x00')) - 1
        if size > maximum:
            raise ValueError('Lean string exceeds byte limit')
    if size > maximum:
        raise ValueError('Lean string exceeds byte limit')
    return '"' + _ESCAPES.sub(lambda match: _REPLACEMENTS.get(match[0], '\\x' + format(ord(match[0]), '02x')), value) + '"'


def term(expression: bytes, *, scope=(), policy=RenderPolicy(), maximum=MAX_BYTES):
    """Render a binary expression graph to raw UTF-8; retain no expanded tree."""
    if type(scope) is not tuple or any(not plain_identifier(name) for name in scope) or len(set(scope)) != len(scope):
        raise ValueError('Expected distinct plain local names')
    renderer = _Renderer(policy, maximum)
    return renderer.finish(renderer.expr(graph.decode(expression), scope))


def level(universe: bytes, *, policy=RenderPolicy(), maximum=MAX_BYTES):
    renderer = _Renderer(policy, maximum)
    return renderer.finish(renderer.level(graph.decode(universe)))
