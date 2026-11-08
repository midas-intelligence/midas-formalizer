"""Candidate expansion and exact edits over frozen subjects and flat definitions."""
from dataclasses import dataclass, field
from hashlib import sha256

from ..domain import encode_submission, load_submission
from ..domain.encoding import load_record
from ..domain.identity import simple_name, validate_definitions, validate_imports
from ..domain.records import identifier, items, uuid
from .memory import pack, text

MAX_BYTES = 8 * 1024**2
DRAFT_METADATA_BYTES = 256 * 1024
EMPTY = pack([])
EMPTY_DRAFT_METADATA = pack([[], [], []])


class RevisionError(ValueError):
    """An edit cannot apply to the current candidate and needs model revision."""


@dataclass(frozen=True, slots=True)
class Subject:
    kind: str
    name: str
    scope_target: str | None = None
    statement: str | None = None

    def __post_init__(self):
        text(self.name, 1024)
        if self.kind == 'target':
            if self.scope_target is not None or self.statement is not None:
                raise ValueError('Target statements belong to the frozen selection')
        elif self.kind == 'lemma':
            simple_name(self.name)
            if self.scope_target is None:
                raise ValueError('Supporting lemmas require scope_target naming a selected target')
            text(self.scope_target, 1024)
            text(self.statement, 1024**2)
        else:
            raise ValueError('Unknown strategy subject')

    def value(self):
        return dict(kind=self.kind, name=self.name, **({} if self.kind == 'target' else
                    dict(scope_target=self.scope_target, statement=self.statement)))


@dataclass(frozen=True, slots=True)
class Reference:
    """One advertised external entry; never resolve arbitrary model UUIDs."""
    label: str
    key: str
    submission_id: str

    def __post_init__(self):
        text(self.label, 256)
        text(self.key, 256)
        uuid(self.submission_id)
        if self.label == 'current':
            raise ValueError('current is reserved for the proposed submission')


@dataclass(frozen=True, slots=True)
class Prerequisite:
    parameter: str
    reference: Reference
    statement: str | None = None

    def __post_init__(self):
        from ..domain.records import instance
        simple_name(self.parameter)
        instance(self.reference, Reference)
        if self.statement is not None:
            text(self.statement, 131072)

    def dependency(self):
        return dict(parameter=self.parameter, entry=dict(
            submission_id=self.reference.submission_id, key=self.reference.key))


