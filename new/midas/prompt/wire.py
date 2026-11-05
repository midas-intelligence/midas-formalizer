"""Exact provider wire construction; large text is escaped in bounded byte chunks."""
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
import re

from ..domain import DomainError
from ..domain.encoding import canonical, hash_field, record_parts
from .protocol import MAX_BYTES, SCHEMA_INSTRUCTIONS, Prompt, Renderer, Snapshot

_SPECIAL = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_ESCAPES = {i: ('\\u%04x' % i).encode() for i in range(32)}
_ESCAPES.update({34: b'\\"', 92: b'\\\\', 8: b'\\b', 9: b'\\t', 10: b'\\n', 12: b'\\f', 13: b'\\r'})


class _Writer:
    def __init__(self, maximum):
        self.maximum, self.size, self.parts = maximum, 0, []

    def add(self, value):
        self.size += len(value)
        if self.size > self.maximum:
            raise DomainError('Escaped prompt wire exceeds byte limit')
        self.parts.append(value)

    def string(self, parts):
        self.add(b'"')
        for part in parts:
            for start in range(0, len(part), 8192):
                chunk = part[start:start+8192]
                # Common source escapes stay in C, rather than one callback per
                # line. Rare control characters still use bounded substitution.
                chunk = (chunk.replace(b'\\', b'\\\\').replace(b'"', b'\\"')
                         .replace(b'\n', b'\\n').replace(b'\r', b'\\r').replace(b'\t', b'\\t'))
                self.add(_SPECIAL.sub(lambda m: _ESCAPES[m[0][0]], chunk))
        self.add(b'"')

    def finish(self):
        return b''.join(self.parts)


