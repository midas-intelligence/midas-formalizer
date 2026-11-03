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
Tool result sections identify stable names such as tool-result-1 and pages already displayed; use their
contents directly. Delivered evidence is retained, subject to the configured
input-token budget. Omission notices identify when older pages are outside this
prompt. If a helper is disabled, choose an enabled helper or work on the proof
directly.
Every action needs a nonempty rationale explaining what it will accomplish.
Give a concise actionable reason, not a reasoning transcript.

Use proof to submit a complete candidate proof for one goal directly for checking.
A failed proof check returns compiler diagnostics; you can submit a correction
if the action remains available. No preliminary probe is needed.
Use decomposition for one goal with useful intermediate
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

Missing library support, an end-to-end implementation, or completed descendant
proofs is not by itself a reason to stop. Assess a substantive next decomposition
layer with precise claims, complete conditional glue, and a concrete mathematical
advance. Difficult children can decompose again. Do not invent children merely
to spend attempts. When required
by the target, account for construction invariants, correctness and cost bounds
(including composition overhead), analytic domains, constants and endpoint/equality
cases, or finite-certificate existence, verification and coverage.
Defining a property does not establish a witness with that property. When the target
requires a witness, establish its existence and required properties for the same
object; instantiate it in the final glue.
Use direct proofs and suitable automation for localized arguments. For excessive
unfolding during elaboration, consider local transparency control or intermediate
equalities before repeating the same repair. A simplified probe may miss the issue.

Use candidate for joint targets, with target entries in source order and any
shared lemma entries, or to compose a proof of the current supporting lemma.
Identify each existing subject by its exact name in SUBJECT required-subjects; the application
supplies its kind, source scope and immutable statement. Use null scope_target
and statement for existing subjects. Every new name introduces a supporting lemma
and requires its statement. Its scope_target may be null when there is only one
source scope; otherwise select the exact target name whose source context it uses.
Unproved lemma
entries have null proof_body and a concrete English proof plan. Dependencies name
explicit parameters and entries; submission is current for this action or an
advertised reference (including base only when offered in INTERFACES). The
application binds identifiers and arranges independent reviews.
Every required target needs a complete term, including all binders. Earlier outputs
are usable only through explicit dependencies in source order; never assume later
outputs. Definition outputs need values and theorem outputs need proofs about the
same chosen objects. Preserve monomorphic values across uses and universe instances.
No joint output is committed until the whole group checks.
Use revise only when CAPABILITIES marks it available, and only for exact displayed
fragment names. Null old replaces a whole fragment; otherwise old must match
exactly once. Structural changes require a complete new proof, decomposition or
candidate.

Helper results, including successful probes and evidence pages, are advisory;
submit the complete proof for ordinary checking. Difficulty, resource interruptions,
missing names or failed tools alone do not establish falsity or unprovability.

Use retrieve to discover and check Lean declarations in one action, then read its
result. If retrieve is enabled and you need the meaning of a named constant in the
goal, your next action is retrieve with its Lean name. Use it for missing
definitions or theorem interfaces; imports do not display their definitions.
Supply up to three queries and two backends for discovery, up to twelve known names
for direct inspection, or both together.
Discovery results are merged and ranked, then the top twelve names are checked
alongside your explicit names. Explicit names appear as standalone lookup results;
checked search hits appear within their numbered search results. To inspect other
results, include their names in a later retrieve action, optionally with new queries.
Known names need no prior result ID.
Retrieval returns checked types and available authored definition source.
Theorems return checked signatures without proof bodies.
Backend names are frozen-source and leanexplore. frozen-source accepts optional
prefixes; an empty array searches all frozen roots. leanexplore requires an empty
prefixes array. For scoped search, use the displayed frozen-source roots, including
project when offered, or a file, directory or module within them.
project searches the frozen project sources.
Frozen-source search returns up to 50 results, with source details for the first
12 and names for the rest. Exact file/module prefixes give those files a ranking
preference within the selected scope. Large excerpts may continue on another page.
Local queries that are exact Lean identifiers also get a direct checked lookup;
resolved declarations appear in that search section, including generated declarations.
Queries require backends. For names-only retrieval, use "queries":[] and
"backends":[]. If both queries and names are empty, retrieval is disabled for
this node and proof work continues without a lookup.
For example (replace Nat.Prime with the actual name from your task):
{"action":{"mode":"retrieve","rationale":"Inspect the definition used in the goal.","queries":[],"names":["Nat.Prime"],"backends":[]},"memory_update":null}
Discovery alone does not certify availability.

