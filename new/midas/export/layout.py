"""Source-bearing library layouts and durable, non-overwriting publication."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import ctypes
import os
import shutil
import tempfile
from uuid import UUID, uuid5

from ..checker.requests import read
from ..checker.rebuild import modules as validate_modules
from ..domain import ArtifactRef, JobKind
from ..domain import encoding as codec
from ..environment import safe_read, scan
from ..environment.inventory import open_root, sync_directory
from .protocol import MAX_BYTES, MAX_FILES, Request, file_rows


def reference(data):
    digest = sha256(data).hexdigest()
    return ArtifactRef(str(uuid5(UUID('9f789091-3ca3-4b42-977b-6a57532c4d37'), digest)), digest, len(data))


@dataclass(frozen=True, slots=True)
class Library:
    environment_id: str
    payload: bytes

    def __post_init__(self):
        from ..domain.records import identifier
        identifier(self.environment_id, 'env_')
        validate_modules(codec.decode(self.payload))

    def decode(self):
        return codec.decode(self.payload)


def prepare_library(environment, modules, replay, dependencies, staging, *, maximum=16*1024**3):
    """Capture an environment-admitted source closure after its source replay.

    The worker repeats the build independently. The replay only supplies a valid
    order and source provenance; it never supplies export acceptance.
    """
    if replay.environment_id != environment.id or replay.modules_id != modules.id or replay.options:
        raise ValueError('Library replay differs from environment/modules')
    return _capture_library(environment, modules, replay.build_order, dependencies, staging, maximum=maximum)


def prepare_compiled_library(environment, modules, source_order, dependencies, staging, *, maximum=16*1024**3):
    """Trust frozen external objects; retain local helpers as ordered sources."""
    local = {m.name for m in modules.modules if environment.manifest.modules.get(m.name, '').startswith('project/')}
    if type(source_order) is not tuple or len(set(source_order)) != len(source_order) or set(source_order) != local:
        raise ValueError('Compiled library requires every local helper in source order')
    trusted = frozenset(m.name for m in modules.modules if m.name not in local)
    library = _capture_library(environment, modules, (*sorted(trusted), *source_order), dependencies, staging,
                               maximum=maximum, trusted=trusted)
    from ..checker.rebuild import trusted_roots
    trusted_roots(environment, library.decode())
    return library


def _capture_library(environment, modules, order, dependencies, staging, *, maximum, trusted=frozenset()):
    environment.validate()
    by_name = {m.name: m for m in modules.modules}
    roots = {d.name: d.path for d in dependencies}
    rows, total = [], 0
    for name in order:
        item = by_name[name]
        object_path = environment.manifest.modules.get(name)
        if object_path is None or object_path.startswith('toolchain/'):
            raise ValueError('Library module is outside the admitted environment')
        compiled = {f.path[len(name.replace('.', '/')):]: f.sha256 for f in item.compiled}
        for suffix, digest in compiled.items():
            pinned = environment.manifest.inventory.by_path.get(object_path[:-6] + suffix)
            if pinned is None or pinned.sha256 != digest:
                raise ValueError('Library compiled interface differs from environment')
        if name in trusted:
            rows.append({'name': name, 'package': item.lake_package, 'source': None,
                         'assets': [], 'options': [], 'compiled': compiled})
            continue
        source = safe_read(roots[item.package] / item.source.path, MAX_BYTES)
        total += len(source)
        if total > maximum or len(source) != item.source.size or sha256(source).hexdigest() != item.source.sha256:
            raise ValueError('Library source changed or exceeds export limit')
        ref = staging.write_bounded(source, max_bytes=MAX_BYTES)
        ref.verify(source, max_bytes=MAX_BYTES)
        assets = []
        for path, asset in item.assets:
            data = safe_read(roots[item.package] / asset.path, MAX_BYTES)
            total += len(data)
            if total > maximum or len(data) != asset.size or sha256(data).hexdigest() != asset.sha256:
                raise ValueError('Library source asset changed or exceeds export limit')
            asset_ref = staging.write_bounded(data, max_bytes=MAX_BYTES)
            assets.append([path, asset_ref.to_dict()])
        rows.append({'name': name, 'package': item.lake_package, 'source': ref.to_dict(),
                     'assets': assets, 'options': [list(pair) for pair in item.lean_options],
                     'compiled': compiled})
    return Library(environment.id, codec.encode(rows))


def _blocks(reader, ref):
    stream = getattr(reader, 'stream_artifact', None)
    if stream is not None:
        yield from stream(ref, max_bytes=ref.byte_count)
    else:
        yield read(reader, ref, max(MAX_BYTES, ref.byte_count))


def _write(reader, ref, path, check):
    digest, count = sha256(), 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as target:
        for block in _blocks(reader, ref):
            check()
            count += len(block)
            if count > ref.byte_count:
                raise ValueError('Export artifact grew while copying')
            digest.update(block)
            target.write(block)
        if count != ref.byte_count or digest.hexdigest() != ref.sha256:
            raise ValueError('Export artifact changed while copying')
        target.flush()
        os.fsync(target.fileno())


def _inventory(directory, expected, maximum, check):
    inventory, before = scan(directory, maximum=maximum, max_entries=MAX_FILES*4, check=check)
    actual = {f.path: (f.sha256, f.size) for f in inventory.files}
    desired = {path: (ref.sha256, ref.byte_count) for path, ref in expected.items()}
    folders = {'.'} | {p.as_posix() for path in expected for p in Path(path).parents}
    if actual != desired or set(inventory.directories) != folders:
        raise ValueError('Export destination differs from verified files')
    _, after = scan(directory, hash_content=False, maximum=maximum, max_entries=MAX_FILES*4, check=check)
    if before != after:
        raise ValueError('Export changed during verification')
    return inventory


class _Reader:
    def __init__(self, reader, extra):
        self.reader, self.extra = reader, extra

    def stream_artifact(self, ref, *, max_bytes):
        if ref in self.extra:
            yield ref.verify(self.extra[ref], max_bytes=max_bytes)
        else:
            yield from _blocks(self.reader, ref)


def materialize(stage, reader, destination, *, protected=(), check=lambda: None):
    if stage.request.kind != JobKind.EXPORT_VERIFY:
        raise ValueError('Only independently verified exports may be published')
    files = stage.files
    for path in ('request.bin', 'audit.bin', 'manifest.bin'):
        if path in files:
            raise ValueError('Export file conflicts with metadata')
    extra = {reference(stage.request.payload): stage.request.payload}
    files['request.bin'] = next(iter(extra))
    files['audit.bin'] = stage.evidence
    manifest = codec.encode({'format': 'midas-export-files-v1',
        'files': [[p, r.to_dict()] for p, r in sorted(files.items())]})
    files['manifest.bin'] = reference(manifest)
    extra[files['manifest.bin']] = manifest
    maximum = stage.request.decode()['export']['maximum']
    return publish_files(files, _Reader(reader, extra), destination, maximum=maximum,
                         protected=protected, check=check)


def publish_files(files, reader, destination, *, maximum=16*1024**3, protected=(), check=lambda: None):
    """Publish an exact file set atomically; callers own semantic acceptance."""
    files = file_rows([[p, r.to_dict()] for p, r in files.items()], maximum)
    if sum(r.byte_count for r in files.values()) > maximum:
        raise ValueError('Export metadata exceeds total byte limit')
    destination = Path(destination).absolute()
    if any(destination.is_relative_to(Path(p).resolve()) or Path(p).resolve().is_relative_to(destination)
           for p in protected):
        raise ValueError('Export destination overlaps protected inputs or run directory')
    fd = open_root(destination.parent)
    os.close(fd)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError('Export destination is not a regular directory')
        _inventory(destination, files, maximum, check)
        return destination
    temporary = Path(tempfile.mkdtemp(prefix='.midas-export-', dir=destination.parent))
    try:
        for path, ref in files.items():
            _write(reader, ref, temporary / path, check)
        inventory = _inventory(temporary, files, maximum, check)
        for folder in sorted(inventory.directories, key=lambda p: p.count('/'), reverse=True):
            sync_directory(temporary / folder)
        check()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renameat2(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot publish export without replacing output')
        sync_directory(destination.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


class BundleReader:
    """Read exact exported files without a run database or original project."""
    def __init__(self, directory, files):
        self.directory = directory
        self.files = {ref: path for path, ref in files.items()}

    def read_artifact(self, ref, *, max_bytes):
        return ref.verify(safe_read(self.directory / self.files[ref], max_bytes), max_bytes=max_bytes)

    def stream_artifact(self, ref, *, max_bytes):
        from ..environment.inventory import _open_file, stamp
        if ref.byte_count > max_bytes:
            raise ValueError('Export artifact exceeds read limit')
        path = self.directory / self.files[ref]
        fd, parent = _open_file(path)
        try:
            with os.fdopen(fd, 'rb') as source:
                before = stamp(os.fstat(fd))
                digest, count = sha256(), 0
                while block := source.read(1024**2):
                    count += len(block)
                    if count > ref.byte_count:
                        raise ValueError('Export artifact grew during read')
                    digest.update(block)
                    yield block
                if (count != ref.byte_count or digest.hexdigest() != ref.sha256 or before != stamp(os.fstat(fd))
                        or before != stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                    raise ValueError('Export artifact changed during read')
        finally:
            os.close(parent)


def read_bundle(directory, *, maximum=16*1024**3):
    directory = Path(directory).absolute()
    payload = safe_read(directory / 'manifest.bin', MAX_BYTES)
    value = codec.decode(payload)
    codec.fields(value, ('format', 'files'))
    if value['format'] != 'midas-export-files-v1':
        raise ValueError('Unknown export manifest')
    files = file_rows(value['files'], maximum)
    if 'manifest.bin' in files or not {'request.bin', 'audit.bin'} <= files.keys():
        raise ValueError('Invalid export metadata inventory')
    _inventory(directory, dict(files, **{'manifest.bin': reference(payload)}), maximum, lambda: None)
    reader = BundleReader(directory, files)
    request = Request(read(reader, files['request.bin']))
    if request.kind != JobKind.EXPORT_VERIFY:
        raise ValueError('Bundle has no independent verification request')
    expected = file_rows(request.decode()['export']['files'], maximum)
    if {p: r for p, r in files.items() if p not in ('request.bin', 'audit.bin')} != expected:
        raise ValueError('Bundle files differ from its audit request')
    return request, reader
