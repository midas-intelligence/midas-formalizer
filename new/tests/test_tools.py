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
from midas.tools import (Archive, Backend, HelperRequest, Lookup, Match, Probe, Research, WebSearch, Retrieve,
    Scope, Search, SearchResult, SearchService, SourceEntry, SourceIndex, Stage, StageEvidence,
    admit_checked, continue_retrieval, execute_search, finish_helper, fuse, lookup_sources, plan_helper,
    prepare_archive, probe_summary, read_page, research_evidence, research_summary, retrieval_summary, web_search_evidence, web_search_summary, search_stages, search_summary, source_excerpt, web_actions)
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
        self.services = Services(research=True, web_search=True, web_calls=12)

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
                Probe((self.a.save(pack({'id': 'p', 'code': 'simp'})),)), Research('Why?'), WebSearch('Why?', ('https://example.test',))):
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
            lambda: Probe(()), lambda: WebSearch('q', ('https://user:password@host',)), lambda: WebSearch('q', web_calls=13))
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

    def test_policy_is_used_for_request_units_and_web_search_bounds(self):
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
        web_search = plan_helper(self.request(WebSearch('q')), self.policy, OwnerUsage(),
                               replace(self.services, web_calls=2))
        self.assertEqual(web_search.request.action.web_calls, 2)
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
        request = self.request(Retrieve(('a query', 'b query', 'c query'), backends=(Backend('frozen-source', ('Mathlib',)),)))
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

    def test_local_identifier_query_requests_lookup_without_indexed_name(self):
        for error in (None, 'search failure'):
            plan = self.plan(Retrieve(('_root_.N.Generated.rec',),
                backends=(Backend('frozen-source'),)))
            reports = (SearchResult(plan.stages[0].sha256, error=error),)
            following, fusion = continue_retrieval(plan, reports, self.scope)
            self.assertEqual(fusion.ranked, ())
            self.assertEqual(following.stages[0].action, Lookup(('_root_.N.Generated.rec',)))
            self.assertIsNone(following.charge)

    def test_local_direct_lookup_priority_deduplication_and_bound(self):
        names = tuple(f'N.term{n:02}' for n in range(20))
        plan = self.plan(Retrieve(('_root_.N.term19', 'N.«term19»'), ('Explicit',),
            (Backend('frozen-source'),)))
        reports = tuple(SearchResult(stage.sha256, tuple(
            Match((name,), 'project/Main.lean', 1, 1) for name in names)) for stage in plan.stages)
        following, fusion = continue_retrieval(plan, reports, self.scope)
        self.assertEqual(fusion.selected, ('Explicit', '_root_.N.term19', *names[:11]))
        self.assertEqual(tuple(n for s in following.stages for n in s.action.names), fusion.selected)

    def test_only_allowed_local_identifiers_get_direct_lookups(self):
        for query, backend, forbidden in (
                ('N.Generated.rec', Backend('leanexplore'), ()),
                ('a natural language query', Backend('frozen-source'), ()),
                ('by exact h', Backend('frozen-source'), ()),
                ('_root_.N.«Generated».rec', Backend('frozen-source'), ('N.Generated.rec',)),
                ('«MidasInternal».generated', Backend('frozen-source'), ())):
            with self.subTest(query=query, backend=backend):
                plan = self.plan(Retrieve((query,), backends=(backend,)), forbidden=forbidden)
                reports = (SearchResult(plan.stages[0].sha256),)
                following, _ = continue_retrieval(plan, reports, self.scope)
                self.assertEqual(following.stages, ())

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

    def test_probe_and_web_search_never_classify_checked_names(self):
        for action in (Probe((self.a.save(b'case'),)), WebSearch('question')):
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
            admit_checked(self.stage(WebSearch('q')), admitted, compiler_hash, self.a)

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
            '-- Morley\ntheorem triangle : True := by trivial',
            'Source match (no declaration name)\nSource: mathlib/Mathlib/Comment.lean:2\n'
            '-- Morley triangle'))
        self.assertEqual(search_summary(SearchResult('a'*64), search)[-1], 'No matches.')
        self.assertEqual(search_summary(SearchResult('a'*64, error='unavailable'), search)[-1],
                         'Search failed: unavailable')
        hosted = search_summary(SearchResult('a'*64, (Match(('Geometry.morley',)),)),
                                Search('Morley', Backend('leanexplore')))
        self.assertEqual(hosted, ('Search (leanexplore): Morley', 'Geometry.morley'))

    def test_retrieval_displays_four_explicit_lookups_and_checks_inside_fifty_search_hits(self):
        explicit = tuple(f'N.explicit{i}' for i in range(4))
        hits = tuple(f'N.hit{i}' for i in range(50))
        action = Retrieve(('find terms',), explicit, (Backend('frozen-source'),))
        search = Search(action.queries[0], action.backends[0])
        result = SearchResult('a'*64, tuple(Match((name,), 'project/Main.lean', i+1, i+1,
            'indexed source '+name) for i, name in enumerate(hits)))
        checked = {name: f'theorem {name} : True' for name in (*explicit, *hits[:12])}
        rows, positions = retrieval_summary(action, ((result, search),), checked)
        self.assertEqual(rows[:4], tuple(checked[name] for name in explicit))
        self.assertEqual(rows[4], 'Search (frozen-source): find terms')
        self.assertEqual(len(rows), 4+1+50)
        for i, name in enumerate(hits[:12]):
            self.assertEqual(rows[5+i], f'{i+1}. {name}\nSource: project/Main.lean:{i+1}\n{checked[name]}')
        self.assertEqual(rows[-1], '50. N.hit49')
        for name, index in positions:
            self.assertIn(checked[name], rows[index])
            self.assertEqual('\n'.join(rows).count(checked[name]+'\n'), 1)
        self.assertEqual(len(positions), 16)

    def test_checked_search_details_keep_review_positions_when_paged(self):
        from midas.application.helpers import page_text
        hits = tuple(f'N.hit{i}' for i in range(50))
        action = Retrieve(('find terms',), backends=(Backend('leanexplore'),))
        search = Search(action.queries[0], action.backends[0])
        result = SearchResult('a'*64, tuple(Match((name,)) for name in hits))
        checked = {hits[0]: 'def N.hit0 : Nat\n\n'+('large source body\n'*1500)}
        rows, positions = retrieval_summary(action, ((result, search),), checked)
        self.assertEqual(rows[0], 'Search (leanexplore): find terms')
        self.assertIn('50. N.hit49', rows[1])
        self.assertEqual(positions, (('N.hit0', 2),))
        archive = prepare_archive(self.scope, 'a'*64, rows, (self.a.save(b'provenance'),), self.a)
        selected = b''.join(page_text(self.a.data[ref.id], records={'2'}) for ref in archive.pages)
        self.assertIn(b'def N.hit0 : Nat', selected)
        self.assertNotIn(b'N.hit49', selected)
        self.assertGreater(len(archive.pages), 1)

    def test_direct_identifier_lookup_stays_in_its_search_section_without_source_hits(self):
        action = Retrieve(('N.Generated.rec',), backends=(Backend('frozen-source'),))
        search = Search(action.queries[0], action.backends[0])
        checked = {'N.Generated.rec': 'theorem N.Generated.rec : True'}
        for error in (None, 'search failure'):
            result = SearchResult('a'*64, error=error)
            rows, positions = retrieval_summary(action, ((result, search),), checked)
            self.assertEqual(rows[0], 'Search (frozen-source): N.Generated.rec')
            self.assertEqual(rows[-1], 'Exact identifier lookup:\n'+checked['N.Generated.rec'])
            self.assertEqual(positions, (('N.Generated.rec', 2),))

    def test_checked_search_source_is_not_repeated_within_entry_or_as_another_lookup(self):
        action = Retrieve(('first query', 'second query'), backends=(Backend('frozen-source'),))
        source = 'def example : Nat := 1'
        result = SearchResult('a'*64, (Match(('N.example',), 'project/Main.lean', 1, 1, source),))
        searches = tuple((result, Search(query, action.backends[0])) for query in action.queries)
        checked = {'N.example': 'def N.example : Nat\n\n'+source}
        rows, positions = retrieval_summary(action, searches, checked)
        self.assertEqual(rows[0], 'Search (frozen-source): first query')
        self.assertEqual(rows[1].splitlines()[:2], ['N.example', 'Source: project/Main.lean:1'])
        self.assertEqual(rows[1].count(source), 1)
        self.assertEqual('\n'.join(rows).count('def N.example : Nat'), 1)
        self.assertEqual(positions, (('N.example', 1),))

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

    def test_probe_excerpts_keep_boundary_lines_and_count_omissions(self):
        from midas.tools.evidence import _probe_excerpt
        for count in (0, 1, 5, 9, 10, 11, 12, 20):
            for newline in ('\n', '\r\n'):
                for trailing in ('', newline):
                    with self.subTest(count=count, newline=newline, trailing=trailing):
                        lines = [f'line {i}: α😀' for i in range(count)]
                        value = newline.join(lines) + trailing
                        actual = _probe_excerpt(value)
                        if count <= 10:
                            self.assertEqual(actual, value)
                        else:
                            expected = (newline.join(lines[:5]) + newline +
                                        f'... [{count-10} lines omitted] ...\n' +
                                        newline.join(lines[-5:]) + trailing)
                            self.assertEqual(actual, expected)

    def test_probe_pages_compact_each_error_goal_and_inspection_without_json(self):
        from midas.application.helpers import page_text
        lines = [f'expanded line {i}: α😀' for i in range(20000)]
        expansion = '\n'.join(lines)
        code = 'run_tac do\n' + '\n'.join('  pure ()' for _ in range(12))
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            summaries = []
            for index in range(1, 4):
                case = dict(id=f'case{index}', operation='tactic', code=code, setup='', expected=None)
                messages = [dict(severity='error', file=f'case{index}.code', line=n, column=2,
                                 text=f'Error {n}\n'+expansion) for n in (2, 3)]
                result = dict(status='error', diagnostic=expansion, goals=[expansion],
                    contains_sorry=False, unresolved_metavariables=True,
                    inspection=dict(expression=expansion, inferred_type=expansion,
                                    contains_sorry=False, unresolved_metavariables=True))
                summaries.append(probe_summary(case, dict(result=result, messages=messages)))
            archive = prepare_archive(self.scope, 'a'*64, summaries, (self.a.save(b'receipt'),), self.a)
            self.assertEqual(len(archive.pages), 1)
            displayed = page_text(read_page(archive, self.scope, self.a).payload).decode()
            for index in range(1, 4):
                self.assertIn(f'Probe case{index} (tactic): error', displayed)
                for line in (2, 3):
                    self.assertIn(f'error at case{index}.code:{line}:2:\nError {line}', displayed)
            self.assertEqual(displayed.count('lines omitted'), 18)
            for content in (code, lines[0], lines[-1], 'Goal 1:', 'Inspected expression:', 'Inferred type:',
                            'Contains sorry: false', 'Unresolved metavariables: true'):
                self.assertIn(content, displayed)
            self.assertNotIn(lines[100]+'\n', displayed)

    def test_readable_probe_pages_preserve_long_single_lines_without_json(self):
        from midas.application.helpers import page_text
        message = 'α😀'*20000
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
        self.assertEqual(result[0], 'helper\n\ndef helper := 0')
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
        self.assertEqual(result, ('helper\n\ndef helper := 0', 'unrelated\n\ndef unrelated := 1'))
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
        self.assertEqual(result, ('theorem result : value = 42', 'def value : Nat\n\n'+source.rstrip()))
        self.assertEqual(self.a.reads, [index.entries[0].artifact])

    def test_generated_lookup_keeps_identity_when_source_is_parent_command(self):
        source = 'structure Box where\n  value : Nat\n'
        index = self.index((('project/Helpers.lean', source.encode()),))
        records = tuple(dict(query=name, status='available', kind=kind, type='checked type',
            source_range=dict(module='Helpers', start=[1, 0], end=[2, 13]))
            for name, kind in (('Box.rec', 'recursor'), ('Box.mk', 'constructor'), ('Box.value', 'def')))
        result = lookup_sources(records, environment_id=self.scope.environment_id, index=index,
                                prefix=SourceFile('Main.lean', b''), reader=self.a)
        for row, rendered in zip(records, result):
            self.assertIn(row['query']+' : checked type\n\n'+source.rstrip(), rendered)
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
        self.assertEqual(result[0], 'α\n\ndef α := "😀"')
        other = replace(index, entries=index.entries+(SourceEntry('other/Mathlib/X.lean', self.a.save(data), 'Mathlib.X'),))
        self.assertIn('missing or ambiguous frozen module', lookup_sources(records,
            environment_id=self.scope.environment_id, index=other,
            prefix=SourceFile('Main.lean', b''), reader=self.a)[0])
        with self.assertRaises(ValueError):
            source_excerpt(data, dict(location, end=[5,0]))

    def test_research_admits_guidance_without_web_events_and_rejects_source_claims(self):
        from midas.prompt.wire import _Writer
        stage = self.stage(Research('How can this be proved?'))
        value = dict(answer='Use induction. '+ 'α' * 12000, unresolved=['Check the base case.'])
        wire = _Writer(131072)
        wire.add(b'{"answer":')
        wire.string((value['answer'].encode(),))
        wire.add(b',"unresolved":["Check the base case."]}')
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            evidence = research_evidence(stage, wire.finish())
            summary = research_summary(evidence)
        self.assertIn(value['answer'], summary)
        self.assertIn('Check the base case.', summary)
        self.assertEqual(codec.load_record(evidence, limit=131072)['authority'], 'advisory')
        for bad in (dict(answer='x', unresolved=[], sources=[]), dict(answer='', unresolved=[]),
                    dict(answer='x', unresolved=['q']*33), dict(answer='x', unresolved=[True])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                research_evidence(stage, json.dumps(bad).encode())
        with self.assertRaises(ValueError):
            research_evidence(self.stage(WebSearch('Find references.')), b'{"answer":"x","unresolved":[]}')

    def web_search_value(self, **source_changes):
        return {'answer':'Use the theorem.', 'sources':[dict(url='https://example.test/paper', title='Paper',
                location='Theorem 2', excerpt='compact sets are closed', summary='Useful', hypotheses='Hausdorff',
                accessed=True, **source_changes)], 'unresolved':[]}

    def test_web_search_access_claims_quotes_and_binary_evidence(self):
        stage = self.stage(WebSearch('question'))
        value = self.web_search_value()
        raw = json.dumps(value).encode()
        event = {'type':'webSearch', 'id':'w1', 'action':{'type':'openPage'}}
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            result = codec.load_record(web_search_evidence(stage, raw, events(event)), limit=131072)
        self.assertEqual(result['authority'], 'advisory')
        self.assertEqual(result['stage'], stage.sha256)
        self.assertEqual(result['warnings'], [])
        warned = codec.load_record(web_search_evidence(stage, raw,
            events(dict(event, action={'type':'search'}))), limit=131072)
        self.assertEqual(warned['warnings'], [
            'Web search claimed access without an open/find or matching document view'])
        for field in ('answer', 'sources', 'unresolved'):
            self.assertEqual(warned[field], result[field])
        viewed = dict(event, action={'type':'other', 'results':[{'type':'text_result', 'ref_id':'turn1view0', 'url':value['sources'][0]['url']}]})
        matched = codec.load_record(web_search_evidence(stage, raw, events(viewed)), limit=131072)
        self.assertEqual(matched['warnings'], [])
        value['sources'] *= 6
        value['sources'][0]['excerpt'] = 'one two three four five'
        with self.assertRaisesRegex(ValueError, 'quote allowance'):
            web_search_evidence(stage, json.dumps(value).encode(), events(event))

    def test_web_search_pages_lead_with_warning_and_preserve_all_response_text(self):
        from midas.application.helpers import page_text
        stage = self.stage(WebSearch('question'))
        value = self.web_search_value()
        value['answer'] = 'First mathematical step.\n' + 'αβγ proof detail\n' * 1200 + 'Last mathematical step.'
        value['unresolved'] = ['Check the boundary case.', 'Find the exact declaration.']
        # Build the large wire from bounded pieces, without a whole-document JSON operation.
        from midas.prompt.wire import _Writer
        wire = _Writer(131072)
        wire.add(b'{"answer":')
        wire.string((value['answer'].encode(),))
        wire.add(b',"sources":'+json.dumps(value['sources']).encode())
        wire.add(b',"unresolved":'+json.dumps(value['unresolved']).encode()+b'}')
        recorded = events({'type':'webSearch', 'id':'private-web-action', 'action':{'type':'search'}})
        with patch('json.dumps', side_effect=AssertionError('JSON')), patch('json.loads', side_effect=AssertionError('JSON')):
            evidence = web_search_evidence(stage, wire.finish(), recorded)
            text = web_search_summary(evidence)
            archive = prepare_archive(self.scope, stage.helper_id, (text,), (self.a.save(evidence),), self.a)
            pages = [read_page(archive, self.scope, self.a, cursor=f'{archive.id}:{i}')
                     for i in range(len(archive.pages))]
            displays = [page_text(page.payload).decode() for page in pages]
        self.assertGreater(len(pages), 1)
        self.assertTrue(displays[0].startswith('Warning: Web search claimed access'))
        self.assertIn('First mathematical step.', displays[0])
        self.assertLess(displays[0].index('Warning:'), displays[0].index('Web-search results'))
        # Page framing is separate from the source text: reconstruct exact UTF-8 fragments.
        restored = ''.join(row[4] for page in pages
                           for row in codec.load_record(page.payload, limit=16000)[4])
        self.assertEqual(restored, text)
        self.assertIn(value['answer'], restored)
        for field, content in value['sources'][0].items():
            self.assertIn(str(content), restored)
        for question in value['unresolved']:
            self.assertIn(question, restored)
        for clutter in ('0/actions', 'private-web-action', 'midas-web-search-evidence-v1', self.scope.environment_id):
            self.assertNotIn(clutter, ''.join(displays))
        saved = codec.load_record(evidence, limit=131072)
        self.assertEqual(saved['actions'][0], ['private-web-action', 'search', []])
        self.assertIn('Source 1: Paper', restored)
        self.assertNotIn(saved['sources'][0]['id'], restored)

    def test_web_search_web_limits_duplicates_and_malformed_response(self):
        event = {'type':'webSearch', 'id':'w1', 'action':{'type':'search'}}
        self.assertEqual(len(web_actions(events(event, event), limit=1)), 1)
        with self.assertRaises(ValueError):
            web_actions(events(event, dict(event, action={'type':'openPage'})), limit=2)
        with self.assertRaises(ValueError):
            web_actions(events(event, dict(event, id='w2')), limit=1)
        for raw in (b'', b'\x00', events(event)[:-1]):
            with self.assertRaises(ValueError):
                web_actions(raw, limit=2)
        stage = self.stage(WebSearch('question'))
        for raw in (b'{"answer":"a","answer":"b","sources":[],"unresolved":[]}', b'['*20+b']'*20):
            with self.assertRaises(ValueError):
                web_search_evidence(stage, raw, events(event))

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
