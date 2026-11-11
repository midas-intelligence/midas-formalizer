"""Pinned Git closure, source/module inventories and isolated source replay.

Metadata admission is not proof authority. Replay admits only the explicitly
requested source closure; native plugins and Lake freshness belong to the
profile's compiler/export validation, never to an inventory cache hit.
"""
from collections import deque
from dataclasses import dataclass
from functools import cached_property
from graphlib import TopologicalSorter, CycleError
from hashlib import sha1, sha256
import os
from pathlib import Path
import re
import tempfile

from ..domain.encoding import load_metadata
from ..executor import Mount
from ..source.context import module_name
from .inventory import (BLOCK, EnvironmentError, File, Inventory, MAX_BYTES, _open_file,
                        chunks, copy_file, file_hash, relative, safe_read, scan, stamp)
from .manifest import label
from .assets import source_assets
from .options import module_setup, option_rows
from .package import Bootstrap, output

SUFFIXES = ('.olean', '.olean.private', '.olean.server', '.ir', '.ir.sig', '.ilean')


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    path: Path
    module_paths: tuple[str, ...]
    source_roots: tuple[str, ...] = ('.',)

    def __post_init__(self):
        label(self.name)
        if self.name in ('project', 'toolchain', 'runtime', 'checker'):
            raise EnvironmentError('Reserved dependency name')
        for values in (self.module_paths, self.source_roots):
            if type(values) is not tuple or not 1 <= len(values) <= 32 or len(set(values)) != len(values):
                raise EnvironmentError('Expected distinct immutable module/source roots')
            for path in values:
                relative(path, root=True)


@dataclass(frozen=True, slots=True)
class Pin:
    name: str
    revision: str
    url: str
    config: str

    def __post_init__(self):
        label(self.name)
        if (type(self.revision) is not str or not re.fullmatch('[0-9a-f]{40}', self.revision)
                or type(self.url) is not str or not self.url or len(self.url.encode()) > 4096):
            raise EnvironmentError('Expected pinned Git dependency')
        if self.config not in ('lakefile.lean', 'lakefile.toml'):
            raise EnvironmentError('Unsupported Lake configuration path')


def lake_name(value):
    """Map Lake's quoted single-component names to admitted root labels."""
    if isinstance(value, str) and value.startswith('«') and value.endswith('»'):
        value = value[1:-1]
    return label(value)


def lake_pins(payload):
    """Lake lockfiles are small metadata: reject above 16 KiB before JSON parsing."""
    value = load_metadata(payload)
    if (type(value) is not dict or value.get('packagesDir') != '.lake/packages'
            or type(value.get('packages')) is not list or len(value['packages']) > 32):
        raise EnvironmentError('Unsupported Lake lock inventory')
    result = {}
    for item in value['packages']:
        if (type(item) is not dict or item.get('type') != 'git' or item.get('subDir') is not None
                or item.get('manifestFile', 'lake-manifest.json') != 'lake-manifest.json'):
            raise EnvironmentError('Unsupported dependency source or metadata paths')
        pin = Pin(lake_name(item.get('name')), item.get('rev'), item.get('url'), item.get('configFile'))
        if pin.name in result:
            raise EnvironmentError('Duplicate Lake dependency')
        result[pin.name] = pin
    return result


class GitReader:
    def __init__(self, root, context, *, bootstrap=None):
        self.root = Path(root).absolute()
        self.context = context
        self.bootstrap = bootstrap or Bootstrap.installed()

    def run(self, *arguments):
        # No fetch, checkout, shell, hooks or inherited user configuration.
        return self.bootstrap.run(self.context, ('/usr/bin/git', '-C', '/repo', *arguments),
            mounts=(Mount(str(self.root), '/repo'),), variables=(
                ('GIT_CONFIG_NOSYSTEM', '1'), ('GIT_CONFIG_GLOBAL', '/dev/null'),
                ('GIT_NO_REPLACE_OBJECTS', '1'), ('GIT_NO_LAZY_FETCH', '1'),
                ('GIT_OPTIONAL_LOCKS', '0'), ('GIT_TERMINAL_PROMPT', '0'), ('GIT_ALLOW_PROTOCOL', '')))

    def head(self):
        return self.run('rev-parse', '--verify', 'HEAD').decode().strip()

    def listing(self, revision, *paths, suffixes=()):
        if re.fullmatch('[0-9a-f]{40}', revision) is None:
            raise EnvironmentError('Invalid pinned Git revision')
        result = {}
        for record in self.run('ls-tree', '-r', '-z', '--full-tree', revision, '--', *paths).split(b'\0'):
            if not record:
                continue
            try:
                header, path = record.split(b'\t', 1)
                mode, kind, oid = header.split()
                path = relative(path.decode())
            except (ValueError, UnicodeError) as error:
                raise EnvironmentError('Invalid Git tree record') from error
            if suffixes and not path.endswith(suffixes):
                continue
            if path in result or mode not in (b'100644', b'100755') or kind != b'blob' or not re.fullmatch(b'[0-9a-f]{40}', oid):
                raise EnvironmentError('Unsafe Git tree entry, link or submodule')
            result[path] = (mode == b'100755', oid.decode())
            if len(result) > 500000:
                raise EnvironmentError('Git inventory exceeds entry bound')
        return result

    def committed(self, revision, path, *, maximum=16384):
        listing = self.listing(revision, path)
        if set(listing) != {path} or listing[path][0]:
            raise EnvironmentError('Missing or unsafe pinned metadata')
        value = self.run('cat-file', 'blob', listing[path][1])
        if len(value) > maximum or safe_read(self.root / path, maximum) != value:
            raise EnvironmentError('Pinned Git metadata differs from working tree')
        return value


