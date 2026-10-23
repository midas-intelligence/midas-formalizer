"""Helper lowering, retrieval continuations and bounded advisory delivery."""
from dataclasses import replace
import re

from ..checker import ProbeCase
from ..controller import Effect, artifact, reserve_helper
from ..domain import ArtifactRef, JobKind, ResourceLimits, ResultStatus, Signature
from ..domain import encoding as codec
from ..prompt import Options, Section, build, researcher
from ..provider import Limits, ModelRequest, Route
from ..source import CheckedAlias, SourceFile
from ..strategy import Context
from ..tools import (Archive, HelperRequest, Lookup, Plan, Probe, Research, Retrieve, Search,
    SearchResult, SourceIndex, Stage, StageEvidence, admit_checked, admit_research, continue_retrieval,
    lookup_sources, prepare_archive, probe_summary, search_stages, search_summary)
from . import records as r, token_usage


def begin(flow, reader, turn, plan, artifacts=(), parent=None):
    values = tuple(artifact(a.data) for a in artifacts)
    request, original = artifact(plan.request.payload), artifact(turn.payload)
    stages = tuple(artifact(s.payload) for s in plan.stages)
    saved = dict(request=r.ref(request.reference), turn=r.ref(original.reference),
        stages=[r.ref(s.reference) for s in stages], results={}, searches=[], parent=parent)
    effects = [reserve_helper(reader, flow.policy, plan.request.scope.owner, plan, flow.services),
        r.record(reader, 'application.helpers', plan.request.id, saved),
        r.record(reader, 'application.pending', turn.version.owner.obligation_id, 'helper:'+plan.request.id),
        Effect(artifacts=(*values, request, original, *stages))]
    overlay = flow.overlay(reader, values)
    for stage in plan.stages:
        effects.append(lower(flow, reader, stage, overlay))
    return r.merge(*effects)


def lower(flow, reader, stage, inputs=None):
    inputs = inputs or reader
    owner = stage.scope.owner
    _, state = r.owner_state(reader, owner.obligation_id)
    action = stage.action
    binding = ['helper', stage.helper_id, stage.ordinal]
    if isinstance(action, Search):
        if flow.search is None:
            raise ValueError('Search service is unavailable')
        return r.job(reader, stage.key, stage.kind, owner, stage.scope.environment_id, stage.payload,
            stage.inputs, (owner,), flow.policy.config.local.limits(), binding)
    if isinstance(action, Research):
        route = Route.from_config(flow.policy.config.research)
        limits = Limits.from_config(flow.policy.config.research_model, web_calls=action.web_calls)
        options = Options(route.transport, route.model)
        snapshot = flow.snapshot(reader, owner)
        if snapshot.version.context != stage.scope.context:
            raise ValueError('Research context differs from its frozen helper scope')
        formal = flow.display(reader, snapshot, formal_only=True)
        display = replace(formal, sections=(*formal.sections,
            Section('question', 'question', action.question.encode()),
            Section('question', 'urls', '\n'.join(action.urls).encode()),
            Section('capabilities', 'web-allowance', f'At most {action.web_calls} web calls.'.encode())))
        renderer = (lambda snapshot: replace(researcher.render(snapshot),
            instructions=flow.instructions['researcher'])) if 'researcher' in flow.instructions else researcher.render
        rendered = build(display, options, renderer=renderer,
            input_token_budget=flow.policy.config.research.input_token_budget,
            token_sample=token_usage.sample(reader, owner, 'research', route))
        wire = r.wire_artifact(rendered.wire, stage.key)
        setup = artifact(rendered.setup) if rendered.setup else None
        request = ModelRequest(route, 'research', limits, wire.reference, setup.reference if setup else None)
        result = r.merge(r.job(reader, stage.key, JobKind.MODEL, owner, stage.scope.environment_id,
            request.payload, request.inputs, display.owners,
            ResourceLimits(limits.invocation_ms, flow.policy.config.local.memory_mib,
                           limits.output_bytes, limits.event_bytes+16384), binding, helper='research'),
            token_usage.prepare(reader, stage.key, owner, 'research', route, rendered),
            Effect(artifacts=tuple(a for a in (wire, setup) if a)))
        return r.merge(result, flow.script.bind(reader, request)) if flow.script else result
    context = Context.load(r.read(reader, state.context, 8*1024**2))
    aliases, expected = [], [owner]
    from .candidates import identity, prerequisites
    for parameter, child, ident in prerequisites(reader, context, stage.scope.environment_id):
        aliases.append(CheckedAlias(parameter, Signature(ident.target, ident.universes, ident.context)))
        expected.append(child.owner)
    if isinstance(action, Lookup):
        request = flow.checker.prepare('lookup', flow.selection, state.index, owner, action.names,
            aliases=tuple(aliases), imports=context.inherited_imports,
            include_body=True, source_ranges=True)
    elif isinstance(action, Probe):
        cases = tuple(ProbeCase(**codec.load_record(r.read(inputs, ref), limit=r.META)) for ref in action.cases)
        ident = identity(reader, state, stage.scope.environment_id)
        request = flow.checker.prepare('probe', flow.selection, state.index, owner, cases, goal=ident,
                                      aliases=tuple(aliases), imports=context.inherited_imports)
    else:
        raise ValueError('Unsupported helper stage')
    return flow.compiler_job(reader, stage.key, request, tuple(expected), binding)


