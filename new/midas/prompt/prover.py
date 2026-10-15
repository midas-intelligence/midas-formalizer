"""Default prover strategy. Replace render to change wording, schema or layout."""
from .protocol import (CONTEXT_RULES, DomainError, Prompt, array, display, nullable,
                       obj, schema_bytes, string)

INSTRUCTIONS = b"""Prove the exact displayed task, covering every required target in source order.
Submit a complete Lean proof term in proof_body, without sorry, new axioms, or
changes to the given source and assumptions. Use normal Lean notation and named
definitions from the supplied context. Imports must be advertised prepared modules;
do not put import commands inside proof_body.

Use proof for one goal. Use decomposition for one goal with useful intermediate
lemmas and complete conditional glue in proof_body. Children have explicit
statements, English proof plans, and depends_on names: only those prerequisites
are available to each child. Parent hypotheses are not inherited implicitly.
Definitions are ordered named def/abbrev declarations with bodies; they may use
earlier definitions but cannot assume child theorems. Explain a credible next
mathematical step for every child. A stronger lemma is useful when it unlocks a
method; renaming the task alone is not progress. Hard children may decompose again.

Use candidate for joint targets, with target entries in source order and any
shared lemma entries. Use null scope_target/statement for targets. Unproved lemma
entries have null proof_body and a concrete English proof plan. Dependencies name
explicit parameters and entries; submission is current, base, or an advertised
reference. The application binds identifiers and arranges independent reviews.
Use revise only for exact displayed fragment names. Null old replaces a whole
fragment; otherwise old must match exactly once. Structural changes require a
complete new proof, decomposition or candidate.

Helpers continue this task with advisory evidence. Search discovers hints;
lookup checks named interfaces. retrieve combines searches and checked lookup.
Backend names are frozen-source and leanexplore; only frozen-source uses prefixes.
Queries require backends, and names-only retrieval uses an empty backends array.
inspect requests authored source; discovery alone does not certify availability.
Probe cases run in the frozen checked target context, without proposed children or
definitions. operation=tactic takes tactic commands without an outer by;
operation=term inspects a Lean expression and synth inspects instance synthesis
for a type. setup contains initial tactics; expected is null or an expected type.
Research supplies a mathematical question and at most six public source URLs.
Recall/page copy an advertised result ID/cursor exactly; evidence pages remain
advisory. Failed tools do not establish that a mathematical claim is false.

memory_update is null or concise strategy/lessons and evidence pins to retain.
Use stop when unable to progress. An optional objection must give mathematical
evidence about the exact statement: suspected_false, missing_assumption, or
context_mismatch. Exhaustion alone is not an objection. Explain proof departures
from any assigned English argument; never silently replace the task.
""" + CONTEXT_RULES


def schema():
    text, code = string(), string(1024 * 1024)
    ref = lambda name: {'$ref': '#/$defs/' + name}
    definitions = {
        'definition': obj(name=string(256), kind={'enum': ['def', 'abbrev'], 'type': 'string'},
                          type=code, value=code),
        'child': obj(name=string(256), statement=code, proof=text, usefulness=text,
                     depends_on=array(string(256), 128)),
        'dependency': obj(parameter=string(1024), submission=string(256), key=string(256)),
        'entry': obj(key=string(256), kind={'type': 'string', 'enum': ['target', 'lemma']},
                     name=string(1024), scope_target=nullable(string(1024)), statement=nullable(code),
                     proof_body=nullable(code), proof=text, dependencies=array(ref('dependency'), 128)),
        'backend': obj(name={'type': 'string', 'enum': ['frozen-source', 'leanexplore']},
                       prefixes=array(string(1024), 16)),
        'memory': obj(strategy=text, upsert=array(obj(id=string(256), text=text), 32),
                      forget=array(string(256), 32), pin_evidence=array(string(256), 24),
                      unpin_evidence=array(string(256), 24)),
    }
    def action(mode, **fields):
        return obj(mode={'type': 'string', 'enum': [mode]}, rationale=text, **fields)
    mathematical = dict(proof_body=code, proof=text, proof_departures=text,
                        definitions=array(ref('definition'), 128), imports=array(string(256), 32))
    actions = [
        action('proof', **mathematical),
        action('decomposition', **mathematical, children=array(ref('child'), 128)),
        action('candidate', entries=array(ref('entry'), 256), proof=text, proof_departures=text,
               definitions=array(ref('definition'), 128), imports=array(string(256), 32)),
        action('revise', edits=array(obj(path=string(1024), old=nullable(code), new=code), 128)),
        action('search', query=string(2048), backend=ref('backend')),
        action('lookup', names=array(string(1024), 16), inspect={'type': 'boolean'}),
        action('retrieve', queries=array(string(2048), 3), names=array(string(1024), 12),
               backends=array(ref('backend'), 2), offset={'type': 'integer', 'minimum': 0, 'maximum': 200},
               inspect={'type': 'boolean'}),
        action('probe', cases=array(obj(id=string(48), code=string(16000),
               operation={'type': 'string', 'enum': ['tactic', 'term', 'synth']},
               setup=string(16000), expected=nullable(string(16000))), 3)),
        action('research', question=string(8000), urls=array(string(8000), 6)),
        action('recall', result_id=string(256)),
        action('page', cursor=string(4096)),
        action('stop', objection=nullable(obj(category={'type': 'string', 'enum': [
            'suspected_false', 'missing_assumption', 'context_mismatch']}, evidence=text))),
    ]
    return dict(obj(action={'anyOf': actions}, memory_update=nullable(ref('memory'))), **{'$defs': definitions})


SCHEMA = schema_bytes(schema())


def render(snapshot):
    if not any(s.kind == 'subject' for s in snapshot.sections):
        raise DomainError('Prover requires an exact subject')
    sections = display(snapshot, ('source', 'definitions', 'subject', 'interfaces', 'history',
                       'candidate', 'memory', 'capabilities', 'evidence', 'feedback', 'question'))
    return Prompt('prover', 'prover-v1', 'prover-action-v1', INSTRUCTIONS, SCHEMA, sections)
