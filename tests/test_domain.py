"""Pure domain acceptance cases. No compiler, process isolation, or external I/O."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import ast
from pathlib import Path
import sys
import unittest
from uuid import NAMESPACE_URL, uuid5

from midas.domain import (
    AdmittedBinding, AdmittedCheck, AdmittedReview, ArtifactRef, Candidate,
    CheckKind, DeliveryCertainty, Dependency, DomainError, FrozenRequest,
    Invocation, Job, JobKind, OwnerVersion, ProofGraph, ProofNode, ProofState,
    ReceiptBinding, ResourceLimits, ResultStatus, Session, Termination, Usage,
    WorkerResult, accept_assembly, accept_binding, accept_checked_proof,
    obligation_id, retire_plan,
)


def uid(label):
    return str(uuid5(NAMESPACE_URL, label))


def artifact(label):
    payload = label.encode()
    return ArtifactRef(uid(label), sha256(payload).hexdigest(), len(payload))


ENV = "env_" + sha256(b"environment").hexdigest()


def node(label):
    return ProofNode(OwnerVersion("obl_" + sha256(label.encode()).hexdigest(), 0), ENV, artifact(label))


def receipt(label):
    return ReceiptBinding(uid(label), artifact(label + ":request"),
                          artifact(label + ":result"), artifact(label + ":seal"))


def candidate(subject, children=(), *, label="candidate"):
    return Candidate(uid(subject.owner.obligation_id + label), subject.owner, subject.environment_id,
                     artifact(label), tuple(Dependency(child.owner, f"child{i}", child.interface)
                                            for i, child in enumerate(children)))


def check(value):
    return AdmittedCheck(value, CheckKind.CONDITIONAL if value.dependencies else CheckKind.DIRECT,
                         receipt(value.id), artifact(value.id + ":evidence"))


def review(value):
    return AdmittedReview(value, receipt(value.id + ":review"), True)


def select(graph, subject, children=()):
    value = candidate(subject, children)
    return accept_checked_proof(graph, check(value), review(value) if children else None)


def assemble(graph, subject):
    selected = graph.node(subject.owner).selected
    references = tuple(graph.node(dep.owner).proof for dep in selected.candidate.dependencies)
    checked = AdmittedCheck(selected.candidate, CheckKind.ASSEMBLY,
                            receipt(selected.candidate.id + ":assembly"), artifact("assembly"), references)
    return accept_assembly(graph, checked)


class ValueTests(unittest.TestCase):
    def test_domain_imports_only_itself_and_the_standard_library(self):
        domain = Path(__file__).resolve().parents[1] / "midas" / "domain"
        for path in domain.glob("*.py"):
            for statement in ast.walk(ast.parse(path.read_text())):
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names, str(path))
                elif isinstance(statement, ast.ImportFrom):
                    if statement.level:
                        self.assertEqual(statement.level, 1, str(path))
                    else:
                        self.assertIn(statement.module.split(".")[0], sys.stdlib_module_names, str(path))

    def test_artifact_wire_shape_and_exact_bounded_bytes(self):
        ref = artifact("hello")
        self.assertEqual(ref.to_dict(), {"id": uid("hello"), "sha256": sha256(b"hello").hexdigest(), "bytes": 5})
        self.assertEqual(ArtifactRef.from_dict(ref.to_dict()), ref)
        self.assertEqual(ref.verify(b"hello", max_bytes=5), b"hello")
        for payload, bound in ((b"hello", 4), (b"Hello", 5), (b"hell", 5), (bytearray(b"hello"), 5)):
            with self.subTest(payload=payload, bound=bound), self.assertRaises(DomainError):
                ref.verify(payload, max_bytes=bound)
        with self.assertRaises(DomainError):
            ArtifactRef.from_dict({**ref.to_dict(), "authority": "trusted"})

    def test_invalid_scalars_and_mutable_containers_are_rejected(self):
        ref = artifact("hello")
        for changes in ({"id": ref.id.upper()}, {"sha256": "X" * 64}, {"byte_count": True},
                        {"byte_count": -1}, {"byte_count": 2**63}):
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                replace(ref, **changes)
        with self.assertRaises(FrozenInstanceError):
            ref.byte_count = 9
        with self.assertRaises(DomainError):
            replace(candidate(node("root")), dependencies=[])
        with self.assertRaises(DomainError):
            Session(uid("run"), uid("session"), 0)
        with self.assertRaises(DomainError):
            OwnerVersion(node("root").owner.obligation_id, True)

    def test_unknown_usage_is_distinct_from_zero(self):
        self.assertIsNone(Usage().input_tokens)
        self.assertEqual(Usage(0, 0, 0).input_tokens, 0)
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(DomainError):
                Usage(cost_microusd=value)

    def test_identity_hashes_exact_envelope_bytes_without_source_normalization(self):
        self.assertEqual(obligation_id(b"abc"), "obl_ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertNotEqual(obligation_id(b"abc"), obligation_id(b"abc\n"))
        with self.assertRaises(DomainError):
            obligation_id("abc")

    def test_candidate_dependencies_and_revision_bindings(self):
        root, first, second = node("root"), node("first"), node("second")
        original = candidate(root, (first, second))
        self.assertEqual(tuple(dep.owner for dep in original.dependencies), (first.owner, second.owner))
        for dependencies in (
            (Dependency(root.owner, "self", root.interface),),
            (original.dependencies[0], replace(original.dependencies[1], alias="child0")),
            (original.dependencies[0], Dependency(replace(first.owner, generation=1), "other", first.interface)),
        ):
            with self.subTest(dependencies=dependencies), self.assertRaises(DomainError):
                replace(original, dependencies=dependencies)
        revised = replace(original, id=uid("revision"), revision_of=original.id)
        revised.validate_revision(original)
        for changes in ({"owner": replace(root.owner, generation=1)}, {"environment_id": "env_" + "a" * 64}):
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                replace(revised, **changes).validate_revision(original)


class WorkTests(unittest.TestCase):
    def test_recursion_depth_is_a_positive_configurable_maximum(self):
        limits = ResourceLimits(1000, 256, 1000, 1000)
        self.assertEqual(limits.max_rec_depth, 1000)
        self.assertEqual(replace(limits, max_rec_depth=256).max_rec_depth, 256)
        for value in (0, -1, True, 1.5, None, 2**63):
            with self.subTest(value=value), self.assertRaises(DomainError):
                replace(limits, max_rec_depth=value)

    def test_heartbeat_limits_accept_zero_and_reject_invalid_counts(self):
        limits = ResourceLimits(1000, 256, 1000, 1000)
        self.assertEqual(limits.max_heartbeat, 2_000_000)
        self.assertEqual(replace(limits, max_heartbeat=0).max_heartbeat, 0)
        for value in (-1, True, 1.5, None, 2**63):
            with self.subTest(value=value), self.assertRaises(DomainError):
                replace(limits, max_heartbeat=value)

    def setUp(self):
        self.owner = node("root").owner
        self.job = Job("proof:root", JobKind.CHECK_PROOF, self.owner, (artifact("input"),),
                       (self.owner,), ResourceLimits(1000, 256, 1000, 1000))
        self.request = FrozenRequest(self.job, ENV, artifact("operation"), artifact("request"))
        self.invocation = Invocation(uid("execution"), Session(uid("run"), uid("session"), 1),
                                     self.request, artifact("reservation"))
        self.result = WorkerResult(uid("execution"), self.request.request_hash, ResultStatus.SUCCEEDED,
                                   (artifact("output"),), artifact("output"), None, Usage(),
                                   DeliveryCertainty.DELIVERED, Termination.TERMINATED)

    def test_job_requires_unique_current_owner_fence(self):
        for expected in ((), (replace(self.owner, generation=1),), (self.owner, self.owner)):
            with self.subTest(expected=expected), self.assertRaises(DomainError):
                replace(self.job, expected_versions=expected)
        with self.assertRaises(DomainError):
            replace(self.job, depends_on=(self.job.semantic_key,))
        with self.assertRaises(DomainError):
            replace(self.job, kind="CHECK_PROOF")

    def test_result_requires_exact_invocation_and_request(self):
        self.result.validate_for(self.invocation)
        for changes in ({"invocation_id": uid("another execution")}, {"request_hash": "a" * 64}):
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                replace(self.result, **changes).validate_for(self.invocation)

    def test_result_artifacts_and_delivery_remain_explicit(self):
        for changes in ({"output": artifact("other")}, {"staged_artifacts": ()},
                        {"staged_artifacts": (artifact("output"), artifact("output"))},
                        {"delivery_certainty": DeliveryCertainty.UNKNOWN}):
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                replace(self.result, **changes)
        cancelled = replace(self.result, status=ResultStatus.CANCELLED,
                            delivery_certainty=DeliveryCertainty.UNKNOWN, termination=Termination.UNKNOWN)
        self.assertIsNone(cancelled.usage.cost_microusd)
        self.assertEqual(cancelled.termination, Termination.UNKNOWN)

    def test_worker_success_is_not_proof_evidence(self):
        root = node("root")
        graph = ProofGraph(0, (root.owner.obligation_id,), (root,))
        with self.assertRaises(DomainError):
            accept_checked_proof(graph, self.result)
        self.assertEqual(graph.node(root.owner).state, ProofState.UNRESOLVED)


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.root, self.child = node("root"), node("child")
        self.graph = ProofGraph(0, (self.root.owner.obligation_id,), (self.root, self.child))

    def test_binding_does_not_promote_proof_state(self):
        binding = AdmittedBinding(self.root.owner, ENV, self.root.interface, receipt("binding"))
        graph = accept_binding(self.graph, binding)
        self.assertEqual(graph.node(self.root.owner).state, ProofState.UNRESOLVED)
        self.assertIs(accept_binding(graph, binding), graph)
        with self.assertRaises(DomainError):
            accept_binding(self.graph, replace(binding, interface=artifact("wrong")))

    def test_direct_proof_is_immutable_and_idempotent(self):
        checked = check(candidate(self.root))
        graph = accept_checked_proof(self.graph, checked)
        self.assertEqual(graph.node(self.root.owner).state, ProofState.VERIFIED)
        self.assertEqual(self.graph.node(self.root.owner).state, ProofState.UNRESOLVED)
        self.assertIs(accept_checked_proof(graph, checked), graph)
        with self.assertRaises(DomainError):
            accept_checked_proof(graph, check(candidate(self.root, label="alternative")))

    def test_conditional_proof_needs_matching_approved_review(self):
        value = candidate(self.root, (self.child,))
        checked = check(value)
        for verdict in (None, replace(review(value), approved=False), review(replace(value, source=artifact("edited")))):
            with self.subTest(verdict=verdict), self.assertRaises(DomainError):
                accept_checked_proof(self.graph, checked, verdict)
        graph = accept_checked_proof(self.graph, checked, review(value))
        self.assertEqual(graph.node(self.root.owner).state, ProofState.CONDITIONAL)
        self.assertIsNone(graph.node(self.root.owner).proof)

    def test_stale_and_mismatched_check_inputs_are_rejected(self):
        value = candidate(self.root, (self.child,))
        invalid = (
            replace(value, owner=replace(self.root.owner, generation=1)),
            replace(value, environment_id="env_" + "a" * 64),
            replace(value, dependencies=(replace(value.dependencies[0], interface=artifact("wrong")),)),
            replace(value, dependencies=(replace(value.dependencies[0], owner=replace(self.child.owner, generation=1)),)),
        )
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(DomainError):
                accept_checked_proof(self.graph, check(changed), review(changed))

    def test_selected_plan_cycle_is_rejected(self):
        graph = select(self.graph, self.root, (self.child,))
        with self.assertRaisesRegex(DomainError, "cycle"):
            select(graph, self.child, (self.root,))

    def test_candidate_identity_cannot_be_rebound_to_another_owner(self):
        value = candidate(self.root)
        graph = accept_checked_proof(self.graph, check(value))
        with self.assertRaisesRegex(DomainError, "multiple owners"):
            accept_checked_proof(graph, check(replace(value, owner=self.child.owner)))

    def test_verified_children_do_not_replace_checked_assembly(self):
        graph = select(self.graph, self.root, (self.child,))
        graph = select(graph, self.child)
        self.assertEqual(graph.node(self.root.owner).state, ProofState.CONDITIONAL)
        graph = assemble(graph, self.root)
        self.assertEqual(graph.node(self.root.owner).state, ProofState.VERIFIED)
        self.assertIs(accept_assembly(graph, graph.node(self.root.owner).assembly), graph)

    def test_assembly_rejects_unverified_or_different_child_evidence(self):
        graph = select(self.graph, self.root, (self.child,))
        checked_child = check(candidate(self.child))
        from midas.domain import ProofReference
        claimed = ProofReference(self.child.owner, checked_child.receipt, checked_child.evidence)
        assembly = AdmittedCheck(graph.node(self.root.owner).selected.candidate, CheckKind.ASSEMBLY,
                                receipt("assembly"), artifact("assembly"), (claimed,))
        with self.assertRaises(DomainError):
            accept_assembly(graph, assembly)
        graph = accept_checked_proof(graph, checked_child)
        with self.assertRaises(DomainError):
            accept_assembly(graph, replace(assembly, assembled_from=(replace(claimed, receipt=receipt("other")),)))
        self.assertEqual(accept_assembly(graph, assembly).node(self.root.owner).state, ProofState.VERIFIED)

    def test_retirement_fences_only_newly_unreachable_owners(self):
        other, shared, unique, already_idle = node("other"), node("shared"), node("unique"), node("idle")
        graph = ProofGraph(0, (self.root.owner.obligation_id, other.owner.obligation_id),
                           (self.root, other, shared, unique, already_idle))
        graph = select(graph, self.root, (shared, unique))
        graph = select(graph, other, (shared,))
        graph = select(graph, unique)
        plan = graph.node(self.root.owner).selected
        change = retire_plan(graph, self.root.owner, plan.candidate.id)
        self.assertEqual(change.expected_revision, graph.revision)
        self.assertEqual(change.cancel_after_commit, (unique.owner,))
        self.assertEqual(change.graph.node(shared.owner), graph.node(shared.owner))
        self.assertEqual(change.graph.node(already_idle.owner), already_idle)
        self.assertEqual(change.graph.node(self.root.owner).state, ProofState.UNRESOLVED)
        self.assertEqual(change.graph.node(replace(unique.owner, generation=1)).state, ProofState.UNRESOLVED)
        with self.assertRaises(DomainError):
            accept_checked_proof(change.graph, plan, graph.node(self.root.owner).review)
        self.assertEqual(graph.node(self.root.owner).state, ProofState.CONDITIONAL)

    def test_retiring_assembled_child_demotes_ancestor_assembly(self):
        leaf = node("leaf")
        graph = replace(self.graph, nodes=(*self.graph.nodes, leaf))
        graph = select(graph, self.root, (self.child,))
        graph = select(graph, self.child, (leaf,))
        graph = select(graph, leaf)
        graph = assemble(assemble(graph, self.child), self.root)
        plan = graph.node(self.child.owner).selected
        change = retire_plan(graph, self.child.owner, plan.candidate.id)
        self.assertEqual(change.graph.node(self.root.owner).state, ProofState.CONDITIONAL)
        self.assertEqual(change.graph.node(self.root.owner).selected, graph.node(self.root.owner).selected)
        self.assertEqual(change.graph.node(self.child.owner).state, ProofState.UNRESOLVED)
        self.assertEqual(change.cancel_after_commit, (leaf.owner,))

    def test_deep_graph_uses_iterative_traversal(self):
        nodes = [node(str(index)) for index in range(1100)]
        selected = []
        for index, subject in enumerate(nodes):
            children = (nodes[index + 1],) if index + 1 < len(nodes) else ()
            value = candidate(subject, children)
            selected.append(replace(subject, selected=check(value), review=review(value) if children else None))
        graph = ProofGraph(0, (nodes[0].owner.obligation_id,), tuple(selected))
        self.assertEqual(len(graph.reachable()), len(nodes))


if __name__ == "__main__":
    unittest.main()
