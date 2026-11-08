"""Offline prompt/provider contracts. No Lean, network, model, or subprocess calls."""
import ast
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from midas.domain import OwnerVersion
from midas.domain.wire import decode
from midas.prompt import Options, Prompt, Section, Snapshot, build, prover, researcher, reviewer, stop_reviewer, web_searcher, subject
from midas.prompt.protocol import MAX_BYTES, SCHEMA_INSTRUCTIONS, obj, schema_bytes
from midas.prompt.wire import _prefix
from midas.provider import ModelRequest, Route
from midas.provider.wire import validate_wire
from midas.tools import Research, Scope, Stage, WebSearch, research_evidence, web_search_evidence
from tests.test_provider import Artifacts, LIMITS
from tests.test_source import signature


def snapshot():
    return Snapshot((Section('source', 'Main.lean', b'import Mathlib\n'),
                     subject('goal', signature()),
                     Section('candidate', 'current-proof', b'by\n  trivial'),
                     Section('capabilities', 'remaining', b'web_calls=2; research available'),
                     Section('question', 'research-question', b'Find a precise reference.')))


def stop_snapshot():
    turn = snapshot()
    return replace(turn, sections=tuple(s for s in turn.sections if s.kind != 'candidate') +
                   (Section('candidate', 'stop-request', b'The statement lacks a necessary hypothesis.'),))


def cache_prefixes(request):
    """Exact wire prefixes at provider lookup boundaries; no large JSON operations."""
    wire = decode(request.wire, limit=MAX_BYTES)
    names = [m['content'][0]['text'].splitlines()[0] for m in wire['input']
             if isinstance(m['content'], list) and 'prompt_cache_breakpoint' in m['content'][0]]
    prefixes = [request.wire[:m.start()] for m in re.finditer(b',"prompt_cache_breakpoint":', request.wire)]
    return dict(zip(names, prefixes, strict=True))


