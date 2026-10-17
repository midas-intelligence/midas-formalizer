"""Concrete route construction and saved scripted responses, without credentials in records."""
from dataclasses import replace

from ..controller import Effect
from ..domain import Usage
from ..policy import cost_microusd
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


def _priced(transport, prices):
    """Apply frozen role prices before the provider seals the result and usage."""
    if not callable(transport):
        raise ValueError('Invalid transport')
    def invoke(request, raw, setup, cancel, deadline):
        output = transport(request, raw, setup, cancel, deadline)
        if isinstance(output, ModelOutput) and output.usage.cost_microusd is None:
            cost = cost_microusd(prices.get(request.role), output.usage,
                                cache_write_tokens=output.cache_write_tokens)
            if cost is not None:
                output = replace(output, usage=replace(output.usage, cost_microusd=cost))
        return output
    return invoke


def construct(config, *, transports=None, script=None, codex=None, roles=('prover', 'reviewer', 'research')):
    prices = {role: getattr(config, role).prices for role in roles}
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
                if codex is None:
                    raise ValueError('Supply --codex or a pinned CodexTransport for route '+route.scope)
                transport = codex_transport(*codex)
            else:
                transport = ResponsesTransport(BearerCredentials.from_environment(route))
        # Several roles can share a transport while retaining different prices.
        bindings[route.scope] = route, _priced(transport, prices)
    return Provider(tuple(bindings.values()))


def codex_transport(executable, environment_names):
    from hashlib import file_digest
    from pathlib import Path
    import os
    from ..provider import CodexTransport
    executable = Path(executable).resolve(strict=True)
    with executable.open('rb') as source:
        digest = file_digest(source, 'sha256').hexdigest()
    names = ('PATH', 'HOME', 'CODEX_HOME', *environment_names)
    return CodexTransport((str(executable),), executable_sha256=digest, cwd=Path.cwd(),
                          environment={key: os.environ[key] for key in names if key in os.environ})
