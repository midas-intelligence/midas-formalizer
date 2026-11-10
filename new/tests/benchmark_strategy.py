"""Offline strategy comparison. V2 JSON workloads stay below 16 KiB.

Large source exercises only the new implementation, with JSON libraries disabled.
These timings measure local interpretation/retention, not model or Lean latency.
"""
from pathlib import Path
from statistics import median
import sys
from timeit import timeit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.strategy_protocol import decode as old_decode
from midas_formalizer.strategy_protocol import parse_response, response_guidance
from midas_formalizer.persisted import canonical
from midas_formalizer import strategy_memory as old_memory
from midas.strategy import Lesson, Memory, Update, expand, interpret, revise, update_memory
from tests.test_strategy import context, model_json, proof, response
from tests.test_domain import uid


def compare(label, previous, current, count=100):
    samples = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1 - sample % 2):
            samples[side].append(timeit((previous, current)[side], number=count) / count)
    before, after = map(median, samples)
    print(f'{label}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


def main():
    for size in (1024, 8192):
        action = proof()
        action['proof_body'] = 'by\n  -- ' + 'x'*size + '\n  rfl'
        payload = response(action)
        assert len(payload) < 16384
        compare(f'interpret_{size}', lambda: old_decode(payload),
                lambda: interpret(payload, role='prover', contract='prover-action-v5'))
    ctx = context()
    binding = dict(obligation='obl_'+'a'*64, generation=0, context_sha256='b'*64)
    for size in (16, 1024, 8192):
        action = proof()
        action['proof_body'] = 'by\n-- ' + 'x'*size + '\nrfl'
        entry = dict(key='parent', subject=dict(kind='target', name='goal'), proof=action['proof_body'],
                     dependencies=[], review=None, revision_of=None)
        submission = dict(format='midas-candidates-v1', submission_id=uid('candidate'),
                          selection_id=ctx.selection_id, entries=[entry])
        raw = model_json(dict(format='midas-strategy-response-v1', binding=binding, mode='candidate',
            rationale=action['rationale'], memory_update={}, submission=submission, proof=action['proof'],
            children=[], proof_departures=[]))
        payload = response(action)
        assert max(len(payload), len(raw)) < 16384
        def old():
            parsed = parse_response(raw, expected=binding, selection_id=ctx.selection_id,
                                    submission_id=uid('candidate'), required_targets=('goal',))
            return canonical(parsed['submission']), canonical(response_guidance(parsed))
        def current():
            return expand(interpret(payload, role='prover', contract='prover-action-v5').value, ctx, uid('candidate'))
        compare(f'candidate_admission_{size}', old, current)
    # Same semantic memory update and complete lessons. New also increments an
    # explicit version and returns immutable, binary-persistable records.
    before = old_memory.empty()
    before['lessons'] = [dict(id=f'lesson{i}', text='An approach and its limitation. '*20) for i in range(12)]
    after = Memory(lessons=tuple(Lesson(x['id'], x['text']) for x in before['lessons']))
    old_change = dict(upsert=[dict(id='new', text='The successor case requires rewriting.')])
    new_change = Update((Lesson('new', old_change['upsert'][0]['text']),))
    assert len(model_json(before)) < 16384
    compare('memory_update', lambda: old_memory.update(before, old_change),
            lambda: update_memory(after, new_change))
    action = proof()
    action['proof_body'] = 'by\n  -- ' + 'x'*900000 + '\n  rfl'
    payload, ctx = response(action), context()
    draft = expand(action, ctx, uid('large-base'))
    revision = dict(mode='revise', rationale='Use a different tactic.',
                    edits=[dict(path='/entries/parent/proof', old='  rfl', new='  simp')])
    for label, run in (
        ('large_interpret_expand', lambda: expand(interpret(payload, role='prover', contract='prover-action-v5').value, ctx, uid('large'))),
        ('large_exact_revision', lambda: revise(revision, draft, ctx, uid('revision')))):
        with patch('json.loads', side_effect=AssertionError), patch('json.dumps', side_effect=AssertionError):
            elapsed = median(timeit(run, number=10) / 10 for _ in range(5))
        print(f'{label}: {len(payload)} response bytes in {elapsed*1000:.2f} ms')


if __name__ == '__main__':
    main()
