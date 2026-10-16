"""Concrete route construction and saved scripted responses, without credentials in records."""
from ..controller import Effect
from ..domain import Usage
from ..provider import (BearerCredentials, ModelOutput, Provider, ResponsesTransport, Route, ScriptedTransport)
from ..storage import Changes, CounterChange
from . import records as r


class Script:
    """Consume one saved response per turn reference; retries reuse that binding."""
    def __init__(self, reader):
        self.bindings = {}
        after = ''
        while page := reader.records('application.script_bindings', after=after, limit=1000):
            for saved in page:
                reference = r.unref(r.value(reader, saved.scope, saved.key))
                self.bindings[saved.key] = reference, reader.artifact_reader((reference,))
            after = page[-1].key

    def bind(self, reader, request):
        if reader.record('application.script_bindings', request.wire.id) is not None:
            return Effect()
        index = reader.counter('application.script', request.role)
        saved = reader.record('application.script:'+request.role, str(index))
        if saved is None:
            raise ValueError('Script exhausted for '+request.role)
        reference = saved.artifact
        self.bindings[request.wire.id] = reference, reader.artifact_reader((reference,))
        return r.merge(r.record(reader, 'application.script_bindings', request.wire.id, r.ref(reference)),
            Effect(Changes(counters=(CounterChange('application.script', request.role, index, index+1),))))

    def __call__(self, request, raw, setup, cancel, deadline):
        reference, reader = self.bindings[request.wire.id]
        output = ModelOutput(raw=r.read(reader, reference, request.limits.output_bytes), usage=Usage(0, 0, 0, 0))
        return ScriptedTransport({request.wire.sha256: output})(request, raw, setup, cancel, deadline)


def construct(config, *, transports=None, script=None, roles=('prover', 'reviewer', 'research')):
    bindings = {}
    for role in roles:
        setting = getattr(config, role)
        if not setting.configured:
            continue
        route = Route.from_config(setting)
        if role == 'research' and route.transport != 'codex':
            raise ValueError('Research requires the app-server transport')
        if route.scope in bindings:
            continue
        transport = script or (transports or {}).get(route.scope)
        if transport is None:
            if route.transport == 'codex':
                raise ValueError('Supply a pinned CodexTransport for route '+route.scope)
            transport = ResponsesTransport(BearerCredentials.from_environment(route))
        bindings[route.scope] = route, transport
    return Provider(tuple(bindings.values()))
