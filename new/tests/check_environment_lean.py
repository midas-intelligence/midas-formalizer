"""Opt-in real environment preparation and executor checks; requires AGENTS approval.

python3 -B -m tests.check_environment_lean /absolute/path/to/toolchain/bin/lean
"""
from dataclasses import replace
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import time

from midas.domain import CapacityGrant, ResourceLimits, ResultStatus
from midas.environment import (AuthorizedInput, Bootstrap, Dependency, Environment, EnvironmentError,
    PackageConfig, Pin, file_hash, inventory_modules, replay_dependencies, resolve_closure)
from midas.environment.package import output
from midas.executor import Executor, Mount, Outcome
from tests.test_executor import invocation, success
from tests.test_environment import lock


def main():
    compiler = Path(sys.argv[1]).resolve(strict=True)
    toolchain = compiler.parent.parent
    version = toolchain.name.split('---v')[-1]
    with TemporaryDirectory(prefix='midas-environment-lean-') as temporary:
        root = Path(temporary)
        source = root / 'source'
        source.mkdir()
        (source / 'Main.lean').write_text('example : True := by trivial\n')
        workspace = root / 'worker'
        workspace.mkdir()
        config = PackageConfig(version, toolchain, (('project', source),),
                               ('project/Main.lean',), ('project', 'toolchain/lib/lean'))
        with Executor(root, workers=1, compiler_slots=1, compiler_memory_mib=4096) as executor:
            def work(request, context):
                started = time.monotonic()
                env = Environment.prepare(root / 'package', config, context)
                print(f'package/version/source preflight: passed ({time.monotonic()-started:.1f}s)', flush=True)
                env.validate(hash_toolchain=True, fresh=True, check=context.check)
                restored = Environment.restore(env.directory, manifest=env.manifest, expected_id=env.id,
                                                toolchain=toolchain, check=context.check)
                assert restored.id == env.id
                shutil.copytree(env.directory, root / 'relocated')
                restored = Environment.restore(root / 'relocated', manifest=env.manifest, expected_id=env.id,
                                                toolchain=toolchain, check=context.check)
                assert restored.id == env.id
                print('exact toolchain admission and package relocation: passed', flush=True)
                for name, proof, expected in (
                    ('Good', 'True := by trivial', Outcome.COMPLETED),
                    ('Bad', 'False := by trivial', Outcome.COMPILER_ERROR),
                ):
                    path = workspace / (name + '.lean')
                    path.write_text('example : ' + proof + '\n')
                    authorized = AuthorizedInput(path.name, path, file_hash(path), path.stat().st_size)
                    inputs = env.materialize((authorized,), workspace / name, check=context.check)
                    result = context.run_process(env.launch(('/inputs/' + path.name,), inputs=inputs))
                    assert result.outcome == expected, result
                print('generated input checking and rejection: passed', flush=True)
                build = workspace / 'build'
                build.mkdir()
                module = workspace / 'Native.lean'
                module.write_text('def main : IO Unit := IO.println "native package OK"\n')
                inputs = env.materialize((AuthorizedInput(module.name, module, file_hash(module), module.stat().st_size),),
                                          workspace / 'native-inputs', check=context.check)
                output(context.run_process(env.launch(('--root=/inputs', '--c=/build/Native.c', '/inputs/Native.lean'), inputs=inputs, build=build)))
                output(context.run_process(env.launch(('-O2', '-o', '/build/native', '/build/Native.c'),
                                                       build=build, executable='toolchain/bin/leanc')))
                assert (build / 'native').is_file()
                print('frozen native compiler runtime: passed', flush=True)
                dependency_checks(env, context, root)
                (source / 'Main.lean').write_text('example : False := by trivial\n')
                try:
                    Environment.prepare(root / 'rejected', config, context)
                except EnvironmentError:
                    assert not (root / 'rejected').exists()
                else:
                    raise AssertionError('Bad source package was published')
                print('failed preflight leaves no published package: passed', flush=True)
                return success(request, context)

            inv = invocation(limits=ResourceLimits(600000, 4096, 16 * 1024**2, 64 * 1024**2))
            executor.start(inv, work, grant=CapacityGrant(inv), workspace=workspace)
            while not (finished := executor.poll()):
                time.sleep(0.05)
            result = finished[0]
            if result.result.status != ResultStatus.SUCCEEDED:
                raise AssertionError(result)
    print('environment Lean checks passed', flush=True)


