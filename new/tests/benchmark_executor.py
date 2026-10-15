"""Alternating local supervisor comparisons against v2; no Lean/bubblewrap."""
from pathlib import Path
from statistics import median
import sys
from timeit import repeat

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer import process_runner as old
from midas.domain import ResourceLimits
from midas.executor import process as new


def measure(previous, current, count):
    previous()
    current()
    times = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1 - sample % 2):
            times[side].append(repeat((previous, current)[side], number=count, repeat=1)[0] / count)
    before, after = (median(t) for t in times)
    return before * 1e6, after * 1e6, after / before


def main():
    block = b'x' * 65536
    def capture(module):
        out = module._Capture(1024 * 1024)
        for i in range(16):
            out.append(i % 2, block)
        return out.result(0), out.result(1)
    old_limits, limits = old.Limits(memory_mib=128), ResourceLimits(90000, 128, 8 * 1024 * 1024, 64 * 1024 * 1024)
    command = (sys.executable, '-c', 'import os; os.write(1,b"x"*65536)')
    for label, previous, current, count in (
        ('capture_1mib', lambda: capture(old), lambda: capture(new), 300),
        ('fresh_python', lambda: old.run_process(command, old_limits), lambda: new._run_process(command, limits), 12),
    ):
        before, after, ratio = measure(previous, current, count)
        print(f'{label}: v2={before:.2f} us new={after:.2f} us ratio={ratio:.3f}')


if __name__ == '__main__':
    main()
