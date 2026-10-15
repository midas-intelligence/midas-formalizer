# Clean Midas: domain, policy, source, executor, environment, checker, and provider

`midas/policy` now implements the policy layer on top of the domain. All policy
settings are defined in `midas/policy/config.py` and loaded from one shallow JSON
document. [`config.json`](config.json) contains the policy defaults and the
prover/reviewer model settings from the supplied v2 `live.json` example.

`midas/source` implements the pure source layer: names, explicit declaration and
lookup contexts, shared rendering, byte edits, source maps, and flat module layouts.
It adds no persisted formats or JSON files.

`midas/executor` implements reserved worker dispatch, isolated subprocesses,
cancellation, and retained compiler lifetimes. It consumes explicit launch recipes
and imports only the domain and the standard library.

`midas/environment` implements frozen packages, toolchain admission and relocation,
dependency provenance/module inventories, source replay, library attachment, and
trusted benchmark archives. It imports only domain, source, and executor.

`midas/checker` implements source inspection, binding and proof checks, checked
lookup, probes, whole-source kernel auditing, and compiler receipt admission.
It uses the existing environment and executor services.

`midas/provider` implements frozen model routing, one invocation, bounded transport
parsing, delivery/usage reporting and authenticated model receipts. It imports only
domain and executor.

## Policy

There are two implementation modules plus `__init__.py`:

| New module | Responsibility |
| --- | --- |
| `policy/config.py` | Run, scheduling, mathematics, helpers, process, export, model routes and limits, research, recovery and price settings; validation, bounded JSON loading, policy identity and prospective limit increases |
| `policy/policy.py` | Admission, helper capabilities, candidate/revision/backtrack eligibility, backlog hysteresis, provider gates, settlement, token pricing, retry decisions, export residual budgets and completion |

These consolidate the policy responsibilities previously spread through
`export_limits`, `live_config`, `live_limits`, `model_pricing`, `scheduling`,
`strategy_allowances` and `strategy_capabilities`. Process/model/recovery limit
definitions also live in the same config module. The preserved v2 files are not
imported by the new implementation.

Config sections contain scalar values, with one fixed `prices` object inside each
model role. Input is capped at **16 KiB** and nesting at **three objects**, checked
before JSON parsing. Duplicate keys, unknown settings, booleans
where integers are expected, nonfinite numbers and inconsistent limits fail
validation. Large source, request, result and environment payloads are never
part of config. Provider/model choices and non-secret binding references live in
this file. Client construction, credential resolution, supported provider/transport
combinations and request validation belong to `midas/provider`; application
constructs the configured transport bindings. Environment admission lives in `midas/environment`.

For example, application code can read one config with a bounded read:

```python
from midas.policy import Policy, load_config
from midas.policy.config import MAX_CONFIG_BYTES

with open("config.json", "rb") as source:
    policy = Policy(load_config(source.read(MAX_CONFIG_BYTES + 1)))
```

`prover`, `reviewer` and `research` each have a `ModelRoute` section with
`provider`, `model`, `transport`, `account`, `endpoint`, `credential_env`,
`prompt_cache` and `prices`. The checked-in example configures the prover and
reviewer from the supplied `live.json`; research remains unconfigured. `Config()`
and omitted roles remain unconfigured by default. Set both `provider` and `model`
to configure a role; the other fields are optional. Identifiers are preserved
exactly. For example, the reviewer settings are:

```json
{
  "format": "midas-config-v1",
  "reviewer": {
    "provider": "azure",
    "model": "gpt-6.1-sol",
    "transport": "azure_responses",
    "account": "midas-azure",
    "endpoint": "https://midas-openai.services.ai.azure.com/openai/v1/responses",
    "credential_env": "AZURE_OPENAI_API_KEY",
    "prompt_cache": "explicit",
    "prices": {
      "input": 2000000,
      "cached_input": 100000,
      "output": 10000000,
      "cache_write": 2500000
    }
  }
}
```

`credential_env` stores the environment variable **name**; config loading does
not read credentials or contact the provider. Endpoints accept HTTP(S) URLs
without embedded credentials, queries or fragments; the provider may enforce
stricter transport requirements. The `model` and `research_model` sections still
hold invocation limits. Each role owns its prices (`config.reviewer.prices`, for
example); there are no separate top-level price sections. `prompt_cache` may be
`explicit`, `disabled` or null. Route and price changes affect the config identity
and cannot be applied through
`increase`, which only permits the existing prospective limit changes.

Omitted settings use defaults. Omitted backlog stop thresholds scale with model
concurrency; explicit thresholds remain exact. Prices are integer **micro-USD per
million tokens**: the example's $2/$0.10/$10/$2.50 rates become
`2000000`/`100000`/`10000000`/`2500000`. A null or omitted `prices` means price
is unknown, while an explicit zero rate means free. Cache-write pricing is
optional; explicit write pricing requires write telemetry when it affects cost.

`Policy` and `Config` are immutable. `config.increase(...)` produces a new snapshot
for prospective run/mathematical increases. It does not reset usage, move the
run's time origin, enlarge existing repair grants or rewrite frozen requests.
Storage/controller must persist the change with its audit identity and expected
version. Recovery rules passed to `retry` come from the **saved request**, even
when current run limits have increased.

Policy consumes explicit, bounded values. `RunUsage` and `OwnerUsage` contain
consumed plus reserved units, so simultaneous admissions cannot overbook budgets
when the controller rechecks and commits them in one transaction. Run budget
totals use confirmed final usage, or retained upper bounds for unknown usage;
unbounded unknowns remain `None`. Any reported bound overrun sets
`RunUsage.bound_exceeded`. No policy decision queries SQL or scans run history.

`Capacity` includes prepared reservations and active processes. Compiler checks,
lookups, probes, assembly, integration and export verification share one compiler
pool, including retained worker memory. A retained worker being reused is removed
from the retained-memory projection and its full footprint reserved by the job.
`tick` advances explicit backlog hysteresis once per controller tick; callers pass
that same `BacklogState` to each admission in the tick.

