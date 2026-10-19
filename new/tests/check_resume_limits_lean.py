"""Opt-in resume/resource regression with real Lean; needs AGENTS.md permission."""
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

from midas.application import Application, records as r
from midas.domain import JobKind, SelectionRequest
from midas.environment import PackageConfig
from midas.policy import Config, ModelRoute
from tests.test_strategy import proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-resume-limits-') as temporary:
        root = Path(temporary)
        project = root/'project'
        project.mkdir()
        (project/'Main.lean').write_text('import Lean\ntheorem target : True := by sorry\n')
        app = Application('/tmp/midas-new-checker-runtime')
        package = app.package(root/'package', PackageConfig('4.35.0-rc3', toolchain,
            (('project', project),), ('project/Main.lean',), ('project', 'toolchain/lib/lean')))
        selected = app.select(package, SelectionRequest('Main.lean', all_unfinished=True))
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        base = Config()
        config = replace(base, prover=route, reviewer=route,
                         export=replace(base.export, timeout_ms=1))
        decompose = dict(proof(), mode='decomposition', proof_body='by have witness := h; trivial',
            children=[dict(name='h', statement='1 = 1', proof='Use reflexivity.',
                           usefulness='Proves the target.', depends_on=[])])
        review = b'{"verdict":"accept","rationale":"The glue uses its premise.","findings":[],"question":"","urls":[]}'
        with app.start(root/'run', package=package, selection=selected, config=config,
                       workflow='scripted', script=(('prover', response(decompose)),
                       ('prover', response(proof())), ('reviewer', review))) as run:
            result = run.run()
            assert result.exit_code != 0, result
            graph = run.workflow.graph(run.writer)
            checks = {n.owner: n.selected for n in graph.nodes}
            assert all(checks.values()), graph
            name = 'assembly:'+graph.roots[0]
            failure = r.value(run.writer, 'application.export', name)
            assert failure[0] == 'failed', failure
            executions = [run.writer.invocation(key) for key in run.writer.executions('RECEIVED', limit=100)]
            failed = next(i for i in executions if i.request.job.kind == JobKind.ASSEMBLE)
            receipt = run.writer.receipt(failed.original_execution_id)
            usage = r.value(run.writer, 'application.export', 'usage')
            turns = run.writer.status_snapshot().accounting.turns
            frozen = run.writer.frozen_run().configuration
        print('Real Lean assembly failed under its original 1 ms limit.', flush=True)
        calls = []
        with app.resume(root/'run', increases={
                'checker': {'memory_mib': 8192, 'wall_ms': 480000},
                'export': {'memory_mib': 8192, 'timeout_ms': 480000},
                'scheduling': {'compiler_memory_mib': 8192}}) as run:
            assert r.value(run.writer, 'application.export', 'usage') == usage
            operations = run.controller.operations
            def observe(callback):
                def execute(request, context):
                    calls.append(request.job.kind)
                    return callback(request, context)
                return execute
            run.controller.operations = replace(operations, **{field.name:
                replace(op, execute=observe(op.execute)) for field in fields(operations)
                if (op := getattr(operations, field.name)) is not None})
            result = run.run()
            assert result.exit_code == 0, result
            assert calls == [JobKind.ASSEMBLE, JobKind.INTEGRATE, JobKind.EXPORT_VERIFY], calls
            assert {n.owner: n.selected for n in run.workflow.graph(run.writer).nodes} == checks
            assert run.writer.status_snapshot().accounting.turns == turns
            assert run.writer.frozen_run().configuration == frozen
            assert run.writer.invocation(failed.original_execution_id) == failed
            assert run.writer.receipt(failed.original_execution_id) == receipt
            run.writer.validate(force_artifacts=True)
        with app.resume(root/'run') as run:
            assert run.run().exit_code == 0
            assert run.writer.status_snapshot().accounting.turns == turns
        print('Raised-limit resume assembled, integrated and verified; proofs, turns, receipts and usage retained.', flush=True)


if __name__ == '__main__':
    main()
