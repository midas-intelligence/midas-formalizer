"""Source-bound native builds and explicit executor recipes; no hidden runner."""
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import fcntl
import os
import shutil
import tempfile
import time

from ..domain import encoding as codec
from ..environment import Bootstrap, EnvironmentError, file_hash, safe_read, scan
from ..environment.package import output
from ..executor import (ImportInput, ImportRecipe, LINK_FLAGS, Mount,
                        import_runtime_source, import_worker_platform_supported)

RESOURCE = Path(__file__).with_name('lean')
MAX_METADATA = 16384


def sources(*, retained=False, native=True):
    def read(name):
        return (RESOURCE / name).read_text().removeprefix('import Lean\n')
    header = ('module\npublic import Lean\npublic meta import Lean\n'
              'import all Lean.Environment\nimport all Lean.CompactedRegion\npublic section\n')
    common = header + '\n'.join(read(name) for name in
        ('Identity.lean', 'CachedImports.lean', 'Probe.lean', 'CertificateCodec.lean'))
    check = read('Check.lean').replace('\ndef main (', '\ndef checkMain (', 1)
    native_source = common + '\n' + check + '\n' + read('NativeMain.lean')
    extractor = header + read('Identity.lean') + read('CertificateCodec.lean') + read('Extract.lean')
    result = {'Extract.lean': extractor.encode()}
    if native:
        result['Native.lean'] = native_source.encode()
    else:
        result['MidasCheck.lean'] = (common.replace('public section\n', 'public meta section\n', 1)
                                   + '\n' + read('Check.lean')).encode()
        result['Check.lean'] = b'import MidasCheck\nrun_elab Midas.Check.launchEnvironment.set (some (\xe2\x86\x90 Lean.getEnv))\n'
    if retained:
        worker = common + '\n' + check + '\n' + read('isolated_import_main.lean')
        begin = worker.index('open Elab in\ndef restoreImports (')
        end = worker.index('\nend Midas.Check', begin)
        worker = worker[:begin] + read('incremental_import_restore.lean') + worker[end:]
        result.update({'Worker.lean': worker.encode(), 'isolation.c': import_runtime_source()})
    return result


def _inventory(directory):
    inventory, _ = scan(directory, maximum=512 * 1024**2)
    return [[f.path, f.size, f.executable, f.sha256] for f in inventory.files
            if f.path != 'runtime.bin']


@dataclass(frozen=True, slots=True)
class Runtime:
    directory: Path
    id: str
    environment_id: str
    retained: bool
    files: tuple[tuple, ...]
    native: bool = True

    def validate(self):
        if tuple(map(tuple, _inventory(self.directory))) != self.files:
            raise EnvironmentError('Checker runtime changed')

    def launch(self, environment, memory_mib, *, inputs=None, retained=False):
        if environment.id != self.environment_id or retained and not self.retained:
            raise EnvironmentError('Checker runtime differs from environment or mode')
        self.validate()
        if not self.native:
            base = environment.launch(('--run', '/checker/Check.lean', '/inputs/Packet.bin'), inputs=inputs)
            return replace(base, variables=tuple((k, '/checker:' + v if k == 'LEAN_PATH' else v)
                           for k, v in base.variables if k != 'MIDAS_EVIDENCE_PATH'),
                           mounts=(*base.mounts, Mount(str(self.directory), '/checker')))
        base = environment.launch((), inputs=inputs)
        # Environment owns loader and root admission; checker owns this executable.
        executable = '/checker/worker' if retained else '/checker/native'
        args = ('--memory=' + str(memory_mib), 'cached', '--stream') if retained else (
            '--memory=' + str(memory_mib), '/inputs/Packet.bin')
        variables = tuple((k, '/midas-inputs:' + v if retained and k == 'LEAN_PATH' else v)
                          for k, v in base.variables)
        variables = tuple((k, v) for k, v in variables if k not in ('LEAN_MAIN_USE_THREAD', 'MIDAS_EVIDENCE_PATH')) + (('LEAN_MAIN_USE_THREAD', '0'),)
        if retained:
            variables += (('MIDAS_EVIDENCE_PATH', '/midas-inputs/.midas-evidence'),)
        return replace(base, argv=(*base.argv[:4], executable, *args), variables=variables,
                       mounts=(*base.mounts, Mount(str(self.directory), '/checker')))

    def recipe(self, environment, memory_mib, files):
        return ImportRecipe(self.launch(environment, memory_mib, retained=True),
            environment.id, self.id, 'Packet.bin', tuple(
                ImportInput(path.name, file_hash(path), path.stat().st_size) for path in files))


