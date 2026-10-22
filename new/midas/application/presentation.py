"""Flat readable candidate and memory sections, without archive envelopes or JSON."""
import re

from ..prompt import Section
from ..strategy import candidate_material


def exact(text):
    """Frame one exact string, including empty strings and trailing newlines."""
    raw = text.encode()
    fence = b'`' * max(3, 1 + max((len(m[0]) for m in re.finditer(rb'`+', raw)), default=0))
    return fence + b'\n' + raw + b'\n' + fence


def candidate(draft, context, *, selected=None, checked=None):
    value, guidance, editable = candidate_material(draft, context)
    references = {(r.submission_id, r.key): r.label for r in context.references}
    references.update(((p.reference.submission_id, p.reference.key), p.reference.label)
                      for p in context.prerequisites)
    definitions = value.get('definitions', [])
    paths = dict(editable)
    reviewing = selected is not None
    rows = [b'Candidate for independent review.' if reviewing else
            b'Current candidate. Local entry labels describe this candidate; only references\n'
            b'offered in INTERFACES are available to a new candidate action.',
            b'Rationale:\n' + exact(guidance['rationale'])]
    if selected is not None:
        entry = next((e for e in value['entries'] if e['key'] == selected), None)
        if entry is None:
            raise ValueError('Reviewed entry is missing from candidate')
        if checked is not None and entry['proof'].encode() != checked:
            raise ValueError('Reviewed entry differs from checked proof')
        rows.append(('Selected review entry: ' + selected).encode())
        if checked is not None:
            rows.append(b'The selected entry has a checked conditional composition; its children remain obligations.')
    if value.get('imports'):
        rows.append(('Candidate imports: ' + ', '.join(value['imports'])).encode())
    for definition in definitions:
        rows.append(f"Definition: {definition['kind']} {definition['name']}".encode())
        if reviewing:
            rows.extend((b'Type:\n' + exact(definition['type']), b'Value:\n' + exact(definition['value'])))
    for entry in value['entries']:
        goal = entry['subject']
        prefix = '/entries/' + entry['key'].replace('~', '~0').replace('/', '~1')
        lines = [f"Entry: {entry['key']}", f"Subject: {goal['kind']} {goal['name']}"]
        if goal['kind'] == 'lemma':
            lines.append('Scope target: ' + goal['scope_target'])
        for field in ('statement', 'proof'):
            path = prefix + '/' + field
            if not reviewing and path in paths:
                lines.append(field.capitalize() + ' fragment: ' + path)
        if entry['key'] in draft.unproved:
            lines.append('Proof: pending obligation; see child English guidance.')
        dependencies = []
        for dep in entry['dependencies']:
            ref = dep['entry']
            label = references.get((ref['submission_id'], ref['key']))
            origin = ('local entry ' + ref['key'] if ref['submission_id'] == value['submission_id'] else
                      label + ':' + ref['key'] if label else 'saved entry ' + ref['key'] + ' (not offered)')
            dependencies.append(f"{dep['parameter']} <- {origin}")
        lines.append('Dependencies: ' + ('; '.join(dependencies) or 'none'))
        for scope in ('statement', 'proof'):
            names = [d['name'] for d in definitions[:entry['definition_scope'][scope]]]
            lines.append('Definitions in ' + scope + ': ' + (', '.join(names) or 'none'))
        rows.append('\n'.join(lines).encode())
        # Fixed local statements are not editable but still part of the review.
        if goal['kind'] == 'lemma' and (reviewing or prefix + '/statement' not in paths):
            rows.append((b'Statement:\n' if reviewing else b'Fixed statement:\n') + exact(goal['statement']))
        if reviewing:
            if entry['key'] not in draft.unproved:
                rows.append(b'Proof:\n' + exact(entry['proof']))
            child = guidance['children'].get(entry['key'])
            if child:
                rows.append(b'English proof plan:\n' + exact(child['proof']))
                if child['usefulness']:
                    rows.append(b'Usefulness:\n' + exact(child['usefulness']))
    if reviewing:
        for key, label in (('proof', b'Overall proof plan'), ('proof_departures', b'Proof departures')):
            if guidance[key]:
                rows.append(label + b':\n' + exact(guidance[key]))
    else:
        rows.extend(b'Editable fragment: ' + path.encode() + b'\n' + exact(text) for path, text in editable)
    return Section('candidate', 'current-draft', b'\n\n'.join(rows))


def memory(value):
    sections = []
    # Complete observations are stable history records; strategy/lessons are
    # changing turn material. Never flatten diagnostic newlines or lose notices.
    observations = tuple(o for o in value.observations if o.status != 'protocol')
    for index, observation in enumerate(observations):
        rows = [f'{observation.source}: {observation.status}'.encode()]
        rows.extend(message.encode() for message in observation.messages)
        if observation.omitted:
            rows.append(f'{observation.omitted} diagnostic message(s) omitted.'.encode())
        kind = 'feedback' if index == len(observations) - 1 else 'history'
        sections.append(Section(kind, f'observation-{index+1}', b'\n\n'.join(rows)))
    rows = []
    if value.strategy:
        rows.append('Strategy:\n' + value.strategy)
    rows.extend(f'Lesson {lesson.id}:\n{lesson.text}' for lesson in value.lessons)
    if value.pins:
        rows.append('Pinned evidence:\n' + '\n'.join(value.pins))
    if value.evicted:
        rows.append(f'{value.evicted} older memory record(s) evicted.')
    if rows:
        sections.append(Section('memory', 'memory', '\n\n'.join(rows).encode()))
    return tuple(sections)


def stop_request(request):
    rows = [b'Stop request for independent review. This node is still active.',
            b'Rationale:\n' + exact(request.rationale)]
    if request.objection:
        rows.extend((b'Statement objection: ' + request.objection.category.encode(),
                     b'Evidence claimed:\n' + exact(request.objection.evidence)))
    return Section('candidate', 'stop-request', b'\n\n'.join(rows))
