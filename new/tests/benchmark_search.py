"""Replay simplicial-nerve queries on frozen source files, without Lean/network.

Usage: python -B -m tests.benchmark_search /path/to/mathlib-checkout
The cache lives in a temporary directory. Readings include verified excerpts.
"""
from hashlib import sha256
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, perf_counter
from uuid import UUID, uuid5
import sys

from midas.domain import ArtifactRef, OwnerVersion
from midas.tools import (Backend, HelperRequest, Scope, Search, SourceEntry, SourceIndex,
                        Stage, execute_search, search_summary)
from midas.tools import bm25
from tests.test_tools import Artifacts


def main():
    checkout = Path(sys.argv[1]).resolve(strict=True)
    paths, entries = {}, []
    start = perf_counter()
    for path in sorted((checkout/'Mathlib').rglob('*.lean')):
        data = path.read_bytes()
        digest = sha256(data).hexdigest()
        ref = ArtifactRef(str(uuid5(UUID(int=0), digest)), digest, len(data))
        entries.append(SourceEntry('mathlib/'+str(path.relative_to(checkout)), ref))
        paths[ref] = path
    index = SourceIndex('env_'+'2'*64, tuple(entries))
    class Reader:
        def read_artifact(self, ref, *, max_bytes):
            if ref.byte_count > max_bytes:
                raise ValueError('File exceeds allowance')
            return ref.verify(paths[ref].read_bytes(), max_bytes=max_bytes)
    reader = Reader()
    cancel, deadline = Event(), monotonic()+300
    scope = Scope(OwnerVersion('obl_'+'1'*64, 0), index.environment_id, Artifacts().save(b'context'))
    print(f'Frozen {len(entries)} files, {sum(e.artifact.byte_count for e in entries)/1024**2:.1f} MiB '
          f'in {perf_counter()-start:.3f}s', flush=True)
    prefixes = ('Mathlib/AlgebraicTopology/SimplicialNerve.lean', 'Mathlib/CategoryTheory')
    with TemporaryDirectory(prefix='midas-bm25-benchmark-') as cache:
        start = perf_counter()
        with bm25.database(index, reader, cancel, deadline, cache) as db:
            count = db.execute('SELECT count(*) FROM documents').fetchone()[0]
        print(f'Cold build {perf_counter()-start:.3f}s; {count} declarations; '
              f'{sum(p.stat().st_size for p in Path(cache).glob("*.sqlite"))/1024**2:.1f} MiB index', flush=True)
        for query in ('SimplicialNerve', 'simplicial nerve', 'SimplicialThickening', 'horn'):
            action = Search(query, Backend('frozen-source', prefixes))
            stage = Stage(HelperRequest(scope, action, 'continue').id, 0, scope, action)
            timings = []
            for _ in range(5):
                start = perf_counter()
                result = execute_search(stage, reader, cancel, deadline, index=index, cache_directory=cache)
                timings.append(perf_counter()-start)
            rows = search_summary(result, action)
            print(f'{query!r}: {median(timings)*1000:.2f} ms warm, {len(result.records)} hits, '
                  f'{len(chr(10).join(rows).encode())} display bytes', flush=True)
            if query == 'SimplicialNerve':
                for rank, hit in enumerate(result.records[:12], 1):
                    print(f'  {rank}: {", ".join(hit.names) or "(anonymous)"} [{hit.path}:{hit.line}]')

        # v2's original lexical scanner on the same files and archived query.
        # It returns 20 hits; the indexed implementation above returns up to 50.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from midas_formalizer.discovery import content_matches
        from midas_formalizer.source_names import declaration_names
        selected = [e for e in index.entries if any(e.path == 'mathlib/'+p or
                    e.path.startswith('mathlib/'+p+'/') for p in prefixes)]
        timings = []
        for _ in range(3):
            start = perf_counter()
            for entry in selected:
                source = reader.read_artifact(entry.artifact, max_bytes=bm25.FILE_BYTES).decode()
                content_matches(source.splitlines(keepends=True), declaration_names(source),
                                'SimplicialNerve', entry.path, 20, cancel, deadline)
            timings.append(perf_counter()-start)
        print(f'v2 source scan {len(selected)} files: {median(timings)*1000:.2f} ms warm', flush=True)


if __name__ == '__main__':
    main()
