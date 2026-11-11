"""Validate mathematical guidance; research is advisory and uses no web tools."""
from ..domain import ResultStatus
from ..domain import encoding as codec
from ..domain.wire import decode_object
from .protocol import Research, Stage, pack, read, text

RESEARCH_BYTES = 131072


def research_evidence(stage, raw):
    """Called only after provider receipt admission; returns flat binary evidence."""
    if not isinstance(stage, Stage) or not isinstance(stage.action, Research):
        raise ValueError('Expected research stage')
    value = decode_object(raw, limit=RESEARCH_BYTES)
    codec.fields(value, ('answer', 'unresolved'))
    text(value['answer'], 'research answer', 32000)
    if type(value['unresolved']) is not list or len(value['unresolved']) > 32:
        raise ValueError('Invalid unresolved research questions')
    for question in value['unresolved']:
        text(question, 'unresolved question', 8000)
    return pack({'format': 'midas-research-evidence-v2', 'stage': stage.sha256,
        'answer': value['answer'], 'unresolved': value['unresolved'],
        'authority': 'advisory'}, RESEARCH_BYTES)


def research_summary(payload):
    value = codec.load_record(payload, limit=RESEARCH_BYTES)
    if value['format'] != 'midas-research-evidence-v2':
        raise ValueError('Expected admitted research evidence')
    parts = ['Research guidance (advisory):\n'+value['answer']]
    if value['unresolved']:
        parts.append('Unresolved questions:\n'+'\n'.join(value['unresolved']))
    return '\n\n'.join(parts)


def admit_research(stage, admitted, reader):
    """Interpret a provider handoff bound by the controller to this helper stage."""
    if admitted.status != ResultStatus.SUCCEEDED:
        raise ValueError('Research model invocation did not succeed')
    return research_evidence(stage, read(reader, admitted.raw, RESEARCH_BYTES))
