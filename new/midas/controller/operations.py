"""The existing provider/checker/search boundaries in the common receipt loop."""
from ..domain import ResultStatus
from ..domain.encoding import load_record
from ..policy import Failure
from ..provider import ModelRequest
from ..checker import CompilerRequest, admit_probe_progress
from ..tools import Stage
from .protocol import Accepted, Operation


def model_operation(provider, reduce):
    def admit(invocation, result, receipt, reader, authority):
        request = ModelRequest.load(reader.read_artifact(invocation.request.operation_payload, max_bytes=65536))
        if result.output is None:
            failure = Failure.CANCELLED if result.status == ResultStatus.CANCELLED else Failure.TRANSPORT
            return Accepted(failure=failure)
        value = provider.admit_receipt(request, invocation, result, receipt, reader, authority)
        failure = None
        if value.status != ResultStatus.SUCCEEDED:
            failure = Failure.CANCELLED if value.status == ResultStatus.CANCELLED else {
                'transient': Failure.TRANSPORT, 'crash': Failure.TRANSPORT, 'rate_limit': Failure.RATE_LIMIT,
                'timeout': Failure.TIMEOUT, 'malformed_output': Failure.PROTOCOL, 'output_limit': Failure.PROTOCOL,
            }.get(value.outcome.error, Failure.PERMANENT)
        return Accepted(value, failure, value.outcome.retry_after_ms or 0,
                        request.route.provider, value.outcome.request_id)
    def route(frozen, reader):
        return ModelRequest.load(reader.read_artifact(frozen.operation_payload, max_bytes=65536)).route.scope
    return Operation(provider.execute, admit, reduce, route=route)


def checker_operation(checker, reduce, *, candidate=None):
    def request(invocation, reader):
        return CompilerRequest(reader.read_artifact(invocation.request.operation_payload, max_bytes=8*1024**2))

    def admit(invocation, result, receipt, reader, authority):
        exact = request(invocation, reader)
        exact.validate_for(invocation.request, reader)
        if result.status != ResultStatus.SUCCEEDED:
            failure = Failure.CANCELLED if result.status == ResultStatus.CANCELLED else Failure.CRASH
            if result.output is not None:
                report = load_record(reader.read_artifact(result.output, max_bytes=65536), limit=65536)
                if report.get('request') != invocation.request.operation_payload.to_dict():
                    raise ValueError('Failed compiler report differs from request')
                failure = (Failure.CANCELLED if result.status == ResultStatus.CANCELLED else
                    {'invalid_candidate': Failure.REJECTION, 'resource_limit': Failure.TIMEOUT,
                     'setup_failure': Failure.PERMANENT}.get(report.get('outcome'), Failure.CRASH))
            return Accepted(failure=failure)
        value = checker.admit_receipt(exact, invocation, result, receipt, reader, authority,
            candidate=candidate(reader, invocation) if candidate else None)
        return Accepted(value)

    def progress(invocation, message, reader):
        return admit_probe_progress(request(invocation, reader), invocation, message, reader)

    def retained(frozen):
        return checker.runtime.id if checker.runtime is not None and checker.retained else None

    return Operation(checker.execute, admit, reduce, progress, retained)


def search_operation(search, reduce):
    def admit(invocation, result, receipt, reader, authority):
        stage = Stage.load(reader.read_artifact(invocation.request.operation_payload, max_bytes=65536))
        value = search.admit_receipt(stage, invocation, result, receipt, reader, authority)
        failure = None if result.status == ResultStatus.SUCCEEDED else (
            Failure.CANCELLED if result.status == ResultStatus.CANCELLED else Failure.CRASH)
        return Accepted(value, failure)
    return Operation(search.execute, admit, reduce)
