"""Pure proof selection and retirement over an explicit graph snapshot.

Admitted records are internal handoffs from checker/export receipt validation.
They are not wire formats and must never be decoded from untrusted worker output.
This module checks graph bindings; it does not verify Lean certificates or seals.
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .records import (
    ArtifactRef, Candidate, DependencyOrigin, DomainError, OwnerVersion, ReceiptBinding, identifier,
    instance, items, natural, uuid,
)
from .selection import FrozenSelection


def target_id(run_id: str, selection_id: str, ordinal: int) -> str:
    """Stable persisted root identity, independent of storage or current session."""
    from uuid import UUID, uuid5
    uuid(run_id)
    identifier(selection_id, 'sel_')
    natural(ordinal, 'Target ordinal')
    return str(uuid5(UUID(run_id), f'proof-target:{selection_id}:{ordinal}'))


@dataclass(frozen=True, slots=True)
class ProofAlias:
    id: str
    owner: OwnerVersion
    label: str
    submission: ArtifactRef
    target_id: str | None = None
    plan_id: str | None = None

    def __post_init__(self):
        from .identity import bounded_text
        uuid(self.id)
        instance(self.owner, OwnerVersion)
        bounded_text(self.label, 'alias', 256)
        instance(self.submission, ArtifactRef)
        if (self.target_id is None) == (self.plan_id is None):
            raise DomainError('Alias requires exactly one proposing target or plan')
        uuid(self.target_id if self.target_id is not None else self.plan_id)


def review_digest(attempt_id, obligation_id, candidate, review, dependencies):
    """Bind saved review bytes to the immutable attempt and ordered interfaces.

    Dependencies are fully validated structural BoundDependency records supplied
    by proposal admission. Raw fields are hashed incrementally with lengths.
    """
    from hashlib import sha256
    from .encoding import fields, hash_field, load_json
    from .identity import BoundDependency, bounded_text
    uuid(attempt_id)
    identifier(obligation_id, 'obl_')
    instance(candidate, ArtifactRef)
    items(dependencies, BoundDependency)
    value = load_json(review)
    fields(value, ('decision', 'rationale'))
    if value['decision'] != 'approve':
        raise DomainError('Supplied review must approve')
    bounded_text(value['rationale'], 'review rationale', 16 * 1024)
    if len(dependencies) > 128:
        raise DomainError('Review supports at most 128 dependencies')
    digest = sha256(b'MidasReview\x02')
    for text in (attempt_id, obligation_id, candidate.id, str(candidate.byte_count), candidate.sha256):
        hash_field(digest, text.encode('ascii'))
    hash_field(digest, review)
    for dep in dependencies:
        for value in (dep.identity.id.encode('ascii'), dep.parameter, dep.interface, dep.origin.encode('ascii')):
            hash_field(digest, value)
    return digest.hexdigest()


class ProofState(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    CONDITIONAL = "CONDITIONAL"
    VERIFIED = "VERIFIED"


class CheckKind(str, Enum):
    DIRECT = "DIRECT"
    CONDITIONAL = "CONDITIONAL"
    ASSEMBLY = "ASSEMBLY"


@dataclass(frozen=True, slots=True)
class AdmittedBinding:
    owner: OwnerVersion
    environment_id: str
    interface: ArtifactRef
    receipt: ReceiptBinding

    def __post_init__(self) -> None:
        instance(self.owner, OwnerVersion)
        identifier(self.environment_id, "env_")
        instance(self.interface, ArtifactRef)
        instance(self.receipt, ReceiptBinding)


@dataclass(frozen=True, slots=True)
class ProofReference:
    owner: OwnerVersion
    receipt: ReceiptBinding
    evidence: ArtifactRef

    def __post_init__(self) -> None:
        instance(self.owner, OwnerVersion)
        instance(self.receipt, ReceiptBinding)
        instance(self.evidence, ArtifactRef)


@dataclass(frozen=True, slots=True)
class AdmittedCheck:
    candidate: Candidate
    kind: CheckKind
    receipt: ReceiptBinding
    evidence: ArtifactRef
    assembled_from: tuple[ProofReference, ...] = ()

    def __post_init__(self) -> None:
        instance(self.candidate, Candidate)
        instance(self.kind, CheckKind)
        instance(self.receipt, ReceiptBinding)
        instance(self.evidence, ArtifactRef)
        items(self.assembled_from, ProofReference)
        if (self.kind == CheckKind.DIRECT) != (not self.candidate.logical_dependencies):
            raise DomainError("Check kind differs from candidate dependencies")
        if self.kind == CheckKind.ASSEMBLY:
            if tuple(ref.owner for ref in self.assembled_from) != tuple(dep.owner for dep in self.candidate.logical_dependencies):
                raise DomainError("Assembly must bind every child in dependency order")
        elif self.assembled_from:
            raise DomainError("Only assembly checks carry child proofs")


@dataclass(frozen=True, slots=True)
class AdmittedReview:
    candidate: Candidate
    receipt: ReceiptBinding
    approved: bool

    def __post_init__(self) -> None:
        instance(self.candidate, Candidate)
        instance(self.receipt, ReceiptBinding)
        if type(self.approved) is not bool:
            raise DomainError("Review approval must be boolean")


@dataclass(frozen=True, slots=True)
class ProofNode:
    owner: OwnerVersion
    environment_id: str
    interface: ArtifactRef
    binding: AdmittedBinding | None = None
    selected: AdmittedCheck | None = None
    review: AdmittedReview | None = None
    assembly: AdmittedCheck | None = None

    def __post_init__(self) -> None:
        instance(self.owner, OwnerVersion)
        identifier(self.environment_id, "env_")
        instance(self.interface, ArtifactRef)
        if self.binding is not None:
            instance(self.binding, AdmittedBinding)
            if (self.binding.owner != self.owner or self.binding.environment_id != self.environment_id
                    or self.binding.interface != self.interface):
                raise DomainError("Binding differs from node")
        if self.selected is None:
            if self.review is not None or self.assembly is not None:
                raise DomainError("Review or assembly has no selected candidate")
            return
        instance(self.selected, AdmittedCheck)
        candidate = self.selected.candidate
        if candidate.owner != self.owner or candidate.environment_id != self.environment_id:
            raise DomainError("Selected check differs from node")
        if self.selected.kind == CheckKind.DIRECT:
            if self.review is not None or self.assembly is not None:
                raise DomainError("Direct proofs have no plan review or assembly")
        elif self.selected.kind == CheckKind.CONDITIONAL:
            instance(self.review, AdmittedReview)
            if self.review.candidate != candidate or not self.review.approved:
                raise DomainError("Conditional selection requires a current approved review")
            if self.assembly is not None:
                instance(self.assembly, AdmittedCheck)
                if self.assembly.kind != CheckKind.ASSEMBLY or self.assembly.candidate != candidate:
                    raise DomainError("Assembly differs from selected candidate")
        else:
            raise DomainError("Assembly cannot replace the original conditional check")

    @property
    def state(self) -> ProofState:
        if self.selected is None:
            return ProofState.UNRESOLVED
        if self.selected.kind == CheckKind.DIRECT or self.assembly is not None:
            return ProofState.VERIFIED
        return ProofState.CONDITIONAL

    @property
    def proof(self) -> ProofReference | None:
        if self.state != ProofState.VERIFIED:
            return None
        checked = self.assembly or self.selected
        return ProofReference(self.owner, checked.receipt, checked.evidence)


def _reachable(nodes: dict[str, ProofNode], roots: tuple[str, ...]) -> frozenset[str]:
    pending = list(roots)
    seen = set()
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            selected = nodes[key].selected
            if selected is not None:
                pending.extend(dep.owner.obligation_id for dep in selected.candidate.dependencies)
    return frozenset(seen)


@dataclass(frozen=True, slots=True)
class ProofGraph:
    revision: int
    roots: tuple[str, ...]
    nodes: tuple[ProofNode, ...]
    retired_candidates: tuple[str, ...] = ()
    _by_id: Mapping[str, ProofNode] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        natural(self.revision, "Graph revision")
        items(self.roots, str)
        items(self.nodes, ProofNode)
        items(self.retired_candidates, str)
        for key in self.roots:
            identifier(key, "obl_")
        for key in self.retired_candidates:
            uuid(key)
        nodes = {node.owner.obligation_id: node for node in self.nodes}
        if len(nodes) != len(self.nodes) or len(set(self.roots)) != len(self.roots):
            raise DomainError("Duplicate graph node or root")
        if len(set(self.retired_candidates)) != len(self.retired_candidates):
            raise DomainError("Duplicate retired candidate")
        if any(root not in nodes for root in self.roots):
            raise DomainError("Missing graph root")
        retired = set(self.retired_candidates)
        selected_ids = set()
        incoming = dict.fromkeys(nodes, 0)
        edges = {}
        for key, node in nodes.items():
            edges[key] = []
            if node.selected is None:
                continue
            candidate = node.selected.candidate
            if candidate.id in selected_ids:
                raise DomainError("Candidate ID is selected by multiple owners")
            selected_ids.add(candidate.id)
            if candidate.id in retired:
                raise DomainError("Retired candidate is still selected")
            for dep in candidate.dependencies:
                child = nodes.get(dep.owner.obligation_id)
                if (child is None or child.owner != dep.owner or child.environment_id != node.environment_id
                        or child.interface != dep.interface):
                    raise DomainError("Dependency is missing or has changed version, environment, or interface")
                edges[key].append(dep.owner.obligation_id)
                incoming[dep.owner.obligation_id] += 1
            if node.assembly is not None:
                actual = tuple(nodes[dep.owner.obligation_id].proof for dep in candidate.logical_dependencies)
                if actual != node.assembly.assembled_from:
                    raise DomainError("Assembly differs from current verified children")
        # Iterative traversal supports deep graphs without Python recursion.
        ready = [key for key, count in incoming.items() if count == 0]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for child in edges[key]:
                incoming[child] -= 1
                if incoming[child] == 0:
                    ready.append(child)
        if visited != len(nodes):
            raise DomainError("Selected proof dependencies contain a cycle")
        object.__setattr__(self, '_by_id', MappingProxyType(nodes))

    def node(self, owner: OwnerVersion) -> ProofNode:
        instance(owner, OwnerVersion)
        node = self._by_id.get(owner.obligation_id)
        if node is None:
            raise DomainError("Unknown proof owner")
        if node.owner != owner:
            raise DomainError("Stale owner generation")
        return node

    def reachable(self) -> frozenset[str]:
        return _reachable(self._by_id, self.roots)

    def _replace(self, node: ProofNode) -> "ProofGraph":
        if self.node(node.owner) == node:
            return self
        return replace(self, revision=self.revision + 1,
                       nodes=tuple(node if old.owner == node.owner else old for old in self.nodes))


def accept_binding(graph: ProofGraph, binding: AdmittedBinding) -> ProofGraph:
    instance(graph, ProofGraph)
    instance(binding, AdmittedBinding)
    node = graph.node(binding.owner)
    if node.binding is not None and node.binding != binding:
        raise DomainError("Existing checked binding cannot be replaced")
    # ProofNode checks the exact environment/interface; proof selection is preserved.
    return graph._replace(replace(node, binding=binding))


def accept_checked_proof(graph: ProofGraph, checked: AdmittedCheck,
                         review: AdmittedReview | None = None) -> ProofGraph:
    instance(graph, ProofGraph)
    instance(checked, AdmittedCheck)
    if checked.kind == CheckKind.ASSEMBLY:
        raise DomainError("Use accept_assembly for checked composition")
    candidate = checked.candidate
    if candidate.joint_submission_id is not None:
        raise DomainError('Joint candidates must be accepted atomically')
    node = graph.node(candidate.owner)
    if candidate.id in graph.retired_candidates:
        raise DomainError("Retired candidate cannot be reactivated")
    if node.selected is not None:
        if node.selected == checked and node.review == review:
            return graph
        raise DomainError("Retire the existing selection before replacing it")
    return graph._replace(replace(node, selected=checked, review=review))


def accept_assembly(graph: ProofGraph, checked: AdmittedCheck) -> ProofGraph:
    instance(graph, ProofGraph)
    instance(checked, AdmittedCheck)
    node = graph.node(checked.candidate.owner)
    if node.selected is None or checked.kind != CheckKind.ASSEMBLY:
        raise DomainError("Assembly requires a selected conditional candidate")
    if node.assembly is not None and node.assembly != checked:
        raise DomainError("Existing assembly cannot be replaced")
    return graph._replace(replace(node, assembly=checked))


@dataclass(frozen=True, slots=True)
class Retirement:
    expected_revision: int
    graph: ProofGraph
    cancel_after_commit: tuple[OwnerVersion, ...]

    def __post_init__(self) -> None:
        natural(self.expected_revision, "Expected graph revision")
        instance(self.graph, ProofGraph)
        items(self.cancel_after_commit, OwnerVersion)
        if self.graph.revision != self.expected_revision + 1:
            raise DomainError("Retirement must advance exactly one graph revision")
        keys = set()
        for old in self.cancel_after_commit:
            if old.obligation_id in keys:
                raise DomainError("Duplicate cancellation owner")
            keys.add(old.obligation_id)
            current = self.graph.node(OwnerVersion(old.obligation_id, old.generation + 1))
            if current.state != ProofState.UNRESOLVED:
                raise DomainError("Retired owner still has a selected proof")


def retire_plan(graph: ProofGraph, owner: OwnerVersion, candidate_id: str) -> Retirement:
    """Propose an atomic graph replacement and cancellation of newly unreachable work.

    Storage must compare expected_revision when committing. Only after that commit
    may the controller cancel the returned old owner generations. History remains
    in receipts; retired candidate IDs prevent their late reactivation.
    """
    instance(graph, ProofGraph)
    uuid(candidate_id)
    selected = graph.node(owner).selected
    if selected is None or selected.candidate.id != candidate_id or selected.kind != CheckKind.CONDITIONAL:
        raise DomainError("Retirement differs from the current selected plan")
    nodes = {node.owner.obligation_id: node for node in graph.nodes}
    before = _reachable(nodes, graph.roots)
    retired = set(graph.retired_candidates)

    def clear(node: ProofNode, *, fence: bool = False) -> ProofNode:
        if node.selected is not None:
            retired.add(node.selected.candidate.id)
        return replace(node, selected=None, review=None, assembly=None,
                       owner=OwnerVersion(node.owner.obligation_id, node.owner.generation + 1) if fence else node.owner,
                       binding=None if fence else node.binding)

    nodes[owner.obligation_id] = clear(nodes[owner.obligation_id])
    removed = before - _reachable(nodes, graph.roots)
    cancel = tuple(node.owner for node in graph.nodes if node.owner.obligation_id in removed)
    for key in removed:
        nodes[key] = clear(nodes[key], fence=True)
    # Disconnected historical selections may also reference a fenced generation.
    for key, node in nodes.items():
        if node.selected and any(nodes[dep.owner.obligation_id].owner != dep.owner
                                 for dep in node.selected.candidate.dependencies):
            nodes[key] = clear(node)
    # Retiring a verified plan invalidates checked assemblies of its ancestors,
    # while retaining their conditional checks and reviews for later reassembly.
    parents = {key: set() for key in nodes}
    for key, node in nodes.items():
        if node.selected:
            for dep in node.selected.candidate.dependencies:
                parents[dep.owner.obligation_id].add(key)
    pending = list(nodes)
    while pending:
        key = pending.pop()
        node = nodes[key]
        if node.assembly is not None:
            actual = tuple(nodes[dep.owner.obligation_id].proof for dep in node.selected.candidate.logical_dependencies)
            if actual != node.assembly.assembled_from:
                nodes[key] = replace(node, assembly=None)
                pending.extend(parents[key])
    updated = replace(graph, revision=graph.revision + 1, nodes=tuple(nodes.values()),
                      retired_candidates=tuple(sorted(retired)))
    return Retirement(graph.revision, updated, cancel)





def joint_targets(selection):
    instance(selection, FrozenSelection)
    chosen = selection.selected.targets
    definitions = {t.constant for t in chosen if t.kind == 'def'}
    if not definitions or not any(definitions.intersection(t.type_references) for t in chosen):
        return ()
    if len(chosen) > 13:
        raise ValueError('A joint definition/property candidate supports at most 13 selected targets')
    return tuple(zip(chosen, selection.identities))


def joint_coordinator(selection):
    group = joint_targets(selection)
    return next((identity.id for target, identity in group if target.kind != 'def'),
                group[-1][1].id if group else None)



def validate_joint_submission(selection, packet, submission):
    if not packet.get('joint_problem'):
        return
    if packet['obligation'] != joint_coordinator(selection):
        raise ValueError('Joint coordinator differs from frozen selection')
    if packet['required_targets'] != [t.selector for t, _ in joint_targets(selection)]:
        raise ValueError('Joint target set differs from frozen selection')
    if not joint_targets(selection):
        return
    entries = submission['entries']
    group = joint_targets(selection)
    if len(entries) != len(group):
        raise ValueError('Joint candidates require complete target proofs; use local Lean helper lemmas inside each proof')
    earlier = {}
    for entry, (target, _) in zip(entries, group):
        if entry['subject'] != {'kind':'target', 'name':target.selector}:
            raise ValueError('Joint target order differs from frozen selection')
        for dep in entry['dependencies']:
            ref = dep['entry']
            if ref['submission_id'] != submission['submission_id'] or ref['key'] not in earlier:
                raise ValueError('Joint dependencies must reference earlier definitions in this candidate')
        # Freeze the complete preceding definition context, including values
        # referenced transitively by another definition's certificate.
        expected = set(earlier)
        supplied = {dep['entry']['key'] for dep in entry['dependencies']}
        if not expected <= supplied:
            raise ValueError('Include every earlier selected definition as an explicit dependency')
        if target.kind == 'def':
            earlier[entry['key']] = target.constant


def accept_joint(graph: ProofGraph, selection: FrozenSelection, submission_id: str,
                 checks: tuple[AdmittedCheck, ...]) -> ProofGraph:
    """Select every required root in one replacement, or raise without changes.

    The checker must have replayed each closed definition certificate in this
    exact submission. Its evidence can supply subsequent roots here, but cannot
    escape the group through ordinary accept_checked_proof.
    """
    instance(graph, ProofGraph)
    uuid(submission_id)
    items(checks, AdmittedCheck)
    group = joint_targets(selection)
    if not group or len(checks) != len(group):
        raise DomainError('Joint acceptance requires every selected target')
    updates = {}
    definitions = {}
    all_unchanged = True
    for checked, (target, identity) in zip(checks, group):
        candidate = checked.candidate
        if (candidate.owner.obligation_id != identity.id or candidate.environment_id != identity.environment_id
                or candidate.joint_submission_id != submission_id or checked.kind != CheckKind.DIRECT):
            raise DomainError('Joint check differs from target, environment, submission, or direct-proof kind')
        node = graph.node(candidate.owner)
        if candidate.id in graph.retired_candidates:
            raise DomainError('Retired joint candidate')
        supplied = set()
        for dep in candidate.dependencies:
            prior = definitions.get(dep.owner.obligation_id)
            if (dep.origin != DependencyOrigin.CLOSED_DEFINITION or prior is None
                    or dep.owner != prior.candidate.owner or dep.submission_id != submission_id
                    or dep.certificate != prior.evidence):
                raise DomainError('Joint dependency must use an earlier definition certificate from this submission')
            supplied.add(dep.owner.obligation_id)
        if supplied != set(definitions):
            raise DomainError('Joint check must include every earlier selected definition')
        if node.selected is not None and node.selected != checked:
            raise DomainError('Joint selection conflicts with current proof')
        all_unchanged &= node.selected == checked
        updates[node.owner.obligation_id] = replace(node, selected=checked)
        if target.kind == 'def':
            definitions[node.owner.obligation_id] = checked
    if all_unchanged:
        return graph
    # A partial selection indicates a broken atomic commit, not work to silently adopt.
    if any(graph.node(checked.candidate.owner).selected is not None for checked in checks):
        raise DomainError('Partially selected joint candidate')
    return replace(graph, revision=graph.revision + 1,
                   nodes=tuple(updates.get(node.owner.obligation_id, node) for node in graph.nodes))
