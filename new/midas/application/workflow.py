"""One application workflow over the controller's execution and acceptance loop."""
from dataclasses import replace
from hashlib import sha256
from uuid import UUID, uuid5

from ..checker import CompilerRequest
from ..controller import (Effect, Operations, artifact, checker_operation, export_operation,
    model_operation, owner_usage, search_operation)
from ..domain import (ArtifactRef, JobKind, OwnerVersion, ProofGraph, ProofState, ResourceLimits,
    Signature, accept_assembly)
from ..domain import encoding as codec
from ..export import AdmittedStage, Request, current
from ..policy import ExportUsage, Failure, RunUsage, Services, Stage
from ..prompt import Options, Section, Snapshot as Display, subject
from ..provider import Limits, ModelRequest, Route
from ..storage import Changes
from ..status import progress as committed_progress
from ..strategy import Attempt, Context, Dispatch, Draft, Memory, Snapshot, StopRequest, Strategy, Turn, Version
from . import candidates, records as r, token_usage


class Buffer:
    """Private preparation staging; controller publishes every returned reference."""
    def __init__(self):
        self.artifacts = []

    def write_bounded(self, data, *, max_bytes):
        if len(data) > max_bytes:
            raise ValueError('Prepared artifact exceeds limit')
        item = artifact(data)
        self.artifacts.append(item)
        return item.reference


class Overlay:
    def __init__(self, reader, artifacts):
        self.reader = reader
        self.values = {a.reference: a.source for a in artifacts}

    def read_artifact(self, reference, *, max_bytes):
        data = self.values.get(reference)
        return reference.verify(data, max_bytes=max_bytes) if data is not None else r.read(self.reader, reference, max_bytes)


