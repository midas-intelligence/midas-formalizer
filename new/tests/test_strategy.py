"""Offline strategy contracts and state transitions. No Lean or provider execution."""
import ast
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from midas.domain import (AdmittedBinding, AdmittedReview, ArtifactRef, FrozenRequest, Job, JobKind,
    ProofGraph, ProofState, ResourceLimits, ResultStatus, accept_checked_proof, load_submission)
from midas.domain.encoding import load_record
from midas.domain.wire import decode
from midas.policy import Config, OwnerUsage, Policy, Services
from midas.prompt import Options, Section, Snapshot as Display, prover
from midas.prompt.wire import _Writer
from midas.strategy import (Attempt, Context, Dispatch, Draft, Lesson, Memory, Observation, Objection,
    Prerequisite, Reference, Snapshot, StopRequest, Strategy, Subject, Turn, Update, Version,
    child_context, consumers, expand, fragments, interpret, reopen, revise, update_memory)
from midas.strategy.memory import pack
from midas.tools import Scope, StageEvidence, prepare_archive
from tests.test_domain import artifact, candidate, check, node, receipt, review, select, uid


class Artifacts:
    def __init__(self):
        self.data = {}

    def save(self, data):
        digest = sha256(data).hexdigest()
        ref = ArtifactRef(uid(digest), digest, len(data))
        self.data[ref.id] = data
        return ref

    def read_artifact(self, ref, *, max_bytes):
        return ref.verify(self.data[ref.id], max_bytes=max_bytes)

    def write_bounded(self, data, *, max_bytes):
        if len(data) > max_bytes:
            raise ValueError('Fixture exceeds byte limit')
        return self.save(data)


def model_json(value):
    """Fixture writer with bounded string chunks, including large response cases."""
    writer = _Writer(8 * 1024**2)
    def emit(item):
        if isinstance(item, dict):
            writer.add(b'{')
            for index, (key, child) in enumerate(item.items()):
                writer.add(b',' if index else b'')
                writer.string((key.encode(),))
                writer.add(b':')
                emit(child)
            writer.add(b'}')
        elif isinstance(item, list):
            writer.add(b'[')
            for index, child in enumerate(item):
                writer.add(b',' if index else b'')
                emit(child)
            writer.add(b']')
        elif isinstance(item, str):
            writer.string((item.encode(),))
        else:
            writer.add(b'null' if item is None else b'true' if item is True else
                       b'false' if item is False else str(item).encode())
    emit(value)
    return writer.finish()


def proof(**extra):
    return dict(mode='proof', rationale='Use reflexivity.', proof_body='by rfl',
                proof='Reflexivity proves the equality.', proof_departures='', definitions=[], imports=[], **extra)


def decomposition():
    value = proof()
    value.update(mode='decomposition', proof_body='by exact second', children=[
        dict(name='second', statement='True', proof='Apply the first lemma.', usefulness='Concludes the goal.', depends_on=['first']),
        dict(name='first', statement='True', proof='Use the constructor.', usefulness='Provides a witness.', depends_on=[])])
    return value


def response(action, memory=None):
    return model_json(dict(action=action, memory_update=memory))


def context():
    return Context('sel_' + 'a'*64, (Subject('target', 'goal'),))


def fixture(*, workflow='live', conditional=False):
    artifacts = Artifacts()
    parent, child = node('parent'), node('child')
    graph = ProofGraph(0, (parent.owner.obligation_id,), (parent, child))
    ctx = context()
    snapshot = Snapshot(Version(parent.owner, 0, artifacts.save(ctx.payload)), ctx, parent.environment_id,
                        graph, workflow=workflow, services=Services(research=True, research_web_calls=3))
    if conditional:
        value = candidate(parent, (child,))
        snapshot = replace(snapshot, attempt=Attempt(value, True, check(value)))
    return artifacts, snapshot


def dispatch(snapshot, artifacts, strategy=None):
    strategy = strategy or Strategy()
    work = strategy.plan(snapshot, Policy(Config())).work[0]
    refs, owners = (snapshot.version.context,), (snapshot.version.owner,)
    if snapshot.attempt:
        value = snapshot.attempt.candidate
        refs += (value.source,) + tuple(d.interface for d in value.dependencies)
        owners += tuple(d.owner for d in value.dependencies)
    sections = (Section('subject', 'goal', b'Goal: True'), Section('candidate', 'candidate', b'Conditional glue'))
    if snapshot.stop_request:
        refs += (snapshot.stop_request.source,)
        sections += (Section('candidate', 'stop-request', snapshot.stop_request.rationale.encode()),)
    display = Display(sections,
                      refs, owners)
    prepared = strategy.prepare_model(snapshot, Policy(Config()), work, display, Options('codex', 'model'))
    refs = (artifacts.save(prepared.wire.wire), artifacts.save(prepared.wire.setup))
    job = Job('turn', JobKind.MODEL, snapshot.version.owner, refs, owners,
              ResourceLimits(1000, 32, 1024**2, 1024**2))
    frozen = FrozenRequest(job, snapshot.environment_id, artifacts.save(b'provider request'), artifacts.save(b'frozen request'))
    return Dispatch.bind(prepared, frozen), prepared, frozen


def admitted(artifacts, dispatched, raw, **kwargs):
    return SimpleNamespace(raw=artifacts.save(raw), receipt=replace(receipt('model'), request_ref=dispatched.request),
        status=kwargs.get('status', ResultStatus.SUCCEEDED), outcome=SimpleNamespace(error=kwargs.get('error')))


