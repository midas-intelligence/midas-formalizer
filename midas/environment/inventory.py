"""Bounded flat inventories and descriptor-relative filesystem operations."""
from collections import OrderedDict
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
import os
from pathlib import Path
import stat
from struct import Struct
import threading
from types import MappingProxyType

MAX_BYTES = 1024**4
MAX_INVENTORY = 64 * 1024**2
MAX_ENTRIES = 500000
BLOCK = 1024 * 1024
# A Mathlib/toolchain validation visits about 171,000 files. A smaller LRU
# evicts the previous pass before it can reuse any digests. Entries contain
# only fixed-size stat stamps and SHA-256 strings, never file contents.
DIGEST_ENTRIES = 262144
U64 = Struct('<Q')
FILE_INFO = Struct('<QB32s')


class EnvironmentError(ValueError):
    pass


def relative(value, *, root=False):
    if root and value == '.':
        return value
    if (type(value) is not str or not value or len(value.encode()) > 4096
            or '\\' in value or '\x00' in value or value.startswith('/')
            or any(p in ('', '.', '..') for p in value.split('/'))
            or value.count('/') > 128):
        raise EnvironmentError('Expected a bounded canonical relative path')
    return value


def absolute(value):
    path = Path(value).absolute()
    if '..' in path.parts:
        raise EnvironmentError('Parent traversal in absolute path')
    return path


def open_root(root):
    root = absolute(root)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_file(path):
    path = absolute(path)
    parent = open_root(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise EnvironmentError('Expected regular file')
        return fd, parent
    except BaseException:
        os.close(parent)
        raise


def safe_read(path, maximum):
    fd, parent = _open_file(path)
    try:
        with os.fdopen(fd, 'rb') as source:
            before = stamp(os.fstat(fd))
            if not 0 <= before[3] <= maximum:
                raise EnvironmentError('File exceeds byte bound')
            value = source.read(before[3] + 1)
            if (len(value) != before[3] or before != stamp(os.fstat(fd))
                    or before != stamp(os.stat(Path(path).name, dir_fd=parent, follow_symlinks=False))):
                raise EnvironmentError('File changed during read')
            return value
    finally:
        os.close(parent)


_digests = OrderedDict()
_lock = threading.Lock()


def reset_cache():
    global _digests, _lock
    _digests, _lock = OrderedDict(), threading.Lock()


os.register_at_fork(after_in_child=reset_cache)


def _digest(fd, before, check):
    with _lock:
        found = _digests.get(before)
        if found is not None:
            _digests.move_to_end(before)
            return found
    digest, count = sha256(), 0
    while block := os.read(fd, BLOCK):
        check()
        count += len(block)
        if count > before[3]:
            raise EnvironmentError('File grew during hashing')
        digest.update(block)
    if count != before[3] or stamp(os.fstat(fd)) != before:
        raise EnvironmentError('File changed during hashing')
    value = digest.hexdigest()
    with _lock:
        _digests[before] = value
        if len(_digests) > DIGEST_ENTRIES:
            _digests.popitem(last=False)
    return value


def file_hash(path, *, maximum=MAX_BYTES, check=lambda: None):
    fd, parent = _open_file(path)
    try:
        before = stamp(os.fstat(fd))
        if before[3] > maximum:
            raise EnvironmentError('File exceeds byte bound')
        value = _digest(fd, before, check)
        if (before != stamp(os.fstat(fd)) or
                before != stamp(os.stat(Path(path).name, dir_fd=parent, follow_symlinks=False))):
            raise EnvironmentError('File changed during hashing')
        return value
    finally:
        os.close(fd)
        os.close(parent)


@dataclass(frozen=True, slots=True)
class File:
    path: str
    size: int
    executable: bool
    sha256: str

    def __post_init__(self):
        relative(self.path)
        if type(self.size) is not int or not 0 <= self.size <= MAX_BYTES or type(self.executable) is not bool:
            raise EnvironmentError('Invalid file metadata')
        if type(self.sha256) is not str or self.sha256 and (len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256)):
            raise EnvironmentError('Invalid file digest')

    def row(self):
        return (b'F', self.path.encode(), FILE_INFO.pack(self.size, self.executable, bytes.fromhex(self.sha256)))


