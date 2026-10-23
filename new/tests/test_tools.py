"""Offline tools contracts; no Lean execution, hosted calls or model calls."""
from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from midas.domain import (ArtifactRef, CapacityGrant, FrozenRequest, Invocation, OwnerVersion,
    ReceiptBinding, ResourceLimits, ResultStatus, Session, Termination)
from midas.domain import encoding as codec
from midas.policy import Config, OwnerUsage, Policy, Services
from midas.source import SourceFile
from midas.tools import (Archive, Backend, HelperRequest, Lookup, Match, Probe, Research, Retrieve,
    Scope, Search, SearchResult, SearchService, SourceEntry, SourceIndex, Stage, StageEvidence,
    admit_checked, continue_retrieval, execute_search, finish_helper, fuse, lookup_sources, plan_helper,
    prepare_archive, probe_summary, read_page, research_evidence, search_stages, search_summary, source_excerpt, web_actions)
from midas.tools.protocol import pack
from midas.tools.search import hosted, worker_payload


class Artifacts:
    def __init__(self):
        self.data, self.reads = {}, []

    def write_bounded(self, data, *, max_bytes):
        if len(data) > max_bytes:
            raise ValueError('artifact byte limit')
        ref = ArtifactRef(str(uuid4()), sha256(data).hexdigest(), len(data))
        self.data[ref.id] = data
        return ref

    def save(self, data):
        return self.write_bounded(data, max_bytes=len(data))

    def read_artifact(self, ref, *, max_bytes):
        self.reads.append(ref)
        return ref.verify(self.data[ref.id], max_bytes=max_bytes)