@dataclass(frozen=True, slots=True)
class Closure:
    pins: tuple[Pin, ...]
    metadata: tuple[tuple[str, str, str], ...]
    id: str
    mappings: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]


def resolve_closure(lock, required, dependencies, context, *, bootstrap=None):
    locked = lake_pins(lock)
    if type(dependencies) is not tuple or len(dependencies) > 32:
        raise EnvironmentError('Expected bounded dependency mappings')
    mappings = {item.name: item for item in dependencies}
    if len(mappings) != len(dependencies):
        raise EnvironmentError('Duplicate dependency mappings')
    pending, admitted, readers, observations = deque(required), {}, {}, []
    while pending:
        context.check()
        name = pending.popleft()
        if name in admitted:
            continue
        if name not in locked or name not in mappings:
            raise EnvironmentError('Missing prepared dependency: ' + str(name))
        pin = locked[name]
        reader = GitReader(mappings[name].path, context, bootstrap=bootstrap)
        readers[name] = reader
        if reader.head() != pin.revision:
            raise EnvironmentError('Prepared dependency HEAD differs from pin')
        metadata = {}
        for path in ('lake-manifest.json', pin.config, 'lean-toolchain'):
            maximum = 16384 if path == 'lake-manifest.json' else 8 * 1024**2
            metadata[path] = reader.committed(pin.revision, path, maximum=maximum)
            observations.append((name, path, metadata[path]))
        if not metadata['lean-toolchain'].strip():
            raise EnvironmentError('Missing dependency development toolchain pin')
        # Lake materializes the entire workspace from the root lock. A dependency's
        # development lock can pin older revisions; retain it as provenance, but
        # resolve its names to the root pins and verify those actual checkouts.
        for child in lake_pins(metadata['lake-manifest.json']):
            if child not in locked:
                raise EnvironmentError('Transitive dependency absent from root lock')
            pending.append(child)
        admitted[name] = pin
    if set(mappings) != set(admitted):
        raise EnvironmentError('Dependency mapping is outside the requested closure')
    for name, path, value in observations:
        if safe_read(mappings[name].path / path, len(value)) != value:
            raise EnvironmentError('Dependency metadata changed during admission')
    for name, reader in readers.items():
        if reader.head() != admitted[name].revision:
            raise EnvironmentError('Dependency HEAD changed during admission')
    pins = tuple(admitted[name] for name in sorted(admitted))
    metadata = tuple(sorted((name, path, sha256(value).hexdigest()) for name, path, value in observations))
    rows = [(b'P', p.name.encode(), p.revision.encode(), p.url.encode(), p.config.encode()) for p in pins]
    rows.extend((b'M', name.encode(), path.encode(), bytes.fromhex(digest)) for name, path, digest in metadata)
    for item in dependencies:
        rows.extend((tag, item.name.encode(), path.encode()) for tag, paths in
                    ((b'C', item.module_paths), (b'S', item.source_roots)) for path in paths)
    digest = sha256()
    for block in chunks(rows, b'MIDASCLOS\x01'):
        digest.update(block)
    return Closure(pins, metadata, digest.hexdigest(),
                   tuple((d.name, d.module_paths, d.source_roots) for d in dependencies))


