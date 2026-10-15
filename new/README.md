# Clean Midas: domain and policy

`midas/policy` now implements the policy layer on top of the domain. All policy
settings are defined in `midas/policy/config.py` and loaded from one shallow JSON
document. [`config.json`](config.json) contains the policy defaults and the
prover/reviewer model settings from the supplied v2 `live.json` example.

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
combinations, request validation and environment admission belong to the future
provider/application and environment components.

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
verification. This directory contains domain and policy contracts, not a runnable
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