def events(*values):
    result = bytearray()
    for value in values:
        data = pack(value, 4*1024**2)
        result.extend(len(data).to_bytes(8, 'little'))
        result.extend(data)
    return bytes(result)


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.a = Artifacts()
        self.scope = Scope(OwnerVersion('obl_'+'1'*64, 0), 'env_'+'2'*64, self.a.save(b'frozen context'))
        self.cancel = threading.Event()
        self.deadline = time.monotonic()+30
        self.policy = Policy(Config())
        self.services = Services(research=True, research_web_calls=12)

    def request(self, action, **kwargs):
        return HelperRequest(self.scope, action, 'resume original attempt', **kwargs)

    def plan(self, action, **kwargs):
        return plan_helper(self.request(action, **kwargs), self.policy, OwnerUsage(), self.services)

    def stage(self, action):
        return Stage('3'*64, 0, self.scope, action)

    def index(self, entries):
        return SourceIndex(self.scope.environment_id,
            tuple(SourceEntry(path, self.a.save(data), path.split('/', 1)[1].removesuffix('.lean').replace('/', '.'))
                  for path, data in entries))

    def receipt(self, raw=b'frozen request'):
        return ReceiptBinding(str(uuid4()), self.a.save(raw), self.a.save(b'result'), self.a.save(b'seal'))

    def test_request_binary_roundtrips_and_binding(self):
        for action in (Retrieve(names=('Nat.add',)),
                Retrieve(('compact',), ('Nat.add',), (Backend('frozen-source', ('Mathlib',)),)),
                Probe((self.a.save(pack({'id': 'p', 'code': 'simp'})),)), Research('Why?', ('https://example.test',))):
            request = self.request(action)
            with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
                self.assertEqual(HelperRequest.load(request.payload), request)
                stage = self.plan(action).stages[0]
                self.assertEqual(Stage.load(stage.payload), stage)
            self.assertNotEqual(request.id, replace(request, continuation='other').id)
            self.assertNotEqual(request.id, replace(request, scope=replace(self.scope, owner=replace(self.scope.owner, generation=1))).id)
            self.assertNotIn(b'toolchain', request.payload)
        with self.assertRaises(ValueError):
            HelperRequest.load(pack(['midas-helper-v1', self.scope.value(), ['lookup', 'Nat.add'], 'c', []]))

    def test_search_and_lookup_are_only_internal_stages(self):
        from midas.tools.protocol import action_value
        for action in (Search('compact', Backend('leanexplore')), Lookup(('Nat.add',))):
            with self.subTest(action=action):
                stage = self.stage(action)
                self.assertEqual(Stage.load(stage.payload), stage)
                with self.assertRaisesRegex(ValueError, 'internal stages'):
                    self.request(action)
                with self.assertRaisesRegex(ValueError, 'internal stages'):
                    HelperRequest.load(pack(['midas-helper-v1', self.scope.value(), action_value(action), 'c', []]))

    def test_retrieval_encoding_rejects_removed_offset(self):
        value = self.request(Retrieve(names=('Nat.add',))).value()
        value[2].append(0)
        with self.assertRaisesRegex(ValueError, 'Unknown helper action or fields'):
            HelperRequest.load(pack(value))

    def test_action_validation(self):
        invalid = (lambda: Lookup(('by exact True.intro',)), lambda: Lookup(('Nat.add', 'Nat.add')),
            lambda: Retrieve(('query',)), lambda: Retrieve(backends=(Backend('leanexplore'),)),
            lambda: Search('x', Backend('leanexplore'), True), lambda: Backend('leanexplore', ('Mathlib',)),
            lambda: Backend('frozen-source', ('../secret',)), lambda: Backend('frozen-source', ('runtime',)),
            lambda: Probe(()), lambda: Research('q', ('https://user:password@host',)), lambda: Research('q', web_calls=13))
        for build in invalid:
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()

    def test_subscript_lookup_names_keep_forbidden_name_checks(self):
        name = 'EuclideanGeometry.ne₁₂_of_not_collinear'
        quoted = 'EuclideanGeometry.«ne₁₂_of_not_collinear»'
        self.assertEqual(self.plan(Retrieve(names=(name,))).stages[0].action.names, (name,))
        for names, forbidden in (((name,), (quoted,)), ((quoted,), (name,)), (('Midas.secret',), ())):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, 'no allowed names'):
                self.plan(Retrieve(names=names), forbidden=forbidden)

    def test_policy_is_used_for_request_units_and_research_bounds(self):
        request = self.request(Probe(tuple(self.a.save(pack({'id': str(n)})) for n in range(3))))
        remaining = OwnerUsage(probe=5)
        self.assertIn('probe', self.policy.capabilities(remaining, self.services).available_modes)
        for count in (1, 2, 3):
            batch = replace(request, action=Probe(request.action.cases[:count]))
            plan = plan_helper(batch, self.policy, remaining, self.services)
            self.assertEqual(plan.units, 1)
            self.assertEqual(plan.stages[0].action.cases, batch.action.cases)
        with self.assertRaisesRegex(ValueError, 'probe_allowance'):
            plan_helper(request, self.policy, OwnerUsage(probe=6), self.services)
        research = plan_helper(self.request(Research('q')), self.policy, OwnerUsage(),
                               replace(self.services, research_web_calls=2))
        self.assertEqual(research.request.action.web_calls, 2)
        for action in (Retrieve(names=('Nat.add',)),
                       Retrieve(('q',), backends=(Backend('leanexplore'),)), request.action):
            with self.assertRaisesRegex(ValueError, 'compiler_not_configured'):
                plan_helper(self.request(action), self.policy, OwnerUsage(), Services(compiler=False))
        with self.assertRaisesRegex(ValueError, 'retrieval_allowance'):
            plan_helper(self.request(Retrieve(('q',), backends=(Backend('leanexplore'),))),
                        self.policy, OwnerUsage(retrieval=10), self.services)

    def test_source_selector_resolution_and_ambiguity(self):
        index = self.index((('mathlib/Mathlib/Topology/Basic.lean', b''), ('lean/Init/Prelude.lean', b'')))
        for selector in ('Mathlib.Topology.Basic', 'Mathlib/Topology/Basic', 'mathlib/Mathlib/Topology/Basic.lean'):
            self.assertEqual(index.resolve((selector,)), ('mathlib/Mathlib/Topology/Basic.lean',))
        self.assertEqual(index.resolve(('Mathlib',)), ('mathlib/Mathlib',))
        other = SourceIndex(index.environment_id, index.entries+(SourceEntry('project/Mathlib/Topology/Basic.lean', self.a.save(b'')),))
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            other.resolve(('Mathlib.Topology.Basic',))
        self.assertEqual(other.resolve(('project/Mathlib/Topology/Basic',)), ('project/Mathlib/Topology/Basic.lean',))
        for value in ('/Mathlib', 'checker/Foo', 'Mathlib/../secret', 'missing'):
            with self.assertRaises(ValueError):
                index.resolve((value,))

    def test_source_search_content_names_and_bounded_excerpts(self):
        source = b'namespace Topology\n-- compact sets\ntheorem compact_set : True := by trivial\nend Topology\n'
        index = self.index((('mathlib/Mathlib/Compact.lean', source), ('mathlib/Mathlib/Other.lean', b'-- irrelevant\n')))
        stage = self.stage(Search('compact', Backend('frozen-source', ('Mathlib',))))
        result = execute_search(stage, self.a, self.cancel, self.deadline, index=index)
        self.assertTrue(result.records)
        self.assertIn('Topology.compact_set', result.records[0].names)
        self.assertEqual(SearchResult.load(result.payload), result)
        no_path_match = self.stage(Search('Other', Backend('frozen-source', ('Mathlib/Other',))))
        self.assertFalse(execute_search(no_path_match, self.a, self.cancel, self.deadline, index=index).records)

    def test_source_hash_environment_size_and_stop_fail_closed(self):
        index = self.index((('mathlib/Mathlib/X.lean', b'-- compact\n'),))
        stage = self.stage(Search('compact', Backend('frozen-source', ('Mathlib',))))
        with self.assertRaises(ValueError):
            execute_search(stage, self.a, self.cancel, self.deadline, index=replace(index, environment_id='env_'+'3'*64))
        self.a.data[index.entries[0].artifact.id] = b'changed'
        with self.assertRaises(ValueError):
            execute_search(stage, self.a, self.cancel, self.deadline, index=index)
        self.cancel.set()
        with self.assertRaises(TimeoutError):
            execute_search(stage, self.a, self.cancel, self.deadline, index=index)
        oversized = replace(index.entries[0], artifact=replace(index.entries[0].artifact, byte_count=2*1024**2+1))
        self.cancel.clear()
        with self.assertRaisesRegex(ValueError, 'byte allowance'):
            execute_search(stage, self.a, self.cancel, self.deadline, index=replace(index, entries=(oversized,)))

    def test_hosted_fixed_endpoint_bounded_protocol_and_no_json(self):
        action = Search('compact α', Backend('leanexplore'))
        raw = b'{"results":[{"name":"Nat.add","description":"'+b'x'*200000+b'"}]}'
        calls = []
        def opener(request, *, timeout):
            calls.append((request.full_url, timeout))
            return io.BytesIO(raw)
        with patch('json.loads', side_effect=AssertionError('large JSON')), patch('json.dumps', side_effect=AssertionError('large JSON')):
            self.assertEqual(hosted(action, self.cancel, self.deadline, opener=opener), (Match(('Nat.add',)),))
        self.assertIn('https://www.leanexplore.com/api/v2/search?q=compact+', calls[0][0])
        self.assertLessEqual(calls[0][1], 15)
        for bad in (b'{"results":[],"results":[]}', b'{"results":[{"name":2}]}', b'['*20+b']'*20,
                    b'{"results":[],"bad":NaN}', b'{"results":[]} trailing'):
            with self.subTest(raw=bad), self.assertRaises(ValueError):
                hosted(action, self.cancel, self.deadline, opener=lambda *a, **k: io.BytesIO(bad))
        with patch('midas.tools.search.HOSTED_BYTES', 20), self.assertRaises(ValueError):
            hosted(action, self.cancel, self.deadline, opener=opener)

    def test_hosted_redirects_rejected(self):
        from midas.tools.search import _NoRedirect
        with self.assertRaises(ValueError):
            _NoRedirect().redirect_request(None, None, 302, None, None, 'https://elsewhere')

    def test_search_receipt_execution_and_tampering(self):
        stage = self.stage(Search('q', Backend('leanexplore')))
        limits = ResourceLimits(1000, 256, 262144, 262144)
        frozen = FrozenRequest(stage.job(limits), self.scope.environment_id, self.a.save(stage.payload), self.a.save(b'frozen request'))
        inv = Invocation(str(uuid4()), Session(str(uuid4()), str(uuid4()), 1), frozen, self.a.save(b'reservation'))
        ctx = SimpleNamespace(capacity_grant=CapacityGrant(inv), reader=self.a, staging=self.a,
                              cancellation=self.cancel, deadline=self.deadline)
        service = SearchService(transport=lambda *args: (Match(('Nat.add',)),))
        result = service.execute(frozen, ctx)
        receipt = ReceiptBinding(inv.original_execution_id, frozen.request_ref, self.a.save(worker_payload(result)), self.a.save(b'seal'))
        authority = SimpleNamespace(verify_receipt=lambda *args: True)
        self.assertEqual(service.admit_receipt(stage, inv, result, receipt, self.a, authority).records, (Match(('Nat.add',)),))
        for changed in (replace(result, termination=Termination.UNKNOWN), replace(result, request_hash='0'*64)):
            with self.assertRaises(ValueError):
                service.admit_receipt(stage, inv, changed, receipt, self.a, authority)
        with self.assertRaises(ValueError):
            service.admit_receipt(stage, inv, result, receipt, self.a, SimpleNamespace(verify_receipt=lambda *a: False))
        interrupted = replace(result, status=ResultStatus.CANCELLED)
        receipt2 = replace(receipt, result_ref=self.a.save(worker_payload(interrupted)))
        self.assertTrue(service.admit_receipt(stage, inv, interrupted, receipt2, self.a, authority).error)
        self.cancel.set()
        failed = service.execute(frozen, ctx)
        self.assertEqual(failed.status, ResultStatus.CANCELLED)
        self.assertTrue(SearchResult.load(self.a.data[failed.output.id]).error)

    def test_fusion_exact_ranks_exclusions_and_single_charge(self):
        plan = self.plan(Retrieve(('a', 'b'), ('Nat.add', 'Goal'), (Backend('leanexplore'),)), forbidden=('Goal',))
        reports = tuple(SearchResult(s.sha256, (Match(('Nat.mul',)), Match(('Nat.add',)),
                        Match(('«MidasInternal»',)), Match(('_root_.Goal',)), Match(('by exact h',)))) for s in plan.stages)
        next_plan, fusion = continue_retrieval(plan, reports, self.scope)
        self.assertEqual(fusion.selected, ('Nat.add', 'Nat.mul'))
        self.assertIn('«MidasInternal»', fusion.excluded)
        self.assertIn('_root_.Goal', fusion.excluded)
        self.assertIn('by exact h', fusion.excluded)
        limits = ResourceLimits(1000, 256, 262144, 262144)
        self.assertEqual(plan.admission(0, limits).helper, 'retrieve')
        self.assertIsNone(plan.admission(1, limits).helper)
        self.assertIsNone(next_plan.admission(0, limits).helper)
        for bad in (reports[:1], tuple(reversed(reports)), reports+(reports[0],)):
            with self.assertRaises(ValueError):
                continue_retrieval(plan, bad, self.scope)
        with self.assertRaises(ValueError):
            continue_retrieval(plan, reports, replace(self.scope, owner=replace(self.scope.owner, generation=1)))

    def test_fusion_matches_fraction_order_and_deduplicates_complete_stages(self):
        request = self.request(Retrieve(('a', 'b', 'c'), backends=(Backend('frozen-source', ('Mathlib',)),)))
        stages = search_stages(request)
        reports, expected = [], {}
        for stage in stages:
            names = tuple(f'N.x{(n*7+stage.ordinal)%1000}' for n in range(1000))
            rows = tuple(Match(names[i:i+20], 'lib/X.lean', i+1, i+1, 'source', 1) for i in range(0,1000,20))
            reports.append(SearchResult(stage.sha256, rows))
            for rank, name in enumerate(names):
                expected[name] = expected.get(name, Fraction())+Fraction(1,60+rank)
        fusion = fuse(request, tuple(reports))
        self.assertEqual(fusion.ranked, tuple(sorted(expected, key=lambda n: (-expected[n], n))))
        self.assertEqual(fusion.selected, tuple(sorted(expected, key=lambda n: (-expected[n], n)))[:12])

    def test_retrieval_checks_top_twelve_and_explicit_names_then_allows_named_followup(self):
        names = tuple(f'N.term{n:02}' for n in range(20))
        plan = self.plan(Retrieve(('terms',), (names[-1], names[0]), (Backend('leanexplore'),)))
        reports = (SearchResult(plan.stages[0].sha256, tuple(Match((name,)) for name in names)),)
        following, fusion = continue_retrieval(plan, reports, self.scope)
        self.assertEqual(fusion.ranked, names)
        self.assertEqual(fusion.selected, (names[-1], *names[:12]))
        self.assertEqual(tuple(name for s in following.stages for name in s.action.names), fusion.selected)
        direct = self.plan(Retrieve(names=(names[12], names[18])))
        self.assertEqual(tuple(s.action for s in direct.stages), (Lookup((names[12], names[18])),))

    def test_retrieval_partial_failure_preserves_explicit_names(self):
        plan = self.plan(Retrieve(('q',), ('Nat.add',), (Backend('leanexplore'),)))
        reports = (SearchResult(plan.stages[0].sha256, error='transport failure'),)
        following, fusion = continue_retrieval(plan, reports, self.scope)
        self.assertEqual(following.stages[0].action, Lookup(('Nat.add',)))
        self.assertEqual(fusion.errors, ((0, 'transport failure'),))
        direct = self.plan(Retrieve(names=('Nat.add',)))
        self.assertEqual(direct.stages[0].action, following.stages[0].action)

    def test_finish_exact_stage_receipts_and_resume(self):
        plan = self.plan(Retrieve(('q',), backends=(Backend('leanexplore'),)))
        searches = (SearchResult(plan.stages[0].sha256, (Match(('Nat.add',)), Match(('Absent',)))),)
        following, _ = continue_retrieval(plan, searches, self.scope)
        stage = following.stages[0]
        receipt = self.receipt()
        ev = StageEvidence(stage, receipt.request_ref.sha256, receipt, ResultStatus.SUCCEEDED,
                           (self.a.save(b'checked evidence'),), ('Nat.add',), ('Absent',))
        change = finish_helper(following, (ev,), self.scope, searches=searches)
        self.assertEqual(change.continuation, plan.request.continuation)
        self.assertEqual(change.authority, 'advisory')
        with self.assertRaises(ValueError):
            replace(ev, available=('Invented',))
        with self.assertRaises(ValueError):
            replace(ev, request_hash='0'*64)
        for entries in ((), (ev, ev)):
            with self.assertRaises(ValueError):
                finish_helper(following, entries, self.scope, searches=searches)

    def test_probe_and_research_never_classify_checked_names(self):
        for action in (Probe((self.a.save(b'case'),)), Research('question')):
            plan = self.plan(action)
            receipt = self.receipt()
            ev = StageEvidence(plan.stages[0], receipt.request_ref.sha256, receipt, ResultStatus.SUCCEEDED,
                               (self.a.save(b'advisory'),))
            self.assertEqual(finish_helper(plan, (ev,), self.scope).authority, 'advisory')
            with self.assertRaises(ValueError):
                replace(ev, available=('Nat.add',))

    def test_checker_handoff_projects_only_exact_lookup_queries(self):
        stage = self.stage(Lookup(('Nat.add', 'Absent')))
        compiler_hash = 'b'*64
        value = {'format':'midas-lookup-result-context-v1', 'request':compiler_hash,
                 'records':[{'query':'Nat.add', 'status':'available'}, {'query':'Absent', 'status':'unavailable'}]}
        admitted = SimpleNamespace(operation='lookup', evidence=self.a.save(codec.encode(value)), receipt=self.receipt())
        result = admit_checked(stage, admitted, compiler_hash, self.a)
        self.assertEqual(result.available, ('Nat.add',))
        self.assertEqual(result.unavailable, ('Absent',))
        with self.assertRaises(ValueError):
            admit_checked(stage, admitted, 'c'*64, self.a)
        with self.assertRaises(ValueError):
            admit_checked(replace(stage, action=Lookup(('Other',))), admitted, compiler_hash, self.a)
        with self.assertRaises(ValueError):
            admit_checked(self.stage(Research('q')), admitted, compiler_hash, self.a)

    def test_probe_handoff_retains_case_batch_without_proof_authority(self):
        stage = self.stage(Probe((self.a.save(pack({'id':'p', 'code':'simp'})),)))
        case = self.a.save(codec.encode({'request':'b'*64, 'row':{'result':{'id':'p', 'operation':'tactic'}}}))
        batch = self.a.save(pack([case.to_dict()]))
        admitted = SimpleNamespace(operation='probe', evidence=batch, receipt=self.receipt())
        result = admit_checked(stage, admitted, 'b'*64, self.a)
        self.assertEqual(result.artifacts, (batch,))
        self.assertFalse(result.available)
        with self.assertRaises(ValueError):
            admit_checked(stage, admitted, 'c'*64, self.a)
        admitted.evidence = self.a.save(pack([]))
        with self.assertRaises(ValueError):
            admit_checked(stage, admitted, 'b'*64, self.a)

    def test_search_display_keeps_ranked_names_sources_and_failures(self):
        search = Search('Morley triangle', Backend('frozen-source', ('Mathlib',)))
        result = SearchResult('a'*64, (
            Match(('Geometry.morley', 'Geometry.triangle'), 'mathlib/Mathlib/Geometry.lean', 12, 10,
                  '-- Morley\ntheorem triangle : True := by trivial', 4),
            Match((), 'mathlib/Mathlib/Comment.lean', 2, 1, '-- Morley triangle', 1)))
        rows = search_summary(result, search)
        self.assertEqual(rows, ('Search (frozen-source): Morley triangle',
            'Geometry.morley\nGeometry.triangle\nSource: mathlib/Mathlib/Geometry.lean:12\n'
            'Excerpt starts at line 10:\n-- Morley\ntheorem triangle : True := by trivial',
            'Source match (no declaration name)\nSource: mathlib/Mathlib/Comment.lean:2\n'
            'Excerpt starts at line 1:\n-- Morley triangle'))
        self.assertEqual(search_summary(SearchResult('a'*64), search)[-1], 'No matches.')
        self.assertEqual(search_summary(SearchResult('a'*64, error='unavailable'), search)[-1],
                         'Search failed: unavailable')
        hosted = search_summary(SearchResult('a'*64, (Match(('Geometry.morley',)),)),
                                Search('Morley', Backend('leanexplore')))
        self.assertEqual(hosted, ('Search (leanexplore): Morley', 'Geometry.morley'))

    def test_probe_display_preserves_diagnostics_goals_flags_and_inspection(self):
        case = dict(id='inspect', operation='tactic', code='run_tac do\n  pure ()', setup='', expected=None)
        message = dict(severity='information', file='inspect.code', line=2, column=2, text='Available: α')
        result = dict(id='inspect', operation='tactic', status='open', goals=['n : Nat\n⊢ n = n'],
                      contains_sorry=False, unresolved_metavariables=True, inspection={})
        text = probe_summary(case, dict(result=result, messages=[message]))
        for content in ('Probe inspect (tactic): open (advisory)',
                        'inspect.code (line 1 below):\nrun_tac do\n  pure ()',
                        'information at inspect.code:2:2:\nAvailable: α', 'Goal 1:\nn : Nat\n⊢ n = n',
                        'Contains sorry: false', 'Unresolved metavariables: true'):
            self.assertIn(content, text)
        for status in ('error', 'interrupted'):
            text = probe_summary(case, dict(result=dict(id='inspect', operation='tactic', status=status,
                diagnostic='[inspect.code] inspect.code:2:2: unknown constant'), messages=[]))
            self.assertIn(status+' (advisory)', text)
            self.assertIn('Diagnostic:\n[inspect.code] inspect.code:2:2: unknown constant', text)
        inspected = dict(result, status='inspected', inspection=dict(expression='Nat.zero', inferred_type='Nat',
            contains_sorry=False, unresolved_metavariables=False))
        text = probe_summary(dict(case, operation='term', code='Nat.zero', setup='intro n', expected='Nat'),
                             dict(result=inspected, messages=[]))
        for content in ('inspect.setup (line 1 below):\nintro n', 'inspect.expected (line 1 below):\nNat',
                        'Inspected expression:\nNat.zero', 'Inferred type:\nNat',
                        'Expression contains sorry: false', 'Expression unresolved metavariables: false'):
            self.assertIn(content, text)

    def test_readable_probe_pages_preserve_large_unicode_messages_without_json(self):
        from midas.application.helpers import page_text
        message = 'α😀\n'*20000
        case = dict(id='large', operation='tactic', code='run_tac do\n  pure ()', setup='', expected=None)
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            text = probe_summary(case, dict(result=dict(status='interrupted', diagnostic=message), messages=[]))
            archive = prepare_archive(self.scope, 'a'*64, (text,), (self.a.save(b'receipt'),), self.a)
            fragments, displays, cursor = [], [], None
            while True:
                page = read_page(archive, self.scope, self.a, cursor=cursor)
                fragments.extend(row[4] for row in codec.load_record(page.payload, limit=16000)[4])
                displays.append(page_text(page.payload).decode())
                cursor = page.next_cursor
                if cursor is None:
                    break
            self.assertGreater(len(displays), 1)
            self.assertEqual(''.join(fragments), text)
            self.assertIn('-- Excerpt continues on the next page.', displays[0])
            self.assertIn('-- Continued excerpt', displays[-1])
            self.assertNotRegex('\n'.join(displays), r'(?m)^\d+(?:/[^:]+)?:')

    def test_project_modules_supply_lookup_source_and_search_results(self):
        index = self.index((('project/ChallengeDeps.lean', b'def helper := 0\n'),
            ('project/Submission.lean', b'namespace Submission\ndef value := 1\nend Submission\n'),
            ('project/Submission/Helpers.lean', b'namespace Submission.Helpers\ndef support := 2\nend Submission.Helpers\n')))
        records = ({'query':'helper', 'status':'available', 'source_range':{
            'module':'ChallengeDeps', 'start':[1,0], 'end':[1,15]}},)
        result = lookup_sources(records, environment_id=self.scope.environment_id, index=index,
                                prefix=SourceFile('Main.lean', b''), reader=self.a)
        self.assertEqual(result[0], 'def helper := 0')
        self.assertEqual(index.roots, ('project',))
        for selector, query, path, name in (
                ('project', 'helper', 'project/ChallengeDeps.lean', 'helper'),
                ('project/ChallengeDeps', 'helper', 'project/ChallengeDeps.lean', 'helper'),
                ('ChallengeDeps', 'helper', 'project/ChallengeDeps.lean', 'helper'),
                ('Submission', 'support', 'project/Submission/Helpers.lean', 'Submission.Helpers.support'),
                ('Submission.Helpers', 'support', 'project/Submission/Helpers.lean', 'Submission.Helpers.support'),
                ('Submission.lean', 'value', 'project/Submission.lean', 'Submission.value')):
            with self.subTest(selector=selector):
                stage = self.stage(Search(query, Backend('frozen-source', (selector,))))
                result = execute_search(stage, self.a, self.cancel, self.deadline, index=index)
                self.assertEqual(len(result.records), 1)
                self.assertEqual((result.records[0].path, result.records[0].names), (path, (name,)))
                self.assertEqual(SearchResult.load(result.payload), result)

    def test_lookup_distinguishes_modules_with_the_same_basename(self):
        index = self.index((('project/ChallengeDeps.lean', b'def helper := 0\n'),
            ('lean-pool/LeanPool/ClassificationOfSurfaces/LeanEval/ChallengeDeps.lean', b'def unrelated := 1\n')))
        # An uncompiled file is searchable but cannot supply an imported body.
        index = replace(index, entries=(*index.entries,
            SourceEntry('other/ChallengeDeps.lean', self.a.save(b'def unrelated := 2\n'))))
        records = tuple({'query': query, 'status': 'available', 'source_range': {
            'module': module, 'start': [1, 0], 'end': [1, end]}}
            for query, module, end in (('helper', 'ChallengeDeps', 15),
                ('unrelated', 'LeanPool.ClassificationOfSurfaces.LeanEval.ChallengeDeps', 18)))
        result = lookup_sources(records, environment_id=self.scope.environment_id, index=index,
                                prefix=SourceFile('Submission.lean', b''), reader=self.a)
        self.assertEqual(result, ('def helper := 0', 'def unrelated := 1'))
        self.assertEqual(self.a.reads, [e.artifact for e in index.entries[:2]])
        missing = replace(index, entries=index.entries[1:])
        result = lookup_sources(records[:1], environment_id=self.scope.environment_id, index=missing,
                                prefix=SourceFile('Submission.lean', b''), reader=self.a)
        self.assertIn('missing or ambiguous frozen module', result[0])

    def test_inspected_theorems_and_aliases_use_checked_signatures_without_source_reads(self):
        sources = ('alias law_sin := original',
                   '/-- A named consequence. -/\n@[simp] protected alias law_sin := original',
                   'alias ⟨forward, backward⟩ := original_iff',
                   '/-- An alias in prose is not an alias command. -/\n'
                   'theorem ordinary : True := by trivial')
        for source in sources:
            with self.subTest(source=source):
                data = (source + '\n').encode()
                index = self.index((('mathlib/Mathlib/Triangle.lean', data),))
                record = dict(query='Geometry.law_sin', status='available', kind='theorem',
                              type='∀ (x : ℝ), f x = g x', source_range=dict(module='Mathlib.Triangle',
                              start=[1, 0], end=[len(source.splitlines()), len(source.splitlines()[-1])]))
                with patch('json.dumps', side_effect=AssertionError('JSON')), \
                        patch('json.loads', side_effect=AssertionError('JSON')):
                    result = lookup_sources((record,), environment_id=self.scope.environment_id,
                                            index=index, prefix=SourceFile('Main.lean', b''), reader=self.a)
                self.assertEqual(result, ('theorem Geometry.law_sin : ∀ (x : ℝ), f x = g x',))
                self.assertEqual(self.a.reads, [])

    def test_inspected_definitions_keep_bodies_and_theorems_need_no_source_range(self):
        source = 'def value : Nat := by\n  exact 42\n'
        index = self.index((('project/Helpers.lean', source.encode()),))
        records = (dict(query='result', status='available', kind='theorem', type='value = 42',
                        source_range=None),
                   dict(query='value', status='available', kind='def', type='Nat',
                        source_range=dict(module='Helpers', start=[1, 0], end=[2, 10])))
        result = lookup_sources(records, environment_id=self.scope.environment_id, index=index,
                                prefix=SourceFile('Main.lean', b''), reader=self.a)
        self.assertEqual(result, ('theorem result : value = 42', source.rstrip()))
        self.assertEqual(self.a.reads, [index.entries[0].artifact])

    def test_archive_large_unicode_indexed_reads_and_duplicate_guard(self):
        value = 'α😀"\\\n'*180000
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            archive = prepare_archive(self.scope, 'a'*64, [{'body':value}], (self.a.save(b'receipt'),), self.a)
            self.assertEqual(Archive.load(archive.payload), archive)
            cursor, reconstructed, expected_offset = None, [], 0
            while True:
                self.a.reads.clear()
                page = read_page(archive, self.scope, self.a, cursor=cursor)
                self.assertEqual(self.a.reads, [archive.pages[page.ordinal]])
                self.assertLessEqual(len(page.payload), 16000)
                for row in codec.load_record(page.payload, limit=16000)[4]:
                    if row[0] != 'text':
                        self.assertEqual(row, ['object', ['0'], 1])
                        continue
                    self.assertEqual(row[2], expected_offset)
                    reconstructed.append(row[4])
                    expected_offset += len(row[4].encode())
                cursor = page.next_cursor
                if cursor is None:
                    break
            self.assertEqual(''.join(reconstructed), value)
            self.a.reads.clear()
            self.assertIsNone(read_page(archive, self.scope, self.a, delivered=lambda key: True))
            self.assertFalse(self.a.reads)

    def test_archive_fragments_preserve_object_and_array_shapes(self):
        record = {'0':['value', {'0':True, '':None}], 'empty':{}, 'array':[]}
        archive = prepare_archive(self.scope, 'a'*64, [record], (self.a.save(b'receipt'),), self.a)
        page = read_page(archive, self.scope, self.a)
        fragments = codec.load_record(page.payload, limit=16000)[4]
        values = {}
        for row in reversed(fragments):
            kind, path, content = row[:3]
            path = tuple(path)
            if kind == 'object':
                values[path] = {key[-1]:item for key, item in values.items() if key[:-1] == path}
            elif kind == 'array':
                values[path] = [values[(*path, str(i))] for i in range(content)]
            elif kind == 'value':
                values[path] = content
            else:
                values[path] = row[4]
        self.assertEqual(values[('0',)], record)

    def test_archive_scope_cursor_integrity_and_depth(self):
        archive = prepare_archive(self.scope, 'a'*64, [{'answer':'text'}], (self.a.save(b'receipt'),), self.a)
        for cursor in ('b'*64+':0', archive.id+':01', archive.id+':99', archive.id+':-1'):
            with self.assertRaises(ValueError):
                read_page(archive, self.scope, self.a, cursor=cursor)
        with self.assertRaises(ValueError):
            read_page(archive, replace(self.scope, context=self.a.save(b'other')), self.a)
        self.a.data[archive.pages[0].id] = b'changed'
        with self.assertRaises(ValueError):
            read_page(archive, self.scope, self.a)
        value = 'x'
        for _ in range(10):
            value = [value]
        with self.assertRaises(ValueError):
            prepare_archive(self.scope, 'a'*64, [value], archive.provenance, self.a)

    def test_frozen_source_excerpt_unicode_ranges_missing_and_ambiguous(self):
        data = 'def α := "😀"\n'.encode()
        location = {'module': 'Mathlib.X', 'start':[1,0], 'end':[1,12]}
        self.assertEqual(source_excerpt(data, location), 'def α := "😀"')
        records = ({'query':'α', 'status':'available', 'source_range':location, 'body':None},)
        index = self.index((('mathlib/Mathlib/X.lean', data),))
        result = lookup_sources(records, environment_id=self.scope.environment_id, index=index,
                                prefix=SourceFile('Main.lean', b''), reader=self.a)
        self.assertEqual(result[0], 'def α := "😀"')
        other = replace(index, entries=index.entries+(SourceEntry('other/Mathlib/X.lean', self.a.save(data), 'Mathlib.X'),))
        self.assertIn('missing or ambiguous frozen module', lookup_sources(records,
            environment_id=self.scope.environment_id, index=other,
            prefix=SourceFile('Main.lean', b''), reader=self.a)[0])
        with self.assertRaises(ValueError):
            source_excerpt(data, dict(location, end=[5,0]))

    def research_value(self, **source_changes):
        return {'answer':'Use the theorem.', 'sources':[dict(url='https://example.test/paper', title='Paper',
                location='Theorem 2', excerpt='compact sets are closed', summary='Useful', hypotheses='Hausdorff',
                accessed=True, **source_changes)], 'unresolved':[]}

    def test_research_access_claims_quotes_and_binary_evidence(self):
        stage = self.stage(Research('question'))
        value = self.research_value()
        raw = json.dumps(value).encode()
        event = {'type':'webSearch', 'id':'w1', 'action':{'type':'openPage'}}
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            result = codec.load_record(research_evidence(stage, raw, events(event)), limit=131072)
        self.assertEqual(result['authority'], 'advisory')
        self.assertEqual(result['stage'], stage.sha256)
        with self.assertRaisesRegex(ValueError, 'claimed access'):
            research_evidence(stage, raw, events(dict(event, action={'type':'search'})))
        viewed = dict(event, action={'type':'other', 'results':[{'type':'text_result', 'ref_id':'turn1view0', 'url':value['sources'][0]['url']}]})
        research_evidence(stage, raw, events(viewed))
        value['sources'] *= 6
        value['sources'][0]['excerpt'] = 'one two three four five'
        with self.assertRaisesRegex(ValueError, 'quote allowance'):
            research_evidence(stage, json.dumps(value).encode(), events(event))

    def test_research_web_limits_duplicates_and_malformed_response(self):
        event = {'type':'webSearch', 'id':'w1', 'action':{'type':'search'}}
        self.assertEqual(len(web_actions(events(event, event), limit=1)), 1)
        with self.assertRaises(ValueError):
            web_actions(events(event, dict(event, action={'type':'openPage'})), limit=2)
        with self.assertRaises(ValueError):
            web_actions(events(event, dict(event, id='w2')), limit=1)
        for raw in (b'', b'\x00', events(event)[:-1]):
            with self.assertRaises(ValueError):
                web_actions(raw, limit=2)
        stage = self.stage(Research('question'))
        for raw in (b'{"answer":"a","answer":"b","sources":[],"unresolved":[]}', b'['*20+b']'*20):
            with self.assertRaises(ValueError):
                research_evidence(stage, raw, events(event))

    def test_component_import_boundary(self):
        import ast
        directory = Path(__file__).parents[1]/'midas'/'tools'
        for path in directory.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level == 2:
                    self.assertIn(node.module.split('.')[0], ('domain', 'policy', 'source'), str(path))
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self.assertNotIn('midas_formalizer', ast.unparse(node))


if __name__ == '__main__':
    unittest.main()
