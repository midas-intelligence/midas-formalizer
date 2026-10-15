"""Bounded protocol parsing without whole-document JSON decoding/serialization.

The prompt owner produces exact wire bytes. This parser validates their protocol
shape and incoming frames. Strings are scanned in C and unescaped in bounded
pieces; large plain text never passes through a JSON library.
"""
import re
from ..domain import DomainError
from ..domain.wire import decode

def small_json(value):
    """JSON is permitted only for fixed-size transport control values."""
    import json
    from ..domain.encoding import record_size
    record_size(value, limit=16 * 1024)
    payload = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    if len(payload) > 16 * 1024:
        raise DomainError('Control message exceeds byte limit')
    return payload


def extend_object(raw, fields):
    """Append small invocation controls to previously validated exact params bytes."""
    suffix = small_json(fields)
    end = len(raw.rstrip()) - 1
    return raw[:end] + (b',' if raw[1:end].strip() else b'') + suffix[1:] + raw[end+1:]


def validate_wire(request, raw, setup=None):
    from .protocol import WIRE_BYTES
    w = decode(raw, limit=WIRE_BYTES)
    if not isinstance(w, dict):
        raise DomainError('Expected wire object')
    route = request.route
    if route.transport == 'codex':
        if set(w) != {'input'} or type(w['input']) is not list or not w['input']:
            raise DomainError('Expected frozen turn input')
        for block in w['input']:
            if type(block) is not dict or set(block) != {'type', 'text'} or block['type'] != 'text' or type(block['text']) is not str:
                raise DomainError('Only text turn inputs are supported')
        config = decode(setup, limit=WIRE_BYTES)
        if type(config) is not dict or set(config) != {'baseInstructions'} or type(config['baseInstructions']) is not str:
            raise DomainError('Expected complete frozen base instructions')
    else:
        allowed = {'model', 'input', 'stream', 'store', 'reasoning', 'max_output_tokens', 'text',
                   'prompt_cache_key', 'prompt_cache_options'}
        if (set(w) - allowed or w.get('model') != route.model or w.get('stream') is not True
                or w.get('store') is not False or type(w.get('input')) is not list or not w['input']):
            raise DomainError('Expected stateless streaming Responses request for saved model')
        text_config = w.get('text')
        if type(text_config) is not dict or set(text_config) != {'format'}:
            raise DomainError('Responses requires an explicit JSON output format')
        output_format = text_config['format']
        if type(output_format) is not dict:
            raise DomainError('Invalid Responses output format')
        if output_format.get('type') == 'json_object':
            if set(output_format) != {'type'}:
                raise DomainError('Invalid JSON object output format')
        elif output_format.get('type') == 'json_schema':
            if (set(output_format) - {'type', 'name', 'schema', 'strict', 'description'}
                    or output_format.get('strict') is not True
                    or type(output_format.get('schema')) is not dict
                    or type(output_format.get('name')) is not str
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', output_format['name']) is None
                    or 'description' in output_format and type(output_format['description']) is not str):
                raise DomainError('Expected a named strict JSON schema from prompt')
        else:
            raise DomainError('Responses output must use json_schema or json_object')
        for message in w['input']:
            if (type(message) is not dict or set(message) != {'role', 'content'}
                    or message['role'] not in ('system', 'developer', 'user', 'assistant')):
                raise DomainError('Unsupported Responses input message')
            content = message['content']
            if type(content) is not str:
                if type(content) is not list or not content:
                    raise DomainError('Invalid message content')
                for part in content:
                    if (type(part) is not dict or set(part) != {'type', 'text'}
                            or part['type'] not in ('input_text', 'output_text') or type(part['text']) is not str):
                        raise DomainError('Only text content is supported')
        if 'max_output_tokens' in w and (type(w['max_output_tokens']) is not int or w['max_output_tokens'] <= 0):
            raise DomainError('Invalid output token bound')
        if route.prompt_cache == 'disabled' and any(k in w for k in ('prompt_cache_key', 'prompt_cache_options')):
            raise DomainError('Cache controls supplied for disabled cache')
        if 'reasoning' in w and (type(w['reasoning']) is not dict or set(w['reasoning']) - {'effort', 'summary'}
                or any(type(v) is not str or len(v) > 128 for v in w['reasoning'].values())):
            raise DomainError('Invalid reasoning controls')
        for k in ('prompt_cache_key', 'prompt_cache_options'):
            if k in w:
                small_json({k: w[k]})
