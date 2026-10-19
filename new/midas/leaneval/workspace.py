"""Bind a trusted benchmark to an existing package and prepare its source library."""
from dataclasses import dataclass, replace
from contextlib import closing
from hashlib import sha256
from graphlib import TopologicalSorter
from pathlib import Path

from ..application.preparation import index_sources, operate
from ..domain import ArtifactRef, FrozenSelection, SelectionRequest, encode_selection, load_selection
from ..domain import encoding as codec
from ..environment import (Archive, Dependency, File, ModulePin, archive_benchmark, describe,
    inventory_modules, load_archive, resolve_closure, safe_read, scan)
from ..environment.dependencies import SUFFIXES
from ..environment.inventory import chunks
from ..environment.package import output
from ..export import BundleReader, Library, prepare_compiled_library, prepare_library
from ..export.layout import _Reader, publish_files, reference
from ..checker.rebuild import module_files
from . import attachments

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
    imports: tuple[str, ...] = ()
    attachments: tuple[tuple[str, str], ...] = ()


def load(application, directory, *, package=None):
    directory = Path(directory).absolute()
    value = codec.decode(safe_read(directory/'workspace.bin', 16384))
    codec.fields(value, ('format', 'package', 'environment', 'archive', 'selection', 'library', 'imports', 'attachments'))
    if value['format'] != 'midas-leaneval-workspace-v2':
        raise ValueError('Unknown LeanEval workspace')
    environment = application.environment(package or value['package'], expected_id=value['environment'])
    package = environment.directory
    archive = load_archive(safe_read(directory/'archive.bin', MAX_BYTES), expected_id=value['archive'])
    selected = safe_read(directory/'selection.bin', MAX_BYTES)
    library = safe_read(directory/'library.bin', MAX_BYTES)
    attached = safe_read(directory/'attachments.bin', MAX_BYTES)
    if sha256(selected).hexdigest() != value['selection'] or sha256(library).hexdigest() != value['library']:
        raise ValueError('Workspace selection/library changed')
    if sha256(attached).hexdigest() != value['attachments']:
        raise ValueError('Workspace attachments changed')
    mappings = codec.decode(attached)
    if (type(mappings) is not list or len(mappings) > 10000 or
            any(type(row) is not list or len(row) != 2 or any(type(p) is not str for p in row) for row in mappings)):
        raise ValueError('Invalid attachment source mappings')
    selection = load_selection(selected, environment.selection_inputs())
    check_selection(selection, archive)
    library = Library(environment.id, library)
    rows = library.decode()
    from ..domain.identity import validate_imports
    validate_imports(value['imports'])
    available = {row.name for row in rows} | {n for n, p in environment.manifest.modules.items() if p.startswith('toolchain/')}
    if not set(value['imports']) <= available:
        raise ValueError('Advertised import is outside the workspace library')
    files = {ref.sha256+'.bin': ref for ref in module_files(rows).values()}
    reader = BundleReader(directory/'library', files)
    # Read each source through its existing bounded artifact verifier.
    for ref in files.values():
        for _ in reader.stream_artifact(ref, max_bytes=MAX_BYTES):
            pass
    return Workspace(directory, package, environment.id, archive, selection,
                     library if rows else None, reader, tuple(value['imports']), tuple(tuple(row) for row in mappings))


