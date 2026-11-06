"""Opt-in joint decomposition, private definition assembly, and recovery with Lean."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

from midas.application import Application
from midas.application import records as r
from midas.domain import CheckKind, SelectionRequest
from midas.environment import PackageConfig
from midas.policy import Config, ModelRoute
from midas.strategy import Memory
from tests.check_application_lean import complete
from tests.test_strategy import proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-joint-lean-') as temporary:
        root = Path(temporary)
        fixture = Path('/tmp/midas-joint-decomposition-fixture')
        project = fixture/'project'
        project.mkdir(parents=True, exist_ok=True)
        (project/'Main.lean').write_text('import Lean\nnoncomputable def value : Nat := sorry\n'
                                      'theorem property : value = 0 := by sorry\n')
        app = Application('/tmp/midas-new-checker-runtime')
        package = (app.environment(fixture/'package') if (fixture/'package').exists() else
            app.package(fixture/'package', PackageConfig('4.35.0-rc3', toolchain,
                (('project', project),), ('project/Main.lean',), ('project', 'toolchain/lib/lean'))))
        selection = app.select(package, SelectionRequest('Main.lean', all_unfinished=True))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, reviewer=route)
        config = replace(config, scheduling=replace(config.scheduling, model_concurrency=1),
                         run=replace(config.run, runtime_ms=180000))
        dep = lambda name, key: dict(parameter=name, submission='current', key=key)
        action = dict(mode='candidate', rationale='Choose and verify a supported witness.', definitions=[], imports=[],
            proof='Prove existence, choose a value, then verify its specification.', proof_departures='',
            entries=[dict(key='exists', name='exists_zero', scope_target='value',
                          statement='∃ n : Nat, n = 0', proof_body=None, proof='Choose zero.', dependencies=[]),
                     dict(key='value', name='value', scope_target=None, statement=None,
                          proof_body='Classical.choose h', proof='Choose the witness.', dependencies=[dep('h', 'exists')]),
                     dict(key='spec', name='chosen_spec', scope_target='property',
                          statement='value = 0 ∧ True', proof_body=None, proof='Use the chosen witness specification.',
                          dependencies=[dep('value', 'value'), dep('h', 'exists')]),
                     dict(key='property', name='property', scope_target=None, statement=None,
                          proof_body='by exact hp.1', proof='Use its specification.',
                          dependencies=[dep('value', 'value'), dep('hp', 'spec')])])
        child = dict(proof(), mode='decomposition', proof_body='by exact ⟨0, zero⟩',
                     children=[dict(name='zero', statement='(0 : Nat) = 0', proof='Reflexivity.',
                                    usefulness='Witness specification.', depends_on=[])])
        review = b'{"verdict":"accept","rationale":"The glue uses its stated premise.","findings":[],"question":"","urls":[],"child_declarations":[]}'
        script = (('prover', response(action)), ('prover', response(child)),
                  ('prover', response(dict(proof(), proof_body='by rfl'))),
                  ('prover', response(dict(proof(), proof_body='by exact ⟨Classical.choose_spec h, trivial⟩'))),
                  *(('reviewer', review) for _ in range(4)))
        saw_pending = []
        with app.start(root/'run', package=package, selection=selection, config=config,
                       workflow='scripted', script=script) as run:
            def inspect(_):
                graph, pending = run.workflow.work_graph(run.writer)
                if pending:
                    saw_pending.append(True)
                    actual = run.workflow.graph(run.writer)
                    assert all(actual.node(n.owner).selected is None for n in graph.nodes
                               if n.owner.obligation_id in actual.roots)
                    run.request_stop()
            result = run.run(observe=inspect)
            assert result.exit_code == 130, result
            assert saw_pending, 'No private joint proof was observed'
        with app.resume(root/'run') as run:
            result = run.run()
            if result.exit_code:
                for node in run.workflow.graph(run.writer).nodes:
                    _, state = r.owner_state(run.writer, node.owner.obligation_id)
                    print(node.owner, node.state, state.reason, state.submission, flush=True)
                    if state.submission:
                        print(r.value(run.writer, 'application.submissions', state.submission), flush=True)
                    if state.memory:
                        print(Memory.load(r.read(run.writer, state.memory)), flush=True)
                print('EXPORT', [(item.key, r.value(run.writer, 'application.export', item.key))
                      for item in run.writer.records('application.export', limit=100)], flush=True)
            assert result.exit_code == 0, result
            graph = run.workflow.graph(run.writer)
            assert len(graph.nodes) == 5
            first = graph.node(next(n.owner for n in graph.nodes if n.owner.obligation_id == graph.roots[0]))
            assert first.selected.kind == CheckKind.CONDITIONAL and first.assembly is not None
            run.materialize(root/'bundle')
        with app.resume(root/'run') as run:
            complete(run)
        assert app.verify(root/'bundle', package=package).verified
        print('joint supporting obligations, nested decomposition, closed witness replay, interrupted resume and export passed', flush=True)


if __name__ == '__main__':
    main()
