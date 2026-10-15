"""Alternating native-evidence decoder comparison with v2; no Lean or JSON payloads."""
from pathlib import Path
from statistics import median
import sys
from timeit import repeat

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.source_cache import decode_frame as previous
from midas.checker.evidence import NATIVE, decode_output
from midas.domain import encoding as codec


def main():
    for size in (1024, 1024**2, 8*1024**2):
        proof = {'format': 'benchmark', 'source': 'by trivial\n--' + 'x'*100000}
        header = codec.encode({'format': 'midas-native-declarations-v2', 'proof': proof,
                               'artifacts': [{'id': 'producer', 'bytes': size}]})
        frame = NATIVE + len(header).to_bytes(8, 'little') + header + b'x'*size
        current = lambda: decode_output(frame, maximum=64*1024**2)
        old = lambda: previous(frame)
        assert old()['proof'] == current()[0]
        samples = ([], [])
        for sample in range(7):
            for side in (sample % 2, 1 - sample % 2):
                samples[side].append(repeat((old, current)[side], number=40, repeat=1)[0]/40)
        before, after = map(median, samples)
        print(f'native_{size}_bytes: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


if __name__ == '__main__':
    main()
