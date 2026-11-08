"""Pure, replaceable prover/reviewer/researcher prompts over explicit snapshots.

Only domain, source and the standard library are dependencies. The application
chooses a renderer per model and stores exact wire/setup bytes as provider inputs.
"""
from . import prover, researcher, reviewer, stop_reviewer, web_searcher
from .protocol import Prompt, Renderer, Section, Snapshot, subject
from .wire import Options, WireRequest, build

__all__ = ['Options', 'Prompt', 'Renderer', 'Section', 'Snapshot', 'WireRequest',
           'build', 'prover', 'researcher', 'reviewer', 'stop_reviewer', 'web_searcher', 'subject']
