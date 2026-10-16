"""Bind a trusted benchmark to an existing package and prepare its source library."""
from dataclasses import dataclass, replace
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from ..application.preparation import operate
from ..domain import ArtifactRef, FrozenSelection, SelectionRequest, encode_selection, load_selection
from ..domain import encoding as codec
from ..environment import (Archive, Dependency, File, ModulePin, archive_benchmark, describe,
    inventory_modules, load_archive, replay_dependencies, resolve_closure, safe_read, scan)
from ..environment.dependencies import SUFFIXES
from ..environment.inventory import chunks
from ..environment.package import output
from ..export import BundleReader, Library, prepare_library
from ..export.layout import _Reader, publish_files, reference

MAX_BYTES = 64*1024**2


def evidence(metadata, payloads, maximum):
    """Use the environment's flat framing for arbitrary raw diagnostic bytes."""
    blocks, total = [], 0
    for block in chunks(((b'M', codec.encode(metadata)), *payloads), b'MIDASEVL\x01'):
        total += len(block)
        if total > maximum:
            raise ValueError('LeanEval evidence exceeds byte limit')
        blocks.append(block)
    return b''.join(blocks)


def starter(archive):
    prefix = 'generated/'+describe(archive).problem+'/'
    return {f.path[len(prefix):]: f.data for f in archive.files if f.path.startswith(prefix)}


def check_selection(selection, archive):
    benchmark = describe(archive)
    actual = tuple((t.selector, 'def' if t.kind == 'def' else 'theorem') for t in selection.selected.targets)
    if (selection.selected.request.source != 'Submission.lean'
            or selection.source != starter(archive)['Submission.lean']
            or len(actual) != len(benchmark.targets) or set(actual) != set(benchmark.targets)):
        raise ValueError('Selection must contain exactly the trusted benchmark outputs')


@dataclass(frozen=True)
class Workspace:
    directory: Path
    package: Path
    environment_id: str
    archive: Archive
    selection: FrozenSelection
    library: Library | None
    library_reader: BundleReader


def load(application, directory, *, package=None):
    directory = Path(directory).absolute()
    value = codec.decode(safe_read(directory/'workspace.bin', 16384))
    codec.fields(value, ('format', 'package', 'environment', 'archive', 'selection', 'library'))
    if value['format'] != 'midas-leaneval-workspace-v1':
        raise ValueError('Unknown LeanEval workspace')
    package = Path(package or value['package']).absolute()
    environment = application.environment(package, expected_id=value['environment'])
    archive = load_archive(safe_read(directory/'archive.bin', MAX_BYTES), expected_id=value['archive'])
    selected = safe_read(directory/'selection.bin', MAX_BYTES)
    library = safe_read(directory/'library.bin', MAX_BYTES)
    if sha256(selected).hexdigest() != value['selection'] or sha256(library).hexdigest() != value['library']:
        raise ValueError('Workspace selection/library changed')
    selection = load_selection(selected, environment.selection_inputs())
    check_selection(selection, archive)
    library = Library(environment.id, library)
    files = {row['source']['sha256']+'.lean': ArtifactRef.from_dict(row['source']) for row in library.decode()}
    reader = BundleReader(directory/'library', files)
    # Read each source through its existing bounded artifact verifier.
    for ref in files.values():
        for _ in reader.stream_artifact(ref, max_bytes=MAX_BYTES):
            pass
    return Workspace(directory, package, environment.id, archive, selection,
                     library if files else None, reader)


