"""Compare cached inventory reuse with the previous warm decode path; no Lean.

Optionally pass a saved library.bin. Both paths still verify artifact bytes;
only the old path reconstructs the mutable graph on each lookup.
"""
from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import thread_time
from uuid import uuid4

from midas.checker.rebuild import inventory, load_inventory, modules, _INVENTORIES
from midas.checker.requests import read
from midas.domain import ArtifactRef
from midas.domain import encoding as codec


class Reader:
    def __init__(self, payload):
        self.payload = payload

    def read_artifact(self, reference, *, max_bytes):
        # A fresh byte object models storage reads without timing a disk cache.
        return memoryview(self.payload).tobytes()


def measure(previous, current):
    times = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1 - sample % 2):
            start = thread_time()
            for _ in range(3):
                (previous, current)[side]()
            times[side].append((thread_time() - start) / 3)
    return tuple(median(t) for t in times)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('inventory', nargs='?', type=Path)
    args = parser.parse_args()
    if args.inventory:
        with args.inventory.open('rb') as stream:
            payload = stream.read(codec.MAX_BYTES + 1)
        if len(payload) > codec.MAX_BYTES:
            raise ValueError('Inventory exceeds byte bound')
    else:
        payload = codec.encode([dict(name='Dep'+str(index), package='library', source=None,
            assets=[], options=[], compiled={'.olean': sha256(str(index).encode()).hexdigest()})
            for index in range(8967)])
    codec._GRAPH_ADMISSIONS.reset()
    _INVENTORIES.reset()
    start = thread_time()
    rows = inventory(payload)
    cold = thread_time() - start
    assert modules(codec.decode(payload)) == rows
    ref = ArtifactRef(str(uuid4()), sha256(payload).hexdigest(), len(payload))
    reader = Reader(payload)
    packet = {'inventory': ref.to_dict()}
    # Prior warm admissions skipped schema validation and canonical encoding,
    # but always decoded again. Exclude its negligible admission-cache lookup.
    previous = lambda: codec.decode(read(reader, ref))
    current = lambda: load_inventory(packet, reader)
    before, after = measure(previous, current)
    assert current() is rows
    print(f'modules={len(rows)} bytes={len(payload)} retained_cache_bytes={_INVENTORIES.bytes}')
    print(f'first_admission_cpu_s={cold:.6f}')
    print(f'verified_warm_load_cpu: previous={before*1000:.3f} ms '
          f'current={after*1000:.3f} ms speedup={before/after:.1f}x')


if __name__ == '__main__':
    main()
