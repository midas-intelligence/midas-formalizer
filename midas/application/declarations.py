"""Resolve accepted reviewer suggestions before a child's next prover call."""
from dataclasses import replace
from hashlib import sha256

from ..checker import CompilerRequest
from ..controller import Effect, artifact
from ..domain import OwnerVersion, Signature
from ..domain import encoding as codec
from ..source import CheckedAlias, SourceFile, parse_identifier
from ..tools import Scope, SourceIndex, lookup_sources, prepare_archive
from . import candidates, records as r

SCOPE = 'application.child-declarations'


def current(reader, owner, context):
    saved = r.value(reader, SCOPE, owner.obligation_id)
    return saved if saved and saved['scope'] == [owner.generation, context.sha256] else None


def remember(reader, submission, selections, source):
    """Bind entry labels to checked child owners; shared children get one update."""
    if not selections:
        return Effect()
    draft, _, _ = candidates.entries(reader, submission)
    pending = set(draft.unproved)
    owners = {}
    for child, names in selections:
        if child not in pending:
            continue
        row = submission['rows'][child]
        owner = OwnerVersion(*row['version'])
        if reader.node(owner.obligation_id).node.owner != owner:
            raise ValueError('Suggested child binding changed')
        owners.setdefault(owner, []).extend(name for name in names if parse_identifier(name) is not None)
    effects = []
    for owner, names in owners.items():
        if not names:
            continue
        _, state = r.owner_state(reader, owner.obligation_id)
        saved = current(reader, owner, state.context) or dict(
            scope=[owner.generation, state.context.sha256], names=[], completed=[])
        # Stable declaration order also stabilizes the excerpts in child prompts.
        names = sorted(set(saved['names']).union(names))
        if names != saved['names']:
            effects.append(r.record(reader, SCOPE, owner.obligation_id,
                dict(saved, names=names, source=r.ref(source))))
    return r.merge(*effects)


def plan(flow, reader, snapshot):
    owner = snapshot.version.owner
    saved = current(reader, owner, snapshot.version.context)
    if not saved:
        return None
    completed = set(saved['completed'])
    names = tuple(sorted(set(saved['names']) - completed))[:16]
    if not names:
        return None
    _, state = r.owner_state(reader, owner.obligation_id)
    aliases, expected = [], [owner]
    for alias, child, identity in candidates.prerequisites(reader, snapshot.context, snapshot.environment_id):
        aliases.append(CheckedAlias(alias, Signature(identity.target, identity.universes, identity.context)))
        expected.append(child.owner)
    request = flow.checker.prepare('lookup', flow.selection, state.index, owner, names,
        aliases=tuple(aliases), imports=snapshot.context.inherited_imports, include_body=True, source_ranges=True)
    key = 'child-declarations:'+request.sha256
    return r.merge(flow.compiler_job(reader, key, request, tuple(expected),
        ['child-declarations', snapshot.version.context.sha256, list(names)]),
        r.record(reader, 'application.pending', owner.obligation_id, key))


def reduce(flow, reader, invocation, accepted, binding):
    """Lookup failures are advisory and must not stop an accepted child plan."""
    from .workflow import Buffer
    owner = invocation.request.job.owner
    _, state = r.owner_state(reader, owner.obligation_id)
    saved = current(reader, owner, state.context)
    if (saved is None or state.context.sha256 != binding[1] or
            r.value(reader, 'application.pending', owner.obligation_id) != invocation.request.job.semantic_key):
        return Effect(disposition='STALE')
    names = tuple(binding[2])
    receipt = reader.receipt(invocation.original_execution_id)
    provenance = [r.unref(saved['source']), receipt.result_ref]
    rows = ['Reviewer-selected declarations for this child. Check applicability to the problem.']
    available = {}
    try:
        if accepted.failure or accepted.value is None:
            raise ValueError(accepted.failure.value if accepted.failure else 'Missing lookup result')
        admitted = accepted.value
        if admitted.operation != 'lookup':
            raise ValueError('Expected a checked lookup result')
        value = codec.decode(r.read(reader, admitted.evidence, 4*1024**2))
        if (value['request'] != invocation.request.operation_payload.sha256 or
                tuple(row['query'] for row in value['records']) != names):
            raise ValueError('Lookup differs from suggested declarations')
        request = CompilerRequest(r.read(reader, invocation.request.operation_payload, r.DATA))
        packet = request.decode()['packet']
        records = tuple(value['records'])
        rows.extend(lookup_sources(records, environment_id=flow.selection.environment_id,
            index=getattr(flow.search, 'index', None) or SourceIndex(flow.selection.environment_id, ()),
            prefix=SourceFile(packet['logical'], packet['source'].encode()),
            reader=getattr(flow.search, 'source_reader', None) or reader))
        available = {str(i+1): row['query'] for i, row in enumerate(records)
                     if row['status'] == 'available' and row.get('type')}
        provenance.append(admitted.evidence)
    except ValueError as error:
        rows.append('Automatic declaration lookup failed: '+str(error)[:2048])
        rows.append('Suggested names: '+', '.join(names))
    buffer = Buffer()
    archive = prepare_archive(Scope(owner, flow.selection.environment_id, state.context),
        sha256(invocation.request.job.semantic_key.encode()).hexdigest(), rows,
        tuple(dict.fromkeys(provenance)), buffer)
    effects = []
    # Only mark a declaration shown on the page where its checked type begins.
    for page in buffer.artifacts:
        fragments = codec.load_record(page.source, limit=16000)[4]
        shown = [available[row[1][0]] for row in fragments
                 if row[0] == 'text' and row[2] == 0 and row[1][0] in available]
        if shown:
            effects.append(r.record(reader, 'application.page-declarations', page.reference.sha256, shown))
    data = artifact(archive.payload)
    archives = r.value(reader, 'application.archives', owner.obligation_id) or []
    return r.merge(*effects, Effect(artifacts=(*buffer.artifacts, data)),
        r.record(reader, 'application.archives', owner.obligation_id, archives+[r.ref(data.reference)]),
        r.save_owner(reader, owner.obligation_id, replace(state,
            evidence=tuple(dict.fromkeys((*state.evidence, archive.pages[0]))))),
        r.record(reader, SCOPE, owner.obligation_id,
            dict(saved, completed=sorted(set(saved['completed']).union(names)))),
        r.record(reader, 'application.pending', owner.obligation_id, None))
