"""Pure shared records and proof rules; imports only the Python standard library.

AdmittedBinding, AdmittedCheck and AdmittedReview are trusted internal handoffs,
not decoders for model output. Their producers must validate receipt provenance.
"""

from .identity import (
    BoundDependency, ObligationIdentity, decode_interface, encode_interface, load_identity,
    validate_dependencies, validate_identity, validate_signature,
)
from .proofs import (
    AdmittedBinding, AdmittedCheck, AdmittedReview, CheckKind, ProofAlias, ProofGraph, ProofNode,
    ProofReference, ProofState, Retirement, accept_assembly, accept_binding, accept_checked_proof,
    accept_joint, joint_coordinator, joint_targets, retire_plan, review_digest, target_id,
    validate_joint_submission,
)
from .records import (
    ArtifactReader, ArtifactRef, ArtifactStaging, Candidate, CapacityGrant, DeliveryCertainty, Dependency, DependencyOrigin,
    DomainError, FrozenRequest, Invocation, Job, JobKind, OwnerVersion, ReceiptBinding, ResourceLimits,
    ResultStatus, Session, Termination, Usage, WorkerProgress, WorkerResult, obligation_id,
)
from .selection import (
    ByteSpan, Declaration, FrozenSelection, SelectedTargets, SelectionInputs, SelectionRequest,
    Signature, SourceBinding, encode_selection, freeze_selection, load_selection, select_targets,
)
from .submission import encode_submission, lexical_scope, load_submission, resolve_entry, validate_submission


__all__ = [
    "ArtifactStaging", "CapacityGrant", "WorkerProgress",
    "AdmittedBinding", "AdmittedCheck", "AdmittedReview", "ArtifactReader", "ArtifactRef",
    "Candidate", "CheckKind", "DeliveryCertainty", "Dependency", "DependencyOrigin", "DomainError",
    "FrozenRequest", "Invocation", "Job", "JobKind", "OwnerVersion", "ProofGraph",
    "ProofNode", "ProofReference", "ProofState", "ReceiptBinding", "ResourceLimits",
    "ResultStatus", "Retirement", "Session", "Termination", "Usage", "WorkerResult",
    "accept_assembly", "accept_binding", "accept_checked_proof", "obligation_id", "retire_plan",
    "BoundDependency", "ByteSpan", "Declaration", "FrozenSelection", "ObligationIdentity",
    "ProofAlias", "SelectedTargets", "SelectionInputs", "SelectionRequest", "Signature", "SourceBinding",
    "accept_joint", "decode_interface", "encode_interface", "freeze_selection", "joint_coordinator",
    "joint_targets", "lexical_scope", "load_identity", "load_selection", "load_submission",
    "resolve_entry", "review_digest", "select_targets", "target_id", "validate_dependencies",
    "validate_identity", "validate_joint_submission", "validate_signature", "validate_submission", "encode_selection",
    "encode_submission",
]