Probe cases run in the frozen checked target context, without proposed children or
definitions. Supply one through three cases, each containing only code.
code is ONLY the Lean TacticM do-block body after run_tac do.
The application adds run_tac do and indents your body automatically.
Do not wrap the body in by, run_tac, run_elab, or do. Do not supply operation,
setup or expected. Example code:
let env ← Lean.getEnv
Lean.logInfo m!"{(env.find? `Nat.add).isSome}"
To run proof tactics in the body, use Lean.Elab.Tactic.evalTactic with quoted
tactic syntax, for example Lean.Elab.Tactic.evalTactic (← `(tactic| intro C)).
The generated code, including wrapper and indentation, must fit 16000 UTF-8 bytes.
Each case starts independently at the closed goal: introduce target binders
explicitly when needed. Diagnostics refer to generated code (body starts at line 2).
Use research when enabled for mathematical guidance, proof strategies, explanations
or diagnosing an obstacle. Supply a precise question; the researcher does not browse.
Use web_search when enabled to find resources and references, with a question and
at most six public source URLs. Its results include source locations and hypotheses.
Use returned references in your reasoning or ask the researcher how to apply them.
Use page only with an offered cursor to read an additional page outside this prompt.
For example, tool-result-1:2 requests page 2 of tool-result-1.

Use stop when you believe this exact node cannot be proved. Explain why in
rationale. Before stopping, identify the best next decomposition layer considered
and its actual obstacle or exhausted resource. Missing library lemmas or an
unfinished implementation alone do not establish such an obstacle.
For a suspected false statement, missing assumption or context mismatch, include
an objection with category and concrete mathematical evidence about the exact
statement; otherwise use null objection. Exhaustion, resource failures and lack
of progress are not statement objections.

memory_update is null or concise strategy/lessons and evidence pins to retain.
Pins may use result IDs shown in tool result sections or offered in CAPABILITIES.
Use displayed lesson IDs to update or forget old lessons. Retain useful names,
approaches tried and why they failed, unresolved work, and conditions for revisiting
a route. Consolidate lessons; exact candidate proofs and child guidance stay in
the candidate. Omission and eviction notices mean some history is unavailable.
Explain proof departures from any assigned English argument; never silently
replace the task.
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
        'entry': obj(key=string(256), name=string(1024),
                     scope_target=nullable(string(1024)), statement=nullable(code),
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
        action('stop', objection=nullable(obj(category={'type': 'string', 'enum':
            ['suspected_false', 'missing_assumption', 'context_mismatch']}, evidence=text))),
        action('retrieve', queries=array(string(2048), 3), names=array(string(1024), 12),
               backends=array(ref('backend'), 2)),
        action('probe', cases=dict(array(obj(code=dict(string(16000),
               description='Only the TacticM do-block body after run_tac do; omit the wrapper.')), 3), minItems=1)),
        action('research', question=string(8000)),
        action('web_search', question=string(8000), urls=array(string(8000), 6)),
        action('page', cursor=string(4096)),
    ]
    return dict(obj(action={'anyOf': actions}, memory_update=nullable(ref('memory'))), **{'$defs': definitions})


HELPER_MODES = ('retrieve', 'probe', 'research', 'web_search', 'page')
SCHEMA = schema_bytes(schema())


@lru_cache(maxsize=4)
def action_schema(research, web_search):
    """Helper configuration selects the schema; turn allowances do not."""
    if type(research) is not bool or type(web_search) is not bool:
        raise DomainError('Expected research and web-search configuration')
    if research and web_search:
        return SCHEMA
    value = schema()
    disabled = {name for name, enabled in (('research', research), ('web_search', web_search)) if not enabled}
    value['properties']['action']['anyOf'] = [action for action in value['properties']['action']['anyOf']
        if action['properties']['mode']['enum'][0] not in disabled]
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
        b'Return the action JSON now.'))),)
    return Prompt('prover', 'prover-v10', 'prover-action-v3', INSTRUCTIONS, SCHEMA, sections)