Candidate and helper fields on `AdmissionRequest` reserve a logical allowance at
its initial admission. Later jobs belonging to that already-reserved candidate or
helper leave those fields unset. Probe units count cases. Research requests that
were sent or have uncertain delivery remain charged; never-sent abandoned
requests can release their logical reservation. Storage owns those lifetime
totals and atomically records candidate decisions using `mathematical_charge`.
Success and mathematical rejection consume attempts; cancellation, staleness,
infrastructure abandonment and separately charged exceptions do not.

`settle` verifies exact invocation/request bindings and returns a ledger change.
An exact duplicate returns `None`; conflicting reports fail. Only confirmed
never-sent, terminated work refunds a model turn. Unknown usage stays unknown,
with reservation bounds retained for admission. Unconfirmed termination retains
capacity and usage bounds. The controller must validate receipt seals and saved
reservation bytes before invoking policy, persist settlements once per original
execution, and obtain termination confirmation from the executor.

`retry` distinguishes transport/rate-limit failures, uncertain delivery, explicit
cancellation/resume, local replay and mathematical/protocol revisions. It returns
charges and an earliest time without sleeping. Provider failure-window counts
are supplied by storage; provider gate decisions likewise do not own timers or
workers. Export budgets consume explicit elapsed/output/artifact totals, replacing
the old implicit context-variable budget. Workers still enforce limits while
streaming and before publishing artifacts.

`completion` separates proof, integration and independent export verification.
Only verified export with no active work or outstanding recovery yields exit 0.
Running/waiting has no terminal exit code; blocked/incomplete/finalization failure
uses 1, setup/recovery uses 2, and explicit stop uses 130. These are new interfaces,
not v2 persisted-format compatibility requirements.

Validation: **22 distinct Python policy tests passed**, including targeted reruns
after tightening accounting and retry bindings. No Lean or external-service tests
were run. The local cache-aware pricing comparison measured **0.115× v2 time**
(about 0.417 μs versus 3.611 μs per call), using seven alternating samples of
10,000 operations. It covers pricing, not end-to-end formalization. Admission
uses constant-size snapshots and constructs resource profiles once per policy.

For the provider/model configuration addition, **8 targeted configuration tests
passed**, including 3 new cases covering per-role settings, default-file loading,
round trips, config identities and malformed routes. Unchanged policy test results
were retained; no provider or Lean invocation was needed.

For grouping prices into model roles, **14 targeted configuration and accounting
tests passed**. They cover nested price round trips, immutable rates, unknown
versus zero cost, rejection of the removed top-level price sections, and bounded
parsing. The example's model settings and converted rates were checked against
the supplied `live.json`; no provider or Lean invocation was needed.

```sh
python3 -B -m unittest tests.test_policy -v
python3 -B -m tests.benchmark_policy
```

## Domain

The domain responsibilities of the 11 modules in the
[cleanup blueprint](../docs/cleanup-blueprint.md) are grouped under
`midas/domain`. Related responsibilities are consolidated into **seven modules
plus `__init__.py`**, with no compatibility wrappers or imports from the original
`../midas_formalizer` folder. That folder remains unchanged.

| Original module | New owner | Implemented domain contract |
| --- | --- | --- |
| `certificate_codec` | `encoding` | Canonical binary DAG encoding/decoding, shared nodes, bounded tree projection |
| `dependency_interfaces` | `identity` | Bounded interfaces; exact signatures, aliases, environments, and frozen peer order |
| `formats` | `encoding` | Binary format identifiers and archive layout constants |
| `identity` | `identity` | Structural Lean signatures, binders, universe scopes, local/declaration contexts, envelope/reference loading and hashing |
| `joint_targets` | `proofs` | Group/coordinator selection, complete ordered submissions, scoped definition certificates, atomic root acceptance |
| `persisted` | `encoding` | Small JSON metadata, shallow binary records, duplicate/nonfinite/Unicode rejection |
| `proof_state` | `proofs`, `records` | Proof selection, reviews, exact assembly, generation fences, cycle rejection, retirement, aliases and stable target/review identities |
| `scoped_submission` | `submission` | Separate statement/proof definition prefixes, lazy entry resolution, subject matching and inherited prerequisite scope |
| `selection` | `selection` | Source paths/spans, UTF-8 boundaries, prerequisite closure, exact inventory/signature agreement, frozen selections |
| `serialization_cache` | `cache` | Byte/entry-bounded LRU admission, concurrent build coalescing, failure/reset/environment/fork behavior |
| `submission` | `submission` | Binary candidate persistence, direct validation, revisions, imports, ordered dependencies, reviews, scope bounds and local cycle rejection |

`records` also holds immutable artifact, owner, session, usage, candidate,
job/request/result, and receipt-reference values. Unknown usage stays unknown.
`ProofGraph` indexes owners for constant-time lookup and traverses dependencies
iteratively, including deep graphs and shared subproofs.

## Boundaries

Domain functions consume values and propose new values. They do not open files,
execute SQL, schedule work, invoke Lean, contact providers, or render source.
SQL, receipt fetching, transaction handling, and allowance settlement formerly
inside `proof_state` and `joint_targets` belong to the future storage/controller
components. File reading formerly in `submission` likewise belongs to callers.

Environment admission belongs to the environment service. It supplies an admitted
`env_…` hash and explicit authorized source bindings (`SelectionInputs`); manifests,
toolchains, and package inventories stay outside the domain. The domain checks the
source bytes against those bindings and checks persisted environment hashes against
the supplied hash.

`freeze_selection` consumes immutable declarations and `Signature` values containing
binary graph bytes. It validates these values directly and retains their references.
It neither serializes a selection nor calls its loader. `encode_selection` explicitly
produces saved bytes; `load_selection` decodes once and uses the same typed admission.
Freezing checks the aggregate field size without allocating a saved envelope; the
selection ID hashes that field stream lazily. Shared signature nodes are visited
once, and each selected peer interface is encoded once for reuse across roots.

`validate_submission` checks supplied records and their total size directly, without
building binary headers or serializing through JSON. `encode_submission` saves them
explicitly; `load_submission` accepts only the binary format. Proofs, statements, and
definition text remain raw UTF-8 fields. The two record kinds are `midas-candidates`
and `midas-scoped-candidates`; both retain the existing entry and scope semantics.

