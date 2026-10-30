"""Opt-in application workflows with real Lean; requires root AGENTS.md permission."""
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import sys

from midas.application import Application
from midas.domain import SelectionRequest, encode_submission, joint_targets
from midas.environment import PackageConfig
from midas.policy import Config, ModelRoute
from tests.test_strategy import model_json, proof, response


def complete(run):
    result = run.run()
    if result.exit_code:
        from midas.application import records as r
        from midas.strategy import Memory
        for node in run.workflow.graph(run.writer).nodes:
            _, state = r.owner_state(run.writer, node.owner.obligation_id)
            print(node.owner, node.state, state.reason, state.submission, flush=True)
            if state.memory:
                print(Memory.load(r.read(run.writer, state.memory)), flush=True)
    assert result.exit_code == 0, result


def submission(selection, code='by trivial'):
    return encode_submission(dict(format='midas-scoped-candidates', submission_id=str(uuid4()),
        selection_id=selection.id, definitions=[], entries=[dict(key='parent',
        subject=dict(kind='target', name='target'), proof=code, dependencies=[], review=None,
        revision_of=None, definition_scope=dict(statement=0, proof=0))]))


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-application-lean-') as temporary:
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
        selected = app.select(package, SelectionRequest('Main.lean', all_unfinished=True))
        print('package and selection passed', flush=True)
        with app.start(root/'supplied', package=package, selection=selected, workflow='supplied') as run:
            run.submit(submission(selected))
            result = run.run()
            assert result.exit_code == 0, result
            run.materialize(root/'export')
            assert run.writer.status_snapshot().accounting.turns == 0
        print('supplied proof, integration and export passed', flush=True)
        with app.resume(root/'supplied') as run:
            assert run.run().exit_code == 0
        assert app.inspect(root/'supplied').completion.exit_code == 0
        print('authenticated resume and read-only inspection passed', flush=True)
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, reviewer=route)
        action = proof()
        action['proof_body'] = 'by trivial'
        with app.start(root/'scripted', package=package, selection=selected, config=config,
                       workflow='scripted', script=(('prover', response(action)),)) as run:
            result = run.run()
            assert result.exit_code == 0, result
            assert run.writer.status_snapshot().accounting.turns == 1
        print('scripted provider through the shared workflow passed', flush=True)
        assert app.verify(root/'export', package=package).verified
        print('standalone bundle recheck passed', flush=True)
        decompose = dict(action, mode='decomposition', proof_body='by have witness := h; trivial',
            children=[dict(name='h', statement='1 = 1', proof='Use reflexivity.', usefulness='Test dependency.', depends_on=[])])
        review = b'{"verdict":"accept","rationale":"The glue uses its stated premise.","findings":[],"question":"","urls":[]}'
        investigate = model_json(dict(verdict='research', rationale='Check the supporting argument.', findings=[],
            question='What proves reflexivity?', urls=[]))
        researched = model_json(dict(answer='Reflexivity follows from rfl.', unresolved=[]))
        research_config = replace(config, research=ModelRoute(provider='openai', model='fixture'))
        with app.start(root/'decomposition', package=package, selection=selected, config=research_config, workflow='scripted',
            script=(('prover', response(decompose)), ('prover', response(proof())),
                    ('reviewer', investigate), ('reviewer', review), ('research', researched))) as run:
            result = run.run()
            assert result.exit_code == 0, result
            assert run.writer.status_snapshot().accounting.turns == 5
        with app.resume(root/'decomposition') as run:
            assert run.run().exit_code == 0
        with app.fork(root/'decomposition', root/'fork') as run:
            complete(run)
            assert not run.writer.status_snapshot().executions
        with app.resume(root/'fork') as run:
            complete(run)
        print('decomposition, independent review, child proof, assembly and resume passed', flush=True)
        with app.start(root/'partial', package=package, selection=selected, workflow='supplied'):
            pass
        assert app.export(root/'partial', root/'partial-export', allow_sorry=True).exit_code == 0
        assert app.verify(root/'partial-export', package=package).verified
        print('explicit partial export and standalone recheck passed', flush=True)
        retrieve = dict(mode='retrieve', rationale='Inspect a library declaration.', names=['Nat.add'],
                        queries=[], backends=[])
        probe = dict(mode='probe', rationale='Try closing the current goal.', cases=[
            dict(id='inspect', code='let env ← Lean.getEnv\nLean.logInfo m!"Nat.add available: {(env.find? `Nat.add).isSome}"'),
            dict(id='close', code='Lean.Elab.Tactic.evalTactic (← `(tactic| trivial))')])
        with app.start(root/'helpers', package=package, selection=selected, config=config, workflow='scripted',
            script=(('prover', response(retrieve)), ('prover', response(probe)), ('prover', response(action)))) as run:
            def strict(reducer):
                def apply(*args):
                    try:
                        return reducer(*args)
                    except (ValueError, KeyError) as error:
                        raise AssertionError('Unexpected helper reduction error') from error
                return apply
            run.controller.operations = replace(run.controller.operations, **{field.name:
                replace(op, reduce=strict(op.reduce)) for field in fields(run.controller.operations)
                if (op := getattr(run.controller.operations, field.name)) is not None})
            result = run.run()
            assert result.exit_code == 0, result
            owner = selected.identities[0].id
            assert run.writer.counter('owner:'+owner, 'retrieval') == 1
            assert run.writer.counter('owner:'+owner, 'probe') == 2
            assert run.writer.status_snapshot().accounting.turns == 3
            from midas.application import helpers, records as r
            archives = helpers.advertised(run.writer, run.workflow.graph(run.writer).nodes[0].owner)
            text = helpers.page_text(r.read(run.writer, archives[-1].pages[0], 16000))
            assert b'Probe inspect (tactic): open (advisory)' in text, text
            assert b'Nat.add available: true' in text, text
            assert b'Probe close (tactic): closed_exploratory (advisory)' in text, text
            assert b'Goal 1:' in text, text
            assert b'0/' not in text, text
        print('named retrieval, probe, advisory evidence and original continuations passed', flush=True)
        for approved in (False, True):
            name = 'stop-approved' if approved else 'stop-rejected'
            plan = dict(decompose, children=[dict(decompose['children'][0], statement='False')]) if approved else decompose
            objection = dict(mode='stop', rationale='The child statement appears unprovable.',
                objection=dict(category='suspected_false', evidence='No witness of False exists.' if approved else
                    'The equality appears unprovable.'))
            finding = dict(category='mathematical', location='goal', issue='Equality is reflexive.',
                           suggested_revision='Use by rfl to prove 1 = 1.')
            verdict = model_json(dict(verdict='accept' if approved else 'reject',
                rationale='False cannot be proved.' if approved else 'Reflexivity proves the child.',
                findings=[] if approved else [finding], question='', urls=[]))
            with app.start(root/name, package=package, selection=selected, config=config, workflow='scripted',
                script=(('prover', response(plan)), ('prover', response(objection)),
                        ('prover', response(action if approved else proof())), ('reviewer', review),
                        ('reviewer', verdict))) as run:
                complete(run)
                graph = run.workflow.graph(run.writer)
                child = next(n for n in graph.nodes if n.owner.obligation_id not in graph.roots)
                assert child.owner.generation == int(approved)
                assert run.writer.counter('owner:'+selected.identities[0].id, 'backtracks') == int(approved)
                assert not run.writer.counter('owner:'+selected.identities[0].id, 'repair_open')
                assert run.writer.status_snapshot().accounting.turns == 5
                from midas.application import records as r
                from midas.strategy import Memory
                report = r.value(run.writer, 'application.objections', child.owner.obligation_id)
                assert bool(report) == approved
                if not approved:
                    _, state = r.owner_state(run.writer, child.owner.obligation_id)
                    assert any('Use by rfl' in message for observation in Memory.load(r.read(run.writer, state.memory)).observations
                               for message in observation.messages)
            with app.resume(root/name) as run:
                assert run.run().exit_code == 0
        print('reviewed stop approval repairs parent; rejection continues child; both resume passed', flush=True)
        joint_fixture = Path('/tmp/midas-application-joint-fixture')
        joint_fixture.mkdir(exist_ok=True)
        if not (joint_fixture/'package').exists():
            project = joint_fixture/'project'
            project.mkdir(exist_ok=True)
            (project/'Main.lean').write_text('import Lean\ndef value : Nat := sorry\ntheorem property : value = value := by sorry\n')
            joint_package = app.package(joint_fixture/'package', PackageConfig('4.35.0-rc3', toolchain,
                (('project', project),), ('project/Main.lean',), ('project', 'toolchain/lib/lean')))
        else:
            joint_package = app.environment(joint_fixture/'package')
        joint_selection = app.select(joint_package, SelectionRequest('Main.lean', all_unfinished=True))
        assert len(joint_targets(joint_selection)) == 2
        key = str(uuid4())
        payload = encode_submission(dict(format='midas-scoped-candidates', submission_id=key,
            selection_id=joint_selection.id, definitions=[], entries=[dict(key=name,
                subject=dict(kind='target', name=name), proof=body, dependencies=dependencies,
                review=dict(decision='approve', rationale='Use the earlier closed definition.') if dependencies else None,
                revision_of=None, definition_scope=dict(statement=0, proof=0))
                for name, body, dependencies in (('value', '0', []), ('property', 'by rfl',
                    [dict(parameter='value', entry=dict(submission_id=key, key='value'))]))]))
        with app.start(root/'joint', package=joint_package, selection=joint_selection, workflow='supplied') as run:
            run.submit(payload)
            result = run.run()
            assert result.exit_code == 0, result
            run.materialize(root/'joint-export')
        with app.resume(root/'joint') as run:
            assert run.run().exit_code == 0
        assert app.verify(root/'joint-export', package=joint_package).verified
        print('joint definition/property acceptance, export and resume passed', flush=True)
        # Route by the displayed local subject, so concurrent sibling turns do
        # not depend on worker completion order.
        from midas.provider import ModelOutput, Route
        from midas.domain import Usage
        splitting = dict(decompose, proof_body='by have witness := second; trivial', children=[
            dict(name='first', statement='1 = 1', proof='Use reflexivity.', usefulness='Premise.', depends_on=[]),
            dict(name='second', statement='2 = 2', proof='Use reflexivity.', usefulness='Conclusion.', depends_on=['first'])])
        alias_probe = dict(mode='probe', rationale='Check the inherited alias.', cases=[dict(id='alias',
            code='Lean.Elab.Tactic.evalTactic (← `(tactic| have witness := first; rfl))')])
        second_calls = []
        def transport(request, raw, *args):
            if request.role == 'reviewer':
                assert b'Selected review entry: parent' in raw
                if b'lemma: second' in raw:
                    assert b'first <- base:first' in raw
                else:
                    assert b'first <- local entry first' in raw
                    assert b'Proof: pending obligation' in raw
                answer = review
            elif b'lemma: second' in raw:
                assert b'INTERFACES inherited-0\\nfirst :\\n' in raw
                second_calls.append(1)
                answer = response(alias_probe if len(second_calls) == 1 else dict(proof(), proof_body='by have witness := first; rfl'))
            elif b'lemma: first' in raw:
                answer = response(proof())
            else:
                answer = response(splitting)
            return ModelOutput(raw=answer, usage=Usage(0, 0, 0, 0))
        app.transports = {Route.from_config(route).scope: transport}
        with app.start(root/'aliases', package=package, selection=selected, config=config, workflow='live') as run:
            complete(run)
            assert len(second_calls) == 2
            child = next(n for n in run.workflow.graph(run.writer).nodes
                if run.writer.counter('owner:'+n.owner.obligation_id, 'probe'))
            from midas.application import helpers, records as r
            archives = helpers.advertised(run.writer, child.owner)
            text = helpers.page_text(r.read(run.writer, archives[0].pages[0], 16000))
            assert b'closed_exploratory' in text, text
        print('derived probe with inherited checked aliases and concurrent sibling work passed', flush=True)


if __name__ == '__main__':
    main()
