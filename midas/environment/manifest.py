"""Environment-owned settings and canonical, relocatable binary manifests."""
from dataclasses import dataclass, field
from collections import OrderedDict
from functools import cached_property
from hashlib import sha256
import io
import os
from pathlib import Path
import re
import threading

from ..domain.encoding import LEAN_SERIALIZER
from ..domain.selection import SelectionInputs, SourceBinding
from ..executor import PROFILE
from .inventory import (EnvironmentError, File, Inventory, FILE_INFO, MAX_INVENTORY, MAX_BYTES,
                        _file, _inventory, chunks, read_rows, relative)

MAGIC = b'MIDASENV\x02'
VERSION = r'4\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?'
RESERVED = frozenset(('proc', 'dev', 'tmp', 'inputs', 'build'))
_cache = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def reset_manifest_cache():
    global _cache, _cache_bytes, _cache_lock
    _cache, _cache_bytes, _cache_lock = OrderedDict(), 0, threading.Lock()


os.register_at_fork(after_in_child=reset_manifest_cache)


def version_number(value):
    if type(value) is not str or len(value) > 128:
        raise EnvironmentError('Expected a Lean 4 release version')
    version = value.removeprefix('leanprover/lean4:').removeprefix('v')
    if re.fullmatch(VERSION, version) is None:
        raise EnvironmentError('Expected a pinned Lean 4 release version')
    return version


def check_version(output, expected):
    match = re.fullmatch(r'Lean \(version (' + VERSION + r'), [^\r\n]+\)', output.strip())
    if match is None or match[1] != version_number(expected):
        raise EnvironmentError('Installed Lean report differs from pinned version')
    return match[1]


def installed_directory(version, *, elan=None):
    base = Path(elan) if elan is not None else Path(os.environ.get('ELAN_HOME', Path.home() / '.elan'))
    return base / 'toolchains' / ('leanprover--lean4---v' + version_number(version))


def label(value):
    if type(value) is not str or len(value) > 64 or re.fullmatch('[A-Za-z][A-Za-z0-9_-]*', value) is None or value.lower() in RESERVED:
        raise EnvironmentError('Invalid or reserved environment root')
    return value


def logical(value):
    relative(value)
    label(value.split('/')[0])
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    version: str
    platform: str
    abi: str
    project_inputs: tuple[str, ...]
    search_paths: tuple[str, ...]
    runtime_loader: str
    runtime_paths: tuple[str, ...] = ('runtime', 'toolchain/lib', 'toolchain/lib/lean')
    lean_executable: str = 'toolchain/bin/lean'
    serializer: str = LEAN_SERIALIZER
    profile: str = PROFILE
    trust_policy: str = 'kernel-check-required-v1'
    plugins: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()
    variables: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if self.version != version_number(self.version):
            raise EnvironmentError('Expected normalized pinned version')
        if (self.profile != PROFILE or self.serializer != LEAN_SERIALIZER
                or self.trust_policy != 'kernel-check-required-v1'
                or not self.platform.startswith('linux-') or not self.abi.startswith('ELF-')):
            raise EnvironmentError('Unsupported isolated Lean profile')
        for value in (self.platform, self.abi):
            if len(value.encode()) > 256 or '\x00' in value:
                raise EnvironmentError('Invalid platform metadata')
        if logical(self.lean_executable).split('/')[0] != 'toolchain':
            raise EnvironmentError('Compiler must belong to toolchain')
        if logical(self.runtime_loader).split('/')[0] != 'runtime':
            raise EnvironmentError('Loader must belong to frozen runtime')
        for name in ('project_inputs', 'search_paths', 'runtime_paths', 'plugins'):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) > 10000 or len(set(values)) != len(values):
                raise EnvironmentError('Expected unique immutable settings paths')
            for value in values:
                logical(value)
                if name == 'project_inputs' and not value.startswith('project/'):
                    raise EnvironmentError('Project input must belong to project')
        if not self.project_inputs or not self.search_paths or not self.runtime_paths:
            raise EnvironmentError('Missing input/search/runtime paths')
        forbidden = {'PATH', 'HOME', 'TMPDIR', 'LC_ALL', 'TZ', 'LEAN_PATH', 'LEAN_SRC_PATH',
                     'LEAN_SYSROOT', 'LD_LIBRARY_PATH', 'LD_PRELOAD'}
        for name in ('options', 'variables'):
            pairs = getattr(self, name)
            if type(pairs) is not tuple or len(pairs) > 64:
                raise EnvironmentError('Expected bounded immutable settings pairs')
            seen = set()
            for pair in pairs:
                if type(pair) is not tuple or len(pair) != 2:
                    raise EnvironmentError('Invalid setting pair')
                key, value = pair
                if (type(key) is not str or type(value) is not str or key in seen
                        or not re.fullmatch('[A-Za-z_][A-Za-z0-9_.]*', key)
                        or '\x00' in value or len((key + value).encode()) > 1024):
                    raise EnvironmentError('Invalid setting pair')
                if name == 'variables' and key in forbidden:
                    raise EnvironmentError('Profile variable override is forbidden')
                seen.add(key)

    def rows(self):
        yield (b'S', *(getattr(self, name).encode() for name in
                      ('version', 'platform', 'abi', 'runtime_loader', 'lean_executable',
                       'serializer', 'profile', 'trust_policy')))
        for tag, name in ((b'P', 'project_inputs'), (b'Q', 'search_paths'), (b'R', 'runtime_paths'), (b'L', 'plugins')):
            for value in getattr(self, name):
                yield (tag, value.encode())
        for tag, name in ((b'O', 'options'), (b'V', 'variables')):
            for key, value in getattr(self, name):
                yield (tag, key.encode(), value.encode())


