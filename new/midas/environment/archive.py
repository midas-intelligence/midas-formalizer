"""Small verifier configuration and flat raw-byte trusted benchmark archives.

This freezes provenance and target metadata. The LeanEval CLI wrapper owns Lake
and comparator commands; proof acceptance and source exports remain checker/export operations.
"""
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
import io
from pathlib import Path
import re
import tomllib

from ..domain.encoding import canonical, fields, load_json
from ..source import parse_identifier
from .dependencies import Dependency, GitReader, lake_pins
from .inventory import (EnvironmentError, chunks, file_hash, read_rows,
                        relative, safe_read, scan)

TOOLS = ('landrun', 'lean4export', 'comparator', 'nanoda_bin')
ROOT_FILES = ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain', 'LICENSE', 'SECURITY.md')
STARTER = ('Challenge.lean', 'Solution.lean', 'Submission.lean', 'WorkspaceTest.lean',
           'holes.json', 'config.json', 'lakefile.toml', 'lean-toolchain')
FORMAT = 'midas-verification-config-v2'
MAGIC = b'MIDASARC\x02'


def _pin(value, length=64):
    if type(value) is not str or len(value) != length or any(c not in '0123456789abcdef' for c in value):
        raise EnvironmentError('Expected lowercase hexadecimal content pin')
    return value


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    path: Path
    sha256: str

    def __post_init__(self):
        if self.name not in TOOLS:
            raise EnvironmentError('Unknown verifier tool')
        _pin(self.sha256)


@dataclass(frozen=True)
class VerificationConfig:
    benchmark: Path
    revision: str
    problem: str
    tools: tuple[Tool, ...]
    dependencies: tuple[Dependency, ...] = ()
    allow_archived_fixture: bool = False

    def __post_init__(self):
        _pin(self.revision, 40)
        if type(self.problem) is not str or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,127}', self.problem):
            raise EnvironmentError('Invalid problem identifier')
        if type(self.tools) is not tuple or tuple(t.name for t in self.tools) != TOOLS:
            raise EnvironmentError('Expected the four pinned verifier tools in canonical order')
        if type(self.dependencies) is not tuple or len(self.dependencies) > 32 or len({d.name for d in self.dependencies}) != len(self.dependencies):
            raise EnvironmentError('Invalid verifier dependency mappings')
        if type(self.allow_archived_fixture) is not bool:
            raise EnvironmentError('Archived fixture opt-in must be boolean')

    def identity_bytes(self):
        # Locators can change; pins and search ordering cannot.
        return canonical(dict(format=FORMAT, revision=self.revision, problem=self.problem,
            tools={t.name: t.sha256 for t in self.tools},
            dependencies=[dict(name=d.name, module_paths=list(d.module_paths), source_roots=list(d.source_roots))
                          for d in self.dependencies], allow_archived_fixture=self.allow_archived_fixture))

    @property
    def id(self):
        return sha256(self.identity_bytes()).hexdigest()

    def admit_tools(self, *, max_bytes=16 * 1024**3, check=lambda: None):
        total = 0
        for tool in self.tools:
            info = tool.path.stat()
            total += info.st_size
            if total > max_bytes or not info.st_mode & 0o111 or file_hash(tool.path, maximum=info.st_size, check=check) != tool.sha256:
                raise EnvironmentError('Verifier tool differs from its pin or exceeds byte bound')
        return total


def load_config(payload, *, base=Path('.')):
    value = load_json(payload)  # Hard 16 KiB cap before JSON decoding.
    fields(value, ('format', 'benchmark', 'revision', 'problem', 'tools', 'dependencies', 'allow_archived_fixture'))
    if value['format'] != FORMAT:
        raise EnvironmentError('Unsupported verifier configuration')

    def locator(path):
        if type(path) is not str or not path or '\x00' in path:
            raise EnvironmentError('Invalid locator')
        return (Path(base) / path).absolute()

    fields(value['tools'], TOOLS)
    tools = []
    for name in TOOLS:
        entry = value['tools'][name]
        fields(entry, ('path', 'sha256'))
        tools.append(Tool(name, locator(entry['path']), entry['sha256']))
    if type(value['dependencies']) is not list or len(value['dependencies']) > 32:
        raise EnvironmentError('Invalid dependency list')
    dependencies = []
    for entry in value['dependencies']:
        fields(entry, ('name', 'path', 'module_paths', 'source_roots'))
        if type(entry['module_paths']) is not list or type(entry['source_roots']) is not list:
            raise EnvironmentError('Invalid module/source path list')
        dependencies.append(Dependency(entry['name'], locator(entry['path']), tuple(entry['module_paths']), tuple(entry['source_roots'])))
    return VerificationConfig(locator(value['benchmark']), value['revision'], value['problem'],
                              tuple(tools), tuple(dependencies), value['allow_archived_fixture'])