class Workflow:
    def __init__(self, selection, policy, checker, export, *, mode='supplied', provider=None,
                 search=None, instructions=(), script=None, export_only=False, allow_sorry=False,
                 library=None, strategy=None):
        self.selection, self.policy, self.checker, self.export = selection, policy, checker, export
        self.mode, self.provider, self.search = mode, provider, search
        self.instructions, self.script = dict(instructions), script
        self.export_only, self.allow_sorry, self.library = export_only, allow_sorry, library
        self.strategy = strategy or Strategy()
        self.services = Services(research=provider is not None and policy.config.research.configured,
            research_web_calls=12 if policy.config.research.configured else 0)
        self._graph = None
        self._stages = {}
        self._progress_cache = None
        self.owner_usage = owner_usage
        compiler = checker_operation(checker, self.reduce, candidate=self.candidate)
        exporting = export_operation(export, self.reduce, self.export_inputs)
        exporting = replace(exporting, observe=self.export_usage, retry_allowed=self.export_retry)
        model = replace(model_operation(provider, self.reduce), observe=token_usage.observe) if provider else None
        self.operations = Operations(model=model,
            binding=compiler, proof=compiler, lookup=compiler, probe=compiler,
            search=search_operation(search, self.reduce) if search else None,
            assemble=exporting, integrate=exporting, export_verify=exporting)

    overlay = staticmethod(Overlay)

    def graph(self, reader):
        revision = reader.status_snapshot().graph_revision
        if self._graph is None or self._graph.revision != revision:
            nodes, after = [], ''
            while page := reader.nodes(after=after, limit=1000):
                nodes.extend(n.node for n in page)
                after = page[-1].node.owner.obligation_id
            roots = tuple(key for _, key in reader.roots(limit=1000))
            retired = r.value(reader, r.SCOPE, 'retired') or []
            self._graph = ProofGraph(revision, roots, tuple(nodes), tuple(retired))
        return self._graph

    def snapshot(self, reader, owner, *, attempt=None):
        saved, state = r.owner_state(reader, owner.obligation_id)
        context = Context.load(r.read(reader, state.context, 8*1024**2))
        memory = Memory.load(r.read(reader, state.memory)) if state.memory else Memory()
        a = reader.status_snapshot().accounting
        pending = r.value(reader, 'application.pending', owner.obligation_id)
        from .helpers import advertised
        archives = advertised(reader, owner)
        displayed = set(state.evidence)
        return Snapshot(Version(owner, saved.revision, state.context, memory.version, len(state.evidence)),
            context, self.selection.environment_id, self.work_graph(reader)[0], memory,
            owner_usage(reader, owner), RunUsage(turns=a.turns, backtracks=reader.counter(r.SCOPE, 'backtracks'),
                intents=sum(n for _, n in reader.status_snapshot().intents)), self.services, self.mode,
            Draft.load(r.read(reader, state.base, 17*1024**2)) if state.base else None,
            attempt, busy=pending is not None,
            stopped=state.stopped and not (attempt and attempt.candidate.joint_submission_id),
            forbidden_names=tuple(t.selector for t in self.selection.selected.targets),
            allowed_pins=tuple(a.id for a in archives),
            cursors=tuple(f'{a.id}:{i}' for a in archives for i in range(1, min(len(a.pages), 128))
                          if a.pages[i] not in displayed),
            stop_request=StopRequest.load(r.read(reader, state.stop_request, 65536)) if state.stop_request else None)

    def display(self, reader, snapshot, *, formal_only=False, review=None, bounded_evidence=False):
        from . import presentation
        _, state = r.owner_state(reader, snapshot.version.owner.obligation_id)
        ident = candidates.identity(reader, state, self.selection.environment_id)
        target = self.selection.selected.targets[state.index]
        sections = [Section('source', 'source-prefix', self.selection.source[:target.declaration.start]),
            subject('goal', Signature(ident.target, ident.universes, ident.context))]
        refs = [state.context, state.identity]
        owners = [snapshot.version.owner]
        guidance = reader.record('application.guidance', snapshot.version.owner.obligation_id)
        if guidance:
            sections.append(Section('subject', 'proof-guidance', r.read(reader, guidance.artifact, 2*1024**2)))
            refs.append(guidance.artifact)
        for ordinal, (alias, child, child_identity) in enumerate(candidates.prerequisites(
                reader, snapshot.context, self.selection.environment_id)):
            signature = Signature(child_identity.target, child_identity.universes, child_identity.context)
            sections.append(Section('interfaces', f'inherited-{ordinal}',
                alias.encode() + b' :\n' + subject('type', signature).text))
            refs.append(child.interface)
            owners.append(child.owner)
        if snapshot.context.references and review is None:
            sections.append(Section('interfaces', 'entry-references', '\n'.join(
                f'{ref.label}: {ref.key}' for ref in snapshot.context.references).encode()))
        definitions = codec.load_record(snapshot.context.definitions, limit=8*1024**2)
        if definitions:
            sections.append(Section('definitions', 'inherited-definitions', '\n'.join(
                f"{d['kind']} {d['name']} : {d['type']} := {d['value']}" for d in definitions).encode()))
        for required in snapshot.context.subjects:
            if required.kind != 'target' or required.name == target.selector:
                continue
            index = next(i for i, t in enumerate(self.selection.selected.targets) if t.selector == required.name)
            peer = self.selection.identities[index]
            sections.append(subject('required-'+str(index), Signature(peer.target, peer.universes, peer.context)))
            owners.append(reader.node(peer.id).node.owner)
        sections.append(Section('subject', 'required-subjects', '\n'.join(
            f'{s.kind}: {s.name}' for s in snapshot.context.subjects).encode()))
        import_lines = ['The imports in SOURCE source-prefix are already active in this node.']
        if snapshot.context.inherited_imports:
            import_lines.append('Additional active imports inherited from parent: ' +
                                ', '.join(snapshot.context.inherited_imports))
        if snapshot.context.imports:
            import_lines.append('Additional prepared modules available for import: ' +
                                ', '.join(snapshot.context.imports))
        else:
            import_lines.append('No additional import entries are advertised for this node; '
                                'the source imports and their transitive dependencies remain active.')
        import_lines.append('Use retrieval or probes to inspect declarations in the active context. '
                            'Develop missing intermediate lemmas as proof obligations.')
        sections.append(Section('interfaces', 'imports', '\n'.join(import_lines).encode()))
        if formal_only:
            return Display(tuple(sections), tuple(dict.fromkeys(refs)), tuple(dict.fromkeys(owners)))
        if review is None:
            sections.extend(presentation.memory(snapshot.memory))
        if review is not None:
            key, entry_key = review
            saved = candidates.submission(reader, key)
            draft_ref = r.unref(saved['draft'])
            draft = snapshot.base if draft_ref == state.base else Draft.load(r.read(reader, draft_ref, 17*1024**2))
            _, coordinator = r.owner_state(reader, saved['owner'][0])
            context = (snapshot.context if coordinator.context == state.context else
                       Context.load(r.read(reader, coordinator.context, 8*1024**2)))
            checked = r.read(reader, snapshot.attempt.candidate.source, 1024**2)
            sections.append(presentation.candidate(draft, context, selected=entry_key, checked=checked))
            refs.extend((draft_ref, coordinator.context))
            owners.append(OwnerVersion(*saved['owner']))
        elif snapshot.base:
            sections.append(presentation.candidate(snapshot.base, snapshot.context))
            refs.append(state.base)
        if snapshot.stop_request:
            sections.append(presentation.stop_request(snapshot.stop_request))
            refs.extend((state.stop_request, snapshot.stop_request.source))
        if snapshot.attempt:
            candidate = snapshot.attempt.candidate
            if review is None:
                sections.append(Section('candidate', 'checked-candidate', r.read(reader, candidate.source, 1024**2)))
            refs.append(candidate.source)
            for ordinal, dep in enumerate(candidate.dependencies):
                _, child_state = r.owner_state(reader, dep.owner.obligation_id)
                child = candidates.identity(reader, child_state, self.selection.environment_id)
                signature = Signature(child.target, child.universes, child.context)
                sections.append(Section('interfaces', f'dependency-{ordinal}',
                    dep.alias.encode() + b' :\n' + subject('type', signature).text))
                refs.append(dep.interface)
                owners.append(dep.owner)
        from .helpers import advertised, page_text, review_evidence
        displayed = tuple(dict.fromkeys(state.evidence))
        if bounded_evidence and review is None:
            # Keep preparation itself within the prompt's hard byte/section
            # bounds before the calibrated token window selects its final pages.
            # Use artifact sizes, without loading pages that cannot be displayed.
            from ..prompt.protocol import MAX_BYTES
            size = sum(len(s.text) for s in sections)
            retained = []
            for reference in reversed(displayed):
                if retained and (size + reference.byte_count + 1024 > MAX_BYTES - 65536
                                 or len(sections) + len(retained) >= 1000):
                    break
                retained.append(reference)
                size += reference.byte_count + 1024
            omitted = len(displayed) - len(retained)
            displayed = tuple(reversed(retained))
            if omitted:
                sections.append(Section('feedback', 'evidence-preparation-limit',
                    f'{omitted} older evidence page(s) omitted by the prompt preparation byte/section limit.'.encode()))
        if review is not None:
            for ordinal, (reference, text) in enumerate(review_evidence(reader, snapshot, review, sections, displayed)):
                sections.append(Section('evidence', f'review-result-{ordinal+1}', text))
                refs.append(reference)
            displayed = ()
        pages = {reference: (archive.id, ordinal+1, len(archive.pages))
                 for archive in (advertised(reader, snapshot.version.owner) if displayed else ())
                 for ordinal, reference in enumerate(archive.pages) if reference in displayed}
        for ordinal, reference in enumerate(displayed):
            details = pages.get(reference)
            if details is None:
                header = 'Tool output included below.\n\n'
            else:
                result_id, page, count = details
                header = f'Result ID: {result_id}\nPage {page} of {count}. '
                header += 'Complete result included below.\n\n' if count == 1 else 'This page is included below.\n\n'
            sections.append(Section('evidence', f'tool-result-{ordinal+1}',
                header.encode()+page_text(r.read(reader, reference, 16000))))
            refs.append(reference)
        if review is None and self.search and self.search.index:
            sections.append(Section('source', 'search-roots',
                ('Frozen-source roots: '+', '.join(self.search.index.roots)).encode()))
        # The renderer's byte allowance covers all selected pages. Any configured
        # token window is applied after rendering, including instructions/schema.
        return Display(tuple(sections), tuple(dict.fromkeys(refs)), tuple(dict.fromkeys(owners)),
                       evidence_bytes=sum(len(s.text) for s in sections if s.kind == 'evidence'))

    def compiler_job(self, reader, key, request, expected, binding, *, limits=None, helper=None):
        value = request.decode()
        from ..checker.requests import OPERATIONS
        kind = OPERATIONS[value['operation']]
        owners = tuple(OwnerVersion(d[0], d[1]) for d in value['candidate']['dependencies']) if value['candidate'] else ()
        return r.job(reader, key, kind, OwnerVersion(*value['owner']), self.selection.environment_id,
            request.payload, tuple(ArtifactRef.from_dict(v) for v in value['inputs']), (*expected, *owners),
            limits or self.policy.config.checker.limits(), binding, helper=helper)

    def model_job(self, reader, snapshot, work, *, binding=None):
        role = 'reviewer' if work.operation in ('review', 'review_stop') else 'prover'
        setting = getattr(self.policy.config, role)
        route = Route.from_config(setting)
        options = Options(route.transport, route.model, **({} if route.transport == 'codex' else
            dict(reasoning=setting.reasoning_effort, prompt_cache=route.prompt_cache,
                 cache_scope=route.scope if route.prompt_cache == 'explicit' else None)))
        renderer = None
        if role in self.instructions:
            from ..prompt import prover, reviewer
            base = reviewer.render if role == 'reviewer' else prover.render
            renderer = lambda display: replace(base(display), instructions=self.instructions[role])
        prepared = self.strategy.prepare_model(snapshot, self.policy, work,
                                                self.display(reader, snapshot, review=binding if role == 'reviewer' else None,
                                                    bounded_evidence=getattr(self.policy.config, role).input_token_budget is not None),
                                                options, renderer=renderer,
                                                token_sample=token_usage.sample(reader, snapshot.version.owner, role, route))
        wire = r.wire_artifact(prepared.wire.wire, prepared.turn.id)
        setup = artifact(prepared.wire.setup) if prepared.wire.setup is not None else None
        turn = artifact(prepared.turn.payload)
        limits = Limits.from_config(self.policy.config.model)
        request = ModelRequest(route, role, limits, wire.reference, setup.reference if setup else None)
        key = 'model:' + prepared.turn.id
        effects = [r.job(reader, key, JobKind.MODEL, snapshot.version.owner, self.selection.environment_id,
            request.payload, request.inputs, prepared.wire.snapshot.owners,
            ResourceLimits(limits.invocation_ms, self.policy.config.local.memory_mib,
                           limits.output_bytes, limits.event_bytes+16384),
            ['model', r.ref(turn.reference), binding], downstream=1),
            token_usage.prepare(reader, key, snapshot.version.owner, role, route, prepared.wire),
            r.record(reader, 'application.pending', snapshot.version.owner.obligation_id, key),
            Effect(artifacts=tuple(a for a in (wire, setup, turn) if a))]
        if self.script:
            effects.append(self.script.bind(reader, request))
        return r.merge(*effects)

    def review_job(self, reader, key, saved, entry_key, checked):
        snapshot = self.snapshot(reader, checked.candidate.owner, attempt=Attempt(checked.candidate, True, checked))
        if snapshot.busy:
            return None
        work = self.strategy.plan(snapshot, self.policy).work[0]
        effect = self.model_job(reader, snapshot, work, binding=[key, entry_key])
        job_key = effect.work[0].intent.request.job.semantic_key
        return r.merge(effect, r.record(reader, 'application.submissions', key, dict(saved, current=job_key)))

    def candidate(self, reader, invocation):
        binding = r.value(reader, 'application.jobs', invocation.request.job.semantic_key)
        if binding[0] != 'proof':
            return None
        saved = candidates.submission(reader, binding[1])
        return r.uncandidate(saved['rows'][binding[2]]['candidate'])

    def stage(self, reader, name):
        value = r.value(reader, 'application.export', name)
        if value is None or value[0] != 'accepted':
            return None
        key = r.unref(value[1])
        cached = self._stages.get(name)
        if cached is None or cached[0] != value:
            cached = (value, AdmittedStage(Request(r.read(reader, key, r.DATA)),
                r.unreceipt(value[2]), r.unref(value[3])))
            self._stages[name] = cached
        return cached[1]

    def work_graph(self, reader):
        revision = reader.status_snapshot().revision
        cached = getattr(self, '_work_graph_cache', None)
        if cached is None or cached[0] != revision:
            cached = revision, candidates.pending_graph(self, reader)
            self._work_graph_cache = cached
        return cached[1]

    def parent_request(self, reader, owner):
        node = self.work_graph(reader)[0].node(owner)
        invocation = reader.invocation(node.selected.receipt.original_execution_id)
        return CompilerRequest(r.read(reader, invocation.request.operation_payload, r.DATA))

    def export_inputs(self, reader, invocation):
        result = dict(selection=self.selection, graph=self.graph(reader))
        if invocation.request.job.kind == JobKind.ASSEMBLE:
            result['graph'] = self.work_graph(reader)[0]
            result['parent_request'] = self.parent_request(reader, invocation.request.job.owner)
        elif invocation.request.job.kind == JobKind.EXPORT_VERIFY:
            result['integration'] = self.stage(reader, 'integration')
        return result

    def export_plan(self, reader):
        graph, pending = self.work_graph(reader)
        profile = 'library' if self.library else 'source'
        integration, verification = self.stage(reader, 'integration'), self.stage(reader, 'verification')
        work = self.export.plan(self.selection, graph, profile=profile, integration=integration,
            verification=verification, allow_sorry=self.allow_sorry, limit=1)
        if not work:
            return None
        work = work[0]
        if pending and work.kind != JobKind.ASSEMBLE:
            return None
        name = 'assembly:'+work.owner.obligation_id if work.kind == JobKind.ASSEMBLE else (
            'integration' if work.kind == JobKind.INTEGRATE else 'verification')
        prior = r.value(reader, 'application.export', name)
        epoch = reader.counter(r.SCOPE, 'export_epoch')
        if prior and prior[0] == 'pending':
            state = reader.intent_state(prior[1])
            if state and state[0] in ('PENDING', 'RUNNING'):
                # Other branches can advance the graph while this job runs.
                # Its explicit owner fences, not the global graph revision,
                # determine whether the controller must cancel it.
                return None
        prior_epoch = prior[-2] if prior and len(prior) == 4 else 0
        if (prior and prior[0] in ('pending', 'failed') and prior[-1] == graph.revision
                and prior_epoch == epoch):
            return None
        usage = r.value(reader, 'application.export', 'usage') or [0, 0, 0]
        try:
            limits = self.policy.export_limits(ExportUsage(*usage), comparator=work.kind == JobKind.EXPORT_VERIFY)
            buffer = Buffer()
            if work.kind == JobKind.ASSEMBLE:
                request = self.export.prepare_assembly(self.selection, graph, work.owner,
                    self.parent_request(reader, work.owner), reader, maximum=self.policy.config.export.max_bytes)
            elif work.kind == JobKind.INTEGRATE:
                request = self.export.prepare_integration(self.selection, graph, reader, buffer, profile=profile,
                    library=self.library, maximum=self.policy.config.export.max_bytes, allow_sorry=self.allow_sorry)
            else:
                request = self.export.prepare_verification(integration, self.selection, graph, reader)
        except ValueError as error:
            return r.record(reader, 'application.export', name, ['failed', str(error)[:2048], epoch, graph.revision])
        key = 'export:'+request.sha256
        if epoch:
            key += ':limits:'+str(epoch)
        if reader.intent_state(key) is not None:
            # Retries belong to the existing controller intent. Unchanged
            # export inputs must not publish a second admission for its key.
            return None
        return r.merge(self.compiler_job(reader, key, request, request.expected_versions, ['export', name, epoch], limits=limits),
            r.record(reader, 'application.export', name, ['pending', key, epoch, graph.revision]),
            Effect(artifacts=tuple(buffer.artifacts)))

    def plan(self, reader, policy, limit):
        """Generator observes each preceding committed effect; no history scans."""
        graph = self.graph(reader)
        handled = set()
        count = 0
        if not self.export_only:
            for node in graph.nodes:
                _, state = r.owner_state(reader, node.owner.obligation_id)
                from .repairs import plan as repair_plan
                repair = repair_plan(self, reader, node.owner)
                if repair:
                    yield 'repair:'+node.owner.obligation_id+':'+str(reader.status_snapshot().revision), repair
                    node = reader.node(node.owner.obligation_id).node
                    _, state = r.owner_state(reader, node.owner.obligation_id)
                    count += 1
                    if count >= limit:
                        return
                if state.submission and state.submission not in handled:
                    handled.add(state.submission)
                    while True:
                        saved = candidates.submission(reader, state.submission)
                        try:
                            effect = candidates.plan(self, reader, state.submission, saved)
                        except ValueError as error:
                            effect = candidates.finish(self, reader, state.submission, saved, 'rejection',
                                reason=str(error)[:256], evidence=r.unref(saved['draft']))
                        if not effect:
                            break
                        yield 'plan:'+sha256(r.pack([state.submission, reader.status_snapshot().revision])).hexdigest(), effect
                        count += 1
                        if count >= limit or effect.work:
                            break
                    if count >= limit:
                        return
            reachable = self.work_graph(reader)[0].reachable()
            for node in self.graph(reader).nodes:
                if node.owner.obligation_id not in reachable or node.state != ProofState.UNRESOLVED:
                    continue
                _, state = r.owner_state(reader, node.owner.obligation_id)
                if state.submission or self.mode == 'supplied':
                    continue
                snapshot = self.snapshot(reader, node.owner)
                proposal = self.strategy.plan(snapshot, policy)
                if not proposal.work:
                    continue
                try:
                    effect = self.model_job(reader, snapshot, proposal.work[0])
                    key = effect.work[0].intent.request.job.semantic_key
                except ValueError as error:
                    effect = r.save_owner(reader, node.owner.obligation_id,
                        replace(state, stopped=True, reason=str(error)[:256]))
                    key = 'blocked:'+node.owner.obligation_id+':'+str(snapshot.version.revision)
                yield key, effect
                count += 1
                if count >= limit:
                    return
        effect = self.export_plan(reader)
        if effect:
            yield 'export-plan:'+str(reader.status_snapshot().revision), effect

    def progress(self, reader):
        revision = reader.status_snapshot().revision
        if self._progress_cache is not None and self._progress_cache[0] == revision:
            return self._progress_cache[1]
        graph = self.graph(reader)
        states = []
        for name in ('integration', 'verification'):
            stage = self.stage(reader, name)
            raw = r.value(reader, 'application.export', name)
            states.append(Stage.VERIFIED if stage and current(stage, self.selection, graph,
                allow_sorry=self.allow_sorry) else Stage.FAILED if raw and raw[0] == 'failed' and raw[-1] == graph.revision
                else Stage.PENDING)
        # Locally ready transitions also keep the controller alive between ticks.
        local = 0
        for node in graph.nodes:
            _, state = r.owner_state(reader, node.owner.obligation_id)
            if state.submission:
                saved = candidates.submission(reader, state.submission)
                local += int(saved['current'] is None and saved['phase'] not in ('done', 'failed'))
        result = committed_progress(reader, integration=states[0], verification=states[1],
                                    local_ready=local, allow_sorry=self.allow_sorry)
        self._progress_cache = revision, result
        return result

    def reduce(self, reader, invocation, accepted):
        binding = r.value(reader, 'application.jobs', invocation.request.job.semantic_key)
        kind = binding[0]
        if kind == 'model':
            return self.on_model(reader, invocation, accepted, binding)
        if kind == 'helper':
            from .helpers import reduce
            return reduce(self, reader, invocation, accepted, binding)
        if kind == 'export':
            return self.on_export(reader, invocation, accepted, binding[1], *binding[2:])
        key, entry_key = binding[1:]
        saved = candidates.submission(reader, key)
        if saved is None or saved['current'] != invocation.request.job.semantic_key:
            return Effect(disposition='STALE')
        if accepted.failure:
            result = reader.result(invocation.original_execution_id)
            from ..prompt.protocol import clean_diagnostics
            text = (clean_diagnostics(r.read(reader, result.diagnostics, 8*1024**2))
                    .decode(errors='replace')[-2000:] if result.diagnostics else '')
            diagnostics = (text,) if text.strip() else ()
            return self.fail_candidate(reader, key, saved, accepted.failure,
                reader.receipt(invocation.original_execution_id).result_ref, diagnostics=diagnostics)
        try:
            if kind == 'binding':
                return candidates.on_binding(self, reader, key, saved, entry_key, accepted.value)
            return candidates.on_check(self, reader, key, saved, entry_key, accepted.value)
        except ValueError as error:
            return candidates.finish(self, reader, key, saved, 'rejection', reason=str(error)[:256],
                evidence=reader.receipt(invocation.original_execution_id).result_ref)

    def fail_candidate(self, reader, key, saved, failure, evidence, *, diagnostics=()):
        exception = {Failure.TIMEOUT: 'timeout', Failure.MEMORY: 'memory', Failure.RECURSION_DEPTH: 'elaboration',
                     Failure.PROTOCOL: 'protocol'}.get(failure)
        outcome = 'exception' if exception else 'rejection' if failure == Failure.REJECTION else 'infrastructure'
        return candidates.finish(self, reader, key, saved, outcome, exception=exception,
                                 reason=failure.value, evidence=evidence, diagnostics=diagnostics)

    def on_model(self, reader, invocation, accepted, binding):
        turn = Turn.load(r.read(reader, r.unref(binding[1])))
        owner = turn.version.owner
        clear = r.record(reader, 'application.pending', owner.obligation_id, None)
        attempt = None
        if binding[2]:
            key, entry_key = binding[2]
            saved = candidates.submission(reader, key)
            checked = r.uncheck(saved['rows'][entry_key]['checked'])
            attempt = Attempt(checked.candidate, True, checked)
        snapshot = replace(self.snapshot(reader, owner, attempt=attempt), busy=False)
        _, state = r.owner_state(reader, owner.obligation_id)
        evidence = reader.receipt(invocation.original_execution_id).result_ref
        if accepted.failure or accepted.value is None:
            if binding[2]:
                return r.merge(clear, self.fail_candidate(reader, key, saved, accepted.failure or Failure.PROTOCOL, evidence))
            return r.merge(clear, r.save_owner(reader, owner.obligation_id,
                replace(state, stopped=True, reason=(accepted.failure or Failure.PROTOCOL).value)))
        try:
            proposal = self.strategy.on_model(snapshot, self.policy, Dispatch(turn, invocation.request.request_ref),
                accepted.value, reader, submission_id=str(uuid5(UUID(invocation.original_execution_id), 'submission')))
        except ValueError as error:
            # Keep semantic rejection details in the next prompt, including when
            # an unavailable helper was syntactically valid. Bound before encoding
            # so even a long Unicode exception fits one memory observation.
            message = str(error)[:1800] or type(error).__name__
            diagnostics = ('Action rejected: '+message+'\n'
                'Correct the action using the current CAPABILITIES and output schema.',)
            if binding[2]:
                return r.merge(clear, self.fail_candidate(reader, key, saved, Failure.PROTOCOL,
                    evidence, diagnostics=diagnostics))
            proposal = self.strategy.on_failure(snapshot, self.policy, 'protocol', evidence=evidence,
                                               diagnostics=diagnostics)
        effects = [clear]
        if proposal.objection:
            from .repairs import report
            effects.append(report(self, reader, proposal.objection))
        if proposal.memory:
            data = artifact(proposal.memory.payload)
            effects.append(Effect(artifacts=(data,)))
            state = replace(state, memory=data.reference)
        state = replace(state, reason=proposal.reason,
                        stopped=proposal.stopped if proposal.stopped is not None else state.stopped)
        if proposal.stop_request:
            data = artifact(proposal.stop_request.payload)
            effects.append(Effect(artifacts=(data,)))
            state = replace(state, stop_request=data.reference)
        elif proposal.stopped is not None:
            state = replace(state, stop_request=None)
        if proposal.reason and proposal.reason.endswith('_revision_allowance_exhausted'):
            state = replace(state, stopped=True)
        if proposal.draft:
            effects.append(candidates.begin(self, reader, owner, proposal.draft, state=state))
        elif proposal.helper:
            from .helpers import begin
            effects = effects[1:]  # Helper replaces the pending turn atomically.
            effects.extend((r.save_owner(reader, owner.obligation_id, state),
                            begin(self, reader, turn, proposal.helper, proposal.artifacts, parent=binding[2])))
        elif binding[2]:
            if proposal.attempt and proposal.attempt.review:
                effects.append(candidates.activate(self, reader, key, saved, entry_key,
                    proposal.attempt.checked, proposal.attempt.review))
            elif proposal.graph:
                # Strategy has performed the independent review decision.
                effects.extend((r.graph_changes(reader, snapshot.graph, proposal.graph),
                    candidates._row(reader, key, saved, entry_key, dict(saved['rows'][entry_key], done=True))))
            elif proposal.decision:
                effects.append(candidates.finish(self, reader, key, saved, proposal.decision.outcome,
                    exception=proposal.decision.exception, reason=proposal.reason, evidence=accepted.value.raw))
            else:
                raise ValueError('Review supplied no decision')
        else:
            if proposal.work and proposal.work[0].operation == 'page':
                from .helpers import page_effect
                state, extra = page_effect(self, reader, snapshot, turn, proposal.work[0], state)
                effects.append(extra)
            effects.append(r.save_owner(reader, owner.obligation_id, state))
            if proposal.disable_retrieval:
                from ..storage import CounterChange
                scope = 'owner:'+owner.obligation_id
                before = reader.counter(scope, 'retrieval_disabled')
                if not before:
                    effects.append(Effect(Changes(counters=(CounterChange(scope, 'retrieval_disabled', before, 1),))))
            if proposal.revision_charge or proposal.stop_charge:
                from ..storage import CounterChange
                scope, name = 'owner:'+owner.obligation_id, proposal.revision_charge or 'stop_requests'
                before = reader.counter(scope, name)
                effects.append(Effect(Changes(counters=(CounterChange(scope, name, before, before+1),))))
        return r.merge(*effects)

    def on_export(self, reader, invocation, accepted, name, epoch=0):
        graph = self.graph(reader)
        if accepted.failure:
            return r.record(reader, 'application.export', name, ['failed', accepted.failure.value, epoch, graph.revision])
        if invocation.request.job.kind == JobKind.ASSEMBLE:
            _, pending = self.work_graph(reader)
            location = pending.get(accepted.value.candidate.owner.obligation_id)
            if location:
                key, entry_key = location
                saved = candidates.submission(reader, key)
                row = dict(saved['rows'][entry_key], assembly=r.check(accepted.value))
                return r.merge(candidates._row(reader, key, saved, entry_key, row),
                    r.record(reader, 'application.export', name, ['done', graph.revision]))
            effect = r.graph_changes(reader, graph, accept_assembly(graph, accepted.value))
            return r.merge(effect, r.record(reader, 'application.export', name, ['done', graph.revision]))
        stage = accepted.value
        return r.record(reader, 'application.export', name,
            ['accepted', r.ref(invocation.request.operation_payload), r.receipt(stage.receipt), r.ref(stage.evidence),
             graph.revision, stage.request.allow_sorry])

    def export_usage(self, reader, invocation, result):
        """Charge each receipt before retry admission; replay never counts twice."""
        key = invocation.original_execution_id
        if reader.record('application.export_usage', key):
            return None
        usage = r.value(reader, 'application.export', 'usage') or [0, 0, 0]
        elapsed = captured = 0
        if result.output:
            try:
                report = codec.load_record(r.read(reader, result.output, 65536), limit=65536)
                for phase in report['phases']:
                    counts = [phase['elapsed_ms'], *((phase[label] or {}).get('observed', 0)
                              for label in ('stdout', 'stderr'))]
                    if any(type(value) is not int or value < 0 for value in counts):
                        raise ValueError('Invalid export usage')
                    elapsed += counts[0]
                    captured += sum(counts[1:])
            except (ValueError, KeyError, TypeError, AttributeError):
                # Unreadable telemetry cannot buy another full-budget attempt.
                elapsed, captured = invocation.request.job.limits.wall_ms, invocation.request.job.limits.output_bytes
        else:
            elapsed, captured = invocation.request.job.limits.wall_ms, invocation.request.job.limits.output_bytes
        total = [usage[0]+elapsed, usage[1]+captured,
                 usage[2]+sum(ref.byte_count for ref in result.staged_artifacts)]
        return r.merge(r.record(reader, 'application.export', 'usage', total),
                       r.record(reader, 'application.export_usage', key, True))

    def export_retry(self, reader, invocation):
        usage = ExportUsage(*(r.value(reader, 'application.export', 'usage') or [0, 0, 0]))
        try:
            remaining = self.policy.export_limits(usage,
                comparator=invocation.request.job.kind == JobKind.EXPORT_VERIFY)
        except ValueError:
            return False
        limits = invocation.request.job.limits
        return (remaining.wall_ms >= limits.wall_ms and remaining.output_bytes >= limits.output_bytes
                and remaining.evidence_bytes >= limits.evidence_bytes)
