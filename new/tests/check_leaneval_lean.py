"""Opt-in Lean/bubblewrap/comparator integration; requires AGENTS.md permission."""
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from midas.application import Application
from midas.domain import encode_submission
from midas.domain import encoding as codec
from midas.environment import PackageConfig, file_hash
from midas.environment.archive import FORMAT, TOOLS
from midas.leaneval import cli, workspace


HARNESS = b'''import Lean
def main : IO UInt32 := do
  let config <- IO.FS.readFile "config.json"
  let config <- IO.ofExcept (Lean.Json.parse config)
  let config := config.setObjVal! "enable_nanoda" (Lean.Json.bool true)
  IO.FS.withTempFile fun handle path => do
    handle.putStr config.pretty
    handle.flush
    let child <- IO.Process.spawn { cmd := "lake", args := #["env", "comparator", path.toString] }
    child.wait
'''


def make_runtime(directory, executables, toolchain):
    def copy(source, relative):
        target = directory/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source).resolve(), target)
        target.chmod(0o755)
    binaries = ('sh', 'git', 'cc', 'ld', 'ar', 'uname')
    paths = [Path(shutil.which(name)) for name in binaries]
    for name, path in zip(binaries, paths):
        copy(path, 'bin/'+name)
    # Capture only the declared executables' loader closure as regular files.
    for path in (*executables, *paths):
        result = subprocess.run(['/usr/bin/ldd', str(path)], capture_output=True, timeout=20)
        for match in re.finditer(r'(?:=>\s+)?(/\S+)\s+\(0x[0-9a-f]+\)', result.stdout.decode()):
            library = Path(match[1])
            if library.is_relative_to(toolchain):
                continue
            relative = library.as_posix().removeprefix('/usr/').lstrip('/')
            copy(library, relative)


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    tool_root = Path(sys.argv[2]).resolve(strict=True)
    lean4export = Path(sys.argv[3]).resolve(strict=True) if len(sys.argv) > 3 else tool_root/'lean4export'
    with TemporaryDirectory(prefix='midas-leaneval-lean-') as temporary:
        root = Path(temporary)
        repo = root/'benchmark'
        starter = repo/'generated/sample'
        starter.mkdir(parents=True)
        source = b'import Lean\nimport Submission.Helper\nnamespace Submission\ntheorem goal : True := by sorry\nend Submission\n'
        data = {
            'Submission.lean': source,
            'Submission/Helper.lean': b'import Lean\nnamespace Submission\ntheorem helper : True := by trivial\nend Submission\n',
            'Challenge.lean': b'import Lean\ntheorem goal : True := by sorry\n',
            'Solution.lean': b'import Lean\nimport Submission\ntheorem goal : True := Submission.goal\n',
            'WorkspaceTest.lean': HARNESS,
            'lean-toolchain': b'leanprover/lean4:v4.35.0-rc3\n',
            'lakefile.toml': b'name="sample"\ntestDriver="workspace_test"\n[[lean_lib]]\nname="Challenge"\n[[lean_lib]]\nname="Solution"\n[[lean_lib]]\nname="Submission"\n[[lean_exe]]\nname="workspace_test"\nroot="WorkspaceTest"\n',
            'holes.json': codec.canonical(dict(id='sample', module='Sample', holes=[dict(name='goal', kind='theorem')])),
            'config.json': codec.canonical(dict(challenge_module='Challenge', solution_module='Solution',
                theorem_names=['goal'], permitted_axioms=['propext', 'Quot.sound', 'Classical.choice'], enable_nanoda=True)),
        }
        for path, payload in data.items():
            target = starter/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        for name in ('lakefile.toml', 'lean-toolchain'):
            (repo/name).write_bytes(data[name])
        (repo/'LICENSE').write_bytes(b'Test fixture\n')
        (repo/'SECURITY.md').write_bytes(b'Test fixture\n')
        (repo/'lake-manifest.json').write_bytes(codec.canonical(dict(version='1.1.0', packagesDir='.lake/packages', packages=[])))
        catalog = repo/'manifests/problems/sample.toml'
        catalog.parent.mkdir(parents=True)
        catalog.write_bytes(b'id="sample"\nstatus="active"\nmodule="Sample"\nholes=["goal"]\n')
        def git(*args):
            return subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.test', '-c', 'core.hooksPath=/dev/null', *args],
                check=True, capture_output=True, timeout=30).stdout
        git('init')
        git('add', '.')
        git('commit', '-m', 'fixture')
        revision = git('rev-parse', 'HEAD').decode().strip()
        tools = {name: lean4export if name == 'lean4export' else tool_root/name for name in TOOLS}
        verification = dict(format=FORMAT, benchmark=str(repo), revision=revision, problem='sample',
            tools={name: dict(path=str(path), sha256=file_hash(path)) for name, path in tools.items()},
            dependencies=[], allow_archived_fixture=False)
        config_path = root/'verification.json'
        config_path.write_bytes(codec.canonical(verification))
        runtime = root/'runtime'
        runtime.mkdir()
        make_runtime(runtime, (*tools.values(), *(toolchain/'bin'/n for n in ('lean', 'lake', 'leanc', 'clang', 'ld.lld'))), toolchain)
        project = root/'project'
        shutil.copytree(starter, project)
        subprocess.run([str(toolchain/'bin/lean'), '--root='+str(project), '-o', str(project/'Submission/Helper.olean'),
            '-i', str(project/'Submission/Helper.ilean'),
            str(project/'Submission/Helper.lean')], check=True, capture_output=True, timeout=60)
        app = Application('/tmp/midas-new-checker-runtime')
        app.package(root/'package', PackageConfig('4.35.0-rc3', toolchain, (('project', project),),
            ('project/Submission.lean',), ('project', 'toolchain/lib/lean')))
        def command(*args, code=0):
            out, err = io.StringIO(), io.StringIO()
            result = cli.main([str(a) for a in (*args, '--json')], stdout=out, stderr=err,
                              application_factory=lambda *a, **kw: Application('/tmp/midas-new-checker-runtime'))
            assert result == code, (args, result, err.getvalue(), out.getvalue())
            return [json.loads(line) for line in out.getvalue().splitlines()]
        command('prepare', root/'workspace', '--package', root/'package', '--verification-config', config_path)
        bound = workspace.load(app, root/'workspace')
        assert bound.library is not None
        assert all(path.is_file() and len(path.stem) == 64 for path in (root/'workspace/library').iterdir())
        print('archive, exact selection and local helper source replay passed', flush=True)
        candidate = encode_submission(dict(format='midas-scoped-candidates', submission_id=str(uuid4()),
            selection_id=bound.selection.id, definitions=[], entries=[dict(key='parent',
            subject=dict(kind='target', name='Submission.goal'), proof='by exact Submission.helper', dependencies=[],
            review=None, revision_of=None, definition_scope=dict(statement=0, proof=0))]))
        (root/'candidate.bin').write_bytes(candidate)
        command('start', root/'run', '--workspace', root/'workspace', '--candidates', root/'candidate.bin',
                '--output', root/'bundle', '--quiet')
        assert command('status', root/'run')[0]['turns'] == 0
        print('ordinary supplied CLI, library export and source audit passed', flush=True)
        options = ('--workspace', root/'workspace', '--verification-config', config_path, '--runtime', runtime)
        result = command('finalize', root/'bundle', root/'submission', *options)[0]
        assert result['submission_ready']
        assert (root/'submission/judge-submission/sample/Submission.lean').is_file()
        assert (root/'submission/judge-submission/sample/Submission/Helper.lean').is_file()
        result = command('verify-submission', root/'submission', *options, '--evidence-output', root/'evidence.bin')[0]
        assert result['verified']
        print('real comparator, both kernels, publication and fresh independent verification passed', flush=True)
        target = root/'submission/submission/Submission.lean'
        target.write_bytes(target.read_bytes()+b'-- tampered\n')
        command('verify-submission', root/'submission', *options, code=2)
        print('tampered published submission rejected', flush=True)
        catalog.write_bytes(catalog.read_bytes().replace(b'status="active"', b'status="archived"'))
        git('add', '.')
        git('commit', '-m', 'archive fixture')
        verification.update(revision=git('rev-parse', 'HEAD').decode().strip(), allow_archived_fixture=True)
        config_path.write_bytes(codec.canonical(verification))
        command('prepare', root/'archived-workspace', '--package', root/'package', '--verification-config', config_path)
        archived_options = ('--workspace', root/'archived-workspace', '--verification-config', config_path, '--runtime', runtime)
        result = command('finalize', root/'bundle', root/'archived-result', *archived_options)[0]
        assert result['verified'] and result['fixture_only'] and not result['submission_ready']
        assert not (root/'archived-result/judge-submission').exists()
        print('archived fixture verified without judge-ready files', flush=True)


if __name__ == '__main__':
    main()
