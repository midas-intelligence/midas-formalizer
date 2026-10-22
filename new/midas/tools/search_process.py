"""Memory-bounded search/index worker and its small binary file protocol.

Only source references and paths cross the boundary; existing package sources
and SQLite postings are reused. No environment or whole-corpus serialization.
"""
import sys

if __name__ == '__main__':
    import resource
    bound = int(sys.argv[1]) * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (bound, bound))
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event

from midas.domain import ArtifactRef
from midas.domain import encoding as codec
from midas.executor import Outcome
from midas.tools import bm25
from midas.tools.protocol import REQUEST_BYTES, SEARCH_BYTES, read


def _write(stream, value, limit=REQUEST_BYTES):
    # Each metadata record is independently bounded, including on index builds.
    payload = b''.join(codec.record_parts(value, limit=limit))
    stream.write(len(payload).to_bytes(4, 'little'))
    stream.write(payload)


def _read(stream, limit=REQUEST_BYTES):
    size = stream.read(4)
    if len(size) != 4 or not 0 < int.from_bytes(size, 'little') <= limit:
        raise ValueError('Invalid search worker record size')
    payload = stream.read(int.from_bytes(size, 'little'))
    return codec.load_record(payload, limit=limit)


def _call(context, directory, header, rows=()):
    context.check()
    request = Path(directory)/'request'
    with request.open('wb') as stream:
        _write(stream, header)
        for row in rows:
            context.check()
            _write(stream, row, 16384)
    result = context.run_search(request)
    if result.outcome != Outcome.COMPLETED:
        detail = result.stderr.data[:2048].decode('utf-8', errors='replace')
        raise RuntimeError('Search worker '+(result.limit or result.outcome.value)+': '+detail)
    context.check()
    return codec.load_record(result.stdout.data, limit=SEARCH_BYTES)


def prepare_index(index, reader, path, context):
    """Build once from file capabilities, with bounded copying for other readers."""
    if (sum(e.artifact.byte_count for e in index.entries) > bm25.CORPUS_BYTES
            or any(e.artifact.byte_count > bm25.FILE_BYTES for e in index.entries)):
        raise ValueError('Source index byte allowance exceeded')
    context.check()
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix=path.stem+'-', suffix='.sqlite', dir=path.parent, delete=False) as file:
        partial = Path(file.name)
    try:
        return _prepare_index(index, reader, path, partial, context)
    finally:
        # The child may have been killed before its own finally block ran.
        for suffix in ('', '-journal', '-wal', '-shm'):
            Path(str(partial)+suffix).unlink(missing_ok=True)


def _prepare_index(index, reader, path, partial, context):
    with TemporaryDirectory(prefix='search-build-', dir=context.workspace) as temporary:
        def rows():
            for entry in index.entries:
                context.check()
                if hasattr(reader, 'artifact_path'):
                    source = reader.artifact_path(entry.artifact)
                else:
                    source = Path(temporary)/entry.artifact.id
                    source.write_bytes(read(reader, entry.artifact, bm25.FILE_BYTES))
                yield [entry.path, entry.artifact.to_dict(), entry.module, str(Path(source).absolute())]
        answer = _call(context, temporary,
            ['build', index.environment_id, str(path), len(index.entries), str(partial)], rows())
        if answer != ['built', index.search_key]:
            raise ValueError('Search index build differs from requested corpus')
    return path


def search_hits(index, reader, action, prefixes, context, *, cache_directory=None, prepared_path=None):
    with TemporaryDirectory(prefix='search-query-', dir=context.workspace) as temporary:
        path = Path(prepared_path).absolute() if prepared_path is not None else None
        fallback = Path(cache_directory or temporary)/(index.search_key+'.sqlite')
        paths = tuple(dict.fromkeys(p for p in (path, fallback.absolute()) if p is not None))
        header = ['query', [str(p) for p in paths], index.search_key, action.query,
                  list(prefixes), action.limit]
        answer = _call(context, temporary, header)
        if answer == ['missing']:
            prepare_index(index, reader, fallback, context)
            answer = _call(context, temporary, header)
        if type(answer) is not list or len(answer) != 2 or answer[0] != 'hits':
            raise ValueError('Invalid search worker hits')
        return tuple((*row[:-1], float.fromhex(row[-1])) for row in answer[1])


def hosted_search(stage, context):
    from midas.tools.search import Match
    with TemporaryDirectory(prefix='search-hosted-', dir=context.workspace) as temporary:
        answer = _call(context, temporary, ['hosted', stage.action.query, stage.action.limit])
        if type(answer) is not list or len(answer) != 2 or answer[0] != 'hosted':
            raise ValueError('Invalid hosted search worker result')
        return tuple(Match((name,)) for name in answer[1])


def _main(request):
    from midas.tools.search import SourceEntry, SourceIndex, hosted
    from midas.tools.protocol import Search, Backend
    cancel, deadline = Event(), float('inf')  # Parent supervisor owns the deadline.
    with Path(request).open('rb') as stream:
        header = _read(stream)
        if header[0] == 'build':
            _, environment_id, path, count, partial = header
            if type(count) is not int or not 0 <= count <= 200_000:
                raise ValueError('Invalid search source count')
            entries, paths = [], {}
            for _ in range(count):
                logical, reference, module, source = _read(stream, 16384)
                ref = ArtifactRef.from_dict(reference)
                entries.append(SourceEntry(logical, ref, module))
                paths[ref] = Path(source)
            index = SourceIndex(environment_id, tuple(entries))
            class Reader:
                def read_artifact(self, reference, *, max_bytes):
                    with paths[reference].open('rb') as source:
                        data = source.read(max_bytes+1)
                    return reference.verify(data, max_bytes=max_bytes)
            bm25.prepare(index, Reader(), cancel, deadline, Path(path), temporary_path=partial)
            answer = ['built', index.search_key]
        elif header[0] == 'query':
            _, paths, key, query, prefixes, limit = header
            path = next((Path(p) for p in paths if bm25._valid(Path(p), key, cancel, deadline)), None)
            answer = ['missing']
            if path is not None:
                db = bm25._connect(path, cancel, deadline, readonly=True)
                try:
                    hits = bm25.ranked(db, query, prefixes, limit=limit)
                    answer = ['hits', [[*row[:-1], row[-1].hex()] for row in hits]]
                finally:
                    db.close()
        elif header[0] == 'hosted':
            action = Search(header[1], Backend('leanexplore'), header[2])
            answer = ['hosted', [row.names[0] for row in hosted(action, cancel, deadline)]]
        else:
            raise ValueError('Unknown search worker operation')
        if stream.read(1):
            raise ValueError('Trailing search worker input')
    for part in codec.record_parts(answer, limit=SEARCH_BYTES):
        sys.stdout.buffer.write(part)


if __name__ == '__main__':
    try:
        _main(sys.argv[2])
    except MemoryError:
        sys.stderr.write('Search memory limit exceeded\n')
        sys.exit(2)
