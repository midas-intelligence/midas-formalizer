"""Command workflows and concrete service ownership; the controller owns all run work."""
from pathlib import Path
from uuid import uuid4
import time

from ..checker import Checker
from ..controller import Controller, Effect, artifact
from ..domain import (FrozenSelection, OwnerVersion, ProofNode, ProofState,
    encode_selection, joint_coordinator, joint_targets, load_selection)
from ..domain import encoding as codec
from ..environment import Environment
from ..executor import Executor
from ..export import Export
from ..policy import Config, Policy, load_config
from ..storage import (Changes, CounterChange, FrozenRun, NodeChange, claim_writer, create, open_reader)
from ..strategy import Context, Subject
from ..tools import SearchService
from . import candidates, preparation, providers, records as r
from .workflow import Workflow
from .inspection import Inspector
from ..status import project


class Run:
    """Exclusive application handle. Only request_stop is cross-thread safe."""
    def __init__(self, writer, executor, flow):
        self.writer, self.executor, self.workflow = writer, executor, flow
        self.controller = Controller(writer, executor, flow.policy, flow.operations,
                                     planner=flow.plan, services=flow.services)
        self.closed = False

    def step(self):
        return self.controller.step()

    def run(self, *, poll_interval=.01, observe=None):
        next_update = 0
        def publish(progress, completion):
            nonlocal next_update
            now = time.monotonic()
            if observe is not None and (completion.exit_code is not None or now >= next_update):
                next_update = now+1
                observe(project(self.writer, self.workflow.policy, workflow=self.workflow.mode,
                                progress=progress, completion=completion))
        result = self.controller.run(self.workflow.progress, poll_interval=poll_interval,
                                     observe=publish if observe is not None else None)
        commit(self.writer, 'completion:'+str(uuid4()), r.record(self.writer, r.SCOPE, 'completion',
            [self.writer.status_snapshot().revision, result.proof_state, result.integration_state.value,
             result.export_verification_state.value, result.outcome, list(result.reasons), result.exit_code]))
        return result

    def request_stop(self):
        self.controller.request_stop()

    def submit(self, payload, *, owner=None, guidance=None):
        if self.workflow.export_only:
            raise ValueError('Export handle does not accept candidates')
        graph = self.workflow.graph(self.writer)
        key = owner or joint_coordinator(self.workflow.selection) or (graph.roots[0] if len(graph.roots) == 1 else None)
        if key is None:
            raise ValueError('Choose the owner for a run with multiple independent roots')
        node = self.writer.node(key).node
        if node.state != ProofState.UNRESOLVED:
            raise ValueError('Retire the selected plan before replacing its proof')
        if r.value(self.writer, 'application.pending', key) is not None:
            raise ValueError('Owner has pending model/helper work')
        snapshot = self.workflow.snapshot(self.writer, node.owner)
        if guidance is None:
            guidance = r.pack(dict(rationale='Supplied candidate', proof='', proof_departures='', children={}))
        proposal = self.workflow.strategy.submit(snapshot, self.workflow.policy, payload, guidance=guidance)
        effect = candidates.begin(self.workflow, self.writer, node.owner, proposal.draft, supplied=True)
        return self.controller.publish('submit:'+proposal.draft.id, effect)

    def materialize(self, destination):
        stage = self.workflow.stage(self.writer, 'verification')
        if stage is None:
            raise ValueError('No independently verified export is available')
        result = self.workflow.export.materialize(stage, self.workflow.selection,
            self.workflow.graph(self.writer), self.writer, destination, protected=(self.writer.directory,))
        # Controller.run has already drained on completion; publication is still
        # protected by this application's exclusive writer session.
        effect = r.record(self.writer, 'application.export', 'publication',
                          [str(Path(destination).absolute()), stage.request.sha256])
        commit(self.writer, 'publication:'+stage.request.sha256+':'+str(Path(destination).absolute()), effect)
        return result

    def close(self):
        if self.closed:
            return
        if not self.controller.closed and not self.controller.drain(timeout=5):
            raise RuntimeError('Run still owns unconfirmed workers; reconcile termination before closing')
        self.executor.drain(timeout=0)
        self.writer.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def commit(writer, key, effect, *, frozen=None):
    """Initialization/publication only. No worker request bypasses the controller."""
    if effect.work or effect.cancel:
        raise ValueError('Work must be published through the controller')
    artifacts = tuple(writer.publish(a.source, reference=a.reference, max_bytes=a.reference.byte_count)
                      for a in effect.artifacts)
    with writer.transaction(writer.session) as tx:
        for item in artifacts:
            tx.register(item)
        if frozen is not None:
            tx.freeze(frozen)
        tx.apply(key, effect.changes)


