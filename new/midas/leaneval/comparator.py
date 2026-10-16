"""Run the archived Lake harness using the existing reserved process executor."""
from dataclasses import replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import tomllib

from ..application.preparation import operate
from ..domain import encoding as codec
from ..environment import File, match_relocation, scan
from ..environment.inventory import chunks, copy_file, relative
from ..environment.dependencies import lake_name
from ..environment.package import output
from ..executor import LaunchSpec, Mount
from .workspace import evidence, starter

MARKERS = (b'nanoda kernel accepts the solution', b'Lean default kernel accepts the solution',
           b'Your solution is okay!')
WHICH = b'#!/bin/sh\n[ "$#" -eq 1 ] || exit 2\ncommand -v -- "$1"\n'


@lru_cache(maxsize=1024)
def submission_path(path):
    relative(path)
    if path != 'Submission.lean' and not (path.startswith('Submission/') and path.endswith('.lean')):
        raise ValueError('Submission cannot replace trusted workspace inputs')


def overlay(trusted, submission):
    if 'Submission.lean' not in submission:
        raise ValueError('Submission root is missing')
    for path, data in submission.items():
        submission_path(path)
        if path != 'Submission.lean' and path in trusted and data != trusted[path]:
            raise ValueError('Submission helper differs from trusted starter')
    return dict(trusted, **submission)


def lake_metadata(archive, dependencies):
    trusted = starter(archive)
    lake = tomllib.loads(trusted['lakefile.toml'].decode())
    lock = codec.load_metadata(next(f.data for f in archive.files if f.path == 'lake-manifest.json'))
    if type(lock.get('version')) is not str:
        raise ValueError('Trusted Lake lock requires a version')
    direct = [r['name'] for r in lake.get('require', ())]
    if len(set(direct)) != len(direct) or not set(direct) <= set(dependencies):
        raise ValueError('Missing or duplicate starter dependencies')
    packages = [dict(p, inherited=lake_name(p['name']) not in direct)
                for p in lock['packages'] if lake_name(p['name']) in dependencies]
    if {lake_name(p['name']) for p in packages} != set(dependencies):
        raise ValueError('Workspace dependencies differ from the trusted lock')
    lock = dict(lock, name=lake['name'], packagesDir='.lake/packages', lakeDir='.lake', fixedToolchain=False, packages=packages)
    overrides = dict(version=lock['version'], packages=[dict(type='path', dir='/deps/'+lake_name(p['name']),
        name=p['name'], manifestFile=p.get('manifestFile', 'lake-manifest.json'),
        configFile=p['configFile'], inherited=p['inherited']) for p in packages])
    payload = codec.canonical(lock)
    if 'lake-manifest.json' in trusted and trusted['lake-manifest.json'] != payload:
        raise ValueError('Starter lock conflicts with admitted workspace closure')
    return payload, codec.canonical(overrides)


def acceptance(result):
    output(result)
    if result.stdout.truncated or result.stderr.truncated:
        raise ValueError('Comparator output was truncated')
    lines = {line.strip() for stream in (result.stdout.data, result.stderr.data) for line in stream.splitlines()}
    if not set(MARKERS) <= lines:
        raise ValueError('Comparator did not accept both kernels and the graph comparison')


def run(environment, archive, submission, verification, runtime, *, config, cancellation=None):
    runtime = Path(runtime).absolute()
    trusted = starter(archive)
    files = overlay(trusted, submission)
    dependencies = tuple(d.name for d in verification.dependencies)
    lock, overrides = lake_metadata(archive, dependencies)
    if any(path == '.lake' or path.startswith('.lake/') for path in files):
        raise ValueError('Starter must not supply build artifacts')
    files['lake-manifest.json'] = lock

    def execute(context):
        match_relocation(verification, archive, max_bytes=config.export.max_bytes, check=context.check)
        environment.validate(hash_toolchain=True, fresh=True, check=context.check)
        runtime_inventory, runtime_stamps = scan(runtime, maximum=config.export.max_bytes, check=context.check)
        shell = runtime_inventory.by_path.get('bin/sh')
        if shell is None or not shell.executable:
            raise ValueError('Comparator runtime requires an executable bin/sh')
        project, build, tools = (context.workspace/p for p in ('project', 'lake', 'tools'))
        for path in (project, build, tools):
            path.mkdir()
        for path, data in files.items():
            target = project/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (build/'package-overrides.json').write_bytes(overrides)
        total = sum(f.size for f in runtime_inventory.files) + sum(len(data) for data in files.values())
        for tool in verification.tools:
            size = tool.path.stat().st_size
            total += size
            copy_file(tool.path, tools/tool.name, File(tool.name, size, True, tool.sha256), check=context.check)
        (tools/'which').write_bytes(WHICH)
        (tools/'which').chmod(0o755)
        roots = ('toolchain', *dependencies)
        total += sum(f.size for f in environment.manifest.inventory.files if f.path.split('/')[0] in roots)
        if total > config.export.max_bytes:
            raise ValueError('Comparator inputs exceed byte limit')
        # Nonoverlapping mounts keep sources/tools/dependencies read-only and only Lake output writable.
        mounts = [Mount(str(environment.toolchain), '/toolchain'), Mount(str(tools), '/tools'),
                  Mount(str(build), '/workspace/.lake', True), Mount(str(runtime), '/usr')]
        mounts.extend(Mount(str(runtime/p), '/'+p) for p in ('bin', 'lib', 'lib64') if (runtime/p).is_dir())
        mounts.extend(Mount(str(path), '/workspace/'+path.name) for path in sorted(project.iterdir()))
        mounts.extend(Mount(str(environment.roots[name]), '/deps/'+name) for name in dependencies)
        variables = dict(PATH='/tools:/toolchain/bin:/usr/bin:/bin', HOME='/tmp', TMPDIR='/tmp', LC_ALL='C',
            LEAN_SYSROOT='/toolchain', COMPARATOR_BIN='/tools/comparator', COMPARATOR_LANDRUN='/tools/landrun',
            COMPARATOR_LEAN4EXPORT='/tools/lean4export', COMPARATOR_NANODA='/tools/nanoda_bin')
        recipe = LaunchSpec(('/toolchain/bin/lake', 'test'), tuple(mounts), environment.launcher,
                            environment.manifest.launcher_sha256, tuple(variables.items()), cwd='/workspace')
        before = scan(project, check=context.check)[0], scan(tools, check=context.check)[0]
        result = context.run_process(recipe)
        scan(build, maximum=config.export.max_bytes-total, check=context.check)
        acceptance(result)
        if before != (scan(project, check=context.check)[0], scan(tools, check=context.check)[0]):
            raise ValueError('Comparator sources or tools changed')
        if (runtime_inventory, runtime_stamps) != scan(runtime, maximum=config.export.max_bytes, check=context.check):
            raise ValueError('Comparator runtime changed')
        match_relocation(verification, archive, max_bytes=config.export.max_bytes, check=context.check)
        environment.validate(hash_toolchain=True, fresh=True, check=context.check)
        digest = sha256()
        for block in chunks(runtime_inventory.rows(), b'MIDASUSR\x01'):
            digest.update(block)
        return evidence(dict(format='midas-leaneval-comparator-v1', environment=environment.id,
            archive=archive.id, runtime=digest.hexdigest()),
            ((b'O', result.stdout.data), (b'E', result.stderr.data)), config.export.evidence_bytes)
    return operate(execute, limits=replace(config.export.limits(comparator=True),
        wall_ms=min(config.export.comparator_timeout_ms, config.export.deadline_ms)), cancellation=cancellation)
