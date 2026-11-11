"""Opt-in mixed/all-Lean decompositions; requires root AGENTS.md permission."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import time

from midas.application import Application, candidates, records as r
from midas.domain import ProofState, SelectionRequest
from midas.environment import PackageConfig
from midas.policy import Config, ModelRoute
from midas.prompt import prover
from tests.check_application_lean import complete
from tests.test_strategy import model_json, proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-child-proofs-') as temporary:
        root = Path(temporary)
        app = Application('/tmp/midas-new-checker-runtime')
        fixture = Path('/tmp/midas-application-fixture')
        fixture.mkdir(exist_ok=True)
        if not (fixture/'package').exists():
            project = fixture/'project'
            project.mkdir(exist_ok=True)
            (project/'Main.lean').write_text('import Lean\ntheorem target : True := by sorry\n')
            package = app.package(fixture/'package', PackageConfig('4.35.0-rc3', toolchain,
                (('project', project),), ('project/Main.lean',), ('project', 'toolchain/lib/lean')))
        else:
            package = app.environment(fixture/'package')
        selection = app.select(package, SelectionRequest('Main.lean', all_unfinished=True))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, reviewer=route)
        action = dict(proof(), mode='decomposition', proof_body='by have witness := second; trivial', children=[
            dict(name='second', statement='2 = 2', proof_body='by have witness := first; rfl', proof='',
                 usefulness='Conclusion.', depends_on=['first']),
            dict(name='first', statement='1 = 1', proof_body='by rfl', proof='',
                 usefulness='Premise.', depends_on=[])])
        review = model_json(dict(verdict='accept', rationale='The English-guided child follows by reflexivity.',
                                 findings=[], question='', urls=[], child_declarations=[]))

        # Interrupt after a supplied child checks, then finish and fork without
        # any more model calls. The compiler still checks every term and assembly.
        with app.start(root/'all-lean', package=package, selection=selection, config=config,
                workflow='scripted', script=(('prover', response(action)),)) as run:
            def stop_after_child(_):
                if any(n.selected is not None and n.owner.obligation_id not in run.workflow.graph(run.writer).roots
                       for n in run.workflow.graph(run.writer).nodes):
                    run.request_stop()
            assert run.run(observe=stop_after_child).exit_code == 130
            assert run.writer.status_snapshot().accounting.turns == 1
        with app.resume(root/'all-lean') as run:
            complete(run)
            assert run.writer.status_snapshot().accounting.turns == 1
            assert run.writer.counter('application.script', 'reviewer') == 0
            assert all(n.state == ProofState.VERIFIED for n in run.workflow.graph(run.writer).nodes)
            run.materialize(root/'export')
        assert app.verify(root/'export', package=package).verified
        with app.fork(root/'all-lean', root/'fork') as run:
            complete(run)
            assert not run.writer.status_snapshot().executions
        print('all-Lean children, dependency assembly, interrupted resume, fork and export: one model turn', flush=True)

        for english_index in (0, 1):
            mixed = deepcopy(action)
            mixed['children'][english_index].update(proof_body=None, proof='Use reflexivity.')
            with app.start(root/('mixed-'+str(english_index)), package=package, selection=selection, config=config,
                    workflow='scripted', script=(('prover', response(mixed)), ('reviewer', review),
                                                ('prover', response(proof())))) as run:
                complete(run)
                assert run.writer.status_snapshot().accounting.turns == 3
                assert run.writer.counter('application.script', 'reviewer') == 1
                assert all(n.state == ProofState.VERIFIED for n in run.workflow.graph(run.writer).nodes)
                assert len(run.writer.records('application.guidance', limit=10)) == 1
            print(f'mixed dependency direction {english_index}: one parent call, one review, one English child call', flush=True)

        invalid = deepcopy(action)
        invalid['children'][1]['proof_body'] = 'by exact missing_child_term'
        revision = dict(mode='revise', rationale='Correct the supplied child proof.', edits=[
            dict(path='/entries/first/proof', old=None, new='by rfl')])
        captured = []
        original = prover.render
        def capture(snapshot):
            captured.append(snapshot)
            return original(snapshot)
        with patch('midas.prompt.prover.render', side_effect=capture), app.start(root/'repair', package=package,
                selection=selection, config=config, workflow='scripted',
                script=(('prover', response(invalid)), ('prover', response(revision)))) as run:
            complete(run)
            assert run.writer.status_snapshot().accounting.turns == 2
            assert run.writer.counter('application.script', 'reviewer') == 0
            assert len(captured) == 2 and captured[0].owners[0] == captured[1].owners[0]
            assert b'missing_child_term' in b'\n'.join(s.text for s in captured[1].sections if s.kind == 'feedback')
            submissions = [candidates.submission(run.writer, row.key)
                           for row in run.writer.records('application.submissions', limit=10)]
            assert sorted(s['phase'] for s in submissions) == ['done', 'failed']
        print('invalid supplied term rejected; exact revision in proposing context: two model turns', flush=True)

        for label, guidance in (('delegate', 'Use reflexivity to prove the first child in its new context.'),
                                ('empty-guidance', '')):
            delegated = deepcopy(invalid)
            delegated['children'][1]['usefulness'] = ''
            delegate = dict(mode='revise', rationale='Delegate the failed Lean child.', edits=[
                dict(path='/entries/first/proof', old=None, new=None)])
            if guidance:
                delegate['edits'].append(dict(path='/children/first/proof', old=None, new=guidance))
            captured.clear()
            with patch('midas.prompt.prover.render', side_effect=capture):
                with app.start(root/label, package=package, selection=selection, config=config, workflow='scripted',
                        script=(('prover', response(delegated)), ('prover', response(delegate)), ('reviewer', review),
                                ('prover', response(proof())))) as run:
                    # Run's display observer is throttled; inspect each committed
                    # step so fast cache hits cannot pass the intended stop point.
                    deadline = time.monotonic() + 180
                    while not run.writer.records('application.guidance', limit=10):
                        assert time.monotonic() < deadline, 'No child guidance was saved'
                        run.step()
                        time.sleep(.001)
                    run.request_stop()
                    assert run.run().exit_code == 130
                    assert len(captured) == 2, len(captured)
                with app.resume(root/label) as run:
                    complete(run)
                    assert run.writer.status_snapshot().accounting.turns == 4
                    assert run.writer.counter('application.script', 'reviewer') == 1
                    assert len(captured) == 3 and captured[0].owners[0] == captured[1].owners[0]
                    assert captured[2].owners[0] != captured[1].owners[0]
                    assert guidance.encode() == next(s.text for s in captured[2].sections if s.name == 'proof-guidance')
                    assert all(n.state == ProofState.VERIFIED for n in run.workflow.graph(run.writer).nodes)
                    run.materialize(root/(label+'-export'))
            assert app.verify(root/(label+'-export'), package=package).verified
            print(f'{label}: null revision after failed Lean, child handoff, review, interrupted resume and export passed', flush=True)


if __name__ == '__main__':
    main()