def _file(path, size, executable, digest):
    """Internal construction after the scanner/copy/decoder has checked fields."""
    value = object.__new__(File)
    for name, item in (('path', path), ('size', size), ('executable', executable), ('sha256', digest)):
        object.__setattr__(value, name, item)
    return value


@dataclass(frozen=True)
class Inventory:
    directories: tuple[str, ...]
    files: tuple[File, ...]

    def __post_init__(self):
        if type(self.directories) is not tuple or type(self.files) is not tuple:
            raise EnvironmentError('Expected immutable inventory')
        if len(self.files) + len(self.directories) > MAX_ENTRIES:
            raise EnvironmentError('Inventory exceeds entry bound')
        dirs = set(self.directories)
        if tuple(sorted(dirs)) != self.directories or '.' not in dirs:
            raise EnvironmentError('Unsorted or missing inventory directories')
        for path in self.directories:
            relative(path, root=True)
            if path != '.' and (path.rpartition('/')[0] or '.') not in dirs:
                raise EnvironmentError('Unrecorded directory parent')
        previous = ''
        for item in self.files:
            if not isinstance(item, File) or item.path <= previous or item.path in dirs:
                raise EnvironmentError('Duplicate, unsorted or conflicting inventory file')
            if (item.path.rpartition('/')[0] or '.') not in dirs:
                raise EnvironmentError('Unrecorded file parent')
            previous = item.path

    @cached_property
    def by_path(self):
        return MappingProxyType({f.path: f for f in self.files})

    def rows(self):
        for path in self.directories:
            yield (b'D', path.encode())
        for item in self.files:
            yield item.row()


def _inventory(directories, files):
    """Scanner-produced order/parents are already validated during traversal."""
    value = object.__new__(Inventory)
    object.__setattr__(value, 'directories', directories)
    object.__setattr__(value, 'files', files)
    return value


def scan(root, *, hash_content=True, links=False, maximum=MAX_BYTES,
         max_entries=MAX_ENTRIES, check=lambda: None, exclude=(), suffixes=()):
    """No-follow traversal; shared input callers can opt into internal file aliases.

    Iteration depth, enumeration, bytes and file counts are independently bounded.
    The caller owns snapshot stability; stamps detect changes during admission.
    """
    if type(maximum) is not int or not 0 <= maximum <= MAX_BYTES or not 1 <= max_entries <= MAX_ENTRIES:
        raise EnvironmentError('Invalid inventory limits')
    root = absolute(root)
    files, dirs, observed = [], [], {}
    total = metadata = count = 0

    def visit(fd, prefix):
        nonlocal total, metadata, count
        check()
        before = stamp(os.fstat(fd))
        observed[prefix] = before
        dirs.append(prefix)
        with os.scandir(fd) as listing:
            names = []
            for entry in listing:
                check()
                count += 1
                path = entry.name if prefix == '.' else prefix + '/' + entry.name
                relative(path)
                metadata += len(path.encode()) + 160
                if count > max_entries or metadata > MAX_INVENTORY:
                    raise EnvironmentError('Inventory exceeds metadata/entry bound')
                if path not in exclude:
                    names.append((entry.name, path))
        for name, path in sorted(names):
            check()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if suffixes and not stat.S_ISDIR(info.st_mode) and not path.endswith(suffixes):
                continue
            original = stamp(info)
            target = None
            if links and stat.S_ISLNK(info.st_mode):
                target = (root / path).resolve(strict=True)
                if not target.is_relative_to(root) or not target.is_file():
                    raise EnvironmentError('External or directory input link')
                observed[path + '/@link'] = original
                info = target.stat()
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > maximum:
                    raise EnvironmentError('Inventory exceeds content byte bound')
                if target is None:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                else:
                    child, parent = _open_file(target)
                    os.close(parent)
            else:
                raise EnvironmentError('Symlink or special file in snapshot: ' + path)
            try:
                expected = stamp(info)
                if expected != stamp(os.fstat(child)):
                    raise EnvironmentError('Snapshot changed before open')
                if stat.S_ISDIR(info.st_mode):
                    visit(child, path)
                else:
                    observed[path] = expected
                    digest = _digest(child, expected, check) if hash_content else ''
                    files.append(_file(path, info.st_size, bool(info.st_mode & 0o111), digest))
                if (expected != stamp(os.fstat(child)) or
                        original != stamp(os.stat(name, dir_fd=fd, follow_symlinks=False))):
                    raise EnvironmentError('Snapshot changed during scan')
            finally:
                os.close(child)
        if before != stamp(os.fstat(fd)):
            raise EnvironmentError('Snapshot directory changed')

    fd = open_root(root)
    try:
        visit(fd, '.')
    finally:
        os.close(fd)
    return _inventory(tuple(sorted(dirs)), tuple(sorted(files, key=lambda f: f.path))), observed