def prepare(application, destination, *, package, verification, config, cancellation=None, replay_dependencies=False,
            attachment_configs=(), imports=()):
    if type(replay_dependencies) is not bool:
        raise ValueError('Expected a boolean dependency replay option')
    environment = application.environment(package)
    from ..domain.identity import validate_imports
    validate_imports(list(imports))
    configured = tuple(attachments.configuration(path, environment) for path in attachment_configs)
    if {d.name for d, _ in configured} & {d.name for d in verification.dependencies}:
        raise ValueError('Attachment cannot replace a benchmark dependency')

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
                                    max_bytes=config.export.max_bytes, source_only=replay_dependencies)
        for dependency in verification.dependencies:
            if (dependency.name not in environment.roots or
                    scan(dependency.path, links=True, maximum=config.export.max_bytes, check=context.check)[0] !=
                    scan(environment.roots[dependency.name], links=True,
                         maximum=config.export.max_bytes, check=context.check)[0]):
                raise ValueError('Frozen dependency differs from admitted benchmark dependency')
        if replay_dependencies:
            from ..environment import configure_modules
            modules = configure_modules(environment, closure, modules, context)
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
        def imported_modules(source):
            recipe = environment.launch(('--deps', '/project/'+source))
            names = []
            for path in output(context.run_process(recipe)).decode().splitlines():
                name = by_path.get(path)
                if name is None:
                    raise ValueError('Starter imports an unrecorded module: '+path)
                if name not in standard and name not in names:
                    names.append(name)
            return tuple(names)
        required = imported_modules('Submission.lean')
        modules = replace(modules, required=tuple(required), modules=tuple(sorted(combined, key=lambda m: m.name)))
        dependencies = (*verification.dependencies, Dependency(local_name, environment.roots['project'], ('.',)))
        if replay_dependencies:
            from ..environment import replay_dependencies as replay_sources
            replay = replay_sources(environment, modules, dependencies, context)
        else:
            local_names = {m.name for m in local}
            graph = {m.name: sorted(set(imported_modules(m.source.path)) & local_names) for m in local}
            source_order = tuple(TopologicalSorter(graph).static_order())
        # Stream library sources once, without the preparation worker's small artifact quota.
        from ..storage import Staging
        with closing(Staging(context.workspace/'library', max_bytes=config.export.max_bytes, max_artifacts=10000)) as staging:
            if replay_dependencies:
                library = prepare_library(environment, modules, replay, dependencies, staging,
                                          maximum=config.export.max_bytes)
            else:
                library = prepare_compiled_library(environment, modules, source_order, dependencies, staging,
                                                   maximum=config.export.max_bytes)
            extra, mappings = attachments.capture(environment, configured,
                {m.name for m in modules.modules} | standard)
            attachments.sources(environment, mappings, maximum=config.export.max_bytes)
            library = Library(environment.id, codec.encode([*codec.decode(library.payload), *extra]))
            available = {row.name for row in library.decode()} | standard
            if not set(imports) <= available:
                raise ValueError('Advertised import is outside the workspace library')
            selection = application.checker_factory(environment, application.cache).inspect(
                SelectionRequest('Submission.lean', tuple(n for n, _ in benchmark.targets)), context)
            check_selection(selection, archive)
            index_sources(environment, context)
            selected = encode_selection(selection)
            files = {'archive.bin': b''.join(archive.chunks()), 'selection.bin': selected, 'library.bin': library.payload}
            files['attachments.bin'] = codec.encode([list(row) for row in mappings])
            files['workspace.bin'] = codec.encode(dict(format='midas-leaneval-workspace-v2',
                package=str(environment.directory), environment=environment.id, archive=archive.id,
                selection=sha256(selected).hexdigest(), library=sha256(library.payload).hexdigest(),
                imports=list(imports), attachments=sha256(files['attachments.bin']).hexdigest()))
            environment.validate(check=context.check)
            refs = {p: reference(data) for p, data in files.items()}
            refs.update({'library/'+ref.sha256+'.bin': ref for ref in module_files(library.decode()).values()})
            return publish_files(refs, _Reader(staging, {reference(data): data for data in files.values()}),
                destination, protected=(environment.directory, verification.benchmark,
                    environment.toolchain, *(d.path for d in verification.dependencies)),
                check=context.check, maximum=config.export.max_bytes)
    operate(build, limits=replace(config.export.limits(), wall_ms=config.export.deadline_ms), cancellation=cancellation)
    return load(application, destination, package=package)
