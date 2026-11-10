"""One-call transport state, bounded accumulation and conservative delivery reports."""
import asyncio
from email.utils import parsedate_to_datetime
import ssl
import time

from ..domain import DeliveryCertainty, Usage
from ..domain import encoding as codec
from .protocol import ModelOutput, bounded_text


class TransportError(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def classify(exc):
    pending, seen = [exc], set()
    while pending and len(seen) < 32:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, ssl.SSLCertVerificationError):
            return 'certificate'
        pending.extend(x for x in (item.__cause__, item.__context__) if x is not None)
    if isinstance(exc, TransportError):
        return exc.kind
    if isinstance(exc, asyncio.CancelledError):
        return 'cancelled'
    if isinstance(exc, TimeoutError) or 'Timeout' in type(exc).__name__:
        return 'timeout'
    if isinstance(exc, (ValueError, UnicodeError, KeyError, TypeError, AttributeError)):
        return 'malformed_output'
    if isinstance(exc, (OSError, EOFError)):
        return 'transient'
    try:
        import httpx
        if isinstance(exc, httpx.TransportError):
            return 'transient'
    except ImportError:
        pass
    return 'crash'


def retry_after(headers, now=None):
    try:
        if 'retry-after-ms' in headers:
            n = int(float(headers['retry-after-ms']))
        else:
            value = headers.get('retry-after', '0')
            try:
                n = int(float(value) * 1000)
            except ValueError:
                n = int((parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)) * 1000)
        return min(2**63-1, max(0, n))
    except (TypeError, ValueError, OverflowError):
        return 0


def provider_error(payload):
    pending, statuses, codes, delay = [payload], set(), set(), 0
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in ('httpStatusCode', 'statusCode', 'status') and type(value) is int:
                    statuses.add(value)
                if key in ('code', 'type', 'codexErrorInfo') and type(value) is str:
                    codes.add(value.lower())
                if key in ('retry_after_seconds', 'retry-after', 'retry-after-ms') and type(value) in (str, int, float):
                    delay = max(delay, retry_after({'retry-after-ms' if key.endswith('-ms') else 'retry-after': value}))
                if isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
    for kind, known, status in (
        ('certificate', {'certificate_verify_failed', 'invalid_certificate', 'certificate_error'}, set()),
        ('authentication', {'authentication_error', 'unauthorized', 'invalid_api_key'}, {401, 403}),
        ('rate_limit', {'rate_limit', 'rate_limit_exceeded', 'ratelimit'}, {429}),
        ('timeout', {'timeout', 'request_timeout'}, {408}),
        ('service_unavailable', {'service_unavailable'}, {503}),
        ('transient', {'server_error', 'connection_reset', 'stream_error', 'ssl_eof'}, {409, 425})):
        if codes & known or statuses & status:
            return kind, delay
    return ('transient' if any(n >= 500 for n in statuses) else 'configuration'), delay


def normalize_usage(value):
    def number(*names):
        for name in names:
            if name in value:
                x = value[name]
                return x if type(x) is int and 0 <= x < 2**63 else None
        return None
    if not isinstance(value, dict):
        return Usage(), None, None
    details = value.get('input_tokens_details')
    details = details if isinstance(details, dict) else {}
    merged = dict(value)
    merged.update({k: v for k, v in details.items() if k in ('cached_tokens', 'cache_write_tokens')})
    output_details = value.get('output_tokens_details')
    if isinstance(output_details, dict) and 'reasoning_tokens' in output_details:
        merged['reasoning_tokens'] = output_details['reasoning_tokens']
    value = merged
    usage = Usage(number('input_tokens', 'inputTokens'),
                  number('cached_tokens', 'cachedInputTokens', 'cacheReadInputTokens'),
                  number('output_tokens', 'outputTokens'), number('cost_microusd'))
    return (usage, number('cache_write_tokens', 'cacheWriteInputTokens', 'cacheWriteTokens'),
            number('reasoning_tokens', 'reasoningOutputTokens'))


