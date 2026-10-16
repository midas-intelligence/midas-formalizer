"""Opt-in CLI checks with actual Lean/bubblewrap; requires AGENTS.md permission."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import subprocess
import sys

from midas.application import Application
from midas.domain import SelectionRequest
from midas.policy import Config, ModelRoute
from tests.check_application_lean import submission
from tests.test_strategy import proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-cli-lean-') as temporary:
        root = Path(temporary)
        cache = '/tmp/midas-new-checker-runtime'
        def cli(*args, code=0):
            result = subprocess.run([sys.executable, '-B', '-m', 'midas', *map(str, args)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            assert result.returncode == code, (args, result.returncode, result.stdout.decode(), result.stderr.decode())
            return [json.loads(line) for line in result.stdout.splitlines()]
        project = root/'project'
        project.mkdir()
        (project/'Main.lean').write_text('import Lean\ntheorem target : True := by sorry\n')
        spec = root/'package.json'
        spec.write_text(json.dumps(dict(version='4.35.0-rc3', toolchain=str(toolchain),
            roots=dict(project='project'), project_inputs=['project/Main.lean'],
            search_paths=['project', 'toolchain/lib/lean']), indent=2))
        cli('package', root/'package', '--config', spec, '--cache', cache, '--json')
        selected = cli('select', root/'package', '--source', 'Main.lean', '--output', root/'selection',
                       '--cache', cache, '--json')
        assert len(selected) == 2
        print('package and consolidated selection passed', flush=True)
        app = Application(cache)
        selection = app.select(root/'package', SelectionRequest('Main.lean', all_unfinished=True))
        candidate = root/'candidate'
        candidate.write_bytes(submission(selection))
        result = cli('start', root/'run', '--package', root/'package', '--selection', root/'selection',
            '--candidates', candidate, '--output', root/'bundle', '--cache', cache, '--quiet', '--json')[0]
        assert result['outcome'] == 'export_verified' and result['turns'] == 0
        assert cli('status', root/'run', '--json')[0]['outcome'] == 'export_verified'
        assert cli('status', root/'run', '--nodes', '--limit', 1, '--json')[0]['state'] == 'VERIFIED'
        cli('resume', root/'run', '--cache', cache, '--quiet', '--json')
        assert cli('verify', root/'bundle', '--package', root/'package', '--cache', cache, '--json')[0]['verified']
        cli('export', root/'run', root/'second-bundle', '--cache', cache, '--quiet', '--json')
        print('supplied start, status, authenticated resume, export and independent verify passed', flush=True)
        cli('fork', root/'run', root/'child', '--cache', cache, '--json')
        cli('resume', root/'child', '--cache', cache, '--quiet', '--json')
        assert not app.inspect(root/'child').status.executions
        cli('start', root/'partial', '--package', root/'package', '--selection', root/'selection',
            '--workflow', 'supplied', '--cache', cache, '--quiet', '--json', code=1)
        cli('export', root/'partial', root/'partial-bundle', '--allow-sorry', '--cache', cache, '--quiet', '--json')
        assert cli('status', root/'partial', '--json')[0]['proof'] == 'incomplete'
        assert cli('verify', root/'partial-bundle', '--package', root/'package', '--cache', cache, '--json')[0]['verified']
        print('fork reuse and explicitly incomplete export passed', flush=True)
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = root/'config.json'
        config.write_bytes(replace(Config(), prover=route, reviewer=route).encode())
        action = proof()
        action['proof_body'] = 'by trivial'
        (root/'reply').write_bytes(response(action))
        (root/'script.json').write_text('[{"role":"prover","file":"reply"}]')
        result = cli('start', root/'scripted', '--package', root/'package', '--selection', root/'selection',
            '--script', root/'script.json', '--config', config, '--cache', cache, '--quiet', '--json')[0]
        assert result['outcome'] == 'export_verified' and result['turns'] == 1
        print('scripted CLI shares checking, integration and verification passed', flush=True)


if __name__ == '__main__':
    main()