@dataclass(frozen=True, slots=True)
class Options:
    """Explicit saved wire controls. Model selection stays with the caller.

    cache_scope is a provider/account/route hash, never an Environment object.
    Codex model/reasoning settings belong to its pinned provider configuration.
    """
    transport: str
    model: str
    reasoning: str | None = None
    max_output_tokens: int | None = None
    prompt_cache: str | None = None
    cache_scope: str | None = None
    max_bytes: int = MAX_BYTES

    def __post_init__(self):
        if self.transport not in ('codex', 'azure_responses', 'bedrock_responses'):
            raise DomainError('Unsupported prompt transport')
        for value in (self.model, self.reasoning):
            if value is not None and (type(value) is not str or not value.strip()
                    or len(value.encode()) > 256 or value != value.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise DomainError('Invalid prompt model/reasoning setting')
        if self.model is None or self.reasoning is not None and len(self.reasoning) > 128:
            raise DomainError('Invalid prompt model/reasoning setting')
        if self.max_output_tokens is not None and (type(self.max_output_tokens) is not int or self.max_output_tokens <= 0):
            raise DomainError('Invalid output token limit')
        if self.prompt_cache not in (None, 'disabled', 'explicit'):
            raise DomainError('Invalid prompt cache mode')
        if self.cache_scope is not None and (type(self.cache_scope) is not str or
                re.fullmatch('[0-9a-f]{64}', self.cache_scope) is None):
            raise DomainError('Cache scope must be a route hash')
        if self.prompt_cache == 'explicit' and self.cache_scope is None:
            raise DomainError('Explicit prompt caching requires a route scope')
        if self.transport == 'codex' and any(v is not None for v in (
                self.reasoning, self.max_output_tokens, self.prompt_cache, self.cache_scope)):
            raise DomainError('Codex controls belong to its pinned provider configuration')
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_BYTES:
            raise DomainError('Invalid prompt wire byte limit')


@dataclass(frozen=True, slots=True)
class WireRequest:
    role: str
    renderer: str
    contract: str
    wire: bytes
    setup: bytes | None
    snapshot: Snapshot = field(repr=False, compare=False)
    input_bytes: int
    declarations: tuple[str, ...] = ()
    id: str = field(init=False)

    def __post_init__(self):
        # Hash large wire directly. Only the flat provenance uses binary records.
        digest = sha256()
        metadata = ['midas-prompt-v1', self.role, self.renderer, self.contract,
                    [a.to_dict() for a in self.snapshot.artifacts],
                    [[o.obligation_id, o.generation] for o in self.snapshot.owners]]
        for part in record_parts(metadata, limit=MAX_BYTES):
            digest.update(part)
        for data in (self.wire, self.setup or b''):
            digest.update(len(data).to_bytes(8, 'little'))
            digest.update(data)
        object.__setattr__(self, 'id', digest.hexdigest())

    @property
    def provider_role(self):
        return {'researcher': 'research', 'web-searcher': 'web_search'}.get(self.role, self.role)


def _section(section):
    return (section.kind.upper().encode(), b' ', section.name.encode(), b'\n', section.text)


def _content(writer, parts, *, breakpoint=False):
    if breakpoint:
        writer.add(b'[{"type":"input_text","text":')
        writer.string(parts)
        writer.add(b',"prompt_cache_breakpoint":{"mode":"explicit"}}]')
    else:
        writer.string(parts)


def _breakpoints(prompt):
    """Cache reviewer instructions only; preserve growing prover endpoints.

    Reviewer material is usually read once. Its shared instructions are marked
    by _prefix; no later boundary should write the large review-specific suffix.
    For provers, the provider limits writes, not the number of retained markers.
    Explicit lookup checks the first two and latest fifty markers. Keep the
    contract and formal context first, then each evidence/history/candidate
    endpoint, so appending a section does not erase the previous cache boundary.
    Memory, feedback and allowances are changing suffixes, not cache writes.
    https://developers.openai.com/api/docs/guides/prompt-caching
    """
    if prompt.role == 'reviewer':
        return set()
    formal = ('source', 'definitions', 'subject', 'interfaces')
    points = {0, max((i for i, s in enumerate(prompt.sections, 1) if s.kind in formal), default=0)}
    points.update(i for i, s in enumerate(prompt.sections, 1)
                  if s.kind in ('evidence', 'history', 'candidate'))
    return points


def _cache_key(role, renderer, contract, instructions, schema, options):
    digest = sha256(canonical([options.cache_scope, options.model, options.reasoning,
                              options.max_output_tokens, role, renderer, contract]))
    for part in (instructions, SCHEMA_INSTRUCTIONS, schema):
        hash_field(digest, part)
    return 'midas-prefix-' + digest.hexdigest()[:40]


@lru_cache(maxsize=32)
def _prefix(role, renderer, contract, instructions, schema, options):
    """Bounded fixed-prefix reuse, keyed by exact content and wire controls."""
    writer = _Writer(options.max_bytes)
    if options.transport == 'codex':
        writer.add(b'{"baseInstructions":')
        writer.string((instructions, SCHEMA_INSTRUCTIONS, schema))
        writer.add(b'}')
    else:
        controls = dict(model=options.model, stream=True, store=False)
        if options.reasoning is not None:
            controls['reasoning'] = dict(effort=options.reasoning)
        if options.max_output_tokens is not None:
            controls['max_output_tokens'] = options.max_output_tokens
        if options.prompt_cache == 'explicit':
            controls.update(prompt_cache_key=_cache_key(role, renderer, contract, instructions, schema, options),
                            prompt_cache_options=dict(mode='explicit', ttl='30m'))
        writer.add(canonical(controls)[:-1])
        writer.add(b',"input":[{"role":"developer","content":')
        _content(writer, (instructions, SCHEMA_INSTRUCTIONS, schema),
                 breakpoint=options.prompt_cache == 'explicit')
        writer.add(b'}')
    return writer.finish()


def build(snapshot: Snapshot, options: Options, *, renderer: Renderer,
          input_token_budget=None, token_sample=None) -> WireRequest:
    """No registry, model-name heuristics, ambient instructions, or runtime I/O.

    A caller can bind any pure renderer to each model. Freeze the returned bytes
    before dispatch; retries reuse those bytes without rerunning the renderer.
    """
    if not isinstance(snapshot, Snapshot) or not isinstance(options, Options):
        raise DomainError('Expected frozen snapshot and wire options')
    prompt = renderer(snapshot)
    if not isinstance(prompt, Prompt) or not prompt.sections:
        raise DomainError('Renderer must return a nonempty Prompt')
    from .budget import input_bytes, window
    prompt = window(prompt, input_token_budget, token_sample)
    if prompt.role == 'web-searcher' and options.transport != 'codex':
        raise DomainError('Web search requires the app-server transport')
    writer, setup = _Writer(options.max_bytes), None
    prefix = _prefix(prompt.role, prompt.renderer, prompt.contract, prompt.instructions, prompt.schema, options)
    if options.transport == 'codex':
        setup = prefix
        writer.add(b'{"input":[')
        for index, section in enumerate(prompt.sections):
            writer.add(b',' if index else b'')
            writer.add(b'{"type":"text","text":')
            writer.string(_section(section))
            writer.add(b'}')
        writer.add(b']}')
    else:
        writer.add(prefix)
        points = _breakpoints(prompt) if options.prompt_cache == 'explicit' else set()
        for index, section in enumerate(prompt.sections, 1):
            writer.add(b',{"role":"user","content":')
            _content(writer, _section(section), breakpoint=index in points)
            writer.add(b'}')
        writer.add(b']}')
    declarations = tuple(dict.fromkeys(n for s in prompt.sections for n in s.declarations))
    return WireRequest(prompt.role, prompt.renderer, prompt.contract, writer.finish(), setup, snapshot,
                       input_bytes(prompt), declarations)
