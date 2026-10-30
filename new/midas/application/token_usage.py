"""Small, persisted input-size samples; never decode historical model prompts."""
from ..controller import Effect
from ..domain import ResultStatus
from . import records as r


def sample(reader, owner, role, route):
    # Web search reports cumulative agent usage, not the initial prompt's tokens.
    if role == 'web_search':
        return None
    value = r.value(reader, 'application.prompt_usage', owner.obligation_id+':'+role)
    return tuple(value[1:]) if value and value[0] == route.scope else None


def prepare(reader, key, owner, role, route, prompt):
    if role == 'web_search':
        return Effect()
    return r.record(reader, 'application.prompt_inputs', key,
                    [owner.obligation_id+':'+role, route.scope, prompt.input_bytes])


def observe(reader, invocation, result):
    tokens = result.usage.input_tokens
    if result.status != ResultStatus.SUCCEEDED or tokens is None or tokens <= 0:
        return Effect()
    saved = r.value(reader, 'application.prompt_inputs', invocation.request.job.semantic_key)
    if not saved:
        return Effect()
    key, scope, size = saved
    # Also ignore web-search input records prepared before this exclusion.
    if key.endswith(':web_search'):
        return Effect()
    value = [scope, size, tokens]
    if r.value(reader, 'application.prompt_usage', key) == value:
        return Effect()
    return r.record(reader, 'application.prompt_usage', key, value)