class Application:
    def __init__(self, cache_directory, *, transports=None, codex=None, checker_factory=Checker, search_factory=SearchService):
        self.cache = Path(cache_directory).absolute()
        self.transports = transports
        self.codex = codex
        self.checker_factory, self.search_factory = checker_factory, search_factory
        self._inspector = Inspector()

    package = staticmethod(preparation.package)

    def environment(self, package, *, expected_id=None, toolchain=None):
        if isinstance(package, Environment):
            if expected_id is not None and package.id != expected_id:
                raise ValueError('Environment hash changed')
            package.admit()
            return package
        return Environment.restore(package, expected_id=expected_id, toolchain=toolchain)

    def select(self, package, request, **options):
        environment = self.environment(package)
        return preparation.select(self.checker_factory(environment, self.cache), request, **options)

    def start(self, directory, *, package, selection, config=Config(), workflow='live',
              script=(), instructions=(), library=None, library_reader=None, imports=(), cancellation=None, _lineage=None):
        environment = self.environment(package)
        checker = self.checker_factory(environment, self.cache)
        if isinstance(selection, bytes):
            selection = load_selection(selection, environment.selection_inputs())
        elif not isinstance(selection, FrozenSelection):
            selection = preparation.select(checker, selection, cancellation=cancellation)
        if selection.environment_id != environment.id:
            raise ValueError('Selection environment differs from package')
        if workflow != 'supplied' and not config.prover.configured:
            raise ValueError('Model workflow requires a configured prover route')
        if not 1 <= len(selection.identities) <= 256:
            raise ValueError('Expected 1 through 256 selected roots')
        # Validate all caller-owned bytes before creating a run directory.
        instructions = tuple(instructions)
        if len({role for role, _ in instructions}) != len(instructions):
            raise ValueError('Duplicate instruction role')
        from ..prompt import prover, reviewer, researcher
        defaults = {'prover': prover, 'reviewer': reviewer, 'researcher': researcher}
        explicit = dict(instructions)
        if explicit.keys() - defaults.keys():
            raise ValueError('Unknown instruction role')
        for role, data in explicit.items():
            if type(data) is not bytes or len(data) > 16384:
                raise ValueError('Instructions must be bounded UTF-8 bytes')
            data.decode()
        script = tuple(script)
        for role, data in script:
            if role not in ('prover', 'reviewer', 'research') or type(data) is not bytes or len(data) > 4*1024**2:
                raise ValueError('Invalid scripted response')
        if script and workflow != 'scripted':
            raise ValueError('Scripted responses require scripted workflow')
        if library is not None and (library.environment_id != environment.id or library_reader is None):
            raise ValueError('Library requires its admitted environment and source reader')
        from ..domain.identity import validate_imports
        validate_imports(list(imports))
        if imports and not set(imports) <= environment.manifest.modules.keys():
            raise ValueError('Advertised import is outside the prepared package')
        writer = create(directory)
        try:
            selected = writer.put(encode_selection(selection), max_bytes=r.DATA)
            frozen_instructions = tuple((role, writer.put(explicit.get(role, module.INSTRUCTIONS),
                                                           max_bytes=16384)) for role, module in defaults.items())
            inputs = r.Inputs(selected, str(environment.directory), workflow, frozen_instructions)
            inputs_ref = writer.put(inputs.payload, max_bytes=r.META)
            config_ref = writer.put(config.encode(), max_bytes=16384)
            frozen = FrozenRun(environment.id, inputs_ref, config_ref, *(_lineage or (None, None)))
            nodes, effects = [], []
            joint = joint_targets(selection)
            coordinator = joint_coordinator(selection)
            for index, (target, ident) in enumerate(zip(selection.selected.targets, selection.identities)):
                iface = artifact(codec.encode({'constant': codec.load_json(target.constant),
                    'signature': {name: codec.decode(getattr(ident, name)) for name in ('target', 'universes', 'context')}}))
                subjects = tuple(Subject('target', t.selector) for t, _ in joint) if ident.id == coordinator else (Subject('target', target.selector),)
                context = Context(selection.id, subjects, imports=tuple(imports),
                    definition_targets=tuple(t.selector for t, _ in joint if t.kind == 'def') if ident.id == coordinator else ())
                context_data, identity_data = artifact(context.payload), artifact(ident.reference_payload())
                owner = OwnerVersion(ident.id, 0)
                nodes.append(NodeChange(None, ProofNode(owner, environment.id, iface.reference)))
                effects.extend((Effect(artifacts=(iface, context_data, identity_data)),
                    r.save_owner(writer, ident.id, r.Owner(context_data.reference, identity_data.reference, index,
                        stopped=bool(joint and ident.id != coordinator)))))
            effects.append(Effect(Changes(nodes=tuple(nodes), roots=tuple(i.id for i in selection.identities), graph_revision=0)))
            if library:
                item = artifact(library.payload)
                from ..domain import ArtifactRef
                from ..checker.rebuild import module_files
                for ref in module_files(library.decode()).values():
                    writer.copy_artifact(library_reader, ref)
                effects.append(r.save(writer, r.SCOPE, 'library', item.source))
            counters = {}
            for role, data in script:
                index = counters.get(role, 0)
                counters[role] = index+1
                effects.append(r.save(writer, 'application.script:'+role, str(index), data))
            commit(writer, 'initialize', r.merge(*effects), frozen=frozen)
            return self._build(writer, environment, checker, selection, config, inputs, library=library)
        except BaseException:
            writer.close()
            raise

    def _build(self, writer, environment, checker, selection, config, inputs, *, library=None,
               export_only=False, allow_sorry=False):
        script = providers.Script(writer) if inputs.workflow == 'scripted' and not export_only else None
        provider = providers.construct(config, transports=self.transports, script=script, codex=self.codex) if inputs.workflow != 'supplied' and not export_only else None
        instructions = tuple((role, r.read(writer, ref, 16384)) for role, ref in inputs.instructions)
        search = self.search_factory() if provider else None
        if search is not None and search.cache_directory is None:
            search.cache_directory = self.cache/'search'
        if search is not None and hasattr(environment.manifest, 'inventory') and search.index is None:
            sources = preparation.FrozenSources(environment)
            search.index, search.source_reader = sources.index, sources
            if search.prepared_path is None:
                search.prepared_path = environment.directory/preparation.SEARCH_INDEX
        flow = Workflow(selection, Policy(config), checker, Export(checker), mode=inputs.workflow,
            provider=provider, search=search,
            instructions=instructions, script=script, export_only=export_only, allow_sorry=allow_sorry, library=library)
        limits = config.scheduling
        workspace = writer.directory/'workers'
        workspace.mkdir(mode=0o700, exist_ok=True)
        executor = Executor(workspace,
            workers=limits.model_concurrency+limits.compiler_concurrency+limits.network_concurrency,
            network_slots=limits.network_concurrency,
            compiler_slots=limits.compiler_concurrency, compiler_memory_mib=limits.compiler_memory_mib)
        try:
            return Run(writer, executor, flow)
        except BaseException:
            executor.drain(timeout=0)
            raise

    def resume(self, directory, *, package=None, toolchain=None, increases=None,
               export_only=False, allow_sorry=False, cancellation=None):
        writer = claim_writer(directory)
        try:
            frozen = writer.frozen_run()
            if frozen.parent_run_id and not r.value(writer, r.SCOPE, 'fork_ready'):
                raise ValueError('Fork initialization did not complete')
            if not writer.roots(limit=1):
                raise ValueError('Run initialization did not complete')
            inputs = r.Inputs.load(r.read(writer, frozen.inputs))
            environment = self.environment(package or inputs.package, expected_id=frozen.environment_id, toolchain=toolchain)
            selection = load_selection(r.read(writer, inputs.selection, r.DATA), environment.selection_inputs())
            configuration = writer.record(r.SCOPE, 'configuration')
            config = load_config(r.read(writer, configuration.artifact if configuration else frozen.configuration, 16384))
            checker = self.checker_factory(environment, self.cache)
            preparation.ready(checker, cancellation=cancellation)
            if increases:
                previous = config
                config = config.increase(increases)
                effect = r.save(writer, r.SCOPE, 'configuration', config.encode())
                if config.export != previous.export:
                    epoch = writer.counter(r.SCOPE, 'export_epoch')
                    effect = r.merge(effect, Effect(Changes(counters=(
                        CounterChange(r.SCOPE, 'export_epoch', epoch, epoch+1),))))
                commit(writer, 'increase:'+str(uuid4()), effect)
            library_record = writer.record(r.SCOPE, 'library')
            library = None
            if library_record:
                from ..export import Library
                library = Library(environment.id, r.read(writer, library_record.artifact, r.DATA))
            run = self._build(writer, environment, checker, selection, config, inputs,
                              library=library, export_only=export_only, allow_sorry=allow_sorry)
            try:
                self.validate(run)
                run.controller.resume()
                return run
            except BaseException:
                # Validation precedes any controller step. Do not apply saved
                # work while unwinding a failed validation.
                run.executor.drain(timeout=0)
                run.closed = True
                raise
        except BaseException:
            writer.close()
            raise

    def validate(self, run):
        """Re-admit current proof authority in bounded pages, without external work."""
        reader, flow = run.writer, run.workflow
        admitted = {}
        def receipt_value(receipt):
            key = receipt.original_execution_id
            if key not in admitted:
                invocation = reader.invocation(key)
                result = reader.result(key)
                if not reader.verify_receipt(receipt, invocation, result):
                    raise ValueError('Current proof has an unauthenticated receipt')
                operation = flow.operations.for_kind(invocation.request.job.kind)
                if operation is None:
                    # Export-only recovery may still authenticate saved model
                    # reviews using their frozen route with no transport calls.
                    from ..controller import model_operation
                    from ..provider import Provider
                    operation = model_operation(Provider(()), flow.reduce)
                accepted = operation.admit(invocation, result, receipt, reader, reader)
                if accepted.failure:
                    raise ValueError('Current proof has failed admission')
                admitted[key] = accepted.value
            return admitted[key]
        for node in flow.graph(reader).nodes:
            _, state = r.owner_state(reader, node.owner.obligation_id)
            Context.load(r.read(reader, state.context, 8*1024**2))
            ident = candidates.identity(reader, state, flow.selection.environment_id)
            if ident.id != node.owner.obligation_id:
                raise ValueError('Saved owner identity changed')
            if node.binding and receipt_value(node.binding.receipt).admitted != node.binding:
                raise ValueError('Saved binding differs from admitted evidence')
            for checked in (node.selected, node.assembly):
                if checked and receipt_value(checked.receipt) != checked:
                    raise ValueError('Saved proof differs from admitted evidence')
            if node.review:
                value = receipt_value(node.review.receipt)
                if value == node.selected:
                    invocation = reader.invocation(node.review.receipt.original_execution_id)
                    binding = r.value(reader, 'application.jobs', invocation.request.job.semantic_key)
                    if not candidates.submission(reader, binding[1])['supplied']:
                        raise ValueError('Missing supplied review authority')
                else:
                    from ..strategy import interpret
                    from ..strategy.state import candidate_hash
                    from ..strategy import Turn
                    invocation = reader.invocation(node.review.receipt.original_execution_id)
                    binding = r.value(reader, 'application.jobs', invocation.request.job.semantic_key)
                    turn = Turn.load(r.read(reader, r.unref(binding[1])))
                    response = interpret(r.read(reader, value.raw, 8*1024**2), role='reviewer', contract=turn.contract)
                    if turn.candidate_hash != candidate_hash(node.selected.candidate) or response.value['verdict'] != 'accept':
                        raise ValueError('Saved review differs from admitted output')
            if state.submission:
                saved = candidates.submission(reader, state.submission)
                candidates.entries(reader, saved)
                for row in saved['rows'].values():
                    checked = r.uncheck(row.get('checked'))
                    if checked and receipt_value(checked.receipt) != checked:
                        raise ValueError('Pending proof differs from admitted evidence')
        for name in ('integration', 'verification'):
            stage = flow.stage(reader, name)
            if stage and receipt_value(stage.receipt) != stage:
                raise ValueError('Saved export differs from admitted evidence')

    def inspect(self, directory):
        return self._inspector.inspect(directory)

    def nodes(self, directory, *, after='', limit=100, state=None):
        with open_reader(directory) as reader:
            return reader.nodes(after=after, limit=limit, state=state)

    def submit(self, directory, payload, **options):
        with self.resume(directory) as run:
            run.submit(payload, **options)
            return run.run()

    def export(self, directory, destination, *, allow_sorry=False, **options):
        with self.resume(directory, export_only=True, allow_sorry=allow_sorry, **options) as run:
            completion = run.run()
            if completion.exit_code == 0:
                run.materialize(destination)
            return completion

    def verify(self, directory, *, package, config=Config(), cancellation=None):
        from .diagnostics import verify
        return verify(self, directory, package=package, config=config, cancellation=cancellation)

    def cache_probe(self, directory, *, config, cancellation=None):
        from .diagnostics import cache_probe
        return cache_probe(self, directory, config=config, cancellation=cancellation)

    def fork(self, parent, directory, *, config=None, package=None, workflow=None, script=None):
        from .forking import fork
        return fork(self, parent, directory, config=config, package=package, workflow=workflow, script=script)
