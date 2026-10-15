"""Shallow binary metadata; source and evidence always remain artifact references."""
from ..domain import (AdmittedBinding, AdmittedCheck, AdmittedReview, ArtifactRef, Candidate,
    CheckKind, Dependency, DependencyOrigin, FrozenRequest, Job, JobKind, OwnerVersion,
    ProofNode, ProofReference, ReceiptBinding, ResourceLimits, ResultStatus, Usage, DeliveryCertainty,
    Termination, WorkerResult)
from ..domain.encoding import load_record, record_parts
from .protocol import META_LIMIT, USAGE_FIELDS


def pack(value, limit=META_LIMIT):
    return b''.join(record_parts(value, limit=limit))


def unpack(data, limit=META_LIMIT):
    return load_record(data, limit=limit)


def ref(value):
    return [value.id, value.sha256, value.byte_count] if value is not None else None


def unref(value):
    return ArtifactRef(*value) if value is not None else None


def owner(value):
    return [value.obligation_id, value.generation]


def receipt(value):
    return [value.original_execution_id, ref(value.request_ref), ref(value.result_ref), ref(value.seal_ref)]


def unreceipt(value):
    return ReceiptBinding(value[0], *(unref(r) for r in value[1:]))


def candidate(value):
    return [value.id, owner(value.owner), value.environment_id, ref(value.source),
            [[owner(d.owner), d.alias, ref(d.interface), d.origin.value, ref(d.certificate), d.submission_id]
             for d in value.dependencies], value.revision_of, value.joint_submission_id]


def uncandidate(v):
    return Candidate(v[0], OwnerVersion(*v[1]), v[2], unref(v[3]),
        tuple(Dependency(OwnerVersion(*d[0]), d[1], unref(d[2]), DependencyOrigin(d[3]), unref(d[4]), d[5]) for d in v[4]), *v[5:])


def check(value):
    if value is None:
        return None
    return [candidate(value.candidate), value.kind.value, receipt(value.receipt), ref(value.evidence),
            [[owner(p.owner), receipt(p.receipt), ref(p.evidence)] for p in value.assembled_from]]


def uncheck(v):
    if v is None:
        return None
    return AdmittedCheck(uncandidate(v[0]), CheckKind(v[1]), unreceipt(v[2]), unref(v[3]),
        tuple(ProofReference(OwnerVersion(*p[0]), unreceipt(p[1]), unref(p[2])) for p in v[4]))


def node(value):
    return [owner(value.owner), value.environment_id, ref(value.interface),
            receipt(value.binding.receipt) if value.binding else None, check(value.selected),
            receipt(value.review.receipt) if value.review else None, check(value.assembly)]


def unnode(v):
    version, interface = OwnerVersion(*v[0]), unref(v[2])
    selected = uncheck(v[4])
    return ProofNode(version, v[1], interface,
        AdmittedBinding(version, v[1], interface, unreceipt(v[3])) if v[3] else None,
        selected, AdmittedReview(selected.candidate, unreceipt(v[5]), True) if v[5] else None, uncheck(v[6]))


def node_refs(value):
    yield value.interface
    receipts = []
    if value.binding:
        receipts.append(value.binding.receipt)
    if value.review:
        receipts.append(value.review.receipt)
    for checked in (value.selected, value.assembly):
        if checked is None:
            continue
        yield checked.candidate.source
        yield checked.evidence
        receipts.append(checked.receipt)
        for dep in checked.candidate.dependencies:
            yield dep.interface
            if dep.certificate:
                yield dep.certificate
        for proof in checked.assembled_from:
            yield proof.evidence
            receipts.append(proof.receipt)
    for r in receipts:
        yield from (r.request_ref, r.result_ref, r.seal_ref)


def request(value):
    j, l = value.job, value.job.limits
    return [j.semantic_key, j.kind.value, owner(j.owner), [ref(r) for r in j.input_refs],
            [owner(o) for o in j.expected_versions],
            [l.wall_ms, l.memory_mib, l.output_bytes, l.evidence_bytes, l.max_heartbeat],
            list(j.depends_on), value.environment_id, ref(value.operation_payload), ref(value.request_ref)]


def unrequest(v):
    return FrozenRequest(Job(v[0], JobKind(v[1]), OwnerVersion(*v[2]), tuple(unref(r) for r in v[3]),
        tuple(OwnerVersion(*o) for o in v[4]), ResourceLimits(*v[5]), tuple(v[6])), v[7], unref(v[8]), unref(v[9]))


def worker_payload(result):
    # Shared exact format already used by checker, provider and search.
    return pack({'invocation': result.invocation_id, 'request': result.request_hash,
        'status': result.status.value, 'artifacts': [r.to_dict() for r in result.staged_artifacts],
        'output': result.output.to_dict() if result.output else None,
        'diagnostics': result.diagnostics.to_dict() if result.diagnostics else None,
        'usage': [getattr(result.usage, k) for k in USAGE_FIELDS],
        'delivery': result.delivery_certainty.value, 'termination': result.termination.value}, 64 * 1024)


def load_result(payload):
    v = unpack(payload, 65536)
    if type(v) is not dict or set(v) != {'invocation', 'request', 'status', 'artifacts', 'output',
                                       'diagnostics', 'usage', 'delivery', 'termination'}:
        raise ValueError('Invalid saved worker result')
    result = WorkerResult(v['invocation'], v['request'], ResultStatus(v['status']),
        tuple(ArtifactRef.from_dict(r) for r in v['artifacts']),
        ArtifactRef.from_dict(v['output']) if v['output'] is not None else None,
        ArtifactRef.from_dict(v['diagnostics']) if v['diagnostics'] is not None else None,
        Usage(*v['usage']), DeliveryCertainty(v['delivery']), Termination(v['termination']))
    if worker_payload(result) != payload:
        raise ValueError('Noncanonical saved worker result')
    return result


def changes(value):
    # Length-framed independent records avoid wrapping already shallow nodes in
    # another object graph. Counts make the exact application unambiguous.
    groups = ([owner(o) for o in value.expected],
        [[[*owner(c.expected.owner), c.expected.revision] if c.expected else None, node(c.node)] for c in value.nodes],
        [[r.scope, r.key, r.expected, ref(r.artifact)] for r in value.records],
        [[c.scope, c.key, c.expected, c.value] for c in value.counters],
        [[request(i.request), i.priority, i.not_before_ms] for i in value.intents])
    parts = [pack(['midas-storage-changes-v1', [len(g) for g in groups],
                   list(value.roots) if value.roots is not None else None, value.graph_revision])]
    total = len(parts[0])
    for group in groups:
        for row in group:
            payload = pack(row)
            total += len(payload) + 8
            if total > 8 * 1024 * 1024:
                raise ValueError('Application exceeds byte allowance')
            parts.extend((len(payload).to_bytes(8, 'little'), payload))
    return b''.join(parts)