@dataclass(frozen=True, slots=True)
class Context:
    selection_id: str
    subjects: tuple[Subject, ...]
    definitions: bytes = EMPTY
    prerequisites: tuple[Prerequisite, ...] = ()
    references: tuple[Reference, ...] = ()
    imports: tuple[str, ...] = ()
    inherited_imports: tuple[str, ...] = ()
    definition_targets: tuple[str, ...] = ()
    id: str = field(init=False)

    def __post_init__(self):
        identifier(self.selection_id, 'sel_')
        items(self.subjects, Subject)
        if (not 1 <= len(self.subjects) <= 256 or len(set(self.subjects)) != len(self.subjects)
                or len(self.subjects) > 1 and any(s.kind != 'target' for s in self.subjects)):
            raise ValueError('Expected ordered targets or one exact lemma')
        definitions = load_record(self.definitions, limit=MAX_BYTES)
        validate_definitions(definitions)
        if definitions and any(s.kind == 'target' for s in self.subjects):
            raise ValueError('Frozen targets cannot inherit proposed definitions')
        items(self.prerequisites, Prerequisite)
        items(self.references, Reference)
        items(self.imports, str)
        validate_imports(list(self.imports))
        items(self.inherited_imports, str)
        validate_imports(list(self.inherited_imports))
        items(self.definition_targets, str)
        if (not set(self.inherited_imports) <= set(self.imports)
                or len(set(self.definition_targets)) != len(self.definition_targets)
                or not set(self.definition_targets) <= {s.name for s in self.subjects if s.kind == 'target'}):
            raise ValueError('Invalid inherited imports or selected definitions')
        if (len(self.prerequisites) > 128 or len(self.references) > 4096
                or len({p.parameter for p in self.prerequisites}) != len(self.prerequisites)
                or len({(r.label, r.key) for r in self.references}) != len(self.references)):
            raise ValueError('Invalid prerequisite/reference collection')
        if {d['name'] for d in definitions} & {p.parameter for p in self.prerequisites}:
            raise ValueError('Definition shadows prerequisite')
        object.__setattr__(self, 'id', sha256(self.payload).hexdigest())

    @property
    def payload(self):
        return pack(['midas-strategy-context-v1', self.selection_id, [s.value() for s in self.subjects],
                     load_record(self.definitions, limit=MAX_BYTES), [[p.parameter, p.reference.label, p.reference.key,
                     p.reference.submission_id, p.statement] for p in self.prerequisites],
                     [[r.label, r.key, r.submission_id] for r in self.references], list(self.imports),
                     list(self.inherited_imports), list(self.definition_targets)], MAX_BYTES)

    @classmethod
    def load(cls, payload):
        v = load_record(payload, limit=MAX_BYTES)
        if type(v) is not list or len(v) != 9 or v[0] != 'midas-strategy-context-v1':
            raise ValueError('Invalid strategy context')
        if (any(type(v[i]) is not list for i in (2, 3, 4, 5, 6, 7, 8))
                or any(type(s) is not dict for s in v[2])
                or any(type(p) is not list or len(p) != 5 for p in v[4])
                or any(type(r) is not list or len(r) != 3 for r in v[5])):
            raise ValueError('Invalid strategy context rows')
        from ..domain.encoding import fields
        for subject in v[2]:
            fields(subject, ('kind', 'name') if subject.get('kind') == 'target' else
                   ('kind', 'name', 'scope_target', 'statement'))
        return cls(v[1], tuple(Subject(**s) for s in v[2]), pack(v[3], MAX_BYTES),
                   tuple(Prerequisite(p[0], Reference(*p[1:4]), p[4]) for p in v[4]),
                   tuple(Reference(*r) for r in v[5]), tuple(v[6]), tuple(v[7]), tuple(v[8]))