def _identity(payload):
    value = load_json(payload)
    fields(value, ('format', 'revision', 'problem', 'tools', 'dependencies', 'allow_archived_fixture'))
    # Use exactly the same typed admission for loaded and freshly supplied identities.
    config = load_config(canonical(dict(value, benchmark='.',
        tools={name: dict(path='.', sha256=digest) for name, digest in value['tools'].items()},
        dependencies=[dict(d, path='.') for d in value['dependencies']])))
    if config.identity_bytes() != payload:
        raise EnvironmentError('Noncanonical archive configuration')
    return config


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    path: str
    data: bytes
    executable: bool = False

    def __post_init__(self):
        relative(self.path)
        if type(self.data) is not bytes or len(self.data) > 8 * 1024**2 or type(self.executable) is not bool:
            raise EnvironmentError('Invalid or oversized archive file')


@dataclass(frozen=True)
class Archive:
    config: bytes
    files: tuple[ArchiveFile, ...]

    def __post_init__(self):
        config = _identity(self.config)
        if type(self.files) is not tuple or not 1 <= len(self.files) <= 10000:
            raise EnvironmentError('Invalid archive file count')
        if tuple(sorted({f.path for f in self.files})) != tuple(f.path for f in self.files):
            raise EnvironmentError('Duplicate or unsorted archive files')
        if sum(len(f.data) for f in self.files) > 16 * 1024**2:
            raise EnvironmentError('Archive content exceeds byte bound')
        prefix = 'generated/' + config.problem + '/'
        metadata = 'manifests/problems/' + config.problem + '.toml'
        if any(f.path not in ROOT_FILES and f.path != metadata and not f.path.startswith(prefix) for f in self.files):
            raise EnvironmentError('Unrecorded archive namespace')
        describe(self)

    def chunks(self):
        def rows():
            yield (b'C', self.config)
            for item in self.files:
                yield (b'F', item.path.encode(), bytes([item.executable]), item.data)
        return chunks(rows(), MAGIC)

    @cached_property
    def id(self):
        digest = sha256()
        for block in self.chunks():
            digest.update(block)
        return digest.hexdigest()

    def save(self, output):
        for block in self.chunks():
            output.write(block)


@dataclass(frozen=True, slots=True)
class Benchmark:
    problem: str
    targets: tuple[tuple[str, str], ...]
    required_dependencies: tuple[str, ...]
    fixture_only: bool


def describe(archive):
    config = _identity(archive.config)
    files = {f.path: f.data for f in archive.files}
    prefix = 'generated/' + config.problem + '/'
    metadata = 'manifests/problems/' + config.problem + '.toml'
    if not set((*ROOT_FILES, metadata, *(prefix + p for p in STARTER))) <= files.keys():
        raise EnvironmentError('Missing trusted benchmark/starter metadata')
    try:
        catalog = tomllib.loads(files[metadata].decode())
        if catalog.get('id') != config.problem or catalog.get('status') not in ('active', 'archived'):
            raise EnvironmentError('Catalog identifier/status mismatch')
        archived = catalog['status'] == 'archived'
        if archived and not config.allow_archived_fixture:
            raise EnvironmentError('Archived fixture requires explicit opt-in')
        holes = load_json(files[prefix + 'holes.json'])
        settings = load_json(files[prefix + 'config.json'])
        if type(holes) is not dict or holes.get('id') != config.problem or holes.get('module') != catalog.get('module'):
            raise EnvironmentError('Catalog and starter metadata disagree')
        hole_list, catalog_holes = holes.get('holes'), catalog.get('holes')
        if (type(hole_list) is not list or not 1 <= len(hole_list) <= 4096 or type(catalog_holes) is not list
                or len(catalog_holes) != len(hole_list) or len(set(catalog_holes)) != len(catalog_holes)):
            raise EnvironmentError('Invalid benchmark hole inventory')
        def qualified(name):
            if parse_identifier(name) is None:
                raise EnvironmentError('Invalid comparator output name')
            return name if name.startswith('Submission.') else 'Submission.' + name
        targets = []
        for catalog_name, hole in zip(catalog_holes, hole_list):
            if (type(hole) is not dict or catalog_name not in (hole.get('name'), hole.get('basename'))
                    or hole.get('kind') not in ('theorem', 'lemma', 'def')):
                raise EnvironmentError('Catalog hole mismatch')
            targets.append((qualified(hole.get('name') or hole.get('basename')),
                            'def' if hole['kind'] == 'def' else 'theorem'))
        advertised = []
        for key, kind in (('theorem_names', 'theorem'), ('definition_names', 'def')):
            names = settings.get(key, [])
            if type(names) is not list:
                raise EnvironmentError('Invalid comparator output inventory')
            advertised.extend((qualified(name), kind) for name in names)
        if len(targets) == len(advertised) == 1 and targets[0][1] == advertised[0][1] == 'theorem':
            targets = advertised[:]
        if len(set(targets)) != len(targets) or len(advertised) != len(targets) or set(advertised) != set(targets):
            raise EnvironmentError('Missing, duplicate or mismatched output inventory')
        lake = tomllib.loads(files[prefix + 'lakefile.toml'].decode())
        pins = lake_pins(files['lake-manifest.json'])
        required = []
        for requirement in lake.get('require', []):
            pin = pins.get(requirement.get('name'))
            if pin is None or pin.revision != requirement.get('rev') or pin.url != requirement.get('git'):
                raise EnvironmentError('Starter requirements differ from pinned lock')
            required.append(pin.name)
        if any(d.name not in pins for d in config.dependencies):
            raise EnvironmentError('Configured dependency absent from trusted lock')
        if not files[prefix + 'lean-toolchain'].strip():
            raise EnvironmentError('Missing starter toolchain pin')
        return Benchmark(config.problem, tuple(targets), tuple(required), archived)
    except (KeyError, TypeError, AttributeError, UnicodeError) as error:
        raise EnvironmentError('Invalid benchmark metadata') from error