JSON is reserved for metadata with a hard 16 KiB cap. Serialization checks the full
projection budget before calling JSON; loading checks bytes and a depth limit of 16
before parsing. Callers cannot raise the byte cap. Generic JSON normalization and
JSON graph/embedded-evidence fallbacks have been removed.

Context namespace hashes process the source scope once and each definition once,
using length-prefixed UTF-8 fields. Earlier prefix hashes stay stable when later
definitions are appended. Review hashes likewise process raw review and interface
bytes with field lengths, without hex expansion or a combined JSON envelope.

`AdmittedBinding`, `AdmittedCheck`, and `AdmittedReview` are trusted internal
handoffs. Checker/export services must validate controller-issued receipts,
seals, exact requests/results, and Lean evidence before constructing them. Python
constructors check structure, not authenticity. There is no public wire decoder
that turns arbitrary worker output into proof authority.

`accept_binding` records an interface without proving it. `accept_checked_proof`
selects a direct proof or a reviewed conditional plan. Verified children still
require `accept_assembly` with their exact current proof receipts. Joint candidates
enter only through `accept_joint`, which produces one complete replacement.

Graph replacements must commit against the original revision.
`retire_plan` returns the expected revision, replacement graph, and old owner
generations to cancel **after commit**. Only newly unreachable owners are fenced;
shared reachable work survives. Retired candidates cannot reactivate, and affected
ancestor assemblies return to conditional state for later reassembly.

Proof verification is separate from source integration and independent export
verification. This directory contains domain, policy, source, executor, and environment components, not a runnable
formalizer or an implementation of the remaining blueprint components.

## Formats and validation

V2 compatibility is not a requirement. Contexts now use `declaration-context-v2`,
and context namespace hashes and review digests change. Identities containing those
contexts change as well. The job/graph records are in-memory interfaces; this work
does not introduce or migrate a saved-run database schema.

Saved selections now use **`midas-selection-v3` binary**, so selection bytes and
selection IDs change. The format begins with `MIDASSEL` and byte `0x03`, followed by
flat fields prefixed with unsigned little-endian 64-bit byte lengths. Source and
signature data remain raw bytes, without JSON or hex expansion. Structural constant
names retain bounded JSON metadata (16 KiB); signature name projections are bounded
before JSON encoding. The loader enforces the 64 MiB total limit, record counts,
canonical fields, source authorization, and optional expected identity. JSON
selections are rejected.

Saved submissions begin with `MIDASSUB` and byte `0x01`, followed by a flat token
stream. Tags identify null, booleans, integers, UTF-8 strings, lists, and maps;
lengths/counts use unsigned little-endian 64-bit integers. Map keys are unique and
sorted. The reader caps the file at 8 MiB, nesting at 8, and values at 100,000 before
admitting the shallow records. JSON submissions are rejected.

The fixture in `tests/fixtures/domain_v2.json` was generated using the preserved
v2 pure writers. It remains an independent example for unchanged graph and closed
identity encodings, not a compatibility requirement. Its synthetic environment is
not evidence of toolchain admission or Lean correctness.

Recorded validation on 2026-10-15: **47 distinct focused domain tests passed**
(21 foundation cases and 26 additional contract cases). The changed proof,
candidate, import, selection, and cache paths received targeted reruns; unchanged
results were retained. No Lean, packaging, integration, or storage-stress suites
were run.

For the selection construction fix, **13 affected existing tests and 9 new regressions
passed**. The universe-scope case received a further targeted rerun after its
comparison changed. Regressions cover direct construction without save/load calls,
hash-only environment inputs, a 4.4 MB source and 1 MB signature string without
large JSON operations, compact shared graphs, rejected oversized name projections,
shared source/signature validation, and malformed binary fields. Unchanged test
results were reused; no broad or Lean-related suites were run.

For the remaining JSON cleanup, **20 affected existing tests and 8 new regressions
passed**. Targeted reruns covered changes to byte/depth checks and binary size
measurement. New cases cover rejection before JSON is called, large proof/context
text, raw interface hashing, linear context hashing, exact binary size measurement,
aggregate limits, and malformed binary counts, keys, strings, and nesting. No Lean,
packaging, integration, or storage tests were run for this change.

For an intentional complete domain check, from this directory:

```sh
python3 -m unittest tests.test_domain tests.test_domain_contracts -v
```

For the local comparison with the preserved v2 code:

```sh
python3 -B -m tests.benchmark_domain
```

The benchmark alternates old/new order over seven samples of 300 operations.
Earlier measured new/v2 median ratios were 0.951 for scoped admission, 0.964
for scope resolution, and 0.990–1.016 for memo hits, DAG/JSON encoding or decoding,
identity framing, and target selection. These small domain workloads show
comparable performance; they do not measure end-to-end formalization.

The selection fix adds measurements of actual freezing and loading. On the small
two-target fixture, the new/v2 median ratios were **0.527 for freezing**, **0.545
for freezing plus serialization**, **0.626 for loading**, and **0.872 for
identity/reference construction**. The v2 baseline
receives pre-admitted synthetic environment/profile data through scoped test stubs;
these measurements exclude toolchain admission and I/O. V2 freezing includes saving
and loading internally, while new freezing separates persistence.

To measure only the changed paths:

```sh
python3 -B -m tests.benchmark_domain --case freeze_selection --case freeze_and_encode --case load_selection --case identity_envelope
```

After the remaining JSON cleanup, the measured new/v2 median ratios were:

| Operation | New/v2 time |
| --- | ---: |
| Submission validation | 0.912 |
| Submission loading | 0.735 |
| Submission saving with validation | 0.892 |
| Context prefix construction (32 definitions) | 0.187 |
| Entry scope resolution | 0.860 |
| Review digest (8 interfaces) | 0.699 |
| Cached small JSON metadata admission | 0.954 |

Cached metadata decoding uses its already-admitted UTF-8 encoding directly. Review
comparison uses the v2 digest recipe with its database row already supplied;
database access is excluded. These are local synthetic domain measurements, not
end-to-end throughput or a benchmark of large v2 JSON files.

```sh
python3 -B -m tests.benchmark_domain --case scoped_admission --case submission_load --case submission_save --case context_prefixes --case scope_resolution --case review_digest --case json_warm_admission
```