@dataclass(frozen=True, slots=True)
class Draft:
    submission: bytes
    guidance: bytes
    unproved: tuple[str, ...] = ()
    closed_definitions: tuple[str, ...] = ()
    review_entries: tuple[str, ...] = ()
    id: str = field(init=False)
    mode: str = field(init=False)

    def __post_init__(self):
        value = load_submission(self.submission)
        guidance = load_record(self.guidance, limit=MAX_BYTES)
        self._finish(value, guidance)

    def _finish(self, value, guidance):
        from ..domain.encoding import fields
        fields(guidance, ('rationale', 'proof', 'proof_departures', 'children'))
        for key in ('rationale', 'proof', 'proof_departures'):
            text(guidance[key], empty=key != 'rationale')
        if type(guidance['children']) is not dict or len(guidance['children']) > 256:
            raise ValueError('Invalid child guidance')
        keys = {e['key'] for e in value['entries']}
        for key, row in guidance['children'].items():
            fields(row, ('proof', 'usefulness'))
            if key not in keys:
                raise ValueError('Guidance names an unknown entry')
            text(row['proof'], empty=True)
            text(row['usefulness'], empty=True)
        items(self.unproved, str)
        if len(set(self.unproved)) != len(self.unproved) or not set(self.unproved) <= keys:
            raise ValueError('Invalid unproved entries')
        for e in value['entries']:
            if e['key'] in self.unproved and (e['subject']['kind'] != 'lemma'
                    or e['proof'] != 'by sorry' or e['key'] not in guidance['children']):
                raise ValueError('Unproved entries require a lemma binding and guidance slot')
        items(self.closed_definitions, str)
        targets = {e['key'] for e in value['entries'] if e['subject']['kind'] == 'target'}
        if (len(set(self.closed_definitions)) != len(self.closed_definitions)
                or not set(self.closed_definitions) <= targets):
            raise ValueError('Closed definitions must name selected target entries')
        items(self.review_entries, str)
        if self.review_entries and (len(set(self.review_entries)) != len(self.review_entries)
                or not set(self.review_entries) <= keys - set(self.unproved)
                or not self.unproved):
            raise ValueError('Reviews require a composition with English-guided children')
        digest = sha256(self.submission)
        digest.update(self.guidance)
        digest.update(self._metadata)
        object.__setattr__(self, 'id', digest.hexdigest())
        logical = any(d['entry']['submission_id'] != value['submission_id']
            or d['entry']['key'] not in self.closed_definitions for e in value['entries'] for d in e['dependencies'])
        object.__setattr__(self, 'mode', 'decomposition' if self.unproved or logical else 'direct')

    @classmethod
    def _create(cls, value, guidance, unproved, context):
        # Domain validates while encoding. Reuse those private decoded values
        # for context/guidance admission; never decode our new binary bytes again.
        payload = encode_submission(value)
        _admit(value, context)
        result = object.__new__(cls)
        object.__setattr__(result, 'submission', payload)
        object.__setattr__(result, 'guidance', pack(guidance, MAX_BYTES))
        object.__setattr__(result, 'unproved', unproved)
        object.__setattr__(result, 'closed_definitions', tuple(e['key'] for e in value['entries']
            if e['subject']['kind'] == 'target' and e['subject']['name'] in context.definition_targets))
        reviews = ()
        if unproved:
            subjects = {s.name: s.value() for s in context.subjects}
            reviews = tuple(e['key'] for e in value['entries']
                if subjects.get(e['subject']['name']) == e['subject'] and e['key'] not in unproved)
        object.__setattr__(result, 'review_entries', reviews)
        result._finish(value, guidance)
        return result

    @property
    def _metadata(self):
        if not (self.unproved or self.closed_definitions or self.review_entries):
            return EMPTY_DRAFT_METADATA
        return pack([list(self.unproved), list(self.closed_definitions), list(self.review_entries)], DRAFT_METADATA_BYTES)

    @property
    def payload(self):
        return (b'MIDASDRAFT\x02' + len(self.submission).to_bytes(8, 'little') + self.submission
                + len(self.guidance).to_bytes(8, 'little') + self.guidance
                + self._metadata)

    @classmethod
    def load(cls, payload):
        magic = b'MIDASDRAFT\x02'
        if (type(payload) is not bytes or len(payload) > 2 * MAX_BYTES + DRAFT_METADATA_BYTES
                or not payload.startswith(magic)):
            raise ValueError('Invalid candidate draft')
        pos, values = len(magic), []
        for _ in range(2):
            size = int.from_bytes(payload[pos:pos+8], 'little')
            pos += 8
            if not 0 < size <= MAX_BYTES or pos + size > len(payload):
                raise ValueError('Invalid draft field length')
            values.append(payload[pos:pos+size])
            pos += size
        remaining = load_record(payload[pos:], limit=DRAFT_METADATA_BYTES)
        if type(remaining) is not list or len(remaining) != 3 or any(type(v) is not list for v in remaining):
            raise ValueError('Invalid draft unproved entries')
        return cls(*values, *(tuple(row) for row in remaining))

    def checks(self):
        """Least dependent entry first; unproved entries receive binding only.

        Every entry needs binding. Conditional proofs/reviews wait for their
        dependency bindings. This order is deterministic and linear in edges.
        """
        value = load_submission(self.submission)
        entries = {e['key']: e for e in value['entries']}
        counts, parents = dict.fromkeys(entries, 0), {k: [] for k in entries}
        for key, entry in entries.items():
            for dep in entry['dependencies']:
                ref = dep['entry']
                if ref['submission_id'] == value['submission_id']:
                    counts[key] += 1
                    parents[ref['key']].append(key)
        ready = [k for k in entries if not counts[k]]
        ordered = []
        for key in ready:
            ordered.append((key, 'binding' if key in self.unproved else 'proof'))
            for parent in parents[key]:
                counts[parent] -= 1
                if not counts[parent]:
                    ready.append(parent)
        return tuple(ordered)


