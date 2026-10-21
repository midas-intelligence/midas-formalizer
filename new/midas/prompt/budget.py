"""Estimate input size from recorded usage and select whole evidence pages."""
from dataclasses import replace

from ..domain import DomainError
from ..domain.records import natural
from .protocol import Section


def section_bytes(section):
    # Include section labels and a small allowance for message framing. Usage
    # calibrates this measure; serialized JSON escaping is not model input text.
    return len(section.text) + len(section.kind) + len(section.name) + 32


def input_bytes(prompt):
    return len(prompt.instructions) + len(prompt.schema) + sum(map(section_bytes, prompt.sections))


def byte_budget(tokens, sample=None):
    """sample is (input bytes, reported input tokens) for the same route/role.

    With no known usage, start at one token per UTF-8 byte. Leave ten percent
    of the token budget unused because the next prompt's token density can vary.
    This is an estimate, not a provider context-length guarantee.
    """
    natural(tokens, 'input_token_budget', positive=True)
    size, used = sample if sample is not None else (1, 1)
    natural(size, 'sample input bytes', positive=True)
    natural(used, 'sample input tokens', positive=True)
    return tokens * size * 9 // (used * 10)


def window(prompt, tokens, sample=None):
    if tokens is None:
        return prompt
    limit = byte_budget(tokens, sample)
    size = input_bytes(prompt)
    if size <= limit:
        return prompt
    evidence = [s for s in prompt.sections if s.kind == 'evidence']
    name = 'evidence-window'
    names = {s.name for s in prompt.sections}
    while name in names:
        name += '-current'
    omitted = set()
    for section in evidence[:-1]:
        omitted.add(section.name)
        size -= section_bytes(section)
        notice = Section('feedback', name,
            f'{len(omitted)} older evidence page(s) omitted to fit the estimated input-token budget. '.encode()
            + b'Delivered pages remain saved; only the newest pages are shown.')
        if size + section_bytes(notice) <= limit:
            sections = tuple(s for s in prompt.sections if s.name not in omitted)
            # Put the notice before the final question without reordering retained pages.
            return replace(prompt, sections=(*sections[:-1], notice, sections[-1]))
    raise DomainError('Input token budget cannot fit the required prompt and newest evidence page')
