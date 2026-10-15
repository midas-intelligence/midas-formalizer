"""Freeze, relocate, verify and expose packages using the reserved executor."""
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import ctypes
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile
import threading
from types import MappingProxyType

from ..domain.encoding import canonical, load_json
from ..executor import LaunchSpec, Mount, Outcome
from .inventory import (EnvironmentError, File, Inventory, MAX_BYTES, MAX_INVENTORY,
                        MAX_ENTRIES, absolute, chunks, copy_file, file_hash, open_root, relative,
                        safe_read, scan, sync_directory)
from .manifest import (Manifest, Settings, check_version, combine, label, load_manifest,
                       logical, subset, version_number)


def output(result):
    if result.outcome != Outcome.COMPLETED or result.returncode != 0:
        raise EnvironmentError('Isolated command failed: ' + result.outcome.value + ': ' +
                               (result.stderr.data + result.stdout.data)[-4096:].decode(errors='replace'))
    return result.stdout.data


@dataclass(frozen=True)
class Bootstrap:
    """Trusted host inspection only. Compiler jobs use frozen package recipes.

    The Linux host/installed launcher are the bootstrap trust boundary, as in v2.
    Probes run without network, host writes, inherited variables or host fallback.
    """
    launcher: str
    launcher_sha256: str
    mounts: tuple[Mount, ...]

    @classmethod
    def installed(cls, launcher='/usr/bin/bwrap'):
        launcher = str(absolute(launcher))
        roots = tuple(Mount(str(Path(p).resolve(strict=True)), p)
                      for p in ('/usr', '/lib', '/lib64', '/bin') if Path(p).exists())
        return cls(launcher, file_hash(launcher, maximum=16 * 1024**2), roots)

    def run(self, context, argv, *, mounts=(), variables=()):
        recipe = LaunchSpec(tuple(argv), (*self.mounts, *mounts), self.launcher,
                            self.launcher_sha256,
                            (('PATH', '/usr/bin:/bin'), ('HOME', '/tmp'), ('LC_ALL', 'C'), *variables),
                            cwd='/tmp')
        return output(context.run_process(recipe))


@dataclass(frozen=True, slots=True)
class PackageConfig:
    version: str
    toolchain: Path
    roots: tuple[tuple[str, Path], ...]
    project_inputs: tuple[str, ...]
    search_paths: tuple[str, ...]
    max_bytes: int = 16 * 1024**3
    preflight_imports: tuple[str, ...] = ()

    def __post_init__(self):
        version_number(self.version)
        if (type(self.roots) is not tuple or not 1 <= len(self.roots) <= 32
                or any(type(pair) is not tuple or len(pair) != 2 for pair in self.roots)):
            raise EnvironmentError('Expected at most 32 explicit package roots')
        names = [label(name) for name, _ in self.roots]
        if len(set(names)) != len(names) or 'project' not in names or {'toolchain', 'runtime'} & set(names):
            raise EnvironmentError('Supply project/dependency roots; toolchain/runtime are separate')
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_BYTES:
            raise EnvironmentError('Invalid package byte bound')
        from ..source.context import module_name
        if type(self.preflight_imports) is not tuple or len(self.preflight_imports) > 10000:
            raise EnvironmentError('Expected bounded preflight imports')
        for name in self.preflight_imports:
            module_name(name)


@dataclass(frozen=True, slots=True)
class AuthorizedInput:
    path: str
    source: Path
    sha256: str
    byte_count: int

    def __post_init__(self):
        relative(self.path)
        item = File(self.path, self.byte_count, False, self.sha256)
        if not item.sha256:
            raise EnvironmentError('Generated input requires an exact hash')


@dataclass(frozen=True, slots=True)
class SandboxInputs:
    environment_id: str
    directory: Path
    inventory: Inventory


_verified = OrderedDict()
_lock = threading.Lock()


def reset_admissions():
    global _verified, _lock
    _verified, _lock = OrderedDict(), threading.Lock()