## Source

The source responsibilities of the blueprint's 15 v2 modules are consolidated
into **six modules plus `__init__.py`** under `midas/source`:

| New module | Responsibility |
| --- | --- |
| `names.py` | Structural Lean name spelling, exact alias queries, advisory lexical declaration hints |
| `text.py` | Raw UTF-8 source, bounded assembly, one-pass byte edits, authored source maps and coordinates |
| `terms.py` | Expression and universe rendering, Lean string escaping, explicit rendering options |
| `context.py` | Explicit declaration/lookup contexts, sequential definition rendering, import merging and insertion |
| `render.py` | Shared candidate/probe rendering, subject presentation, named proof declarations, result applications and in-place integration |
| `modules.py` | Flat module names, dependency order, reachability, shared modules and aggregate byte limits |

The public boundary uses existing binary `Signature` values, source bytes, and
small immutable records. Expression expansion stays private to rendering. There
are no source JSON files, per-type codecs, save/load round trips, manifests,
environment records, database queries, file operations, or compiler invocations.
The implementation imports only `domain`, its own modules, and the standard
library. The preserved v2 implementation remains unchanged.

`declaration_context` takes an admitted selection and explicit checked interfaces.
`lookup_context` retains only the requested checked aliases and never decodes the
goal or an alias type. These contexts reference the original source bytes and a
prefix offset. Checker/controller own receipt provenance, exact binding selection,
repair ancestry and environment admission; a `CheckedAlias` or
`CheckedDeclaration` constructor does not establish proof authority.

Import functions receive an admitted `frozenset` of module names. They enforce
the existing candidate import limits, reject unavailable/private/self imports,
and preserve an initial `module` command and header comments. Toolchains,
package inventories, compiled-file hashes and import capability display belong
to their respective environment, checker and prompt owners.

`candidate` and `probe` share one renderer and retain authored proof/statement
locations. `RenderPolicy` supplies readable names, raw numeral spelling, grouped
sorts and computability explicitly. Local contexts are closed using a flat
rendering sequence. Named definition contexts preserve domain-generated prefix
names, including inherited alias scopes. A declaration bundle decodes each unique
signature once, emits each definition/proof once, and orders dependencies before
their users. Missing required declarations, conflicts and cycles fail before any
output is published. Certificate composition and semantic admission remain checker
responsibilities; source consumes their explicit named results.

For example, given an admitted `selection`:

```python
from midas.source import SourceFile, candidate, declaration_context

context = declaration_context(selection, 0)
generated = candidate(context, SourceFile("proof", b"by\n  trivial"))
compiler_input = generated.data
# A diagnostic byte offset maps only when it lies in copied authored text.
location = generated.authored_location(generated.data.index(b"trivial"))
```

Source maps are flat, non-overlapping byte spans verified against exact original
bytes. Generated scaffolding and indentation have no authored location. Returned
locations use one-based lines and zero-based Unicode code-point columns;
compiler protocols using bytes or UTF-16 must convert at their boundary.

`integrate` replaces exactly the selected bodies, preserving the remaining source
and its scopes. It applies all edits in one pass instead of copying the whole file
for every replacement. `module_tree` takes explicit module bodies/dependencies and
returns raw sources and build order. Generated names are single components, so a
deep proof graph never creates deep directories. A shared child has one module.
Export owns profile selection, artifact persistence, packaging, declaration audits
and independent rebuild verification; none of those services is introduced here.

Source output is capped at 64 MiB, with caller-supplied smaller limits and an
aggregate module-tree limit. Expression rendering checks rope sizes before
flattening shared DAGs; exponentially large projections fail without expansion.
Large string literals use Lean escaping directly, without JSON. Repeated named
context rendering uses a bounded cache (128 entries, 16 MiB), keyed only by exact
context bytes and the output limit. Conservative size accounting includes source
maps and their lazy line indices; forked workers start with an empty cache.

Validation on 2026-10-15: **35 distinct Python source tests passed**, using the
initial focused run and targeted reruns after changes. Coverage includes large
source/string inputs with JSON operations disabled, lookup without type decoding,
shared signatures, Unicode mapping, inherited definitions, cycles and conflicts,
1,100 local declarations and a 1,200-module chain with flat paths. No unrelated
policy, domain, storage, packaging or provider suites were rerun.

With the permission required by the root `AGENTS.md`, **six generated Lean files
compiled through bubblewrap with Lean 4.34.1**: representative expressions,
control-character strings, candidate aliases, definitions, local contexts,
universe parameters, integration, namespace/section prefixes, a module header,
and a three-module dependency chain. These smoke checks validate generated syntax
and elaboration, not the future checker's receipt or export-audit contracts.

```sh
python3 -B -m unittest tests.test_source -v
# Requires permission for Lean/bubblewrap:
python3 -B -m tests.check_source_lean /absolute/path/to/toolchain/bin/lean
python3 -B -m tests.benchmark_source
```

Local alternating seven-sample comparisons against v2 measured:

| Operation | New/v2 time |
| --- | ---: |
| 32-component Lean name | 0.392 |
| Lexical declaration hints | 0.999 |
| Import insertion after a large header comment | 0.015 |
| Shared expression rendering, including binary decoding | 0.398 |
| Repeated context rendering, warm bounded cache | 0.001 |
| First-use context rendering, including binary decoding on both sides | 0.790 |
| 100 edits in a 2.2 MB source | 0.305 |
| Minimal lookup context / v2 source-prefix loading | 0.674 |

These are synthetic source measurements, not end-to-end formalizer throughput.
The warm context figure measures reuse; the first-use comparison includes binary
decoding on both sides and clears the new cache before each call. New output also
includes source maps. The lookup comparison includes constructing the new
context, while v2 loads its saved source prefix.

## Executor

The executor has **four Python modules plus `__init__.py`**, and one private C
resource:

| Module | Responsibility |
| --- | --- |
| `process.py` | Shared Linux process discovery, pidfd ownership, sampled memory limits, bounded diagnostics/evidence, termination and result normalization |
| `isolation.py` | Explicit namespace launch recipes, launcher hash checks, isolated roots and bounded scratch space |
| `imports.py` | Retained import transport, exact input copying, protected completion messages, native-runtime capability and source access |
| `workers.py` | Bounded dispatch, cancellation, progress, cumulative invocation limits, compiler capacity and retained-worker transfers/eviction |
| `import_runtime.c` | Preserved native import isolation: Landlock, ptrace, descendant termination and private control descriptors |

