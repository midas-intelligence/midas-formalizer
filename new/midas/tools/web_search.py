"""Validate structured web search and recorded web actions; all output is advisory."""
from dataclasses import dataclass

from ..domain import ResultStatus
from ..domain import encoding as codec
from ..domain.wire import decode_object
from .protocol import WebSearch, Stage, pack, public_url, read, text

WEB_SEARCH_BYTES = 131072
EVENT_BYTES = 4 * 1024**2


@dataclass(frozen=True, slots=True)
class WebAction:
    id: str
    action: str


def web_actions(events, *, limit):
    """Read provider's bounded binary event frames, retaining no document bodies."""
    if type(events) is not bytes or len(events) > EVENT_BYTES:
        raise ValueError('Web-search events exceed allowance')
    position, actions = 0, {}
    while position < len(events):
        if len(events)-position < 8:
            raise ValueError('Truncated web-search event length')
        size = int.from_bytes(events[position:position+8], 'little')
        position += 8
        if size > len(events)-position:
            raise ValueError('Truncated web-search event')
        event = codec.load_record(events[position:position+size], limit=EVENT_BYTES)
        position += size
        if type(event) is not dict or type(event.get('type')) is not str:
            raise ValueError('Invalid web-search event')
        if event['type'] != 'webSearch':
            continue
        codec.fields(event, ('type', 'id', 'action'))
        text(event['id'], 'web action ID', 1024)
        raw = event['action']
        if type(raw) is not dict:
            raise ValueError('Invalid web-search action')
        kind = raw.get('type')
        if kind not in ('search', 'openPage', 'findInPage', 'other'):
            kind = 'unknown'
        action = WebAction(event['id'], kind)
        if action.id in actions and actions[action.id] != action:
            raise ValueError('Conflicting repeated web action')
        actions[action.id] = action
        if len(actions) > limit:
            raise ValueError('Web search exceeded frozen web-call limit')
    return tuple(actions.values())


def web_search_evidence(stage, raw, events):
    """Called only after provider receipt admission; returns flat binary evidence."""
    if not isinstance(stage, Stage) or not isinstance(stage.action, WebSearch):
        raise ValueError('Expected web-search stage')
    value = decode_object(raw, limit=WEB_SEARCH_BYTES)
    codec.fields(value, ('answer', 'sources', 'unresolved'))
    text(value['answer'], 'web-search answer', 32000)
    if type(value['sources']) is not list or len(value['sources']) > 6:
        raise ValueError('At most six web-search sources are allowed')
    if type(value['unresolved']) is not list or len(value['unresolved']) > 32:
        raise ValueError('Invalid unresolved web-search questions')
    for question in value['unresolved']:
        text(question, 'unresolved question', 8000)
    actions = web_actions(events, limit=stage.action.web_calls)
    quoted, sources = {}, []
    for ordinal, source in enumerate(value['sources']):
        codec.fields(source, ('url', 'title', 'location', 'excerpt', 'summary', 'hypotheses'))
        url = public_url(source['url'])
        for key in ('title', 'location', 'summary', 'hypotheses'):
            text(source[key], 'web-search '+key, 8000)
        text(source['excerpt'], 'web-search excerpt', 8000, empty=True)
        quoted[url] = quoted.get(url, 0) + len(source['excerpt'].split())
        if quoted[url] > 25:
            raise ValueError('Web search exceeds per-source quote allowance')
        sources.append(dict(source, id=f'{stage.helper_id}S{ordinal+1}'))
    return pack({'format': 'midas-web-search-evidence-v2', 'stage': stage.sha256,
        'answer': value['answer'], 'sources': sources, 'unresolved': value['unresolved'],
        'actions': [[a.id, a.action] for a in actions],
        'authority': 'advisory', 'provenance': 'Quotations are provider-reported.'}, WEB_SEARCH_BYTES)


def web_search_summary(payload):
    """Present web-search content before paging; web actions stay in saved evidence."""
    value = codec.load_record(payload, limit=WEB_SEARCH_BYTES)
    if value['format'] != 'midas-web-search-evidence-v2':
        raise ValueError('Expected admitted web-search evidence')
    parts = ['Web-search results (advisory):\n'+value['answer']]
    for ordinal, source in enumerate(value['sources'], 1):
        parts.append('\n'.join((
            f"Source {ordinal}: {source['title']}",
            'URL: '+source['url'],
            'Location: '+source['location'],
            'Excerpt: '+source['excerpt'],
            'Summary: '+source['summary'],
            'Hypotheses: '+source['hypotheses'])))
    if value['unresolved']:
        parts.append('Unresolved questions:\n'+'\n'.join(value['unresolved']))
    return '\n\n'.join(parts)


def admit_web_search(stage, admitted, reader):
    """Interpret an authenticated provider handoff; provider owns its receipt seal.

    Controller must bind the admitted model request to this planned stage before
    calling, just as it binds a prepared checker request to a lookup/probe stage.
    """
    if admitted.status != ResultStatus.SUCCEEDED:
        raise ValueError('Web-search model invocation did not succeed')
    return web_search_evidence(stage, read(reader, admitted.raw, WEB_SEARCH_BYTES),
                             read(reader, admitted.events, EVENT_BYTES))