os.register_at_fork(after_in_child=reset_admissions)


def _hash_policy(value):
    if value is None:
        configured = os.environ.get('MIDAS_VERIFY_TOOLCHAIN', '0')
        if configured not in ('0', '1'):
            raise EnvironmentError('MIDAS_VERIFY_TOOLCHAIN must be 0 or 1')
        return configured == '1'
    if type(value) is not bool:
        raise EnvironmentError('Toolchain content verification must be boolean')
    return value


def _metadata(inventory):
    return (inventory.directories, tuple((f.path, f.size, f.executable) for f in inventory.files))


def _check_root(path, expected, *, content=True, links=False, check=lambda: None):
    maximum = sum(f.size for f in expected.files)
    actual, before = scan(path, hash_content=content, links=links, maximum=maximum, check=check)
    _, after = scan(path, hash_content=False, links=links, maximum=maximum, check=check)
    if before != after or (actual != expected if content else _metadata(actual) != _metadata(expected)):
        raise EnvironmentError('Frozen input differs from saved inventory: ' + str(path))


def _publish(stage, destination):
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1):
        raise EnvironmentError('Cannot publish package without replacing destination: ' + os.strerror(ctypes.get_errno()))
    sync_directory(destination.parent)


def _locator(directory, toolchain):
    payload = canonical({'toolchain': str(toolchain)})
    with (directory / 'location.json').open('xb') as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def _runtime(bootstrap, context, toolchain):
    files, loader = {}, None
    for tool in ('lean', 'leanc', 'clang', 'ld.lld'):
        text = bootstrap.run(context, ('/usr/bin/ldd', '/toolchain/bin/' + tool),
                             mounts=(Mount(str(toolchain), '/toolchain'),)).decode()
        if 'not found' in text:
            raise EnvironmentError('Missing installed compiler runtime')
        current_loader = None
        for line in text.splitlines():
            match = re.search(r'(?:=>\s+)?(/\S+)\s+\(0x[0-9a-f]+\)', line)
            if match is None:
                continue
            path = Path(match[1])
            if path.is_relative_to('/toolchain'):
                continue
            path = path.resolve(strict=True)
            if path.name in files and files[path.name] != path:
                raise EnvironmentError('Runtime basenames collide')
            files[path.name] = path
            if '=>' not in line:
                current_loader = path
        if current_loader is None or loader is not None and loader != current_loader:
            raise EnvironmentError('Missing or inconsistent dynamic loader')
        loader = current_loader
    return loader, tuple(files[name] for name in sorted(files))