These collect the executor responsibilities of `process_runner`, `lean_runner`,
`import_pool`, `import_worker`, `import_runtime`, and `comparator_runtime`. Package
and runtime inventories/materialization belong to environment; Lean source
assembly, toolchain support admission, source-bound builds, and semantic evidence
validation belong to checker/export. Those later components supply the recipes.
Executor does not import their v2 implementations, discover a toolchain, build
checker sources implicitly, publish artifacts, or own SQLite transactions.

`Executor.start(invocation, execute, grant=..., workspace=..., reader=...,
staging=...)` consumes a controller-issued `CapacityGrant` for the exact invocation,
including its saved request, reservation and session. The new grant and progress
records live in domain. Their constructors check structure; the controller must
issue grants only after committed admission. There is no second admission queue.
Over-dispatch is rejected, and unconsumed completions also bound the dispatch count.

The callable receives only its `FrozenRequest` and `WorkerContext`. The context
has the supplied artifact reader/private staging interfaces, a private workspace,
cancellation, deadline, bounded progress, `run_process`, and `run_import`; it has
no controller or submission method. Recursive dispatch and use of a context from
another thread are rejected. The future storage component supplies the actual
read/staging capabilities and owns publication and workspace cleanup. Completed
workspace files survive executor shutdown for publication or recovery.

`LaunchSpec` describes argv, explicit mounts, process environment variables, and
the admitted bubblewrap executable/hash. It does not contain an Environment,
toolchain manifest, package inventory or source payload. The executor clears the
inherited process environment, unshares namespaces, drops capabilities, binds only
the supplied roots, and requires private bubblewrap status. Missing/changed
launchers and isolation failures never enable host execution. The raw subprocess
function is private and used by supervisor tests. Launch metadata is capped at
16 KiB; generated inputs and outputs stay in files or raw byte streams.

Compiler checks, lookup, probes, assembly, integration and export verification
share compiler slots and memory. Active and retained footprints remain reserved
until their processes terminate. `state()` exposes retained worker IDs and their
full reserved memory so policy can account for them. An explicit
`CapacityGrant(invocation, retained_worker_id)` transfers that footprint into the
new reservation. Other idle workers are evicted before new compiler work starts.
All checks/fallbacks within a context consume the same remaining wall, output and
evidence budgets, including launch preparation. Attempt evidence remains separate;
`ImportResult.attempts` retains each bounded result for later staging.

Retained workers are keyed by the exact launch recipe, environment/runtime hashes,
request filename, non-request input hashes, memory and scratch limits. A changed
request is streamed into the private input workspace; unchanged dependencies are
copied and verified once. Sequential probe cases can share the same parent while
consuming the invocation's remaining evidence budget.
`import_worker_platform_supported()` checks Linux x86-64 and Landlock ABI 6 or
later; executor does not accept or select a Lean version. Checker owns toolchain
compatibility, exact runtime admission and build recipes, using
`import_runtime_source()` and `LINK_FLAGS`. Unsupported optimized execution can
use an explicitly supplied isolated fresh recipe, within the residual budget.
Cancellation and resource exhaustion do not start another attempt.

Memory remains **sampled anonymous RSS**, as in v2, rather than a hard cgroup
ceiling. Descendant discovery is shared for at most 20 ms, resets after fork, and
pins process identities with pidfds. Cleanup waits for observed thread groups;
the namespace contains untrusted descendants between samples. An observation
failure reports unknown termination and keeps capacity held for recovery.
Native request completion comes from the protected control FIFO only after its
traced descendants exit; retaining the parent is accounted for separately.

Trusted Python callbacks use cooperative cancellation and deadlines for external
I/O. They cannot be forcibly killed safely as threads. `drain(timeout=...)` returns
false while such work remains alive, retaining capacity and withholding completion.
A returned result keeps its staged references and known usage even when a late
cancellation changes its status. Final artifact publication and proof acceptance
remain controller/owning-service operations.

Validation on 2026-10-15: **33 executor tests and four existing domain execution
tests passed**. Coverage includes process trees across sessions, cancellation,
output/evidence/memory/wall limits, launch/monitor failures, descriptor cleanup,
progress backpressure, reservation binding, retained transfers/eviction, cumulative
fallback limits and preserved partial results. No unrelated source, policy,
storage or provider suites were rerun.

With the root `AGENTS.md` permission, real bubblewrap/Lean 4.35.0-rc3 smoke checks
passed for fresh success/rejection, explicit read-only roots, compilation of a
small native harness against the bundled C runtime, retained reuse and rejection,
protected-control-FIFO rejection followed by authorized isolated fallback, and
cancellation with confirmed process-tree exit. These test executor behavior,
not the future checker's receipt or proof-admission contracts.

```sh
python3 -B -m unittest tests.test_executor tests.test_domain.WorkTests -v
python3 -B -m tests.benchmark_executor
# Requires permission for Lean/bubblewrap:
python3 -B -m tests.check_executor_lean /absolute/path/to/lean-4.35.0-rc3/bin/lean
```

Seven alternating local samples measured **0.999× v2 time** for capturing 1 MiB
and **1.002×** for launching/monitoring a Python child that emits 64 KiB. These
small differences indicate comparable supervisor performance; they do not measure
end-to-end formalization or compiler import-cache speed.

## Environment

Six implementation modules plus `__init__.py` consolidate the environment-owned
parts of the twelve modules assigned by the cleanup blueprint:

| Module | Responsibility |
| --- | --- |
| `inventory.py` | Bounded no-follow scans, stable streaming hashes/copies, flat binary records, file and directory validation |
| `manifest.py` | Pinned Lean versions, immutable profile settings, canonical manifests, module conflicts, hash-only source bindings, bounded manifest cache |
| `package.py` | Trusted installation probes, frozen runtime closure, preparation/preflight, atomic publication, shared toolchain admission/relocation, generated inputs and launch recipes |
| `dependencies.py` | Pinned Git/Lake closure, complete source provenance, compiled modules/sidecars, metadata revalidation and isolated source replay |
| `archive.py` | Bounded verification configuration, tool pins, committed benchmark archives, target metadata, relocation checks |
| `attachment.py` | Namespace-scoped library attachment, retained original pins, import preflight and new environment identity |