def _git_hash(path, expected, check):
    fd, parent = _open_file(path)
    try:
        before = stamp(os.fstat(fd))
        if before[3] != expected.size:
            raise EnvironmentError('Source size changed')
        digest = sha1(b'blob ' + str(expected.size).encode() + b'\0', usedforsecurity=False)
        content, count = sha256(), 0
        while block := os.read(fd, BLOCK):
            check()
            count += len(block)
            if count > expected.size:
                raise EnvironmentError('Source grew during admission')
            digest.update(block)
            content.update(block)
        if (count != expected.size or before != stamp(os.fstat(fd)) or
                before != stamp(os.stat(Path(path).name, dir_fd=parent, follow_symlinks=False))):
            raise EnvironmentError('Source changed during admission')
        return digest.hexdigest(), content.hexdigest()
    finally:
        os.close(fd)
        os.close(parent)


@dataclass(frozen=True, slots=True)
class ModulePin:
    name: str
    package: str
    source: File
    compiled: tuple[File, ...]
    module_path: str
    lake_package: str | None = None
    assets: tuple[tuple[str, File], ...] = ()
    lean_options: tuple[tuple[str, bool | int | str], ...] = ()


@dataclass(frozen=True)
class Modules:
    closure_id: str
    required: tuple[str, ...]
    modules: tuple[ModulePin, ...]
    # Complete compiled trees (including native assets) are identity-bearing.
    inventories: tuple[tuple[str, str, Inventory], ...]
    sources: tuple[tuple[str, Inventory], ...]

    @cached_property
    def id(self):
        digest = sha256()
        def rows():
            yield (b'C', self.closure_id.encode())
            for name in self.required:
                yield (b'R', name.encode())
            for name, inventory in self.sources:
                yield (b'S', name.encode())
                yield from inventory.rows()
            for name, path, inventory in self.inventories:
                yield (b'I', name.encode(), path.encode())
                yield from inventory.rows()
            for item in self.modules:
                yield (b'M', item.name.encode(), item.package.encode(), item.source.path.encode(), item.module_path.encode(),
                       (item.lake_package or '').encode())
                yield from option_rows(item.lean_options)
                for path, asset in item.assets:
                    yield (b'A', path.encode(), asset.path.encode(), str(asset.size).encode(),
                           str(asset.executable).encode(), bytes.fromhex(asset.sha256))
        for block in chunks(rows(), b'MIDASMOD\x01'):
            digest.update(block)
        return digest.hexdigest()