class Collector:
    def __init__(self, limits):
        self.limits = limits
        self.raw = bytearray()
        self.events = bytearray()
        self.observed = 0
        self.value = dict(error=None, phase='preparing', delivery=DeliveryCertainty.NOT_SENT)

    def identifier(self, name, value):
        if value is not None:
            bounded_text(value, name, 1024)
            self.value[name] = value

    def event(self, event, byte_count):
        self.observed += byte_count
        if byte_count > self.limits.frame_bytes or self.observed > self.limits.event_bytes:
            raise TransportError('output_limit')
        kind = event.get('type', event.get('method', 'rpc'))
        if 'type' not in event and isinstance(event.get('error'), dict):
            error, _ = provider_error(event['error'])
            self.save_event({'type': 'error', 'error': error})
            return
        if type(kind) is not str:
            raise TransportError('malformed_output')
        if kind not in ('response.output_text.delta', 'item/agentMessage/delta'):
            self.save_event({'type': kind[:128]})

    def save_event(self, value):
        remaining = self.limits.event_bytes - len(self.events)
        try:
            data = b''.join(codec.record_parts(value, limit=max(1, remaining - 8)))
        except ValueError as exc:
            raise TransportError('output_limit') from exc
        if len(data) + 8 > remaining:
            raise TransportError('output_limit')
        self.events.extend(len(data).to_bytes(8, 'little'))
        self.events.extend(data)

    def text(self, value, *, append=False):
        if type(value) is not str:
            raise TransportError('malformed_output')
        if not append:
            self.raw.clear()
        data = value.encode('utf-8')
        remaining = self.limits.output_bytes - len(self.raw)
        self.raw.extend(data[:remaining])
        if len(data) > remaining:
            # Remove only a possibly split final codepoint.
            self.raw[:] = self.raw.decode('utf-8', errors='ignore').encode()
            raise TransportError('output_limit')

    def usage(self, value):
        self.value['usage'], self.value['cache_write_tokens'], self.value['reasoning_tokens'] = normalize_usage(value)

    async def run(self, operation, cancel, deadline):
        if cancel.is_set():
            self.value['error'] = 'cancelled'
            return self.finish()
        timeout = min(self.limits.invocation_ms / 1000, deadline - time.monotonic())
        if timeout <= 0:
            self.value['error'] = 'timeout'
            return self.finish()
        async def watch():
            while not cancel.is_set():
                await asyncio.sleep(0.01)
            raise TransportError('cancelled')
        task, watcher = asyncio.create_task(operation()), asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait((task, watcher), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            elif watcher in done:
                await watcher
            else:
                raise TimeoutError()
        except (Exception, asyncio.CancelledError) as exc:
            self.value['error'] = classify(exc)
            # Exception strings can include credentials, bodies, and URLs.
            self.value['diagnostics'] = (type(exc).__name__[:128],)
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            # Do not pretend an uncooperative transport has terminated.
            await asyncio.gather(task, watcher, return_exceptions=True)
        return self.finish()

    def finish(self):
        if self.value.get('error') is None and self.value['phase'] != 'terminal':
            self.value['error'] = 'malformed_output'
        return ModelOutput(raw=bytes(self.raw), events=bytes(self.events), **self.value)


class ScriptedTransport:
    """Deterministic output selected by exact wire hash; no mutable cursor or retries."""
    def __init__(self, outputs):
        self.outputs = dict(outputs)

    def __call__(self, request, raw, setup, cancel, deadline):
        if cancel.is_set():
            return ModelOutput(error='cancelled', phase='preparing', delivery=DeliveryCertainty.NOT_SENT)
        if time.monotonic() >= deadline:
            return ModelOutput(error='timeout', phase='preparing', delivery=DeliveryCertainty.NOT_SENT)
        output = self.outputs[request.wire.sha256]
        output.validate_limits(request.limits)
        return output
