"""Private staging and durable flat archives, with bounded chunk I/O."""
from dataclasses import dataclass
from hashlib import sha256
import io
import os
from pathlib import Path
import stat
import threading
from uuid import UUID, uuid4, uuid5

import zstandard as zstd

from ..domain import ArtifactRef
from ..domain.encoding import CHUNK_HEADER, CHUNK_MAGIC, CHUNK_LIMIT, INLINE_LIMIT, SEGMENT_LIMIT
from ..domain.records import natural
from .codec import pack
from .protocol import StorageError

MAGIC = b'MIDASAR3'
NAMESPACE = UUID('25bc07e4-bbde-413b-baf7-d87d3cd28547')


class ArtifactError(StorageError):
    pass


def checkpoint(stage):
    """Fault-injection seam for publication/recovery tests."""


def regular(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ArtifactError('Expected a regular file')
    return fd


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stamp(path):
    s = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode):
        raise ArtifactError('Expected a regular archive')
    return pack([s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns])


def reference(digest, size):
    return ArtifactRef(str(uuid5(NAMESPACE, digest)), digest, size)


def source_reader(source):
    if type(source) is bytes:
        return io.BytesIO(source)
    if not callable(getattr(source, 'read', None)):
        raise ValueError('Expected bytes or a binary stream')
    return source


def fill(source, count):
    parts, remaining = [], count
    while remaining:
        part = source.read(remaining)
        if type(part) is not bytes or len(part) > remaining:
            raise ArtifactError('Binary stream returned invalid or oversized data')
        if not part:
            break
        parts.append(part)
        remaining -= len(part)
    return b''.join(parts)


class _Region:
    def __init__(self, source, size):
        self.source, self.remaining = source, size

    def read(self, count):
        if self.remaining is not None:
            count = min(count, self.remaining)
        data = self.source.read(count)
        if self.remaining is not None:
            self.remaining -= len(data)
        return data


def stream_file(path, ref, *, offset=0, stored_size=None):
    """Each chunk is verified before yielding; exhaust to verify the whole artifact."""
    total, digest = 0, sha256()
    with os.fdopen(regular(path, os.O_RDONLY), 'rb') as file:
        if stored_size is not None and os.fstat(file.fileno()).st_size < offset+stored_size:
            raise ArtifactError('Truncated artifact segment')
        file.seek(offset)
        source = _Region(file, stored_size)
        if fill(source, len(MAGIC)) != MAGIC:
            raise ArtifactError('Unknown archive format')
        decoder = zstd.ZstdDecompressor()
        while total < ref.byte_count:
            header = fill(source, CHUNK_HEADER.size)
            if len(header) != CHUNK_HEADER.size:
                raise ArtifactError('Truncated archive header')
            magic, codec, reserved, raw_size, encoded_size, expected = CHUNK_HEADER.unpack(header)
            if (magic != CHUNK_MAGIC or codec not in (0, 1) or reserved != b'\0'*7
                    or not 0 < raw_size <= min(CHUNK_LIMIT, ref.byte_count-total)
                    or not 0 < encoded_size <= raw_size or (codec == 0 and encoded_size != raw_size)):
                raise ArtifactError('Invalid archive chunk')
            encoded = fill(source, encoded_size)
            if len(encoded) != encoded_size:
                raise ArtifactError('Truncated archive chunk')
            try:
                if codec and zstd.get_frame_parameters(encoded).content_size != raw_size:
                    raise ArtifactError('Compressed frame exceeds declared output size')
                raw = decoder.decompress(encoded, max_output_size=raw_size, allow_extra_data=False) if codec else encoded
            except zstd.ZstdError as exc:
                raise ArtifactError('Invalid compressed archive chunk') from exc
            if len(raw) != raw_size or sha256(raw).digest() != expected:
                raise ArtifactError('Archive chunk checksum differs')
            total += len(raw)
            digest.update(raw)
            yield raw
        if source.read(1) or digest.hexdigest() != ref.sha256:
            raise ArtifactError('Archive length or aggregate checksum differs')


@dataclass(frozen=True, slots=True)
class Durable:
    reference: ArtifactRef
    inline: bytes | None
    stamp: bytes | None
    authority: object
    location: tuple[str, int, int] | None = None