def inventory_modules(closure, dependencies, context, *, required=(), standard=frozenset(),
                      bootstrap=None, max_bytes=16 * 1024**3, source_only=False):
    if (type(required) is not tuple or len(set(required)) != len(required) or len(required) > 100000
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES):
        raise EnvironmentError('Invalid module inventory request')
    for name in required:
        module_name(name)
    pins = {p.name: p for p in closure.pins}
    if {d.name for d in dependencies} != set(pins):
        raise EnvironmentError('Dependency mappings differ from admitted closure')
    if tuple((d.name, d.module_paths, d.source_roots) for d in dependencies) != closure.mappings:
        raise EnvironmentError('Dependency source/module paths differ from admitted closure')
    roots = {d.name: d.path for d in dependencies}
    def verify_metadata():
        for name, path, digest in closure.metadata:
            if file_hash(roots[name] / path, maximum=8 * 1024**2, check=context.check) != digest:
                raise EnvironmentError('Dependency metadata differs from admitted closure')
    verify_metadata()
    modules, inventories, sources, observations = {}, [], [], []
    remaining = max_bytes
    for dep in dependencies:
        if source_only:
            native, _ = scan(dep.path, hash_content=False, links=True, maximum=remaining, check=context.check,
                             exclude=('.git', '.lake/packages'))
            if any(f.path.endswith(('.so', '.dll', '.dylib')) or '.so.' in f.path for f in native.files):
                raise EnvironmentError('Source-only dependency contains a native plugin')
        reader = GitReader(dep.path, context, bootstrap=bootstrap)
        if reader.head() != pins[dep.name].revision:
            raise EnvironmentError('Dependency HEAD differs from admitted closure')
        listing = reader.listing(pins[dep.name].revision, suffixes=('.lean',))
        assets = {}
        expected = {p: value for p, value in listing.items() if p.endswith('.lean')}
        source, before = scan(dep.path, hash_content=False, exclude=('.git', '.lake'),
                              suffixes=('.lean',), maximum=remaining, check=context.check)
        if set(source.by_path) != set(expected):
            raise EnvironmentError('Missing or unrecorded dependency Lean source')
        pinned = []
        for item in source.files:
            git_hash, digest = _git_hash(dep.path / item.path, item, context.check)
            if expected[item.path] != (item.executable, git_hash):
                raise EnvironmentError('Dependency source differs from committed object')
            pinned.append(File(item.path, item.size, item.executable, digest))
        source = Inventory(source.directories, tuple(pinned))
        remaining -= sum(f.size for f in source.files)
        sources.append((dep.name, source))
        observations.append((dep.path, before, ('.git', '.lake'), ('.lean',)))
        for path in dep.module_paths:
            compiled, before = scan(dep.path / path, maximum=remaining, check=context.check)
            inventories.append((dep.name, path, compiled))
            observations.append((dep.path / path, before, (), ()))
            remaining -= sum(f.size for f in compiled.files)
            if source_only and any(f.path.endswith(('.so', '.dll', '.dylib')) or '.so.' in f.path for f in compiled.files):
                raise EnvironmentError('Source-only dependency contains a native plugin')
            for item in compiled.files:
                if not item.path.endswith('.olean'):
                    continue
                name = item.path[:-6].replace('/', '.')
                module_name(name)
                if name in modules or name in standard or name.split('.')[0] in ('MidasCheck', 'MidasNativeCheck'):
                    raise EnvironmentError('Shadowed dependency module: ' + name)
                relative_source = item.path[:-6] + '.lean'
                candidates = [source.by_path[p] for root in dep.source_roots
                              if (p := (Path(root) / relative_source).as_posix()) in source.by_path]
                if len(candidates) != 1:
                    raise EnvironmentError('Missing or ambiguous source for module: ' + name)
                sidecars = tuple(compiled.by_path[item.path[:-6] + suffix] for suffix in SUFFIXES
                                 if item.path[:-6] + suffix in compiled.by_path)
                included = source_assets(dep.path, candidates[0], name,
                    lambda p: reader.listing(pins[dep.name].revision, p), assets)
                modules[name] = ModulePin(name, dep.name, candidates[0], sidecars, path, dep.name, included)
        remaining -= sum(f.size for f in assets.values())
        if remaining < 0:
            raise EnvironmentError('Source assets exceed inventory byte limit')
        for asset in assets.values():
            if file_hash(dep.path / asset.path, maximum=asset.size, check=context.check) != asset.sha256:
                raise EnvironmentError('Source asset changed during admission')
    if set(required) - modules.keys():
        raise EnvironmentError('Missing required compiled modules')
    for path, before, exclude, suffixes in observations:
        _, after = scan(path, hash_content=False, exclude=exclude, suffixes=suffixes,
                        maximum=max_bytes, check=context.check)
        if before != after:
            raise EnvironmentError('Dependency changed during module admission')
    for dep in dependencies:
        if GitReader(dep.path, context, bootstrap=bootstrap).head() != pins[dep.name].revision:
            raise EnvironmentError('Dependency HEAD changed during module admission')
    verify_metadata()
    return Modules(closure.id, required, tuple(modules[name] for name in sorted(modules)),
                   tuple(inventories), tuple(sources))


@dataclass(frozen=True, slots=True)
class Replay:
    environment_id: str
    modules_id: str
    build_order: tuple[str, ...]
    imports: tuple[tuple[str, tuple[str, ...]], ...]
    options: tuple[str, ...]


