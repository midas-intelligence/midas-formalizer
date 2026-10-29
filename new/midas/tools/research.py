"""Validate structured research and recorded web actions; all output is advisory."""
from dataclasses import dataclass
import re

from ..domain import ResultStatus
from ..domain import encoding as codec
from ..domain.wire import decode_object
from .protocol import Research, Stage, pack, public_url, read, text

RESEARCH_BYTES = 131072
EVENT_BYTES = 4 * 1024**2


@dataclass(frozen=True, slots=True)
class WebAction:
    id: str
    action: str
    viewed_urls: tuple[str, ...]


def web_actions(events, *, limit):
    """Read provider's bounded binary event frames, retaining no document bodies."""
    if type(events) is not bytes or len(events) > EVENT_BYTES:
        raise ValueError('Research events exceed allowance')
    position, actions = 0, {}
    while position < len(events):
        if len(events)-position < 8:
            raise ValueError('Truncated research event length')
        size = int.from_bytes(events[position:position+8], 'little')
        position += 8
        if size > len(events)-position:
            raise ValueError('Truncated research event')
        event = codec.load_record(events[position:position+size], limit=EVENT_BYTES)
        position += size
        if type(event) is not dict or type(event.get('type')) is not str:
            raise ValueError('Invalid research event')
        if event['type'] != 'webSearch':
            continue
        codec.fields(event, ('type', 'id', 'action'))
        text(event['id'], 'web action ID', 1024)
        raw = event['action']
        if type(raw) is not dict:
            raise ValueError('Invalid research web action')
        kind = raw.get('type')
        if kind not in ('search', 'openPage', 'findInPage', 'other'):
            kind = 'unknown'
        viewed = []
        results = raw.get('results', [])
        if type(results) is not list or len(results) > 64:
            raise ValueError('Invalid research action results')
        for row in results:
            if (type(row) is not dict or row.get('type') != 'text_result' or type(row.get('ref_id')) is not str
                    or not re.fullmatch(r'turn\d+view\d+', row['ref_id'])):
                continue
            url = public_url(row.get('url'))
            if url not in viewed:
                viewed.append(url)
        action = WebAction(event['id'], kind, tuple(viewed))
        if action.id in actions and actions[action.id] != action:
            raise ValueError('Conflicting repeated web action')
        actions[action.id] = action
        if len(actions) > limit:
            raise ValueError('Research exceeded frozen web-call limit')
    if not actions:
        raise ValueError('Research performed no recorded web action')
    return tuple(actions.values())


def research_evidence(stage, raw, events):
    """Called only after provider receipt admission; returns flat binary evidence."""
    if not isinstance(stage, Stage) or not isinstance(stage.action, Research):
        raise ValueError('Expected research stage')
    value = decode_object(raw, limit=RESEARCH_BYTES)
    codec.fields(value, ('answer', 'sources', 'unresolved'))
    text(value['answer'], 'research answer', 32000)
    if type(value['sources']) is not list or len(value['sources']) > 6:
        raise ValueError('At most six research sources are allowed')
    if type(value['unresolved']) is not list or len(value['unresolved']) > 32:
        raise ValueError('Invalid unresolved research questions')
    for question in value['unresolved']:
        text(question, 'unresolved question', 8000)
    actions = web_actions(events, limit=stage.action.web_calls)
    opened = any(a.action in ('openPage', 'findInPage') for a in actions)
    viewed = {url for a in actions for url in a.viewed_urls}
    quoted, sources, warnings = {}, [], []
    for ordinal, source in enumerate(value['sources']):
        codec.fields(source, ('url', 'title', 'location', 'excerpt', 'summary', 'hypotheses', 'accessed'))
        url = public_url(source['url'])
        for key in ('title', 'location', 'summary', 'hypotheses'):
            text(source[key], 'research '+key, 8000)
        text(source['excerpt'], 'research excerpt', 8000, empty=True)
        if type(source['accessed']) is not bool:
            raise ValueError('Invalid research access flag')
        quoted[url] = quoted.get(url, 0) + len(source['excerpt'].split())
        if quoted[url] > 25:
            raise ValueError('Research exceeds per-source quote allowance')
        if source['accessed'] and not opened and url not in viewed:
            warning = 'Research claimed access without an open/find or matching document view'
            if warning not in warnings:
                warnings.append(warning)
        if source['excerpt'] and not source['accessed']:
            raise ValueError('Unread research source cannot supply an exact excerpt')
        sources.append(dict(source, id=f'{stage.helper_id}S{ordinal+1}'))
    return pack({'format': 'midas-research-evidence-v1', 'stage': stage.sha256,
        'answer': value['answer'], 'sources': sources, 'unresolved': value['unresolved'],
        'warnings': warnings,
        'actions': [[a.id, a.action, list(a.viewed_urls)] for a in actions],
        'authority': 'advisory', 'provenance': 'Access and quotations are provider-reported.'}, RESEARCH_BYTES)


def research_summary(payload):
    """Present research content before paging; web actions stay in saved evidence."""
    value = codec.load_record(payload, limit=RESEARCH_BYTES)
    if value['format'] != 'midas-research-evidence-v1':
        raise ValueError('Expected admitted research evidence')
    parts = ['Warning: '+warning for warning in value['warnings']]
    parts.append('Research answer (advisory):\n'+value['answer'])
    for ordinal, source in enumerate(value['sources'], 1):
        parts.append('\n'.join((
            f"Source {ordinal}: {source['title']}",
            'URL: '+source['url'],
            'Location: '+source['location'],
            'Accessed (researcher-reported): '+str(source['accessed']),
            'Excerpt: '+source['excerpt'],
            'Summary: '+source['summary'],
            'Hypotheses: '+source['hypotheses'])))
    if value['unresolved']:
        parts.append('Unresolved questions:\n'+'\n'.join(value['unresolved']))
    return '\n\n'.join(parts)


def admit_research(stage, admitted, reader):
    """Interpret an authenticated provider handoff; provider owns its receipt seal.

    Controller must bind the admitted model request to this planned stage before
    calling, just as it binds a prepared checker request to a lookup/probe stage.
    """
    if admitted.status != ResultStatus.SUCCEEDED:
        raise ValueError('Research model invocation did not succeed')
    return research_evidence(stage, read(reader, admitted.raw, RESEARCH_BYTES),
                             read(reader, admitted.events, EVENT_BYTES))
