"""Intentional, local domain microbenchmarks against the preserved v2 modules.

Run from new/: python3 -B -m tests.benchmark_domain
No Lean, storage suite, model, network, or packaging work is performed.
"""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from statistics import median
import argparse
import json
import sys
from timeit import repeat
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from midas_formalizer import certificate_codec as old_codec
from midas_formalizer import declaration_context as old_context
from midas_formalizer import identity as old_identity
from midas_formalizer import persisted as old_json
from midas_formalizer import scoped_submission as old_scope
from midas_formalizer import selection as old_selection
from midas_formalizer import submission as old_submission
from midas_formalizer.serialization_cache import Memo as OldMemo

from midas.domain import encoding as new_codec
from midas.domain import identity as new_identity
from midas.domain import selection as new_selection
from midas.domain import submission as new_submission
from midas.domain.proofs import review_digest
from midas.domain.cache import Memo as NewMemo
from tests.test_domain_contracts import ENVIRONMENT, ENVIRONMENT_ID, FIXTURE, identity, name, selected_problem, submission
from tests.test_domain import artifact, uid


def measure(old, new, *, number=300):
    old()
    new()
    # Alternate the measurement order to reduce systematic warmup/drift bias.
    old_times, new_times = [], []
    for index in range(7):
        operations = ((old, old_times), (new, new_times)) if index % 2 == 0 else ((new, new_times), (old, old_times))
        for operation, values in operations:
            values.append(repeat(operation, number=number, repeat=1)[0] / number)
    previous, current = median(old_times), median(new_times)
    return {'v2_us': round(previous * 1e6, 2), 'new_us': round(current * 1e6, 2),
            'new_over_v2': round(current / previous, 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', action='append', help='Measure only named changed operations')
    args = parser.parse_args()
    shared = ['app', ['const', name('True'), []], ['nat', 3]]
    graph = {'items': [shared] * 128}
    payload = old_codec.encode(graph)
    json_payload = old_json.canonical({'items': list(range(128))})
    components = [old_codec.encode(FIXTURE['signature'][key]) for key in ('target', 'universes', 'context')]
    candidate = submission(scoped=True)
    candidate['imports'] = ['Mathlib', 'Std']
    old_candidate = dict(candidate, format='midas-candidates-v2')
    old_candidate_bytes = old_json.canonical(old_candidate)
    candidate_bytes = new_submission.encode_submission(candidate)
    definitions = [{'name': f'x{i}', 'type': 'Nat', 'value': '0'} for i in range(32)]
    reviewed = identity()
    interface = new_identity.encode_interface({'constant': name('x'), 'signature': FIXTURE['signature']})
    dependencies = tuple(new_identity.BoundDependency(reviewed, new_codec.canonical(name(f'x{i}')), interface)
                         for i in range(8))
    review = new_codec.canonical({'decision': 'approve', 'rationale': 'benchmark'})
    attempt, proof = uid('attempt'), artifact('proof')
    # The preserved v2 review recipe, with its database row already supplied.
    # No SQL or artifact reading is included in this domain-only comparison.
    def old_review_digest():
        return sha256(old_json.canonical({
            'attempt': attempt, 'obligation': reviewed.id,
            'candidate': [proof.id, proof.byte_count, proof.sha256], 'review': review.hex(),
            'dependencies': [[dep.identity.id, dep.parameter.hex(), dep.interface.hex(), dep.origin]
                             for dep in dependencies],
        })).hexdigest()
    source, declarations, inputs, selection = selected_problem()
    old_declarations = []
    for item in declarations:
        fields = asdict(item)
        for key in ('declaration', 'replacement'):
            fields[key] = old_selection.ByteSpan(**fields[key])
        old_declarations.append(old_selection.Declaration(**fields))
    old_declarations = tuple(old_declarations)
    old_request = old_selection.SelectionRequest('Main.lean', all_unfinished=True)
    imported = frozenset((old_json.canonical(name('Nat')), old_json.canonical(name('Eq'))))
    old_memo, new_memo = OldMemo(), NewMemo()
    cases = {
        'dag_encode': (lambda: old_codec.encode(graph), lambda: new_codec.encode(graph)),
        'dag_decode': (lambda: old_codec.decode(payload), lambda: new_codec.decode(payload)),
        'json_warm_admission': (lambda: old_json.load_json(json_payload), lambda: new_codec.load_json(json_payload)),
        'identity_envelope': (lambda: old_identity.ObligationIdentity(*components, ENVIRONMENT).reference_payload(),
                              lambda: new_identity.ObligationIdentity(*components, ENVIRONMENT_ID).reference_payload()),
        'selection': (lambda: old_selection.select_targets(old_request, source, old_declarations, imported_constants=imported),
                      lambda: new_selection.select_targets(selection.selected.request, source, declarations, imported_constants=imported)),
        'scoped_admission': (lambda: old_submission.validate_submission(old_candidate), lambda: new_submission.validate_submission(candidate)),
        'submission_load': (lambda: old_submission.load_submission(old_candidate_bytes), lambda: new_submission.load_submission(candidate_bytes)),
        'submission_save': (lambda: old_json.canonical(old_submission.validate_submission(old_candidate)),
                            lambda: new_submission.encode_submission(candidate)),
        'context_prefixes': (lambda: old_context.make('Root', definitions),
                             lambda: new_identity.make_context('Root', definitions)),
        'review_digest': (old_review_digest,
                          lambda: review_digest(attempt, reviewed.id, proof, review, dependencies)),
        'scope_resolution': (lambda: old_scope.resolve_entry(old_candidate, old_candidate['entries'][0]),
                             lambda: new_submission.resolve_entry(candidate, candidate['entries'][0])),
        'memo_hit': (lambda: old_memo.get(b'env', b'key', lambda: b'value'),
                     lambda: new_memo.get(b'env', b'key', lambda: b'value')),
    }
    signatures = dict(zip((decl.constant for decl in declarations), selection.signatures))
    old_signatures = {key: {field: new_codec.decode(getattr(sig, field))
                            for field in ('target', 'universes', 'context')}
                      for key, sig in signatures.items()}
    # Scope the v2 baseline to domain work: its environment/profile services are
    # represented by already admitted fixture data, with no toolchain or I/O.
    manifest = SimpleNamespace(id=inputs.environment_id, payload=ENVIRONMENT,
        inventory=SimpleNamespace(by_path={'project/Main.lean': {
            'size': len(source), 'sha256': selection.selected.source_sha256}}))
    spec = SimpleNamespace(project_inputs=('project/Main.lean',))
    environment_module = ModuleType('midas_formalizer.environment')
    environment_module.load_environment_manifest = lambda _: (manifest, spec)
    packaging_module = ModuleType('midas_formalizer.packaging')
    packaging_module.validate_profile = lambda *_: None
    with patch.dict(sys.modules, {'midas_formalizer.environment': environment_module,
                                  'midas_formalizer.packaging': packaging_module}):
        old_frozen = old_selection.freeze_selection(old_request, source, old_declarations,
                                                    imported, old_signatures, manifest)
        new_payload = new_selection.encode_selection(selection)
        cases.update({
            'freeze_selection': (
                lambda: old_selection.freeze_selection(old_request, source, old_declarations,
                                                        imported, old_signatures, manifest),
                lambda: new_selection.freeze_selection(selection.selected.request, source, declarations,
                                                        imported, signatures, inputs)),
            'freeze_and_encode': (
                lambda: old_selection.freeze_selection(old_request, source, old_declarations,
                                                        imported, old_signatures, manifest).payload,
                lambda: new_selection.encode_selection(new_selection.freeze_selection(
                    selection.selected.request, source, declarations, imported, signatures, inputs))),
            'load_selection': (
                lambda: old_selection.load_selection(old_frozen.payload, ENVIRONMENT),
                lambda: new_selection.load_selection(new_payload, inputs)),
        })
        if args.case and set(args.case) - cases.keys():
            parser.error('Unknown benchmark case')
        report = {label: measure(*operations) for label, operations in cases.items()
                  if not args.case or label in args.case}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