def _evidence(value):
    return [value.stage.ordinal, value.request_hash, r.receipt(value.receipt), value.status.value,
            [r.ref(a) for a in value.artifacts], list(value.available), list(value.unavailable), value.error]


def _load_evidence(value, stage):
    if value[0] != stage.ordinal:
        raise ValueError('Helper evidence ordinal changed')
    return StageEvidence(stage, value[1], r.unreceipt(value[2]), ResultStatus(value[3]),
        tuple(r.unref(a) for a in value[4]), tuple(value[5]), tuple(value[6]), value[7])


def reduce(flow, reader, invocation, accepted, binding):
    from .workflow import Buffer
    from ..strategy import Turn
    key, ordinal = binding[1:]
    saved = r.value(reader, 'application.helpers', key)
    request = HelperRequest.load(r.read(reader, r.unref(saved['request'])))
    stages = tuple(Stage.load(r.read(reader, r.unref(s))) for s in saved['stages'])
    stage = next(s for s in stages if s.ordinal == ordinal)
    receipt = reader.receipt(invocation.original_execution_id)
    extra, report = [], None
    if isinstance(stage.action, Search):
        report = accepted.value or SearchResult(stage.sha256, error=(accepted.failure.value if accepted.failure else 'invalid_result'))
        item = artifact(report.payload)
        extra.append(Effect(artifacts=(item,)))
        evidence = StageEvidence(stage, invocation.request.request_hash, receipt,
            ResultStatus.FAILED if report.error else ResultStatus.SUCCEEDED, (item.reference,), error=report.error)
    elif accepted.failure:
        evidence = StageEvidence(stage, invocation.request.request_hash, receipt, ResultStatus.FAILED,
                                 (receipt.result_ref,), error=accepted.failure.value)
    elif isinstance(stage.action, Research):
        try:
            data = artifact(admit_research(stage, accepted.value, reader))
            extra.append(Effect(artifacts=(data,)))
            evidence = StageEvidence(stage, invocation.request.request_hash, receipt, ResultStatus.SUCCEEDED, (data.reference,))
        except ValueError as error:
            evidence = StageEvidence(stage, invocation.request.request_hash, receipt, ResultStatus.FAILED,
                                     (accepted.value.raw,), error=str(error)[:2048])
    else:
        evidence = admit_checked(stage, accepted.value, invocation.request.operation_payload.sha256, reader)
    results = dict(saved['results'], **{str(ordinal): _evidence(evidence)})
    saved = dict(saved, results=results)
    if len(results) != len(stages):
        return r.merge(*extra, r.record(reader, 'application.helpers', key, saved))
    overlay = flow.overlay(reader, tuple(a for e in extra for a in e.artifacts))
    plan = Plan(request, stages, None)
    searches = tuple(SearchResult.load(r.read(overlay, r.unref(v))) for v in saved['searches'])
    if isinstance(request.action, Retrieve) and stages and all(isinstance(s.action, Search) for s in stages):
        searches = tuple(SearchResult.load(r.read(overlay, _load_evidence(results[str(s.ordinal)], s).artifacts[0])) for s in stages)
        following, fusion = continue_retrieval(plan, searches, request.scope)
        artifacts = tuple(artifact(s.payload) for s in following.stages)
        extra.append(Effect(artifacts=artifacts))
        saved = dict(saved, stages=[r.ref(s.reference) for s in artifacts], results={},
            searches=[r.ref(_load_evidence(results[str(s.ordinal)], s).artifacts[0]) for s in stages])
        if following.stages:
            return r.merge(*extra, r.record(reader, 'application.helpers', key, saved),
                           *(lower(flow, reader, s) for s in following.stages))
        plan, stages, results = following, (), {}
    evidence = tuple(_load_evidence(results[str(s.ordinal)], s) for s in stages)
    turn = Turn.load(r.read(reader, r.unref(saved['turn'])))
    attempt = None
    if saved['parent']:
        from .candidates import submission
        from ..strategy import Attempt
        subkey, entry_key = saved['parent']
        checked = r.uncheck(submission(reader, subkey)['rows'][entry_key]['checked'])
        attempt = Attempt(checked.candidate, True, checked)
    snapshot = replace(flow.snapshot(reader, request.scope.owner, attempt=attempt), busy=False)
    # Continuation validates owner/context; memory may have advanced at dispatch.
    flow.strategy.on_helper(snapshot, turn, plan, evidence, searches=searches)
    rows, provenance = [], []
    for item in evidence:
        provenance.extend(item.artifacts)
        for reference in item.artifacts:
            data = r.read(overlay, reference, 4*1024**2)
            if isinstance(item.stage.action, Probe) and item.status == ResultStatus.SUCCEEDED:
                remaining = 4*1024**2-len(data)
                for case, raw in zip(item.stage.action.cases, codec.load_record(data, limit=65536)):
                    ref = ArtifactRef.from_dict(raw)
                    payload = r.read(overlay, ref, remaining)
                    remaining -= len(payload)
                    rows.append(probe_summary(codec.load_record(r.read(reader, case), limit=r.META),
                                              codec.decode(payload)['row']))
                    provenance.extend((case, ref))
                continue
            if isinstance(item.stage.action, Lookup) and item.status == ResultStatus.SUCCEEDED:
                records = tuple(codec.decode(data)['records'])
                from ..checker import CompilerRequest
                executed = reader.invocation(item.receipt.original_execution_id)
                packet = CompilerRequest(r.read(reader, executed.request.operation_payload, r.DATA)).decode()['packet']
                source_reader = getattr(flow.search, 'source_reader', None) or reader
                index = getattr(flow.search, 'index', None) or SourceIndex(request.scope.environment_id, ())
                rows.extend(lookup_sources(records, environment_id=request.scope.environment_id,
                    index=index, prefix=SourceFile(packet['logical'], packet['source'].encode()), reader=source_reader))
                continue
            if isinstance(item.stage.action, Probe):
                rows.append('Probe failed: '+(item.error or item.status.value))
                continue
            try:
                rows.append(codec.decode(data) if data.startswith(codec.MAGIC) else codec.load_record(data, limit=4*1024**2))
            except ValueError:
                rows.append(dict(status=item.error or 'advisory', artifact=reference.id))
    if searches:
        for result, search in zip(searches, search_stages(request)):
            rows.extend(search_summary(result, search.action))
        provenance.extend(r.unref(v) for v in saved['searches'])
    if not provenance:
        provenance.append(r.unref(saved['request']))
    buffer = Buffer()
    archive = prepare_archive(request.scope, key, rows, tuple(dict.fromkeys(provenance)), buffer)
    archive_data = artifact(archive.payload)
    _, state = r.owner_state(reader, request.scope.owner.obligation_id)
    archives = r.value(reader, 'application.archives', request.scope.owner.obligation_id) or []
    archives = archives + [r.ref(archive_data.reference)]
    effects = [*extra, Effect(artifacts=(*buffer.artifacts, archive_data)),
        r.record(reader, 'application.archives', request.scope.owner.obligation_id, archives),
        r.save_owner(reader, request.scope.owner.obligation_id, replace(state,
            evidence=tuple(dict.fromkeys((*state.evidence, archive.pages[0]))))),
        r.record(reader, 'application.pending', request.scope.owner.obligation_id, None),
        r.record(reader, 'application.helpers', key, dict(saved, completed=True))]
    if saved['parent']:
        from .candidates import submission
        subkey, _ = saved['parent']
        parent = submission(reader, subkey)
        effects.append(r.record(reader, 'application.submissions', subkey, dict(parent, current=None)))
    return r.merge(*effects)