def _entry(key, subject, proof, dependencies, statement_count, proof_count, *, inherited=()):
    scope = dict(statement=statement_count, proof=proof_count)
    if inherited:
        scope.update(inherited_prerequisites=[p.parameter for p in inherited],
                     prerequisites={p.parameter: p.statement for p in inherited})
    return dict(key=key, subject=subject, proof=proof, dependencies=dependencies,
                review=dict(decision='approve', rationale='Proposed composition; dependencies remain obligations.')
                    if dependencies else None, revision_of=None, definition_scope=scope)


def _admit(value, context):
    # value has passed load_submission or encode_submission immediately before
    # this context admission. This function does not retain the mutable mapping.
    if value['selection_id'] != context.selection_id or not value['entries']:
        raise ValueError('Candidate selection differs or is empty')
    if value['format'] != 'midas-scoped-candidates':
        raise ValueError('Strategy candidates require flat definition scopes')
    entries = value['entries']
    targets = [e['subject'] for e in entries if e['subject']['kind'] == 'target']
    if targets != [s.value() for s in context.subjects if s.kind == 'target']:
        raise ValueError('Candidate must cover exact targets in source order')
    inherited = load_record(context.definitions, limit=MAX_BYTES)
    definitions = value.get('definitions', [])
    if definitions[:len(inherited)] != inherited:
        raise ValueError('Candidate changed inherited definitions')
    if context.subjects[0].kind == 'lemma':
        matches = [e for e in entries if e['subject'] == context.subjects[0].value()]
        if len(matches) != 1 or matches[0]['definition_scope']['statement'] != len(inherited):
            raise ValueError('Candidate changed the current lemma or its definition scope')
        dependencies = matches[0]['dependencies']
        if any(p.dependency() not in dependencies for p in context.prerequisites):
            raise ValueError('Candidate omitted an inherited prerequisite')
    allowed = {(r.submission_id, r.key) for r in context.references}
    allowed.update((p.reference.submission_id, p.reference.key) for p in context.prerequisites)
    scopes = {s.scope_target or s.name for s in context.subjects}
    for entry in entries:
        if entry['subject']['kind'] == 'lemma' and entry['subject']['scope_target'] not in scopes:
            raise ValueError('Lemma is outside the selected source scope')
        for dep in entry['dependencies']:
            ref = dep['entry']
            if ref['submission_id'] != value['submission_id'] and (ref['submission_id'], ref['key']) not in allowed:
                raise ValueError('Unadvertised external dependency')
    if not set(value.get('imports', ())) <= set(context.imports):
        raise ValueError('Candidate import was not advertised')
    if not set(context.inherited_imports) <= set(value.get('imports', ())):
        raise ValueError('Candidate omitted inherited imports')
    if context.definition_targets:
        earlier = set()
        selected = {e['key']: e['subject']['name'] for e in entries if e['subject']['kind'] == 'target'}
        for entry in entries:
            if entry['subject']['kind'] != 'target':
                continue
            actual = {d['entry']['key'] for d in entry['dependencies']
                      if d['entry']['submission_id'] == value['submission_id'] and d['entry']['key'] in selected}
            if actual != earlier:
                raise ValueError('Joint targets require exactly the preceding selected definitions plus supporting lemmas')
            if entry['subject']['name'] in context.definition_targets:
                earlier.add(entry['key'])
    return value


