"""Default prover strategy. Replace render to change wording, schema or layout."""
from functools import lru_cache

from .protocol import (CONTEXT_RULES, DomainError, Prompt, Section, array, display, nullable,
                       obj, schema_bytes, string)

INSTRUCTIONS = """Solve the displayed Lean task over multiple turns. Each reply is one JSON action.
The application executes helper actions and returns their results in a later
request. To call a helper, return its action object; a helper request is a complete
reply for this turn. You can obtain missing context before submitting a proof.

Prove the exact displayed task, covering every required target in source order.
When submitting a proof, give a complete Lean term in proof_body, without sorry,
new axioms, or changes to the given source and assumptions. Use Lean notation and
definitions from the supplied context. Imports must be advertised prepared modules;
do not put import commands inside proof_body.
The local goal is exact: never assume it, an ancestor, or an unlisted sibling.
Background source and English plans do not add hypotheses. Preserve all binders,
universe signatures, and checked dependency aliases. Do not emit top-level commands.

Choose one action using the CAPABILITIES section before filling its fields.
If you need the meaning of a named constant in the goal and lookup is enabled,
your next action must be lookup with its Lean name and inspect=true. Missing
definitions are a reason to call lookup. An import does not
display the definitions it contains; lookup retrieves them. For example:
{"action":{"mode":"lookup","rationale":"Inspect the definition used in the goal.","names":["Nat.Prime"],"inspect":true},"memory_update":null}
Use the actual names from your task in place of this example's name.

lookup.names identifies Lean declarations. recall.result_id identifies an earlier
tool result listed in CAPABILITIES under saved evidence not included in this prompt.
Tool result sections identify the result ID and pages already displayed; use their
contents directly. SOURCE section labels such as source-prefix and search-roots
are not result IDs. With no offered recall results, do not use recall.
With no offered cursors, do not use page. If research is
disabled, choose an enabled local helper or work on the proof directly.
Every action needs a nonempty rationale explaining what it will accomplish.
Give a concise actionable reason, not a reasoning transcript.

Use proof for one goal. Use decomposition for one goal with useful intermediate
lemmas and complete conditional glue in proof_body. Children have explicit
statements, English proof plans, and depends_on names: only those prerequisites
are available to each child. Parent hypotheses are not inherited implicitly.
Definitions are ordered named def/abbrev declarations with bodies; they may use
earlier definitions but cannot assume child theorems. Explain a credible next
mathematical step for every child. A stronger lemma is useful when it unlocks a
method; renaming the task alone is not progress. Hard children may decompose again.
Choose boundaries that remove distinct mathematical obstacles. Identify what each
child resolves and what remains for the hardest child. A dominant hard child is
acceptable when its selected supporting lemmas remove concrete obstacles; balance
the benefit against extra context, review, duplicated work and assembly. No failed
direct attempt, equal difficulty, or fixed child count is required.
Each child must be provable under its exact statement and selected dependencies.
State every needed hypothesis explicitly or derive it from that allowed context.
Give supported constructions, lemmas, tactics and failed routes in its English
proof; make remaining gaps explicit. These plans accompany the child in later work.
A new proof action replaces the proposed decomposition and drops its children.
To keep them, return a corrected decomposition including its children and glue.

Missing library foundations are a reason to investigate or expose the next useful
mathematical layer. Do not invent children merely to spend attempts. Construction
layers should connect primitive operations, invariants, assembly, correctness and
cost bounds for the same objects, including composition overhead. Analytic claims
need domains, constants and endpoint/equality cases. Finite certificates need
existence or construction, sound verification and coverage of all required cases.
Defining a property does not establish a witness with that property. Separate
existence and properties of the same witness; instantiate it in the final glue.
Use direct proofs and suitable automation for localized arguments. For excessive
unfolding during elaboration, consider local transparency control or intermediate
equalities before repeating the same repair. A simplified probe may miss the issue.

Use candidate for joint targets, with target entries in source order and any
shared lemma entries. Use null scope_target/statement for targets. Unproved lemma
entries have null proof_body and a concrete English proof plan. Dependencies name
explicit parameters and entries; submission is current for this action or an
advertised reference (including base only when offered in INTERFACES). The
application binds identifiers and arranges independent reviews.
Every required target needs a complete term, including all binders. Earlier outputs
are usable only through explicit dependencies in source order; never assume later
outputs. Definition outputs need values and theorem outputs need proofs about the
same chosen objects. Preserve monomorphic values across uses and universe instances.
No joint output is committed until the whole group checks.
Use revise only for exact displayed fragment names. Null old replaces a whole
fragment; otherwise old must match exactly once. Structural changes require a
complete new proof, decomposition or candidate.

Helpers continue this task with advisory evidence. Use search to discover Lean
declaration names. Use lookup with names to check declarations and obtain their
types; inspect=true also requests definition bodies and available authored source.
For a named definition in the goal whose meaning you need, use lookup with
inspect=true. Lookup takes Lean names and needs no prior evidence result ID.
Use retrieve to combine searches and checked lookup.
Backend names are frozen-source and leanexplore; only frozen-source uses prefixes.
Use the displayed frozen-source roots, including project when offered, or a file,
directory or module within them. project searches the frozen project sources.
Frozen-source search returns up to 50 results, with source details for the first
12 and names for the rest. Exact file/module prefixes give those files a ranking
preference within the selected scope. Use lookup on a later name for its type,
or inspect=true for authored source. Large excerpts may continue on another page.
Queries require backends, and names-only retrieval uses an empty backends array.
inspect requests authored source; discovery alone does not certify availability.
Probe cases run in the frozen checked target context, without proposed children or
definitions. Supply one through three cases, each containing only id and code.
Each id is distinct, begins with a letter, then letters/digits/underscore/hyphen,
at most 48 characters. code is ONLY the Lean TacticM do-block body after run_tac do.
The application adds run_tac do and indents your body automatically.
Do not include by, run_tac, run_elab, or an outer do. Do not supply operation,
setup or expected. Example code:
let env ← Lean.getEnv
Lean.logInfo m!"{(env.find? `Nat.add).isSome}"
To run proof tactics in the body, use Lean.Elab.Tactic.evalTactic with quoted
tactic syntax, for example Lean.Elab.Tactic.evalTactic (← `(tactic| intro C)).
The generated code, including wrapper and indentation, must fit 16000 UTF-8 bytes.
Each case starts independently at the closed goal: introduce target binders
explicitly when needed. Diagnostics refer to generated code (body starts at line 2).
A successful probe is advisory: submit the complete proof for ordinary checking.
Resource interruptions and missing names do not establish mathematical failure.
Use research only when enabled in Helper modes, for a mathematical question and
at most six public source URLs. Use lookup to inspect the local Lean context.
Use recall to retrieve a saved result's first page when it is outside the current
prompt: copy the offered result ID exactly into result_id.
Use page with an offered cursor to retrieve another page outside this prompt.
Evidence pages remain advisory. Failed tools do not establish that a mathematical
claim is false.

memory_update is null or concise strategy/lessons and evidence pins to retain.
Pins may use result IDs shown in tool result sections or offered in CAPABILITIES.
Use displayed lesson IDs to update or forget old lessons. Retain useful names,
approaches tried and why they failed, unresolved work, and conditions for revisiting
a route. Consolidate lessons; exact candidate proofs and child guidance stay in
the candidate. Omission and eviction notices mean some history is unavailable.
For missing definitions or theorem interfaces, use an enabled lookup or search
that can obtain them and read its result. Explain proof departures from any
assigned English argument; never silently replace the task.
""".encode() + CONTEXT_RULES


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
        action('probe', cases=dict(array(obj(id=string(48), code=dict(string(16000),
               description='Only the TacticM do-block body after run_tac do; omit the wrapper.')), 3), minItems=1)),
        action('research', question=string(8000), urls=array(string(8000), 6)),
        action('recall', result_id=string(256)),
        action('page', cursor=string(4096)),
    ]
    return dict(obj(action={'anyOf': actions}, memory_update=nullable(ref('memory'))), **{'$defs': definitions})