def dependency_checks(env, context, root):
    """Small real Git/source/compiled closure, including a transitive module import."""
    dep = root / 'dependency'
    dep.mkdir()
    (dep / 'Base.lean').write_text('def baseValue : Nat := 1\n')
    (dep / 'Dep.lean').write_text('import Base\ndef value : Nat := baseValue\n')
    (dep / 'lake-manifest.json').write_bytes(lock())
    (dep / 'lakefile.toml').write_text('name = "dep"\n')
    (dep / 'lean-toolchain').write_text('leanprover/lean4:v4.35.0-rc3\n')
    bootstrap = Bootstrap.installed()
    def git(*args):
        return bootstrap.run(context, ('/usr/bin/git', '-C', '/repo', *args),
                             mounts=(Mount(str(dep), '/repo', True),))
    git('init', '-q')
    git('add', 'Base.lean', 'Dep.lean', 'lake-manifest.json', 'lakefile.toml', 'lean-toolchain')
    git('-c', 'user.name=Midas Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
    revision = git('rev-parse', 'HEAD').decode().strip()
    library = dep / '.lake/build/lib/lean'
    library.mkdir(parents=True)
    authorized = tuple(AuthorizedInput(name + '.lean', dep / (name + '.lean'), file_hash(dep / (name + '.lean')),
                                       (dep / (name + '.lean')).stat().st_size) for name in ('Base', 'Dep'))
    inputs = env.materialize(authorized, context.workspace / 'dependency-inputs', check=context.check)
    for name in ('Base', 'Dep'):
        recipe = env.launch(('--root=/inputs', '-o', '/build/' + name + '.olean',
                             '-i', '/build/' + name + '.ilean', '/inputs/' + name + '.lean'), inputs=inputs, build=library)
        variables = tuple((key, '/build:' + value if key == 'LEAN_PATH' else value) for key, value in recipe.variables)
        output(context.run_process(replace(recipe, variables=variables)))
    dependency = Dependency('dep', dep, ('.lake/build/lib/lean',))
    referenced = Environment.prepare(root / 'referenced-package', PackageConfig(
        env.manifest.settings.version, env.toolchain,
        (('project', env.roots['project']), ('dep', dep)), ('project/Main.lean',),
        ('project', 'dep/.lake/build/lib/lean', 'toolchain/lib/lean'),
        preflight_imports=('Dep',)), context)
    assert referenced.roots['dep'] == dep
    assert not (referenced.directory / 'dep').exists()
    assert Environment.restore(referenced.directory).id == referenced.id
    original = (dep / 'Base.lean').read_bytes()
    (dep / 'Base.lean').write_bytes(b'def baseValue : Nat := 2\n')
    try:
        referenced.validate()
    except EnvironmentError:
        pass
    else:
        raise AssertionError('Changed referenced dependency was accepted')
    finally:
        (dep / 'Base.lean').write_bytes(original)
    print('referenced dependency, isolated import and mutation rejection: passed', flush=True)
    pin = Pin('dep', revision, 'https://example.invalid/dep', 'lakefile.toml')
    closure = resolve_closure(lock((pin,)), ('dep',), (dependency,), context)
    modules = inventory_modules(closure, (dependency,), context, required=('Dep',),
                                standard=frozenset(env.manifest.modules))
    replay = replay_dependencies(env, modules, (dependency,), context)
    assert replay.build_order == ('Base', 'Dep'), replay
    assert replay.imports == (('Base', ()), ('Dep', ('Base',))), replay
    print('pinned Git closure, module admission and source replay: passed', flush=True)


if __name__ == '__main__':
    main()
