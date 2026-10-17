"""Provider contracts and offline transport tests. No Lean or live provider calls."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import httpx

from midas.domain import (ArtifactRef, CapacityGrant, DeliveryCertainty as Delivery, FrozenRequest,
    Invocation, Job, JobKind, OwnerVersion, ReceiptBinding, ResourceLimits, ResultStatus, Session, Termination, Usage)
from midas.executor import Executor
from midas.provider import (BearerCredentials, CodexTransport, Limits, ModelOutput, ModelRequest,
    Provider, ResponsesTransport, Route, ScriptedTransport, worker_payload)
from midas.provider.protocol import META_BYTES, pack
from midas.provider.transport import Collector, normalize_usage, retry_after
from midas.provider.wire import decode, extend_object, validate_wire

LIMITS = Limits(2000, 500, 500, 500, 500, 1000, 200, 131072, 1048576, 4194304)
ROUTE = Route('azure', 'model', 'azure_responses', 'test', 'https://example.test/responses')
SCHEMA = b'{"type":"object","properties":{"proof":{"type":"string"}},"required":["proof"],"additionalProperties":false}'
WIRE = (b'{ "model":"model", "input":[{"role":"user","content":"prove"}], "stream":true,"store":false,'
        b'"text":{"format":{"type":"json_schema","name":"proof","strict":true,"schema":' + SCHEMA + b'}} }')


class Artifacts:
    def __init__(self):
        self.data = {}

    def write_bounded(self, data, *, max_bytes):
        if len(data) > max_bytes:
            raise ValueError('artifact limit')
        ref = ArtifactRef(str(uuid4()), sha256(data).hexdigest(), len(data))
        self.data[ref.id] = data
        return ref

    def save(self, data):
        return self.write_bounded(data, max_bytes=len(data))

    def read_artifact(self, ref, *, max_bytes):
        return ref.verify(self.data[ref.id], max_bytes=max_bytes)


def issue(artifacts, request):
    owner = OwnerVersion('obl_' + '0'*64, 0)
    job = Job('model-job', JobKind.MODEL, owner, request.inputs, (owner,),
              ResourceLimits(2000, 256, request.limits.output_bytes, request.limits.event_bytes + META_BYTES))
    frozen = FrozenRequest(job, 'env_'+'1'*64, artifacts.save(request.payload), artifacts.save(b'frozen request'))
    return Invocation(str(uuid4()), Session(str(uuid4()), str(uuid4()), 1), frozen, artifacts.save(b'reservation'))


def context(artifacts, inv, cancel=None):
    return SimpleNamespace(reader=artifacts, staging=artifacts, capacity_grant=CapacityGrant(inv),
        check=lambda: None, cancellation=cancel or threading.Event(), deadline=time.monotonic()+2)


def sse(*events):
    return b''.join(b'data: '+json.dumps(e, ensure_ascii=False).encode()+b'\n\n' for e in events)


def terminal(text='{"proof":"by trivial"}', **extra):
    return {'type':'response.completed', 'response': {'id':'response-1', 'status':'completed',
        'output':[{'content':[{'type':'output_text','text':text}]}], **extra}}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.a = Artifacts()
        self.request = ModelRequest(ROUTE, 'prover', LIMITS, self.a.save(WIRE))

    def http(self, handler, *, request=None, cancel=None, deadline=None):
        request = request or self.request
        transport = ResponsesTransport(BearerCredentials(request.route, 'test-secret'),
                                        transport_factory=lambda: httpx.MockTransport(handler))
        return transport(request, self.a.data[request.wire.id], None,
                         cancel or threading.Event(), deadline or time.monotonic()+2)

    def test_config_projection_and_binary_roundtrip(self):
        from midas.policy import load_config
        cfg = load_config((Path(__file__).parents[1]/'config.json').read_bytes())
        req = replace(self.request, route=Route.from_config(cfg.prover), limits=Limits.from_config(cfg.model))
        self.assertEqual(ModelRequest.load(req.payload), req)
        self.assertLess(len(req.payload), META_BYTES)
        self.assertNotIn(b'env_', req.payload)
        for config in (cfg.model, cfg.research_model):
            limits = Limits.from_config(replace(config, request_max_retries=3, stream_max_retries=1))
            self.assertEqual((limits.request_max_retries, limits.stream_max_retries), (3, 1))
            saved = replace(req, limits=limits)
            self.assertEqual(ModelRequest.load(saved.payload), saved)
            for name in ('request_max_retries', 'stream_max_retries'):
                changed = replace(saved, limits=replace(limits, **{name: 0}))
                self.assertNotEqual(changed.payload, saved.payload)

    def test_invalid_codex_retry_limits_rejected(self):
        for name in ('request_max_retries', 'stream_max_retries'):
            for value in (-1, True, 1.5, '2', None, 2**63):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(LIMITS, **{name: value})

    def test_route_and_role_validation(self):
        for change in ({'provider':'openai'}, {'transport':'unknown'}, {'endpoint':'http://example.test'},
                       {'endpoint':'https://u:p@example.test'}, {'endpoint':'https://example.test/#a'},
                       {'credential_env':'a-b'}, {'model':'m\n'}, {'model':None}, {'model':' m'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(ROUTE, **change)
        for change in ({'role':'research'}, {'limits':replace(LIMITS, web_calls=1)}):
            with self.assertRaises(ValueError):
                replace(self.request, **change)

    def test_request_reservation_binding(self):
        inv = issue(self.a, self.request)
        self.request.validate_for(inv.request, self.a)
        for job in (replace(inv.request.job, kind=JobKind.SEARCH), replace(inv.request.job, input_refs=()),
                    replace(inv.request.job, limits=replace(inv.request.job.limits, output_bytes=1))):
            with self.assertRaises(ValueError):
                self.request.validate_for(replace(inv.request, job=job), self.a)

    def test_large_payloads_never_use_json_library(self):
        raw = WIRE.replace(b'prove', ('x'*500000+'∀').encode())
        frame = b'{"type":"response.completed","response":{"output":[{"content":[{"type":"output_text","text":"'+b'x'*100000+b'"}]}]}}'
        req = replace(self.request, wire=self.a.save(raw))
        with patch('json.loads', side_effect=AssertionError('JSON decode')), patch('json.dumps', side_effect=AssertionError('JSON encode')):
            validate_wire(req, raw)
            self.assertEqual(ModelRequest.load(req.payload), req)
            out = self.http(lambda _: httpx.Response(200, stream=Chunks([b'data: '+frame+b'\n\n'])), request=req)
        self.assertIsNone(out.error)
        self.assertEqual(out.raw, b'x'*100000)

    def test_protocol_unicode_and_invalid_shapes(self):
        value = {'text':'a'*8190+'∀\n"\\😀', 'x':[True, False, None, 1, -2, .25]}
        for ascii in (False, True):
            self.assertEqual(decode(json.dumps(value, ensure_ascii=ascii).encode(), limit=100000), value)
        for data in (b'{"x":1,"x":2}', b'[1,]', b'"\\ud800"', b'"\\udc00"', b'"\xff"',
                     b'{}x', b'['*18+b'0'+b']'*18, b'01', b'"bad\ntext"'):
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                decode(data, limit=100000)

    def test_structured_json_wire_is_preserved_without_trimming(self):
        schema = {'type':'json_schema','name':'proof','strict':True,'schema':{'type':'object',
                  'properties':{'proof':{'type':'string'}},'required':['proof'],'additionalProperties':False}}
        raw = json.dumps({**json.loads(WIRE), 'text':{'format':schema}}, indent=2).encode()
        req = replace(self.request, wire=self.a.save(raw))
        validate_wire(req, raw)
        calls = []
        def handler(request):
            calls.append(request.content)
            return httpx.Response(200, stream=Chunks([sse(terminal(' \n{"proof":"by trivial"}\n '))]))
        out = self.http(handler, request=req)
        self.assertEqual(calls, [raw])
        self.assertEqual(out.raw, b' \n{"proof":"by trivial"}\n ')
        for text in (None, {'format':{'type':'text'}}, {'format':{'type':'json_object'}},
                     {'format':{**schema,'strict':False}}, {'format':{**schema,'strict':None}},
                     {'format':{**schema,'schema':None}}):
            with self.assertRaises(ValueError):
                validate_wire(req, json.dumps({**json.loads(raw),'text':text}).encode())

    def test_schema_rejection_does_not_retry_with_unstructured_output(self):
        calls = []
        def handler(request):
            calls.append(request.content)
            return httpx.Response(400, json={'error': {'code': 'invalid_json_schema'}})
        out = self.http(handler)
        self.assertEqual(out.error, 'configuration')
        self.assertEqual(calls, [WIRE])

    def test_wire_rejects_tools_state_and_model_mismatch(self):
        for update in ({'tools':[]}, {'model':'wrong'}, {'stream':False}, {'store':True},
                       {'input':[{'role':'user','content':[{'type':'input_image','text':'x'}]}]}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_wire(self.request, json.dumps({**json.loads(WIRE), **update}).encode())

    def test_usage_and_exact_wire_one_call(self):
        calls = []
        stream = Chunks([sse(terminal(usage={'input_tokens':12,'output_tokens':5,
                              'input_tokens_details':{'cached_tokens':3,'cache_write_tokens':2}}))])
        def handler(req):
            calls.append(req.content)
            self.assertEqual(req.headers['Authorization'], 'Bearer test-secret')
            return httpx.Response(200, headers={'x-request-id':'request-1'}, stream=stream)
        out = self.http(handler)
        self.assertEqual(calls, [WIRE])
        self.assertTrue(stream.closed)
        self.assertEqual(out.usage, Usage(12,3,5,None))
        self.assertEqual(out.cache_write_tokens, 2)
        self.assertEqual(out.request_id, 'request-1')
        self.assertEqual(out.delivery, Delivery.DELIVERED)

    def test_null_and_invalid_usage(self):
        out = self.http(lambda _: httpx.Response(200, stream=Chunks([sse(terminal())])))
        self.assertEqual(out.usage, Usage())
        usage, writes = normalize_usage({'inputTokens':True,'outputTokens':-1,'cachedInputTokens':0,
                                         'cacheWriteInputTokens':4,'input_tokens_details':[]})
        self.assertEqual(usage, Usage(None,0,None,None))
        self.assertEqual(writes, 4)

    def test_fragmented_stream_and_unicode_repair(self):
        body = sse({'type':'response.output_text.delta','delta':'∀'},
                   {'type':'response.output_text.done','text':'∀'}, terminal('\ufffd'))
        out = self.http(lambda _: httpx.Response(200, stream=Chunks([body[i:i+1] for i in range(len(body))])))
        self.assertEqual(out.raw, '∀'.encode())
        self.assertIsNone(out.error)

    def test_terminal_overrides_deltas_and_done_fallback(self):
        body = sse({'type':'response.output_text.delta','delta':'draft'}, terminal('final'))
        self.assertEqual(self.http(lambda _: httpx.Response(200, stream=Chunks([body]))).raw, b'final')
        body = sse({'type':'response.output_text.done','text':'done'}, terminal(''))
        self.assertEqual(self.http(lambda _: httpx.Response(200, stream=Chunks([body]))).raw, b'done')

    def test_http_failures_no_retry_or_error_body_retention(self):
        for status, kind in ((401,'authentication'),(429,'rate_limit'),(500,'transient'),(302,'configuration')):
            calls = []
            def handler(req):
                calls.append(req)
                return httpx.Response(status, headers={'retry-after-ms':'123'}, content=b'test-secret')
            out = self.http(handler)
            self.assertEqual((len(calls), out.error, out.retry_after_ms), (1,kind,123))
            self.assertNotIn(b'test-secret', out.raw+out.events)
            self.assertEqual(out.delivery, Delivery.DELIVERED)

    def test_connect_failure_not_sent_write_failure_unknown(self):
        for exc, certainty in ((httpx.ConnectError,Delivery.NOT_SENT),(httpx.WriteError,Delivery.UNKNOWN)):
            def handler(req):
                raise exc('test-secret', request=req)
            out = self.http(handler)
            self.assertEqual((out.delivery,out.error), (certainty,'transient'))
            self.assertNotIn('test-secret', repr(out))

    def test_cancellation_before_send(self):
        cancel = threading.Event(); cancel.set()
        out = self.http(lambda _: self.fail('sent'), cancel=cancel)
        self.assertEqual((out.error,out.delivery), ('cancelled',Delivery.NOT_SENT))

    def test_inflight_cancellation_closes_stream(self):
        cancel = threading.Event()
        class Hanging(Chunks):
            async def __aiter__(self):
                yield sse({'type':'response.output_text.delta','delta':'partial'})
                cancel.set()
                await asyncio.sleep(10)
        stream = Hanging([])
        out = self.http(lambda _: httpx.Response(200, stream=stream), cancel=cancel)
        self.assertEqual((out.error,out.raw,out.delivery), ('cancelled',b'partial',Delivery.DELIVERED))
        self.assertTrue(stream.closed)

    def test_deadline_enforced(self):
        async def handler(req):
            await asyncio.sleep(10)
        out = self.http(handler, deadline=time.monotonic()+.03)
        self.assertEqual((out.error,out.delivery), ('timeout',Delivery.UNKNOWN))

    def test_truncation_and_incomplete_keep_partial_and_usage(self):
        body = sse({'type':'response.output_text.delta','delta':'partial'})
        out = self.http(lambda _: httpx.Response(200, stream=Chunks([body])))
        self.assertEqual((out.raw,out.error), (b'partial','transient'))
        body = sse({'type':'response.incomplete','response':{'status':'incomplete','usage':{'output_tokens':7}}})
        out = self.http(lambda _: httpx.Response(200, stream=Chunks([body])))
        self.assertEqual((out.error,out.usage.output_tokens), ('malformed_output',7))

    def test_limits_include_comments_and_unterminated_frames(self):
        req = replace(self.request, limits=replace(LIMITS,output_bytes=8,frame_bytes=128,event_bytes=256))
        for body in (b':'+b'x'*300,b': keepalive\n\n'*30):
            out = self.http(lambda _: httpx.Response(200, stream=Chunks([body])), request=req)
            self.assertEqual(out.error, 'output_limit')
        c = Collector(req.limits); c.text('∀∀')
        with self.assertRaises(Exception):
            c.text('∀',append=True)
        self.assertEqual(bytes(c.raw), '∀∀'.encode())

    def test_refusal_is_not_a_successful_json_response(self):
        body = sse({'type':'response.completed','response':{'status':'completed',
            'usage':{'output_tokens':3},'output':[{'content':[{'type':'refusal','refusal':'declined'}]}]}})
        out = self.http(lambda _:httpx.Response(200,stream=Chunks([body])))
        self.assertEqual(out.error,'refusal')
        self.assertEqual(out.usage.output_tokens,3)
        self.assertIn(b'declined',out.events)

    def test_late_executor_cancel_preserves_receipt_usage(self):
        p,inv,result,receipt,auth = self.service(ModelOutput(raw=b'proof',usage=Usage(5,0,2,None)))
        result = replace(result,status=ResultStatus.CANCELLED)
        receipt = replace(receipt,result_ref=self.a.save(worker_payload(result)))
        admitted = p.admit_receipt(self.request,inv,result,receipt,self.a,auth)
        self.assertEqual(admitted.status,ResultStatus.CANCELLED)
        self.assertEqual(admitted.outcome.usage,Usage(5,0,2,None))

    def test_component_import_boundaries(self):
        import ast
        root = Path(__file__).parents[1]/'midas/provider'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom) and node.level == 2:
                    self.assertIn(node.module.split('.')[0],('domain','executor'))
                if isinstance(node,ast.Import):
                    self.assertFalse(any(x.name.startswith(('midas_formalizer','sqlite3')) for x in node.names))

    def test_credentials_and_retry_headers(self):
        credential = BearerCredentials.from_environment(replace(ROUTE,credential_env='EXAMPLE'),{'EXAMPLE':'secret'})
        with self.assertRaises(ValueError):
            credential(ROUTE)
        for secret in ('','x\ny','x\ry'):
            with self.assertRaises(ValueError):
                BearerCredentials(ROUTE, secret)
        self.assertEqual(retry_after({'retry-after':'NaN'}),0)
        self.assertEqual(retry_after({'retry-after':'1.5'}),1500)

    def service(self, output=None):
        inv = issue(self.a, self.request)
        provider = Provider([(ROUTE, ScriptedTransport({self.request.wire.sha256:output or ModelOutput(raw=b'proof')}))])
        result = provider.execute(inv.request, context(self.a, inv))
        receipt = ReceiptBinding(inv.original_execution_id, inv.request.request_ref,
                                  self.a.save(worker_payload(result)), self.a.save(b'seal'))
        return provider, inv, result, receipt, SimpleNamespace(verify_receipt=lambda *_:True)

    def test_scripted_receipt_and_no_candidate_authority(self):
        provider, inv, result, receipt, auth = self.service()
        admitted = provider.admit_receipt(self.request,inv,result,receipt,self.a,auth)
        self.assertEqual(self.a.data[admitted.raw.id],b'proof')
        self.assertEqual(admitted.outcome.raw,b'')
        self.assertEqual(result.status,ResultStatus.SUCCEEDED)

    def test_receipt_seal_invocation_and_artifact_tampering(self):
        p, inv, result, receipt, auth = self.service()
        for bad in (replace(receipt,original_execution_id=str(uuid4())),replace(receipt,result_ref=self.a.save(b'wrong'))):
            with self.assertRaises(ValueError):
                p.admit_receipt(self.request,inv,result,bad,self.a,auth)
        with self.assertRaises(ValueError):
            p.admit_receipt(self.request,inv,result,receipt,self.a,SimpleNamespace(verify_receipt=lambda *_:False))
        self.a.data[result.staged_artifacts[0].id] = b'changed'
        with self.assertRaises(ValueError):
            p.admit_receipt(self.request,inv,result,receipt,self.a,auth)

    def test_failure_receipt_preserves_cache_write_and_unknowns(self):
        output = ModelOutput(raw=b'partial',error='timeout',phase='reading',delivery=Delivery.UNKNOWN,
                             usage=Usage(10,2,None,None),cache_write_tokens=3)
        p,inv,result,receipt,auth = self.service(output)
        admitted = p.admit_receipt(self.request,inv,result,receipt,self.a,auth)
        self.assertEqual((admitted.outcome.cache_write_tokens,admitted.outcome.delivery),(3,Delivery.UNKNOWN))

    def test_empty_failure_survives_content_addressed_staging(self):
        from midas.storage import Staging
        inv = issue(self.a, self.request)
        output = ModelOutput(error='authentication')
        provider = Provider([(ROUTE, ScriptedTransport({self.request.wire.sha256: output}))])
        with TemporaryDirectory() as temporary:
            staging = Staging(Path(temporary)/'staging', max_bytes=LIMITS.event_bytes)
            try:
                ctx = context(self.a, inv)
                ctx.staging = staging
                result = provider.execute(inv.request, ctx)
                for ref in result.staged_artifacts:
                    self.a.data[ref.id] = staging.read_artifact(ref, max_bytes=LIMITS.event_bytes)
            finally:
                staging.close()
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(len(result.staged_artifacts), 2)
        receipt = ReceiptBinding(inv.original_execution_id, inv.request.request_ref,
            self.a.save(worker_payload(result)), self.a.save(b'seal'))
        admitted = provider.admit_receipt(self.request, inv, result, receipt, self.a,
            SimpleNamespace(verify_receipt=lambda *_: True))
        self.assertEqual(admitted.outcome.error, 'authentication')
        self.assertEqual(admitted.raw, admitted.events)
        self.assertEqual(self.a.data[admitted.raw.id], b'')

    def test_missing_route_invalid_wire_and_adapter_crash(self):
        inv = issue(self.a,self.request)
        out = Provider([]).invoke(self.request,context(self.a,inv))
        self.assertEqual((out.error,out.delivery),('configuration',Delivery.NOT_SENT))
        bad = replace(self.request,wire=self.a.save(b'{bad'))
        out = Provider([(ROUTE,lambda *_:self.fail('invoked'))]).invoke(bad,context(self.a,inv))
        self.assertEqual(out.delivery,Delivery.NOT_SENT)
        def crash(*_):
            raise RuntimeError('secret')
        out = Provider([(ROUTE,crash)]).invoke(self.request,context(self.a,inv))
        self.assertEqual((out.delivery,out.termination),(Delivery.UNKNOWN,Termination.UNKNOWN))
        self.assertNotIn('secret',repr(out))

    def test_real_executor_callback(self):
        inv = issue(self.a,self.request)
        p = Provider([(ROUTE,ScriptedTransport({self.request.wire.sha256:ModelOutput(raw=b'proof')}))])
        with TemporaryDirectory() as directory:
            executor = Executor(directory,workers=1,compiler_slots=1,compiler_memory_mib=256)
            workspace = Path(directory)/'worker'
            workspace.mkdir()
            executor.start(inv,p.execute,grant=CapacityGrant(inv),workspace=workspace,reader=self.a,staging=self.a)
            completed = ()
            until = time.monotonic()+2
            while not completed and time.monotonic() < until:
                completed = executor.poll()
                time.sleep(.005)
            executor.drain(timeout=2)
            self.assertEqual(completed[0].result.status,ResultStatus.SUCCEEDED)


class CodexTests(unittest.TestCase):
    def invoke(self, mode='success', *, web_calls=0, deadline=2, digest=None,
               provider='openai', limits=LIMITS, prepared=None):
        a = Artifacts()
        route = Route(provider,'model','codex')
        raw = prepared.wire if prepared else (b'{"input":[{"type":"text","text":"JSON proof"}],'
                                              b'"outputSchema":' + SCHEMA + b'}')
        setup = prepared.setup if prepared else b'{"baseInstructions":"Return a proof."}'
        role = prepared.provider_role if prepared else 'research' if web_calls else 'prover'
        req = ModelRequest(route, role, replace(limits,web_calls=web_calls), a.save(raw), a.save(setup))
        validate_wire(req, raw, setup)
        executable = str(Path(sys.executable).resolve())
        digest = digest or sha256(Path(executable).read_bytes()).hexdigest()
        with TemporaryDirectory() as directory:
            log = Path(directory)/'calls'
            transport = CodexTransport((executable,str(Path(__file__).parent/'fixtures/provider_server.py')),
                executable_sha256=digest,cwd=directory,environment={
                    'FIXTURE_LOG':str(log),'FIXTURE_MODE':mode})
            out = transport(req,a.data[req.wire.id],a.data[req.setup.id],threading.Event(),time.monotonic()+deadline)
            turn_log = log.with_suffix('.turn')
            if turn_log.exists():
                # Includes the exact schema bytes, with only threadId appended.
                self.assertTrue(turn_log.read_bytes().endswith(
                    b',"params":' + extend_object(raw, {'threadId': 'thread-1'}) + b'}\n'))
            if mode == 'child':
                pid = int(log.with_suffix('.pid').read_text())
                stat = Path(f'/proc/{pid}/stat')
                self.assertTrue(not stat.exists() or stat.read_text().rsplit(')',1)[1].split()[0] == 'Z')
            return out, log.read_text().splitlines() if log.exists() else []

    def test_one_turn_usage_and_process_termination(self):
        out, calls = self.invoke()
        self.assertIsNone(out.error)
        self.assertEqual(out.raw,b'{"proof":"by trivial"}')
        self.assertEqual(out.usage,Usage(12,3,5,None))
        self.assertEqual(calls.count('turn/start'),1)
        self.assertEqual(out.termination,Termination.TERMINATED)

    def test_role_schemas_reach_native_turn_unchanged(self):
        from midas.prompt import Options, Section, Snapshot, build, prover, reviewer, researcher
        snapshot = Snapshot((Section('subject', 'goal', b'True'),
                             Section('candidate', 'proof', b'by trivial'),
                             Section('question', 'question', b'Find a reference.'),
                             Section('capabilities', 'allowance', b'web_calls=1')))
        for renderer in (prover.render, reviewer.render, researcher.render):
            prepared = build(snapshot, Options('codex', 'model'), renderer=renderer)
            with self.subTest(role=prepared.role):
                out, calls = self.invoke(prepared=prepared, web_calls=int(prepared.role == 'researcher'))
                self.assertIsNone(out.error)
                self.assertEqual(calls.count('turn/start'), 1)

    def test_native_schema_rejection_is_reported_without_retry(self):
        out, calls = self.invoke('schema-rejected')
        self.assertEqual(out.error, 'configuration')
        self.assertEqual(calls.count('turn/start'), 1)
        self.assertEqual(out.termination, Termination.TERMINATED)

    def test_detached_descendant_is_terminated(self):
        out,_ = self.invoke('child')
        self.assertIsNone(out.error)
        self.assertEqual(out.termination,Termination.TERMINATED)

    def test_stderr_overflow_interrupts_silent_stdout(self):
        out,_ = self.invoke('stderr')
        self.assertEqual(out.error,'output_limit')
        self.assertEqual(out.termination,Termination.TERMINATED)

    def test_retry_controls_rejected_before_inference(self):
        for limits in (LIMITS, replace(LIMITS, request_max_retries=3, stream_max_retries=1)):
            for mode in ('retry', 'stream_retry', 'missing_retry', 'invalid_retry'):
                with self.subTest(limits=limits, mode=mode):
                    out,calls = self.invoke(mode, limits=limits)
                    self.assertEqual((out.error,out.delivery),('unsupported_retry_control',Delivery.NOT_SENT))
                    self.assertNotIn('turn/start',calls)
                    self.assertEqual(out.termination,Termination.TERMINATED)

    def test_policy_retry_limits_applied_to_each_codex_provider(self):
        from midas.policy import load_config
        config = load_config(b'{"format":"midas-config-v1","model":{"request_max_retries":3,"stream_max_retries":1}}')
        limits = Limits.from_config(config.model)
        for provider in ('openai', 'azure', 'amazon-bedrock-runtime'):
            with self.subTest(provider=provider):
                out, calls = self.invoke(provider=provider, limits=limits)
                self.assertIsNone(out.error)
                self.assertEqual(calls.count('turn/start'), 1)
                self.assertEqual(out.termination, Termination.TERMINATED)

    def test_executable_mismatch_never_starts(self):
        out,calls = self.invoke(digest='0'*64)
        self.assertEqual((out.error,out.delivery),('configuration',Delivery.NOT_SENT))
        self.assertEqual(calls,[])

    def test_timeout_keeps_uncertainty_and_reaps_process(self):
        out,calls = self.invoke('hang',deadline=.15)
        self.assertEqual(out.error,'timeout')
        self.assertEqual(out.delivery,Delivery.UNKNOWN)
        self.assertEqual(out.termination,Termination.TERMINATED)
        self.assertEqual(calls.count('turn/start'),1)

    def test_tools_rejected_and_research_web_evidence_retained(self):
        out,_ = self.invoke('tool')
        self.assertEqual(out.error,'configuration')
        out,_ = self.invoke('web',web_calls=1)
        self.assertIsNone(out.error)
        self.assertIn(b'query',out.events)
        out,_ = self.invoke('web')
        self.assertEqual(out.error,'configuration')


if __name__ == '__main__':
    unittest.main()
