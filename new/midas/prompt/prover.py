"""Default prover strategy. Replace render to change wording, schema or layout."""
from functools import lru_cache

from .protocol import (CONTEXT_RULES, DomainError, Prompt, Section, array, display, nullable,
                       obj, schema_bytes, string)

INSTRUCTIONS = """TASK AND RESPONSE
Solve the exact displayed Lean task across turns, covering every required target
in source order. Return one JSON object with action and memory_update.
- Choose an enabled action from CAPABILITIES before filling its fields.
- Give every action a nonempty rationale: a concise, actionable reason, not a
  reasoning transcript.
- A helper action is the complete reply for this turn. The application executes
  it and returns results in a later request. Obtain missing context as needed.
- Use displayed tool results directly. Their names, such as tool-result-1, are
  stable. Delivered evidence is retained; omission notices identify pages outside
  this prompt.

PROOF REQUIREMENTS
- Submit complete Lean terms in proof_body, preserving all binders, universe
  signatures and checked dependency aliases. Use the supplied notation and definitions.
- Do not use sorry, add axioms, change the source or assumptions, or emit top-level
  commands. Imports must be advertised prepared modules, never commands in proof_body.
- Never assume the local goal, an ancestor or an unlisted sibling. Background
  source and English plans add no hypotheses.
- Definitions are ordered named def/abbrev declarations with bodies. They may use
  earlier definitions but cannot assume child theorems.
- Explain departures from an assigned English argument in proof_departures;
  never silently replace the task.
- Helper results, including successful probes, are advisory. Submit a complete
  proof for ordinary checking.

ACTION: proof
Submit a complete proof of one goal directly for checking; no preliminary probe
is needed. Failed checks return compiler diagnostics. Correct the proof if the
action remains available.
A new proof action replaces any proposed decomposition and drops its children.
To retain them, submit a corrected decomposition with its children and glue.

ACTION: decomposition
Propose useful intermediate lemmas for one goal and complete conditional glue
in proof_body. For each child, supply:
- An explicit statement and depends_on names. Only declared prerequisites are
  available; parent hypotheses are not inherited. State every needed hypothesis
  or derive it from the allowed context.
- A complete Lean term in proof_body, or null proof_body with English guidance in
  proof (empty when unavailable). Failed child Lean checks return diagnostics.
- When guidance is available, a credible next step with supported constructions,
  lemmas, tactics, failed routes and explicit remaining gaps. This guidance
  accompanies the child in later work.
Each child must be provable under its exact statement and selected dependencies.
Apply the proof strategy below when choosing boundaries.

ACTION: candidate
Use for joint targets with target entries in source order and any shared lemma
entries, or to compose a proof of the current supporting lemma.
- Existing subjects: use the exact name from SUBJECT required-subjects, with null
  scope_target and statement. The application supplies kind, scope and immutable statement.
- New names: each introduces a supporting lemma and requires a statement.
  scope_target may be null for a single source scope; otherwise use the exact target
  name whose source context it uses. Unproved lemmas have null proof_body and
  English guidance in proof.
- Dependencies: name explicit parameters and entries. submission is current for
  this action or an advertised reference; base is allowed only when offered in INTERFACES.
- Required targets: supply complete terms including all binders. Use earlier
  outputs only through explicit dependencies in source order; never assume later outputs.
- Definition outputs need values; theorem outputs need proofs about those same
  objects. Preserve monomorphic values across uses and universe instances.
The application binds identifiers and reviews compositions proposing English-guided
children. No joint output is committed until the whole group checks.

ACTION: revise
Use only when enabled, for exact displayed fragment paths. Each edit is {path, old, new}:
- /entries/<child>/proof edits the Lean term submitted as proof_body.
- /children/<child>/proof edits the English guidance submitted as proof.
Copy the advertised path using the displayed child entry key. For guidance, new
is replacement English text; "" clears it. Null old replaces the whole fragment;
otherwise old must match exactly once in the displayed candidate.
Compiled-successful Lean proofs are retained without editable proof fragments.
Edits to paths that are not advertised are ignored; other edits still apply.
All edits match the same candidate before replacement, regardless of edit order.
Overlapping edits are rejected, including a whole-fragment replacement combined
with another edit to that fragment. Guidance and Lean terms may be edited separately
in the same edits list.
To hand a supplied child to a new prover context, set old and new to null for its
/entries/<child>/proof fragment. Existing guidance is retained; missing guidance
defaults to "". Add or update guidance in the same revision if needed.
Supplying Lean for an English-guided child or changing other structure requires
a complete new candidate.

PROOF STRATEGY
- Choose decomposition boundaries that remove distinct mathematical obstacles.
  Explain what each child resolves and what remains for the hardest child. A
  dominant hard child is acceptable when supporting lemmas remove concrete obstacles.
  Weigh that benefit against context, review, duplicated work and assembly costs.
- A stronger lemma can unlock a method; renaming the task alone is not progress.
  No failed direct attempt, equal difficulty or fixed child count is required.
  Hard children may decompose again. Do not invent children merely to spend attempts.
- Where required by the target, account for construction invariants, correctness
  and cost bounds (including composition overhead), analytic domains, constants,
  endpoint/equality cases, and finite-certificate existence, verification and coverage.
- Defining a property does not establish a witness. Prove existence and the required
  properties for the same object, then instantiate it in the final glue.
- Use direct proofs and suitable automation for localized arguments. For excessive
  unfolding during elaboration, consider local transparency control or intermediate
  equalities before repeating a repair. A simplified probe may miss the issue.

ACTION: retrieve
Discover and check Lean declarations. If enabled and you need the meaning of a
named constant in the goal, your next action must retrieve that Lean name. Use
retrieve for missing definitions or theorem interfaces; imports do not display them.
- Supply up to three queries and two backends for discovery, and up to twelve
  known names for direct inspection. Known names need no prior result ID.
- Queries require backends. For names-only retrieval, set queries and backends
  to []. If queries and names are both empty, retrieval is disabled for this node
  and proof work continues without a lookup.
- frozen-source accepts prefixes within the displayed roots: a root, file,
  directory or module. [] searches all frozen roots; project searches frozen
  project sources when offered. Exact file/module prefixes receive ranking preference
  within the selected scope. leanexplore requires prefixes: [].
Results:
- Discovery hits are merged and ranked; the top twelve names are checked. Explicit
  names appear as standalone lookups; checked hits appear in numbered search results.
  Inspect other hits by name in a later retrieve action, optionally with new queries.
- Retrieval returns checked types and available authored definition source.
  Theorems return checked signatures without proof bodies. Discovery alone does
  not certify availability.
- Frozen-source returns up to 50 hits: source details for the first 12, names for
  the rest. Large excerpts may continue on another page. Local queries that are
  exact Lean identifiers also receive direct checked lookups, including generated
  declarations, in that search section.
- Search omits declarations whose type or definition was shown in a prover prompt
  or earlier in this retrieval; names alone do not count. Omitted hits are not
  replaced, so fewer than 50 may remain. Explicit name lookups can request them again.
Example (replace Nat.Prime with the name needed for your task):
{"action":{"mode":"retrieve","rationale":"Inspect the definition used in the goal.","queries":[],"names":["Nat.Prime"],"backends":[]},"memory_update":null}

ACTION: probe
Run one to three independent cases in the frozen checked target context, without
proposed children or definitions. Each case contains only code: the Lean TacticM
do-block body after run_tac do. The application adds the wrapper and indentation.
Do not wrap code in by, run_tac, run_elab or do, or supply operation, setup or expected.
Example code:
let env ← Lean.getEnv
Lean.logInfo m!"{(env.find? `Nat.add).isSome}"
To run proof tactics, use quoted tactic syntax:
Lean.Elab.Tactic.evalTactic (← `(tactic| intro C))
Each case starts at the closed goal; introduce target binders explicitly as needed.
Generated code, including wrapper and indentation, must fit 16000 UTF-8 bytes.
Diagnostics refer to generated code (body starts at line 2).

OTHER HELPERS
- research: ask a precise question for mathematical guidance, proof strategies,
  explanations or obstacle diagnosis. The researcher does not browse.
- web_search: find resources and references with a question and at most six public
  source URLs. Results include source locations and hypotheses. Apply the references
  or ask the researcher how to use them.
- page: read an additional page outside this prompt using an offered cursor only.
  For example, tool-result-1:2 requests page 2 of tool-result-1.

ACTION: stop
Use when you believe this exact node cannot be proved. Explain why in rationale,
including the best next decomposition layer considered and its actual obstacle
or exhausted resource. Assess precise claims, complete conditional glue and a
concrete mathematical advance before stopping. Missing library support, an
unfinished implementation or incomplete descendant proofs alone are insufficient;
hard children can decompose again.
Difficulty, resource interruptions, missing names and failed tools alone do not
establish falsity or unprovability. For a suspected false statement, missing
assumption or context mismatch, include objection with category and concrete
mathematical evidence about the exact statement; otherwise use null objection.
Exhaustion, resource failures and lack of progress are not statement objections.

MEMORY
memory_update is null or concise strategy, lessons and evidence pins to retain.
- Pin result IDs shown in tool results or offered in CAPABILITIES.
- Use displayed lesson IDs to update or forget lessons. Retain useful names,
  attempted approaches and why they failed, unresolved work and conditions for
  revisiting a route. Consolidate lessons; keep exact proofs and child guidance
  in the candidate.
- Omission and eviction notices mean some history is unavailable.

CONTEXT
""".encode() + CONTEXT_RULES


def schema():
    text, code = string(), string(1024 * 1024)
    ref = lambda name: {'$ref': '#/$defs/' + name}
    definitions = {
        'definition': obj(name=string(256), kind={'enum': ['def', 'abbrev'], 'type': 'string'},
                          type=code, value=code),
        'child': obj(name=string(256), statement=code, proof_body=nullable(code), proof=text, usefulness=text,
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
        action('revise', edits=array(obj(path=string(1024), old=nullable(code), new=nullable(code)), 128)),
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
    return Prompt('prover', 'prover-v13', 'prover-action-v5', INSTRUCTIONS, SCHEMA, sections)
