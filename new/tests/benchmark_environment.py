"""Alternating synthetic environment comparisons against v2; no Lean/processes."""
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory
from timeit import repeat

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer import environment as old
from midas.environment import Manifest, load_manifest, scan
from midas.environment.manifest import combine, _decode_manifest
from tests.test_environment import fixture, write


def measure(previous, current, count=20):
    previous()
    current()
    times = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1 - sample % 2):
            times[side].append(repeat((previous, current)[side], number=count, repeat=1)[0] / count)
    before, after = (median(t) for t in times)
    return before * 1e6, after * 1e6, after / before


def main():
    with TemporaryDirectory() as temporary:
        env = fixture(Path(temporary))
        for index in range(200):
            write(env.roots['project'] / f'Module{index}.lean', b'-- source\n' * 100)
        spec = old.EnvironmentSpec('linux-test', 'ELF-test', 'Lean (version 4.35.0-rc3, test)',
            'lean-structural-v1', 'toolchain/bin/lean', ('project/Main.lean',),
            ('checker/Resource.lean',), ('project', 'toolchain/lib/lean'))
        def current():
            inventory = combine({name: scan(path)[0] for name, path in env.roots.items()})
            # Match v2's second metadata scan for initial inventory stability.
            for path in env.roots.values():
                scan(path, hash_content=False)
            return Manifest(env.manifest.settings, inventory, env.manifest.launcher_sha256)
        previous = lambda: old.build_environment_manifest(env.roots, spec)
        old_manifest, new_manifest = previous(), current()
        binary = b''.join(new_manifest.chunks())
        for name, before, after in (
            ('scan_200_files', lambda: old._scan(env.roots['project'], hash_content=True), lambda: scan(env.roots['project'])),
            ('freeze_200_files', previous, current),
            ('load_cold', lambda: old._parse_environment_manifest(old_manifest.payload), lambda: _decode_manifest(binary)),
            ('load_warm', lambda: old.load_environment_manifest(old_manifest.payload), lambda: load_manifest(binary)),
        ):
            v2, rewritten, ratio = measure(before, after)
            print(f'{name}: v2={v2:.1f} us new={rewritten:.1f} us ratio={ratio:.3f}')


if __name__ == '__main__':
    main()