@dataclass(frozen=True)
class Manifest:
    settings: Settings
    inventory: Inventory
    launcher_sha256: str
    id: str = field(init=False)

    def __post_init__(self):
        self._validate()
        digest = sha256()
        for block in self.chunks():
            digest.update(block)
        object.__setattr__(self, 'id', 'env_' + digest.hexdigest())

    def _validate(self):
        if not isinstance(self.settings, Settings) or not isinstance(self.inventory, Inventory):
            raise EnvironmentError('Expected typed settings and inventory')
        if len(self.launcher_sha256) != 64 or any(c not in '0123456789abcdef' for c in self.launcher_sha256):
            raise EnvironmentError('Invalid launcher pin')
        roots = self.roots
        if not {'toolchain', 'runtime', 'project'} <= set(roots):
            raise EnvironmentError('Missing toolchain/runtime/project root')
        for name in roots:
            label(name)
        for item in self.inventory.files:
            if '/' not in item.path or not item.sha256:
                raise EnvironmentError('Unhashed file or file outside roots')
        spec = self.settings
        for path in (spec.lean_executable, spec.runtime_loader, *spec.project_inputs, *spec.plugins):
            if path not in self.inventory.by_path:
                raise EnvironmentError('Missing referenced file: ' + path)
        for path in (*spec.search_paths, *spec.runtime_paths):
            if path not in self.inventory.directories:
                raise EnvironmentError('Missing referenced directory: ' + path)
        for path in (spec.lean_executable, spec.runtime_loader):
            if not self.inventory.by_path[path].executable:
                raise EnvironmentError('Missing executable permission: ' + path)
        # Build this once; all later users receive hashes, bindings, or recipes.
        self.modules

    @cached_property
    def roots(self):
        return tuple(p for p in self.inventory.directories if p != '.' and '/' not in p)

    @cached_property
    def modules(self):
        modules = {}
        by_path = self.inventory.by_path
        # Walk each file's ancestors once, rather than every file for every root.
        searches = set(self.settings.search_paths)
        for path in by_path:
            if not path.endswith('.olean'):
                continue
            parts = path.split('/')
            for count in range(1, len(parts)):
                prefix = '/'.join(parts[:count])
                if prefix not in searches:
                    continue
                module = '.'.join(parts[count:])[:-6]
                if module in modules:
                    raise EnvironmentError('Shadowed compiled module: ' + module)
                modules[module] = path
        from types import MappingProxyType
        return MappingProxyType(modules)

    @cached_property
    def toolchain(self):
        return subset(self.inventory, 'toolchain')

    def selection_inputs(self):
        return SelectionInputs(self.id, tuple(SourceBinding(path[len('project/'):],
            self.inventory.by_path[path].sha256, self.inventory.by_path[path].size)
            for path in self.settings.project_inputs if path.endswith('.lean')))

    def chunks(self):
        def rows():
            yield (b'B', bytes.fromhex(self.launcher_sha256))
            yield from self.settings.rows()
            yield from self.inventory.rows()
        return chunks(rows(), MAGIC)

    def save(self, output):
        for block in self.chunks():
            output.write(block)


def subset(inventory, root):
    prefix = root + '/'
    return _inventory(tuple('.' if p == root else p[len(prefix):] for p in inventory.directories
                           if p == root or p.startswith(prefix)),
                     tuple(_file(f.path[len(prefix):], f.size, f.executable, f.sha256)
                           for f in inventory.files if f.path.startswith(prefix)))