def advertised(reader, owner):
    refs = r.value(reader, 'application.archives', owner.obligation_id) or []
    return tuple(Archive.load(r.read(reader, r.unref(ref))) for ref in refs)


def page_effect(flow, reader, snapshot, turn, work, state):
    key = codec.load_record(work.detail, limit=65536)[0]
    archive = next(a for a in advertised(reader, snapshot.version.owner) if key.split(':')[0] == a.id)
    displayed = set(state.evidence)
    result = flow.strategy.on_page(snapshot, turn, work, archive, reader,
        delivered=lambda cursor: archive.pages[int(cursor.rsplit(':', 1)[1])] in displayed)
    if result.page is None:
        return state, Effect()
    page = result.page
    return replace(state, evidence=tuple(dict.fromkeys((*state.evidence, archive.pages[page.ordinal])))), Effect()


_REVIEW_NAME = re.compile(r"[^\W\d][\w'?!]*(?:\.[^\W\d][\w'?!]*)*")


def review_evidence(reader, snapshot, review, sections, displayed):
    """Select named checked lookups and research for this exact review.

    Lookup records precede search hints in retrieval archives. Their original
    record indices let us select whole excerpts without guessing from prose or
    loading complete historical results. General searches/probes are omitted.
    """
    from ..strategy import Turn
    from ..strategy.state import candidate_hash
    if not displayed:
        return
    names = {match[0].removeprefix('_root_.') for section in sections
             if section.kind in ('subject', 'definitions', 'interfaces', 'candidate')
             for match in _REVIEW_NAME.finditer(section.text.decode())}
    displayed = set(displayed)
    for archive in advertised(reader, snapshot.version.owner):
        if archive.scope.context != snapshot.version.context or not displayed.intersection(archive.pages):
            continue
        saved = r.value(reader, 'application.helpers', archive.helper_id)
        if not saved:
            continue
        request = HelperRequest.load(r.read(reader, r.unref(saved['request'])))
        records = None
        if isinstance(request.action, Research):
            if saved['parent'] != list(review):
                continue
            turn = Turn.load(r.read(reader, r.unref(saved['turn'])))
            if turn.candidate_hash != candidate_hash(snapshot.attempt.candidate):
                continue
        elif isinstance(request.action, Retrieve):
            stages = tuple(Stage.load(r.read(reader, r.unref(s))) for s in saved['stages'])
            if not stages or any(not isinstance(s.action, Lookup) or
                    saved['results'][str(s.ordinal)][3] != ResultStatus.SUCCEEDED.value for s in stages):
                continue
            records, offset = set(), 0
            for stage in stages:
                available = saved['results'][str(stage.ordinal)][5]
                for index, name in enumerate(stage.action.names):
                    normalized = name.removeprefix('_root_.')
                    if (name in available and not normalized.startswith('_private.') and
                            (normalized in names or normalized.rsplit('.', 1)[-1] in names)):
                        records.add(str(offset + index))
                offset += len(stage.action.names)
            if not records:
                continue
        else:
            continue
        for ordinal, ref in enumerate(archive.pages):
            if ref not in displayed:
                continue
            text = page_text(r.read(reader, ref, 16000), records=records)
            if not text:
                continue
            header = f'Result ID: {archive.id}\nPage {ordinal+1} of {len(archive.pages)}. '
            header += 'Selected checked declaration excerpts.\n\n' if records is not None else 'Review research (advisory).\n\n'
            yield ref, header.encode() + text


def page_text(payload, *, records=None):
    """Readable projection of one bounded page; JSON is never used for display."""
    value = codec.load_record(payload, limit=16000)
    lines = []
    for row in value[4]:
        if records is not None and row[1][0] not in records:
            continue
        if row[0] == 'text' and len(row[1]) == 1:
            content = row[4]
            if row[2]:
                content = '-- Continued excerpt\n'+content
            if row[2]+len(row[4].encode()) < row[3]:
                content += '\n-- Excerpt continues on the next page.'
            lines.append(content+'\n')
        else:
            lines.append('/'.join(row[1])+': '+(row[4] if row[0] == 'text' else str(row[2])))
    return '\n'.join(lines).encode()