class ProtocolTests(unittest.TestCase):
    def test_default_contracts_are_strict_and_role_bound(self):
        value = response(proof())
        parsed = interpret(value, role='prover', contract='prover-action-v2')
        self.assertEqual(parsed.mode, 'proof')
        for raw in (b'```json\n' + value + b'\n```', b'prefix ' + value, value + b'{}',
                    b'{"action":{},"action":{}}', b'{"x":NaN}', b'{"x":"\\ud800"}'):
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                interpret(raw, role='prover', contract='prover-action-v2')
        for role, contract in (('reviewer', 'prover-action-v2'), ('prover', 'unknown')):
            with self.assertRaises(ValueError):
                interpret(value, role=role, contract=contract)
        malformed = proof()
        malformed['environment'] = {}
        with self.assertRaises(ValueError):
            interpret(response(malformed), role='prover', contract='prover-action-v2')

    def test_large_response_never_calls_json_library(self):
        action = proof()
        action['proof_body'] = 'by\n  -- ' + 'x'*900000 + '\n  rfl'
        raw = response(action)
        with patch('json.loads', side_effect=AssertionError('JSON loads')), patch('json.dumps', side_effect=AssertionError('JSON dumps')):
            result = interpret(raw, role='prover', contract='prover-action-v2')
            draft = expand(result.value, context(), uid('large'))
            self.assertEqual(load_submission(draft.submission)['entries'][0]['proof'], action['proof_body'])
            self.assertEqual(Draft.load(draft.payload), draft)

    def test_retrieve_responses_reject_removed_fields(self):
        actions = (dict(mode='retrieve', rationale='Read the definition.', names=['Nat.add'],
                        queries=[], backends=[]),
                   dict(mode='retrieve', rationale='Find supporting declarations.', queries=['addition'],
                        names=[], backends=[dict(name='leanexplore', prefixes=[])]))
        for action in actions:
            with self.subTest(mode=action['mode']):
                parsed = interpret(response(action), role='prover', contract='prover-action-v2')
                self.assertEqual(parsed.value, action)
                for extra in (dict(inspect=False), dict(inspect=True), dict(offset=0), dict(offset=12)):
                    with self.assertRaises(ValueError):
                        interpret(response(dict(action, **extra)), role='prover', contract='prover-action-v2')

    def test_search_and_lookup_responses_are_rejected(self):
        actions = (dict(mode='search', rationale='Find addition.', query='addition',
                        backend=dict(name='leanexplore', prefixes=[])),
                   dict(mode='lookup', rationale='Read the definition.', names=['Nat.add']))
        for action in actions:
            with self.subTest(mode=action['mode']), self.assertRaises(ValueError):
                interpret(response(action), role='prover', contract='prover-action-v2')

    def test_review_findings_and_research_invariants(self):
        value = dict(verdict='accept', rationale='A valid plan.', findings=[], question='', urls=[])
        parse = lambda v: interpret(model_json(v), role='reviewer', contract='review-verdict-v1')
        self.assertEqual(parse(value).mode, 'review')
        with self.assertRaises(ValueError):
            parse(dict(value, verdict='reject'))
        finding = dict(category='mathematical', location='child', issue='Hypothesis is missing.', suggested_revision='Add it.')
        self.assertEqual(parse(dict(value, verdict='reject', findings=[finding])).value['verdict'], 'reject')
        with self.assertRaises(ValueError):
            parse(dict(value, findings=[finding]))
        with self.assertRaises(ValueError):
            parse(dict(value, question='Hidden research'))


class CandidateTests(unittest.TestCase):
    def test_recursive_children_use_flat_context_and_inherit_imports(self):
        ctx = replace(context(), imports=('Mathlib',))
        action = decomposition()
        action['imports'] = ['Mathlib']
        action['definitions'] = [dict(name='value', kind='def', type='Nat', value='0')]
        draft = expand(action, ctx, uid('parent-plan'))
        child = child_context(draft, 'second', ctx)
        self.assertEqual(child.inherited_imports, ('Mathlib',))
        self.assertEqual([p.parameter for p in child.prerequisites], ['first'])
        self.assertEqual(child.prerequisites[0].reference.submission_id, uid('parent-plan'))
        local = expand(proof(), child, uid('child-proof'))
        saved = load_submission(local.submission)
        self.assertEqual(len(saved['definitions']), 1)
        self.assertEqual(saved['imports'], ['Mathlib'])
        self.assertEqual(saved['entries'][0]['subject'], child.subjects[0].value())
        self.assertLess(len(child.payload), len(draft.payload))

    def test_joint_definition_dependencies_use_direct_allowance_and_roundtrip(self):
        ctx = Context(context().selection_id, (Subject('target', 'witness'), Subject('target', 'property')),
                      definition_targets=('witness',))
        row = lambda name: dict(key=name, kind='target', name=name, scope_target=None, statement=None,
                               proof_body='by trivial', proof='A construction.', dependencies=[])
        action = dict(mode='candidate', rationale='Supply a witness and its property.', entries=[row('witness'), row('property')],
                      proof='Construct then verify.', proof_departures='', definitions=[], imports=[])
        with self.assertRaises(ValueError):
            expand(action, ctx, uid('joint'))
        action['entries'][1]['dependencies'] = [dict(parameter='witness', submission='current', key='witness')]
        draft = expand(action, ctx, uid('joint'))
        self.assertEqual(draft.mode, 'direct')
        self.assertEqual(draft.closed_definitions, ('witness',))
        self.assertEqual(Draft.load(draft.payload), draft)
        self.assertEqual(draft.checks(), (('witness', 'proof'), ('property', 'proof')))

    def test_malformed_binary_records_and_changed_selection_are_rejected(self):
        draft = expand(proof(), context(), uid('draft'))
        for payload in (draft.payload[:-1], draft.payload + b'x', b'MIDASDRAFT\x01' + b'\xff'*8):
            with self.subTest(payload=payload[:20]), self.assertRaises(ValueError):
                Draft.load(payload)
        for value in (['midas-memory-v1', 0, '', [[]], [], [], 0],
                      ['midas-memory-v1', 0, '', [], [[]], [], 0]):
            with self.assertRaises(ValueError):
                Memory.load(pack(value))
        changed = replace(context(), selection_id='sel_'+'b'*64)
        action = dict(mode='revise', rationale='Changed proof.', edits=[dict(path='/entries/parent/proof', old=None, new='by trivial')])
        with self.assertRaises(ValueError):
            revise(action, draft, changed, uid('changed-selection'))

    def test_flat_decomposition_and_dependency_order(self):
        draft = expand(decomposition(), context(), uid('plan'))
        value = load_submission(draft.submission)
        self.assertEqual(draft.checks(), (('first', 'binding'), ('second', 'binding'), ('parent', 'proof')))
        self.assertEqual(draft.unproved, ('second', 'first'))
        self.assertEqual(draft.mode, 'decomposition')
        self.assertNotIn('/entries/first/proof', dict(fragments(draft, context())))
        self.assertEqual(value['entries'][0]['definition_scope']['inherited_prerequisites'], [])
        self.assertEqual(len(value['definitions']), 0)
        self.assertEqual(Draft.load(draft.payload), draft)
        self.assertEqual(Context.load(context().payload), context())

    def test_cycles_missing_names_and_collisions_are_rejected(self):
        for mutation in ('cycle', 'unknown', 'duplicate', 'definition'):
            value = decomposition()
            if mutation == 'cycle':
                value['children'][1]['depends_on'] = ['second']
            elif mutation == 'unknown':
                value['children'][0]['depends_on'] = ['missing']
            elif mutation == 'duplicate':
                value['children'][1]['name'] = 'second'
            else:
                value['definitions'] = [dict(name='first', kind='def', type='Nat', value='0')]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                expand(value, context(), uid('invalid'))

    def test_child_context_preserves_only_explicit_prerequisites(self):
        definition = dict(name='value', kind='def', type='Nat', value='0')
        reference = Reference('base', 'earlier', uid('old'))
        ctx = Context(context().selection_id, (Subject('lemma', 'child', 'goal', 'value = value'),),
                      pack([definition]), (Prerequisite('h', reference, 'True'),))
        draft = expand(decomposition(), ctx, uid('nested'))
        value = load_submission(draft.submission)
        parent, second, first = value['entries']
        self.assertEqual(parent['subject'], ctx.subjects[0].value())
        self.assertEqual(parent['dependencies'][0], ctx.prerequisites[0].dependency())
        self.assertEqual(first['dependencies'], [])
        self.assertEqual(second['dependencies'][0]['parameter'], 'first')
        self.assertEqual(len(value['definitions']), 1)

    def test_exact_edits_are_sequential_atomic_and_rebind_local_references(self):
        draft = expand(decomposition(), context(), uid('base'))
        action = dict(mode='revise', rationale='Change the glue.', edits=[
            dict(path='/entries/parent/proof', old='second', new='first'),
            dict(path='/entries/parent/proof', old='first', new='second.elim')])
        result = revise(action, draft, context(), uid('revision'))
        value = load_submission(result.submission)
        self.assertEqual(value['entries'][0]['proof'], 'by exact second.elim')
        self.assertTrue(all(d['entry']['submission_id'] == uid('revision') for e in value['entries'] for d in e['dependencies']))
        self.assertEqual(load_submission(draft.submission)['entries'][0]['proof'], 'by exact second')
        bad = deepcopy(action)
        bad['edits'].append(dict(path='/entries/0/proof', old=None, new='by trivial'))
        with self.assertRaises(ValueError):
            revise(bad, draft, context(), uid('bad'))
        self.assertEqual(Draft.load(draft.payload), draft)

    def test_revisions_reject_noops_overlapping_matches_and_goal_changes(self):
        action = proof()
        action['proof_body'] = 'aaaa'
        base = expand(action, context(), uid('base'))
        for edits in ([dict(path='/entries/parent/proof', old='aaa', new='b')],
                      [dict(path='/entries/parent/proof', old=None, new='aaaa')],
                      [dict(path='/entries/parent/proof', old=None, new='b'),
                       dict(path='/entries/parent/proof', old=None, new='aaaa')]):
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                revise(dict(mode='revise', rationale='Edit.', edits=edits), base, context(), uid('new'))
        ctx = Context(context().selection_id, (Subject('lemma', 'child', 'goal', 'True'),))
        base = expand(proof(), ctx, uid('lemma'))
        self.assertNotIn('/entries/parent/statement', dict(fragments(base, ctx)))
        with self.assertRaises(ValueError):
            revise(dict(mode='revise', rationale='Edit.', edits=[dict(path='/entries/parent/statement', old=None, new='False')]),
                   base, ctx, uid('changed'))

    def test_candidate_target_order_imports_and_external_references(self):
        ctx = Context(context().selection_id, (Subject('target', 'a'), Subject('target', 'b')),
                      references=(Reference('previous', 'lemma', uid('previous')),), imports=('Mathlib',))
        row = lambda name: dict(key=name, kind='target', name=name, scope_target=None, statement=None,
                               proof_body='by trivial', proof='Trivial.', dependencies=[])
        value = dict(mode='candidate', rationale='Joint proof.', entries=[row('a'), row('b')],
                     proof='Prove both.', proof_departures='', definitions=[], imports=['Mathlib'])
        self.assertEqual(expand(value, ctx, uid('joint')).mode, 'direct')
        value['entries'].reverse()
        with self.assertRaises(ValueError):
            expand(value, ctx, uid('joint'))
        value['entries'].reverse()
        value['entries'][0]['dependencies'] = [dict(parameter='h', submission='previous', key='lemma')]
        self.assertEqual(expand(value, ctx, uid('joint')).mode, 'decomposition')
        value['entries'][0]['dependencies'][0]['submission'] = uid('previous')
        with self.assertRaises(ValueError):
            expand(value, ctx, uid('joint'))