class Environment:
    """The sole owner of the manifest and physical input roots for one environment."""

    def __init__(self, directory, manifest, toolchain, launcher):
        self.directory = absolute(directory)
        self.manifest = manifest
        self.toolchain = absolute(toolchain)
        self.launcher = str(absolute(launcher))
        self.roots = MappingProxyType({name: self.toolchain if name == 'toolchain' else self.directory / name
                                       for name in manifest.roots})
        self._inventories = {name: subset(manifest.inventory, name) for name in manifest.roots}
        digest = sha256()
        for block in chunks(manifest.toolchain.rows(), b'TOOLCHAIN\x01'):
            digest.update(block)
        self._toolchain_key = digest.digest()
        self._admitted = None

    @property
    def id(self):
        return self.manifest.id

    @classmethod
    def prepare(cls, destination, config, context, *, bootstrap=None, _before_publish=None):
        """Freeze caller-prepared roots; publish only after isolated version/source checks."""
        if not isinstance(config, PackageConfig):
            raise EnvironmentError('Expected package configuration')
        context.check()
        destination = absolute(destination)
        if destination.exists() or destination.is_symlink():
            raise EnvironmentError('Package destination exists')
        toolchain = absolute(config.toolchain)
        sources = {name: absolute(path) for name, path in config.roots}
        all_roots = [toolchain, *sources.values()]
        for index, path in enumerate(all_roots):
            fd = open_root(path)
            os.close(fd)
            if destination.is_relative_to(path):
                raise EnvironmentError('Package destination is inside an input root')
            if any(path.is_relative_to(p) or p.is_relative_to(path) for p in all_roots[:index]):
                raise EnvironmentError('Package roots overlap')
        bootstrap = bootstrap or Bootstrap.installed()
        loader, runtime = _runtime(bootstrap, context, toolchain)
        settings = Settings(version_number(config.version), 'linux-' + platform.machine(),
                            'ELF-' + platform.machine(), config.project_inputs, config.search_paths,
                            'runtime/' + loader.name)
        tool_inventory, observed = scan(toolchain, links=True, check=context.check)
        with tempfile.TemporaryDirectory(prefix='.midas-package-', dir=destination.parent) as temporary:
            stage = Path(temporary) / 'package'
            stage.mkdir()
            inventories, observations, remaining = {}, {}, config.max_bytes
            entries_left = MAX_ENTRIES - len(tool_inventory.files) - len(tool_inventory.directories) - 1
            for name, source in sources.items():
                context.check()
                # Scan metadata then hash while copying, avoiding two full content reads.
                inventory, before = scan(source, hash_content=False, maximum=remaining,
                                         max_entries=entries_left, check=context.check)
                entries_left -= len(inventory.files) + len(inventory.directories)
                observations[name] = before
                target = stage / name
                target.mkdir()
                for path in inventory.directories:
                    (target / path).mkdir(parents=True, exist_ok=True)
                files = tuple(copy_file(source / item.path, target / item.path, item, check=context.check)
                              for item in inventory.files)
                remaining -= sum(f.size for f in files)
                inventories[name] = Inventory(inventory.directories, files)
            target = stage / 'runtime'
            target.mkdir()
            files = []
            for path in runtime:
                info = path.stat()
                if info.st_size > remaining:
                    raise EnvironmentError('Frozen runtime exceeds remaining package allowance')
                item = File(path.name, info.st_size, bool(info.st_mode & 0o111), '')
                files.append(copy_file(path, target / path.name, item, check=context.check))
                remaining -= info.st_size
            inventories['runtime'] = Inventory(('.',), tuple(sorted(files, key=lambda f: f.path)))
            inventories['toolchain'] = tool_inventory
            for name, source in sources.items():
                _, after = scan(source, hash_content=False, maximum=config.max_bytes, check=context.check)
                if observations[name] != after:
                    raise EnvironmentError('Source changed while freezing package')
            _, after = scan(toolchain, hash_content=False, links=True, check=context.check)
            if observed != after:
                raise EnvironmentError('Toolchain changed while freezing package')
            manifest = Manifest(settings, combine(inventories), bootstrap.launcher_sha256)
            environment = cls(stage, manifest, toolchain, bootstrap.launcher)
            report = output(context.run_process(environment.launch(('--version',)))).decode()
            check_version(report, settings.version)
            checks = [(b'version', report.encode())]
            for source in settings.project_inputs:
                if source.endswith('.lean'):
                    checked = output(context.run_process(environment.launch(('--root=/project', '/' + source))))
                    checks.append((source.encode(), checked))
            if config.preflight_imports:
                with tempfile.TemporaryDirectory(prefix='imports-', dir=context.workspace) as temp:
                    source = Path(temp) / 'Imports.lean'
                    source.write_text(''.join('import ' + name + '\n' for name in config.preflight_imports))
                    inputs = environment.materialize((AuthorizedInput('Imports.lean', source,
                        file_hash(source), source.stat().st_size),), Path(temp) / 'inputs', check=context.check)
                    checks.append((b'imports', output(context.run_process(environment.launch(('/inputs/Imports.lean',), inputs=inputs)))))
            environment.validate(hash_toolchain=True, fresh=True, check=context.check)
            if _before_publish is not None:
                _before_publish(environment)
            with (stage / 'environment.bin').open('xb') as target:
                manifest.save(target)
                target.flush()
                os.fsync(target.fileno())
            with (stage / 'preflight.bin').open('xb') as target:
                for block in chunks(checks, b'MIDASPREF\x01'):
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            _locator(stage, toolchain)
            # Account for metadata as well as copied payloads before publication.
            extra = sum((stage / p).stat().st_size for p in ('environment.bin', 'preflight.bin', 'location.json'))
            if extra > remaining:
                raise EnvironmentError('Package metadata exceeds remaining byte allowance')
            for name, inventory in inventories.items():
                if name != 'toolchain':
                    for path in sorted(inventory.directories, key=lambda p: p.count('/'), reverse=True):
                        sync_directory(stage / name / path)
            sync_directory(stage)
            _publish(stage, destination)
        return cls(destination, manifest, toolchain, bootstrap.launcher)

    @classmethod
    def restore(cls, directory, *, manifest=None, expected_id=None, toolchain=None,
                launcher='/usr/bin/bwrap', hash_toolchain=None, check=lambda: None):
        directory = absolute(directory)
        if manifest is None:
            manifest = load_manifest(safe_read(directory / 'environment.bin', MAX_INVENTORY), expected_id=expected_id)
        elif not isinstance(manifest, Manifest):
            manifest = load_manifest(manifest, expected_id=expected_id)
        if expected_id is not None and manifest.id != expected_id:
            raise EnvironmentError('Saved environment identity mismatch')
        if toolchain is None:
            toolchain = os.environ.get('MIDAS_SHARED_TOOLCHAIN')
        if toolchain is None:
            locator = load_json(safe_read(directory / 'location.json', 8192))
            if type(locator) is not dict or set(locator) != {'toolchain'} or not isinstance(locator['toolchain'], str) or not Path(locator['toolchain']).is_absolute():
                raise EnvironmentError('Invalid shared toolchain locator')
            toolchain = locator['toolchain']
        if (directory / 'toolchain').exists() or (directory / 'toolchain').is_symlink():
            raise EnvironmentError('Package has both local and shared toolchains')
        environment = cls(directory, manifest, toolchain, launcher)
        environment.validate(hash_toolchain=hash_toolchain, check=check)
        return environment

    def validate(self, *, hash_toolchain=None, fresh=False, check=lambda: None):
        """Exact package bytes; process-local installed toolchain admission by policy."""
        content = _hash_policy(hash_toolchain)
        if file_hash(self.launcher, maximum=16 * 1024**2, check=check) != self.manifest.launcher_sha256:
            raise EnvironmentError('Installed launcher differs from saved environment')
        key = (self.toolchain, self._toolchain_key, content)
        with _lock:
            admitted = key in _verified and not fresh
        if not admitted:
            _check_root(self.toolchain, self.manifest.toolchain, content=content, links=True, check=check)
            with _lock:
                _verified[key] = True
                _verified.move_to_end(key)
                if len(_verified) > 32:
                    _verified.popitem(last=False)
        for name, path in self.roots.items():
            if name != 'toolchain':
                _check_root(path, self._inventories[name], check=check)
        self._admitted = (os.getpid(), content)
        return self.id

    def _admit(self, check=lambda: None):
        # A service handle owns stable frozen roots for its lifetime, like v2's
        # EnvironmentVerifier. Explicit validate() always rechecks package bytes.
        policy = _hash_policy(None)
        if self._admitted is None or self._admitted[0] != os.getpid() or policy and not self._admitted[1]:
            self.validate(check=check)

    def selection_inputs(self):
        return self.manifest.selection_inputs()

    def materialize(self, inputs, workspace, *, check=lambda: None):
        if type(inputs) is not tuple or len(inputs) > 10000 or any(not isinstance(i, AuthorizedInput) for i in inputs):
            raise EnvironmentError('Expected bounded authorized input records')
        paths = {item.path for item in inputs}
        if len(paths) != len(inputs) or sum(item.byte_count for item in inputs) > MAX_INVENTORY:
            raise EnvironmentError('Duplicate or oversized generated inputs')
        for item in inputs:
            if any(str(p) in paths for p in Path(item.path).parents):
                raise EnvironmentError('Conflicting generated input paths')
            stem = item.path
            for suffix in ('.olean.private', '.olean.server', '.ir.sig', '.olean', '.ilean', '.lean', '.ir'):
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    break
            module = stem.replace('/', '.')
            if module in self.manifest.modules:
                raise EnvironmentError('Generated input shadows a frozen module: ' + module)
        workspace = absolute(workspace)
        self._private(workspace)
        self._admit(check)
        workspace.mkdir()  # Exclusively owned new workspace; never overlay existing files.
        try:
            files = []
            for item in inputs:
                expected = File(item.path, item.byte_count, False, item.sha256)
                files.append(copy_file(item.source, workspace / item.path, expected, check=check))
            inventory, _ = scan(workspace, check=check)
            return SandboxInputs(self.id, workspace, inventory)
        except BaseException:
            shutil.rmtree(workspace)
            raise

    def _private(self, path):
        if any(path.is_relative_to(root) or root.is_relative_to(path) for root in self.roots.values()):
            raise EnvironmentError('Workspace overlaps a frozen root')
        fd = open_root(path.parent)
        os.close(fd)

    def launch(self, arguments, *, inputs=None, build=None, executable=None):
        """Lower admitted files to executor recipes; no compiler execution or scheduling."""
        self._admit()
        spec = self.manifest.settings
        executable = executable or spec.lean_executable
        item = self.manifest.inventory.by_path.get(logical(executable))
        if item is None or not item.executable:
            raise EnvironmentError('Executable is outside the admitted inventory')
        mounts = [Mount(str(path), '/' + name) for name, path in self.roots.items()]
        search = ['/' + path for path in spec.search_paths]
        if inputs is not None:
            if not isinstance(inputs, SandboxInputs) or inputs.environment_id != self.id:
                raise EnvironmentError('Generated input environment mismatch')
            self._private(inputs.directory)
            _check_root(inputs.directory, inputs.inventory)
            mounts.append(Mount(str(inputs.directory), '/inputs'))
            search.insert(0, '/inputs')
        if build is not None:
            build = absolute(build)
            self._private(build)
            fd = open_root(build)
            os.close(fd)
            if inputs is not None and (build.is_relative_to(inputs.directory) or inputs.directory.is_relative_to(build)):
                raise EnvironmentError('Writable output overlaps generated inputs')
            mounts.append(Mount(str(build), '/build', True))
        if executable != spec.lean_executable and (spec.options or spec.plugins):
            raise EnvironmentError('Non-Lean executable cannot receive Lean options/plugins')
        libraries = ':'.join('/' + path for path in spec.runtime_paths)
        variables = (('PATH', '/toolchain/bin'), ('HOME', '/tmp'), ('TMPDIR', '/tmp'), ('LC_ALL', 'C'),
                     ('TZ', 'UTC'), ('LEAN_SYSROOT', '/toolchain'), ('LEAN_PATH', ':'.join(search)),
                     ('LD_LIBRARY_PATH', libraries), *spec.variables)
        argv = ('/' + spec.runtime_loader, '--inhibit-cache', '--library-path', libraries, '/' + executable,
                *('-D' + k + '=' + v for k, v in spec.options),
                *('--plugin=/' + p for p in spec.plugins), *arguments)
        if executable == 'toolchain/bin/leanc':
            if build is None:
                raise EnvironmentError('Native compilation requires an explicit output workspace')
            interpreter = {'x86_64': '/lib64/ld-linux-x86-64.so.2',
                           'aarch64': '/lib/ld-linux-aarch64.so.1'}.get(platform.machine())
            if interpreter is None:
                raise EnvironmentError('Unsupported native build architecture')
            mounts.append(Mount(str(self.directory / spec.runtime_loader), interpreter))
        return LaunchSpec(tuple(argv), tuple(mounts), self.launcher, self.manifest.launcher_sha256, variables)