def archive_benchmark(config, context, *, bootstrap=None, max_bytes=16 * 1024**3):
    total = config.admit_tools(max_bytes=max_bytes, check=context.check)
    reader = GitReader(config.benchmark, context, bootstrap=bootstrap)
    if reader.head() != config.revision:
        raise EnvironmentError('Benchmark HEAD differs from requested commit')
    prefix = 'generated/' + config.problem
    selected = (*ROOT_FILES, 'manifests/problems/' + config.problem + '.toml', prefix)
    listing = reader.listing(config.revision, *selected)
    if len(listing) > 10000:
        raise EnvironmentError('Archive exceeds file count bound')
    starter, before = scan(config.benchmark / prefix, hash_content=False, maximum=max_bytes, check=context.check)
    expected = {path[len(prefix) + 1:]: executable for path, (executable, _) in listing.items() if path.startswith(prefix + '/')}
    if {f.path: f.executable for f in starter.files} != expected:
        raise EnvironmentError('Starter contains missing or unrecorded files')
    files, content_size = [], 0
    for path, (executable, oid) in sorted(listing.items()):
        context.check()
        value = reader.run('cat-file', 'blob', oid)
        total += len(value)
        content_size += len(value)
        if total > max_bytes or content_size > 16 * 1024**2:
            raise EnvironmentError('Archive exceeds content byte bound')
        if safe_read(config.benchmark / path, 8 * 1024**2) != value or bool((config.benchmark / path).stat().st_mode & 0o111) != executable:
            raise EnvironmentError('Benchmark working tree differs from commit')
        files.append(ArchiveFile(path, value, executable))
    archive = Archive(config.identity_bytes(), tuple(files))
    for item in files:
        if (safe_read(config.benchmark / item.path, 8 * 1024**2) != item.data or
                bool((config.benchmark / item.path).stat().st_mode & 0o111) != item.executable):
            raise EnvironmentError('Benchmark changed during archival')
    _, after = scan(config.benchmark / prefix, hash_content=False, maximum=max_bytes, check=context.check)
    if before != after or reader.head() != config.revision:
        raise EnvironmentError('Benchmark checkout changed during archival')
    config.admit_tools(max_bytes=max_bytes, check=context.check)
    return archive


def load_archive(source, *, expected_id):
    _pin(expected_id)
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    rows = iter(read_rows(source, MAGIC))
    try:
        header = next(rows)
        if len(header) != 2 or header[0] != b'C':
            raise EnvironmentError('Invalid archive header')
        files = []
        total = 0
        for row in rows:
            if len(row) != 4 or row[0] != b'F' or row[2] not in (b'\0', b'\1'):
                raise EnvironmentError('Invalid archive file record')
            total += len(row[3])
            if total > 16 * 1024**2 or len(files) >= 10000:
                raise EnvironmentError('Archive exceeds content/entry bound')
            files.append(ArchiveFile(row[1].decode(), row[3], row[2] == b'\1'))
        archive = Archive(header[1], tuple(files))
        if archive.id != expected_id:
            raise EnvironmentError('Archive digest mismatch')
        return archive
    except (StopIteration, UnicodeError) as error:
        raise EnvironmentError('Malformed benchmark archive') from error


def match_relocation(config, archive, *, max_bytes=16 * 1024**3, check=lambda: None):
    if config.identity_bytes() != archive.config:
        raise EnvironmentError('Relocation changes saved verifier identity')
    config.admit_tools(max_bytes=max_bytes, check=check)
    return describe(archive)
