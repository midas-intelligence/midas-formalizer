"""Opt-in real compiler export checks; root AGENTS.md requires permission."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import sys
import time

from midas.checker import Checker, inspect, worker_payload
from midas.checker.requests import signature
from midas.domain import (AdmittedReview, ArtifactRef, Candidate, CapacityGrant, Dependency, DependencyOrigin,
    ObligationIdentity, OwnerVersion, ProofGraph, ProofNode, ReceiptBinding, ResourceLimits,
    ResultStatus, SelectionRequest, accept_assembly)
from midas.domain import encoding as codec
from midas.environment import (Dependency as LibraryDependency, Environment, ModulePin, Modules,
    PackageConfig, replay_dependencies, scan)
from midas.environment.package import output
from midas.executor import Executor
from midas.export import Export, prepare_library, read_bundle
from tests.test_checker import Artifacts, issue as issue_check, name
from tests.test_export import issue as issue_export
from tests.test_executor import invocation, success


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    values, state = Artifacts(), {}
    authority = SimpleNamespace(verify_receipt=lambda *args: True)
    with TemporaryDirectory(prefix='midas-export-lean-') as temporary:
        root = Path(temporary)
        project = root/'project'
        project.mkdir()
        (project/'Main.lean').write_text('import Lean\ntheorem target : True := by sorry\n')
        (project/'Joint.lean').write_text('def witness : Nat := sorry\ntheorem property : witness = witness := by sorry\n')
        (project/'Library.lean').write_text('import Dep\ntheorem target : Dep.value = 7 := by sorry\n')
        (project/'Context.lean').write_text('structure Box where\n  value : Nat\ndef twice : Nat → Nat\n  | 0 => 0\n  | n+1 => twice n + 2\ntheorem target : twice 2 = 4 := by sorry\n')
        with Executor(root, workers=1, compiler_slots=1, compiler_memory_mib=4096) as executor:
            def execute(inv, callback, *, succeeds=True):
                workspace = root/str(uuid4())
                workspace.mkdir()
                executor.start(inv, callback, grant=CapacityGrant(inv), workspace=workspace, reader=values, staging=values)
                while not (completed := executor.poll()):
                    time.sleep(.02)
                result = completed[0].result
                if not succeeds:
                    assert result.status == ResultStatus.FAILED, result
                elif result.status != ResultStatus.SUCCEEDED:
                    print(completed[0], flush=True)
                    for ref in result.staged_artifacts:
                        data = values.data[ref.id]
                        if data and b'\0' not in data and len(data) < 10000:
                            print(data.decode(errors='replace'), flush=True)
                    raise AssertionError('Compiler stage failed')
                receipt = ReceiptBinding(inv.original_execution_id, inv.request.request_ref,
                    values.save(worker_payload(result)), values.save(b'test-seal'))
                return result, receipt

            def setup(request, context):
                base = Environment.prepare(root/'base', PackageConfig('4.35.0-rc3', toolchain,
                    (('project', project),), ('project/Main.lean',), ('project', 'toolchain/lib/lean')), context)
                dep = root/'dependency'
                (dep/'src').mkdir(parents=True)
                (dep/'lib').mkdir()
                (dep/'src/Dep.lean').write_text('module\npublic def Dep.value : Nat := 7\n'
                    'public def Dep.text : String := include_str "assets" / "text.txt"\n')
                (dep/'src/assets').mkdir()
                (dep/'src/assets/text.txt').write_text('export asset\n')
                (dep/'lakefile.toml').write_text('name = "library"\n')
                (dep/'module.setup.json').write_bytes(codec.canonical(dict(name='Dep', package='library',
                    isModule=True, options=dict(maxSynthPendingDepth=3), importArts={}, dynlibs=[], plugins=[])))
                output(context.run_process(base.launch(('--setup', '/build/module.setup.json',
                    '--root=/build/src', '-o', '/build/lib/Dep.olean',
                    '-i', '/build/lib/Dep.ilean', '/build/src/Dep.lean'), build=dep,
                    max_heartbeat=context.limits.max_heartbeat, max_rec_depth=context.limits.max_rec_depth)))
                env = Environment.prepare(root/'package', PackageConfig('4.35.0-rc3', toolchain,
                    (('project', project), ('library', dep)),
                    tuple('project/' + p for p in ('Main.lean', 'Joint.lean', 'Library.lean', 'Context.lean')),
                    ('project', 'library/lib', 'toolchain/lib/lean')), context)
                checker = Checker(env, '/tmp/midas-new-checker-runtime')
                selections = {p: inspect(checker, SelectionRequest(p, all_unfinished=True), context)
                              for p in ('Main.lean', 'Joint.lean', 'Library.lean', 'Context.lean')}
                compiled, _ = scan(env.roots['library']/'lib')
                sources, _ = scan(env.roots['library']/'src')
                source = replace(sources.files[0], path='src/Dep.lean')
                asset = replace(sources.by_path['assets/text.txt'], path='src/assets/text.txt')
                module = ModulePin('Dep', 'library', source, compiled.files, 'lib', 'library',
                    (('assets/text.txt', asset),), (('maxSynthPendingDepth', 3),))
                modules = Modules('0'*64, ('Dep',), (module,), (('library', 'lib', compiled),), (('library', sources),))
                dependencies = (LibraryDependency('library', env.roots['library'], ('lib',), ('src',)),)
                replay = replay_dependencies(env, modules, dependencies, context)
                library = prepare_library(env, modules, replay, dependencies, values)
                state.update(base=base, checker=checker, selections=selections, library=library)
                return success(request, context)
            execute(invocation(limits=ResourceLimits(600000, 4096, 8*1024**2, 64*1024**2)), setup)
            checker, selections = state['checker'], state['selections']
            export = Export(checker)
            print('environment, source inspection and library replay: passed', flush=True)

            def prove(selected, index, code, *, identity=None, statement=None, dependencies=(), identities=(),
                      definitions=(), submission=None):
                identity = identity or selected.identities[index]
                candidate = Candidate(str(uuid4()), OwnerVersion(identity.id, 0), selected.environment_id,
                    values.save(code.encode()), dependencies, joint_submission_id=submission)
                request = checker.prepare('proof', selected, index, candidate, values, identity=identity,
                    statement=statement, dependencies=identities, closed_definitions=definitions)
                inv = issue_check(request, values)
                result, receipt = execute(inv, checker.execute)
                admitted = checker.admit_receipt(request, inv, result, receipt, values, authority, candidate=candidate)
                return admitted, request

            def node(selected, index, admitted, *, identity=None, alias=None, review=None):
                identity = identity or selected.identities[index]
                constant = name(alias) if alias else codec.load_json(selected.selected.targets[index].constant)
                interface = values.save(codec.encode({'constant': constant, 'signature': signature(identity)}))
                return ProofNode(admitted.candidate.owner, selected.environment_id, interface, selected=admitted, review=review)

            def finish(label, selected, graph, *, library=None, allow_sorry=False):
                integrated = export.prepare_integration(selected, graph, values, values,
                    profile='library' if library else 'source', library=library, allow_sorry=allow_sorry)
                inv = issue_export(integrated, values)
                result, receipt = execute(inv, export.execute)
                admitted = export.admit_receipt(integrated, inv, result, receipt, values, authority,
                    selection=selected, graph=graph)
                verification = export.prepare_verification(admitted, selected, graph, values)
                inv = issue_export(verification, values)
                result, receipt = execute(inv, export.execute)
                verified = export.admit_receipt(verification, inv, result, receipt, values, authority,
                    selection=selected, graph=graph, integration=admitted)
                destination = root/label
                export.materialize(verified, selected, graph, values, destination)
                saved, reader = read_bundle(destination)
                # A different package with the same toolchain has no original
                # compiled library mounted, nor any run/certificate artifacts.
                standalone = Checker(state['base'], '/tmp/midas-new-checker-runtime')
                recheck = Export(standalone).prepare_recheck(saved, reader)
                for ref in recheck.decode()['inputs']:
                    ref = ArtifactRef.from_dict(ref)
                    values.data[ref.id] = reader.read_artifact(ref, max_bytes=64*1024**2)
                inv = issue_check(recheck, values)
                result, receipt = execute(inv, standalone.execute)
                standalone.admit_receipt(recheck, inv, result, receipt, values, authority)
                print(label + ': integration, clean build, kernel audit, publication and standalone recheck passed', flush=True)
                return integrated.decode()['packet']['source']

            for path, code in (('Main.lean', 'by trivial'), ('Context.lean', 'by rfl'), ('Library.lean', 'by rfl')):
                selected = selections[path]
                admitted, _ = prove(selected, 0, code)
                graph = ProofGraph(0, (admitted.candidate.owner.obligation_id,), (node(selected, 0, admitted),))
                finish(path[:-5], selected, graph, library=state['library'] if path == 'Library.lean' else None)
                unfinished = replace(graph, nodes=(replace(graph.nodes[0], selected=None),))
                finish('partial-' + path[:-5], selected, unfinished, allow_sorry=True,
                       library=state['library'] if path == 'Library.lean' else None)

            selected = selections['Main.lean']
            identity = ObligationIdentity(codec.encode(['const', name('Nat'), []]), codec.encode([]),
                codec.encode(['closed-telescope']), selected.environment_id, selected.identities[0].prerequisite_interfaces)
            child, _ = prove(selected, 0, '7', identity=identity, statement='Nat')
            child_node = node(selected, 0, child, identity=identity, alias='h')
            parent, request = prove(selected, 0, 'by have _ := h; trivial',
                dependencies=(Dependency(child.candidate.owner, 'h', child_node.interface),), identities=(identity,))
            parent_node = node(selected, 0, parent, review=AdmittedReview(parent.candidate, parent.receipt, True))
            graph = ProofGraph(0, (parent.candidate.owner.obligation_id,), (parent_node, child_node))
            unfinished = replace(graph, nodes=(parent_node, replace(child_node, selected=None)))
            source = finish('partial-child', selected, unfinished, allow_sorry=True)
            assert 'by have _ := h; trivial' in source and 'sorry' in source

            leaf_identity = ObligationIdentity(codec.encode(['forall', name('n'), 'explicit',
                ['const', name('Nat'), []], ['const', name('Nat'), []]]), codec.encode([]),
                codec.encode(['closed-telescope']), selected.environment_id, selected.identities[0].prerequisite_interfaces)
            leaf, _ = prove(selected, 0, 'fun n => n', identity=leaf_identity, statement='Nat → Nat')
            leaf_node = node(selected, 0, leaf, identity=leaf_identity, alias='g')
            middle, _ = prove(selected, 0, 'g 7', identity=identity, statement='Nat',
                dependencies=(Dependency(leaf_node.owner, 'g', leaf_node.interface),), identities=(leaf_identity,))
            middle_node = node(selected, 0, middle, identity=identity, alias='h',
                               review=AdmittedReview(middle.candidate, middle.receipt, True))
            # Keep the parent's exact admitted interface reference for this owner.
            middle_node = replace(middle_node, interface=child_node.interface)
            nested = replace(graph, nodes=(parent_node, middle_node, replace(leaf_node, selected=None)))
            source = finish('partial-nested', selected, nested, allow_sorry=True)
            assert 'g 7' in source and 'by have _ := h; trivial' in source and 'sorry' in source

            for text, allow_sorry in ((selected.source.decode(), False),
                    ('axiom bad : False\ntheorem target : True := by trivial\n', True),
                    ('theorem target : True := by exact (0 : Nat)\n', True)):
                audit = checker.prepare('audit', selected, parent.candidate.owner, values.save(text.encode()),
                                        values, allow_sorry=allow_sorry)
                execute(issue_check(audit, values), checker.execute, succeeds=False)
            print('strict sorry rejection, unrelated axiom rejection and partial elaboration-error rejection: passed', flush=True)

            assembly = export.prepare_assembly(selected, graph, parent.candidate.owner, request, values)
            inv = issue_export(assembly, values)
            result, receipt = execute(inv, export.execute)
            assembled = export.admit_receipt(assembly, inv, result, receipt, values, authority,
                selection=selected, graph=graph, parent_request=request)
            graph = accept_assembly(graph, assembled)
            finish('assembled', selected, graph)

            selected = selections['Joint.lean']
            submission = str(uuid4())
            witness, _ = prove(selected, 0, '0', submission=submission)
            first = node(selected, 0, witness)
            dependency = Dependency(first.owner, 'w', first.interface, DependencyOrigin.CLOSED_DEFINITION,
                                    witness.evidence, submission)
            prop, _ = prove(selected, 1, 'by rfl', dependencies=(dependency,), identities=(selected.identities[0],),
                            definitions=(witness,), submission=submission)
            second = node(selected, 1, prop)
            graph = ProofGraph(0, (first.owner.obligation_id, second.owner.obligation_id), (first, second))
            finish('joint', selected, graph)

            dependency = Dependency(first.owner, 'w', first.interface, DependencyOrigin.FROZEN_PEER)
            parent, request = prove(selected, 1, 'by rfl', dependencies=(dependency,), identities=(selected.identities[0],))
            second = node(selected, 1, parent, review=AdmittedReview(parent.candidate, parent.receipt, True))
            graph = ProofGraph(0, (first.owner.obligation_id, second.owner.obligation_id), (first, second))
            unfinished = replace(graph, nodes=(replace(first, selected=None), second))
            source = finish('partial-frozen-peer', selected, unfinished, allow_sorry=True)
            assert 'sorry' in source and 'fun' in source
            assembly = export.prepare_assembly(selected, graph, parent.candidate.owner, request, values)
            inv = issue_export(assembly, values)
            result, receipt = execute(inv, export.execute)
            assembled = export.admit_receipt(assembly, inv, result, receipt, values, authority,
                selection=selected, graph=graph, parent_request=request)
            finish('frozen-peer', selected, accept_assembly(graph, assembled))

            # The replayed definition may itself be an assembled proof with a
            # shared declaration graph, rather than a single candidate body.
            identity = ObligationIdentity(codec.encode(['forall', name('n'), 'explicit',
                ['const', name('Nat'), []], ['const', name('Nat'), []]]), codec.encode([]),
                codec.encode(['closed-telescope']), selected.environment_id, selected.identities[0].prerequisite_interfaces)
            child, _ = prove(selected, 0, 'fun n => n', identity=identity, statement='Nat → Nat')
            child_node = node(selected, 0, child, identity=identity, alias='h')
            parent, request = prove(selected, 0, 'h 0',
                dependencies=(Dependency(child_node.owner, 'h', child_node.interface),), identities=(identity,))
            first = node(selected, 0, parent, review=AdmittedReview(parent.candidate, parent.receipt, True))
            second = replace(second, selected=None, review=None, assembly=None)
            graph = ProofGraph(0, (first.owner.obligation_id, second.owner.obligation_id), (first, second, child_node))
            assembly = export.prepare_assembly(selected, graph, first.owner, request, values)
            inv = issue_export(assembly, values)
            result, receipt = execute(inv, export.execute)
            assembled = export.admit_receipt(assembly, inv, result, receipt, values, authority,
                selection=selected, graph=graph, parent_request=request)
            graph = accept_assembly(graph, assembled)
            first = graph.node(first.owner)
            dependency = Dependency(first.owner, 'w', first.interface, DependencyOrigin.FROZEN_PEER)
            parent, request = prove(selected, 1, 'by rfl', dependencies=(dependency,), identities=(selected.identities[0],))
            second = node(selected, 1, parent, review=AdmittedReview(parent.candidate, parent.receipt, True))
            graph = replace(graph, nodes=(first, second, child_node))
            assembly = export.prepare_assembly(selected, graph, second.owner, request, values)
            inv = issue_export(assembly, values)
            result, receipt = execute(inv, export.execute)
            assembled = export.admit_receipt(assembly, inv, result, receipt, values, authority,
                selection=selected, graph=graph, parent_request=request)
            finish('composed-definition-peer', selected, accept_assembly(graph, assembled))
    print('export Lean checks passed', flush=True)


if __name__ == '__main__':
    main()