class MemoryTests(unittest.TestCase):
    def test_updates_are_versioned_immutable_and_scope_checked(self):
        original = Memory()
        update = Update('Try induction.', (Lesson('induction', 'The successor case needs rewriting.'),), pin_evidence=('e',))
        result = update_memory(original, update, allowed_pins=('e',))
        self.assertEqual(result.version, 1)
        self.assertEqual(original, Memory())
        self.assertEqual(Memory.load(result.payload), result)
        with self.assertRaises(ValueError):
            update_memory(original, update)
        with self.assertRaises(ValueError):
            Update('', (Lesson('x', 'a'), Lesson('x', 'b')))
        with self.assertRaises(ValueError):
            Update('', pin_evidence=('x',), unpin_evidence=('x',))

    def test_whole_record_eviction_and_observation_omissions(self):
        change = Update('Strategy.', tuple(Lesson(str(i), 'x'*1000) for i in range(32)))
        with patch('json.loads', side_effect=AssertionError), patch('json.dumps', side_effect=AssertionError):
            memory = update_memory(Memory(), change, budget=2000)
            observation = Observation.select('checker', 'rejection', artifact('evidence'), ('x'*9000, 'Actual error.'))
            memory = update_memory(memory, observation=observation, budget=2000)
        self.assertGreater(memory.evicted, 0)
        self.assertEqual(memory.lessons[-1].id, '31')
        self.assertEqual(memory.observations[-1].messages, ('Actual error.',))
        self.assertEqual(memory.observations[-1].omitted, 1)
        self.assertLessEqual(len(memory.payload), 65536)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.strategy, self.policy = Strategy(), Policy(Config())

    def test_prompt_schema_filters_helpers_but_always_includes_revise(self):
        cases = (
            (Services(), OwnerUsage(), (), (), {'retrieve', 'probe'}),
            (Services(), OwnerUsage(), ('result-1',), (), {'retrieve', 'probe'}),
            (Services(research=True, research_web_calls=3), OwnerUsage(), ('result-1',), ('result-1:1',),
             set(prover.HELPER_MODES)),
            (Services(research=True, research_web_calls=3), OwnerUsage(research=7, retrieval=10, probe=6),
             (), (), set()),
            (Services(compiler=False), OwnerUsage(), (), (), set()),
        )
        for services, usage, pins, cursors, expected in cases:
            with self.subTest(services=services, usage=usage, pins=pins):
                artifacts, snapshot = fixture()
                snapshot = replace(snapshot, services=services, usage=usage, allowed_pins=pins,
                                   cursors=cursors)
                _, prepared, _ = dispatch(snapshot, artifacts)
                wire = decode(prepared.wire.wire, limit=8*1024**2)
                schema = wire['outputSchema']
                actions = {a['properties']['mode']['enum'][0] for a in schema['properties']['action']['anyOf']}
                self.assertEqual(actions & set(prover.HELPER_MODES), expected)
                self.assertEqual(actions - set(prover.HELPER_MODES),
                                 {'proof', 'decomposition', 'candidate', 'revise', 'stop'})
                capabilities = wire['input'][-1]['text']
                self.assertTrue(capabilities.startswith('QUESTION next-action\nCAPABILITIES strategy-capabilities\n'))
                self.assertTrue(capabilities.endswith('Return the action JSON now.'))
                advertised = next(line.removeprefix('Helper modes: ') for line in capabilities.splitlines()
                                  if line.startswith('Helper modes: '))
                self.assertEqual(set(advertised.split(', ')) if advertised else set(), expected)
                self.assertNotIn('recall', capabilities)
                if not cursors:
                    self.assertIn('page: unavailable (no additional pages outside this prompt)', capabilities)
                if not services.research:
                    self.assertIn('research: disabled', capabilities)
                    self.assertNotIn('research_remaining:', capabilities)
                expected_schema = prover.action_schema(tuple(sorted(actions)))
                self.assertEqual(schema, decode(expected_schema, limit=16384))
                self.assertEqual(prepared.turn.schema_hash, sha256(expected_schema).hexdigest())

    def test_revision_availability_preserves_schema_and_cache_key(self):
        artifacts, snapshot = fixture()
        display = Display((Section('subject', 'goal', b'Goal: True'),),
                          (snapshot.version.context,), (snapshot.version.owner,))
        for transport in ('azure_responses', 'bedrock_responses'):
            with self.subTest(transport=transport):
                options = Options(transport, 'model', prompt_cache='explicit', cache_scope='a'*64)
                prepared = []
                for base in (None, expand(proof(), snapshot.context, uid('direct-base')),
                             expand(decomposition(), snapshot.context, uid('decomposition-base'))):
                    current = replace(snapshot, base=base)
                    work = self.strategy.plan(current, self.policy).work[0]
                    prepared.append(self.strategy.prepare_model(current, self.policy, work, display, options))
                first = decode(prepared[0].wire.wire, limit=8*1024**2)
                self.assertIn('revise: unavailable (no existing candidate)', first['input'][-1]['content'])
                for changed in prepared[1:]:
                    wire = decode(changed.wire.wire, limit=8*1024**2)
                    self.assertEqual(prepared[0].turn.schema_hash, changed.turn.schema_hash)
                    self.assertEqual(first['text'], wire['text'])
                    self.assertEqual(first['prompt_cache_key'], wire['prompt_cache_key'])
                    self.assertIn('revise: available', wire['input'][-1]['content'])
                    self.assertEqual(prepared[0].wire.wire.partition(b'QUESTION next-action\\n')[0],
                                     changed.wire.wire.partition(b'QUESTION next-action\\n')[0])

    def test_counters_preserve_cache_key_but_action_availability_changes_schema(self):
        artifacts, snapshot = fixture()
        snapshot = replace(snapshot, services=Services())
        display = Display((Section('subject', 'goal', b'Goal: True'),
                           Section('evidence', 'saved-result', b'Immutable source')),
                          (snapshot.version.context,), (snapshot.version.owner,))
        options = Options('azure_responses', 'model', prompt_cache='explicit', cache_scope='a'*64)
        wires = []
        for changed in (snapshot, replace(snapshot, usage=OwnerUsage(retrieval=1, probe=1)),
                        replace(snapshot, cursors=('result:1',)),
                        replace(snapshot, usage=OwnerUsage(research=7, retrieval=10, probe=6))):
            work = self.strategy.plan(changed, self.policy).work[0]
            prepared = self.strategy.prepare_model(changed, self.policy, work, display, options)
            wires.append(prepared.wire.wire)
        first = decode(wires[0], limit=8*1024**2)
        current = decode(wires[1], limit=8*1024**2)
        self.assertEqual(first['prompt_cache_key'], current['prompt_cache_key'])
        self.assertEqual(first['text'], current['text'])
        self.assertEqual(wires[0].partition(b'QUESTION next-action\\n')[0],
                         wires[1].partition(b'QUESTION next-action\\n')[0])
        for wire in wires[2:]:
            current = decode(wire, limit=8*1024**2)
            self.assertNotEqual(first['prompt_cache_key'], current['prompt_cache_key'])
            self.assertNotEqual(first['text'], current['text'])
            self.assertEqual(first['input'][:-1], current['input'][:-1])
            self.assertNotEqual(first['input'][-1], current['input'][-1])

    def test_reviewer_schema_removes_unavailable_research(self):
        for services, expected in ((Services(), {'accept', 'reject'}),
                (Services(research=True, research_web_calls=3), {'accept', 'reject', 'research'})):
            with self.subTest(services=services):
                artifacts, snapshot = fixture(conditional=True)
                snapshot = replace(snapshot, services=services, cursors=('unused-cursor',),
                                   allowed_pins=('unused-result',))
                _, prepared, _ = dispatch(snapshot, artifacts)
                wire = decode(prepared.wire.wire, limit=8*1024**2)
                self.assertEqual(set(wire['outputSchema']['properties']['verdict']['enum']), expected)
                capabilities = next(m['text'] for m in wire['input'] if m['text'].startswith('CAPABILITIES '))
                self.assertIn('Verdicts: accept, reject', capabilities)
                for clutter in ('Helper modes', 'Candidate modes', 'direct_remaining', 'decomposition_remaining',
                                'repair_remaining', 'retrieval_remaining', 'probe_remaining', 'unused-'):
                    self.assertNotIn(clutter, capabilities)
                self.assertEqual('research_remaining:' in capabilities, 'research' in expected)

    def test_named_retrieval_can_progress_and_unavailable_actions_still_fail_admission(self):
        artifacts, snapshot = fixture()
        snapshot = replace(snapshot, services=Services())
        dispatched, _, _ = dispatch(snapshot, artifacts)
        invalid = (
            dict(mode='stop', objection=None, rationale=''),
            dict(mode='recall', rationale='Read the source.', result_id='source-prefix'),
            dict(mode='recall', rationale='Read a result.', result_id=''),
            dict(mode='research', rationale='Find a reference.', question='Which theorem applies?', urls=[]),
            dict(mode='page', rationale='Read more.', cursor='unavailable:1'),
        )
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.strategy.on_model(snapshot, self.policy, dispatched,
                    admitted(artifacts, dispatched, response(action)), artifacts)
        action = dict(mode='retrieve', rationale='Inspect the definition used in the goal.',
                      names=['Nat.Prime'], queries=[], backends=[])
        changes = self.strategy.on_model(snapshot, self.policy, dispatched,
            admitted(artifacts, dispatched, response(action)), artifacts)
        self.assertEqual(changes.helper.request.action.names, ('Nat.Prime',))

    def test_stop_waits_for_review_and_only_approval_reports_objections(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        action = dict(mode='stop', rationale='The statement is missing a hypothesis.',
            objection=dict(category='missing_assumption', evidence='A counterexample violates the goal.'))
        output = admitted(artifacts, dispatched, response(action))
        proposed = self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts)
        self.assertIsNone(proposed.stopped)
        self.assertIsNone(proposed.objection)
        self.assertIsNone(proposed.decision)
        self.assertIsNone(proposed.graph)
        self.assertEqual(proposed.work[0].operation, 'review_stop')
        request = proposed.stop_request
        self.assertEqual(StopRequest.load(request.payload), request)
        snapshot = replace(snapshot, stop_request=request)
        dispatched, prepared, _ = dispatch(snapshot, artifacts)
        self.assertEqual(dispatched.turn.role, 'reviewer')
        self.assertEqual(Turn.load(dispatched.turn.payload), dispatched.turn)
        self.assertEqual(prepared.turn.stop_request_hash, request.id)
        self.assertIsNone(prepared.turn.candidate_hash)
        verdict = dict(verdict='accept', rationale='The missing hypothesis is necessary.', findings=[], question='', urls=[])
        output = admitted(artifacts, dispatched, model_json(verdict))
        result = self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts)
        self.assertTrue(result.stopped)
        self.assertFalse(result.work)
        self.assertIsNone(result.decision)
        self.assertEqual(result.objection, replace(request.objection, source=output.raw))
        self.assertEqual(result.memory.observations[-1].status, 'stop_approved')
        for changed in (replace(snapshot, stop_request=None),
                        replace(snapshot, stop_request=replace(request, rationale='Different request.'))):
            with self.subTest(changed=changed.stop_request), self.assertRaisesRegex(ValueError, 'stop request changed'):
                dispatched.turn.validate_for(changed, continuation=True)

    def test_stop_rejection_requires_and_retains_continuation_feedback(self):
        artifacts, snapshot = fixture()
        snapshot = replace(snapshot, stop_request=StopRequest(snapshot.version.owner, 'No route found.', artifact('stop')))
        dispatched, _, _ = dispatch(snapshot, artifacts)
        finding = dict(category='progress', location='goal', issue='A constructor is available.', suggested_revision='')
        verdict = dict(verdict='reject', rationale='The goal is provable.', findings=[finding], question='', urls=[])
        with self.assertRaisesRegex(ValueError, 'feedback on how to continue'):
            self.strategy.on_model(snapshot, self.policy, dispatched,
                admitted(artifacts, dispatched, model_json(verdict)), artifacts)
        finding['suggested_revision'] = 'Apply the constructor, then prove its fields.'
        result = self.strategy.on_model(snapshot, self.policy, dispatched,
            admitted(artifacts, dispatched, model_json(verdict)), artifacts)
        self.assertFalse(result.stopped)
        self.assertEqual(result.work[0].operation, 'generate')
        self.assertIsNone(result.objection)
        self.assertIsNone(result.decision)
        self.assertIn(finding['suggested_revision'], result.memory.observations[-1].messages)

    def test_stop_review_research_preserves_the_exact_pending_request(self):
        artifacts, snapshot = fixture()
        request = StopRequest(snapshot.version.owner, 'No construction is possible.', artifact('stop'))
        snapshot = replace(snapshot, stop_request=request)
        dispatched, _, _ = dispatch(snapshot, artifacts)
        raw = model_json(dict(verdict='research', rationale='Check the construction.', findings=[],
                              question='Which construction applies?', urls=[]))
        result = self.strategy.on_model(snapshot, self.policy, dispatched, admitted(artifacts, dispatched, raw), artifacts)
        stage = result.helper.stages[0]
        evidence = StageEvidence(stage, receipt('research').request_ref.sha256, receipt('research'),
                                 ResultStatus.SUCCEEDED, (artifact('research'),))
        resumed = self.strategy.on_helper(snapshot, dispatched.turn, result.helper, (evidence,))
        self.assertEqual(resumed.work[0].operation, 'review_stop')
        self.assertIsNone(result.stopped)
        self.assertIsNone(result.objection)
        exhausted = replace(snapshot, usage=OwnerUsage(direct=self.policy.config.mathematics.direct,
                            decomposition=self.policy.config.mathematics.decomposition))
        self.assertEqual(self.strategy.plan(exhausted, self.policy).work[0].operation, 'review_stop')

    def test_retrieve_accepts_morley_subscript_names(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        names = ['EuclideanGeometry.' + name for name in (
            'law_cos', 'law_sin', 'angle_add_angle_add_angle_eq_pi',
            'ne₁₂_of_not_collinear', 'sin_pos_of_not_collinear')]
        action = dict(mode='retrieve', rationale='Check the geometric interfaces.', names=names,
                      queries=['angle_add_angle_add_angle_eq_pi'],
                      backends=[dict(name='frozen-source', prefixes=['mathlib/Mathlib/Geometry/Euclidean'])])
        changes = self.strategy.on_model(snapshot, self.policy, dispatched,
            admitted(artifacts, dispatched, response(action)), artifacts)
        self.assertEqual(changes.helper.request.action.names, tuple(names))
        self.assertEqual(len(changes.helper.stages), 1)

    def test_displayed_result_can_be_pinned_but_not_recalled(self):
        artifacts, snapshot = fixture()
        snapshot = replace(snapshot, allowed_pins=('displayed-result',))
        dispatched, prepared, _ = dispatch(snapshot, artifacts)
        wire = decode(prepared.wire.wire, limit=8*1024**2)
        helpers = next(line.removeprefix('Helper modes: ').split(', ') for line in wire['input'][-1]['text'].splitlines()
                       if line.startswith('Helper modes: '))
        self.assertNotIn('recall', helpers)
        self.assertNotIn('Saved evidence not included', wire['input'][-1]['text'])
        action = dict(mode='recall', rationale='Read the displayed result again.', result_id='displayed-result')
        with self.assertRaisesRegex(ValueError, 'Unknown strategy action'):
            self.strategy.on_model(snapshot, self.policy, dispatched,
                admitted(artifacts, dispatched, response(action)), artifacts)
        memory = dict(strategy='Use the displayed definition.', upsert=[], forget=[],
                      pin_evidence=['displayed-result'], unpin_evidence=[])
        action = dict(mode='retrieve', rationale='Inspect a related definition.', names=['Nat.add'],
                      queries=[], backends=[])
        changes = self.strategy.on_model(snapshot, self.policy, dispatched,
            admitted(artifacts, dispatched, response(action, memory)), artifacts)
        self.assertEqual(changes.memory.pins, ('displayed-result',))

    def test_live_scripted_and_supplied_candidate_paths(self):
        drafts = []
        for workflow in ('live', 'scripted'):
            artifacts, snapshot = fixture(workflow=workflow)
            dispatched, _, _ = dispatch(snapshot, artifacts)
            output = admitted(artifacts, dispatched, response(proof()))
            changes = self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts, submission_id=uid('same'))
            self.assertIsNone(changes.graph)
            self.assertEqual(changes.work[0].operation, 'admit_candidate')
            drafts.append(changes.draft)
        self.assertEqual(*drafts)
        _, snapshot = fixture(workflow='supplied')
        self.assertEqual(self.strategy.plan(snapshot, self.policy).reason, 'supplied_candidate_required')
        changes = self.strategy.submit(snapshot, self.policy, drafts[0].submission, guidance=drafts[0].guidance)
        self.assertEqual(changes.draft, drafts[0])

    def test_exact_dispatch_versions_and_receipts(self):
        artifacts, snapshot = fixture()
        dispatched, prepared, frozen = dispatch(snapshot, artifacts)
        self.assertEqual(Turn.load(prepared.turn.payload), prepared.turn)
        output = admitted(artifacts, dispatched, response(proof()))
        for mutated in (replace(snapshot, version=replace(snapshot.version, revision=1)),
                        replace(snapshot, version=replace(snapshot.version, evidence=1))):
            with self.assertRaises(ValueError):
                self.strategy.on_model(mutated, self.policy, dispatched, output, artifacts, submission_id=uid('x'))
        output.receipt = receipt('other')
        with self.assertRaises(ValueError):
            self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts, submission_id=uid('x'))
        with self.assertRaises(ValueError):
            Dispatch.bind(prepared, replace(frozen, job=replace(frozen.job, input_refs=(artifact('wrong'),))))

    def test_partial_refused_cancelled_outputs_never_update_memory(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        for status, error in ((ResultStatus.FAILED, 'incomplete'), (ResultStatus.SUCCEEDED, 'refusal'),
                              (ResultStatus.CANCELLED, None)):
            output = admitted(artifacts, dispatched, response(proof()), status=status, error=error)
            result = self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts)
            self.assertIsNone(result.draft)
            self.assertIsNone(result.memory)

    def test_last_attempt_can_succeed_and_next_is_gated(self):
        artifacts, snapshot = fixture()
        snapshot = replace(snapshot, usage=OwnerUsage(direct=6))
        dispatched, _, _ = dispatch(snapshot, artifacts)
        output = admitted(artifacts, dispatched, response(proof()))
        self.assertIsNotNone(self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts,
                                                    submission_id=uid('last')).draft)
        exhausted = replace(snapshot, usage=OwnerUsage(direct=7, decomposition=7))
        self.assertEqual(self.strategy.plan(exhausted, self.policy).reason, 'mathematical_allowance_exhausted')
        with self.assertRaises(ValueError):
            self.strategy.on_model(exhausted, self.policy, dispatched, output, artifacts, submission_id=uid('extra'))

    def test_binding_then_direct_check_is_only_proof_authority(self):
        _, snapshot = fixture()
        parent = snapshot.graph.node(snapshot.version.owner)
        value = candidate(parent)
        snapshot = replace(snapshot, attempt=Attempt(value))
        self.assertEqual(self.strategy.plan(snapshot, self.policy).work[0].operation, 'binding')
        binding = AdmittedBinding(parent.owner, parent.environment_id, parent.interface, receipt('binding'))
        change = self.strategy.on_binding(snapshot, binding)
        self.assertEqual(change.graph.node(parent.owner).state, ProofState.UNRESOLVED)
        snapshot = replace(snapshot, attempt=change.attempt, graph=change.graph)
        change = self.strategy.on_check(snapshot, self.policy, check(value))
        self.assertEqual(change.graph.node(parent.owner).state, ProofState.VERIFIED)
        self.assertEqual(change.decision.charge, 1)

    def test_binding_only_child_never_becomes_a_proof_job(self):
        _, snapshot = fixture()
        parent = snapshot.graph.node(snapshot.version.owner)
        value = candidate(parent)
        snapshot = replace(snapshot, attempt=Attempt(value, binding_only=True))
        binding = AdmittedBinding(parent.owner, parent.environment_id, parent.interface, receipt('binding'))
        result = self.strategy.on_binding(snapshot, binding)
        self.assertFalse(result.work)
        snapshot = replace(snapshot, graph=result.graph, attempt=result.attempt)
        self.assertEqual(self.strategy.plan(snapshot, self.policy).reason, 'binding_complete')
        with self.assertRaises(ValueError):
            self.strategy.on_check(snapshot, self.policy, check(value))

    def test_page_uses_advertised_cursor_and_duplicate_delivery_resumes(self):
        artifacts, snapshot = fixture()
        scope = Scope(snapshot.version.owner, snapshot.environment_id, snapshot.version.context)
        archive = prepare_archive(scope, 'a'*64, [{'answer': 'x'*40000}], (artifact('provenance'),), artifacts)
        snapshot = replace(snapshot, allowed_pins=(archive.id,), cursors=(archive.id+':1',))
        dispatched, _, _ = dispatch(snapshot, artifacts)
        action = dict(mode='page', rationale='Read the next page.', cursor=archive.id+':1')
        changes = self.strategy.on_model(snapshot, self.policy, dispatched, admitted(artifacts, dispatched, response(action)), artifacts)
        result = self.strategy.on_page(snapshot, dispatched.turn, changes.work[0], archive, artifacts)
        self.assertEqual(result.page.ordinal, 1)
        self.assertIsNotNone(result.page.next_cursor)
        duplicate = self.strategy.on_page(snapshot, dispatched.turn, changes.work[0], archive, artifacts, delivered=lambda key: True)
        self.assertEqual(duplicate.reason, 'already_delivered')
        self.assertIsNone(duplicate.page)
        self.assertEqual(duplicate.work[0].operation, 'generate')
        action = dict(mode='page', rationale='Read the next page.', cursor=result.page.next_cursor)
        with self.assertRaises(ValueError):
            self.strategy.on_model(snapshot, self.policy, dispatched, admitted(artifacts, dispatched, response(action)), artifacts)

    def test_unreachable_and_supplied_review_work_is_not_dispatched(self):
        _, snapshot = fixture(conditional=True, workflow='supplied')
        self.assertEqual(self.strategy.plan(snapshot, self.policy).reason, 'supplied_review_required')
        result = self.strategy.on_review(snapshot, self.policy, review(snapshot.attempt.candidate), evidence=artifact('review'))
        self.assertEqual(result.graph.node(snapshot.version.owner).state, ProofState.CONDITIONAL)
        detached = replace(snapshot, attempt=None, graph=replace(snapshot.graph, roots=()))
        self.assertEqual(self.strategy.plan(detached, self.policy).reason, 'unreachable_owner')

    def test_valid_repair_then_candidate_revision_remains_generation_fenced(self):
        artifacts, snapshot = fixture()
        base = expand(proof(), snapshot.context, uid('base'))
        snapshot = replace(snapshot, base=base)
        dispatched, _, _ = dispatch(snapshot, artifacts)
        action = dict(mode='revise', rationale='Use a new tactic.', edits=[dict(path='/entries/parent/proof', old='rfl', new='simp')])
        output = admitted(artifacts, dispatched, response(action))
        result = self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts, submission_id=uid('new'))
        self.assertEqual(load_submission(result.draft.submission)['entries'][0]['proof'], 'by simp')
        changed = replace(snapshot, base=expand(proof(), snapshot.context, uid('different-base')))
        with self.assertRaises(ValueError):
            self.strategy.on_model(changed, self.policy, dispatched, output, artifacts, submission_id=uid('new'))
        initial = replace(snapshot, base=None)
        initial_dispatch, _, _ = dispatch(initial, artifacts)
        with self.assertRaisesRegex(ValueError, 'dispatched base'):
            self.strategy.on_model(initial, self.policy, initial_dispatch,
                admitted(artifacts, initial_dispatch, response(action)), artifacts, submission_id=uid('new'))
        exhausted = replace(snapshot, usage=OwnerUsage(direct=self.policy.config.mathematics.direct))
        exhausted_dispatch, prepared, _ = dispatch(exhausted, artifacts)
        wire = decode(prepared.wire.wire, limit=8*1024**2)
        actions = {a['properties']['mode']['enum'][0]
                   for a in wire['outputSchema']['properties']['action']['anyOf']}
        self.assertIn('revise', actions)
        self.assertIn('revise: unavailable (direct allowance exhausted)', wire['input'][-1]['text'])
        with self.assertRaisesRegex(ValueError, 'direct_allowance_exhausted'):
            self.strategy.on_model(exhausted, self.policy, exhausted_dispatch,
                admitted(artifacts, exhausted_dispatch, response(action)), artifacts, submission_id=uid('new'))

    def test_unrelated_graph_commit_preserves_turn_but_owner_state_change_does_not(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        changed = replace(snapshot, graph=replace(snapshot.graph, revision=snapshot.graph.revision+1))
        dispatched.turn.validate_for(changed)
        with self.assertRaisesRegex(ValueError, 'Stale dispatched'):
            dispatched.turn.validate_for(replace(changed,
                version=replace(changed.version, revision=changed.version.revision+1)))

    def test_conditional_check_requires_independent_exact_review(self):
        artifacts, snapshot = fixture(conditional=True)
        checked = snapshot.attempt.checked
        result = self.strategy.on_check(replace(snapshot, attempt=replace(snapshot.attempt, checked=None)), self.policy, checked)
        self.assertIsNone(result.graph)
        self.assertEqual(result.work[0].operation, 'review')
        dispatched, _, _ = dispatch(snapshot, artifacts)
        raw = model_json(dict(verdict='accept', rationale='Valid reduction.', findings=[], question='', urls=[]))
        result = self.strategy.on_model(snapshot, self.policy, dispatched, admitted(artifacts, dispatched, raw), artifacts)
        self.assertEqual(result.graph.node(snapshot.version.owner).state, ProofState.CONDITIONAL)
        self.assertIsNone(result.memory)
        other = replace(snapshot.attempt.candidate, source=artifact('changed'))
        altered = replace(snapshot, attempt=Attempt(other, True, check(other)))
        with self.assertRaises(ValueError):
            self.strategy.on_model(altered, self.policy, dispatched, admitted(artifacts, dispatched, raw), artifacts)

    def test_review_rejection_counts_once_as_mathematical_outcome(self):
        _, snapshot = fixture(conditional=True)
        result = self.strategy.on_review(snapshot, self.policy, replace(review(snapshot.attempt.candidate), approved=False),
                                        evidence=artifact('review'), diagnostics=('Missing hypothesis.',))
        self.assertEqual(result.decision.charge, 1)
        self.assertIsNone(result.graph)
        self.assertEqual(result.memory.observations[0].messages, ('Missing hypothesis.',))

    def test_helpers_resume_same_turn_without_new_helper_charge(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        action = dict(mode='retrieve', rationale='Check a lemma.', names=['Nat.add_comm'],
                      queries=[], backends=[])
        result = self.strategy.on_model(snapshot, self.policy, dispatched,
            admitted(artifacts, dispatched, response(action)), artifacts)
        plan = result.helper
        self.assertEqual(plan.charge, 'retrieve')
        stage = plan.stages[0]
        admitted_stage = StageEvidence(stage, receipt('lookup').request_ref.sha256, receipt('lookup'),
                                      ResultStatus.SUCCEEDED, (artifact('lookup'),), available=stage.action.names)
        resumed = self.strategy.on_helper(snapshot, dispatched.turn, plan, (admitted_stage,))
        self.assertEqual(resumed.work[0].operation, 'generate')
        self.assertIsNone(resumed.helper)
        self.assertEqual(resumed.evidence.authority, 'advisory')
        with self.assertRaises(ValueError):
            self.strategy.on_helper(snapshot, dispatched.turn, replace(plan, request=replace(plan.request, continuation='other')),
                                    (admitted_stage,))

    def test_reviewer_research_resumes_review(self):
        artifacts, snapshot = fixture(conditional=True)
        dispatched, _, _ = dispatch(snapshot, artifacts)
        raw = model_json(dict(verdict='research', rationale='Check the endpoint.', findings=[],
                              question='Does the estimate include zero?', urls=[]))
        result = self.strategy.on_model(snapshot, self.policy, dispatched, admitted(artifacts, dispatched, raw), artifacts)
        self.assertEqual(result.helper.request.action.web_calls, 3)
        stage = result.helper.stages[0]
        evidence = StageEvidence(stage, receipt('research').request_ref.sha256, receipt('research'),
                                 ResultStatus.SUCCEEDED, (artifact('research'),))
        resumed = self.strategy.on_helper(snapshot, dispatched.turn, result.helper, (evidence,))
        self.assertEqual(resumed.work[0].operation, 'review')
        self.assertIsNone(result.memory)

    def test_probe_cases_are_binary_artifacts_and_case_allowance_is_shared(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        action = dict(mode='probe', rationale='Inspect the goal.', cases=[
            dict(id='one', code='Lean.Elab.Tactic.evalTactic (← `(tactic| rfl))'),
            dict(id='two', code='let env ← Lean.getEnv\nLean.logInfo m!"{(env.find? `Nat.add).isSome}"')])
        output = admitted(artifacts, dispatched, response(action))
        result = self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts)
        self.assertEqual(result.helper.units, 2)
        self.assertEqual(load_record(result.artifacts[0].data, limit=65536), dict(id='one',
            code='run_tac do\n  '+action['cases'][0]['code'], operation='tactic', setup='', expected=None))
        self.assertEqual(load_record(result.artifacts[1].data, limit=65536)['code'],
            'run_tac do\n  let env ← Lean.getEnv\n  Lean.logInfo m!"{(env.find? `Nat.add).isSome}"')
        self.assertIsNone(result.graph)
        depleted = replace(snapshot, usage=OwnerUsage(probe=self.policy.config.helpers.probe - 1))
        with self.assertRaises(ValueError):
            self.strategy.on_model(depleted, self.policy, dispatched, output, artifacts)

    def test_probe_rejects_wrappers_legacy_fields_and_generated_byte_overflow(self):
        artifacts, snapshot = fixture()
        dispatched, _, _ = dispatch(snapshot, artifacts)
        def apply(cases):
            output = admitted(artifacts, dispatched, response(dict(mode='probe', rationale='Inspect.', cases=cases)))
            return self.strategy.on_model(snapshot, self.policy, dispatched, output, artifacts)
        for wrapper in ('by', 'run_tac', 'run_elab', 'do'):
            for prefix in ('', '-- comment\n/- outer /- nested -/ -/\n'):
                with self.subTest(wrapper=wrapper, prefix=prefix), self.assertRaisesRegex(ValueError, 'omit the wrapper'):
                    apply([dict(id='one', code=prefix+wrapper+'\n  pure ()')])
        for cases in ([], [dict(id='one', code='pure ()', operation='tactic', setup='', expected=None)],
                      [dict(id='one', code='pure ()')]*2, [dict(id='0bad', code='pure ()')],
                      [dict(id='one', code='-- just a comment')]):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                apply(cases)
        body = 'Lean.logInfo "'+'α'*7986+'"'
        self.assertEqual(len(body.encode()), 15987)
        accepted = apply([dict(id='fits', code=body)])
        self.assertEqual(len(load_record(accepted.artifacts[0].data, limit=65536)['code'].encode()), 16000)
        with self.assertRaisesRegex(ValueError, 'Generated probe code exceeds'):
            apply([dict(id='overflow', code=body+' ')])

    def test_resource_and_protocol_failures_do_not_charge_math(self):
        _, snapshot = fixture(conditional=True)
        for kind in ('timeout', 'elaboration', 'protocol', 'cancelled', 'infrastructure'):
            result = self.strategy.on_failure(snapshot, self.policy, kind, evidence=artifact(kind))
            self.assertEqual(result.decision.charge, 0)
            self.assertIsNone(result.graph)
        snapshot = replace(snapshot, usage=OwnerUsage(timeout=self.policy.config.mathematics.timeout))
        result = self.strategy.on_failure(snapshot, self.policy, 'timeout', evidence=artifact('timeout'))
        self.assertFalse(result.work)
        self.assertIn('exhausted', result.reason)

    def test_recursion_depth_uses_elaboration_revision_allowance_and_retains_output(self):
        _, snapshot = fixture(conditional=True)
        diagnostic = 'maximum recursion depth has been reached (maxRecDepth=256)'
        result = self.strategy.on_failure(snapshot, self.policy, 'recursion_depth',
            evidence=artifact('depth'), diagnostics=(diagnostic,))
        self.assertEqual((result.decision.outcome, result.decision.charge, result.decision.exception),
                         ('exception', 0, 'elaboration'))
        self.assertEqual(result.revision_charge, 'elaboration')
        self.assertTrue(result.work)
        observation = result.memory.observations[-1]
        self.assertEqual(observation.status, 'recursion_depth')
        self.assertEqual(observation.messages, (diagnostic,))
        restored = Memory.load(result.memory.payload)
        self.assertEqual(restored.observations[-1], observation)
        exhausted = replace(snapshot, usage=OwnerUsage(elaboration=self.policy.config.mathematics.elaboration))
        result = self.strategy.on_failure(exhausted, self.policy, 'recursion_depth',
            evidence=artifact('depth'), diagnostics=(diagnostic,))
        self.assertFalse(result.work)
        self.assertEqual(result.reason, 'elaboration_revision_allowance_exhausted')
        self.assertEqual(result.decision.charge, 0)

    def test_replacement_schema_requires_explicit_interpreter(self):
        artifacts, snapshot = fixture()
        work = self.strategy.plan(snapshot, self.policy).work[0]
        display = Display((Section('subject', 'goal', b'True'),), (snapshot.version.context,), (snapshot.version.owner,))
        with self.assertRaises(ValueError):
            self.strategy.prepare_model(snapshot, self.policy, work, display, Options('codex', 'm'),
                                        renderer=lambda s: replace(prover.render(s), contract='other'))


class RepairTests(unittest.TestCase):
    def test_only_current_direct_consumers_reopen_and_shared_children_survive(self):
        artifacts, snapshot = fixture()
        parent, child = snapshot.graph.nodes
        other, grand = node('other'), node('grand')
        graph = ProofGraph(0, (grand.owner.obligation_id, other.owner.obligation_id), (grand, parent, child, other))
        graph = select(graph, parent, (child,))
        graph = select(graph, other, (child,))
        graph = select(graph, grand, (parent,))
        report = Objection(child.owner, 'missing_assumption', 'The claim fails when x is zero.', artifact('objection'))
        events = consumers(graph, report)
        self.assertEqual({e.owner for e in events}, {parent.owner, other.owner})
        snapshot = replace(snapshot, graph=graph)
        event = next(e for e in events if e.owner == parent.owner)
        result = reopen(snapshot, Policy(Config()), event)
        self.assertEqual(result.repair_allowance, 2)
        self.assertEqual(result.graph.node(parent.owner).state, ProofState.UNRESOLVED)
        self.assertEqual(result.cancel_after_commit, ())
        self.assertEqual(result.graph.node(child.owner).owner, child.owner)
        self.assertEqual(graph.node(parent.owner).state, ProofState.CONDITIONAL)
        again = reopen(replace(snapshot, graph=result.graph), Policy(Config()), event)
        self.assertEqual(again.reason, 'obsolete_consumer_plan')

    def test_repair_budget_and_unreachable_cancellation(self):
        _, snapshot = fixture()
        parent, child = snapshot.graph.nodes
        graph = select(snapshot.graph, parent, (child,))
        report = Objection(child.owner, 'suspected_false', 'A concrete counterexample.', artifact('objection'))
        event = consumers(graph, report)[0]
        snapshot = replace(snapshot, graph=graph)
        result = reopen(snapshot, Policy(Config()), event)
        self.assertEqual(result.cancel_after_commit, (child.owner,))
        blocked = replace(snapshot, usage=OwnerUsage(backtracks=3))
        self.assertEqual(reopen(blocked, Policy(Config()), event).reason, 'owner_backtrack_limit')


class ArchitectureTests(unittest.TestCase):
    def test_dependency_direction_and_no_ambient_execution(self):
        directory = Path(__file__).parents[1] / 'midas' / 'strategy'
        for path in directory.glob('*.py'):
            tree = ast.parse(path.read_text())
            for item in ast.walk(tree):
                if isinstance(item, ast.ImportFrom) and item.level == 2:
                    self.assertIn(item.module.split('.')[0], ('domain', 'policy', 'prompt', 'tools'))
                if isinstance(item, (ast.Import, ast.ImportFrom)):
                    names = [n.name for n in item.names] if isinstance(item, ast.Import) else [item.module or '']
                    self.assertFalse(any(n.split('.')[0] in ('sqlite3', 'subprocess', 'midas_formalizer', 'json') for n in names), path)


if __name__ == '__main__':
    unittest.main()
