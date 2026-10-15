"""One ephemeral app-server turn with verified zero-retry controls."""
import asyncio
from collections import deque
from dataclasses import replace

from ..domain import DeliveryCertainty
from ..executor.protocol import ProtocolProcess
from .transport import Collector, TransportError, provider_error
from .wire import decode, extend_object, small_json


class CodexTransport:
    def __init__(self, command, *, executable_sha256, cwd, environment):
        self.command = tuple(command)
        self.executable_sha256, self.cwd = executable_sha256, str(cwd)
        # Application supplies only this route's process configuration/credentials.
        self.environment = dict(environment)

    def __call__(self, request, raw, setup, cancel, deadline):
        if request.route.transport != 'codex':
            raise ValueError('App-server transport mismatch')
        return asyncio.run(self.execute(request, raw, setup, cancel, deadline))

    async def execute(self, request, raw, setup, cancel, deadline):
        c = Collector(request.limits)
        owner = ProtocolProcess(self.command, executable_sha256=self.executable_sha256, cwd=self.cwd,
                                environment=self.environment, frame_bytes=c.limits.frame_bytes,
                                shutdown_ms=c.limits.shutdown_ms)
        stderr_task = None
        startup_error = False
        async def invoke():
            nonlocal stderr_task, startup_error
            provider = 'midas-openai' if request.route.provider == 'openai' else request.route.provider
            args = ['app-server', '-c', 'approval_policy="never"', '-c', 'model_provider=' + small_json(provider).decode()]
            if request.route.provider == 'openai':
                args += ['-c', 'model_providers.midas-openai={name="OpenAI", wire_api="responses", '
                         'requires_openai_auth=true, request_max_retries=0, stream_max_retries=0}']
            else:
                for key in ('request_max_retries', 'stream_max_retries'):
                    args += ['-c', f'model_providers.{provider}.{key}=0']
            for feature in ('shell_tool', 'unified_exec', 'multi_agent', 'apps', 'plugins', 'browser_use',
                    'computer_use', 'image_generation', 'view_image', 'goals', 'sleep_tool', 'hooks', 'skill_search'):
                args += ['-c', f'features.{feature}=false']
            web = c.limits.web_calls > 0
            args += ['-c', 'web_search="live"' if web else 'web_search="disabled"',
                     '-c', f'features.code_mode.enabled={str(web).lower()}',
                     '-c', f'features.code_mode_host={str(web).lower()}']
            if request.route.provider == 'amazon-bedrock-runtime':
                args += ['-c', 'model_reasoning_summary="none"']
            try:
                process = await owner.start(args)
            except Exception as exc:
                raise TransportError('configuration') from exc
            async def drain():
                nonlocal startup_error
                count, tail = 0, b''
                while chunk := await process.stderr.read(8192):
                    count += len(chunk)
                    if count > c.limits.event_bytes:
                        raise TransportError('output_limit')
                    sample = tail + chunk
                    if any(marker in sample for marker in (b'Built-in providers cannot be overridden',
                            b'Error loading configuration', b'Error parsing configuration', b'failed to load configuration')):
                        startup_error = True
                    tail = sample[-128:]
            stderr_task = asyncio.create_task(drain())
            seq, pending = 0, deque()
            received_bytes = 0
            async def send(method, params, ident=None):
                prefix = small_json({'method': method, **({'id': ident} if ident is not None else {})})
                process.stdin.write(prefix[:-1] + b',"params":' + params + b'}\n')
                await process.stdin.drain()
            async def receive():
                nonlocal received_bytes
                if stderr_task.done():
                    await stderr_task
                    line = await process.stdout.readline()
                else:
                    # A noisy stderr must interrupt a silent stdout promptly.
                    reading = asyncio.create_task(process.stdout.readline())
                    try:
                        done, _ = await asyncio.wait((reading, stderr_task), return_when=asyncio.FIRST_COMPLETED)
                        if stderr_task in done:
                            await stderr_task
                        line = await reading
                    finally:
                        if not reading.done():
                            reading.cancel()
                        await asyncio.gather(reading, return_exceptions=True)
                if not line:
                    await stderr_task
                    if startup_error and c.value['delivery'] == DeliveryCertainty.NOT_SENT:
                        raise TransportError('configuration')
                    raise EOFError('App-server closed')
                received_bytes += len(line)
                if len(line) > c.limits.frame_bytes or received_bytes > c.limits.event_bytes:
                    raise TransportError('output_limit')
                msg = decode(line, limit=c.limits.frame_bytes)
                if type(msg) is not dict:
                    raise TransportError('malformed_output')
                c.event(msg, len(line))
                if 'method' in msg and 'id' in msg:
                    raise TransportError('configuration')
                params = msg.get('params') or {}
                if (msg.get('method') == 'thread/tokenUsage/updated' and c.value.get('thread_id') is not None
                        and params.get('threadId') == c.value['thread_id']):
                    c.usage(params.get('tokenUsage', {}).get('total'))
                return msg
            async def rpc(method, params):
                nonlocal seq
                seq += 1
                await send(method, params, seq)
                while True:
                    msg = await receive()
                    if msg.get('id') == seq and 'method' not in msg:
                        if 'error' in msg:
                            if method == 'turn/start':
                                c.value['delivery'] = DeliveryCertainty.DELIVERED
                            error, delay = provider_error(msg['error'])
                            c.value['retry_after_ms'] = delay
                            raise TransportError(error)
                        result = msg.get('result')
                        if type(result) is not dict:
                            raise TransportError('malformed_output')
                        return result
                    if method == 'turn/start':
                        pending.append(msg)
            async with asyncio.timeout(c.limits.startup_ms / 1000):
                await rpc('initialize', small_json({'clientInfo': {'name': 'midas', 'version': '1'},
                                                    'capabilities': {'experimentalApi': True}}))
                await send('initialized', b'{}')
                config = await rpc('config/read', small_json({'includeLayers': False}))
                effective = config.get('config', {}).get('model_providers', {}).get(provider, {})
                if any(type(effective.get(k)) is not int or effective[k] != 0
                       for k in ('request_max_retries', 'stream_max_retries')):
                    raise TransportError('unsupported_retry_control')
                if request.route.provider == 'openai':
                    if effective.get('requires_openai_auth') is not True or effective.get('wire_api') != 'responses':
                        raise TransportError('configuration')
                    account = await rpc('account/read', small_json({'refreshToken': False}))
                    if account.get('requiresOpenaiAuth') and not account.get('account'):
                        raise TransportError('authentication')
                started = await rpc('thread/start', extend_object(setup, {'model': request.route.model,
                    'modelProvider': provider, 'cwd': self.cwd, 'ephemeral': True, 'approvalPolicy': 'never',
                    'sandbox': 'read-only', 'developerInstructions':
                        'Only web search/open/find are allowed.' if web else 'No tools are allowed.'}))
                thread = started.get('thread', {})
                if started.get('modelProvider', thread.get('modelProvider')) != provider:
                    raise TransportError('configuration')
                c.identifier('thread_id', thread['id'])
            c.value.update(phase='sending', delivery=DeliveryCertainty.UNKNOWN)
            turn = await rpc('turn/start', extend_object(raw, {'threadId': c.value['thread_id']}))
            c.identifier('turn_id', turn['turn']['id'])
            c.value.update(phase='reading', delivery=DeliveryCertainty.DELIVERED)
            web_ids = set()
            while True:
                msg = pending.popleft() if pending else await receive()
                method, params = msg.get('method'), msg.get('params') or {}
                if params.get('threadId', c.value['thread_id']) != c.value['thread_id']:
                    continue
                if params.get('turnId', c.value['turn_id']) != c.value['turn_id']:
                    continue
                item = params.get('item') or {}
                if method == 'item/agentMessage/delta':
                    c.text(params.get('delta', ''), append=True)
                if method in ('item/started', 'item/completed'):
                    kind = item.get('type')
                    if kind == 'webSearch' and web:
                        web_ids.add(item['id'])
                        if len(web_ids) > c.limits.web_calls:
                            raise TransportError('output_limit')
                        if method == 'item/completed':
                            # Preserve advisory action data; tools owns interpretation.
                            action = item.get('action') or {}
                            c.save_event({'type': 'webSearch', 'id': item['id'], 'action': action})
                    elif kind in ('commandExecution', 'fileChange', 'mcpToolCall', 'webSearch',
                            'collabAgentToolCall', 'collabToolCall', 'dynamicToolCall', 'imageView', 'imageGeneration'):
                        raise TransportError('configuration')
                    if method == 'item/completed' and kind == 'agentMessage':
                        c.text(item.get('text', ''))
                if method == 'turn/completed' and params.get('turn', {}).get('id') == c.value['turn_id']:
                    final = params['turn']
                    if final.get('status') != 'completed':
                        kind, delay = provider_error(final.get('error'))
                        c.value['retry_after_ms'] = delay
                        raise TransportError(kind)
                    await rpc('thread/read', small_json({'threadId': c.value['thread_id'], 'includeTurns': False}))
                    c.value['phase'] = 'terminal'
                    return
        try:
            result = await c.run(invoke, cancel, deadline)
        finally:
            await owner.close()
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
        return replace(result, termination=owner.termination)
