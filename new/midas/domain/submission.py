"""Bounded supplied-candidate syntax admission; no Lean or proof-state authority.

External entry references, target names, revisions, and structural parameter names
still require controller/Lean resolution before semantic records are published.
"""

import re

from copy import deepcopy

from .encoding import fields, load_record, record_parts, record_size
from .identity import bounded_text as _text, validate_imports
from .records import uuid as _validate_uuid


FORMAT = 'midas-candidates'
SUBMISSION_MAGIC = b'MIDASSUB\x01'
BYTE_LIMIT = 8 * 1024**2
TEXT_LIMIT = 1024**2


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError('Expected a canonical UUID string')
    _validate_uuid(value)
    return value


def load_submission(payload):
    """Decode an explicitly saved binary submission, then validate its records."""
    if type(payload) is not bytes or len(payload) > BYTE_LIMIT or not payload.startswith(SUBMISSION_MAGIC):
        raise ValueError('Expected bounded binary candidate submission')
    value = load_record(payload[len(SUBMISSION_MAGIC):], limit=BYTE_LIMIT - len(SUBMISSION_MAGIC))
    _validate_submission(value)
    return value


def encode_submission(value):
    """Save a submission with raw text fields, without any JSON operations."""
    parts = list(record_parts(value, limit=BYTE_LIMIT - len(SUBMISSION_MAGIC)))
    _validate_submission(value)
    return SUBMISSION_MAGIC + b''.join(parts)


def validate_submission(value, *, copy=False):
    """Validate supplied records directly; serialization is a separate operation."""
    record_size(value, limit=BYTE_LIMIT - len(SUBMISSION_MAGIC))
    _validate_submission(value)
    return deepcopy(value) if copy else value


def _validate_submission(value):
    if not isinstance(value, dict):
        raise ValueError('Expected candidate submission object')
    is_scoped = value.get('format') == SCOPED_FORMAT
    fields(value, ('format', 'submission_id', 'selection_id', 'entries') + (('definitions',) if is_scoped else ()) +
           (('imports',) if 'imports' in value else ()))
    if 'imports' in value:
        validate_imports(value['imports'])
    if value['format'] not in (FORMAT, SCOPED_FORMAT):
        raise ValueError('Unsupported candidate format')
    if is_scoped:
        validate_definition_block(value)
    _uuid(value['submission_id'])
    if not isinstance(value['selection_id'], str) or not re.fullmatch(r'sel_[0-9a-f]{64}', value['selection_id']):
        raise ValueError('Invalid selection ID')
    entries = value['entries']
    if not isinstance(entries, list) or len(entries) > 256:
        raise ValueError('Expected at most 256 candidate entries')
    keys = set()
    for entry in entries:
        fields(entry, ('key', 'subject', 'proof', 'dependencies', 'review', 'revision_of') +
               (('definition_scope',) if is_scoped else ()) +
               (('declaration_context',) if 'declaration_context' in entry else ()) +
               (('imports',) if 'imports' in entry else ()))
        if 'imports' in entry:
            validate_imports(entry['imports'])
        if 'declaration_context' in entry:
            from .identity import validate_context as validate
            validate(entry['declaration_context'])
        _text(entry['key'], 'entry key')
        if entry['key'] in keys:
            raise ValueError('Duplicate candidate entry key')
        keys.add(entry['key'])
        _text(entry['proof'], 'proof', TEXT_LIMIT)
        if entry['revision_of'] is not None:
            _uuid(entry['revision_of'])
        subject = entry['subject']
        if not isinstance(subject, dict):
            raise ValueError('Invalid candidate subject')
        if subject.get('kind') == 'target':
            fields(subject, ('kind', 'name'))
            _text(subject['name'], 'target name', TEXT_LIMIT)
        elif subject.get('kind') == 'lemma':
            fields(subject, ('kind', 'scope_target', 'name', 'statement') +
                   (('declaration_context',) if 'declaration_context' in subject else ()))
            if 'declaration_context' in subject:
                from .identity import extends, entry_context
                if not extends(entry_context(entry), subject['declaration_context']):
                    raise ValueError('Entry does not import its statement context')
            _text(subject['scope_target'], 'scope target', TEXT_LIMIT)
            _text(subject['name'], 'lemma label')
            _text(subject['statement'], 'statement', TEXT_LIMIT)
        else:
            raise ValueError('Unknown candidate subject kind')
        dependencies = entry['dependencies']
        if not isinstance(dependencies, list) or len(dependencies) > 128:
            raise ValueError('Expected at most 128 dependencies')
        parameters = set()
        for dependency in dependencies:
            fields(dependency, ('parameter', 'entry'))
            _text(dependency['parameter'], 'dependency parameter', TEXT_LIMIT)
            if dependency['parameter'] in parameters:
                raise ValueError('Duplicate dependency parameter')
            parameters.add(dependency['parameter'])
            reference = dependency['entry']
            fields(reference, ('submission_id', 'key'))
            _uuid(reference['submission_id'])
            _text(reference['key'], 'dependency entry key')
        if is_scoped:
            validate_entry_scope(value, entry)
        if dependencies:
            review = entry['review']
            fields(review, ('decision', 'rationale'))
            if review['decision'] != 'approve':
                raise ValueError('Conditional candidate requires supplied approval')
            _text(review['rationale'], 'review rationale', 16 * 1024)
        elif entry['review'] is not None:
            raise ValueError('Direct candidate must have null review')
    _local_references(value)
    return value