These replace environment ownership in `environment`, `toolchains`, `packaging`,
`shared_package`, `shared_toolchain`, `problem`, `attachment`,
`dependency_admission`, `dependency_modules`, `dependency_compiler`,
`verification_config`, and `leaneval_admission`. They do not import v2.
Run bindings, artifact publication and run-cache retirement from `problem` and
`shared_package` belong to the future storage/application components. Checker
resource compilation and proof receipt admission belong to checker. Lake's
freshness/hash gates, workspace layout, kernel comparison and final LeanEval
acceptance belong to checker/export; provenance or successful source replay alone
does not report a verified export.

`Environment.prepare(destination, config, worker_context)` consumes a
`PackageConfig` with one pinned release, one installed toolchain, explicit
project/dependency roots, selected project files and ordered module search paths.
It never downloads or builds missing dependencies. The caller supplies an existing
executor reservation; installation probes, preflight, native builds and replay
all use that context's cumulative process limits and cancellation. There is no
second process runner or scheduler inside environment.

Preparation shares the installed toolchain and copies other roots. It checks
source stability, records exact file hashes/modes/directories, freezes the runtime
closure for Lean and native build tools, and checks the reported compiler version
and selected sources under bubblewrap. Only successful preflight publishes the
package, using an atomic rename that cannot replace an existing destination.
Copied bytes, runtime files and package metadata consume the package allowance;
the shared toolchain is inventoried separately. Internal toolchain file aliases
are allowed; external links, directory links and special files are rejected.

The package contains `environment.bin`, `preflight.bin`, a small `location.json`,
and the original roots. Its manifest is a flat binary record stream beginning
with `MIDASENV` and byte `2`. Paths, settings and file metadata have explicit
lengths; hashes remain raw 32-byte fields. `env_…` hashes the canonical manifest.
Loading validates canonical record ordering, bounded lengths/counts, directories,
references and profile fields directly, without serializing the decoded value.
There are no v2 JSON-manifest compatibility readers.

`Environment.restore(package, manifest=saved_manifest, expected_id=saved_hash,
toolchain=relocated_installation)` checks the saved identity and resolves the
supplied locations. A supplied saved manifest is authoritative; a package's loose
manifest cannot replace it. Opening a standalone package reads its binary manifest.
The explicit toolchain argument, `MIDAS_SHARED_TOOLCHAIN`, or the small package
locator selects an installation. Relocation does not change the environment ID.

Frozen package files receive exact content checks. Shared toolchains retain v2's
default metadata admission and optional content policy
(`MIDAS_VERIFY_TOOLCHAIN=1`, or `validate(hash_toolchain=True)`). Successful
toolchain admissions are bounded, process-local and keyed by location, exact
toolchain inventory and policy. `validate(fresh=True)` repeats admission;
dependency source replay requires content admission before and after execution.
File digest reuse requires identical device/inode/mode/size/mtime/ctime stamps.
Caches and service admission reset across forks.

An environment service owns stable roots for its lifetime, matching v2's
`EnvironmentVerifier` contract. Repeated jobs reuse that admitted service;
explicit `validate()` always rechecks package contents. `selection_inputs()`
returns only the environment hash and authorized source hashes/sizes to domain.
Manifests, installation paths and package inventories stay with environment.

`materialize()` accepts exact `AuthorizedInput` records, copies verified bytes into
a new private directory, and rejects path conflicts or frozen-module shadowing,
including compiled sidecars. `launch()` returns an executor `LaunchSpec` with
explicit read-only roots and an optional separate writable build directory.
Generated inputs are checked before each recipe is issued. The installed
bubblewrap hash is also checked at execution; missing isolation never enables a
host fallback. Trusted bootstrap inspection uses an explicit Linux system runtime
under bubblewrap; compiler jobs use the frozen runtime.

Dependency closure admission compares pinned commits, committed Lake metadata,
transitive pins, mapping membership and source/module paths. Development
toolchain metadata is provenance, so it may differ from the workspace compiler.
Module admission checks complete committed Lean source bytes, required modules,
unambiguous source mappings, sidecars and module conflicts. It rechecks metadata
and HEAD after scanning; its source-only profile also rejects native libraries.
Replay resolves the required import closure with empty placeholders, removes all
placeholders, compiles in dependency order without prepared objects mounted, and
compares exact generated sidecars. Results bind the environment hash, module
inventory hash, import graph, build order and explicit options.

Benchmark archives use another flat binary stream (`MIDASARC`, byte `2`), retaining
raw source bytes rather than JSON/hex. They verify the pinned working tree twice,
bound tool identities and target metadata, and require explicit archived-fixture
opt-in. JSON is restricted to configuration, lockfiles and small external
metadata with a hard **16 KiB** cap checked before parsing. Large raw Lake
configuration/source files remain raw bytes. Manifest/record streams are capped
at **64 MiB**; benchmark content at **16 MiB**, with **8 MiB** per file. The layouts
use fixed-depth records, not recursively embedded environments.

Validation on 2026-10-15: **32 distinct Python environment tests passed**, with
focused reruns after the inventory/cache and dependency revalidation changes.
Coverage includes malformed binary records, large raw payloads, path/link and
mutation checks, relocation, toolchain policy, generated inputs, module conflicts,
attachment, publication races, Git/module provenance and archive admission.
With the required `AGENTS.md` permission, real Lean 4.35.0-rc3/bubblewrap checks
passed preparation/preflight, exact toolchain admission, relocation, proof
success/rejection, native compilation, a pinned two-module Git dependency and
source replay, and failed-preflight cleanup.

Seven alternating local samples over 200 source files measured new/v2 median
ratios of **0.810** for scanning, **0.925** for freezing, **0.815** for cold manifest
loading and **0.876** for cached loading. These measure filesystem/manifest work,
not end-to-end formalization. No unrelated component suites were rerun.