def prepare(application, destination, *, package, verification, config, cancellation=None):
    environment = application.environment(package)

    def build(context):
        archive = archive_benchmark(verification, context, max_bytes=config.export.max_bytes)
        trusted = starter(archive)
        expected_toolchain = ('leanprover/lean4:v'+environment.manifest.settings.version).encode()
        if trusted['lean-toolchain'].strip() != expected_toolchain:
            raise ValueError('Benchmark and package toolchains differ')
        for path, data in trusted.items():
            if path.endswith('.lean') and safe_read(environment.roots['project']/path, MAX_BYTES) != data:
                raise ValueError('Package source differs from the trusted starter: '+path)
        root_files = {f.path: f.data for f in archive.files}
        benchmark = describe(archive)
        closure = resolve_closure(root_files['lake-manifest.json'], benchmark.required_dependencies,
                                  verification.dependencies, context)
        standard = frozenset(n for n, p in environment.manifest.modules.items() if p.startswith('toolchain/'))
        modules = inventory_modules(closure, verification.dependencies, context, standard=standard,
                                    max_bytes=config.export.max_bytes, source_only=True)
        for dependency in verification.dependencies:
            if (dependency.name not in environment.roots or
                    scan(dependency.path, links=True, maximum=config.export.max_bytes, check=context.check)[0] !=
                    scan(environment.roots[dependency.name], links=True,
                         maximum=config.export.max_bytes, check=context.check)[0]):
                raise ValueError('Frozen dependency differs from admitted benchmark dependency')
        # Local starter helpers use their archived sources and the package's object inventory.
        local = []
        dependency_names = {d.name for d in verification.dependencies}
        local_name = next(f'starter{i}' for i in range(33) if f'starter{i}' not in dependency_names)
        inventory = environment.manifest.inventory.by_path
        for name, path in environment.manifest.modules.items():
            if not path.startswith('project/'):
                continue
            if name == 'Submission':
                continue
            stem = name.replace('.', '/')
            source_path = stem+'.lean'
            if source_path not in trusted:
                raise ValueError('Unarchived starter module in package')
            data = trusted[source_path]
            compiled = tuple(File(stem+suffix, inventory[path[:-6]+suffix].size,
                                  inventory[path[:-6]+suffix].executable, inventory[path[:-6]+suffix].sha256)
                             for suffix in SUFFIXES if path[:-6]+suffix in inventory)
            module_path = path.removeprefix('project/').removesuffix(stem+'.olean').rstrip('/') or '.'
            local.append(ModulePin(name, local_name, File(source_path, len(data), False, sha256(data).hexdigest()), compiled, module_path))
        combined = (*modules.modules, *local)
        if len({m.name for m in combined}) != len(combined):
            raise ValueError('Starter shadows a dependency module')
        by_path = {'/'+p: n for n, p in environment.manifest.modules.items()}
        required = []
        recipe = environment.launch(('--deps', '/project/Submission.lean'))
        for path in output(context.run_process(recipe)).decode().splitlines():
            name = by_path.get(path)
            if name is None:
                raise ValueError('Submission imports an unrecorded module: '+path)
            if name not in standard and name not in required:
                required.append(name)
        modules = replace(modules, required=tuple(required), modules=tuple(sorted(combined, key=lambda m: m.name)))
        dependencies = (*verification.dependencies, Dependency(local_name, environment.roots['project'], ('.',)))
        replay = replay_dependencies(environment, modules, dependencies, context)
        # Stream library sources once, without the preparation worker's small artifact quota.
        from ..storage import Staging
        with closing(Staging(context.workspace/'library', max_bytes=config.export.max_bytes, max_artifacts=10000)) as staging:
            library = prepare_library(environment, modules, replay, dependencies, staging,
                                      maximum=config.export.max_bytes)
            selection = application.checker_factory(environment, application.cache).inspect(
                SelectionRequest('Submission.lean', tuple(n for n, _ in benchmark.targets)), context)
            check_selection(selection, archive)
            selected = encode_selection(selection)
            files = {'archive.bin': b''.join(archive.chunks()), 'selection.bin': selected, 'library.bin': library.payload}
            files['workspace.bin'] = codec.encode(dict(format='midas-leaneval-workspace-v1',
                package=str(environment.directory), environment=environment.id, archive=archive.id,
                selection=sha256(selected).hexdigest(), library=sha256(library.payload).hexdigest()))
            environment.validate(check=context.check)
            refs = {p: reference(data) for p, data in files.items()}
            refs.update({'library/'+row['source']['sha256']+'.lean': ArtifactRef.from_dict(row['source'])
                         for row in library.decode()})
            return publish_files(refs, _Reader(staging, {reference(data): data for data in files.values()}),
                destination, protected=(environment.directory, verification.benchmark,
                    environment.toolchain, *(d.path for d in verification.dependencies)),
                check=context.check, maximum=config.export.max_bytes)
    operate(build, limits=replace(config.export.limits(), wall_ms=config.export.deadline_ms), cancellation=cancellation)
    return load(application, destination, package=package)