def replay_dependencies(environment, modules, dependencies, context, *, options=()):
    """Resolve imports with empty placeholders, then compile from an empty library."""
    if (type(options) is not tuple or len(options) > 64 or
            any(type(o) is not str or not o.startswith('-D') or '=' not in o or '\x00' in o for o in options)):
        raise EnvironmentError('Replay requires explicit Lean -D assignments')
    spec = environment.manifest.settings
    if spec.plugins or spec.options or spec.variables:
        raise EnvironmentError('Source replay does not admit compiler overrides/plugins')
    environment.validate(hash_toolchain=True, fresh=True, check=context.check)
    by_name = {m.name: m for m in modules.modules}
    roots = {d.name: d.path for d in dependencies}
    if not set(modules.required) <= by_name.keys():
        raise EnvironmentError('Missing required replay module')
    standard = {name for name, path in environment.manifest.modules.items() if path.startswith('toolchain/')}
    if standard & by_name.keys():
        raise EnvironmentError('Dependency shadows a toolchain module')
    with tempfile.TemporaryDirectory(prefix='replay-', dir=context.workspace) as temporary:
        workspace = Path(temporary)
        library, source_root = workspace / 'lib', workspace / 'src'
        library.mkdir()
        source_root.mkdir()
        for name in by_name:
            path = library / (name.replace('.', '/') + '.olean')
            relative(path.relative_to(library).as_posix())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'')

        def run(arguments):
            recipe = environment.launch(tuple(arguments), build=workspace, max_heartbeat=context.limits.max_heartbeat,
                                        max_rec_depth=context.limits.max_rec_depth)
            # Prepared dependency objects are excluded from both mounts and search.
            mounts = tuple(m for m in recipe.mounts if m.target in ('/toolchain', '/runtime', '/build'))
            variables = tuple((k, '/build/lib:/toolchain/lib/lean' if k == 'LEAN_PATH' else v) for k, v in recipe.variables)
            from dataclasses import replace
            return output(context.run_process(replace(recipe, mounts=mounts, variables=variables)))

        graph, pending, assets = {}, deque(modules.required), {}
        while pending:
            context.check()
            name = pending.popleft()
            if name in graph:
                continue
            item = by_name[name]
            destination = source_root / (name.replace('.', '/') + '.lean')
            copy_file(roots[item.package] / item.source.path, destination, item.source, check=context.check)
            for path, asset in item.assets:
                if path in assets and assets[path] != (asset.sha256, asset.size):
                    raise EnvironmentError('Conflicting source assets: ' + path)
                if path.endswith('.lean') and path[:-5].replace('/', '.') in by_name:
                    raise EnvironmentError('Source asset shadows a module')
                if path not in assets:
                    copy_file(roots[item.package] / asset.path, source_root / path, asset, check=context.check)
                    assets[path] = (asset.sha256, asset.size)
            imports = set()
            for path in run(('--deps', '/build/src/' + name.replace('.', '/') + '.lean')).decode().splitlines():
                if path.startswith('/build/lib/') and path.endswith('.olean'):
                    child = path[len('/build/lib/'):-6].replace('/', '.')
                    if child not in by_name:
                        raise EnvironmentError('Unrecorded dependency import')
                    imports.add(child)
                elif path.startswith('/toolchain/lib/lean/') and path.endswith('.olean'):
                    if path[len('/toolchain/lib/lean/'):-6].replace('/', '.') not in standard:
                        raise EnvironmentError('Unrecorded toolchain import')
                else:
                    raise EnvironmentError('Unexpected compiler import path')
            graph[name] = imports
            pending.extend(sorted(imports))
        for name in by_name:
            (library / (name.replace('.', '/') + '.olean')).unlink()
        try:
            order = tuple(TopologicalSorter({name: sorted(graph[name]) for name in sorted(graph)}).static_order())
        except CycleError as error:
            raise EnvironmentError('Cyclic dependency imports') from error
        for name in order:
            stem = name.replace('.', '/')
            setup = ()
            if by_name[name].lake_package is not None or by_name[name].lean_options:
                # Lake's package identity affects modern Lean artifacts. Derive only
                # this small setup from admitted names/options: cached setup files can contain
                # large import maps, native plugins and paths outside the replay.
                (workspace / 'module.setup.json').write_bytes(module_setup(
                    name, by_name[name].lake_package, by_name[name].lean_options))
                setup = ('--setup', '/build/module.setup.json')
            run((*options, *setup, '--root=/build/src', '-o', '/build/lib/' + stem + '.olean',
                 '-i', '/build/lib/' + stem + '.ilean', '/build/src/' + stem + '.lean'))
            present = {suffix for suffix in SUFFIXES if (library / (stem + suffix)).exists()}
            expected = {f.path[len(stem):]: f for f in by_name[name].compiled}
            if present != set(expected) or any(file_hash(library / (stem + suffix), maximum=f.size,
                check=context.check) != f.sha256 for suffix, f in expected.items()):
                raise EnvironmentError('Replayed dependency differs from prepared compiled artifacts: ' + name)
        for name in order:
            item = by_name[name]
            for path, asset in item.assets:
                for candidate in (roots[item.package] / asset.path, source_root / path):
                    if file_hash(candidate, maximum=asset.size, check=context.check) != asset.sha256:
                        raise EnvironmentError('Source asset changed during replay')
            for path in (roots[item.package] / item.source.path, source_root / (name.replace('.', '/') + '.lean')):
                if file_hash(path, maximum=item.source.size, check=context.check) != item.source.sha256:
                    raise EnvironmentError('Dependency source changed during replay')
        environment.validate(hash_toolchain=True, fresh=True, check=context.check)
        return Replay(environment.id, modules.id, order, tuple((name, tuple(sorted(graph[name]))) for name in sorted(graph)), options)