def combine(roots):
    if sum(len(i.directories) + len(i.files) for i in roots.values()) >= 500000:
        raise EnvironmentError('Combined inventory exceeds entry bound')
    dirs, files = ['.'], []
    for name, inventory in sorted(roots.items()):
        label(name)
        dirs.extend(name if p == '.' else name + '/' + p for p in inventory.directories)
        files.extend(_file(name + '/' + f.path, f.size, f.executable, f.sha256) for f in inventory.files)
    return _inventory(tuple(sorted(dirs)), tuple(sorted(files, key=lambda f: f.path)))


def load_manifest(source, *, expected_id=None):
    global _cache_bytes
    payload = source if isinstance(source, bytes) else source.read(MAX_INVENTORY + 1)
    if len(payload) > MAX_INVENTORY:
        raise EnvironmentError('Manifest exceeds byte bound')
    with _cache_lock:
        cached = _cache.get(payload)
        if cached is not None:
            _cache.move_to_end(payload)
            manifest = cached[0]
        else:
            manifest = _decode_manifest(payload)
            # Conservative retained-object accounting, not just encoded bytes.
            retained = len(payload) + sum(800 + 8 * len(f.path.encode()) for f in manifest.inventory.files)
            retained += sum(128 + 8 * len(d.encode()) for d in manifest.inventory.directories)
            if retained <= 32 * 1024**2:
                while _cache and (len(_cache) >= 4 or _cache_bytes + retained > 32 * 1024**2):
                    _, (_, size) = _cache.popitem(last=False)
                    _cache_bytes -= size
                _cache[payload] = manifest, retained
                _cache_bytes += retained
    if expected_id is not None and manifest.id != expected_id:
        raise EnvironmentError('Mismatched manifest identity')
    return manifest


def _decode_manifest(payload):
    identity = 'env_' + sha256(payload).hexdigest()
    rows = iter(read_rows(io.BytesIO(payload), MAGIC))
    try:
        bootstrap, settings = next(rows), next(rows)
        if len(bootstrap) != 2 or bootstrap[0] != b'B' or len(bootstrap[1]) != 32 or len(settings) != 9 or settings[0] != b'S':
            raise EnvironmentError('Invalid manifest header')
        values = dict(zip(('version', 'platform', 'abi', 'runtime_loader', 'lean_executable',
                           'serializer', 'profile', 'trust_policy'), (b.decode() for b in settings[1:])))
        sequences = {tag: [] for tag in (b'P', b'Q', b'R', b'L', b'O', b'V')}
        dirs, files = [], []
        order = {tag: n for n, tag in enumerate((b'P', b'Q', b'R', b'L', b'O', b'V', b'D', b'F'))}
        phase = 0
        for row in rows:
            tag = row[0]
            if tag not in order or order[tag] < phase:
                raise EnvironmentError('Noncanonical manifest record order')
            phase = order[tag]
            if tag in sequences and len(row) == (3 if tag in (b'O', b'V') else 2):
                sequences[tag].append(tuple(b.decode() for b in row[1:]) if len(row) == 3 else row[1].decode())
            elif tag == b'D' and len(row) == 2:
                dirs.append(row[1].decode())
            elif tag == b'F' and len(row) == 3 and len(row[2]) == FILE_INFO.size:
                size, executable, digest = FILE_INFO.unpack(row[2])
                if executable not in (0, 1) or size > MAX_BYTES:
                    raise EnvironmentError('Invalid file metadata')
                path = relative(row[1].decode())
                files.append(_file(path, size, bool(executable), digest.hex()))
            else:
                raise EnvironmentError('Invalid manifest record')
        values.update((name, tuple(sequences[tag])) for tag, name in
                      ((b'P', 'project_inputs'), (b'Q', 'search_paths'), (b'R', 'runtime_paths'),
                       (b'L', 'plugins'), (b'O', 'options'), (b'V', 'variables')))
        # Every byte has a canonical representation (fixed integers, UTF-8,
        # ordered tags/paths). Re-encoding a decoded inventory adds no validation.
        manifest = object.__new__(Manifest)
        object.__setattr__(manifest, 'settings', Settings(**values))
        object.__setattr__(manifest, 'inventory', Inventory(tuple(dirs), tuple(files)))
        object.__setattr__(manifest, 'launcher_sha256', bootstrap[1].hex())
        manifest._validate()
        object.__setattr__(manifest, 'id', identity)
        return manifest
    except (StopIteration, UnicodeError, TypeError) as error:
        raise EnvironmentError('Malformed environment manifest') from error
