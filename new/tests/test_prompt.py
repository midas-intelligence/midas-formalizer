"""Offline prompt/provider contracts. No Lean, network, model, or subprocess calls."""
import ast
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from midas.domain import OwnerVersion
from midas.domain.wire import decode
from midas.prompt import Options, Prompt, Section, Snapshot, build, prover, researcher, reviewer, subject
from midas.prompt.protocol import MAX_BYTES, obj, schema_bytes
from midas.prompt.wire import _prefix
from midas.provider import ModelRequest, Route
from midas.provider.wire import validate_wire
from midas.tools import Research, Scope, Stage, research_evidence
from midas.tools.protocol import pack
from tests.test_provider import Artifacts, LIMITS
from tests.test_source import signature


def snapshot():
    return Snapshot((Section('source', 'Main.lean', b'import Mathlib\n'),
                     subject('goal', signature()),
                     Section('candidate', 'current-proof', b'by\n  trivial'),
                     Section('capabilities', 'remaining', b'web_calls=2; research available'),
                     Section('question', 'research-question', b'Find a precise reference.')))


class PromptTests(unittest.TestCase):
    def test_all_roles_produce_provider_accepted_wire(self):
        routes = (Route('openai', 'model', 'codex'),
                  Route('azure', 'model', 'azure_responses', endpoint='https://example.test/responses'),
                  Route('amazon-bedrock-runtime', 'model', 'bedrock_responses',
                        endpoint='https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses'))
        artifacts = Artifacts()
        for role in (prover, reviewer, researcher):
            for route in routes:
                with self.subTest(role=role.__name__, transport=route.transport):
                    if role is researcher and route.transport != 'codex':
                        with self.assertRaises(ValueError):
                            build(snapshot(), Options(route.transport, route.model), renderer=role.render)
                        continue
                    output = build(snapshot(), Options(route.transport, route.model), renderer=role.render)
                    request = ModelRequest(route, output.provider_role, LIMITS,
                        artifacts.save(output.wire), artifacts.save(output.setup) if output.setup else None)
                    validate_wire(request, output.wire, output.setup)
                    wire = decode(output.wire, limit=MAX_BYTES)
                    if route.transport == 'codex':
                        setup = decode(output.setup, limit=MAX_BYTES)['baseInstructions']
                        self.assertEqual(setup, role.INSTRUCTIONS.decode())
                        self.assertEqual(wire['outputSchema'], decode(role.SCHEMA, limit=16384))
                        self.assertIn(b'"outputSchema":' + role.SCHEMA, output.wire)
                    else:
                        self.assertEqual(wire['text']['format']['schema'], decode(role.SCHEMA, limit=16384))
                        self.assertEqual(wire['input'][0]['content'], role.INSTRUCTIONS.decode())
                        self.assertTrue(wire['text']['format']['strict'])
                        self.assertFalse(wire['store'])
                        self.assertTrue(wire['stream'])

    def test_codex_requires_native_schema_in_turn_not_instructions(self):
        output = build(snapshot(), Options('codex', 'model'), renderer=prover.render)
        artifacts = Artifacts()
        request = ModelRequest(Route('openai', 'model', 'codex'), 'prover', LIMITS,
                               artifacts.save(output.wire), artifacts.save(output.setup))
        base = b'{"input":[{"type":"text","text":"Return JSON matching the schema in instructions"}]'
        for suffix in (b'}', b',"outputSchema":null}', b',"outputSchema":{}}',
                       b',"outputSchema":{"type":"array"}}'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                validate_wire(request, base + suffix, output.setup)

    def test_replacing_schema_changes_native_wire_and_cache_identity(self):
        turn = snapshot()
        def alternative(turn):
            return replace(prover.render(turn), schema=schema_bytes(obj(proof={'type': 'string'})))
        for transport in ('codex', 'azure_responses'):
            options = Options(transport, 'model') if transport == 'codex' else Options(
                transport, 'model', prompt_cache='explicit', cache_scope='a'*64)
            first = build(turn, options, renderer=prover.render)
            second = build(turn, options, renderer=alternative)
            self.assertNotEqual(first.wire, second.wire)
            self.assertNotEqual(first.id, second.id)
            a, b = (decode(p.wire, limit=MAX_BYTES) for p in (first, second))
            if transport == 'codex':
                self.assertEqual(first.setup, second.setup)
                self.assertNotEqual(a['outputSchema'], b['outputSchema'])
            else:
                self.assertEqual(a['input'], b['input'])
                self.assertNotEqual(a['prompt_cache_key'], b['prompt_cache_key'])

    def test_native_schema_counts_toward_turn_wire_limit(self):
        def render(turn):
            schema = schema_bytes(dict(obj(proof={'type': 'string'}), description='x'*3000))
            return Prompt('prover', 'bounded', 'bounded', b'Return a proof.', schema, turn.sections)
        turn = Snapshot((Section('subject', 'goal', b'True'),))
        output = build(turn, Options('codex', 'model'), renderer=render)
        self.assertLess(len(output.setup), 1000)
        self.assertGreater(len(output.wire), 3000)
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
        self.assertEqual(wire['text']['format']['name'], 'direct-proof-v1')
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
            for transport in ('codex', 'azure_responses'):
                output = build(turn, Options(transport, 'model'), renderer=prover.render)
                wire = decode(output.wire, limit=MAX_BYTES)
                text = wire['input'][0]['text'] if transport == 'codex' else wire['input'][1]['content']
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
        self.assertEqual(rendered.sections[-1].text, b'exact diagnostic')
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
        for role in (prover, reviewer, researcher):
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

    def test_research_schema_matches_existing_advisory_evidence_contract(self):
        schema = decode(researcher.SCHEMA, limit=16384)
        self.assertEqual(set(schema['properties']), {'answer', 'sources', 'unresolved'})
        artifacts = Artifacts()
        stage = Stage('a'*64, 0, Scope(OwnerVersion('obl_'+'b'*64, 0), 'env_'+'c'*64,
                      artifacts.save(b'context')), Research('Find a reference.', web_calls=2))
        payload = schema_bytes(dict(answer='The question remains open.', sources=[], unresolved=['Exact bound?']))
        frame = pack(dict(type='webSearch', id='one', action=dict(type='search')))
        evidence = research_evidence(stage, payload, len(frame).to_bytes(8, 'little') + frame)
        self.assertIn(b'advisory', evidence)

    def test_invalid_inputs_and_escaped_limits_fail_before_dispatch(self):
        for bad in (b'\xff', b'x'*(MAX_BYTES+1)):
            with self.assertRaises(ValueError):
                Section('source', 'source', bad)
        with self.assertRaises(ValueError):
            Snapshot([subject('goal', signature())])
        with self.assertRaises(ValueError):
            Snapshot((subject('goal', signature()), subject('goal', signature())))
        for role in (prover, reviewer, researcher):
            with self.assertRaises(ValueError):
                role.render(Snapshot(()))
        for options in (dict(transport='unknown', model='x'),
                        dict(transport='codex', model='x', reasoning='high'),
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