HELPER_MODES = ('search', 'lookup', 'retrieve', 'probe', 'research', 'recall', 'page')
SCHEMA = schema_bytes(schema())


@lru_cache(maxsize=64)
def action_schema(modes):
    """Small cached schema variants; strategy supplies the available actions."""
    value = schema()
    actions = value['properties']['action']['anyOf']
    known = {action['properties']['mode']['enum'][0] for action in actions}
    if not modes or len(set(modes)) != len(modes) or not set(modes) <= known:
        raise DomainError('Invalid available prover actions')
    value['properties']['action']['anyOf'] = [action for action in actions
        if action['properties']['mode']['enum'][0] in modes]
    return schema_bytes(value)


def render(snapshot):
    if not any(s.kind == 'subject' for s in snapshot.sections):
        raise DomainError('Prover requires an exact subject')
    sections = display(snapshot, ('source', 'definitions', 'subject', 'interfaces',
                       'evidence', 'history', 'candidate', 'memory', 'feedback', 'capabilities', 'question'))
    name = 'next-action'
    names = {section.name for section in sections}
    while name in names:
        name += '-current'
    # Keep changing allowances with the final action request so earlier messages
    # remain a reusable prefix when only capabilities change.
    capabilities = tuple(b'CAPABILITIES '+s.name.encode()+b'\n'+s.text
                         for s in sections if s.kind == 'capabilities')
    sections = tuple(s for s in sections if s.kind != 'capabilities')
    sections += (Section('question', name, b'\n\n'.join((*capabilities, b'Choose the next action for this turn. '
        b'The application will execute a helper action and supply its result on the next turn. '
        b'If lookup is enabled and you need definitions of named constants in this goal, return a lookup action with '
        b'those names and inspect=true. Use the enabled helpers to obtain missing context. '
        b'Return the action JSON now.'))),)
    return Prompt('prover', 'prover-v3', 'prover-action-v2', INSTRUCTIONS, SCHEMA, sections)