class Segments:
    """Append at artifact boundaries; chunks remain bounded for arbitrarily large inputs."""
    def __init__(self, directory, latest=None):
        self.directory, self.active = directory, latest

    def append(self, source, *, max_bytes, expected=None, authority, known_digest=None):
        natural(max_bytes, 'Artifact limit')
        if expected is not None and expected.byte_count > max_bytes:
            raise ArtifactError('Artifact exceeds publication limit')
        known_digest = known_digest if type(source) is bytes else None
        source = source_reader(source)
        first = fill(source, min(INLINE_LIMIT+1, max_bytes+1))
        if len(first) > max_bytes:
            raise ArtifactError('Artifact exceeds publication limit')
        if len(first) <= INLINE_LIMIT:
            digest = known_digest or sha256(first).hexdigest()
            ref = expected or reference(digest, len(first))
            if (ref.sha256, ref.byte_count) != (digest, len(first)):
                raise ArtifactError('Published bytes differ from reference')
            return Durable(ref, first, None, authority)
        path = self.directory/(self.active+'.seg') if self.active else None
        if path is None or path.stat().st_size >= SEGMENT_LIMIT:
            self.active = str(uuid4())
            path = self.directory/(self.active+'.seg')
            flags, created = os.O_CREAT | os.O_EXCL, True
        else:
            flags, created = 0, False
        total, digest = 0, sha256()
        compressor = zstd.ZstdCompressor(level=3, write_content_size=True)
        try:
            with os.fdopen(regular(path, os.O_WRONLY | os.O_APPEND | flags), 'ab') as output:
                offset = output.tell()
                output.write(MAGIC)
                raw = first
                while raw:
                    raw += fill(source, min(CHUNK_LIMIT, max_bytes-total+1)-len(raw))
                    if total+len(raw) > max_bytes:
                        raise ArtifactError('Artifact exceeds publication limit')
                    if known_digest is None:
                        digest.update(raw)
                    encoded = compressor.compress(raw)
                    compressed = len(encoded) < len(raw)
                    if not compressed:
                        encoded = raw
                    output.write(CHUNK_HEADER.pack(CHUNK_MAGIC, int(compressed), b'\0'*7,
                                                   len(raw), len(encoded), sha256(raw).digest()))
                    output.write(encoded)
                    total += len(raw)
                    raw = fill(source, min(CHUNK_LIMIT, max_bytes-total+1))
                computed = known_digest or digest.hexdigest()
                ref = expected or reference(computed, total)
                if (ref.sha256, ref.byte_count) != (computed, total):
                    raise ArtifactError('Published bytes differ from reference')
                output.flush()
                length = output.tell()-offset
                os.fsync(output.fileno())
                checkpoint('file_synced')
            if created:
                sync_directory(self.directory)
            checkpoint('directory_synced')
            return Durable(ref, None, stamp(path), authority, (self.active, offset, length))
        except BaseException:
            # Another append must first make this segment's directory entry
            # durable too; use a fresh segment after an interrupted creation.
            if created:
                self.active = None
            raise