def expand(action, context, submission_id):
    """Use controller-allocated identity; no model binding/ID or proof is trusted."""
    uuid(submission_id)
    mode = action['mode']
    inherited = load_record(context.definitions, limit=MAX_BYTES)
    definitions = inherited + action['definitions']
    validate_definitions(definitions)
    names = {d['name'] for d in definitions} | {p.parameter for p in context.prerequisites}
    if len(names) != len(definitions) + len(context.prerequisites):
        raise ValueError('Definition shadows an inherited prerequisite')
    children, unproved = {}, []
    guidance = dict(rationale=action['rationale'], proof=action['proof'],
                    proof_departures=action['proof_departures'], children=children)
    if mode in ('proof', 'decomposition'):
        if len(context.subjects) != 1:
            raise ValueError('Proof/decomposition requires one subject')
        subject = context.subjects[0]
        rows = action.get('children', [])
        if mode == 'decomposition' and not rows:
            raise ValueError('Decomposition requires children')
        by_name = {}
        for child in rows:
            name = child['name']
            simple_name(name)
            if name in names or name in by_name or name == 'parent':
                raise ValueError('Duplicate child or colliding definition/prerequisite')
            by_name[name] = child
        external = {p.parameter: p for p in context.prerequisites}
        def dependency(name):
            if name in by_name:
                return dict(parameter=name, entry=dict(submission_id=submission_id, key=name))
            if name in external:
                return external[name].dependency()
            raise ValueError('Unknown child prerequisite')
        entries = [_entry('parent', subject.value(), action['proof_body'],
                   [p.dependency() for p in context.prerequisites] + [dependency(n) for n in by_name],
                   len(inherited), len(definitions), inherited=context.prerequisites)]
        # Even an empty inherited set must distinguish original premises from
        # children introduced by this plan when a later repair reopens its parent.
        entries[0]['definition_scope']['inherited_prerequisites'] = [p.parameter for p in context.prerequisites]
        for name, child in by_name.items():
            deps = child['depends_on']
            if len(set(deps)) != len(deps):
                raise ValueError('Duplicate child prerequisites')
            proof = child['proof_body']
            if proof is None:
                unproved.append(name)
                proof = 'by sorry'
            entries.append(_entry(name, Subject('lemma', name, subject.scope_target or subject.name,
                          child['statement']).value(), proof, [dependency(n) for n in deps],
                          len(definitions), len(definitions),
                          inherited=tuple(external[n] for n in deps if n in external)))
            # Child work retains all declared prerequisites, including siblings.
            entries[-1]['definition_scope'].pop('inherited_prerequisites', None)
            children[name] = dict(proof=child['proof'], usefulness=child['usefulness'])
    elif mode == 'candidate':
        entries = []
        subjects = {subject.name: subject for subject in context.subjects}
        scopes = {subject.scope_target or subject.name for subject in context.subjects}
        references = {(r.label, r.key): r.submission_id for r in context.references}
        references.update(((p.reference.label, p.reference.key), p.reference.submission_id) for p in context.prerequisites)
        for row in action['entries']:
            subject = subjects.get(row['name'])
            if subject is None:
                scope = row['scope_target']
                if scope is None and len(scopes) == 1:
                    scope = next(iter(scopes))
                subject = Subject('lemma', row['name'], scope, row['statement'])
            dependencies = []
            for dep in row['dependencies']:
                origin = submission_id if dep['submission'] == 'current' else references.get((dep['submission'], dep['key']))
                if origin is None:
                    raise ValueError('Unadvertised candidate reference')
                dependencies.append(dict(parameter=dep['parameter'], entry=dict(submission_id=origin, key=dep['key'])))
            proof = row['proof_body']
            if proof is None:
                if subject.kind != 'lemma':
                    raise ValueError('Required targets need complete proofs')
                unproved.append(row['key'])
                proof = 'by sorry'
            entries.append(_entry(row['key'], subject.value(), proof, dependencies,
                           (0 if subject.kind == 'target' else len(inherited) if subject in context.subjects
                            else len(definitions)), len(definitions)))
            if subject.kind == 'lemma':
                children[row['key']] = dict(proof=row['proof'], usefulness='')
    else:
        raise ValueError('Expected a complete candidate action')
    value = dict(format='midas-scoped-candidates', submission_id=submission_id,
                 selection_id=context.selection_id, definitions=definitions,
                 imports=list(dict.fromkeys((*context.inherited_imports, *action['imports']))), entries=entries)
    return Draft._create(value, guidance, tuple(unproved), context)