class PromptTests(unittest.TestCase):
    def test_review_instructions_and_cache_prefixes_are_separate(self):
        decomposition = reviewer.render(snapshot())
        stop = stop_reviewer.render(stop_snapshot())
        self.assertEqual(decomposition.instructions, reviewer.INSTRUCTIONS)
        self.assertEqual(stop.instructions, stop_reviewer.INSTRUCTIONS)
        self.assertNotIn(stop.instructions.splitlines()[0], decomposition.instructions)
        self.assertNotIn(decomposition.instructions.splitlines()[0], stop.instructions)
        self.assertNotEqual(decomposition.renderer, stop.renderer)
        for transport in ('azure_responses', 'bedrock_responses'):
            options = Options(transport, 'model', prompt_cache='explicit', cache_scope='a'*64)
            before = build(snapshot(), options, renderer=reviewer.render)
            after = build(stop_snapshot(), options, renderer=stop_reviewer.render)
            self.assertNotEqual(cache_prefixes(before), cache_prefixes(after))
            self.assertNotEqual(decode(before.wire, limit=MAX_BYTES)['prompt_cache_key'],
                                decode(after.wire, limit=MAX_BYTES)['prompt_cache_key'])
        with self.assertRaisesRegex(ValueError, 'cannot review a stop request'):
            reviewer.render(stop_snapshot())
        with self.assertRaisesRegex(ValueError, 'requires an exact subject and stop request'):
            stop_reviewer.render(snapshot())

    def test_runtime_traces_are_removed_from_prover_and_reviewer_diagnostics(self):
        noise = (b'MIDAS_THREAD_GUARD count=1\r\nMIDAS_RUNTIME_OPEN /proc/stat\n'
                 b'MIDAS_TRACE tasks=2 syscalls=105 remaining=0 ready=1\n')
        error = b'candidate.lean:3:9: error: unknown constant\n  exact missing\nMIDAS_CHECK_REJECTED\n'
        turn = Snapshot((subject('goal', signature()), Section('candidate', 'proof', noise),
                         Section('source', 'raw-source', noise),
                         *(Section(kind, kind, noise + error + noise.rstrip(b'\n'))
                           for kind in ('history', 'feedback', 'evidence'))))
        for role in (prover, reviewer):
            sections = {s.name: s.text for s in role.render(turn).sections}
            for kind in ('history', 'feedback', 'evidence'):
                self.assertEqual(sections[kind], error)
            # Exact program inputs are not diagnostic streams.
            self.assertEqual(sections['proof'], noise)
            self.assertEqual(sections['raw-source'], noise)

    def test_saved_diagnostics_omit_secondary_sorry_axioms_only_within_the_same_section(self):
        error = b'candidate.lean:3:9: error: unsolved goals\n  original goal\n'
        footer = b'unexpected internal error: unapproved axiom: sorryAx (from MidasCandidateResult)\n'
        saved = error + footer
        turn = Snapshot((subject('goal', signature()), Section('candidate', 'proof', saved),
                         Section('source', 'raw-source', saved),
                         *(Section(kind, kind, saved) for kind in ('history', 'feedback', 'evidence')),
                         Section('feedback', 'standalone-rejection', footer)))
        for role in (prover, reviewer):
            with self.subTest(role=role.__name__):
                sections = {s.name: s.text for s in role.render(turn).sections}
                for kind in ('history', 'feedback', 'evidence'):
                    self.assertEqual(sections[kind], error)
                self.assertEqual(sections['standalone-rejection'], footer)
                self.assertEqual(sections['proof'], saved)
                self.assertEqual(sections['raw-source'], saved)
        # Rendering does not rewrite stored diagnostic material.
        for section in turn.sections:
            if section.name in ('history', 'feedback', 'evidence'):
                self.assertEqual(section.text, saved)

    def test_stable_blocks_precede_changing_turn_material_for_each_role(self):
        turn = replace(snapshot(), sections=(*snapshot().sections,
            Section('history', 'older-check', b'old diagnostic'),
            Section('memory', 'strategy', b'next approach'),
            Section('evidence', 'lookup', b'exact checked type'),
            Section('feedback', 'latest-check', b'current diagnostic')))
        for role in (prover, reviewer, researcher, web_searcher):
            for transport in ('codex', 'azure_responses', 'bedrock_responses'):
                if role is web_searcher and transport != 'codex':
                    continue
                with self.subTest(role=role.__name__, transport=transport):
                    def messages(value):
                        wire = decode(build(value, Options(transport, 'model'), renderer=role.render).wire, limit=MAX_BYTES)
                        return ([m['text'] for m in wire['input']] if transport == 'codex' else
                                [m['content'] for m in wire['input'][1:]])
                    before = messages(turn)
                    index = next(i for i, text in enumerate(before) if text.startswith('CANDIDATE '))
                    self.assertTrue(any(text.startswith('HISTORY ') for text in before[:index]))
                    evidence = next(i for i, text in enumerate(before) if text.startswith('EVIDENCE '))
                    history = next(i for i, text in enumerate(before) if text.startswith('HISTORY '))
                    self.assertLess(evidence, history)
                    for kind in ('memory', 'feedback', 'capabilities', 'question'):
                        changed = replace(turn, sections=tuple(replace(s, text=b'changed') if s.kind == kind else s
                                                              for s in turn.sections))
                        self.assertEqual(before[:index+1], messages(changed)[:index+1])

    def test_explicit_cache_boundaries_match_roles_and_are_provider_accepted(self):
        turn = replace(snapshot(), sections=(*snapshot().sections,
            Section('history', 'older-check', b'old diagnostic'),
            Section('history', 'another-check', b'another diagnostic'),
            Section('evidence', 'first-result', b'first result'),
            Section('evidence', 'second-result', b'second result'),
            Section('memory', 'strategy', b'changing strategy'),
            Section('feedback', 'latest-check', b'current diagnostic')))
        artifacts = Artifacts()
        for transport, provider in (('azure_responses', 'azure'), ('bedrock_responses', 'amazon-bedrock-runtime')):
            endpoint = ('https://example.test/responses' if transport == 'azure_responses' else
                        'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses')
            route = Route(provider, 'model', transport, endpoint=endpoint, prompt_cache='explicit')
            for role in (prover, reviewer):
                options = Options(transport, 'model', prompt_cache='explicit', cache_scope=route.scope)
                result = build(turn, options, renderer=role.render)
                request = ModelRequest(route, role.render(turn).role, LIMITS, artifacts.save(result.wire))
                validate_wire(request, result.wire)
                wire = decode(result.wire, limit=MAX_BYTES)
                cached = [m for m in wire['input'] if isinstance(m['content'], list)]
                # More than four markers are valid: old endpoints remain lookup
                # candidates even though the provider limits writes per request.
                self.assertEqual(len(cached), 7 if role is prover else 1)
                self.assertEqual(cached[0]['role'], 'developer')
                self.assertEqual([m['content'][0]['text'].split()[0] for m in cached[1:]],
                                 ['SUBJECT', 'EVIDENCE', 'EVIDENCE', 'HISTORY', 'HISTORY', 'CANDIDATE']
                                 if role is prover else [])
                malformed = result.wire.replace(b'"prompt_cache_breakpoint":{"mode":"explicit"}',
                                                b'"prompt_cache_breakpoint":{"mode":"unknown"}', 1)
                with self.assertRaisesRegex(ValueError, 'breakpoint'):
                    validate_wire(request, malformed)
                disabled = build(turn, replace(options, prompt_cache='disabled'), renderer=role.render)
                self.assertNotIn(b'prompt_cache_breakpoint', disabled.wire)

    def test_large_review_material_stays_outside_writes_without_losing_content(self):
        kinds = ('source', 'definitions', 'subject', 'interfaces', 'evidence',
                 'history', 'candidate', 'memory', 'feedback', 'capabilities', 'question')
        turn = Snapshot(tuple(Section(kind, kind, (kind + '\nα"\\\n').encode() * 4096)
                              for kind in kinds), evidence_bytes=128 * 1024)
        changed = replace(turn, sections=tuple(replace(s, text=b'Another review\n' + s.text)
                                               for s in turn.sections))
        for transport in ('azure_responses', 'bedrock_responses'):
            with self.subTest(transport=transport):
                options = Options(transport, 'model', prompt_cache='explicit', cache_scope='a'*64)
                before = build(turn, options, renderer=reviewer.render)
                after = build(changed, options, renderer=reviewer.render)
                self.assertEqual(len(cache_prefixes(before)), 1)
                self.assertEqual(cache_prefixes(before), cache_prefixes(after))
                for snapshot_, result in ((turn, before), (changed, after)):
                    wire = decode(result.wire, limit=MAX_BYTES)
                    self.assertEqual(wire['input'][0]['content'][0]['text'].encode(),
                                     reviewer.INSTRUCTIONS + SCHEMA_INSTRUCTIONS + reviewer.SCHEMA)
                    # Every review section remains intact after the final marker,
                    # including large interfaces and evidence before the candidate.
                    expected = [s.kind.upper() + ' ' + s.name + '\n' + s.text.decode()
                                for s in reviewer.render(snapshot_).sections]
                    self.assertEqual([m['content'] for m in wire['input'][1:]], expected)
                    self.assertLess(result.wire.rfind(b'"prompt_cache_breakpoint"'),
                                    result.wire.index(b'SOURCE source'))

    def test_growing_evidence_and_history_reuse_previous_written_endpoints(self):
        options = Options('azure_responses', 'model', prompt_cache='explicit', cache_scope='a'*64)
        for kind, count in (('evidence', 8), ('history', 60)):
            with self.subTest(kind=kind):
                retained = tuple(Section(kind, f'record-{i}', f'Immutable result {i}.'.encode())
                                 for i in range(count))
                turn = replace(snapshot(), sections=(*snapshot().sections, *retained))
                for step in range(3):
                    before = cache_prefixes(build(turn, options, renderer=prover.render))
                    next_record = Section(kind, f'record-{count+step}', b'New result.')
                    changed = tuple(replace(s, text=b'Updated turn material')
                                    if s.kind in ('candidate', 'capabilities') else s for s in turn.sections)
                    after_turn = replace(turn, sections=(*changed, next_record))
                    after = cache_prefixes(build(after_turn, options, renderer=prover.render))
                    # Exercise the documented write cap and lookup window,
                    # not merely the presence of annotations on the new tail.
                    written = set(list(before.values())[-4:])
                    incoming = list(after.values())
                    matches = written.intersection(incoming[:2] + incoming[-50:])
                    self.assertTrue(matches)
                    self.assertEqual(max(matches, key=len),
                                     before[f'{kind.upper()} record-{count+step-1}'])
                    turn = after_turn

    def test_draft_memory_and_allowances_do_not_invalidate_retained_evidence(self):
        options = Options('azure_responses', 'model', prompt_cache='explicit', cache_scope='a'*64)
        turn = replace(snapshot(), sections=(*snapshot().sections,
            Section('evidence', 'saved-page', b'Retained exact source'),
            Section('history', 'old-check', b'Old diagnostic'),
            Section('memory', 'strategy', b'Current strategy'),
            Section('feedback', 'new-check', b'Current diagnostic')))
        before = cache_prefixes(build(turn, options, renderer=prover.render))
        for kind in ('candidate', 'memory', 'feedback', 'capabilities', 'question'):
            changed = replace(turn, sections=tuple(replace(s, text=b'Changed') if s.kind == kind else s
                                                   for s in turn.sections))
            after = cache_prefixes(build(changed, options, renderer=prover.render))
            self.assertEqual(before['EVIDENCE saved-page'], after['EVIDENCE saved-page'])
            self.assertEqual(before['HISTORY old-check'], after['HISTORY old-check'])
        wire = build(turn, options, renderer=prover.render).wire
        last = wire.rfind(b'"prompt_cache_breakpoint"')
        self.assertGreater(wire.index(b'MEMORY strategy'), last)
        self.assertGreater(wire.index(b'FEEDBACK new-check'), last)

    def test_all_roles_produce_provider_accepted_wire(self):
        routes = (Route('openai', 'model', 'codex'),
                  Route('azure', 'model', 'azure_responses', endpoint='https://example.test/responses'),
                  Route('amazon-bedrock-runtime', 'model', 'bedrock_responses',
                        endpoint='https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses'))
        artifacts = Artifacts()
        for role in (prover, reviewer, stop_reviewer, researcher, web_searcher):
            for route in routes:
                with self.subTest(role=role.__name__, transport=route.transport):
                    if role is web_searcher and route.transport != 'codex':
                        with self.assertRaises(ValueError):
                            build(snapshot(), Options(route.transport, route.model), renderer=role.render)
                        continue
                    turn = stop_snapshot() if role is stop_reviewer else snapshot()
                    output = build(turn, Options(route.transport, route.model), renderer=role.render)
                    request = ModelRequest(route, output.provider_role, LIMITS,
                        artifacts.save(output.wire), artifacts.save(output.setup) if output.setup else None)
                    validate_wire(request, output.wire, output.setup)
                    wire = decode(output.wire, limit=MAX_BYTES)
                    if route.transport == 'codex':
                        setup = decode(output.setup, limit=MAX_BYTES)['baseInstructions']
                        self.assertEqual(setup.encode(), role.INSTRUCTIONS + SCHEMA_INSTRUCTIONS + role.SCHEMA)
                        self.assertNotIn('outputSchema', wire)
                    else:
                        self.assertNotIn('text', wire)
                        self.assertEqual(wire['input'][0]['content'].encode(),
                                         role.INSTRUCTIONS + SCHEMA_INSTRUCTIONS + role.SCHEMA)
                        self.assertFalse(wire['store'])
                        self.assertTrue(wire['stream'])

    def test_codex_accepts_text_input_and_rejects_native_schema(self):
        output = build(snapshot(), Options('codex', 'model'), renderer=prover.render)
        artifacts = Artifacts()
        request = ModelRequest(Route('openai', 'model', 'codex'), 'prover', LIMITS,
                               artifacts.save(output.wire), artifacts.save(output.setup))
        base = b'{"input":[{"type":"text","text":"Return JSON matching the schema in instructions"}]'
        validate_wire(request, base + b'}', output.setup)
        for suffix in (b',"outputSchema":null}', b',"outputSchema":{}}',
                       b',"outputSchema":{"type":"array"}}'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                validate_wire(request, base + suffix, output.setup)

    def test_codex_reasoning_effort_is_frozen_and_validated(self):
        artifacts, outputs = Artifacts(), []
        for effort in (None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
            with self.subTest(effort=effort):
                output = build(snapshot(), Options('codex', 'model', reasoning=effort), renderer=prover.render)
                request = ModelRequest(Route('openai', 'model', 'codex'), 'prover', LIMITS,
                                       artifacts.save(output.wire), artifacts.save(output.setup))
                validate_wire(request, output.wire, output.setup)
                wire = decode(output.wire, limit=MAX_BYTES)
                self.assertEqual(wire.get('effort'), effort)
                self.assertEqual('effort' in wire, effort is not None)
                outputs.append(output)
        self.assertEqual(len({output.id for output in outputs}), len(outputs))
        self.assertEqual(len({output.setup for output in outputs}), 1)
        base = outputs[0].wire[:-1]
        for value in (b'null', b'true', b'1', b'{}', b'[]', b'""', b'"invalid"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_wire(request, base + b',"effort":' + value + b'}', output.setup)

    def test_replacing_schema_changes_instructions_and_cache_identity(self):
        turn = snapshot()
        def alternative(turn):
            return replace(prover.render(turn), schema=schema_bytes(obj(proof={'type': 'string'})))
        for transport in ('codex', 'azure_responses'):
            options = Options(transport, 'model') if transport == 'codex' else Options(
                transport, 'model', prompt_cache='explicit', cache_scope='a'*64)
            first = build(turn, options, renderer=prover.render)
            second = build(turn, options, renderer=alternative)
            self.assertNotEqual(first.id, second.id)
            a, b = (decode(p.wire, limit=MAX_BYTES) for p in (first, second))
            if transport == 'codex':
                self.assertNotEqual(first.setup, second.setup)
                self.assertEqual(first.wire, second.wire)
            else:
                self.assertNotEqual(a['input'][0], b['input'][0])
                self.assertEqual(a['input'][1:], b['input'][1:])
                self.assertNotEqual(a['prompt_cache_key'], b['prompt_cache_key'])

    def test_prompt_schema_counts_toward_setup_wire_limit(self):
        def render(turn):
            schema = schema_bytes(dict(obj(proof={'type': 'string'}), description='x'*3000))
            return Prompt('prover', 'bounded', 'bounded', b'Return a proof.', schema, turn.sections)
        turn = Snapshot((Section('subject', 'goal', b'True'),))
        output = build(turn, Options('codex', 'model'), renderer=render)
        self.assertGreater(len(output.setup), 3000)
        self.assertLess(len(output.wire), 1000)
        with self.assertRaises(ValueError):
            build(turn, Options('codex', 'model', max_bytes=1000), renderer=render)

    def test_renderer_replacement_owns_instructions_schema_and_layout(self):
        frozen, options = snapshot(), Options('azure_responses', 'model-a')
        default = build(frozen, options, renderer=prover.render)
        def alternative(turn):
            return Prompt('prover', 'model-b-direct', 'direct-proof-v1', b'Give only a proof.',
                          schema_bytes(obj(proof={'type': 'string'})), tuple(reversed(turn.sections)))
        alternate = build(frozen, replace(options, model='model-b'), renderer=alternative)
        wire = decode(alternate.wire, limit=MAX_BYTES)
        self.assertEqual(wire['input'][1]['content'].splitlines()[0], 'QUESTION research-question')
        self.assertIn('Give only a proof.', wire['input'][0]['content'])
        self.assertNotIn('text', wire)
        self.assertTrue(wire['input'][0]['content'].endswith(alternative(frozen).schema.decode()))
        self.assertNotEqual(default.id, alternate.id)
        # A later renderer change cannot alter already frozen requests.
        self.assertEqual(default, build(frozen, options, renderer=prover.render))

    def test_exact_unicode_large_text_without_large_json_operations(self):
        # Multi-byte characters straddle wire writer chunks; all JSON escapes occur.
        raw = b'x'*8191 + ('😀α"\\\n'*30000).encode() + bytes(range(32))
        turn = replace(snapshot(), sections=(subject('goal', signature()), Section('source', 'raw', raw)))
        _prefix.cache_clear()
        original = json.dumps
        sizes = []
        def bounded(value, *args, **kwargs):
            def measure(v):
                if isinstance(v, str):
                    self.assertLessEqual(len(v.encode()), 16384)
                elif isinstance(v, dict):
                    for k, child in v.items():
                        measure(k)
                        measure(child)
                elif isinstance(v, (list, tuple)):
                    for child in v:
                        measure(child)
            measure(value)
            result = original(value, *args, **kwargs)
            sizes.append(len(result.encode()))
            self.assertLessEqual(sizes[-1], 16384)
            return result
        with patch('json.dumps', side_effect=bounded), patch('json.loads', side_effect=AssertionError('JSON decode')):
            for options in (Options('codex', 'model'), Options('azure_responses', 'model'),
                            Options('azure_responses', 'model', prompt_cache='explicit', cache_scope='a'*64)):
                output = build(turn, options, renderer=prover.render)
                wire = decode(output.wire, limit=MAX_BYTES)
                text = wire['input'][0]['text'] if options.transport == 'codex' else wire['input'][1]['content']
                if isinstance(text, list):
                    text = text[0]['text']
                self.assertEqual(text.partition('\n')[2].encode(), raw)
        self.assertTrue(sizes)

    def test_whole_evidence_pages_are_bounded_and_omissions_visible(self):
        turn = Snapshot((subject('goal', signature()),
                         Section('evidence', 'pinned-first', 'αβ'.encode()),
                         Section('evidence', 'too-large', b'123456'),
                         Section('evidence', 'recent', b'xy'),
                         Section('feedback', 'compiler', b'exact diagnostic')), evidence_bytes=6)
        rendered = prover.render(turn)
        pages = [s for s in rendered.sections if s.kind == 'evidence']
        self.assertEqual([s.name for s in pages[:2]], ['pinned-first', 'recent'])
        self.assertIn(b'1 evidence section(s) omitted', pages[2].text)
        self.assertEqual(next(s.text for s in rendered.sections if s.kind == 'feedback'), b'exact diagnostic')
        self.assertEqual(turn.sections[2].text, b'123456')

    def test_wire_identity_binds_exact_artifacts_and_owner_generations(self):
        artifacts = Artifacts()
        first, second = artifacts.save(b'first'), artifacts.save(b'second')
        turn = replace(snapshot(), artifacts=(first,), owners=(OwnerVersion('obl_'+'a'*64, 0),))
        options = Options('codex', 'model')
        before = build(turn, options, renderer=prover.render)
        for changed in (replace(turn, artifacts=(second,)),
                        replace(turn, owners=(replace(turn.owners[0], generation=1),))):
            after = build(changed, options, renderer=prover.render)
            self.assertEqual(before.wire, after.wire)
            self.assertNotEqual(before.id, after.id)
        with self.assertRaises(FrozenInstanceError):
            before.wire = b'changed'

    def test_cache_keys_preserve_prefix_and_separate_model_contract_and_route(self):
        turn = snapshot()
        options = Options('azure_responses', 'model', prompt_cache='explicit', cache_scope='a'*64)
        def key(turn=turn, options=options, renderer=prover.render):
            return decode(build(turn, options, renderer=renderer).wire, limit=MAX_BYTES)['prompt_cache_key']
        original = key()
        changed = replace(turn, sections=(*turn.sections, Section('feedback', 'new', b'new feedback')))
        self.assertEqual(original, key(changed))
        changed = replace(turn, sections=tuple(replace(s, text=b'web_calls=1; research available')
            if s.kind == 'capabilities' else s for s in turn.sections))
        for transport in ('codex', 'azure_responses', 'bedrock_responses'):
            controls = Options(transport, 'model')
            before = build(turn, controls, renderer=prover.render)
            after = build(changed, controls, renderer=prover.render)
            prefix, marker, ending = before.wire.partition(b'QUESTION next-action\\n')
            next_prefix, next_marker, next_ending = after.wire.partition(marker)
            self.assertTrue(marker)
            self.assertEqual((prefix, marker), (next_prefix, next_marker))
            self.assertNotIn(b'CAPABILITIES remaining', prefix)
            self.assertIn(b'CAPABILITIES remaining', ending)
            self.assertIn(b'Return the action JSON now.', ending)
            self.assertNotEqual(ending, next_ending)
        self.assertNotEqual(original, key(options=replace(options, cache_scope='b'*64)))
        self.assertNotEqual(original, key(options=replace(options, model='different')))
        self.assertNotEqual(original, key(renderer=reviewer.render))
        def changed_wording(turn):
            return replace(prover.render(turn), instructions=b'Updated instructions.')
        self.assertNotEqual(original, key(renderer=changed_wording))
        for cache in (None, 'disabled'):
            wire = decode(build(turn, replace(options, prompt_cache=cache), renderer=prover.render).wire,
                          limit=MAX_BYTES)
            self.assertFalse({'prompt_cache_key', 'prompt_cache_options'} & wire.keys())

    def test_schemas_have_strict_objects_and_resolvable_local_refs(self):
        for role in (prover, reviewer, researcher, web_searcher):
            schema = decode(role.SCHEMA, limit=16384)
            pending = [schema]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if value.get('type') == 'object':
                        self.assertFalse(value['additionalProperties'])
                        self.assertEqual(set(value['required']), set(value['properties']))
                    if '$ref' in value:
                        self.assertIn(value['$ref'].removeprefix('#/$defs/'), schema['$defs'])
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)

    def test_probe_contract_accepts_only_bodies_and_offers_executable_examples(self):
        schema = decode(prover.SCHEMA, limit=16384)
        action = next(a for a in schema['properties']['action']['anyOf']
                      if a['properties']['mode']['enum'] == ['probe'])
        cases = action['properties']['cases']
        self.assertEqual((cases['minItems'], cases['maxItems']), (1, 3))
        self.assertEqual(set(cases['items']['properties']), {'code'})
        self.assertFalse(cases['items']['additionalProperties'])
        self.assertIn(b'only code', prover.INSTRUCTIONS)
        self.assertIn('Lean.Elab.Tactic.evalTactic (← `(tactic| intro C))'.encode(), prover.INSTRUCTIONS)
        self.assertIn(b'body starts at line 2', prover.INSTRUCTIONS)

    def test_research_schema_needs_only_a_question_and_no_web_allowance(self):
        turn = Snapshot((Section('question', 'question', b'How should I approach this proof?'),))
        schema = decode(researcher.SCHEMA, limit=16384)
        self.assertEqual(set(schema['properties']), {'answer', 'unresolved'})
        for transport in ('codex', 'azure_responses', 'bedrock_responses'):
            result = build(turn, Options(transport, 'model'), renderer=researcher.render)
            self.assertEqual(result.provider_role, 'research')
        with self.assertRaisesRegex(ValueError, 'allowance'):
            web_searcher.render(turn)

    def test_web_search_schema_matches_advisory_evidence_without_access_claims(self):
        schema = decode(web_searcher.SCHEMA, limit=16384)
        self.assertEqual(set(schema['properties']), {'answer', 'sources', 'unresolved'})
        source = dict(url='https://example.test/paper', title='Paper', location='Section 2',
                      excerpt='', summary='The question remains open.', hypotheses='Positive integers.')
        self.assertEqual(set(schema['properties']['sources']['items']['properties']), set(source))
        artifacts = Artifacts()
        stage = Stage('a'*64, 0, Scope(OwnerVersion('obl_'+'b'*64, 0), 'env_'+'c'*64,
                      artifacts.save(b'context')), WebSearch('Find a reference.', web_calls=2))
        payload = schema_bytes(dict(answer='The question remains open.', sources=[source], unresolved=['Exact bound?']))
        evidence = web_search_evidence(stage, payload, b'')
        self.assertIn(b'advisory', evidence)

    def test_invalid_inputs_and_escaped_limits_fail_before_dispatch(self):
        for bad in (b'\xff', b'x'*(MAX_BYTES+1)):
            with self.assertRaises(ValueError):
                Section('source', 'source', bad)
        with self.assertRaises(ValueError):
            Snapshot([subject('goal', signature())])
        with self.assertRaises(ValueError):
            Snapshot((subject('goal', signature()), subject('goal', signature())))
        for role in (prover, reviewer, researcher, web_searcher):
            with self.assertRaises(ValueError):
                role.render(Snapshot(()))
        for options in (dict(transport='unknown', model='x'),
                        dict(transport='codex', model='x', max_output_tokens=10),
                        dict(transport='azure_responses', model='x', prompt_cache='explicit'),
                        dict(transport='azure_responses', model=None)):
            with self.assertRaises(ValueError):
                Options(**options)
        with self.assertRaises(ValueError):
            build(snapshot(), Options('codex', 'model', max_bytes=10), renderer=prover.render)
        turn = Snapshot((subject('goal', signature()), Section('source', 'escapes', b'\x00'*16000)))
        with self.assertRaises(ValueError):
            build(turn, Options('codex', 'model', max_bytes=32000), renderer=prover.render)

    def test_prompt_imports_only_domain_source_and_itself(self):
        root = Path(__file__).parents[1]/'midas'/'prompt'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level == 2:
                    self.assertIn(node.module.split('.')[0], ('domain', 'source'), path.name)
                if isinstance(node, ast.Import):
                    self.assertFalse(any(n.name.startswith('midas_formalizer') for n in node.names))


if __name__ == '__main__':
    unittest.main()
