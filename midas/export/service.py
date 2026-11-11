"""Export planning, checked composition, source integration and stage admission."""
from hashlib import sha256

from ..checker import CompilerRequest
from ..checker.requests import _candidate, read, signature
from ..domain import AdmittedCheck, ArtifactRef, CheckKind, JobKind, ProofReference, ProofState
from ..domain import encoding as codec
from ..source import GeneratedSource, add_imports, parse_identifier
from . import proofs
from .layout import Library, materialize
from .protocol import AdmittedStage, Request, Work, file_rows, proof_ref, proof_row


def _metadata(profile, roots, maximum, toolchain, *, parent=None, files=(), integration=None,
              allow_sorry=False, nodes=(), lean_options=None):
    return {'format': 'midas-export-v1', 'profile': profile, 'roots': [proof_row(r) for r in roots],
        'parent': proof_row(parent) if parent else None, 'files': [[p, r.to_dict()] for p, r in files],
        'integration': integration, 'maximum': maximum, 'toolchain': toolchain,
        'allow_sorry': allow_sorry, 'nodes': [n.row() for n in nodes],
        **({'lean_options': list(lean_options)} if lean_options is not None else {})}


def _selected(selection, graph, allow_sorry):
    if type(allow_sorry) is not bool:
        raise ValueError('Expected a boolean sorry option')
    if allow_sorry:
        nodes = proofs.snapshot(selection, graph)
        return tuple(n.proof for n in nodes if n.proof is not None), nodes
    return proofs.roots(selection, graph), ()


def _source(selection, graph, roots, reader, allow_sorry, available, *, aliases=(), generated=None, regions=None,
            calls=None, lean_options):
    prefix = (f'set_option maxHeartbeats {lean_options[0]}\n'
              f'set_option maxRecDepth {lean_options[1]}\n\n').encode()
    if allow_sorry:
        return proofs.render_partial(selection, graph, reader, available=available, aliases=aliases,
                                     generated=generated, regions=regions, calls=calls, command_prefix=prefix)
    return proofs.render(selection, roots, reader, available=available, aliases=aliases,
                         generated=generated, regions=regions, calls=calls, command_prefix=prefix)


def _request(value, metadata, refs):
    value = dict(value, export=metadata)
    value['inputs'] = [r.to_dict() for r in dict.fromkeys(refs)]
    return Request(codec.encode(value))


def current(stage, selection, graph, *, profile=None, allow_sorry=None):
    if not isinstance(stage, AdmittedStage):
        return False
    request = stage.request
    try:
        roots, nodes = _selected(selection, graph, request.allow_sorry)
    except ValueError:
        return False
    return (request.environment_id == selection.environment_id and request.selection_id == selection.id
        and (profile is None or request.profile == profile)
        and (allow_sorry is None or request.allow_sorry == allow_sorry)
        and request.proofs == roots and request.nodes == nodes)


def plan(selection, graph, *, profile='source', integration=None, verification=None, limit=128, allow_sorry=False):
    """Read only selected proof metadata; rendering and artifact reads happen later."""
    if (profile not in ('source', 'library') or type(limit) is not int or not 1 <= limit <= 1000
            or type(allow_sorry) is not bool):
        raise ValueError('Invalid export planning bounds/profile')
    if graph.roots != tuple(dict.fromkeys(i.id for i in selection.identities)):
        raise ValueError('Export graph differs from selection')
    nodes = {n.owner.obligation_id: n for n in graph.nodes}
    reachable = graph.reachable() if not allow_sorry else ()
    work = []
    for node in graph.nodes:
        if node.owner.obligation_id not in reachable or node.state != ProofState.CONDITIONAL:
            continue
        children = tuple(nodes[d.owner.obligation_id].proof for d in node.selected.candidate.logical_dependencies)
        if children and all(children):
            work.append(Work(JobKind.ASSEMBLE, node.owner, children))
            if len(work) == limit:
                break
    if work:
        return tuple(work)
    try:
        roots, snapshots = _selected(selection, graph, allow_sorry)
    except ValueError:
        return ()
    owner = snapshots[0].owner if snapshots else roots[0].owner
    if (not current(integration, selection, graph, profile=profile, allow_sorry=allow_sorry)
            or integration.request.kind != JobKind.INTEGRATE):
        return (Work(JobKind.INTEGRATE, owner, roots),)
    if (not current(verification, selection, graph, profile=profile, allow_sorry=allow_sorry)
            or verification.request.kind != JobKind.EXPORT_VERIFY
            or verification.request.integration != integration.request.sha256):
        return (Work(JobKind.EXPORT_VERIFY, owner, roots),)
    return ()


