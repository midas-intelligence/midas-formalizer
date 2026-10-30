"""Fork a pinned checkpoint with authenticated evidence and fresh local budgets."""
from dataclasses import replace

from ..controller import Effect
from ..domain import ProofNode, joint_coordinator, joint_targets, load_selection
from ..export import Library
from ..policy import load_config
from ..storage import Changes, NodeChange, open_reader
from . import preparation, records as r
from .service import commit


def fork(application, parent, directory, *, config=None, package=None, workflow=None, script=None):
    with open_reader(parent) as reader, reader.snapshot():
        frozen = reader.frozen_run()
        inputs = r.Inputs.load(r.read(reader, frozen.inputs))
        environment = application.environment(package or inputs.package, expected_id=frozen.environment_id)
        selection = load_selection(r.read(reader, inputs.selection, r.DATA), environment.selection_inputs())
        configuration = reader.record(r.SCOPE, 'configuration')
        config = config or load_config(r.read(reader, configuration.artifact if configuration else frozen.configuration, 16384))
        workflow = workflow or inputs.workflow
        instructions = tuple((role, r.read(reader, ref, 16384)) for role, ref in inputs.instructions)
        if script is None:
            script = []
            if workflow == 'scripted':
                for role in ('prover', 'reviewer', 'research', 'web_search'):
                    index = reader.counter('application.script', role)
                    while saved := reader.record('application.script:'+role, str(index)):
                        script.append((role, r.read(reader, saved.artifact, 4*1024**2)))
                        index += 1
        saved_library = reader.record(r.SCOPE, 'library')
        library = Library(environment.id, r.read(reader, saved_library.artifact, r.DATA)) if saved_library else None
        child = application.start(directory, package=environment, selection=selection, config=config,
            workflow=workflow, script=script, instructions=instructions, library=library, library_reader=reader,
            _lineage=(reader.run_id, reader.status_snapshot().revision))
        try:
            # Streams retain exact references without serializing a source tree.
            after = ''
            while page := reader.artifacts(after=after, limit=256):
                for reference in page:
                    child.writer.copy_artifact(reader, reference)
                after = page[-1].id
            after = ''
            while page := reader.receipt_ids(after=after, limit=256):
                for key in page:
                    child.writer.import_receipt(reader, key)
                after = page[-1]
            scopes = ('application.owners', 'application.submissions', 'application.jobs', 'application.archives', 'application.guidance')
            joint, coordinator = joint_targets(selection), joint_coordinator(selection)
            for scope in scopes:
                after = ''
                while page := reader.records(scope, after=after, limit=256):
                    effects = []
                    for saved in page:
                        if scope == 'application.owners':
                            state = r.Owner.load(r.read(reader, saved.artifact))
                            effects.append(r.save_owner(child.writer, saved.key, replace(state, submission=None,
                                stopped=bool(joint and saved.key != coordinator), reason=None, stop_request=None)))
                        else:
                            effects.append(Effect(Changes(records=(r.pointer(child.writer, scope, saved.key, saved.artifact),))))
                    commit(child.writer, 'fork-records:'+scope+':'+page[-1].key, r.merge(*effects))
                    after = page[-1].key
            retired = r.value(reader, r.SCOPE, 'retired')
            if retired:
                commit(child.writer, 'fork-retired', r.record(child.writer, r.SCOPE, 'retired', retired))
            nodes, after = {}, ''
            while page := reader.nodes(after=after, limit=256):
                changes = []
                for saved in page:
                    node = saved.node
                    nodes[node.owner.obligation_id] = node
                    try:
                        old = child.writer.node(node.owner.obligation_id).version
                    except KeyError:
                        old = None
                    changes.append(NodeChange(old, ProofNode(node.owner, node.environment_id, node.interface, node.binding)))
                commit(child.writer, 'fork-nodes:'+page[-1].node.owner.obligation_id,
                    Effect(Changes(nodes=tuple(changes), graph_revision=child.writer.status_snapshot().graph_revision)))
                after = page[-1].node.owner.obligation_id
            # Dependencies first also preserves exact assembly provenance.
            parents, remaining = {}, {}
            for key, node in nodes.items():
                deps = {d.owner.obligation_id for d in node.selected.candidate.dependencies} if node.selected else set()
                remaining[key] = len(deps)
                for dep in deps:
                    parents.setdefault(dep, []).append(key)
            ready = [key for key, count in remaining.items() if count == 0]
            visited = 0
            while ready:
                key = ready.pop()
                node = nodes[key]
                if node.selected:
                    commit(child.writer, 'fork-proof:'+key, Effect(Changes(nodes=(
                        NodeChange(child.writer.node(key).version, node),),
                        graph_revision=child.writer.status_snapshot().graph_revision)))
                visited += 1
                for parent_key in parents.get(key, ()):
                    remaining[parent_key] -= 1
                    if remaining[parent_key] == 0:
                        ready.append(parent_key)
            if visited != len(nodes):
                raise ValueError('Parent proof graph is cyclic')
            for name in ('integration', 'verification'):
                value = r.value(reader, 'application.export', name)
                if value and value[0] == 'accepted' and value[4] == reader.status_snapshot().graph_revision:
                    value[4] = child.writer.status_snapshot().graph_revision
                    commit(child.writer, 'fork-export:'+name, r.record(child.writer, 'application.export', name, value))
            preparation.ready(child.workflow.checker)
            application.validate(child)
            commit(child.writer, 'fork-ready', r.record(child.writer, r.SCOPE, 'fork_ready', True))
            return child
        except BaseException:
            # No application work may run while unwinding failed re-admission.
            child.executor.drain(timeout=0)
            child.writer.close()
            child.closed = True
            raise