def prepare_runtime(environment, context, cache):
    """Compile under an existing reservation, atomically publish, then admit hashes."""
    settings = environment.manifest.settings
    if 'MidasCheck' in environment.manifest.modules:
        raise EnvironmentError('Frozen module collides with private checker module MidasCheck')
    native = not settings.options and not settings.plugins
    retained = native and settings.version == '4.35.0-rc3' and import_worker_platform_supported()
    compiler = Path(shutil.which('cc')).resolve() if retained and shutil.which('cc') else None
    if retained and compiler is None:
        retained = False
    source = sources(retained=retained, native=native)
    digest = sha256(b'MidasCheckerRuntime\x01')
    codec.hash_field(digest, environment.id.encode())
    codec.hash_field(digest, Path(__file__).read_bytes())
    if retained:
        codec.hash_field(digest, file_hash(compiler).encode())
    for name, data in sorted(source.items()):
        codec.hash_field(digest, name.encode())
        codec.hash_field(digest, data)
    key = digest.hexdigest()
    cache = Path(cache).absolute()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cache.is_symlink() or cache.stat().st_uid != os.getuid() or cache.stat().st_mode & 0o077:
        raise EnvironmentError('Checker cache must be private and owned by this user')
    destination = cache / key
    fd = os.open(cache / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as lock:
        while True:
            context.check()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.02)
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix='build-', dir=context.workspace) as temporary:
                root = Path(temporary)
                src, build = root / 'source', root / 'output'
                src.mkdir()
                build.mkdir()
                for name, data in source.items():
                    (src / name).write_bytes(data)
                def launch(arguments, *, executable=None):
                    base = environment.launch(arguments, build=build, executable=executable,
                        max_heartbeat=context.limits.max_heartbeat if executable is None else None)
                    return replace(base, mounts=(*base.mounts, Mount(str(src), '/checker-source')))
                if not native:
                    output(context.run_process(launch(('--root=/checker-source', '-o', '/build/MidasCheck.olean',
                        '/checker-source/MidasCheck.lean'))))
                for module, binary in ([('Native', 'native'), *([('Worker', 'worker')] if retained else [])] if native else []):
                    output(context.run_process(launch(('--root=/checker-source',
                        '--c=/build/' + module + '.c', '/checker-source/' + module + '.lean'))))
                    if module == 'Worker':
                        Bootstrap.installed(environment.launcher).run(context,
                            (str(compiler), '-O2', '-fPIC', '-Wall', '-Wextra', '-Werror',
                             '-I/toolchain/include', '-c', '/source/isolation.c', '-o', '/output/isolation.o'),
                            mounts=(Mount(str(environment.roots['toolchain']), '/toolchain'),
                                    Mount(str(src), '/source'), Mount(str(build), '/output', True)))
                    extra = ('/build/isolation.o', *LINK_FLAGS) if module == 'Worker' else ()
                    output(context.run_process(launch(('-O2', '-rdynamic', '-o',
                        '/build/' + binary, '/build/' + module + '.c', *extra),
                        executable='toolchain/bin/leanc')))
                with tempfile.TemporaryDirectory(prefix='publish-', dir=cache) as staged:
                    stage = Path(staged) / 'runtime'
                    stage.mkdir()
                    for name, data in source.items():
                        (stage / name).write_bytes(data)
                    binaries = ('native', 'worker') if retained else ('native',) if native else tuple(
                        p.name for p in build.iterdir() if p.name.startswith('MidasCheck.'))
                    for name in binaries:
                        shutil.copyfile(build / name, stage / name)
                        (stage / name).chmod(0o755 if native else 0o644)
                    manifest = {'id': key, 'files': _inventory(stage)}
                    (stage / 'runtime.bin').write_bytes(b''.join(codec.record_parts(manifest, limit=MAX_METADATA)))
                    stage.rename(destination)
        saved = codec.load_record(safe_read(destination / 'runtime.bin', MAX_METADATA), limit=MAX_METADATA)
        codec.fields(saved, ('id', 'files'))
        if saved['id'] != key or saved['files'] != _inventory(destination):
            raise EnvironmentError('Invalid checker runtime cache')
        for name, data in source.items():
            if safe_read(destination / name, len(data)) != data:
                raise EnvironmentError('Checker runtime source mismatch')
    return Runtime(destination, key, environment.id, retained, tuple(map(tuple, saved['files'])), native)