def chunks(rows, magic):
    """Flat record stream: one-byte field count, then u64 lengths and raw fields."""
    total = len(magic)
    yield magic
    for row in rows:
        if not 1 <= len(row) <= 32:
            raise EnvironmentError('Invalid flat record')
        total += 1 + 8 * len(row) + sum(len(part) for part in row)
        if total + 1 > MAX_INVENTORY:
            raise EnvironmentError('Binary inventory exceeds byte bound')
        small = [bytes([len(row)])]
        for part in row:
            small.append(U64.pack(len(part)))
            if len(part) > 16384:
                yield b''.join(small)
                small = []
                yield part
            else:
                small.append(part)
        if small:
            yield b''.join(small)
    yield b'\0'


def read_rows(source, magic):
    total = count = 0

    def take(size):
        nonlocal total
        total += size
        if total > MAX_INVENTORY:
            raise EnvironmentError('Binary inventory exceeds byte bound')
        value = source.read(size)
        if len(value) != size:
            raise EnvironmentError('Truncated binary inventory')
        return value

    if take(len(magic)) != magic:
        raise EnvironmentError('Unsupported binary inventory')
    while size := take(1)[0]:
        count += 1
        if size > 32 or count > MAX_ENTRIES + 50000:
            raise EnvironmentError('Binary record count/width exceeds bound')
        yield tuple(take(U64.unpack(take(8))[0]) for _ in range(size))
    if source.read(1):
        raise EnvironmentError('Trailing inventory bytes')


def copy_file(source, destination, expected, *, check=lambda: None, links=False):
    """Copy/hash once; no hardlinks to a mutable source or shared installation."""
    source = Path(source)
    if links:
        source = source.resolve(strict=True)
    fd, parent = _open_file(source)
    try:
        with os.fdopen(fd, 'rb') as incoming:
            before = stamp(os.fstat(fd))
            if before[3] != expected.size or bool(before[2] & 0o111) != expected.executable:
                raise EnvironmentError('Copy source metadata changed')
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, count = sha256(), 0
            with destination.open('xb') as outgoing:
                while block := incoming.read(BLOCK):
                    check()
                    count += len(block)
                    if count > expected.size:
                        raise EnvironmentError('Copy source grew')
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fchmod(outgoing.fileno(), 0o555 if expected.executable else 0o444)
                os.fsync(outgoing.fileno())
            if (count != expected.size or expected.sha256 and digest.hexdigest() != expected.sha256
                    or before != stamp(os.fstat(fd)) or
                    before != stamp(os.stat(source.name, dir_fd=parent, follow_symlinks=False))):
                raise EnvironmentError('Copy source changed')
            return File(expected.path, count, expected.executable, digest.hexdigest())
    finally:
        os.close(parent)


def sync_directory(path):
    fd = open_root(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
