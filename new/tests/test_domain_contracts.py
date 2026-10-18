"""Acceptance of the remaining domain contracts; all evidence is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import select
from struct import pack, unpack_from
import threading
import unittest
from unittest.mock import patch

from midas.domain import encoding as codec
from midas.domain import selection as selection_module
from midas.domain.cache import Memo
from midas.domain.identity import (
    BoundDependency, ObligationIdentity, decode_interface, encode_interface,
    load_identity, make_context, validate_context, validate_dependencies,
    validate_identity, validate_signature,
)
from midas.domain.proofs import accept_joint, joint_coordinator, joint_targets, validate_joint_submission
from midas.domain.records import DependencyOrigin
from midas.domain.selection import (
    ByteSpan, Declaration, SelectionInputs, SelectionRequest, Signature, SourceBinding,
    encode_selection, freeze_selection, load_selection, select_targets,
)
from midas.domain.submission import (
    encode_submission, entry_subject, lexical_scope, load_submission, matches_subject,
    resolve_entry, validate_submission,
)
from midas.domain import (
    AdmittedCheck, Candidate, CheckKind, Dependency, DomainError, OwnerVersion,
    ProofGraph, ProofNode, ProofState, accept_checked_proof,
)
from tests.test_domain import artifact, receipt, uid

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/domain_v2.json').read_text())
ENVIRONMENT = bytes.fromhex(FIXTURE['environment_hex'])
ENVIRONMENT_ID = 'env_' + sha256(ENVIRONMENT).hexdigest()


def name(value):
    return ['str', ['anonymous'], value]


def identity(signature=None, *, environment_id=ENVIRONMENT_ID, interfaces=()):
    signature = signature or FIXTURE['signature']
    return ObligationIdentity(*(codec.encode(signature[key]) for key in ('target', 'universes', 'context')),
                              environment_id, interfaces)


def submission(*, scoped=False):
    value = {'format': 'midas-scoped-candidates' if scoped else 'midas-candidates',
             'submission_id': uid('submission'), 'selection_id': 'sel_' + 'a' * 64,
             'entries': [{'key': 'root', 'subject': {'kind': 'target', 'name': 'root'},
                          'proof': 'by trivial', 'dependencies': [], 'review': None, 'revision_of': None}]}
    if scoped:
        value['definitions'] = [{'name': 'x', 'kind': 'abbrev', 'type': 'Nat', 'value': '0'}]
        value['entries'][0]['definition_scope'] = {'statement': 0, 'proof': 1}
    return value


def selected_problem():
    source = b'def witness : Nat := sorry\ntheorem property : witness = witness := sorry\n'
    split = source.index(b'\n') + 1
    witness, prop, nat, eq = (name(s) for s in ('witness', 'property', 'Nat', 'Eq'))
    encoded = {s: codec.canonical(v) for s, v in (('witness', witness), ('property', prop), ('Nat', nat), ('Eq', eq))}
    first = Declaration('witness', encoded['witness'], 'def', ByteSpan(0, split),
                        ByteSpan(source.index(b'sorry'), source.index(b'sorry') + 5), True,
                        type_references=(encoded['Nat'],))
    second = Declaration('property', encoded['property'], 'theorem', ByteSpan(split, len(source)),
                         ByteSpan(source.rindex(b'sorry'), source.rindex(b'sorry') + 5), True,
                         type_references=(encoded['Eq'], encoded['Nat'], encoded['witness']))
    witness_sig = {'target': ['const', nat, []], 'universes': [], 'context': ['closed-telescope']}
    prop_sig = {'target': ['app', ['app', ['app', ['const', eq, [['succ', ['zero']]]],
                                               ['const', nat, []]], ['const', witness, []]], ['const', witness, []]],
                'universes': [], 'context': ['closed-telescope']}
    inputs = SelectionInputs(ENVIRONMENT_ID, (SourceBinding('Main.lean', sha256(source).hexdigest(), len(source)),))
    selection = freeze_selection(SelectionRequest('Main.lean', all_unfinished=True), source, (first, second),
                                 frozenset((encoded['Nat'], encoded['Eq'])),
                                 {encoded['witness']: Signature(*(codec.encode(witness_sig[k]) for k in ('target', 'universes', 'context'))),
                                  encoded['property']: Signature(*(codec.encode(prop_sig[k]) for k in ('target', 'universes', 'context')))}, inputs)
    return source, (first, second), inputs, selection


class EncodingTests(unittest.TestCase):
    def test_graph_admission_reuses_bytes_without_sharing_mutations_or_limits(self):
        shared = ['immutable admission']
        payload = codec.encode([shared, shared])
        codec._GRAPH_ADMISSIONS.reset()
        with patch.object(codec, 'encode', wraps=codec.encode) as encode:
            first = codec.decode(payload)
            self.assertIs(first[0], first[1])
            first[0].append('caller mutation')
            second = codec.decode(payload)
            self.assertEqual(second, [shared, shared])
            self.assertIs(second[0], second[1])
            self.assertIsNot(first[0], second[0])
            self.assertEqual(encode.call_count, 1)
            with self.assertRaises(codec.GraphLimit):
                codec.decode(payload, limit=len(payload)-1)
        # Duplicate/unused nodes cannot acquire admission, including after retry.
        bad = codec.MAGIC + b'\x03\x00\x00\x05\x01\x00\x02'
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'Noncanonical'):
                codec.decode(bad)

    def test_binary_identity_fixture(self):
        self.assertEqual(codec.encode(FIXTURE['graph_value']).hex(), FIXTURE['graph_hex'])
        value = codec.decode(bytes.fromhex(FIXTURE['graph_hex']))
        self.assertIs(value['z'][0], value['z'][1])
        current = identity()
        self.assertEqual(current.id, FIXTURE['identity_id'])
        self.assertEqual(current.payload().hex(), FIXTURE['identity_envelope_hex'])
        self.assertEqual(current.reference_payload().hex(), FIXTURE['identity_reference_hex'])

    def test_dag_stays_compact_and_tree_expansion_is_bounded(self):
        value = ['leaf']
        for _ in range(50):
            value = [value, value]
        payload = codec.encode(value)
        self.assertLess(len(payload), 400)
        decoded = codec.decode(payload)
        self.assertIs(decoded[0], decoded[1])
        with self.assertRaises(codec.GraphLimit):
            codec.require_tree_budget(decoded, limit=1024)

    def test_cycles_depth_limits_and_malformed_graphs(self):
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(ValueError):
            codec.encode(cycle)
        deep = None
        for _ in range(codec.MAX_DEPTH + 1):
            deep = [deep]
        with self.assertRaises(codec.GraphLimit):
            codec.encode(deep)
        payload = codec.encode({'a': 1})
        for invalid in (payload[:-1], payload + b'x', codec.MAGIC + b'\x81\x00',
                        codec.MAGIC + b'\x01\x05\x01\x00\x00'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                codec.decode(invalid)
        with self.assertRaises(codec.GraphLimit):
            codec.decode(payload, limit=len(payload) - 1)

    def test_json_admission_rejects_noncanonical_duplicate_nonfinite_and_mutation(self):
        for payload in (b'{"a":1,"a":2}', b'{ "a":1}', b'{"z":0,"a":1}', b'NaN', b'1e999', b'"\\ud800"'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                codec.load_json(payload)
        payload = codec.canonical({'a': [1]})
        codec.load_json(payload)['a'].append(2)
        self.assertEqual(codec.load_json(payload), {'a': [1]})
        with self.assertRaises(ValueError):
            codec.load_json(payload, limit=len(payload) - 1)

    def test_binary_graph_and_interface_byte_caps(self):
        payload = codec.encode(FIXTURE['signature'])
        self.assertEqual(codec.decode(payload), FIXTURE['signature'])
        self.assertEqual(decode_interface(encode_interface({'constant': name('T'), 'signature': FIXTURE['signature']})),
                         {'constant': name('T'), 'signature': FIXTURE['signature']})
        with self.assertRaises(codec.GraphLimit):
            encode_interface('x' * (1024**2))
        with self.assertRaises(ValueError):
            codec.decode(b'{\"legacy\":true}')


class IdentityTests(unittest.TestCase):
    def test_identity_loader_checks_environment_claim_and_expected_id(self):
        payload = bytes.fromhex(FIXTURE['identity_reference_hex'])
        restored = load_identity(payload, environment_id=ENVIRONMENT_ID, expected_id=FIXTURE['identity_id'])
        self.assertEqual(restored, identity())
        for kwargs in ({'environment_id': 'env_' + 'f' * 64}, {'environment_id': ENVIRONMENT_ID, 'expected_id': 'obl_' + '0' * 64}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                load_identity(payload, **kwargs)
        altered = codec.decode(payload)
        altered['obligation'] = 'obl_' + '0' * 64
        with self.assertRaises(ValueError):
            load_identity(codec.encode(altered), environment_id=ENVIRONMENT_ID)

    def test_cached_identity_has_detached_public_state(self):
        payload = bytes.fromhex(FIXTURE['identity_reference_hex'])
        first = load_identity(payload, environment_id=ENVIRONMENT_ID)
        first.__dict__['id'] = 'corrupted'
        self.assertEqual(load_identity(payload, environment_id=ENVIRONMENT_ID).id, FIXTURE['identity_id'])

    def test_binding_depth_universes_local_instances_and_context_names(self):
        base = {'target': ['bvar', 0], 'universes': [], 'context': ['closed-telescope']}
        with self.assertRaises(ValueError):
            validate_signature(base)
        local = ['local-context', [['local', name('h'), 'explicit', 'default', ['sort', ['zero']]]],
                 [['local-instance', name('h'), ['bvar', 0]]]]
        validate_signature(dict(base, context=local))
        bad_local = deepcopy(local)
        bad_local[2][0][2] = ['sort', ['zero']]
        with self.assertRaises(ValueError):
            validate_signature(dict(base, context=bad_local))
        sig = {'target': ['sort', ['param', name('u')]], 'universes': [], 'context': ['closed-telescope']}
        with self.assertRaises(ValueError):
            validate_signature(sig)
        validate_signature(dict(sig, universes=[name('u')]))
        with self.assertRaises(ValueError):
            validate_signature(dict(sig, universes=[name('u'), name('u')]))
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(ValueError):
            validate_signature(dict(base, target=cycle))

    def test_all_supported_expression_forms_and_invalid_tags(self):
        typ = ['sort', ['zero']]
        for target in (['sort', ['imax', ['zero'], ['max', ['zero'], ['succ', ['zero']]]]],
                       ['lam', name('x'), 'implicit', typ, ['bvar', 0]],
                       ['forall', name('x'), 'instance', typ, ['bvar', 0]],
                       ['let', name('x'), typ, typ, ['bvar', 0], False],
                       ['proj', name('Pair'), 0, typ], ['nat', 0], ['string', 'α']):
            with self.subTest(target=target):
                validate_signature({'target': target, 'universes': [], 'context': ['closed-telescope']})
        with self.assertRaises(ValueError):
            validate_signature({'target': ['mvar', 'x'], 'universes': [], 'context': ['closed-telescope']})
        with self.assertRaises(ValueError):
            validate_identity(replace(identity(), serializer='unknown'))

    def test_named_context_hashes_bind_scope_kind_and_order(self):
        definitions = [{'name': 'x', 'type': 'Nat', 'value': '0'}, {'name': 'y', 'type': 'Nat', 'value': 'x'}]
        first = make_context('Root', definitions[:1])
        extended = make_context('Root', definitions)
        self.assertEqual(first[2], extended[2][:1])
        validate_context(extended)
        self.assertNotEqual(first, make_context('Other', definitions[:1]))
        self.assertNotEqual(first, make_context('Root', [dict(definitions[0], kind='abbrev')]))
        edited = deepcopy(extended)
        edited[2][0]['value'] = '1'
        with self.assertRaises(ValueError):
            validate_context(edited)

    def test_dependency_signatures_aliases_and_frozen_order(self):
        child = identity()
        sig2 = dict(FIXTURE['signature'], target=['const', name('False'), []])
        second = identity(sig2)
        first_interface = encode_interface({'constant': name('a'), 'signature': FIXTURE['signature']})
        second_interface = encode_interface({'constant': name('b'), 'signature': sig2})
        parent = identity(dict(FIXTURE['signature'], target=['sort', ['zero']]),
                          interfaces=(first_interface, second_interface))
        deps = (BoundDependency(child, codec.canonical(name('a')), first_interface, 'frozen_peer'),
                BoundDependency(second, codec.canonical(name('b')), second_interface, 'frozen_peer'))
        self.assertEqual(validate_dependencies(parent, deps), deps)
        for invalid in (deps[::-1], (deps[0], deps[0]), (replace(deps[0], interface=second_interface),),
                        (replace(deps[0], origin='abstracted_lemma', parameter=codec.canonical(name('wrong'))),)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_dependencies(parent, invalid)


class SelectionTests(unittest.TestCase):
    def test_binary_selection_roundtrip_preserves_roots_and_source_order(self):
        source, declarations, inputs, selection = selected_problem()
        payload = encode_selection(selection)
        self.assertNotEqual(selection.id, FIXTURE['selection_id'])
        with self.assertRaisesRegex(ValueError, 'binary bytes'):
            load_selection(bytes.fromhex(FIXTURE['selection_hex']), inputs)
        self.assertEqual([item.id for item in selection.identities], FIXTURE['root_ids'])
        self.assertEqual([target.selector for target in selection.selected.targets], ['witness', 'property'])
        self.assertEqual(load_selection(payload, inputs, expected_id=selection.id), selection)
        selected = select_targets(SelectionRequest('Main.lean', names=('property', 'witness')), source, declarations,
                                  imported_constants=frozenset((codec.canonical(name('Nat')), codec.canonical(name('Eq')))))
        self.assertEqual(selected.targets, declarations)
        with self.assertRaises(ValueError):
            load_selection(payload, replace(inputs, sources=(replace(inputs.sources[0], sha256='0'*64),)))

    def test_missing_prerequisites_and_unclassified_imports_fail(self):
        source, declarations, _, _ = selected_problem()
        with self.assertRaisesRegex(ValueError, 'prerequisites'):
            select_targets(SelectionRequest('Main.lean', names=('property',)), source, declarations,
                           imported_constants=frozenset((codec.canonical(name('Nat')), codec.canonical(name('Eq')))))
        with self.assertRaisesRegex(ValueError, 'unclassified'):
            select_targets(SelectionRequest('Main.lean', all_unfinished=True), source, declarations)

    def test_paths_spans_and_extractor_holes_fail(self):
        for path in ('../Main.lean', '/Main.lean', 'a//Main.lean', 'Main.txt'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                SelectionRequest(path, all_unfinished=True)
        source, declarations, _, _ = selected_problem()
        for changed in (replace(declarations[0], synthetic_sorry=True),
                        replace(declarations[0], declaration=ByteSpan(True, 10))):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                select_targets(SelectionRequest('Main.lean', all_unfinished=True), source, (changed, declarations[1]))
        unicode_source = 'α sorry'.encode()
        split = Declaration('a', codec.canonical(name('a')), 'theorem', ByteSpan(1, len(unicode_source)),
                            ByteSpan(3, len(unicode_source)), True)
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            select_targets(SelectionRequest('Main.lean', all_unfinished=True), unicode_source, (split,))


class SelectionConstructionTests(unittest.TestCase):
    def setUp(self):
        self.source, self.declarations, self.inputs, self.selection = selected_problem()
        self.signatures = dict(zip((d.constant for d in self.declarations), self.selection.signatures))

    def freeze(self, **changes):
        values = dict(request=self.selection.selected.request, source=self.source,
                      declarations=self.declarations,
                      imported_constants=frozenset(self.selection.imported_constants),
                      signatures=self.signatures, inputs=self.inputs)
        return freeze_selection(**dict(values, **changes))

    def test_freeze_keeps_typed_inputs_without_serializing_or_loading(self):
        with patch.object(selection_module, 'load_selection', side_effect=AssertionError('unexpected load')), \
             patch.object(selection_module, 'encode_selection', side_effect=AssertionError('unexpected save')):
            result = self.freeze()
            result.id
        self.assertIs(result.source, self.source)
        self.assertIs(result.declarations, self.declarations)
        self.assertIs(result.selected.request, self.selection.selected.request)
        for declaration, signature, root in zip(self.declarations, result.signatures, result.identities):
            self.assertIs(signature, self.signatures[declaration.constant])
            self.assertIs(root.target, signature.target)
        self.assertEqual(result.id, 'sel_' + sha256(encode_selection(result)).hexdigest())

    def test_only_environment_hash_crosses_domain_boundary(self):
        for value in (self.inputs, self.selection, *self.selection.identities):
            self.assertEqual(value.environment_id, ENVIRONMENT_ID)
            self.assertFalse(hasattr(value, 'environment'))
        for invalid in (ENVIRONMENT, 'env_bad', 'env_' + 'A' * 64):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    replace(self.inputs, environment_id=invalid)
                with self.assertRaises(ValueError):
                    replace(self.selection.identities[0], environment_id=invalid)

    def test_large_source_and_signature_use_raw_binary_fields(self):
        source = self.source + b'-- padding\n' * 400_000
        signature = Signature(codec.encode(['string', 'x' * 1_000_000]),
                              codec.encode([]), codec.encode(['closed-telescope']))
        declaration = replace(self.declarations[0], type_references=())
        inputs = replace(self.inputs, sources=(SourceBinding('Main.lean', sha256(source).hexdigest(), len(source)),))
        real_dumps, real_loads = json.dumps, json.loads

        def small_dump(value, *args, **kwargs):
            codec.require_tree_budget(value, limit=selection_module.NAME_BYTES)
            return real_dumps(value, *args, **kwargs)

        def small_load(payload, *args, **kwargs):
            self.assertLessEqual(len(payload), selection_module.NAME_BYTES)
            return real_loads(payload, *args, **kwargs)

        with patch.object(json, 'dumps', side_effect=small_dump), patch.object(json, 'loads', side_effect=small_load):
            result = self.freeze(source=source, declarations=(declaration,), imported_constants=frozenset(),
                                 signatures={declaration.constant: signature}, inputs=inputs)
            payload = encode_selection(result)
            restored = load_selection(payload, inputs, expected_id=result.id)
        self.assertIs(result.source, source)
        self.assertEqual(restored, result)
        self.assertIn(source, payload)
        self.assertIn(signature.target, payload)
        self.assertLess(len(payload), len(source) + len(signature.target) + 2048)

    def test_shared_signature_graph_does_not_expand(self):
        target = ['const', name('Nat'), []]
        for _ in range(50):
            target = ['app', target, target]
        signature = replace(self.selection.signatures[0], target=codec.encode(target))
        result = self.freeze(declarations=self.declarations[:1],
                             signatures={self.declarations[0].constant: signature})
        payload = encode_selection(result)
        self.assertLess(len(payload), 2048)
        restored = load_selection(payload, self.inputs)
        self.assertEqual(restored.identities[0].id, result.identities[0].id)
        decoded = codec.decode(restored.signatures[0].target)
        self.assertIs(decoded[1], decoded[2])

    def test_oversized_constant_rejected_before_json_projection(self):
        signature = replace(self.selection.signatures[0], target=codec.encode(['const', name('x' * 100_000), []]))
        real_dumps = json.dumps
        def small_dump(value, *args, **kwargs):
            try:
                codec.require_tree_budget(value, limit=selection_module.NAME_BYTES)
            except codec.GraphLimit as error:
                raise AssertionError('oversized name reached JSON') from error
            return real_dumps(value, *args, **kwargs)
        with patch.object(json, 'dumps', side_effect=small_dump):
            with self.assertRaises(codec.GraphLimit):
                self.freeze(signatures=self.signatures | {self.declarations[0].constant: signature})

    def test_freeze_and_load_share_source_and_signature_validation(self):
        bad_signatures = (replace(self.selection.signatures[0], target=codec.encode(['nat', 0])),
                          self.selection.signatures[1])
        with self.assertRaisesRegex(ValueError, 'references disagree'):
            self.freeze(signatures=dict(zip(self.signatures, bad_signatures)))
        with self.assertRaisesRegex(ValueError, 'references disagree'):
            load_selection(encode_selection(replace(self.selection, signatures=bad_signatures)), self.inputs)
        bad_inputs = replace(self.inputs, sources=(replace(self.inputs.sources[0], sha256='0' * 64),))
        with self.assertRaisesRegex(ValueError, 'authorized input'):
            self.freeze(inputs=bad_inputs)
        with self.assertRaisesRegex(ValueError, 'authorized input'):
            load_selection(encode_selection(self.selection), bad_inputs)
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            self.freeze(signatures=self.signatures | {codec.canonical(name('extra')): bad_signatures[0]})

    def test_nonclosed_context_rejected_before_signature_admission(self):
        signature = replace(self.selection.signatures[0], context=codec.encode(
            ['declaration-context-v1', 'x' * 100_000, []]))
        signatures = self.signatures | {self.declarations[0].constant: signature}
        with patch.object(selection_module, 'validate_signature', side_effect=AssertionError('unexpected context admission')):
            with self.assertRaisesRegex(ValueError, 'closed signature'):
                self.freeze(signatures=signatures)
            with self.assertRaisesRegex(ValueError, 'closed signature'):
                load_selection(encode_selection(replace(self.selection,
                    signatures=(signature, self.selection.signatures[1]))), self.inputs)

    def test_binary_reader_rejects_malformed_fields_and_hashes(self):
        payload = encode_selection(self.selection)
        magic = selection_module.SELECTION_MAGIC
        fields, position = [], len(magic)
        while position < len(payload):
            size = unpack_from('<Q', payload, position)[0]
            position += 8
            fields.append(payload[position:position + size])
            position += size

        def changed(index, value):
            values = fields.copy()
            values[index] = value
            return magic + b''.join(pack('<Q', len(item)) + item for item in values)

        invalid = (payload[:-1], payload + b'x', magic + pack('<Q', 2**64 - 1),
                   changed(2, b'\x02'), changed(2, b''), changed(3, pack('<Q', 1_000_001)),
                   changed(3, pack('<Q', 100_000)), changed(3, b'\x00'),
                   changed(0, ('env_' + 'f' * 64).encode()),
                   changed(6, fields[7]), changed(1, b'\xff'))
        for index, bad in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ValueError):
                load_selection(bad, self.inputs)
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            load_selection(payload, self.inputs, expected_id='sel_' + '0' * 64)


class SignatureBinaryTests(unittest.TestCase):
    def test_large_universe_names_do_not_use_json(self):
        universe = name('u' * 100_000)
        signature = {'target': ['sort', ['param', universe]], 'universes': [universe],
                     'context': ['closed-telescope']}
        with patch.object(json, 'dumps', side_effect=AssertionError('unexpected JSON projection')):
            validate_signature(signature)
            current = identity(signature)
            self.assertEqual(load_identity(current.payload(), environment_id=ENVIRONMENT_ID), current)


class SubmissionTests(unittest.TestCase):
    def test_binary_submission_preserves_authored_strings(self):
        for scoped in (False, True):
            value = submission(scoped=scoped)
            value['entries'][0]['proof'] = 'by\n  exact True.intro\n'
            self.assertEqual(load_submission(encode_submission(value)), value)
            detached = validate_submission(value, copy=True)
            detached['entries'][0]['proof'] = 'changed'
            self.assertNotEqual(detached, value)

    def test_bad_shapes_duplicate_keys_and_cycles_are_rejected(self):
        for payload in (b'[]', b'{"format":"a","format":"b"}', b'NaN', b'[' * 65 + b']' * 65):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                load_submission(payload)
        value = submission()
        entry = value['entries'][0]
        entry['dependencies'] = [{'parameter': 'self', 'entry': {'submission_id': value['submission_id'], 'key': 'root'}}]
        entry['review'] = {'decision': 'approve', 'rationale': 'test'}
        with self.assertRaisesRegex(ValueError, 'cycle'):
            validate_submission(value)
        entry['dependencies'][0]['entry']['key'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'Missing'):
            validate_submission(value)

    def test_statement_scope_is_fixed_while_proof_scope_extends(self):
        value = submission(scoped=True)
        value['entries'][0]['subject'] = {'kind': 'lemma', 'scope_target': 'Root', 'name': 'child', 'statement': 'x = x'}
        value['definitions'].append({'name': 'y', 'type': 'Nat', 'value': 'x'})
        value['entries'][0]['definition_scope'] = {'statement': 1, 'proof': 2}
        validate_submission(value)
        before = deepcopy(value)
        entry = resolve_entry(value, value['entries'][0])
        self.assertEqual(len(entry['subject']['declaration_context'][2]), 1)
        self.assertEqual(len(entry['declaration_context'][2]), 2)
        expected = entry_subject(value, value['entries'][0])
        self.assertTrue(matches_subject(value, value['entries'][0], expected))
        self.assertEqual(lexical_scope(value, value['entries'][0])['definitions'], value['definitions'][:1])
        self.assertEqual(value, before)

    def test_invalid_scope_and_imports_cannot_bypass_admission(self):
        value = submission(scoped=True)
        for bounds in ([], {'statement': 1, 'proof': 1}, {'statement': 0, 'proof': 99},
                       {'statement': 0, 'proof': 1, 'inherited_prerequisites': ['missing']}):
            altered = deepcopy(value)
            altered['entries'][0]['definition_scope'] = bounds
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                validate_submission(altered)
        for imports in (['MidasCheck'], ['MidasDecl_x'], ['Mathlib', 'Mathlib'], ['bad; import X']):
            with self.subTest(imports=imports), self.assertRaises(ValueError):
                validate_submission(dict(value, imports=imports))
        value['imports'] = ['Mathlib']
        value['entries'][0]['imports'] = ['Mathlib', 'Std']
        validate_submission(value)
        self.assertEqual(resolve_entry(value, value['entries'][0])['imports'], ['Mathlib', 'Std'])


class BinaryBoundaryTests(unittest.TestCase):
    def test_large_json_is_rejected_before_any_json_operation(self):
        with patch.object(json, 'dumps', side_effect=AssertionError('unexpected JSON serialization')), \
             patch.object(json, 'loads', side_effect=AssertionError('unexpected JSON parsing')):
            for value in ('x' * 1_000_000, {'source': 'x' * 1_000_000}, ['x'] * 100_000):
                with self.subTest(kind=type(value)), self.assertRaises(ValueError):
                    codec.canonical(value)
            with self.assertRaises(ValueError):
                codec.load_json(b'"' + b'x' * 1_000_000 + b'"', limit=2_000_000)
            with self.assertRaises(ValueError):
                codec.load_json(b'[' * 20 + b'0' + b']' * 20)
            with self.assertRaises(ValueError):
                codec.decode(b'{"old_graph":true}')
        self.assertFalse(hasattr(codec, 'embedded'))
        self.assertFalse(hasattr(codec, 'embedded_bytes'))
        self.assertFalse(hasattr(codec, 'validate_json_value'))

    def test_small_json_preserves_unicode_and_escaping_with_a_hard_cap(self):
        for ascii_mode in (False, True):
            value = {'text': 'α😀\x00\n"\\'}
            payload = codec.canonical(value, ascii=ascii_mode)
            self.assertEqual(codec.load_json(payload, ascii=ascii_mode), value)
        self.assertEqual(codec.load_json(codec.canonical({'text': 'x' * 16_000})), {'text': 'x' * 16_000})
        with self.assertRaises(ValueError):
            codec.canonical({'text': '\x00' * 3000})
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(ValueError):
            codec.canonical(cycle)
        deep = []
        for _ in range(codec.JSON_DEPTH - 1):
            deep = [deep]
        self.assertEqual(codec.load_json(codec.canonical(deep)), deep)
        with self.assertRaises(ValueError):
            codec.canonical([deep])

    def test_large_submission_and_context_never_use_json(self):
        value = submission(scoped=True)
        entry = value['entries'][0]
        entry['proof'] = 'by\n-- ' + 'α' * 400_000
        entry['subject'] = {'kind': 'lemma', 'name': 'large', 'scope_target': 'Root',
                            'statement': 'x' * 500_000}
        value['definitions'][0]['value'] = '0' * 100_000
        with patch.object(json, 'dumps', side_effect=AssertionError('unexpected JSON serialization')), \
             patch.object(json, 'loads', side_effect=AssertionError('unexpected JSON parsing')):
            self.assertIs(validate_submission(value), value)
            payload = encode_submission(value)
            restored = load_submission(payload)
            resolved = resolve_entry(restored, restored['entries'][0])
        self.assertEqual(restored, value)
        self.assertEqual(resolved['proof'], entry['proof'])
        raw_size = len(entry['proof'].encode()) + len(entry['subject']['statement']) + 100_000
        self.assertLess(len(payload), raw_size + 2048)
        self.assertIn(entry['proof'].encode(), payload)
        self.assertEqual(encode_submission(dict(reversed(list(value.items())))), payload)

    def test_submission_validation_does_not_build_binary_headers_or_reload(self):
        from midas.domain import submission as submission_module
        value = submission(scoped=True)
        payload = encode_submission(value)
        with patch.object(codec, '_RECORD_HEADER') as header, \
             patch.object(submission_module, 'load_submission', side_effect=AssertionError('unexpected reload')):
            header.pack.side_effect = AssertionError('unexpected binary serialization')
            self.assertIs(validate_submission(value), value)
            self.assertEqual(codec.record_size(value, limit=8 * 1024**2),
                             len(payload) - len(submission_module.SUBMISSION_MAGIC))

    def test_submission_rejects_large_totals_invalid_text_and_internal_cycles(self):
        value = submission()
        template = value['entries'][0]
        value['entries'] = [dict(template, key=f'k{i}', proof='x' * 1_000_000) for i in range(9)]
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            validate_submission(value)
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            encode_submission(value)
        for invalid in ('\ud800', 'α' * 600_000, float('inf'), ('by trivial',)):
            value = submission()
            value['entries'][0]['proof'] = invalid
            with self.subTest(kind=type(invalid)), self.assertRaises(ValueError):
                validate_submission(value)
        value = submission()
        value['entries'][0]['proof'] = value
        with self.assertRaisesRegex(ValueError, 'structural limits'):
            validate_submission(value)

    def test_binary_reader_rejects_bad_counts_keys_depth_and_trailing_bytes(self):
        from midas.domain.submission import SUBMISSION_MAGIC
        string = lambda value: b'\x04' + pack('<Q', len(value)) + value
        invalid_records = (
            b'\xff', b'\x03', b'\x03' + pack('<Q', 2**63),
            b'\x04' + pack('<Q', 2**64 - 1), string(b'\xff'),
            b'\x05' + pack('<Q', 100_001),
            b'\x06' + pack('<Q', 2) + string(b'a') + b'\x00' + string(b'a') + b'\x00',
            b'\x06' + pack('<Q', 2) + string(b'b') + b'\x00' + string(b'a') + b'\x00',
            (b'\x05' + pack('<Q', 1)) * 10 + b'\x00',
            b'\x00\x00',
        )
        for index, record in enumerate(invalid_records):
            with self.subTest(case=index), self.assertRaises(ValueError):
                load_submission(SUBMISSION_MAGIC + record)
        payload = encode_submission(submission())
        for invalid in (payload[:-1], payload + b'\x00', b'{"format":"midas-candidates-v1"}'):
            with self.assertRaises(ValueError):
                load_submission(invalid)

    def test_context_hashing_reads_each_definition_once(self):
        from midas.domain import identity as identity_module
        scope = 'S' * 100_000
        definitions = [{'name': f'x{i}', 'type': 'Nat', 'value': '0' * 100_000} for i in range(16)]
        calls = []
        def observe(digest, value):
            calls.append(len(value))
            codec.hash_field(digest, value)
        with patch.object(json, 'dumps', side_effect=AssertionError('unexpected JSON serialization')), \
             patch.object(identity_module, 'hash_field', side_effect=observe):
            context = make_context(scope, definitions)
        self.assertEqual(len(calls), 1 + 4 * len(definitions))
        self.assertEqual(sum(calls), len(scope) + sum(len(d[k]) for d in definitions for k in ('name', 'type', 'value'))
                         + 3 * len(definitions))  # Default kind is 'def'.
        self.assertEqual(make_context(scope, definitions[:-1])[2], context[2][:-1])
        altered = [*definitions[:-1], dict(definitions[-1], value='1')]
        changed = make_context(scope, altered)
        self.assertEqual(changed[2][:-1], context[2][:-1])
        self.assertNotEqual(changed[2][-1]['namespace'], context[2][-1]['namespace'])
        self.assertNotEqual(make_context('R', [{'name': 'a', 'type': 'bc', 'value': '0'}]),
                            make_context('R', [{'name': 'ab', 'type': 'c', 'value': '0'}]))

    def test_review_hashes_raw_interfaces_and_binds_order_and_fields(self):
        from midas.domain import review_digest
        child = identity()
        interface = codec.encode({'constant': name('x'), 'signature': dict(FIXTURE['signature'],
                                 target=['string', 'x' * 900_000])})
        dep = BoundDependency(child, codec.canonical(name('x')), interface)
        second = replace(dep, parameter=codec.canonical(name('y')))
        review = codec.canonical({'decision': 'approve', 'rationale': 'checked'})
        child.id
        observed = []
        real_hash_field, real_dumps, real_loads = codec.hash_field, json.dumps, json.loads
        def observe(digest, value):
            observed.append(value)
            real_hash_field(digest, value)
        def small_dump(value, *args, **kwargs):
            codec._metadata_budget(value)
            return real_dumps(value, *args, **kwargs)
        def small_load(value, *args, **kwargs):
            self.assertLessEqual(len(value), codec.JSON_LIMIT)
            return real_loads(value, *args, **kwargs)
        with patch.object(codec, 'hash_field', side_effect=observe), \
             patch.object(json, 'dumps', side_effect=small_dump), patch.object(json, 'loads', side_effect=small_load):
            digest = review_digest(uid('attempt'), child.id, artifact('proof'), review, (dep, second))
        self.assertEqual(sum(item is interface for item in observed), 2)
        for deps in ((second, dep), (dep,), (replace(dep, interface=interface + b'x'), second),
                     (replace(dep, origin='frozen_peer'), second)):
            self.assertNotEqual(digest, review_digest(uid('attempt'), child.id, artifact('proof'), review, deps))


class JointTests(unittest.TestCase):
    def setUp(self):
        _, _, _, self.selection = selected_problem()
        self.nodes = tuple(ProofNode(OwnerVersion(item.id, 0), item.environment_id, artifact(item.id))
                           for item in self.selection.identities)
        self.graph = ProofGraph(0, tuple(item.owner.obligation_id for item in self.nodes), self.nodes)
        self.sid = uid('joint')
        first = Candidate(uid('definition'), self.nodes[0].owner, self.nodes[0].environment_id,
                          artifact('definition'), joint_submission_id=self.sid)
        first_check = AdmittedCheck(first, CheckKind.DIRECT, receipt('first'), artifact('first certificate'))
        dep = Dependency(self.nodes[0].owner, 'witness', self.nodes[0].interface,
                         DependencyOrigin.CLOSED_DEFINITION, first_check.evidence, self.sid)
        second = Candidate(uid('property'), self.nodes[1].owner, self.nodes[1].environment_id,
                           artifact('property'), (dep,), joint_submission_id=self.sid)
        self.checks = (first_check, AdmittedCheck(second, CheckKind.DIRECT, receipt('second'), artifact('second certificate')))

    def test_joint_target_order_and_atomic_acceptance(self):
        self.assertEqual(len(joint_targets(self.selection)), 2)
        self.assertEqual(joint_coordinator(self.selection), self.selection.identities[1].id)
        with self.assertRaises(DomainError):
            accept_joint(self.graph, self.selection, self.sid, self.checks[:1])
        self.assertTrue(all(item.state == ProofState.UNRESOLVED for item in self.graph.nodes))
        result = accept_joint(self.graph, self.selection, self.sid, self.checks)
        self.assertTrue(all(item.state == ProofState.VERIFIED for item in result.nodes))
        self.assertEqual(result.revision, 1)
        self.assertIs(accept_joint(result, self.selection, self.sid, self.checks), result)

    def test_joint_certificate_cannot_escape_or_be_substituted(self):
        with self.assertRaises(DomainError):
            accept_checked_proof(self.graph, self.checks[0])
        candidate = self.checks[1].candidate
        with self.assertRaises(DomainError):
            replace(candidate, joint_submission_id=uid('different submission'))
        with self.assertRaises(DomainError):
            replace(candidate, dependencies=(*candidate.dependencies,
                    replace(candidate.dependencies[0], alias='another', certificate=artifact('different'))))
        wrong = replace(candidate, dependencies=(replace(candidate.dependencies[0], certificate=artifact('wrong')),))
        with self.assertRaises(DomainError):
            accept_joint(self.graph, self.selection, self.sid, (self.checks[0], replace(self.checks[1], candidate=wrong)))
        with self.assertRaises(DomainError):
            accept_joint(self.graph, self.selection, self.sid, self.checks[::-1])

    def test_joint_submission_requires_all_targets_and_earlier_definition_scope(self):
        value = submission()
        value['entries'][0]['subject']['name'] = 'witness'
        value['entries'][0]['key'] = 'definition'
        prop = deepcopy(value['entries'][0])
        prop.update(key='property', subject={'kind': 'target', 'name': 'property'},
                    dependencies=[{'parameter': 'witness', 'entry': {'submission_id': value['submission_id'], 'key': 'definition'}}],
                    review={'decision': 'approve', 'rationale': 'joint fixture'})
        value['entries'].append(prop)
        packet = {'joint_problem': True, 'obligation': joint_coordinator(self.selection),
                  'required_targets': ['witness', 'property']}
        validate_joint_submission(self.selection, packet, value)
        for altered in (dict(value, entries=value['entries'][:1]), dict(value, entries=value['entries'][::-1])):
            with self.assertRaises(ValueError):
                validate_joint_submission(self.selection, packet, altered)
        prop['dependencies'][0]['entry']['submission_id'] = uid('another')
        with self.assertRaises(ValueError):
            validate_joint_submission(self.selection, packet, value)


class ProofMetadataTests(unittest.TestCase):
    def test_root_ids_alias_consumers_and_review_digest_bind_exact_inputs(self):
        from uuid import UUID, uuid5
        from midas.domain import ProofAlias, target_id, review_digest
        child = identity()
        owner = OwnerVersion(child.id, 0)
        tid = target_id(uid('run'), 'sel_'+'a'*64, 2)
        self.assertEqual(tid, str(uuid5(UUID(uid('run')), 'proof-target:sel_'+'a'*64+':2')))
        alias = ProofAlias(uid('alias'), owner, 'root', artifact('submission'), target_id=tid)
        with self.assertRaises(DomainError):
            replace(alias, plan_id=uid('plan'))
        interface = encode_interface({'constant': name('x'), 'signature': FIXTURE['signature']})
        dep = BoundDependency(child, codec.canonical(name('x')), interface)
        review = codec.canonical({'decision': 'approve', 'rationale': 'checked plan'})
        digest = review_digest(uid('attempt'), child.id, artifact('proof'), review, (dep,))
        self.assertNotEqual(digest, review_digest(uid('attempt'), child.id, artifact('edited'), review, (dep,)))
        self.assertNotEqual(digest, review_digest(uid('attempt'), child.id, artifact('proof'), review,
                                                 (replace(dep, parameter=codec.canonical(name('y'))),)))


class CacheTests(unittest.TestCase):
    def test_single_build_for_concurrent_key_and_failure_not_cached(self):
        memo = Memo()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def build():
            calls.append(1)
            entered.set()
            if not release.wait(2):
                raise AssertionError('test build was not released')
            return b'value'
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(memo.get, b'env', b'key', build)
            self.assertTrue(entered.wait(2))
            second = pool.submit(memo.get, b'env', b'key', build)
            self.assertEqual(memo.get(b'env', b'other', lambda: b'other'), b'other')
            release.set()
            self.assertEqual(first.result(2), b'value')
            self.assertEqual(second.result(2), b'value')
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            memo.get(b'env', b'bad', lambda: (_ for _ in ()).throw(ValueError('bad')))
        self.assertEqual(memo.get(b'env', b'bad', lambda: b'good'), b'good')

    def test_lru_byte_limits_and_environment_switch(self):
        memo = Memo(max_entries=1, max_bytes=1024)
        memo.get(b'env', b'first', lambda: b'first')
        memo.get(b'env', b'second', lambda: b'second')
        self.assertNotIn(b'first', memo.entries)
        memo.get(b'env', b'oversized', lambda: b'x' * 2048)
        self.assertNotIn(b'oversized', memo.entries)
        self.assertLessEqual(memo.bytes, memo.max_bytes)
        self.assertEqual(memo.get(b'other', b'second', lambda: b'changed'), b'changed')

    def test_reset_retires_inflight_build_even_when_environment_switches_back(self):
        memo = Memo()
        entered, release = threading.Event(), threading.Event()
        def build():
            entered.set()
            if not release.wait(2):
                raise AssertionError('test build was not released')
            return b'old'
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(memo.get, b'env', b'key', build)
            self.assertTrue(entered.wait(2))
            memo.reset()
            self.assertEqual(memo.get(b'env', b'key', lambda: b'new'), b'new')
            release.set()
            self.assertEqual(old.result(2), b'old')
        self.assertEqual(memo.get(b'env', b'key', lambda: b'wrong'), b'new')

    @unittest.skipUnless(hasattr(os, 'fork'), 'fork is a Linux runtime capability')
    def test_fork_discards_parent_cache_and_inherited_lock(self):
        memo = Memo()
        memo.get(b'env', b'key', lambda: b'parent')
        read_fd, write_fd = os.pipe()
        memo.lock.acquire()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                result = memo.get(b'env', b'key', lambda: b'child')
                os.write(write_fd, result)
            finally:
                os._exit(0)
        memo.lock.release()
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], 2)
            if not ready:
                os.kill(pid, 9)
                self.fail('child inherited a locked cache')
            self.assertEqual(os.read(read_fd, 10), b'child')
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)


if __name__ == '__main__':
    unittest.main()
