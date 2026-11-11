"""Alternating local comparator packaging costs against v2; no Lean/model calls."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.comparator import overlay as old_overlay
from midas_formalizer.persisted import canonical as old_encode
from midas.leaneval.comparator import overlay
from midas.leaneval.workspace import evidence
from tests.benchmark_application import compare


def main():
    trusted = {'Submission.lean': b'sorry', 'Challenge.lean': b'challenge'}
    submission = {'Submission.lean': b'proof', **{f'Submission/H{i}.lean': b'-- helper' for i in range(100)}}
    compare('100 helper overlay', lambda: old_overlay(trusted, submission), lambda: overlay(trusted, submission), 300)
    log = b'compiler/comparator diagnostic\n'*100000
    compare('3 MB comparator evidence', lambda: old_encode(dict(stdout=log.decode(), stderr='')),
            lambda: evidence({}, ((b'O', log), (b'E', b'')), 8*1024**2), 20)


if __name__ == '__main__':
    main()
