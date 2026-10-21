"""Pure strategy plans and reductions; imports only domain, policy, prompt, tools.

Controller authenticates receipts, resolves candidate entries through checker,
publishes proposed bytes and commits each Changes value with its expected versions.
None of these objects, including a model review, substitutes for a proof check.
"""
from .candidates import (Context, Draft, Prerequisite, Reference, Subject, candidate_material,
                         child_context, expand, fragments, revise, supplied)
from .memory import Lesson, Memory, Observation, Update, update_memory
from .protocol import Response, interpret
from .repairs import Repair, consumers, reopen
from .service import Strategy
from .state import (ArtifactData, Attempt, AttemptDecision, Changes, Dispatch, Objection, PreparedModel,
                    Snapshot, Turn, Version, Work)

__all__ = ['ArtifactData', 'Attempt', 'AttemptDecision', 'Changes', 'Context', 'Dispatch', 'Draft',
    'Lesson', 'Memory', 'Observation', 'Objection', 'Prerequisite', 'PreparedModel', 'Reference',
    'Repair', 'Response', 'Snapshot', 'Strategy', 'Subject', 'Turn', 'Update', 'Version', 'Work',
    'candidate_material', 'child_context', 'consumers', 'expand', 'fragments', 'interpret', 'reopen',
    'revise', 'supplied', 'update_memory']
