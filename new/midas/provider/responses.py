"""Stateless Azure/Bedrock Responses with TLS, no redirects and zero retries."""
import asyncio
import os
from .transport import Collector, TransportError, provider_error, retry_after
from .wire import decode
from ..domain import DeliveryCertainty


class BearerCredentials:
    """A route-bound, ephemeral credential capability; never persisted or logged."""
    def __init__(self, route, secret):
        self.route = route
        if type(secret) is not str or not secret or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError('Missing or invalid bearer credential')
        self._secret = secret

    @classmethod
    def from_environment(cls, route, environ=None):
        values = os.environ if environ is None else environ
        name = route.credential_env or {'azure_responses': 'AZURE_OPENAI_API_KEY',
                                        'bedrock_responses': 'AWS_BEARER_TOKEN_BEDROCK'}.get(route.transport)
        return cls(route, values.get(name, '') if name else '')

    def __call__(self, route):
        if route != self.route:
            raise ValueError('Credential capability belongs to another route')
        return {'Authorization': 'Bearer ' + self._secret, 'Content-Type': 'application/json'}


class ResponsesTransport:
    def __init__(self, credentials, *, transport_factory=None):
        self.credentials, self.transport_factory = credentials, transport_factory

    def __call__(self, request, raw, setup, cancel, deadline):
        if request.route.transport not in ('azure_responses', 'bedrock_responses'):
            raise ValueError('Responses transport mismatch')
        return asyncio.run(self.execute(request, raw, cancel, deadline))

    async def execute(self, request, raw, cancel, deadline):
        c = Collector(request.limits)
        async def invoke():
            try:
                import httpx
                headers = self.credentials(request.route)
                if type(headers) is not dict or not headers:
                    raise ValueError('Missing credentials')
                if any(type(k) is not str or type(v) is not str or '\r' in v or '\n' in v for k, v in headers.items()):
                    raise ValueError('Invalid headers')
                lim = request.limits
                timeout = httpx.Timeout(connect=lim.connect_ms/1000, write=lim.write_ms/1000,
                                        read=lim.read_ms/1000, pool=lim.pool_ms/1000)
                transport = self.transport_factory() if self.transport_factory else httpx.AsyncHTTPTransport(retries=0, verify=True)
            except Exception as exc:
                raise TransportError('configuration') from exc
            async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
                c.value.update(phase='sending', delivery=DeliveryCertainty.UNKNOWN)
                try:
                    async with client.stream('POST', request.route.endpoint, headers=headers, content=raw) as response:
                        c.value.update(phase='reading', delivery=DeliveryCertainty.DELIVERED)
                        c.identifier('request_id', next((response.headers[k] for k in
                            ('x-request-id', 'apim-request-id', 'x-amzn-requestid') if k in response.headers), None))
                        c.value['retry_after_ms'] = retry_after(response.headers)
                        if response.status_code != 200:
                            error, _ = provider_error({'status': response.status_code})
                            raise TransportError(error)
                        await self.stream(response, c)
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    c.value.update(phase='connecting', delivery=DeliveryCertainty.NOT_SENT)
                    raise
        return await c.run(invoke, cancel, deadline)

    @staticmethod
    async def stream(response, c):
        buffer, lines, size, observed = bytearray(), [], 0, 0
        done_texts = _DoneTexts()
        async for chunk in response.aiter_bytes():
            observed += len(chunk)
            if observed > c.limits.event_bytes:
                raise TransportError('output_limit')
            for offset in range(0, len(chunk), 8192):
                buffer.extend(chunk[offset:offset+8192])
                start = 0
                while True:
                    end = buffer.find(b'\n', start)
                    if end < 0:
                        break
                    line = bytes(buffer[start:end]).rstrip(b'\r')
                    size += end - start + 1
                    start = end + 1
                    if size > c.limits.frame_bytes:
                        raise TransportError('output_limit')
                    if line.startswith(b'data:'):
                        lines.append(line[5:].lstrip(b' '))
                    elif not line:
                        if lines:
                            data = b'\n'.join(lines)
                            if data == b'[DONE]':
                                raise EOFError('No terminal response')
                            event = decode(data, limit=c.limits.frame_bytes)
                            if type(event) is not dict:
                                raise TransportError('malformed_output')
                            c.event(event, size)
                            if ResponsesTransport.event(event, c, done_texts):
                                return
                        lines, size = [], 0
                if start:
                    del buffer[:start]
                if len(buffer) + size > c.limits.frame_bytes:
                    raise TransportError('output_limit')
        raise EOFError('Stream ended before terminal response')

    @staticmethod
    def event(event, c, done_texts):
        kind = event.get('type')
        response = event.get('response') or {}
        c.identifier('response_id', response.get('id'))
        if response.get('usage') is not None:
            c.usage(response['usage'])
        if kind == 'response.output_text.delta':
            c.text(event.get('delta', ''), append=True)
        if kind == 'response.output_text.done':
            key = event.get('output_index', 0), event.get('content_index', 0)
            if any(type(n) is not int or n < 0 for n in key) or type(event.get('text')) is not str:
                raise TransportError('malformed_output')
            done_texts.add(key, event['text'], c.limits.output_bytes)
        if kind in ('response.completed', 'response.failed', 'response.incomplete'):
            refusals = [part.get('refusal', '') for item in response.get('output', [])
                        for part in item.get('content', []) if part.get('type') == 'refusal']
            if refusals:
                c.save_event({'type': 'refusal', 'text': ''.join(refusals)})
                c.value['phase'] = 'terminal'
                raise TransportError('refusal')
            terminal = ''.join(part.get('text', '') for item in response.get('output', [])
                               for part in item.get('content', []) if part.get('type') == 'output_text')
            done = ''.join(done_texts[k] for k in sorted(done_texts))
            # Preserve only the narrow, independently corroborated Unicode repair.
            repaired = bool(done and bytes(c.raw) == done.encode() and '\ufffd' in terminal
                            and terminal == ''.join(ch if ord(ch) < 128 else '\ufffd' for ch in done))
            if terminal and not repaired:
                c.text(terminal)
            elif not c.raw and done:
                c.text(done)
            c.value['phase'] = 'terminal'
            if kind != 'response.completed' or response.get('status', 'completed') != 'completed':
                error, delay = provider_error(response.get('error')) if response.get('error') else ('malformed_output', 0)
                c.value['retry_after_ms'] = max(c.value.get('retry_after_ms', 0), delay)
                raise TransportError(error)
            return True
        if kind == 'error':
            error, delay = provider_error(event)
            c.value['retry_after_ms'] = max(c.value.get('retry_after_ms', 0), delay)
            raise TransportError(error)
        return False


class _DoneTexts(dict):
    def __init__(self):
        super().__init__()
        self.byte_count = 0

    def add(self, key, text, maximum):
        self.byte_count += len(text.encode()) - len(self.get(key, '').encode())
        if self.byte_count > maximum:
            raise TransportError('output_limit')
        self[key] = text