class Export:
    def __init__(self, checker):
        self.checker = checker

    plan = staticmethod(plan)
    current = staticmethod(current)

    def prepare_assembly(self, selection, graph, owner, parent_request, reader, *, maximum=16*1024**3):
        node = graph.node(owner)
        if node.selected is None or node.selected.kind != CheckKind.CONDITIONAL:
            raise ValueError('Assembly requires a selected conditional plan')
        parent = node.selected
        value = parent_request.decode()
        if (value['selection'] != selection.id or value['environment'] != self.checker.environment.id
                or value['candidate'] != _candidate(parent.candidate) or value['operation'] != 'proof'):
            raise ValueError('Assembly parent request differs from selected check')
        certificate = proofs.load(reader, parent.evidence, closed=False)
        if certificate['request'] != parent_request.sha256:
            raise ValueError('Assembly parent certificate differs from checked request')
        references, children, dependencies = [], [], []
        for dependency in parent.candidate.logical_dependencies:
            child = graph.node(dependency.owner).proof
            if child is None:
                raise ValueError('Assembly requires verified children')
            interface = codec.decode(read(reader, dependency.interface))
            codec.fields(interface, ('constant', 'signature'))
            if dependency.origin.value == 'abstracted_lemma':
                interface = dict(interface, constant=parse_identifier(dependency.alias))
            dependencies.append({'parameter': parse_identifier(dependency.alias),
                'origin': dependency.origin.value, 'interface': interface})
            children.append(proofs.load(reader, child.evidence))
            references.append(child)
        if not proofs.same(value['packet']['dependencies'], dependencies):
            raise ValueError('Assembly dependencies differ from the conditional compiler request')
        composed = proofs.compose(certificate, children, dependencies)
        packet = dict(value['packet'], candidate=None, dependencies=[], certificate=composed)
        packet.pop('dependency_protocol', None)
        original = packet['source'].encode()
        augmented = add_imports(GeneratedSource(original), tuple(composed.get('imports', ())),
            available=frozenset(self.checker.environment.manifest.modules),
            current_module=packet['logical'][:-5].replace('/', '.'))
        packet.update(source=augmented.data.decode(), offset=packet['offset'] + len(augmented.data) - len(original),
                      imports=composed.get('imports', []))
        proofs.bind_peers(selection, graph, parent.candidate, packet, reader,
                          available=frozenset(self.checker.environment.manifest.modules))
        value.update(operation='replay', packet=packet)
        parent_ref = ProofReference(owner, parent.receipt, parent.evidence)
        refs = [ArtifactRef.from_dict(r) for r in value['inputs']]
        refs += [parent.evidence, *(r.evidence for r in references)]
        return _request(value, _metadata('source', references, maximum,
            self.checker.environment.toolchain_id, parent=parent_ref), refs)

    def prepare_integration(self, selection, graph, reader, staging, *, profile='source', library=None,
                            maximum=16*1024**3, allow_sorry=False, max_heartbeat=2_000_000, max_rec_depth=1000):
        from ..domain.records import natural
        natural(max_heartbeat, 'max_heartbeat')
        natural(max_rec_depth, 'max_rec_depth', positive=True)
        lean_options = (max_heartbeat, max_rec_depth)
        if profile not in ('source', 'library'):
            raise ValueError('Unsupported export profile')
        environment = self.checker.environment
        settings = environment.manifest.settings
        if settings.options or settings.plugins or settings.variables:
            raise ValueError('Source export does not admit compiler overrides/plugins')
        if library is not None and (profile != 'library' or not isinstance(library, Library)
                                   or library.environment_id != environment.id):
            raise ValueError('Library differs from export profile/environment')
        roots, nodes = _selected(selection, graph, allow_sorry)
        owner = nodes[0].owner if nodes else roots[0].owner
        generated, regions, calls = [], [], []
        source = _source(selection, graph, roots, reader, allow_sorry, frozenset(environment.manifest.modules),
                         generated=generated, regions=regions, calls=calls, lean_options=lean_options)

        def save(data):
            ref = staging.write_bounded(data, max_bytes=64*1024**2)
            ref.verify(data, max_bytes=64*1024**2)
            return ref

        source_ref = save(source.data)
        inventory_data = library.payload if library else codec.encode([])
        inventory_ref = save(inventory_data)
        # Preparation can use separate read/staging capabilities.
        class SourceReader:
            def read_artifact(self, ref, *, max_bytes):
                if ref == inventory_ref:
                    return inventory_ref.verify(inventory_data, max_bytes=max_bytes)
                return source_ref.verify(source.data, max_bytes=max_bytes) if ref == source_ref else read(reader, ref, max_bytes)
        value = self.checker.prepare('audit', selection, owner, source_ref, SourceReader(), allow_sorry=allow_sorry).decode()
        rows = library.decode() if library else []
        value['operation'] = 'integrate'
        value['packet']['inventory'] = inventory_ref.to_dict()
        value['packet']['share'] = generated
        value['packet']['regions'] = regions
        value['packet']['calls'] = calls
        files = [('src/' + selection.selected.request.source, source_ref),
                 ('inventory.bin', inventory_ref),
                 ('lean-toolchain', save(('leanprover/lean4:v' + settings.version + '\n').encode()))]
        from ..checker.rebuild import module_files
        files.extend(('src/' + path, ref) for path, ref in module_files(rows).items())
        refs = [r.evidence for r in roots] + [n.interface for n in nodes] + [r for _, r in files]
        request = _request(value, _metadata(profile, roots, maximum, environment.toolchain_id, files=sorted(files),
            allow_sorry=allow_sorry, nodes=nodes, lean_options=lean_options), refs)
        self._layout(request.decode(), SourceReader(), source=source.data)
        return request

    def prepare_verification(self, integration, selection, graph, reader, staging):
        """Reuse Lean-proven duplicates before independently checking the reduced source."""
        if not current(integration, selection, graph) or integration.request.kind != JobKind.INTEGRATE:
            raise ValueError('Verification requires the current admitted integration')
        value = integration.request.decode()
        evidence = codec.decode(read(reader, integration.evidence))
        self.checker._audit_result(evidence, value['packet'], integration.request.sha256)
        aliases = evidence.get('aliases', [])
        value['packet'].pop('share', None)
        regions = value['packet'].pop('regions')
        calls = value['packet'].pop('calls')
        inputs = list(integration.request.inputs)
        if regions:
            from .projection import project
            source = project(value['packet']['source'].encode(), regions, evidence['layout'], aliases, calls=calls)
            reference = staging.write_bounded(source.data, max_bytes=64*1024**2)
            reference.verify(source.data, max_bytes=64*1024**2)
            path = 'src/' + value['packet']['logical']
            value['export']['files'] = [[p, reference.to_dict() if p == path else ref]
                                        for p, ref in value['export']['files']]
            value['packet']['source'] = source.data.decode()
            inputs.append(reference)
        value['operation'] = 'audit'
        metadata = dict(value['export'], integration=integration.request.sha256)
        return _request(value, metadata, (*inputs, integration.evidence))

    def _layout(self, value, reader, *, source=None):
        """Profile validation also runs on recovered receipts, before stage authority."""
        environment, packet, metadata = self.checker.environment, value['packet'], value['export']
        if value['environment'] != environment.id:
            raise ValueError('Export environment differs')
        if metadata['toolchain'] != environment.toolchain_id:
            raise ValueError('Export toolchain pin differs')
        files = file_rows(metadata['files'], metadata['maximum'])
        from ..checker.rebuild import load_inventory, module_files, trusted_roots
        rows = load_inventory(packet, reader)
        trusted_roots(environment, rows)
        if metadata['profile'] == 'source' and rows:
            raise ValueError('Source-only profile cannot contain a library')
        expected = {'src/' + packet['logical'], 'lean-toolchain', 'inventory.bin'}
        if files.get('inventory.bin') != ArtifactRef.from_dict(packet['inventory']):
            raise ValueError('Export inventory differs from its audit input')
        for path, ref in module_files(rows).items():
            path = 'src/' + path
            if path in expected or files.get(path) != ref:
                raise ValueError('Exported dependency source/asset differs from audit input')
            expected.add(path)
        for row in rows:
            object_path = environment.manifest.modules.get(row.name)
            if object_path is None or object_path.startswith('toolchain/'):
                raise ValueError('Unadmitted exported dependency')
            for suffix, digest in row.compiled:
                item = environment.manifest.inventory.by_path.get(object_path[:-6] + suffix)
                if item is None or item.sha256 != digest:
                    raise ValueError('Exported dependency compiled identity differs')
        if set(files) != expected:
            raise ValueError('Unexpected export files')
        expected_toolchain = ('leanprover/lean4:v' + environment.manifest.settings.version + '\n').encode()
        toolchain = files['lean-toolchain']
        if toolchain.sha256 != sha256(expected_toolchain).hexdigest() or toolchain.byte_count != len(expected_toolchain):
            raise ValueError('Exported toolchain differs')
        if source is not None:
            files['src/' + packet['logical']].verify(source, max_bytes=64*1024**2)

    def execute(self, frozen, context):
        # Checker owns staging, cancellation, process limits, failures and receipts.
        return self.checker.execute(frozen, context)

    def prepare_recheck(self, request, reader):
        """Recheck bundle sources, using frozen dependencies when explicitly trusted.

        The returned checker audit grants no run-state authority. An application
        issues it through the same reserved compiler loop as any other audit.
        """
        value = request.decode()
        if request.kind != JobKind.EXPORT_VERIFY or value['export']['toolchain'] != self.checker.environment.toolchain_id:
            raise ValueError('Standalone audit requires the exported toolchain pin')
        files = file_rows(value['export']['files'], value['export']['maximum'])
        packet = value['packet']
        from ..checker.rebuild import load_inventory, trusted_roots
        rows = load_inventory(packet, reader)
        if any(row.source is None for row in rows):
            if request.environment_id != self.checker.environment.id:
                raise ValueError('Trusted compiled bundle requires its original frozen environment')
            trusted_roots(self.checker.environment, rows)
        data = read(reader, files['src/' + packet['logical']])
        if data.decode() != packet['source']:
            raise ValueError('Standalone source differs from saved verification')
        from ..checker.rebuild import module_files
        for path, ref in module_files(rows).items():
            if files.get('src/' + path) != ref:
                raise ValueError('Standalone module differs from exported files')
        value.pop('export')
        value.update(environment=self.checker.environment.id, inputs=[r.to_dict() for r in dict.fromkeys(files.values())])
        return CompilerRequest(codec.encode(value))

    def admit_receipt(self, request, invocation, result, receipt, reader, authority, *,
                      selection, graph, parent_request=None, integration=None):
        value = request.validate_for(invocation.request, reader)
        if value['selection'] != selection.id or value['environment'] != selection.environment_id:
            raise ValueError('Export receipt differs from selection')
        if request.kind == JobKind.ASSEMBLE:
            if parent_request is None:
                raise ValueError('Assembly admission requires its checked parent request')
            expected = self.prepare_assembly(selection, graph, invocation.request.job.owner,
                parent_request, reader, maximum=value['export']['maximum'])
            if expected.payload != request.payload:
                raise ValueError('Assembly receipt inputs are stale or changed')
        else:
            roots, nodes = _selected(selection, graph, request.allow_sorry)
            if request.proofs != roots or request.nodes != nodes:
                raise ValueError('Export receipt roots are stale')
            owner = nodes[0].owner if nodes else roots[0].owner
            packet = value['packet']
            codec.fields(packet, ('source', 'logical', 'offset', 'targets', 'inventory',
                                  *(('allow_sorry',) if request.allow_sorry else ()),
                                  *(('share', 'regions', 'calls') if request.kind == JobKind.INTEGRATE else ())))
            targets = [{'constant': codec.load_json(target.constant), 'signature': signature(identity)}
                       for target, identity in zip(selection.selected.targets, selection.identities)]
            if (value['candidate'] is not None or value['owner'] != [owner.obligation_id, owner.generation]
                    or packet['logical'] != selection.selected.request.source or packet['offset'] != 0
                    or not proofs.same(packet['targets'], targets)):
                raise ValueError('Export audit targets differ from the selected signatures')
            self._layout(value, reader)
            if request.kind == JobKind.INTEGRATE:
                generated, regions, calls = [], [], []
                source = _source(selection, graph, roots, reader, request.allow_sorry,
                    frozenset(self.checker.environment.manifest.modules), generated=generated, regions=regions, calls=calls,
                    lean_options=value['export']['lean_options'])
                if (source.data.decode() != value['packet']['source'] or generated != packet['share']
                        or regions != packet['regions'] or calls != packet['calls']):
                    raise ValueError('Integration differs from selected proof sources')
            else:
                reference = dict(request.files)['src/' + packet['logical']]
                class ExactSource:
                    def write_bounded(self, data, *, max_bytes):
                        reference.verify(data, max_bytes=max_bytes)
                        return reference
                if (integration is None or self.prepare_verification(integration, selection, graph, reader,
                        ExactSource()).payload != request.payload):
                    raise ValueError('Verification differs from current integration')
        admitted = self.checker.admit_receipt(request.compiler, invocation, result, receipt, reader, authority)
        if request.kind == JobKind.ASSEMBLE:
            parent = graph.node(invocation.request.job.owner).selected
            return AdmittedCheck(parent.candidate, CheckKind.ASSEMBLY, receipt, admitted.evidence,
                                 tuple(proof_ref(row) for row in value['export']['roots']))
        return AdmittedStage(request, receipt, admitted.evidence)

    def materialize(self, stage, selection, graph, reader, destination, *, protected=(), check=lambda: None):
        if not current(stage, selection, graph):
            raise ValueError('Cannot publish a stale export')
        return materialize(stage, reader, destination,
            protected=(*protected, *self.checker.environment.roots.values()), check=check)