```sh
python3 -B -m unittest tests.test_environment -v
python3 -B -m tests.benchmark_environment
# Requires permission for Lean/bubblewrap:
python3 -B -m tests.check_environment_lean /absolute/path/to/toolchain/bin/lean
```

## Checker

Six implementation modules plus `__init__.py` separate compiler preparation,
execution and evidence admission:

| Module | Responsibility |
| --- | --- |
| `requests.py` | Immutable compiler requests, exact source/candidate/dependency bindings, closed signatures, lookup prefixes and probe cases |
| `evidence.py` | Strict binary certificates, native frames, binding/replay receipts and advisory result validation |
| `runtime.py` | Exact installed resource hashes, isolated compilation, atomic private runtime cache and explicit fresh/retained recipes |
| `inspection.py` | Binary Lean declaration inventories and frozen selection construction |
| `proofs.py` | Rendering admitted earlier definition proofs into their original source scopes |
| `service.py` | One executor callback and staging path, cumulative process limits, probe progress and trusted receipt admission |

These replace the checker-owned portions of `check_protocol`, `checker_runtime`,
`checking`, `extraction`, `interface_lookup`, `probe_runtime`, `proof_execution`,
`proof_jobs`, `source_cache`, and `source_proof_admission`. No implementation imports
v2. SQL, task publication, graph updates and scheduling stay with their future
owners. V2's `cache_probe` is a model-provider diagnostic workflow, rather than a
Lean checker operation. Export owns certificate composition, final layouts and
export acceptance; checker supplies the independent whole-source kernel audit.

`Checker(environment, cache_directory)` owns one admitted environment service.
`inspect(selection_request, worker_context)` returns a `FrozenSelection` from
recorded project source. `prepare(operation, selection, ...)` accepts `binding`,
`proof`, `lookup`, `probe` or `audit`, and returns a `CompilerRequest` with immutable
binary `payload` bytes. The controller saves those bytes as
`FrozenRequest.operation_payload`, binds the exact input references and owner
versions in its job, then dispatches `checker.execute` through the existing
executor reservation. Preparation also rejects source bytes that differ from the
environment's authorized input. Environment manifests, packages and toolchain
inventories never enter requests, results or certificates; only the hash crosses
those boundaries.

Source and proof text remain raw UTF-8 fields in the binary graph format. Types
cross the boundary only as compact signature DAGs where identity/binding needs
them. Expanded expressions remain private to Lean. Lookup returns display text
and optional source ranges, without expression trees; it reads only the selected
declaration's prefix. No source, certificate, inventory, lookup or probe output
uses JSON parsing/stringification, stdout framing or hex payloads. Lean's internal
`Json` value type is only a private DAG container for the binary codec. The sole
remaining checker JSON encoding is the executor's small protected control-FIFO
completion record, containing a fixed request filename, exit code and timing.
Existing domain structural-name metadata retains its explicit 16 KiB cap.

Binding elaboration and its independent replay are separate compiler requests
within the same invocation budget. Binding admission returns the checked identity,
parameter names and an `AdmittedBinding`; it never reports a proof. Proof production
retains submitted source and independently audits every new declaration, including
generated helpers. Replay restores the original environment, checks the exact
subject and assumptions, and kernel-checks native declarations when available.
Only `propext`, `Classical.choice` and `Quot.sound` are permitted global axioms.
Conditional assumptions remain explicit and cannot become a direct proof.
Closed definition dependencies require matching admitted proofs from the same
joint submission, and are rendered in a flat, explicit dependency order.

`admit_receipt` requires the controller/storage `ReceiptVerifier` capability. It
checks the seal callback, original invocation, saved result bytes, request hash,
runtime hash, staged artifacts, phase order, diagnostic accounting, signature and
replay evidence before producing a domain handoff. A model-supplied certificate,
ordinary successful `WorkerResult`, or probe result cannot establish proof
authority. `worker_payload` defines the small exact binary result representation
that receipt publication must save. The actual durable seal implementation and
artifact publication belong to storage/controller. `admit_probe_progress` validates
executor-delivered starts/completions against each case and invocation; it remains
advisory, including when a probe closes its goal.

Runtime builds and compiler calls use the existing executor context. Retained
imports require Lean 4.35.0-rc3 and the executor's supported Linux/Landlock platform.
The retained launcher explicitly disables Lean's extra main thread and uses the bundled
executor C runtime; that runtime's C build uses the existing isolated bootstrap
capability. Other supported launches use fresh processes, and configured Lean
options/plugins select the interpreted launcher. Fallback consumes the residual
wall/output/evidence allowance. Cancellation and resource exhaustion never start
a fresh fallback. Successful fallback retains the failed attempt's bounded
diagnostics, while admission requires complete evidence from each successful phase.

Compiler input/evidence graphs are capped at **64 MiB**, authored proof/statement
terms at **1 MiB**, native cache blobs at **8 MiB** per batch, and result metadata
at **64 KiB**. Native blobs use memory views until materialization. Signature reuse
retains only compact immutable bytes in a bounded, fork-safe cache. Runtime files
are inventoried and hash-checked before use. Source/native scratch files remain
in private executor workspaces for storage's later publication and cleanup.

Validation on 2026-10-15: **13 Python checker tests passed**, covering exact
requests/dependencies, malformed binary frames, source inventory admission,
source/signature changes, lookup boundaries, progress binding, runtime mutation,
and rejected forged/incomplete/over-budget receipts. With the required root
`AGENTS.md` permission, real Lean 4.35.0-rc3/bubblewrap checks passed native runtime
builds, binary inspection, fresh and retained proof/replay, sorry rejection,
binding elaboration/replay, derived definitions, conditional/frozen-peer proofs, lookup/ranges,
probe states, whole-source audit, closed joint definitions, evidence/wall limits,
cancellation with confirmed termination, and the configured interpreted launcher.
No unrelated component suites were rerun.

Seven alternating decoder samples measured new/v2 median ratios of **0.965**,
**0.873** and **0.480** for native frames carrying 1 KiB, 1 MiB and 8 MiB blobs,
each with a 100 KiB source field. These measure evidence decoding, not complete
formalization throughput. Real integration checks also exercised import reuse
across the cases of a probe batch without fallback.

