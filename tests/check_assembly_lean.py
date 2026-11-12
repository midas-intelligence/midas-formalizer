"""Real Lean regression for Erdős 548 assembly and namespace projection.

Usage: python -B -m tests.check_assembly_lean /path/to/bin/lean [checker-cache]
Append --failure-only after checker-cache to run only the scheduler rejection case.
Needs namespace execution permission as described in AGENTS.md.
"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys

from midas.application import Application
from midas.domain import JobKind, ProofState, ResultStatus, SelectionRequest
from midas.environment import PackageConfig
from midas.export import proofs
from midas.policy import Config, ModelRoute
from tests.check_application_lean import complete
from tests.test_strategy import proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('/tmp/midas-new-checker-runtime')
    with TemporaryDirectory(prefix='midas-assembly-lean-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        sources = {
            'Plain': 'import Lean\ntheorem target : True := by sorry\n',
            'Collision': ('import Lean\nnamespace Midas\ndef leaf : Nat := 17\nend Midas\n'
                          'theorem target : True := by sorry\n'),
            'Nested': ('import Lean\nnamespace Authored\n'
                       'theorem target : True := by sorry\nend Authored\n'),
        }
        for name, source in sources.items():
            (project / (name + '.lean')).write_text(source)
        app = Application(cache)
        package = app.package(root / 'package', PackageConfig('4.35.0-rc3', toolchain,
            (('project', project),), tuple('project/' + name + '.lean' for name in sources),
            ('project', 'toolchain/lib/lean')))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, reviewer=route)
        action = dict(proof(), mode='decomposition', proof_body='''by
  have h1 := implicitProof (V := Nat) 0
  have h2 := strictProof (V := Nat) 0
  have h3 := instanceProof (V := Nat) 0
  have h4 := lambdaProof (V := Nat) 0
  trivial''', children=[
            dict(name='leaf', statement='1 = 1', proof_body='by rfl', proof='',
                 usefulness='Shared verified premise.', depends_on=[]),
            dict(name='implicitProof', statement='∀ {V : Type} (x : V), 1 = 1',
                 proof_body='by\n  intro V x\n  exact leaf', proof='',
                 usefulness='Leading implicit binder.', depends_on=['leaf']),
            dict(name='strictProof', statement='∀ ⦃V : Type⦄ (x : V), 1 = 1',
                 proof_body='by\n  intro V x\n  exact leaf', proof='',
                 usefulness='Leading strict implicit binder.', depends_on=['leaf']),
            dict(name='instanceProof', statement='∀ {V : Type} [Inhabited V] (x : V), 1 = 1',
                 proof_body='by\n  intro V inst x\n  exact leaf', proof='',
                 usefulness='Implicit and instance binders.', depends_on=['leaf']),
            dict(name='lambdaProof', statement='∀ {V : Type} (x : V), x = x',
                 proof_body='fun x => (fun (_ : 1 = 1) => Eq.refl x) leaf', proof='',
                 usefulness='Preserve implicit introduction for lambda terms.', depends_on=['leaf']),
        ])
        for name in (() if '--failure-only' in sys.argv else sources):
            selected = app.select(package, SelectionRequest(name + '.lean', all_unfinished=True))
            with app.start(root / name, package=package, selection=selected, config=config,
                           workflow='scripted', script=(('prover', response(action)),)) as run:
                complete(run)
                assert run.writer.status_snapshot().accounting.turns == 1
                graph = run.workflow.graph(run.writer)
                assert all(node.state == ProofState.VERIFIED for node in graph.nodes)
                assemblies = [execution for execution in run.writer.executions('RECEIVED', limit=100)
                              if run.writer.invocation(execution).request.job.kind == JobKind.ASSEMBLE]
                assert len(assemblies) == 5, assemblies
                run.materialize(root / (name + '-export'))
            assert app.verify(root / (name + '-export'), package=package).verified
            source = (root / (name + '-export/src') / (name + '.lean')).read_text()
            if name == 'Plain':
                assert 'namespace Midas' in source and 'open _root_.Midas' not in source, source
            elif name == 'Collision':
                assert 'open _root_.Midas renaming' in source, source
            else:
                assert 'with_weak_namespace _root_.Midas' in source, source
            print(f'{name}: implicit, strict, instance and lambda proofs assembled and independently exported',
                  flush=True)

        # Reintroduce the old wrapper for one branch only. A real Lean rejection
        # must not prevent the other three independent assemblies from finishing.
        compose = proofs.compose
        def broken_branch(parent, children, dependencies):
            certificate = compose(parent, children, dependencies)
            for row in certificate['declarations']:
                source = row['source']
                if source.startswith('by\n  open ') and 'intro V inst x' in source:
                    row['source'] = source.removeprefix('by\n  ').replace(
                        ' in\n  exact (\n', ' in\n', 1).removesuffix('\n  )')
            return certificate
        selected = app.select(package, SelectionRequest('Plain.lean', all_unfinished=True))
        with patch('midas.export.proofs.compose', side_effect=broken_branch), app.start(
                root / 'failed-branch', package=package, selection=selected, config=config,
                workflow='scripted', script=(('prover', response(action)),)) as run:
            result = run.run()
            assert result.outcome == 'search_blocked' and result.reasons == ('no_ready_work',), result
            graph = run.workflow.graph(run.writer)
            assert sum(node.state == ProofState.VERIFIED for node in graph.nodes) == 4, graph
            assert sum(node.state == ProofState.CONDITIONAL for node in graph.nodes) == 2, graph
            assemblies = [run.writer.result(execution)
                          for execution in run.writer.executions('RECEIVED', limit=100)
                          if run.writer.invocation(execution).request.job.kind == JobKind.ASSEMBLE]
            assert len(assemblies) == 4, assemblies
            failures = [result for result in assemblies if result.status == ResultStatus.FAILED]
            assert len(failures) == 1, assemblies
            assert b'introN' in run.writer.read_artifact(failures[0].diagnostics, max_bytes=1024**2)
        print('One real assembly rejection: all independent assemblies completed without duplicate retries',
              flush=True)


if __name__ == '__main__':
    main()
