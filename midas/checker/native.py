"""Disposable compiled declarations and raw request framing; no proof authority."""
from hashlib import sha256
from pathlib import Path
import os
import tempfile

from ..domain import encoding as codec
from ..environment import safe_read

MAGIC = b'MIDAS_REQUEST_V1\n'
MAX_BYTES = 8 * 1024**2
MAX_FILES = 256


def frame(packet, artifacts=()):
    """Keep opaque native bytes out of the graph codec and import search path."""
    if not artifacts:
        return codec.encode(packet)
    seen, total, rows = set(), 0, []
    for ident, data in artifacts:
        if (type(ident) is not str or len(ident) != 64
                or any(c not in '0123456789abcdef' for c in ident) or ident in seen):
            raise ValueError('Invalid native request identity')
        if not isinstance(data, (bytes, memoryview)) or not 0 < len(data) <= MAX_BYTES:
            raise ValueError('Invalid native request bytes')
        seen.add(ident)
        total += len(data)
        rows.append({'id': ident, 'bytes': len(data)})
    if len(rows) > MAX_FILES or total > MAX_BYTES:
        raise ValueError('Native request batch exceeds limit')
    header = codec.encode({'packet': packet, 'artifacts': rows})
    size = len(MAGIC) + 8 + len(header) + total
    if size > 64 * 1024**2:
        raise ValueError('Native request exceeds byte limit')
    return b''.join((MAGIC, len(header).to_bytes(8, 'little'), header,
                     *(data for _, data in artifacts)))


def specification(packet, certificate, environment_id, runtime_id):
    prefix = sha256(codec.encode([environment_id, runtime_id, packet['source'],
        packet['logical'], packet['offset'], certificate.get('assumptions', []),
        certificate.get('imports', [])])).digest()
    ids, nodes = {}, []
    for declaration in certificate['declarations']:
        dependencies = [ids[codec.encode(n)] for n in declaration['requires']]
        # Native roots are normalized on output and renamed on restore. The
        # assigned assembly name does not change the declaration's cache key.
        digest = sha256(prefix)
        codec.hash_field(digest, codec.encode([declaration['signature'],
            declaration['source'], dependencies]))
        ident = digest.hexdigest()
        ids[codec.encode(declaration['name'])] = ident
        nodes.append({'id': ident, 'declaration': declaration})
    root = ['str', ['str', ['anonymous'], 'MidasCandidateResult'], 'cached' + nodes[-1]['id']]
    nodes[-1] = dict(nodes[-1], declaration=dict(nodes[-1]['declaration'], name=root))
    return {'nodes': nodes, 'root': root}


class DeclarationCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def load(self, spec):
        result, total, seen = [], 0, set()
        for node in spec['nodes']:
            if total >= MAX_BYTES or len(result) >= MAX_FILES:
                break
            ident = node['id']
            if ident in seen:
                continue
            seen.add(ident)
            try:
                data = safe_read(self.directory / ident, MAX_BYTES - total + 32)
            except (OSError, ValueError):
                continue
            if len(data) <= 32 or sha256(data[32:]).digest() != data[:32]:
                continue
            size = len(data) - 32
            if total + size > MAX_BYTES or len(result) >= MAX_FILES:
                continue
            total += size
            result.append((ident, memoryview(data)[32:]))
        return tuple(result)

    def store(self, artifacts, spec):
        allowed = {n['id'] for n in spec['nodes']}
        for ident, data in artifacts:
            if ident == 'producer':
                ident = spec['nodes'][-1]['id']
            if ident not in allowed or not 0 < len(data) <= MAX_BYTES:
                raise ValueError('Unexpected compiled declaration')
            temporary = None
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=self.directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(sha256(data).digest())
                    stream.write(data)
                os.replace(temporary, self.directory / ident)
            except OSError:
                pass  # A disposable cache write never invalidates a checked proof.
            finally:
                if temporary is not None:
                    try:
                        Path(temporary).unlink(missing_ok=True)
                    except OSError:
                        pass
