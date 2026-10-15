"""Reserved model execution and authenticated transport receipts; no scheduling."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ..domain import (ArtifactRef, DeliveryCertainty, ResultStatus, Termination, Usage, WorkerResult)
from ..domain import encoding as codec
from ..executor import WorkerStopped
from .protocol import (META_BYTES, WIRE_BYTES, ModelOutput, ModelRequest, pack, read)
from .wire import validate_wire


class ReceiptVerifier(Protocol):
    def verify_receipt(self, receipt, invocation, result) -> bool: ...


def worker_payload(result):
    """Small exact binary result encoding to be saved by controller/storage."""
    return pack({'invocation': result.invocation_id, 'request': result.request_hash,
        'status': result.status.value, 'artifacts': [r.to_dict() for r in result.staged_artifacts],
        'output': result.output.to_dict() if result.output else None,
        'diagnostics': result.diagnostics.to_dict() if result.diagnostics else None,
        'usage': [getattr(result.usage, k) for k in ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')],
        'delivery': result.delivery_certainty.value, 'termination': result.termination.value})


def status(output):
    return (ResultStatus.SUCCEEDED if output.error is None else ResultStatus.CANCELLED
            if output.error == 'cancelled' else ResultStatus.FAILED)


def metadata(output):
    return {'error': output.error, 'phase': output.phase, 'delivery': output.delivery.value,
        'usage': [output.usage.input_tokens, output.usage.cache_tokens, output.usage.output_tokens, output.usage.cost_microusd],
        'cache_write_tokens': output.cache_write_tokens, 'request_id': output.request_id,
        'response_id': output.response_id, 'thread_id': output.thread_id, 'turn_id': output.turn_id,
        'retry_after_ms': output.retry_after_ms, 'diagnostics': list(output.diagnostics),
        'termination': output.termination.value}


@dataclass(frozen=True, slots=True)
class AdmittedOutput:
    """Authenticated model bytes remain advisory; strategy/tools interpret them."""
    raw: ArtifactRef
    events: ArtifactRef
    receipt: object
    outcome: ModelOutput  # Small metadata only; raw/events remain references above.
    status: ResultStatus


class Provider:
    def __init__(self, bindings):
        routes = {}
        for route, transport in bindings:
            if route.scope in routes or not callable(transport):
                raise ValueError('Duplicate route or invalid transport')
            routes[route.scope] = (route, transport)
        self._routes = MappingProxyType(routes)

    def invoke(self, request, context):
        """Exactly one configured transport invocation; never sleeps or retries."""
        try:
            context.check()
            route, transport = self._routes[request.route.scope]
            if route != request.route:
                raise ValueError('Route mismatch')
            raw = read(context.reader, request.wire, WIRE_BYTES)
            setup = read(context.reader, request.setup, WIRE_BYTES) if request.setup else None
            validate_wire(request, raw, setup)
            context.check()
        except WorkerStopped:
            return ModelOutput(error='cancelled' if context.cancellation.is_set() else 'timeout',
                               phase='preparing', delivery=DeliveryCertainty.NOT_SENT)
        except Exception as exc:
            return ModelOutput(error='configuration', phase='preparing', delivery=DeliveryCertainty.NOT_SENT,
                               diagnostics=(type(exc).__name__[:128],))
        try:
            output = transport(request, raw, setup, context.cancellation, context.deadline)
            if not isinstance(output, ModelOutput):
                raise ValueError('Invalid transport output')
            output.validate_limits(request.limits)
            return output
        except Exception as exc:
            # An unclassified adapter failure may have happened after dispatch.
            return ModelOutput(error='crash', phase='sending', delivery=DeliveryCertainty.UNKNOWN,
                               diagnostics=(type(exc).__name__[:128],), termination=Termination.UNKNOWN)

    def execute(self, frozen, context):
        invocation = context.capacity_grant.invocation
        context.capacity_grant.validate_for(invocation)
        if frozen != invocation.request:
            raise ValueError('Provider execution differs from capacity grant')
        request = ModelRequest.load(read(context.reader, frozen.operation_payload, META_BYTES))
        request.validate_for(frozen, context.reader)
        output = self.invoke(request, context)
        raw = context.staging.write_bounded(output.raw, max_bytes=request.limits.output_bytes)
        events = context.staging.write_bounded(output.events, max_bytes=request.limits.event_bytes)
        report = context.staging.write_bounded(pack({'format': 'midas-provider-result-v1',
            'request': frozen.operation_payload.to_dict(), 'raw': raw.to_dict(), 'events': events.to_dict(),
            'outcome': metadata(output)}), max_bytes=META_BYTES)
        return WorkerResult(invocation.original_execution_id, frozen.request_hash, status(output),
            (raw, events, report), report, None, output.usage, output.delivery, output.termination)

    def admit_receipt(self, request, invocation, result, receipt, reader, authority):
        result.validate_for(invocation)
        request.validate_for(invocation.request, reader)
        if (receipt.original_execution_id != invocation.original_execution_id
                or receipt.request_ref != invocation.request.request_ref
                or read(reader, receipt.result_ref, META_BYTES) != worker_payload(result)
                or authority.verify_receipt(receipt, invocation, result) is not True):
            raise ValueError('Unauthenticated provider receipt')
        if result.output is None or result.diagnostics is not None:
            raise ValueError('Missing provider report or unexpected diagnostics artifact')
        report = codec.load_record(read(reader, result.output, META_BYTES), limit=META_BYTES)
        codec.fields(report, ('format', 'request', 'raw', 'events', 'outcome'))
        if (report['format'] != 'midas-provider-result-v1'
                or report['request'] != invocation.request.operation_payload.to_dict()):
            raise ValueError('Provider report differs from saved request')
        raw, events = ArtifactRef.from_dict(report['raw']), ArtifactRef.from_dict(report['events'])
        if result.staged_artifacts != (raw, events, result.output):
            raise ValueError('Provider report has missing or unexpected artifacts')
        m = report['outcome']
        codec.fields(m, metadata(ModelOutput()))
        output = ModelOutput(raw=read(reader, raw, request.limits.output_bytes),
            events=read(reader, events, request.limits.event_bytes),
            **{k: v for k, v in m.items() if k not in ('usage', 'delivery', 'termination', 'diagnostics')},
            usage=Usage(*m['usage']), delivery=DeliveryCertainty(m['delivery']),
            termination=Termination(m['termination']), diagnostics=tuple(m['diagnostics']))
        output.validate_limits(request.limits)
        # Executor may mark a returned result cancelled/failed when stop/deadline
        # wins after the transport returns. Preserve its bytes and known usage,
        # while retaining that execution disposition for downstream acceptance.
        interrupted = (status(output) == ResultStatus.SUCCEEDED
                       and result.status in (ResultStatus.CANCELLED, ResultStatus.FAILED))
        if (status(output) != result.status and not interrupted or output.usage != result.usage
                or output.delivery != result.delivery_certainty or output.termination != result.termination):
            raise ValueError('Provider report differs from worker outcome')
        # Verify every saved event frame before passing its reference to tools.
        position = 0
        while position < len(output.events):
            if len(output.events) - position < 8:
                raise ValueError('Truncated provider event frame')
            size = int.from_bytes(output.events[position:position+8], 'little')
            position += 8
            if size > len(output.events)-position:
                raise ValueError('Truncated provider event')
            event = codec.load_record(output.events[position:position+size], limit=request.limits.event_bytes)
            if type(event) is not dict or type(event.get('type')) is not str:
                raise ValueError('Invalid provider event')
            position += size
        # Retain only small metadata in the admitted value.
        from dataclasses import replace
        return AdmittedOutput(raw, events, receipt, replace(output, raw=b'', events=b''), result.status)