def _escape(value):
    return value.replace('~', '~0').replace('/', '~1')


def _fragments(value, guidance, context, unproved, compiled=()):
    paths = {}
    hidden = set(unproved) | set(compiled)
    fixed = [subject.value() for subject in context.subjects]
    for entry in value['entries']:
        prefix = '/entries/' + _escape(entry['key'])
        if entry['key'] not in hidden:
            paths[prefix + '/proof'] = (entry, 'proof')
        if entry['subject']['kind'] == 'lemma' and entry['subject'] not in fixed:
            paths[prefix + '/statement'] = (entry['subject'], 'statement')
    inherited_count = len(load_record(context.definitions, limit=MAX_BYTES))
    for definition in value.get('definitions', [])[inherited_count:]:
        for key in ('type', 'value'):
            paths['/definitions/' + _escape(definition['name']) + '/' + key] = (definition, key)
    for key in ('proof', 'proof_departures'):
        paths['/' + key] = (guidance, key)
    for name, row in guidance['children'].items():
        for key in ('proof', 'usefulness'):
            paths['/children/' + _escape(name) + '/' + key] = (row, key)
    return paths


def candidate_material(draft, context, *, compiled=()):
    """Decode a display once: structure, guidance and exact editable strings."""
    value = load_submission(draft.submission)
    guidance = load_record(draft.guidance, limit=MAX_BYTES)
    editable = tuple((path, owner[key]) for path, (owner, key) in _fragments(
        value, guidance, context, draft.unproved, compiled).items())
    return value, guidance, editable


def fragments(draft, context, *, compiled=()):
    return candidate_material(draft, context, compiled=compiled)[2]


