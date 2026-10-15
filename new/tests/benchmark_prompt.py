"""Offline prompt construction comparison and large raw-text throughput.

The v2 comparison uses only small JSON controls/blocks (under 16 KiB). Large
snapshots exercise the new byte writer alone, without JSON library operations.
"""
from pathlib import Path
from statistics import median
import sys
from timeit import timeit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.strategy_prompts import build_blocks_wire
from midas.prompt import Options, Section, Snapshot, build, prover
from midas.domain.wire import decode
from tests.test_source import signature
from midas.prompt import subject


def compare(label, previous, current, count=40):
    samples = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1-sample % 2):
            samples[side].append(timeit((previous, current)[side], number=count)/count)
    before, after = map(median, samples)
    print(f'{label}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


def main():
    schema = decode(prover.SCHEMA, limit=16384)
    instruction = dict(effective=prover.INSTRUCTIONS.decode(), schema=schema)
    blocks = ['SOURCE\n' + 'x'*2048, 'CANDIDATE\n' + 'y'*2048, 'TURN\nfeedback']
    turn = Snapshot((Section('source', 'source', b'x'*2048), subject('goal', signature()),
                     Section('candidate', 'candidate', b'y'*2048),
                     Section('feedback', 'turn', b'feedback')))
    for transport in ('codex', 'azure_responses'):
        request = dict(transport=transport, provider='azure', account='test', model='model',
                       endpoint='https://example.test/responses', role='prover',
                       prompt_cache='disabled', wire={})
        options = Options(transport, 'model')
        compare(transport + '_small',
                lambda: build_blocks_wire(request, blocks, instruction, _legacy_display=True),
                lambda: build(turn, options, renderer=prover.render))
    # V2 above returns an intermediate dictionary; new also produces final wire
    # bytes and provenance hash. Exclude fixture construction from both timings.
    large = Snapshot((subject('goal', signature()),
                      Section('source', 'source', ('-- α source line\n'*65536).encode())))
    for transport in ('codex', 'azure_responses'):
        options = Options(transport, 'model')
        result = build(large, options, renderer=prover.render)
        duration = median(timeit(lambda: build(large, options, renderer=prover.render), number=10)/10
                          for _ in range(5))
        print(f'{transport}_large: {len(result.wire)} wire bytes in {duration*1000:.2f} ms')


if __name__ == '__main__':
    main()