def _local_references(submission):
    entries = {entry['key']: entry for entry in submission['entries']}
    incoming = dict.fromkeys(entries, 0)
    children = {key: [] for key in entries}
    for key, entry in entries.items():
        for dependency in entry['dependencies']:
            reference = dependency['entry']
            if reference['submission_id'] != submission['submission_id']:
                continue
            child = reference['key']
            if child not in entries:
                raise ValueError('Missing same-submission dependency')
            children[key].append(child)
            incoming[child] += 1
    pending = [key for key, count in incoming.items() if count == 0]
    visited = 0
    while pending:
        key = pending.pop()
        visited += 1
        for child in children[key]:
            incoming[child] -= 1
            if incoming[child] == 0:
                pending.append(child)
    if visited != len(entries):
        raise ValueError('Same-submission dependency cycle')




SCOPED_FORMAT = 'midas-scoped-candidates'


def validate_definition_block(value):
    from .identity import validate_definitions
    validate_definitions(value['definitions'])


def validate_entry_scope(value, entry):
    bounds = entry['definition_scope']
    if not isinstance(bounds, dict):
        raise ValueError('Expected candidate definition scope object')
    fields(bounds, {'statement', 'proof'} | ({'prerequisites', 'inherited_prerequisites'} & bounds.keys()))
    start, end = bounds['statement'], bounds['proof']
    if (type(start) is not int or type(end) is not int
            or not 0 <= start <= end <= len(value['definitions'])):
        raise ValueError('Invalid candidate definition scope')
    if entry['subject']['kind'] == 'target' and start != 0:
        raise ValueError('Frozen target cannot have a statement definition scope')
    if 'declaration_context' in entry or 'declaration_context' in entry['subject']:
        raise ValueError('Scoped entries cannot duplicate declaration contexts')
    if 'inherited_prerequisites' in bounds:
        parameters = bounds['inherited_prerequisites']
        if (not isinstance(parameters, list) or any(not isinstance(p, str) for p in parameters)
                or len(set(parameters)) != len(parameters)
                or not set(parameters) <= {d['parameter'] for d in entry['dependencies']}):
            raise ValueError('Invalid inherited prerequisite scope')
    types = bounds.get('prerequisites', {})
    if not isinstance(types, dict) or not types.keys() <= {d['parameter'] for d in entry['dependencies']}:
        raise ValueError('Unknown scoped prerequisite presentation')
    for text in types.values():
        if text is not None:
            _text(text, 'prerequisite presentation', 131072)


def resolve_entry(value, entry):
    """Return one checker-compatible entry without changing the saved submission."""
    entry = deepcopy(entry)
    from .identity import entry_imports
    imports = entry_imports(value, entry)
    if imports:
        entry['imports'] = imports
    if value.get('format') != SCOPED_FORMAT:
        return entry
    from .identity import make_context as make, set_entry_context
    bounds = entry.pop('definition_scope')
    subject = entry['subject']
    scope = subject.get('scope_target', subject['name'])
    statement = make(scope, value['definitions'][:bounds['statement']])
    work = statement if bounds['proof'] == bounds['statement'] else make(scope, value['definitions'][:bounds['proof']])
    if subject['kind'] == 'lemma':
        subject['declaration_context'] = statement
    set_entry_context(entry, work)
    return entry


def entry_subject(value, entry):
    subject = deepcopy(entry['subject'])
    if value.get('format') == SCOPED_FORMAT and subject['kind'] == 'lemma':
        from .identity import make_context as make
        subject['declaration_context'] = make(subject['scope_target'], value['definitions'][:entry['definition_scope']['statement']])
    return subject


def matches_subject(value, entry, expected):
    """Avoid deriving a context for unrelated entries during subject selection."""
    if expected is None:
        return False
    plain = {k: v for k, v in expected.items() if k != 'declaration_context'}
    actual = {k: v for k, v in entry['subject'].items() if k != 'declaration_context'}
    return actual == plain and entry_subject(value, entry) == expected


def lexical_scope(value, entry):
    """Derive the selected statement's source scope and explicit prerequisites."""
    bounds = entry['definition_scope']
    entries = {e['key']: e for e in value['entries']}
    types = {}
    inherited = bounds.get('inherited_prerequisites')
    for dep in entry['dependencies']:
        # A parent proof depends on its proposed children, but replacing that
        # proof must start from the goal's original prerequisites. Ordinary
        # entries without an explicit boundary retain all their dependencies.
        if inherited is not None and dep['parameter'] not in inherited:
            continue
        ref = dep['entry']
        # External prerequisite source is optional; its checked interface remains
        # authoritative. Same-submission statements are available without a copy.
        sibling = entries.get(ref['key']) if ref['submission_id'] in (value.get('submission_id'), 'base', 'current') else None
        types[dep['parameter']] = sibling['subject'].get('statement') if sibling else bounds.get('prerequisites', {}).get(dep['parameter'])
    return dict(definitions=deepcopy(value['definitions'][:bounds['statement']]),
                statement=entry['subject']['statement'], prerequisites=types,
                representation='declarations-v1')