def _stage(directory, source, max_bytes, authority):
    """Private raw bytes need no compression or durability before publication."""
    if type(source) is bytes:
        if len(source) > max_bytes:
            raise ArtifactError('Artifact exceeds staging limit')
        ref = reference(sha256(source).hexdigest(), len(source))
        if len(source) <= INLINE_LIMIT:
            return Durable(ref, source, None, authority)
        path = directory/(ref.id+'.raw')
        try:
            fd = regular(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            for _ in _raw_stream(path, ref):
                pass
        else:
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(source)
            except BaseException:
                path.unlink()
                raise
        return Durable(ref, None, None, authority)
    source = source_reader(source)
    first = fill(source, min(INLINE_LIMIT+1, max_bytes+1))
    if len(first) > max_bytes:
        raise ArtifactError('Artifact exceeds publication limit')
    if len(first) <= INLINE_LIMIT:
        ref = reference(sha256(first).hexdigest(), len(first))
        return Durable(ref, first, None, authority)
    temporary = directory / (str(uuid4()) + '.tmp')
    try:
        total, digest = 0, sha256()
        with os.fdopen(regular(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as output:
            raw = first
            while raw:
                raw += fill(source, min(CHUNK_LIMIT, max_bytes-total+1)-len(raw))
                if total+len(raw) > max_bytes:
                    raise ArtifactError('Artifact exceeds publication limit')
                digest.update(raw)
                output.write(raw)
                total += len(raw)
                raw = fill(source, min(CHUNK_LIMIT, max_bytes-total+1))
            ref = reference(digest.hexdigest(), total)
        destination = directory / (ref.id + '.raw')
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            for _ in _raw_stream(destination, ref):
                pass
        temporary.unlink()
        return Durable(ref, None, None, authority)
    finally:
        if temporary.exists():
            temporary.unlink()


def _raw_stream(path, ref):
    digest, total = sha256(), 0
    with os.fdopen(regular(path, os.O_RDONLY), 'rb') as source:
        while total < ref.byte_count:
            data = source.read(min(CHUNK_LIMIT, ref.byte_count-total))
            if not data:
                raise ArtifactError('Truncated staged artifact')
            digest.update(data)
            total += len(data)
            yield data
        if source.read(1) or digest.hexdigest() != ref.sha256:
            raise ArtifactError('Staged artifact checksum differs')


class Staging:
    """One private worker scope; byte and artifact quotas span all its writes."""
    def __init__(self, directory, *, max_bytes, max_artifacts=1024):
        natural(max_bytes, 'Staging byte limit')
        natural(max_artifacts, 'Staging artifact limit', positive=True)
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700)
        self.max_bytes, self.max_artifacts = max_bytes, max_artifacts
        self._values, self._used, self._closed = {}, 0, False
        self._pid, self._thread = os.getpid(), None
        self._lock = threading.Lock()

    def write_bounded(self, data, *, max_bytes):
        if type(data) is not bytes:
            raise ValueError('Staging requires immutable bytes')
        return self.write_stream(data, max_bytes=max_bytes)

    def write_stream(self, source, *, max_bytes):
        natural(max_bytes, 'Staging write limit')
        with self._lock:
            if self._closed or os.getpid() != self._pid:
                raise ArtifactError('Staging is closed or belongs to another process')
            current = threading.get_ident()
            if self._thread not in (None, current):
                raise ArtifactError('Staging writes belong to one worker thread')
            self._thread = current
            if len(self._values) >= self.max_artifacts:
                raise ArtifactError('Staging artifact quota exceeded')
            durable = _stage(self.directory, source, min(max_bytes, self.max_bytes-self._used), self)
            ref = durable.reference
            if ref.id not in self._values:
                self._values[ref.id] = durable
                self._used += ref.byte_count
            return ref

    def read_artifact(self, ref, *, max_bytes):
        natural(max_bytes, 'Artifact read limit')
        if ref.byte_count > max_bytes:
            raise ArtifactError('Artifact exceeds read limit')
        return b''.join(self.stream(ref))

    def stream(self, ref):
        with self._lock:
            if self._closed or os.getpid() != self._pid:
                raise ArtifactError('Staging is closed or belongs to another process')
            saved = self._values.get(ref.id)
            if saved is None or saved.reference != ref:
                raise ArtifactError('Unknown or mismatched staged artifact')
            if saved.inline is not None:
                yield ref.verify(saved.inline, max_bytes=INLINE_LIMIT)
            else:
                yield from _raw_stream(self.directory / (ref.id+'.raw'), ref)

    def close(self):
        with self._lock:
            if os.getpid() != self._pid:
                raise ArtifactError('Staging belongs to another process')
            if not self._closed:
                for value in self._values.values():
                    if value.inline is None:
                        (self.directory / (value.reference.id+'.raw')).unlink()
                self.directory.rmdir()
                self._closed = True


class ChunkReader:
    """Adapt a bounded chunk iterator to a binary read capability."""
    def __init__(self, chunks):
        self.chunks, self.pending = iter(chunks), b''

    def read(self, count):
        if count < 0:
            raise ValueError('Unbounded reads are forbidden')
        if not count:
            return b''
        if not self.pending:
            self.pending = next(self.chunks, b'')
        result, self.pending = self.pending[:count], self.pending[count:]
        return result
