"""Opt-in real compiler export checks; root AGENTS.md requires permission."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import subprocess
import re
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


def check_type_source(compiler):
    """Catch lossy display syntax before building the complete checker runtime."""
    identity = Path(__file__).resolve().parents[1]/'midas/checker/lean/Identity.lean'
    source = identity.read_text() + '''
open Lean Elab Command
universe u v
theorem instanceType {G : Type u} [Mul G] [One G] (g : G) : g * 1 = g * 1 := rfl
theorem nestedType : (fun {G : Type} [Inhabited G] => (default : G)) =
    (fun {G : Type} [Inhabited G] => (default : G)) := rfl
theorem proofType : (⟨0, rfl⟩ : {n : Nat // n = 0}) = ⟨0, rfl⟩ := rfl
theorem integerType : (0 : Int) = 0 := rfl
theorem universeType {α : Type u} {β : Type v} (f : α → β) : f = f := rfl
theorem implicitIdType : (fun {α : Type u} (x : α) => x) = (fun {α : Type u} (x : α) => x) := rfl
-- Untyped ↑z lets equality infer Fin L (and its OfNat instance) instead of Nat.
theorem finZeroType {L : Nat} (z : Fin L) (h : z.val = 0) : z.val = 0 := h
theorem finSuccessorType {L : Nat} (z : Fin (L + 1)) (h : z.val = 0) : z.val = 0 := h
theorem finReversedType {L : Nat} (z : Fin L) (h : 0 = z.val) : 0 = z.val := h
theorem natIntType (n : Nat) (h : (n : Int) = 0) : (n : Int) = 0 := h
theorem subtypeZeroType (z : {n : Nat // n < 10}) (h : z.val = 0) : z.val = 0 := h
run_cmd do
  for original in [`instanceType, `nestedType, `proofType, `integerType, `universeType, `implicitIdType,
      `finZeroType, `finSuccessorType, `finReversedType, `natIntType, `subtypeZeroType] do
    let ci ← getConstInfo original
    let source ← liftTermElabM <| Midas.Identity.present ci.type (source := true)
    let .ok parsed := Parser.runParserCategory (← getEnv) `term source
      | throwError "invalid rendered type: {source}"
    let typeSyntax : Term := ⟨parsed⟩
    let result := original.appendAfter "Rendered"
    elabCommand (← `(command| theorem $(mkIdent result):ident : $typeSyntax := @$(mkIdent original)))
    let actual ← getConstInfo result
    liftTermElabM do
      unless ← Meta.isDefEq ci.type actual.type do
        throwError "rendered type changed: {source}"
    logInfo m!"{original}: {source}"
'''
    with TemporaryDirectory(prefix='midas-export-types-') as temporary:
        path = Path(temporary)/'Types.lean'
        path.write_text(source)
        result = subprocess.run(['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
            '--dev', '/dev', '--proc', '/proc', '--bind', temporary, temporary,
            str(compiler), str(path)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            print(result.stdout + result.stderr, flush=True)
            raise AssertionError('Rendered types failed to compile')
        # Keep readable coercion notation while fixing its destination type.
        assert '(↑z : Nat)' in result.stdout, result.stdout
        print('readable instance, nested-function, proof-bearing and coercion types: round-tripped', flush=True)


def check_flat_contexts(compiler):
    """Check name resolution after projection, including reused sibling scopes."""
    from midas.domain.identity import make_context
    from midas.export.names import ExportNames
    from midas.source import CheckedDeclaration, SourceFile, declarations
    from tests.test_export_names import declaration, dependencies, generated, result
    from tests.test_source import signature as source_signature

    nat = ['const', name('Nat'), []]
    definitions = [{'name': 'x', 'type': 'Nat', 'value': '1'},
                   {'name': 'y', 'type': 'Nat', 'value': 'x + 1'},
                   {'name': 'T', 'type': 'Type', 'value': 'Nat'}]
    prefix = make_context('flat', definitions[:1])
    extended = make_context('flat', definitions)
    sibling = make_context('flat', [{'name': 'x', 'type': 'Nat', 'value': '7'},
                                    {'name': 'y', 'type': 'Nat', 'value': 'x + 2'},
                                    {'name': 'T', 'type': 'Type', 'value': 'Bool'}])
    isolated = 'by\n  fail_if_success have : Nat := x\n  fail_if_success have : Nat := y\n  exact 9'
    rows, checks = [], []
    def add(label, context, proof, expected, *, target=nat, type_source=None):
        old = 'MidasCandidateResult.s' + sha256(label.encode()).hexdigest()
        row = declaration(old, proof)
        row['signature'] = dict(target=target, universes=[], context=context)
        if type_source:
            row['type_source'] = type_source
        rows.append(row)
        checks.append((old, expected))
        return old
    add('prefix', prefix, 'by exact x', '= 1')
    add('extended', extended, 'by exact y', '= 2')
    add('sibling', sibling, 'by exact y', '= 9')
    add('isolated', ['closed-telescope'], isolated, '= 9')
    for label, ctx, arg in (('natId', extended, '(13 : Nat)'), ('boolId', sibling, '(true : Bool)')):
        t = ['const', name(ctx[2][-1]['namespace'] + '.T'), []]
        add(label, ctx, 'by intro x; exact x', f'{arg} = {arg}',
            target=['forall', name('x'), 'explicit', t, t], type_source='T → T -- authored type')
    add('later', extended, 'by exact y', '= 2')
    later_row = rows.pop()
    child = add('dependency', ['closed-telescope'], '42', '= 42')
    scope = dependencies('a')
    alias = declaration(scope + '.x', '@_root_.' + child, (child,))
    alias['signature'] = rows[-1]['signature']
    rows.append(alias)
    add('usesAlias', ['closed-telescope'], 'open _root_.' + scope + ' in\n'
        'by\n  have f : Nat → Nat := fun x => x\n  exact f x', '= 42')
    rows[-1]['requires'] = [alias['name']]
    uses_alias = rows[-1]
    u = ['param', name('u')]
    poly = declaration(result('b'), 'fun α x => x')
    poly['signature'] = dict(target=['forall', name('α'), 'explicit', ['sort', u],
        ['forall', name('x'), 'explicit', ['bvar', 0], ['bvar', 1]]],
        universes=[name('u')], context=['closed-telescope'])
    poly_alias = declaration(scope + '.poly', '@_root_.' + result('b') + '.{u}', (result('b'),))
    poly_alias['signature'] = poly['signature']
    rows.extend((poly, poly_alias))
    uses_alias['requires'].append(poly_alias['name'])
    add('usesPoly', ['closed-telescope'], 'open _root_.' + scope + ' in\npoly Nat 12', '= 12')
    rows[-1]['requires'] = [alias['name'], poly_alias['name']]
    # This authored constant reserves the preferred name, and a nested Midas
    # namespace tests that generated opens resolve from the root.
    original = b'namespace Midas\ndef x := 99\nend Midas\nnamespace Authored\nnamespace Midas\nend Midas\n'
    names = ExportNames(original)
    names.add([*rows, later_row], rows[-1]['name'], 'answer')
    emitted = {}
    def render(batch, external=frozenset()):
        checked = tuple(CheckedDeclaration(codec.encode(row['name']),
            source_signature(row['signature']['target'], universes=row['signature']['universes'],
                             context=row['signature']['context']), SourceFile('proof', row['source'].encode()),
            tuple(codec.encode(n) for n in row['requires']),
            SourceFile('type', row['type_source'].encode()) if 'type_source' in row else None) for row in batch)
        source = declarations(checked, emitted=emitted, external=external)
        names.contexts(emitted.values())
        return names.rewrite(source, maximum=1024**2).data
    source = original + render(rows) + render([later_row])
    for old, expected in checks:
        source += names.rewrite(generated(f'example : _root_.{old} {expected} := rfl\n'), maximum=1024**2).data
    source += ('example : Nat := ' + isolated + '\nend Authored\n').encode()
    assert b'MidasContext' not in source and b'MidasCandidateResult' not in source
    assert source.count('T → T -- authored type'.encode()) == 2
    with TemporaryDirectory(prefix='midas-flat-contexts-') as temporary:
        path = Path(temporary)/'Flat.lean'
        path.write_bytes(source)
        checked = subprocess.run(['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
            '--dev', '/dev', '--proc', '/proc', '--bind', temporary, temporary,
            str(compiler), str(path)], capture_output=True, text=True, timeout=60)
        if checked.returncode:
            print(source.decode() + checked.stdout + checked.stderr, flush=True)
            raise AssertionError('Flat context resolution failed')
    print('flat sibling contexts, local shadowing, authored types and polymorphic aliases: compiled', flush=True)


def check_source_sharing(compiler):
    """Exercise kernel comparison before building the complete checker runtime."""
    directory = Path(__file__).resolve().parents[1]/'midas/checker/lean'
    check = (directory/'Check.lean').read_text()
    source = (directory/'Identity.lean').read_text() + '\nopen Lean Elab Command\nnamespace Midas.Check\n'
    source += check[check.index('partial def readName'):check.index('partial def readLevel')]
    source += check[check.index('def checked ('):check.index('def closedContext')] + '\n'
    source += check[check.index('def sourceAliases ('):check.index('def auditSource (')]
    source += '''
end Midas.Check
def Midas.x : Nat := 1
def Midas.x_2 : Nat := 1
def Midas.y : Nat := Midas.x + 1
def Midas.y_2 : Nat := Midas.x_2 + 1
def Midas.changed : Nat := 7
def Midas.different : Nat := Midas.changed + 1
abbrev Midas.reducible : Nat := 1
def Midas.alias : Nat := Midas.x_2
universe u
def Midas.poly (α : Sort u) (x : α) : α := x
def Midas.poly_2 : (α : Sort u) → α → α := @Midas.poly.{u}
def Midas.implicit {α : Sort u} (x : α) : α := x
namespace First
def ambient : Nat := 1
def _root_.Midas.first : Nat := ambient
end First
namespace Second
def ambient : Nat := 7
def _root_.Midas.second : Nat := ambient
end Second
run_cmd do
  let names := #[`Midas.x, `Midas.x_2, `Midas.y, `Midas.y_2, `Midas.changed,
    `Midas.different, `Midas.reducible, `Midas.alias, `Midas.poly, `Midas.poly_2,
    `Midas.implicit, `Midas.first, `Midas.second]
  let aliases ← Midas.Check.sourceAliases (names.map Midas.Identity.name)
  let expected := #[(`Midas.x_2, `Midas.x), (`Midas.y_2, `Midas.y),
    (`Midas.alias, `Midas.x), (`Midas.poly_2, `Midas.poly)]
  unless aliases == expected.map (fun (a, b) => toJson #[Midas.Identity.name a, Midas.Identity.name b]) do
    throwError "unexpected kernel aliases: {aliases}"
'''
    with TemporaryDirectory(prefix='midas-sharing-lean-') as temporary:
        path = Path(temporary)/'Sharing.lean'
        path.write_text(source)
        result = subprocess.run(['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
            '--dev', '/dev', '--proc', '/proc', '--bind', temporary, temporary,
            str(compiler), str(path)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            print(result.stdout + result.stderr, flush=True)
            raise AssertionError('Kernel source sharing failed')
    print('kernel sharing: identical definitions and forwards merged; distinct scopes and binder kinds preserved', flush=True)


def check_empty_sections(compiler):
    from tests.test_export_names import section_sources

    original, reduced, _ = section_sources()
    checks = (b'example : Midas.proof = 2 := rfl\n'
              b'example : Nat := by\n  fail_if_success exact y\n  exact 0\n')
    with TemporaryDirectory(prefix='midas-empty-sections-') as temporary:
        for label, source in (('Original', original), ('Reduced', reduced)):
            path = Path(temporary)/(label + '.lean')
            path.write_bytes(source.data + checks)
            result = subprocess.run(['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
                '--dev', '/dev', '--proc', '/proc', '--bind', temporary, temporary,
                str(compiler), str(path)], capture_output=True, text=True, timeout=60)
            if result.returncode:
                print(source.data.decode() + result.stdout + result.stderr, flush=True)
                raise AssertionError('Section cleanup changed Lean name resolution')
    print('empty sections removed; surviving definitions, proofs and local opens: compiled', flush=True)


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    check_flat_contexts(toolchain/'bin/lean')
    check_source_sharing(toolchain/'bin/lean')
    check_empty_sections(toolchain/'bin/lean')
    check_type_source(toolchain/'bin/lean')
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
        (project/'Instances.lean').write_text('''universe u
theorem target {G : Type u} [Mul G] [One G] (g : G) : g * 1 = g * 1 := by sorry
theorem named {G : Type u} [mul : Mul G] [one : One G] (g : G) : g * 1 = g * 1 := by sorry
theorem nested : (fun {G : Type} [Inhabited G] => (default : G)) =
    (fun {G : Type} [Inhabited G] => (default : G)) := by sorry
theorem proof_terms : (⟨0, rfl⟩ : {n : Nat // n = 0}) = ⟨0, rfl⟩ := by sorry
''')
        (project/'Namespaces.lean').write_text('''def count : Nat := 10
namespace Midas
def count : Nat := 99
def opened : Nat := 99
def localCount : Nat := 99
end Midas
namespace Imported
def opened : Nat := 7
end Imported
open Imported
namespace Authored
def localCount : Nat := 11
private def secret : Nat := 5
theorem target : True := by sorry
end Authored
''')
        paths = ('Main.lean', 'Joint.lean', 'Library.lean', 'Context.lean', 'Instances.lean', 'Namespaces.lean')
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
                    tuple('project/' + p for p in paths),
                    ('project', 'library/lib', 'toolchain/lib/lean')), context)
                checker = Checker(env, '/tmp/midas-new-checker-runtime')
                selections = {p: inspect(checker, SelectionRequest(p, all_unfinished=True), context)
                              for p in paths}
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
                verification = export.prepare_verification(admitted, selected, graph, values, values)
                inv = issue_export(verification, values)
                try:
                    result, receipt = execute(inv, export.execute)
                except AssertionError:
                    print(verification.decode()['packet']['source'], flush=True)
                    raise
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
                return verification.decode()['packet']['source']

            selected = selections['Namespaces.lean']
            identity = ObligationIdentity(codec.encode(['forall', name('n'), 'explicit',
                ['const', name('Nat'), []], ['const', name('Nat'), []]]), codec.encode([]),
                codec.encode(['closed-telescope']), selected.environment_id, selected.identities[0].prerequisite_interfaces)
            child, _ = prove(selected, 0, 'fun n => count + opened + localCount + secret + n', identity=identity,
                             statement='Nat → Nat')
            child_node = node(selected, 0, child, identity=identity, alias='helper')
            parent, request = prove(selected, 0,
                'by have _ := helper 0; let count := (1 : Nat); have : count = 1 := rfl; trivial',
                dependencies=(Dependency(child_node.owner, 'helper', child_node.interface),), identities=(identity,))
            parent_node = node(selected, 0, parent, review=AdmittedReview(parent.candidate, parent.receipt, True))
            graph = ProofGraph(0, (parent_node.owner.obligation_id,), (parent_node, child_node))
            assembly = export.prepare_assembly(selected, graph, parent_node.owner, request, values)
            inv = issue_export(assembly, values)
            result, receipt = execute(inv, export.execute)
            assembled = export.admit_receipt(assembly, inv, result, receipt, values, authority,
                selection=selected, graph=graph, parent_request=request)
            source = finish('namespace-collisions', selected, accept_assembly(graph, assembled))
            assert 'with_weak_namespace _root_.Midas' in source and 'open _root_.Midas' not in source, source
            assert '_root_.count' in source and '_root_.Authored.localCount' in source, source
            assert '_root_.Imported.opened' in source and '_root_.Authored.secret' in source, source
            assert 'let count := (1 : Nat); have : count = 1 := rfl' in source, source
            assert 'fun n =>' in source and 'Nat → Nat' in source, source

            def check_projection(body, checks, *, prefix=b'import Lean\n', deduplicate=True):
                from midas.export.projection import project as project_source
                from midas.source import parse_identifier
                selected = selections['Main.lean']
                owner = OwnerVersion(selected.identities[0].id, 0)
                source = prefix + body + checks + b'theorem target : True := by trivial\n'
                request = checker.prepare('audit', selected, owner, values.save(source), values)
                value = request.decode()
                value['packet'].update(
                    share=[parse_identifier(m[1].decode()) for m in re.finditer(rb'def _root_\.(Midas\.[\w]+)', body)],
                    regions=[[len(prefix), len(prefix + body)]],
                    calls=[[len(prefix + body), len(prefix + body + checks)]])
                request = replace(request, payload=codec.encode(value))
                inv = issue_check(request, values)
                result, receipt = execute(inv, checker.execute)
                admitted = checker.admit_receipt(request, inv, result, receipt, values, authority)
                evidence = codec.decode(values.data[admitted.evidence.id])
                reduced = project_source(source, value['packet']['regions'], evidence['layout'],
                                         evidence['aliases'] if deduplicate else [],
                                         calls=value['packet']['calls']).data
                request = checker.prepare('audit', selected, owner, values.save(reduced), values)
                inv = issue_check(request, values)
                try:
                    result, receipt = execute(inv, checker.execute)
                except AssertionError:
                    print(reduced.decode(), flush=True)
                    raise
                checker.admit_receipt(request, inv, result, receipt, values, authority)
                return reduced

            from tests.test_export_names import section_sources
            reduced = check_projection(section_sources()[0].data,
                b'example : _root_.Midas.answer = 2 := rfl\n')
            assert len(re.findall(rb'(?m)^(?:noncomputable )?def ', reduced)) == 3, reduced
            assert b'open _root_.Midas' not in reduced, reduced
            assert not re.search(rb'(?m)^section$', reduced), reduced
            assert b'/- section\nend -/' in reduced and b'"section\\nend"' in reduced

            reduced = check_projection('''def _root_.Midas.x : Nat := 1
section
open _root_.Midas (x)
def _root_.Midas.y : Nat := x + 1
end
def _root_.Midas.x_2 : Nat := 7
section
open _root_.Midas renaming x_2 → x
def _root_.Midas.y_2 : Nat := x + 2
end
def _root_.Midas.answer : Nat := open _root_.Midas renaming y_2 → y in y + 3
def _root_.Midas.tactic : Nat := by open _root_.Midas renaming y_2 → y in exact y + 4
def _root_.Midas.ambient : Nat := _root_.ambient + 1
def _root_.Midas.external : Nat := ambient + 2
def _root_.Midas.cast : Nat → Int := fun n => (n : Int)
def _root_.Midas.zero : Nat := 99
def _root_.Midas.succ : Nat := 99
def _root_.Midas.branches : Nat → Nat := by
  intro n
  induction n with
  | zero => exact 0
  | succ n ih =>
    cases n with
    | zero => exact 1
    | succ k => exact ih + 1
'''.encode(), b'''example : _root_.Midas.y = 2 := rfl
example : _root_.Midas.y_2 = 9 := rfl
example : _root_.Midas.answer = 12 := rfl
example : _root_.Midas.tactic = 13 := rfl
example : _root_.Midas.ambient = 11 := rfl
example : _root_.Midas.external = 12 := rfl
example : _root_.Midas.cast 2 = 2 := rfl
example : _root_.Midas.branches 2 = 2 := rfl
''', prefix=b'import Lean\ndef ambient : Nat := 10\n')
            assert b'open _root_.Midas (' not in reduced, reduced
            assert 'renaming x_2 → x'.encode() in reduced, reduced
            assert b'_root_.ambient + 1' in reduced and b'_root_.ambient + 2' in reduced, reduced
            assert b'| zero =>' in reduced and b'| succ n ih =>' in reduced, reduced
            assert b'| _root_.Nat.' not in reduced, reduced
            print('namespace projection: deduplication, distinct siblings, inline opens and generated root shadowing passed', flush=True)

            reduced = check_projection(b'''def _root_.Midas.conditional : (if True then 1 else 0) = 1 := by
  exact if_pos True.intro
def _root_.Midas.sequence : True := by
  apply id <;> (skip <;> exact True.intro)
def _root_.Midas.branches : And True True := by
  constructor <;> trivial
def _root_.Midas.singleSyntax : True := by
  cases () with apply id <;> apply id
  | unit => trivial
def _root_.Midas.shadow : True := by
  have if_pos : True := True.intro
  exact if_pos
def _root_.Midas.implicit : True := oldImplicit True.intro
def _root_.Midas.changed : True := oldType
def _root_.Midas.named : True := oldName (p := True) True.intro
def _root_.Midas.data : Nat := oldData
def _root_.Midas.literal : String := "if_pos <;>"
-- if_pos <;> authored comment
''', b'''example : _root_.Midas.data = 4 := rfl
example : _root_.Midas.literal = "if_pos <;>" := rfl
''', prefix=b'''import Lean
theorem newImplicit (p : Prop) (h : p) : p := h
@[deprecated newImplicit (since := "2026-01-01")]
theorem oldImplicit {p : Prop} (h : p) : p := h
theorem newType : And True True := And.intro True.intro True.intro
@[deprecated newType (since := "2026-01-01")]
theorem oldType : True := True.intro
theorem newName (q : Prop) (h : q) : q := h
@[deprecated newName (since := "2026-01-01")]
theorem oldName (p : Prop) (h : p) : p := h
def newData : Nat := 9
@[deprecated newData (since := "2026-01-01")]
def oldData : Nat := 4
''', deduplicate=False)
            assert b'exact _root_.ite_eq_left True.intro' in reduced, reduced
            assert b'(apply id ; ((skip ; exact True.intro)))' in reduced, reduced
            assert b'constructor <;> trivial' in reduced, reduced
            assert b'with (apply id ; apply id)' in reduced, reduced
            assert b'exact if_pos' in reduced, reduced
            assert b':= oldImplicit True.intro' in reduced, reduced
            assert b':= oldType' in reduced and b':= oldName (p := True) True.intro' in reduced, reduced
            assert b':= oldData' in reduced and b'"if_pos <;>"' in reduced, reduced
            print('export cleanup: aliases, shadowing, binder changes, nested and multigoal sequences passed', flush=True)

            if '--namespace-only' in sys.argv:
                return

            # The same unnamed instance has a different generated binder name
            # when the export is compiled under a new module. Keep the frozen
            # signatures from Instances, including nested lambda binders.
            selected = selections['Instances.lean']
            owner = OwnerVersion(selected.identities[0].id, 0)
            for allow_sorry in (False, True):
                source = selected.source if allow_sorry else selected.source.replace(b'by sorry', b'by rfl')
                audit = checker.prepare('audit', selected, owner, values.save(source), values,
                                        allow_sorry=allow_sorry)
                value = audit.decode()
                value['packet']['logical'] = 'Partial/PInstances.lean'
                audit = replace(audit, payload=codec.encode(value))
                inv = issue_check(audit, values)
                result, receipt = execute(inv, checker.execute)
                checker.admit_receipt(audit, inv, result, receipt, values, authority)
            print('cross-module strict and partial instance audits: passed', flush=True)

            # Only generated binder scopes may differ. Each mutation changes a
            # separate signature field while keeping the source well-typed.
            for change in ('binder kind', 'instance type', 'universe parameters', 'universe level', 'binder name'):
                value = audit.decode()
                sig = next(t['signature'] for t in value['packet']['targets'] if t['constant'] == name('target'))
                target = sig['target']
                if change == 'binder kind':
                    target[4][2] = 'implicit'
                elif change == 'instance type':
                    target[4][3][1][1] = name('Add')
                elif change == 'universe parameters':
                    sig['universes'].append(name('extra'))
                elif change == 'universe level':
                    target[3][1] = ['succ', target[3][1]]
                else:
                    target[1] = name('H')
                altered = replace(audit, payload=codec.encode(value))
                result, _ = execute(issue_check(altered, values), checker.execute, succeeds=False)
                assert result.diagnostics is not None, (change, result)
                assert b'export target signature mismatch: target' in values.data[result.diagnostics.id], change
            print('binder kind, type, universe and authored-name mismatches: rejected', flush=True)

            for path, code in (('Main.lean', 'by trivial'), ('Context.lean', 'by rfl'),
                               ('Library.lean', 'by rfl'), ('Instances.lean', 'by intros; rfl')):
                selected = selections[path]
                nodes = tuple(node(selected, i, prove(selected, i, code)[0]) for i in range(len(selected.identities)))
                graph = ProofGraph(0, tuple(n.owner.obligation_id for n in nodes), nodes)
                source = finish(path[:-5], selected, graph, library=state['library'] if path == 'Library.lean' else None)
                assert '_midas' not in source and '«' not in source and '⋯' not in source, source[:2000]
                assert 'Midas.target' in source
                unfinished = replace(graph, nodes=(replace(graph.nodes[0], selected=None), *graph.nodes[1:]))
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
            assert 'def g :' in source and 'def h :' in source
            assert '_proof' not in source and 'renaming' not in source, source
            assert 'namespace Midas' in source and 'open _root_.Midas' not in source

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
            source = finish('assembled', selected, graph)
            assert '_proof' not in source and 'renaming' not in source, source

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
            authored_type = 'Nat → Nat -- original function type'
            child, _ = prove(selected, 0, 'fun n => n', identity=identity, statement=authored_type)
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
            source = finish('composed-definition-peer', selected, accept_assembly(graph, assembled))
            assert authored_type in source and '_midas' not in source
    print('export Lean checks passed', flush=True)


if __name__ == '__main__':
    main()
