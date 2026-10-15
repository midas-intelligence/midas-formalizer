"""Pure source algorithms over explicit values. No I/O or new persisted formats.

Only domain and standard-library imports are permitted. Compiler protocols,
receipt admission, environment inventories, and export orchestration stay with
their owning components. Structural graphs are expanded privately for rendering.
"""

from .context import (
    CheckedAlias, Context, LookupContext, add_imports, check_imports,
    declaration_context, lookup_context, merge_imports, render_context,
)
from .modules import Module, SourceTree, generated_module, module_tree
from .names import code_only, declaration_names, lean_name, parse_identifier, plain_identifier
from .render import (
    CheckedDeclaration, RootReplacement, candidate, declarations, integrate,
    present_subject, probe, proof_application,
)
from .terms import RenderPolicy, level, string_literal, term
from .text import Edit, GeneratedSource, Location, SourceFile, SourceMapEntry, apply_edits

__all__ = [
    'CheckedAlias', 'CheckedDeclaration', 'Context', 'Edit', 'GeneratedSource',
    'Location', 'LookupContext', 'Module', 'RenderPolicy', 'RootReplacement',
    'SourceFile', 'SourceMapEntry', 'SourceTree', 'add_imports', 'apply_edits',
    'candidate', 'check_imports', 'code_only', 'declaration_context',
    'declaration_names', 'declarations', 'generated_module', 'integrate',
    'lean_name', 'level', 'lookup_context', 'merge_imports', 'module_tree',
    'parse_identifier', 'plain_identifier', 'present_subject', 'probe',
    'proof_application', 'render_context', 'string_literal', 'term',
]