```sh
python3 -B -m unittest tests.test_checker -v
python3 -B -m tests.benchmark_checker
# Requires permission for Lean/bubblewrap:
python3 -B -m tests.check_checker_lean /absolute/path/to/toolchain/bin/lean
```


## Provider

Six implementation modules plus `__init__.py` own the provider slice:

| Module | Responsibility |
| --- | --- |
| `protocol.py` | Immutable routes, projected transport bounds, artifact-based requests and normalized outcomes |
| `wire.py` | Bounded protocol parsing, small control messages and validation of already-rendered wire bytes |
| `transport.py` | Linear text accumulation, usage/error normalization, cancellation and deterministic scripted responses |
| `responses.py` | One stateless Azure/Bedrock HTTPS Responses invocation with no redirects or retries |
| `codex.py` | One ephemeral app-server turn, effective zero-retry verification, tool restrictions and advisory web events |
| `service.py` | Executor callback, private staging and exact authenticated receipt admission |

These replace the provider-owned responsibilities of `model_adapters`,
`model_protocol` and `model_runtime`. Their old SQL, timers, retry policy, task
publication and recovery coordination remain assigned to storage, policy and
controller. Provider does not import those components or v2. The small new
`executor/protocol.py` owns bidirectional subprocess lifetimes, using the existing
process-tree monitor and waiting for observed descendants to exit, including
children which create another process group. This is a trusted protocol client,
not a compiler launch or a way to run model-generated code.

Application projects `Route.from_config(config.prover)` and
`Limits.from_config(config.model)`, resolves credentials, and constructs
`Provider([(route, transport), ...])`. Limits contain frozen transport bounds,
not scheduling/retry rules or prices. `BearerCredentials` binds an ephemeral
secret to one exact route; `from_environment` reads only its named variable.
Codex receives an explicit environment-variable mapping and a pinned absolute
executable. Credential values are never in request/result artifacts. Its config
and credential provisioning remain application responsibilities.

Prompt creates the exact UTF-8 wire artifacts. `ModelRequest(route, role, limits,
wire, setup=None)` contains only their references and small transport metadata.
Its `payload` is a shallow binary record capped at **16 KiB**. Each wire artifact
is capped at **8 MiB**. The controller saves the payload as
`FrozenRequest.operation_payload`, sets `job.input_refs` to `request.inputs`,
and dispatches `provider.execute` through the existing executor grant. The job's
output allowance must cover `limits.output_bytes`; its evidence allowance must
cover `limits.event_bytes + 16 KiB`. An environment is never passed to provider.
The surrounding shared job retains only the environment hash required by domain.

For Responses, `wire` is the complete HTTP body, sent unchanged. It must select
the saved model with `stream=true`, `store=false`, text messages and an explicit
`text.format` of `json_schema` (named, with `strict=true`) or `json_object`.
**Prompt owns the role-specific schema and renders it into this field.** Provider
validates the transport envelope, sends it without rewriting, and returns the
model's response text without whitespace trimming, fence stripping or extracting
JSON from prose. It does not interpret a mathematical candidate. Unsupported
backend settings remain a reported error; provider never removes the schema or
retries with plain text. Refusals have a distinct outcome, and incomplete streams
retain their partial text and available usage. See the
[official Responses Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

For Codex, `wire` contains the frozen `turn/start` `input` text blocks and `setup`
contains the complete frozen `baseInstructions`, including the role contract.
Provider adds only thread/model/protocol controls. It verifies the effective
request/stream retry settings before inference, disables local tools, and admits
bounded web operations only for OpenAI research. Usage notifications before the
turn acknowledgement and before the final thread-read acknowledgement are kept.
The subprocess is terminated and reaped before reporting termination.

Protocol-required JSON stays at the transport boundary. The parser scans incoming
bounded frames and decodes strings in **8 KiB** pieces; it rejects duplicate keys,
invalid Unicode, excessive depth, excessive values and nonfinite numbers. Large
wire strings and result text are never passed to `json.loads` or `json.dumps`.
Only small, bounded app-server controls use JSON encoding. SSE byte accounting
includes comments and incomplete frames. Text deltas append to a byte buffer,
avoiding repeated copying of the accumulated response.

Worker output consists of raw UTF-8, length-prefixed binary advisory events and a
small binary report. The report retains request/response/thread/turn IDs, error
phase, delivery certainty, nullable usage, cache-write telemetry and retry-after
metadata. Unknown telemetry remains unknown. Costs and retry decisions stay in
policy. A pre-send cancellation or connection failure is `NOT_SENT`; a write
failure is `UNKNOWN`; an HTTP response is `DELIVERED`, including errors. Closing a
local HTTP stream does not imply cancellation of remote computation.

`worker_payload(result)` gives controller/storage the exact small result bytes to
save. `admit_receipt` requires the controller's `ReceiptVerifier`, verifies those
bytes, the saved request, artifact hashes/limits, report consistency and all event
frames. It returns `AdmittedOutput` with raw/event references, small outcome
metadata and the executor's final status. A stop arriving after transport return
can change execution status while preserving known usage and complete bytes.
These remain advisory results, never proof authority. `ScriptedTransport` supplies
a deterministic saved outcome by exact wire hash through the same staging and
receipt path.

Validation: **34 offline tests passed**, covering config projection, route/wire
validation, structured JSON output, exact wire bytes, large payloads with JSON
operations disabled, usage/cache telemetry, refusals, partial/truncated streams,
limits, cancellation, HTTP failures, zero retries, receipt tampering, executor
handoff, pinned app-server startup, detached-child termination and stderr overflow.
No Lean, external-service, or unrelated component tests were run.

Seven alternating local samples measured new/v2 median time ratios of **0.026**
for 128 KiB text accumulation, **0.014** for 1 MiB accumulation, and **0.686** for
parsing a complete SSE stream of 1,024 deltas. The stream comparison includes
framing, event decoding and text accumulation. These measure local provider work,
not live model latency or end-to-end formalization.

Run from `new/` with the repository virtual environment (HTTP tests use `httpx`):

```sh
../.venv/bin/python -B -m unittest tests.test_provider -v
../.venv/bin/python -B -m tests.benchmark_provider
```