def revise(action, base, context, submission_id, *, compiled=()):
    """Resolve exact edits against the base, then apply disjoint spans atomically."""
    uuid(submission_id)
    value = load_submission(base.submission)
    original_id = value['submission_id']
    if submission_id == original_id:
        raise ValueError('Revision requires a fresh submission identity')
    guidance = load_record(base.guidance, limit=MAX_BYTES)
    paths = _fragments(value, guidance, context, base.unproved, compiled)
    edits = action['edits']
    if not edits:
        raise RevisionError('Revision requires edits')
    pending, removed = {}, set()
    for index, edit in enumerate(edits, 1):
        if edit['path'] not in paths:
            continue
        owner, key = paths[edit['path']]
        old, new = edit['old'], edit['new']
        current = owner[key]
        first, end = 0, len(current)
        if old is not None:
            if not old:
                raise RevisionError('Old fragment must be nonempty')
            first = current.find(old)
            if first < 0 or current.find(old, first + 1) >= 0:
                matches = 'no matches' if first < 0 else 'multiple matches'
                raise RevisionError(f'Edit {index} at {edit["path"]}: '
                                    f'Old fragment must match exactly once ({matches})')
            end = first + len(old)
        if new is None:
            if (key != 'proof' or owner.get('subject', {}).get('kind') != 'lemma'
                    or owner['subject'] in [subject.value() for subject in context.subjects]):
                raise RevisionError('Null new is allowed only for a supporting child Lean proof: ' + edit['path'])
            if first != 0 or end != len(current):
                raise RevisionError('Null new must replace the whole child Lean proof: ' + edit['path'])
            removed.add(owner['key'])
        if new == (current if old is None else old):
            raise RevisionError('No-op fragment edit: ' + edit['path'])
        pending.setdefault(edit['path'], []).append((first, end, new, index))
    if not pending:
        return base
    changed = False
    for path, replacements in pending.items():
        replacements.sort(key=lambda replacement: replacement[0])
        for left, right in zip(replacements, replacements[1:]):
            # Equal starts also catch two whole replacements of an empty fragment.
            if right[0] < left[1] or right[0] == left[0]:
                raise RevisionError(f'Edits {left[3]} and {right[3]} at {path}: Overlapping edits')
        owner, key = paths[path]
        current = owner[key]
        if replacements[0][2] is None:
            owner[key] = 'by sorry'
            changed = True
            continue
        parts, end = [], 0
        for first, stop, new, _ in replacements:
            parts.extend((current[end:first], new))
            end = stop
        parts.append(current[end:])
        new = ''.join(parts)
        text(new, 1024**2, empty=True)
        changed |= new != current
        owner[key] = new
    if not changed:
        raise RevisionError('Revision has no net changes')
    for name in sorted(removed):
        guidance['children'].setdefault(name, dict(proof='', usefulness=''))
    guidance['rationale'] = action['rationale']
    value['submission_id'] = submission_id
    for entry in value['entries']:
        for dep in entry['dependencies']:
            if dep['entry']['submission_id'] == original_id:
                dep['entry']['submission_id'] = submission_id
    unproved = base.unproved
    if removed:
        keys = set(unproved) | removed
        unproved = tuple(entry['key'] for entry in value['entries'] if entry['key'] in keys)
    return Draft._create(value, guidance, unproved, context)


def supplied(payload, context, *, guidance):
    """Supplied candidates share domain/context admission and checker scheduling."""
    value = _admit(load_submission(payload), context)
    if value['selection_id'] != context.selection_id or not value['entries']:
        raise ValueError('Supplied candidate selection differs or is empty')
    return Draft(payload, guidance, closed_definitions=tuple(e['key'] for e in value['entries']
        if e['subject']['kind'] == 'target' and e['subject']['name'] in context.definition_targets))


def child_context(draft, key, context):
    """Project a child without embedding its ancestor draft or unrelated premises."""
    value = _admit(load_submission(draft.submission), context)
    entries = {e['key']: e for e in value['entries']}
    if key not in entries or entries[key]['subject']['kind'] != 'lemma':
        raise ValueError('Expected a proposed child lemma')
    entry = entries[key]
    bounds = entry['definition_scope']
    inherited = bounds.get('inherited_prerequisites')
    prerequisites, references = [], list(context.references)
    references.extend(Reference('base', k, value['submission_id']) for k in entries)
    # The newest base owns the base label. Other advertised labels remain stable.
    by_name = {(r.label, r.key): r for r in references}
    for dep in entry['dependencies']:
        if inherited is not None and dep['parameter'] not in inherited:
            continue
        ref = dep['entry']
        reference = next((r for r in by_name.values() if (r.submission_id, r.key) ==
                          (ref['submission_id'], ref['key'])), None)
        if reference is None:
            reference = Reference('dependency-' + str(len(prerequisites)), ref['key'], ref['submission_id'])
            by_name[(reference.label, reference.key)] = reference
        sibling = entries.get(ref['key']) if ref['submission_id'] == value['submission_id'] else None
        statement = sibling['subject'].get('statement') if sibling else bounds.get('prerequisites', {}).get(dep['parameter'])
        prerequisites.append(Prerequisite(dep['parameter'], reference, statement))
    return Context(context.selection_id, (Subject(**entry['subject']),),
        pack(value['definitions'][:bounds['statement']], MAX_BYTES), tuple(prerequisites),
        tuple(by_name.values()), context.imports, tuple(value.get('imports', ())))
