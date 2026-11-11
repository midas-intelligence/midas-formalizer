"""Offline prompt material fidelity; no compiler, network or provider calls."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from midas.application import presentation
from midas.prompt import Snapshot, prover, subject
from midas.strategy import Lesson, Memory, Observation, expand, fragments, revise, update_memory
from tests.test_domain import artifact, uid
from tests.test_source import signature
from tests.test_strategy import context, decomposition, proof


class PresentationTests(unittest.TestCase):
    def test_successful_proofs_are_hidden_but_statements_dependencies_and_guidance_remain(self):
        action = decomposition()
        action['children'][1].update(proof_body='by exact unique_successful_term',
                                     proof='Retain this English plan.')
        ctx = context()
        draft = expand(action, ctx, uid('mixed-status'))
        text = presentation.candidate(draft, ctx, compiled=('first',),
            failures={'parent': {'stage': 'proof'}}, blocked=('second',)).text
        self.assertIn(b'Proof: compiled successfully.', text)
        self.assertNotIn(b'unique_successful_term', text)
        self.assertNotIn(b'/entries/first/proof', text)
        self.assertIn(b'/entries/parent/proof', text)
        self.assertIn(action['proof_body'].encode(), text)
        self.assertIn(b'proof failed; see node feedback', text)
        self.assertIn(b'blocked by an unavailable dependency; not compiled', text)
        self.assertIn(b'first <- local entry first', text)
        self.assertIn(b'Retain this English plan.', text)
        self.assertIn(action['children'][1]['statement'].encode(), text)
        reviewed = presentation.candidate(draft, ctx, selected='parent', compiled=('first',)).text
        self.assertNotIn(b'unique_successful_term', reviewed)
        self.assertEqual(reviewed.count(b'Proof: compiled successfully.'), 1)
        self.assertIn(action['proof_body'].encode(), reviewed)
        self.assertIn(b'Proof: pending obligation', reviewed)
        self.assertIn(b'first <- local entry first', reviewed)
        self.assertIn(b'Retain this English plan.', reviewed)

    def test_candidate_contains_dependency_map_guidance_and_exact_fragments_once(self):
        action = decomposition()
        action['proof'] = 'An English plan with ``` fences.\n\n'
        action['definitions'] = [dict(kind='abbrev', name='Shared', type='Prop', value='True')]
        ctx = context()
        draft = expand(action, ctx, uid('display-candidate'))
        before = draft.payload
        # No JSON operation is needed even for arbitrarily large authored strings.
        with patch('json.dumps', side_effect=AssertionError('JSON encode')), \
             patch('json.loads', side_effect=AssertionError('JSON decode')):
            text = presentation.candidate(draft, ctx).text
        self.assertEqual(draft.payload, before)
        self.assertIn(b'first <- local entry first', text)
        self.assertIn(b'Definition: abbrev Shared', text)
        # The parent's fixed statement cannot use the new definition; its proof
        # and the child statements can. Only the narrower scope needs a label.
        self.assertEqual(text.count(b'Definitions in statement:'), 1)
        self.assertIn(b'Definitions in statement: none', text)
        self.assertNotIn(b'Definitions in proof:', text)
        self.assertIn(b'All listed definitions are available in statements and proofs', text)
        self.assertIn(b'Proof: pending obligation', text)
        self.assertNotIn(b'by sorry', text)
        self.assertNotIn(uid('display-candidate').encode(), text)
        for path, raw in fragments(draft, ctx):
            self.assertEqual(text.count(b'Editable fragment: ' + path.encode() + b'\n'), 1)
            self.assertIn(presentation.exact(raw), text)
        self.assertIn(b'````\nAn English plan with ``` fences.\n\n\n````', text)
        with self.assertRaisesRegex(ValueError, 'missing'):
            presentation.candidate(draft, ctx, selected='absent')
        with self.assertRaisesRegex(ValueError, 'differs'):
            presentation.candidate(draft, ctx, selected='parent', checked=b'by trivial')
        with patch('json.dumps', side_effect=AssertionError('JSON encode')), \
             patch('json.loads', side_effect=AssertionError('JSON decode')):
            reviewed = presentation.candidate(draft, ctx, selected='parent', checked=action['proof_body'].encode())
        self.assertNotIn(action['proof_body'].encode(), reviewed.text)
        self.assertIn(b'Proof: compiled successfully.', reviewed.text)
        self.assertIn(b'unproved dependencies remain obligations.', reviewed.text)
        self.assertIn(b'Selected review entry: parent', reviewed.text)
        self.assertIn(b'first <- local entry first', reviewed.text)
        self.assertIn(b'Type:\n' + presentation.exact('Prop'), reviewed.text)
        self.assertIn(b'Value:\n' + presentation.exact('True'), reviewed.text)
        self.assertEqual(reviewed.text.count(b'Definitions in statement:'), 1)
        self.assertIn(b'Definitions in statement: none', reviewed.text)
        self.assertNotIn(b'Definitions in proof:', reviewed.text)
        for path, raw in fragments(draft, ctx):
            if raw and path != '/entries/parent/proof':
                self.assertIn(presentation.exact(raw), reviewed.text)
        for clutter in (b'Editable', b' fragment:', b'/entries/', b'new candidate action', b'by sorry'):
            self.assertNotIn(clutter, reviewed.text)

    def test_exact_string_boundaries_round_trip_large_unicode_and_empty_fields(self):
        for raw in ('', '\n', 'α\\"\n```\ntrailing\n\n' * 20000):
            with patch('json.dumps', side_effect=AssertionError('JSON encode')):
                framed = presentation.exact(raw)
            fence, _, rest = framed.partition(b'\n')
            self.assertEqual(rest[:-len(fence)-1].decode(), raw)
            self.assertNotIn(fence, raw.encode())

    def test_memory_displays_latest_check_preserving_lessons_and_omissions(self):
        old = Observation('checker', 'rejected', artifact('old'), ('old failure\n  exact line',), 2)
        latest = replace(old, status='binding_failed', messages=('latest failure\n  another line',), omitted=1)
        saved = Memory(lessons=(Lesson('invariant', 'Keep its boundary case.'),),
                       observations=(old, latest), evicted=3)
        sections = presentation.memory(saved)
        rendered = prover.render(Snapshot((subject('goal', signature()), *sections)))
        self.assertFalse(any(s.kind == 'history' for s in rendered.sections))
        feedback = next(s for s in rendered.sections if s.kind == 'feedback')
        memory = next(s for s in rendered.sections if s.kind == 'memory')
        self.assertNotIn(old.messages[0].encode(), feedback.text)
        self.assertNotIn(b'2 diagnostic message(s) omitted', feedback.text)
        self.assertIn(latest.messages[0].encode(), feedback.text)
        self.assertIn(b'1 diagnostic message(s) omitted', feedback.text)
        self.assertIn(b'Lesson invariant:\nKeep its boundary case.', memory.text)
        self.assertNotIn(b'Pinned evidence:', memory.text)
        self.assertIn(b'3 older memory record(s) evicted', memory.text)
        self.assertNotIn(old.evidence.id.encode(), feedback.text)
        self.assertEqual(saved.observations, (old, latest))

    def test_latest_failed_edit_keeps_current_check_until_a_new_check_arrives(self):
        old = Observation('checker', 'rejection', artifact('old'), ('Outdated compiler error.',))
        current = replace(old, evidence=artifact('current'), messages=('Current compiler error.',))
        first_edit = Observation('revision', 'revision_failed', artifact('first-edit'), ('First failed edit.',))
        last_edit = replace(first_edit, evidence=artifact('last-edit'), messages=('Latest failed edit.',))
        protocol = Observation('protocol', 'protocol', artifact('protocol'), ('Invalid action.',))
        objection = Observation('objection', 'missing_assumption', artifact('objection'), ('A hypothesis is missing.',))
        saved = Memory(observations=(objection, old, current, first_edit, last_edit, protocol))
        before = saved.payload
        sections = presentation.memory(saved)
        self.assertEqual(presentation.memory(Memory.load(saved.payload)), sections)
        text = b'\n'.join(s.text for s in sections)
        for message in (*current.messages, *last_edit.messages, *objection.messages, *protocol.messages):
            self.assertIn(message.encode(), text)
        for message in (*old.messages, *first_edit.messages):
            self.assertNotIn(message.encode(), text)
        self.assertEqual(sum(s.kind == 'feedback' for s in sections), 3)
        for source in ('checker', 'review'):
            with self.subTest(source=source):
                next_check = replace(current, source=source, evidence=artifact('next'), messages=('New candidate feedback.',))
                updated = update_memory(saved, observation=next_check)
                text = b'\n'.join(s.text for s in presentation.memory(updated))
                self.assertIn(b'New candidate feedback.', text)
                self.assertIn(objection.messages[0].encode(), text)
                for message in (*old.messages, *current.messages, *first_edit.messages, *last_edit.messages):
                    self.assertNotIn(message.encode(), text)
        self.assertEqual(saved.payload, before)

    def test_check_protocol_failure_is_visible_and_supersedes_old_candidate_errors(self):
        old = Observation('checker', 'rejection', artifact('old'), ('An outdated error.',))
        failed_edit = Observation('revision', 'revision_failed', artifact('edit'), ('An outdated edit failure.',))
        latest = Observation('checker', 'protocol', artifact('latest'), ('A protocol failure.',))
        sections = presentation.memory(Memory(observations=(old, failed_edit, latest)))
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].kind, 'feedback')
        self.assertIn(b'A protocol failure.', sections[0].text)

    def test_protocol_feedback_keeps_only_latest_rejection_and_current_check(self):
        checked = Observation('checker', 'rejection', artifact('check'), ('Fix the proof.',))
        failures = tuple(Observation('protocol', 'protocol', artifact(str(i)),
            (f'Action rejected: invalid-{i}',)) for i in range(6))
        sections = presentation.memory(Memory(observations=(checked, *failures)))
        self.assertEqual(len(sections), 2)
        text = b'\n'.join(s.text for s in sections)
        self.assertIn(b'Fix the proof.', text)
        self.assertIn(b'Action rejected: invalid-5', text)
        self.assertNotIn(b'Action rejected: invalid-4', text)
        self.assertTrue(all(s.kind == 'feedback' for s in sections))

    def test_consecutive_revisions_replace_code_and_errors_without_prompt_growth(self):
        ctx = context()
        draft = expand(dict(proof(), proof_body='by exact candidate_0'), ctx, uid('base'))
        memory = Memory(lessons=(Lesson('approach', 'Keep the useful approach.'),))
        sizes = []
        for index in range(4):
            with self.subTest(revision=index), patch('json.dumps', side_effect=AssertionError('JSON encode')), \
                 patch('json.loads', side_effect=AssertionError('JSON decode')):
                memory = update_memory(memory, observation=Observation('checker', 'rejection',
                    artifact('check-'+str(index)), (f'Unknown identifier candidate_{index}.',)))
                rendered = prover.render(Snapshot((subject('goal', signature()),
                    presentation.candidate(draft, ctx), *presentation.memory(memory))))
                text = b'\n'.join(s.text for s in rendered.sections)
                sizes.append(len(text))
                self.assertEqual(text.count(f'by exact candidate_{index}'.encode()), 1)
                self.assertEqual(text.count(f'Unknown identifier candidate_{index}.'.encode()), 1)
                self.assertIn(b'Keep the useful approach.', text)
                for previous in range(index):
                    self.assertNotIn(f'candidate_{previous}'.encode(), text)
                if index < 3:
                    action = dict(mode='revise', rationale='Use the next candidate.', edits=[
                        dict(path='/entries/parent/proof', old=f'candidate_{index}', new=f'candidate_{index+1}')])
                    draft = revise(action, draft, ctx, uid('revision-'+str(index)))
        # The initial rationale differs; subsequent equal-size revisions stay flat.
        self.assertEqual(len(set(sizes[1:])), 1)
        self.assertEqual(len(memory.observations), 4)

    def test_review_evidence_filters_search_records_and_binds_research_to_candidate(self):
        from midas.application import helpers, records as r
        from midas.prompt import Section
        from midas.tools import Backend, HelperRequest, Lookup, Research, Retrieve, Scope, Stage, WebSearch, prepare_archive
        from tests.test_strategy import dispatch, fixture
        artifacts, snapshot = fixture(conditional=True)
        _, prepared, _ = dispatch(snapshot, artifacts)
        scope = Scope(snapshot.version.owner, snapshot.environment_id, snapshot.version.context)
        saved, archives = {}, []
        def add(action, rows, *, lookup=None, parent=None, turn=None):
            request = HelperRequest(scope, action, str(len(archives)))
            archive = prepare_archive(scope, request.id, rows, (artifact('provenance'),), artifacts)
            stages = (Stage(request.id, 0, scope, lookup),) if lookup else ()
            saved[request.id] = dict(request=r.ref(artifacts.save(request.payload)),
                stages=[r.ref(artifacts.save(s.payload)) for s in stages],
                results={str(s.ordinal): [s.ordinal, '', None, 'SUCCEEDED', [], list(s.action.names), [], None]
                         for s in stages}, parent=parent,
                lookup_records=[[name, i] for i, name in enumerate(lookup.names)] if lookup else [],
                turn=r.ref(artifacts.save((turn or prepared.turn).payload)))
            archives.append(archive)
            return archive
        retrieval = add(Retrieve(queries=('addition',), backends=(Backend('frozen-source', ('project',)),)),
            ['theorem Nat.add_comm : a + b = b + a', 'theorem Nat.mul_comm : a * b = b * a',
             'Search (frozen-source): addition', 'Nat.add_comm\nUnrelated search excerpt'],
            lookup=Lookup(('Nat.add_comm', 'Nat.mul_comm')))
        fragmented = add(Retrieve(names=('Nat.add_assoc',)), ['def Nat.add_assoc\n' + 'exact supporting source\n'*1500],
                         lookup=Lookup(('Nat.add_assoc',)))
        add(Research('Earlier prover research'), [dict(answer='PROVER_RESEARCH')])
        add(Research('Old candidate research'), [dict(answer='STALE_RESEARCH')],
            parent=['submission', 'parent'], turn=replace(prepared.turn, candidate_hash='a'*64))
        current = add(Research('Current review research'), [dict(answer='CURRENT_RESEARCH')],
                      parent=['submission', 'parent'])
        add(WebSearch('Old candidate references'), [dict(answer='STALE_WEB_SEARCH')],
            parent=['submission', 'parent'], turn=replace(prepared.turn, candidate_hash='a'*64))
        current_web = add(WebSearch('Current review references'), [dict(answer='CURRENT_WEB_SEARCH')],
                          parent=['submission', 'parent'])
        sections = (Section('candidate', 'candidate', b'Use Nat.add_comm and Nat.add_assoc.'),)
        displayed = tuple(ref for a in archives for ref in a.pages)
        with patch.object(helpers, 'advertised', return_value=tuple(archives)), \
             patch.object(r, 'value', side_effect=lambda reader, scope, key: saved[key]):
            result = tuple(helpers.review_evidence(artifacts, snapshot, ('submission', 'parent'), sections, displayed))
        text = b'\n'.join(content for _, content in result)
        for useful in (b'Nat.add_comm', b'Nat.add_assoc', b'CURRENT_RESEARCH', b'CURRENT_WEB_SEARCH'):
            self.assertIn(useful, text)
        for clutter in (b'Nat.mul_comm', b'Unrelated search excerpt', b'Search (', b'PROVER_RESEARCH', b'STALE_RESEARCH', b'STALE_WEB_SEARCH'):
            self.assertNotIn(clutter, text)
        self.assertEqual({ref for refs, _ in result for ref in refs},
                         set((*retrieval.pages, *fragmented.pages, *current.pages, *current_web.pages)))
        for i, archive in enumerate(archives, 1):
            for refs, content in result:
                if set(refs).intersection(archive.pages):
                    if archive in (retrieval, fragmented):
                        self.assertEqual(refs, archive.pages)
                        self.assertNotIn(b'Result ID:', content)
                        self.assertNotIn(b'Page ', content)
                    else:
                        self.assertIn(f'Result ID: tool-result-{i}\n'.encode(), content)
                    self.assertNotIn(archive.id.encode(), content)
        self.assertNotIn(b'Continued excerpt', text)
        self.assertNotIn(b'Excerpt continues on the next page.', text)
        self.assertEqual(text.count(b'exact supporting source\n'), 1500)


if __name__ == '__main__':
    unittest.main()
