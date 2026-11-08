# Clean Midas: domain through CLI and LeanEval

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

`midas/tools` implements advisory helper plans, frozen-source and hosted search,
retrieval continuations, research validation, source excerpts, and indexed evidence
paging. It imports only domain, policy, and source.
`retrieve` is the sole model-facing retrieval action: known names go directly to
checked lookup; queries run searches and automatically check the selected results.
Search and lookup remain internal stages, with one helper charge for the whole
retrieval request.

`midas/prompt` implements independent, replaceable prover, decomposition-reviewer, stop-reviewer, researcher and web-searcher
renderers, bounded readable context, and exact frozen wire construction. It imports
only domain and source.

`midas/strategy` implements pure candidate, review, repair, memory and continuation
decisions. It imports only domain, policy, prompt and tools.

`midas/storage` implements SQLite ownership, immutable artifact publication,
fenced task accounting, recovery and indexed read views. Its only component
dependency is domain; compression uses the existing `zstandard` dependency.

`midas/controller` implements one run loop over these components. It owns
admission, publication, receipt application, retries, cancellation and recovery.

`midas/export` implements assembly, source/library integration, independent export
verification and exact-file publication. `midas/leaneval` wraps the existing CLI
and adds benchmark preparation, comparator execution, judge publication and fresh
submission verification. It is not an export profile or a second controller loop.

`midas/application` constructs these services and implements the complete run
workflow. `midas/status` owns committed views, node queries and presentation;
`midas/cli` owns arguments, signals, stop IPC, output routing and exit handling.

## LeanEval

`python -B -m midas.leaneval` passes ordinary commands to `midas.cli`. Its extra
commands are `prepare`, `finalize`, and `verify-submission`; `start --workspace`
supplies a prepared selection and source library to the ordinary start workflow.
No v2 modules are imported by the implementation.

First freeze a package with the existing `package` command. Its project contains
the exact generated benchmark starter, and its project inputs include
`project/Submission.lean`. Search paths must include compiled local helpers and
the configured dependencies. The package command checks those inputs; LeanEval
does not fetch or build missing external dependencies. Prepared helper objects
must include the sidecars produced by Lean's `-o` and `-i` options. A normal Lake
build supplies them. Use the existing package/attachment functions to prepare
these inputs.

Dependency roots are referenced in place, including large Mathlib trees. Package
preparation copies only the project and compiler runtime, and pins dependency
contents in the binary manifest. Keep referenced directories available and stable;
restoring or explicitly validating a package rejects changed dependency inputs.
Package preparation also builds `search.sqlite` for local BM25 search. LeanEval
`prepare` reuses this index, or builds it for an existing package before publishing
the workspace. Subsequent starts, resumes and forks share the package index,
including when they use a different application cache.

Measured on the Morley fixture (2026-10-16): ordinary CLI package preparation
completed in **67.08 seconds**, referencing **8.28 GB** across ten dependencies.
The package contained **27.62 MB**, mostly its binary inventory, and a **1,033-byte**
locator. The previous copying attempt exceeded the CLI's ten-minute allowance.
This measures package preparation and Lean source preflight, not proof generation.

```sh
# From new/; package.json uses the ordinary package configuration below.
../.venv/bin/python -B -m midas.leaneval package package --config package.json
../.venv/bin/python -B -m midas.leaneval prepare workspace --package package --verification-config verification.json
../.venv/bin/python -B -m midas.leaneval start run --workspace workspace --config config.json --output bundle
../.venv/bin/python -B -m midas.leaneval status run
../.venv/bin/python -B -m midas.leaneval resume run --output bundle
../.venv/bin/python -B -m midas.leaneval finalize bundle result --workspace workspace --verification-config verification.json --runtime /absolute/path/to/regular-usr
../.venv/bin/python -B -m midas.leaneval verify-submission result --workspace workspace --verification-config verification.json --runtime /absolute/path/to/regular-usr --evidence-output verification.bin
```

`start --workspace` also accepts the ordinary scripted and supplied-candidate
options. It selects every archived benchmark output, including definitions, and
rejects competing package/source/target flags. Resume, fork, stop, status, artifact,
and export use the ordinary CLI and its saved run records. `finalize` takes an
ordinary **export bundle**, produced by `--output` or `export RUN BUNDLE`.
`verify` remains the ordinary source-bundle command; `verify-submission` checks the
LeanEval result, including its submission and judge copies.

The verifier config is the existing environment format, capped at 16 KiB. Paths
resolve relative to the config file. For example:

```json
{
  "format": "midas-verification-config-v2",
  "benchmark": "/absolute/path/to/benchmark-checkout",
  "revision": "<40 lowercase hex digits>",
  "problem": "sample",
  "tools": {
    "landrun": {"path": "/tools/landrun", "sha256": "<64 lowercase hex digits>"},
    "lean4export": {"path": "/tools/lean4export", "sha256": "<64 lowercase hex digits>"},
    "comparator": {"path": "/tools/comparator", "sha256": "<64 lowercase hex digits>"},
    "nanoda_bin": {"path": "/tools/nanoda_bin", "sha256": "<64 lowercase hex digits>"}
  },
  "dependencies": [],
  "allow_archived_fixture": false
}
```

External JSON configuration, benchmark metadata and Lake lockfiles accept ordinary
JSON whitespace and key ordering, with the 16 KiB cap checked before parsing.
Duplicate keys remain invalid, and archived files retain their exact original
bytes. Lake's quoted package names map to the corresponding plain root labels.

Each dependency entry supplies `name`, `path`, `module_paths`, and `source_roots`.
Its name must identify the matching frozen package root. By default, LeanEval
trusts those dependencies' frozen `.olean` files and required sidecars, including
Mathlib and lean-pool. Preparation checks pinned Git revisions, committed sources,
module mappings and compiled-file hashes, without rebuilding dependencies or
evaluating their compiler options. Compiled files remain referenced in place.
Local starter helpers retain their source and are rebuilt during export checking;
generated proofs still receive a fresh kernel audit.

Use `prepare --replay-dependencies` to request full dependency source replay and
subsequent export rebuilds. The chosen behavior is saved in the workspace library
and carried through starts, resumes, export, finalization and independent verification.
Trusted compiled bundles require their original frozen environment, including
the dependencies; relocation to identical files is supported. Changed dependency
objects are rejected. The ordinary comparator still checks both kernels.

Compiled-dependency validation (2026-10-16): **71 focused Python tests passed**.
Real Lean checks passed source-library export and standalone verification, plus
a pinned Git dependency through default preparation, replay opt-in, supplied
proof execution, export, both comparator kernels and fresh verification. Invalid
proofs, modified compiled dependencies and modified submissions were rejected.
The Morley retry completed default preparation in **120.82 seconds**, recording
**8,965 trusted compiled modules** and two source helpers (`ChallengeDeps` and
`Submission.Helpers`). Its library metadata occupied 4,326,109 bytes. This timing
includes dependency admission and source inspection; it is not a completed proof run.

Preparation uses the existing environment admission and export library capture.
It binds the starter bytes, complete target selection, dependency objects and
toolchain to the existing package hash. The workspace stores `archive.bin`,
`selection.bin`, `library.bin`, a small `workspace.bin`, and flat
`library/<source-hash>.bin` source/asset files. It does not copy the environment manifest
or the trusted dependencies' source trees.

Finalization first independently audits the ordinary bundle, then runs the
archived `lake test` harness with the existing executor. The explicit runtime is
a regular-file tree representing `/usr`, including its shell, Git, dynamic loader
and required native libraries. Source, tools, toolchain and frozen dependencies
are mounted read-only; only the private Lake build directory is writable. Lake
uses the archived dependency lock and generated path overrides. Both kernels and
the comparator must report acceptance, with successful exit and complete output.
Compiler limits, cancellation, capture bounds and export byte limits use the
existing services and policy configuration.

The result contains `bundle/`, `submission/`, `verification.bin`, `manifest.bin`,
and, for active problems, `judge-submission/<problem>/`. Only verified active
benchmarks produce judge files; archived fixtures stay non-ready. Partial
`--allow-sorry` bundles are rejected. Publication uses the shared atomic exporter
and never overwrites conflicting files. Use a new result directory for a new
verification report.

`verify-submission` checks all inventories and copies, then performs fresh source
auditing and comparator execution. Historical report contents grant no acceptance.
It needs the workspace and its frozen package; `--package` permits relocation to
an identical package. The original benchmark checkout is needed only for
preparation. Sources, archives, library records, inventories and diagnostic output
use raw/binary framing; only bounded configuration, Lake metadata and CLI summaries
use JSON. The raw evidence uses the environment's flat `read_rows` framing with
magic `MIDASEVL\x01`: metadata followed by audit/comparator or stdout/stderr fields.

Validation on 2026-10-16: **476 regression tests passed**, including 14 focused
LeanEval checks. Real Lean 4.35.0-rc3/bubblewrap checks passed source replay for a
local helper, supplied CLI execution, library export, comparator acceptance by
Lean and nanoda, fresh independent verification, tamper rejection, and verified
archived fixtures without judge files. No live model calls were made. Seven
alternating timing medians measured new/v2 ratios of **0.148** for repeated
100-helper overlays and **0.010** for framing 3 MB of comparator diagnostics.
These measure local wrapper costs, not full benchmark or model throughput.

```sh
# Permission is required by the root AGENTS.md for Lean-related tests.
../.venv/bin/python -B -m unittest tests.test_leaneval tests.test_export tests.test_environment tests.test_cli -q
../.venv/bin/python -B -m tests.benchmark_leaneval
../.venv/bin/python -B -m tests.check_leaneval_lean /absolute/path/to/toolchain/bin/lean /absolute/path/to/verifier-tools /absolute/path/to/lean4export
```

## Status and CLI

Run the new entry point from `new/`:

```sh
../.venv/bin/python -B -m midas --help
```

The command set has one name per operation. Old names are not aliases:

| v2 commands | New command |
| --- | --- |
| `targets`, `select`, `preflight` | `select PACKAGE --source Main.lean [--target NAME] [--output selection.bin]` |
| `inspect`, `nodes` | `status RUN [--nodes | --owner ID]` |
| `finalize` | `export RUN OUTPUT` |
| `attach-library` | `package OUTPUT --base PACKAGE --config attachment.json` |
| `start`, `resume`, `submit`, `fork`, `stop`, `artifact`, `verify` | Retained operations, using positional directories |

`select` checks the package source and defaults to all unfinished targets when no
names are supplied. `start` requires explicit `--target`/`--all-unfinished` with
`--source`, or a saved binary `--selection`. `verify BUNDLE --package PACKAGE`
independently checks a published bundle. `cache-probe RUN --config CONFIG` is the
explicit four-call Responses diagnostic.

```sh
../.venv/bin/python -B -m midas select package --source Main.lean --output selection.bin
../.venv/bin/python -B -m midas start run --package package --selection selection.bin --config config.json
../.venv/bin/python -B -m midas status run
../.venv/bin/python -B -m midas status run --nodes --state UNRESOLVED --limit 100 --json
../.venv/bin/python -B -m midas stop run
../.venv/bin/python -B -m midas resume run --raise-limits increase.json
../.venv/bin/python -B -m midas export run export
../.venv/bin/python -B -m midas verify export --package package
```

Live is the default workflow. `start --script manifest.json` selects scripted
execution; `start --candidates candidate.bin` selects supplied execution. Both
use the existing application/controller pipeline. `--workflow supplied` creates
an unresolved run for later `submit RUN --candidates candidate.bin`; an unresolved
idle run exits 1. `fork PARENT CHILD` creates a checkpoint; `resume CHILD` continues
it. `--output DIR` on start/resume/submit publishes a successful verified bundle.
`export --allow-sorry` explicitly permits incomplete proofs and keeps their proof
state incomplete.

`--config` reads the existing policy format. Responses credentials come from the
configured environment variable. App-server routes require `--codex /path/to/codex`;
the executable is resolved and hashed before dispatch. PATH, HOME and CODEX_HOME
are passed to it; additional required variable names use repeatable `--codex-env`.
Credential values are not written to run artifacts. `--instructions ROLE=FILE`
accepts prover, reviewer, stop-reviewer, researcher and web-searcher instruction
files, each at most 16 KiB. `reviewer` applies only to decomposition reviews;
`stop-reviewer` applies only to stop reviews. Both instruction sets are frozen
separately and preserved on resume and fork.

A script is a **16 KiB maximum** ordinary JSON manifest pointing to raw reply files:

```json
[{"role": "prover", "file": "reply.txt"}, {"role": "reviewer", "file": "review.txt"}]
```

Paths resolve relative to the manifest. Roles are prover, reviewer, research and web_search;
there are at most 512 replies, each at most 4 MiB, and 64 MiB combined. Replies,
candidate submissions, saved selections, source and evidence remain binary/raw
payloads and never pass through CLI JSON serialization.

`package OUTPUT --config package.json` accepts this small configuration. Paths
resolve relative to its file; the package stores the environment once:

```json
{
  "version": "4.35.0-rc3",
  "toolchain": "/absolute/path/to/toolchain",
  "roots": {"project": "project"},
  "project_inputs": ["project/Main.lean"],
  "search_paths": ["project", "toolchain/lib/lean"]
}
```

Optional fields are `max_bytes` and `preflight_imports`. With `--base`, the config
instead contains `name`, `path`, `module_paths`, `source_roots` and `prefixes` for
the library being attached. Existing environment admission performs the checks.

Status reads only committed storage and bounded application metadata. It does not
construct compilers, providers, credentials or a writer, and node summaries read
indexed columns without decoding proof history. `--owner ID` adds the owner's
reason and artifact references; use `artifact RUN ID --output FILE` to stream and
verify a payload. Output files are published only after verification and never
replace existing files.

Node filters are `--state`, `--roots`, `--owner`, `--after` and `--limit` (1–1000).
Each page holds one database snapshot and returns its revision and next exclusive
ID cursor. A subsequent page reads current state; its revision lets callers detect
intervening changes. `--json` emits one bounded JSON record per line, including a
page record after node rows. Each JSON operation is capped at 16 KiB. Long summary
reasons are explicitly marked `reasons_truncated`; single-owner details preserve
the saved reason. Unknown usage remains null, with known totals and unknown-report
counts shown separately.

Run progress uses the same status projection, is throttled to once per second,
and goes to stderr. Terminal output shows time, turns, cost and resource usage,
then separate `completed`, `conditional`, `active` and `waiting` node counts.
Active nodes are distinct unresolved owners of running work; waiting nodes are
the remaining unresolved nodes. Routine stage flags and `active_workers` reasons
are omitted. In a terminal, elapsed-time-only updates overwrite the last frame,
including wrapped rows; status changes add new rows. The last frame is cleared
before final output. Redirected progress remains line-based. Elapsed-time-only
updates are emitted at most every ten seconds.
A single pending frame is replaced when output is slow;
a blocked or broken progress sink cannot delay controller admission or draining.
`--quiet` suppresses progress. Final output goes to stdout. Status/query success
exits 0; run results retain policy exit codes (0 verified, 1 incomplete/blocked,
2 setup/recovery error, 130 cooperative stop). SIGINT, SIGTERM and `stop` request
controller cancellation/draining. Stop IPC checks the peer UID and exact run,
session and epoch; it never signals an arbitrary PID.

Validation on 2026-10-16: **460 offline regression tests and 17 focused status/CLI
checks passed**. Real Lean 4.35.0-rc3/bubblewrap CLI checks passed package,
selection, supplied/scripted execution, status, authenticated resume, fork reuse,
complete and partial exports, and independent verification. No live model calls
were made. Seven alternating timing medians measured new/v2 ratios of **0.729**
for warm committed status reads and **0.920** for parser construction/argument
handling. These are local command costs, not complete Lean/model throughput.

```sh
# From new/. The CLI tests need permission for local Unix sockets in this sandbox.
../.venv/bin/python -B -m unittest tests.test_status tests.test_cli -q
../.venv/bin/python -B -m tests.benchmark_status_cli
# Requires permission for Lean/bubblewrap under the root AGENTS.md:
../.venv/bin/python -B -m tests.check_cli_lean /absolute/path/to/toolchain/bin/lean
```

## Application

`Application(cache_directory, transports=...)` owns command preparation and service
construction. `Run` owns an exclusive storage session and the existing controller.
Live, scripted and supplied candidates share binding, checking, review, activation,
assembly, integration and independent export verification.

| Module | Responsibility |
| --- | --- |
| `service.py` | Start/resume handles, inspection, submission, publication and semantic recovery validation |
| `preparation.py` | Bounded package/selection preparation and frozen-source read capabilities |
| `providers.py` | Route/credential construction and durable scripted turn bindings |
| `records.py` | Shallow binary metadata, artifact pointers and complete frozen job envelopes |
| `workflow.py` | Controller planning/reduction, prompt snapshots, completion and cumulative export accounting |
| `candidates.py` | Dependency-ordered binding/proof stages, review and joint acceptance |
| `helpers.py` | Search/lookup/probe/research/web search, retrieval continuations and advisory pages |
| `repairs.py` | Objections, direct-consumer retirement, cancellation and repair grants |
| `forking.py` | Pinned checkpoints, authenticated evidence reuse and fresh local budgets |
| `diagnostics.py` | Standalone bundle verification and explicit provider-cache diagnostics |

```python
from midas.application import Application
from midas.domain import SelectionRequest

app = Application('/tmp/midas-checker-cache')
selection = app.select('package', SelectionRequest('Main.lean', all_unfinished=True))
with app.start('run', package='package', selection=selection,
               workflow='supplied') as run:
    run.submit(candidate_bytes)  # domain.encode_submission(...) binary payload
    result = run.run()
    if result.exit_code == 0:
        run.materialize('export')

view = app.inspect('run')
nodes = app.nodes('run', limit=100)  # cursor: after=last.node.owner.obligation_id
```

`package(destination, PackageConfig(...))` prepares a frozen environment;
`select` returns checked frozen selections. Start accepts a selection request,
an existing `FrozenSelection`, or saved selection bytes. CLI startup prepares the
selection and run with one environment handle and checker; passing an admitted
environment handle reuses its validation. Explicit `Environment.validate()` still
rechecks package bytes. The package locator
lives once in the frozen inputs; jobs, contexts and receipts carry the environment
hash. Source inventories remain behind an explicit source-reader capability.

Live runs use `Config.prover`, `reviewer` and optional `research` and `web_search` routes. Supply
pinned `CodexTransport` instances through the route-scope transport mapping;
Responses credentials are resolved at service construction and never persisted.
Web search uses the app-server transport. Research can also use Responses transports. Scripted runs take `(role, raw_bytes)`
pairs, with roles `prover`, `reviewer`, `research` and `web_search`. Each distinct turn consumes
one response, even when prompt bytes match another turn. Retries reuse the same
saved binding. Instruction bytes and exact wire/schema bytes are frozen before
dispatch. Supplied candidates schedule no model turns; conditional submissions
must contain explicit approval and still pass compiler checks.

Derived obligations are interned by checked identity. Revised submissions reuse
verified supporting lemmas with the same checked identity and generation, preserving
their accepted proofs without another proof check. Joint definition/property proofs
remain tied to their original submission. Compiler failures are saved per entry;
checking continues for every other entry whose required bindings and definition
certificates are available. Failed proof terms do not prevent conditional checking
against their successfully bound statements. Missing bindings or definition
certificates leave dependents explicitly blocked, rather than marked successful.
Available compiler checks run before conditional activation and review. A failed
submission settles its attempt once and retains each node's feedback across resume.
Helpers receive their exact
inherited aliases; retired generations require new fenced bindings. Probe feedback
contains the actual checked case results, and inspected lookup ranges resolve to
hash-checked original source excerpts. Child prompts retain their own proof guidance,
definitions and prerequisite interfaces. Conditional
proofs require independent review in model workflows. Joint definition/property
submissions check closed definitions first and publish the group atomically.
Rejected decomposition reviews retain their findings, locations, suggested revisions
and rationale in the prover's bounded feedback, including after resume. Findings
precede the general rationale when the memory limit requires omissions; the complete
review remains saved as the observation's evidence.
Prover `stop` actions save a pending request for the reviewer; they do not stop
the node or report a statement objection immediately. The reviewer accepts stopping
or rejects with actionable continuation guidance. Rejection returns its findings
to the prover's memory without spending a mathematical attempt. Research and web search continue
the same stop review, and pending requests survive resume with exact request and
owner bindings. Accepted statement objections reach current direct consumers, preserve shared reachable
work, and enter the repaired owner's memory. Mathematical and helper reservations
settle once through controller transactions.

`resume(directory, increases=...)` restores the package and re-admits selected and
pending proof evidence, reviews and export stages before stepping. Policy increases
are prospective; frozen requests retain their original bounds. Checker, local and
export resource limits and scheduling capacity can be raised alongside run and
mathematics limits. For example, `--raise-limits increase.json` accepts:

```json
{"checker":{"memory_mib":8192,"wall_ms":480000},"export":{"memory_mib":8192,"timeout_ms":480000},"scheduling":{"compiler_memory_mib":122880}}
```

The compiler memory pool must cover each compiler/export job. Lower bounds are
rejected; zero heartbeats means unlimited, so changing it to a finite bound is a
decrease. Raising export limits permits a new assembly, integration or verification
attempt with a new request identity, retaining prior receipts and cumulative export
usage. Completed proofs and current successful export stages are reused. Pending
jobs finish with their frozen limits; a failed export can then use the raised limits.
Checker increases apply to newly planned checks; they do not reopen settled candidate
decisions. Received model
outputs keep their original accounting identity during recovery. `request_stop()`
uses controller cancellation/draining. Inspection opens read-only storage without
constructing providers or compilers. Eight idle read connections and revision-bound
inspection/progress caches avoid repeated SQLite opens and decoding; reused readers
still acquire fresh snapshots and reject replacement database inodes.

`fork(parent, directory, config=..., workflow=..., script=...)` currently preserves
the exact environment hash and selection, allowing package relocation. It streams
artifacts from a pinned parent snapshot, authenticates imported receipts under the
child's own key, and re-admits proof semantics without rerunning proof compilation.
Selected proofs, reviews, assemblies, current exports and draft provenance survive.
Pending execution is not copied. The workflow defaults to the parent's; scripted
forks inherit remaining responses unless explicitly replaced. Local usage starts
fresh and parent accounting remains unchanged. Forks remain verifiable after the
parent moves; incomplete fork initialization cannot be resumed. Storage schema 2
adds a separate imported-receipt ledger, without counting imports as executions.

`export(run, destination, allow_sorry=True)` explicitly produces a checked partial
bundle while keeping unresolved proof state. `verify(bundle, package=...)` performs
a separate compiler audit without model services. Export receipts, including failed
attempts, consume cumulative wall/output/evidence budgets exactly once; retries
must fit the remaining allowance. `cache_probe(directory, config=...)` is a separate
four-call Responses diagnostic and preserves unknown cache telemetry.

Application source, candidate, evidence and wire payloads stay in bounded binary
artifacts; no whole payload is passed through JSON serialization. Ordinary metadata
is capped at 256 KiB, compiler/selection artifacts at 64 MiB, and instructions at
16 KiB. Node inspection and checkpoint copies use bounded pages.

Validation on 2026-10-16: **445 offline tests passed**, followed by **125 focused
checks** after the final workflow refinements. Real Lean 4.35.0-rc3/bubblewrap
checks passed supplied and scripted proofs, authenticated resume, independent
reviews with research continuation, decomposition/assembly, evidence-preserving
forks, partial exports and standalone verification, objections and generation
rebinding, joint definition/property acceptance, and probes with inherited aliases
while sibling work proceeds. Scripted/provider fixtures exercise the transport
boundaries without calling an external model service.

Validation commands, run from `new/` (use an absolute interpreter path for the full
suite's executable-path checks):

```sh
/home/ubuntu/midas-formalizer-v2.1/.venv/bin/python -B -m unittest discover -s tests -t . -q
../.venv/bin/python -B -m tests.benchmark_application
# Requires permission for Lean/bubblewrap under the root AGENTS.md:
../.venv/bin/python -B -m tests.check_application_lean /absolute/path/to/toolchain/bin/lean
../.venv/bin/python -B -m tests.check_joint_lean /absolute/path/to/toolchain/bin/lean
```

The application benchmark compares warm inspection commands and idle
application/controller ticks against v2 using seven alternating sample medians.
On 2026-10-16 their new/v2 ratios were **0.634** and **0.768**, respectively.
It does not measure complete model/Lean throughput. Fork checks additionally assert
that compatible completed runs create no new compiler or model executions.

## Export

Four implementation modules plus `__init__.py` own export responsibilities:

| Module | Responsibility |
| --- | --- |
| `protocol.py` | Binary requests, exact proof/receipt references, file inventories and admitted stages |
| `proofs.py` | Shared source declarations, polymorphic and frozen-peer composition, definition replay and source integration |
| `layout.py` | Capture replayed libraries, stream exact files, publish without replacement and read standalone bundles |
| `service.py` | Stage eligibility, request preparation, current-proof checks and authenticated stage admission |

Construct `Export(checker)` and call `plan(selection, graph, ...)` with the current
admitted integration and verification, if present. Planning reads selected proof
metadata only. It returns ready assemblies first, then integration when all roots
are verified, then independent verification. Changed root receipts, child proofs,
generations, selection or profile invalidate their dependent stages.

Set `allow_sorry=True` on both `plan` and `prepare_integration` to export incomplete
work. This path integrates immediately: unresolved goals become typed `sorry`
helpers, and conditional parent proofs are composed using those helpers and any
available verified children. Shared dependencies are emitted once; deep graphs use
an iterative traversal. Unselected unfinished declarations remain in the source.
The default remains `allow_sorry=False`.

```python
work = export.plan(selection, graph, allow_sorry=True)
request = export.prepare_integration(selection, graph, reader, staging, allow_sorry=True)
```

The option is saved in `Request.allow_sorry`, follows independent verification and
standalone rechecking, and produces distinct audit evidence. The audit allows
`sorryAx` while still checking target signatures, elaboration errors, other axioms,
and kernel replay. Partial export does not admit assemblies or change node proof
states. Its flat snapshots fence every consumed node, including unresolved leaves;
changes to their versions, interfaces, states or selected receipts invalidate the
export. `current(..., allow_sorry=False)` only accepts the strict mode.

`prepare_assembly` takes the selected conditional check's original compiler request
and exact verified children. Composition shares named declarations and discharges
polymorphic assumptions without expanding transitive proof trees. Earlier selected
definition values are replayed before dependent frozen-peer checks. Checker kernel
replay produces an `AdmittedCheck(ASSEMBLY)` for `domain.accept_assembly`.
Native-cache restoration rebuilds namespace prefixes from kernel-checked declaration
names, so later source proofs can open dependency namespaces after cache reuse.
Cached elaborator extension state remains excluded.

`prepare_integration` renders checked roots through the shared source renderer and
writes raw source to the supplied staging capability. Authored scopes, attributes,
definitions and source suffixes survive replacement. Strict export rejects
unselected unfinished work. Its compiler stage audits the integrated source but does not complete an
export. `prepare_verification` binds the exact admitted integration and file set.
`Request.inputs`, `expected_versions`, `kind` and `payload` supply the frozen job.

During export projection, compiler-resolved deprecated theorem names are updated
when the replacement has exactly the same type, binder names and binder kinds
(after matching universe parameters). Data definitions and changed APIs are left
alone. Lean's executed-goal analysis identifies unnecessary `tac1 <;> tac2` uses;
only these become `(tac1; tac2)`, with parentheses preserving tactic grouping.
This cleanup is limited to generated declarations. The existing independent
verification checks the rewritten source, so cleanup adds no compilation round
or model call and leaves submitted proof artifacts intact.

Independent verification uses a fresh compiler workspace. It rebuilds source-bearing
library modules in dependency order, checks their compiled hashes against the admitted
interfaces, and rechecks every source/output after the final writable compiler
process. Explicitly trusted compiled dependencies are mounted read-only from the
frozen environment and checked again after the final audit. The original project's
compiled files are excluded. The audit elaborates the complete entry and independently
kernel-replays its declarations, including non-target declarations. Selected target
signatures must match exactly after erasing generated macro scopes from binder
names, which can change when compiling the source in another module. Binder kinds,
types, constants and universes remain exact. Native proof caches and retained
import workers are not used for this stage.

`prepare_library(environment, modules, replay, dependencies, staging)` captures an
environment-admitted source closure. That earlier replay supplies provenance and
build order; export still performs its own rebuild. The source profile uses the
toolchain alone. The library profile adds dependency sources, admitted compiler
options, and compiled hashes.
Plugins and compiler/environment overrides are outside these source profiles.

`prepare_compiled_library(environment, modules, source_order, dependencies, staging)`
captures compiled dependency identities plus ordered local helper sources. A flat
library row with `source=None` records compiled trust; its complete sidecar hashes
must match the frozen dependency. No environment inventory is copied into these rows.

`export_operation(export, reduce, inputs)` connects all three stages to the existing
controller. `inputs(reader, invocation)` supplies current `selection` and `graph`,
plus `parent_request` for assembly or `integration` for verification. Admission
authenticates the controller receipt, exact request and compiler evidence before
returning a proof or stage handoff. Reducers commit under the usual version fences;
failed finalization preserves accepted proofs. Policy still owns retries and whole
export budgets: the application passes `Policy.export_limits(usage)` to each job and
the configured `max_bytes` to preparation. Every build subprocess shares its job's
cumulative wall, memory, output and evidence limits.

`Export.materialize` publishes only a current independently verified stage. Pass the
run directory in `protected`; frozen environment roots are protected automatically.
It streams and hashes files into a sibling staging directory, fsyncs them and uses
an atomic rename without replacement. An existing exact destination is idempotent;
different files, symlinks, extra directories and publication races are rejected.
Cancellation removes only private staging. Successful publication can then be
recorded by the application/controller.

Bundles contain `src/` Lean sources, `lean-toolchain`, `inventory.bin`, `request.bin`,
`audit.bin` and `manifest.bin`. Integration, verification and build evidence refer
to the same module inventory artifact instead of embedding its table in each
request or receipt. The inventory is a frozen input and travels with the bundle;
every read verifies its hash and length. `Library.decode()` and `load_inventory()`
return shared tuples of frozen module records: source/asset references are typed,
and options and compiled hashes are tuples. The bounded 32 MiB inventory cache
accounts for both input bytes and decoded records. A cache hit skips decoding,
schema validation and canonical re-encoding; the first admission still performs
all three checks. General-purpose binary decoding still returns detached mutable
graphs. File metadata and requests use the binary codec; source is never
hex-encoded or passed through JSON. Only environment/toolchain hashes cross these
boundaries. Generated proof names have fixed namespace depth. `read_bundle` checks
the exact inventory, and `Export.prepare_recheck` prepares a fresh checker audit
from the bundle's files using any admitted environment with the same toolchain
hash. This needs no original project, compiled library or run database and grants
no authority to mutate the original run.

On the saved 8,967-module Morley inventory (4,326,109 bytes), seven alternating
samples on 2026-10-18 reduced repeated verified inventory loading from **161.74 ms
to 3.63 ms CPU** (44.5×). This benchmark includes fresh byte copies and hash/length
verification, but no disk I/O or Lean execution. The retained inventory uses
**14.6 MiB**, and first admission costs about **0.62 seconds**, versus **0.51 seconds**
before caching decoded records. The saving pays for that initial cost on the
second lookup. **80 relevant non-Lean tests passed**, covering immutable records,
cache bounds, corrupted inventory files, export/recheck and application consumers.

Validation (from `new/`):

```sh
python3 -B -m unittest tests.test_export -v
python3 -B -m tests.benchmark_export
python3 -B -m tests.benchmark_inventory # Optional argument: a saved library.bin
# Requires the Lean permission specified by root AGENTS.md:
python3 -B -m tests.check_export_lean /path/to/lean/bin/lean
```

Validation passed: **420 Python tests**, including **10 export contract tests**,
and the real Lean export suite. The contract checks cover stage ordering, exact child/root fences, false seals,
missing build evidence, dropped target signatures, stale completion, library hash
mismatches, standalone bundles and publication conflicts/cancellation. Real Lean
checks cover ordinary proofs, structures and recursive definitions, polymorphic
assembly, joint definition/property proofs, direct and composed definition peers,
and library reconstruction. Alternating seven-sample medians against v2 measured
new/v2 ratios of **0.92** for small integration, **0.44** for 2 MiB integration and **0.79** for polymorphic
assembly. These measure Python preparation/rendering, not compiler wall time.

## Controller

Five implementation modules plus `__init__.py` replace the controller lifecycle
responsibilities distributed across the twelve v2 modules in the blueprint:

| Module | Responsibility |
| --- | --- |
| `protocol.py` | Bounded effects, frozen planned work and the fixed operation table |
| `records.py` | Small binary reservations, frozen retry rules and indexed budget projections |
| `allowances.py` | Atomic candidate/helper reservations and candidate settlement |
| `operations.py` | Existing provider, checker and search receipt admission |
| `service.py` | One loop for progress, receipts, planning, dispatch, stop, drain and recovery |

Construct `Controller(writer, executor, policy, operations, ...)` after freezing
the run. One controller owns each writer. `Operations` has explicit slots for
model, binding, proof, lookup, probe, search, assembly, integration and export
verification. There is no dynamic registration or separate live/scripted loop.
`model_operation`, `checker_operation` and `search_operation` use the existing
owning services. Compiler memory-limit failures retain their distinct policy
classification. Their reducers receive authenticated results and current storage
reads, and return `Effect` values. The application supplies those reducers, a
bounded planner and the current root/export completion projection. Candidate
lowering, graph selection, helper continuations and export algorithms remain with
their owning services. `export_operation` connects the export service to the same
loop. Application supplies the complete workflow above; CLI remains separate work.

An `Effect` combines storage `Changes`, `Planned` frozen requests, streamed
artifacts and post-commit cancellation. `publish(key, effect)` publishes bytes
before atomically saving their references, version checks, counters and work.
The caller lowers strategy/tool proposals into that effect; storage does not
interpret a raw strategy proposal. `reserve_candidate`, `settle_candidate` and
`reserve_helper` produce allowance effects to compose with the corresponding
checks/stages. Owner counters retain lifetime usage across generations. Candidate
exceptions refund the reserved mathematical attempt and keep their separate
exception charge. A helper request is charged once across its stages; research and web search
retain their own model resource profiles after reserving their separate allowances.

`step()` drains bounded progress, receives completions, admits saved receipts,
applies effects, plans changed state, then dispatches ready jobs. Ready and receipt
pages have cursors, so a blocked first page cannot starve later work. Completed
workers report their final progress count: their staging survives until every
queued message has been processed, even with a full progress channel. Invalid
progress cancels the emitter. Progress publication and commit acknowledgement
failures retain at most one batch for replay. Its completion fence advances only
after durable recording or explicit rejection. Proof state changes only through
the owning semantic reducer after receipt authentication; saving a successful
result alone selects nothing.

Local workspace/input preparation finishes before admission. Policy is rechecked
under the write transaction, and reservation plus the dispatch marker commit
together before any worker call. Workers receive only an artifact allowlist,
private staging and the exact capacity grant. A launch rejection follows the normal
not-sent receipt path. A crash after the dispatch commit retains uncertain work;
it does not infer that the provider was never called. Separately saved PREPARED
work is refundable during recovery. Retained compiler memory participates in
admission and is freed only after executor-confirmed eviction.

Worker artifacts, receipt bytes and accounting commit together through storage's
`prepare_receipt`/`receive` boundary. Semantic admission follows outside that
transaction. Application, downstream work and disposition then commit together.
Publication/commit acknowledgement failures retain the completion for replay.
Duplicate receipt delivery cannot settle usage twice. Stale work keeps its receipt
and original charge without acquiring current proof authority.

Model budgets use bounded per-invocation binary projections and indexed counters,
including unknown bounds and overrun flags. Admission never scans execution
history. `report_usage` applies cumulative late usage to its original invocation.
Retry rules and the first-attempt time are saved; retries reuse the exact request
and create a new original invocation. Provider cooldowns are scoped to frozen
routes, with indexed recent failures. `reset_provider` is an explicit application
recovery action after checking provider health.

`request_stop()` is safe from another thread. The loop persists the stop, cancels
owned workers and keeps receiving results. Cancelled receipts remain available
for `resume()`. On a newly claimed writer, `recover()` reconciles bounded batches;
saved receipts pass through normal semantic admission before adoption. Original
invocation IDs, charges and usage remain unchanged. `reconcile_termination`
requires an application OS observer to confirm exit before releasing uncertain
capacity. That confirmation does not grant proof authority.

`drain(timeout=...)` keeps polling progress while cancelling and returns `False`
if workers or uncertain processes remain. `run(progress)` uses the application's
current root/export projection with policy completion and the controller's actual
worker/recovery state. A failed final drain returns `recovery_required` with exit
code 2 and retains controller ownership until termination is confirmed. The writer
remains application-owned. Controller metadata contains artifact references and
environment hashes, with no environment inventory, large JSON operation or
recursive persisted structure.

Validation and benchmark commands (from `new/`):

```sh
python3 -B -m unittest tests.test_controller -v
python3 -B -m tests.benchmark_controller
# Requires the Lean permission specified by root AGENTS.md:
python3 -B -m tests.check_controller_lean /path/to/lean/bin/lean
```

The controller checks cover real storage, deterministic workers, threaded progress
backpressure, actual scripted-provider/strategy composition, crash replay, budget
accounting, retries, stop/resume and receipt adoption. The real Lean integration
checks target binding, direct proof selection, probes, sealed receipt admission
and confirmed drain. **216 offline controller/adjacent tests passed**, along with
the real Lean integration. Seven alternating benchmark samples measured new/v2
median ratios of **0.901** for the model lifecycle and **0.451–0.516** for idle
ticks with/without history. The comparison uses deterministic workers; it does
not measure formalization throughput or a live provider.

## Storage

Nine implementation modules plus `__init__.py` replace the storage-owned portions
of the twenty v2 modules in the blueprint. There is no v2 import or schema migration.

| Module | Responsibility |
| --- | --- |
| `protocol.py` | Explicit bounded changes, versions, run inputs and read views |
| `codec.py` | Shallow binary requests, proof-node records and exact worker results |
| `schema.py` | One SQLite schema, foreign keys, indexes and immutable history |
| `artifacts.py` | Worker staging, compressed archive segments and verified streams |
| `queries.py` | Bounded graph, state, intent, receipt and accounting reads |
| `transaction.py` | Artifact registration and atomic graph/state applications |
| `ledger.py` | Reservations, dispatch, receipts, cumulative usage and recovery actions |
| `integrity.py` | Agreement between saved records and indexes, iterative cycle checks |
| `service.py` | Writer lock, sessions, publication, receipt seals and structural recovery |

`create(directory)` requires a new directory and returns an active `RunWriter`.
Publish the input/configuration artifacts and call `tx.freeze(FrozenRun(...))`
before adding nodes or work. `FrozenRun` contains the environment **hash**, two
artifact references and optional parent run/revision. It cannot be replaced.
`claim_writer(directory)` acquires the exclusive run lock, checks the current
schema and storage integrity, reconciles archive files and starts a new session.
It does not call higher-level semantic validators or decide retries.
`open_reader(directory)` opens a read-only SQLite connection. WAL and FULL
synchronization protect commits; handles reject use in another process/thread.

The controller owns `writer.transaction(writer.session)`. It never passes SQL
connections to workers or domain services. Operations use savepoints, so catching
a failed operation cannot commit its partial effects. `Changes` commits node
updates, versioned artifact pointers, counters, roots and downstream intents as
one application. It checks owner generations, node/record revisions, expected
counter values and graph revision. An exact application replay is a no-op;
reusing its key for different effects or another receipt fails. The mutable-state
revision advances for state/ledger changes, not standalone immutable artifacts.

Proof nodes are stored individually, with indexed selected dependencies and
immutable node history. Updates check current dependency bindings, direct
consumers and cycles without decoding the whole history. **Callers must first
validate proof authority through domain/checker/strategy.** Constructing a stored
node, publishing an artifact or retaining a successful worker receipt does not
perform a Lean check. Historical semantic revalidation remains with the owning
services. Structural recovery uses streamed records and disk-backed temporary
degree tables, including for deep dependency graphs.

Versioned `(scope, key)` artifact pointers cover frozen model/script inputs,
submissions, drafts, memory, helper/archive indexes, guidance and repair state.
Counters retain explicit allowance changes. The controller chooses these scopes
and maps service proposals to storage values; storage does not interpret their
policy or schemas. A `None` pointer removes the current value while preserving
earlier revisions. Probe progress is stored separately by original invocation
and sequence, with exact duplicate checks and artifact foreign keys.

`reserve` saves the exact `FrozenRequest`, reservation artifact, resource bounds
and original invocation. The controller evaluates policy against current reads
inside the transaction and supplies `expected_revision`; storage also rechecks
owners, prerequisites and readiness. Dispatch is recorded before external I/O.
Model invocations retain one turn each, cumulative usage and separate unknown
counts. Repeated usage reports are idempotent; conflicting or decreasing reports
and integer overflow fail atomically. Provider request IDs identify one original
invocation. Late correlated usage can update that invocation after retirement,
recovery or retry without granting it current proof authority.

Workers receive an `ArtifactAccess` allowlist and a private `Staging` capability.
Staging enforces byte/item quotas and one writer thread. Small staged values stay
in memory; large values use raw private files. Publication copies and verifies
them into storage-owned archives. Up to **16 KiB** stays inline in SQLite;
larger artifacts use independent **4 MiB** chunks, compressed only when smaller.
Segments rotate at artifact boundaries after a **256 MiB** target; a single
larger artifact remains streamed. Artifact identities are content-derived unless
an explicit reference is supplied. No source/proof/evidence JSON operations occur.

`publish`/`publish_staged` return `Durable` values after file synchronization.
`tx.register` then commits their metadata, possibly with the referencing effects.
Recovery validates committed placements before truncating unpublished tails or
removing orphan segments/staging. Unreferenced interior segment bytes are retained;
there is no compaction or deletion of committed artifacts. Every read verifies
lengths and hashes. Unchanged filesystem stamps can reuse archive integrity checks
on reopen; `force_artifacts=True` forces rehashing. This cache does not admit
semantic receipts. Executor workspace retirement remains coordinated by the
controller after confirmed process termination and artifact publication.

The receipt flow is explicit:

```python
published = tuple(writer.publish_staged(staging, ref)
                  for ref in result.staged_artifacts)
with writer.transaction(writer.session) as tx:
    for durable in published:
        tx.register(durable)

receipt = writer.seal_result(invocation, result)
with writer.transaction(writer.session) as tx:
    tx.receive(invocation, result, receipt)

# Invoke the owning semantic validator, using writer as ReceiptVerifier.
# Convert its admitted effects to a storage Changes value.
with writer.transaction(writer.session) as tx:
    tx.apply(application_key, changes, receipt=receipt)
```

Seals bind the original run/session, invocation, request, reservation and exact
binary worker result with a private run key. Checker, provider and search already
use that result encoding. `verify_receipt` requires the matching committed receipt.
`result` reconstructs a saved worker result for normal semantic admission on resume.
`adopt_receipt` transfers application authority to the new session while keeping
the original invocation and charge. Prepared work can release its reservation;
dispatched work becomes UNKNOWN in bounded `reconcile` batches and retains its
charge/capacity. Retry and termination reconciliation require explicit controller
decisions. A not-sent receipt refunds a model turn only with confirmed termination
and no contradictory usage. Receiving a result never selects a proof.

Read pages cap at **1,000** rows and use key cursors. `work_snapshot` reads explicit
owners and state keys consistently. `snapshot()` pins a consistent parent
checkpoint for paginated fork reads; `copy_artifact` streams authorized bytes to
the child. Fork selection and evidence compatibility remain domain/application
decisions. Status uses maintained counts and totals, with immutable cached views
invalidated by local writes or external SQLite commits. Accounting exposes known
usage, unknown counts, held bounds and active capacity separately; policy owns
budget interpretation, prices and retained-worker accounting.

Validation: **44 storage tests and 96 adjacent domain/policy tests passed**.
Storage coverage includes interrupted publication and commits, truncation and
corruption, session fencing, uncertain dispatch, cumulative/late usage, receipt
adoption, graph retirement, a 1,100-level dependency chain, paging and live reader
invalidation. No Lean, live provider or external integration was run.

Seven alternating benchmark samples measured new/v2 median ratios of
**0.992–1.004** for small inline writes, **0.902–0.960** for 64 KiB/1 MiB writes,
**0.096–0.958** for reads, **0.130/0.942** for small/1 MiB staging, and **0.814**
for a complete task lifecycle. Repeated status reads measured **0.224–0.303**.
Inline writes are effectively at parity within filesystem timing variability.
The new lifecycle batches intent/reservation and receipt/effect commits; the v2
comparison uses its public task API. These are local storage measurements, not
formalization throughput. A separate 5 MB publication/read check disabled JSON
library operations throughout.

Validation and local benchmark commands (run from `new/`):

```sh
python3 -B -m unittest tests.test_storage -v
python3 -B -m tests.benchmark_storage
```

## Strategy

Six implementation modules plus `__init__.py` replace the strategy-owned parts
of the sixteen v2 modules mapped in the cleanup blueprint:

| Module | Responsibility |
| --- | --- |
| `protocol.py` | Strict default prover/reviewer contract interpretation with bounded wire decoding |
| `candidates.py` | Flat candidate expansion, ordered checks, child contexts, exact revisions and supplied admission |
| `memory.py` | Immutable semantic lessons, scoped evidence pins, admitted observations and whole-record retention |
| `state.py` | Frozen snapshots, dispatch bindings and atomic proposed effects |
| `service.py` | Planning, prompt preparation, model/check/review reduction and helper continuations |
| `repairs.py` | Current direct-consumer objection delivery, policy-gated retirement and repair grants |

The package does not import v2 or perform SQL, filesystem writes, provider
invocations, compiler launches or scheduling. Scripted model responses use the
provider's existing scripted transport and enter `on_model` through the same
admitted-output boundary. Supplied candidates use `submit`, share domain/context
validation and checking, and require explicit admitted reviews for conditional
plans. Supplied workflow does not schedule model turns.

`Context` contains the selected subjects, one flat definition block, explicit
prerequisites, advertised entry references and imports. `child_context` projects
only the child's declared premises and retains inherited imports. Definition and
child dependency cycles are rejected by domain/checker boundaries; candidate
check order visits dependencies first. Unproved children receive binding checks
only. Definitions remain named declarations with bodies, never assumed facts.
Joint definition/property candidates require every preceding selected definition
as an explicit dependency and may include supporting lemma entries. Lemmas use
`scope_target` to name the selected declaration supplying their source context;
targets leave that field null. Supporting obligations use the ordinary recursive
decomposition, review and assembly workflow. A definition depending on lemmas is
assembled into a closed proof before its value is replayed in later targets.
Reviewed joint proofs remain private while that work runs, and required roots
enter domain's atomic `accept_joint` path together. Candidates with only closed
definition dependencies retain the direct-attempt classification.

Decomposition children choose between a complete Lean term in `proof_body` and
`proof_body: null` with English guidance in `proof`, which may be empty. Both forms can occur
in the same flat child list and use explicit `depends_on` names. Supplied Lean
children are checked directly in dependency order, without child prover or
reviewer calls. Only English-guided children receive new prover contexts and
reviewer-selected declarations. A failed supplied term returns diagnostics to the
proposing context, where its `/entries/<child>/proof` fragment can be revised.
A revision may clear a supplied child Lean proof by setting `new: null` on
`/entries/<child>/proof`. Use `old: null` or the exact whole old term. The child's
`/children/<child>/proof` guidance carries over when present and defaults to an
empty string when missing; it can also be edited in the same revision. Missing
guidance does not request another revision. Clearing and guidance edits apply
atomically, and the result receives the ordinary English-child review and work.
This also works when the earlier Lean attempt already bound the child. Current
goal and required target proofs cannot be cleared. Other fragment replacements
remain strings; supplying Lean for an English-guided child still requires a
complete new candidate.

Guidance edits use the same `{path, old, new}` format as Lean proof edits.
`/entries/<child>/proof` edits Lean code; `/children/<child>/proof` edits English
guidance. Copy the displayed fragment path for the child entry key. Set `old: null`
to replace the whole guidance, or use an exact matching substring for a partial
edit. Set `new: ""` to clear guidance. For example:

```json
{"action":{"mode":"revise","rationale":"Clarify the child's approach.","edits":[
  {"path":"/children/helper/proof","old":null,"new":"Apply induction on n, then simplify the successor case."}
]},"memory_update":null}
```

Guidance can be edited by itself or alongside a Lean proof edit in the same `edits` list.

Independent review remains for the current subject's composition when it proposes
English-guided children. All-Lean decompositions go directly through checking and
assembly. Conditional supplied terms retain their checked prerequisites and need
verified dependency proofs before assembly can close them. Their local acceptance
uses the compiler receipt; it does not invoke a model or certify those dependencies.
The general `candidate` action uses the same routing for its entries.

`Draft` saves the binary submission, separate English guidance, and a flat list of
entries requiring independent review. Its binary format is `MIDASDRAFT` version 2.
Exact named fragment edits all resolve against the displayed base before any replacements.
Disjoint spans apply atomically, independent of edit order; each affected fragment
is rebuilt once. A whole-fragment replacement cannot share its path with another
edit. Unknown or unadvertised paths are ignored without discarding valid edits in
the same response. If all paths are ignored, the base is unchanged and no candidate
attempt or revision allowance is charged. Edits to available paths still reject
stale bases, overlapping edits, ambiguous matches and no-ops. Inherited definitions,
target statements and candidate structure have no editable paths. Local references
bind to the controller-allocated new submission ID. `fragments` exposes the exact
strings for prompt presentation.
An edit to an available path that cannot apply (including missing or ambiguous old
text or a no-op) returns revision feedback with the candidate unchanged.
The next prover prompt includes the reason and correction guidance. These failures
consume model turns but no protocol revision allowance or mathematical attempt.
Successful Lean proof bodies and their edit paths are omitted from prover revision
prompts; names, statements, dependencies and compilation status remain visible.
The revision validator applies the same filtering. English guidance remains editable
for reviewer corrections, and independent review still receives the full proofs.
Consecutive revisions replace the displayed candidate. Prompts include each failed
node's latest compiler feedback, or the latest review feedback, and the latest
failed-edit message after that check, if any. A failed edit leaves the current check relevant; a new check
supersedes both earlier check and edit failures. Older results stay saved, while
strategy, lessons, objections and helper evidence retain their existing treatment.

The controller supplies an immutable `Snapshot`, then calls `Strategy.plan`.
Returned `Work` values describe operations to lower through the owning component.
`prepare_model` renders once and saves a `Turn` binding the exact schema, wire,
context, owner, graph revision, memory/evidence versions, base and review candidate.
The review presentation must bind the candidate artifact and each dependency's
owner/interface. `Dispatch.bind` checks the final frozen provider request against
those wire bytes and owner versions. Replacement renderers can change wording and
layout; replacing the schema requires an explicitly supplied matching interpreter.

`on_model` accepts provider-admitted complete output for that exact dispatch.
Partial, refused, cancelled and failed output cannot produce candidates or memory
changes. A model approval permits a conditional selection only alongside the exact
checker-admitted composition. Binding checks, research, lookup, probes, page reads
and model claims cannot establish proof authority. `activate` restores a selection
from previously admitted check/review handoffs during recovery.

Helpers use tools' existing plans and receipt projections. Search-to-lookup
continuations use `tools.continue_retrieval`; `on_helper` accepts the final stage
set and resumes the original prover or reviewer operation without charging another
helper request. Probe case bytes are proposed for controller publication before
their referenced jobs are admitted. Recall/page requests require advertised
result IDs/cursors, use tools' scoped indexed reads and preserve duplicate delivery
keys. A duplicate page resumes the turn without replaying its content.

Every `Changes` value carries expected versions. The controller must use storage
to publish proposed artifacts, reserve candidate/helper allowances, dedupe
the original receipt application, recheck versions and commit all effects and
downstream intents together. `AttemptDecision` closes one candidate reservation;
policy charges only mathematical success/rejection. Resource/protocol revision
allowances remain separate. An explicit statement objection targets only current
direct consumers. Repair proposes graph retirement and a policy-sized grant in
one effect; cancellation occurs after commit and preserves shared reachable work.
Transport retry, receipt authentication, durable recovery and export execution
remain with controller, checker/provider and export.

Model JSON is parsed with the shared bounded, chunked decoder; fences and prose
wrappers are rejected. Responses, contexts and submissions are capped at **8 MiB**;
source/proof strings at **1 MiB**. Drafts use length-prefixed raw binary fields;
there is no large JSON serialization, hex encoding or recursively embedded parent
context. Memory has a **16 KiB** soft target and **64 KiB** hard limit. Retention
evicts whole older records and counts omissions. Model lessons remain separate
from bounded diagnostic excerpts and their admitted evidence references.

Validation: **33 strategy tests and 133 adjacent domain, policy, tools and prompt
tests passed**. These are offline Python tests; no Lean or live model was run.
Seven alternating benchmark samples measured new/v2 median ratios of **0.884**,
**0.699**, and **0.299** for complete candidate interpretation/admission with
16-byte, 1 KiB and 8 KiB proof payloads, and **0.176** for memory updates. New-only
900 KiB response admission and exact revision took approximately **4.2 ms** and
**3.4 ms**. These measure local strategy work, not formalization throughput.
V2 benchmark JSON inputs stay below 16 KiB; large new workloads run with JSON
library operations disabled.

```sh
python3 -B -m unittest tests.test_strategy -v
python3 -B -m unittest tests.test_decomposition -v
python3 -B -m tests.benchmark_strategy
# Requires the Lean-test permission described in AGENTS.md:
python3 -B -m tests.check_decomposition_lean /absolute/path/to/toolchain/bin/lean
```

## Prompt

Each default renderer owns its wording and context layout in one file:
`prompt/prover.py`, `prompt/reviewer.py`, `prompt/stop_reviewer.py`,
`prompt/researcher.py` and `prompt/web_searcher.py`. Decomposition and stop reviews
share the reviewer verdict contract and configured model route. Strategy dispatch
selects the renderer by the planned operation (`review` or `review_stop`), including
helper continuations; each call receives only that operation's instructions.
Their renderer identities also separate prompt cache keys. Shared `protocol.py` defines immutable display inputs;
`wire.py` builds provider wire bytes. There is no import of the old implementation,
database lookup, provider call, or environment inventory in this layer.

The v2 structure was traced through `midas_formalizer/strategy_context.py`
(role contracts), `strategy_contract.py` (effective instructions),
`strategy_prompts.py` (block order and caching), `strategy_presentation.py`
(readable candidate/evidence projection), and `research_protocol.py` (research
context). Those files remain in the repository root for reference. The v2.1
adaptation keeps these responsibilities without importing the old implementation:

| v2 prompt block | v2.1 presentation |
| --- | --- |
| Fixed role instructions and response schema | Role wording followed by the response schema as prompt text; renderer versions are `prover-v13`, `decomposition-reviewer-v1`, `stop-reviewer-v1`, `researcher-v3`, and `web-searcher-v2`. |
| Exact local task and formal context | Source prefix, named definitions, exact subjects, and dependency aliases paired with their checked types. Prepared/inherited imports are explicit. |
| Retained history dictionary | Flat retained observations; superseded candidate-check and edit failures are excluded from prompts. Compiler feedback reserves error headers and adds bounded first/last-five-line excerpts. No prior-session access. |
| Current candidate and checked interfaces | Ordered entries, definition scopes, dependency map, English guidance, and exact fenced editable fragments. Reviews identify the selected entry explicitly. |
| Current turn: memory, feedback and evidence | Strategy, named lessons, pins, evidence pages and latest feedback, followed by changing capabilities and the action request. Omitted diagnostics and evicted memory are counted. |
| Research context and question | Readable source, exact task, proof guidance, checked interfaces, available candidate/evidence and the question. |
| Web-search context and question | Formal context, proof guidance, checked interfaces, web allowance, question and URLs. Candidate/history/evidence archives are excluded. |

The mathematical guidance retains v2's requirements for useful decomposition,
explicit child hypotheses, concrete next steps, consistent witnesses and joint
outputs, and independent review. Response fields and helper behavior follow v2.1:
there are no copied v2 identity fields or import actions. A `stop` action has a
nonempty `rationale` and nullable `objection`; a statement objection contains
`category` (`suspected_false`, `missing_assumption`, or `context_mismatch`) and
nonempty `evidence`. It requests independent reviewer approval to stop this node.
For this review, `accept` stops the node and delivers any statement objection to
its current parents; `reject` requires at least one nonempty `suggested_revision`
and continues the prover with the review feedback. This is an advisory work decision,
not a compiler certificate of unprovability.
Probes follow v2's body-only approach: each case has only `id` and `code`, where
`code` is the TacticM body following `run_tac do`. The application rejects outer
`by`, `run_tac`, `run_elab` and `do` wrappers, then adds `run_tac do` and indentation.
The generated code must fit 16,000 UTF-8 bytes; diagnostics refer to that generated
code, with the body starting on line 2. Use
``Lean.Elab.Tactic.evalTactic (← `(tactic| intro C))`` to run a proof tactic in the body.
This schema is versioned as `prover-action-v5`. A new direct proof replaces the
previous decomposition; keeping proposed
children requires a complete decomposition or an exact revision of its fragments.

Accepted decomposition reviews include `child_declarations`, a list of
`{"child": "entry-key", "names": ["Nat.add", "Nat.add_zero"]}` rows. The reviewer
selects useful declaration names for each pending child; an empty `names` list is
appropriate when no useful declarations are known. Rejections, helper verdicts,
and stop reviews use an empty `child_declarations` list. The review contract is
`review-verdict-v2`. Suggestions returned with non-accept decisions or stop reviews
are ignored. Unknown child keys and invalid declaration identifiers are skipped;
valid suggestions for existing children still apply.

Before a child's next prover call, the application looks up the selected names in
that child's frozen source/import context and checked dependency aliases. Checked
types and available original definition sources appear as ordinary evidence with
the problem. Large results use the existing evidence pages and prompt budget.
These automatic lookups consume no model turn or retrieval allowance. Unknown
names and terminal lookup failures are shown as feedback and child work continues.
Names are deduplicated and sorted before lookup so prompt excerpts have a stable
order. Shared children combine suggestions, and saved lookup progress survives
resume and fork.

Replacement means supplying a different `renderer(snapshot) -> Prompt` callable.
It may change instructions, the schema, and the ordering/presentation of context.
The application selects that callable for each configured model; there are no
model-name heuristics, global registration, or dynamic imports. For example:

```python
from midas.prompt import Options, Section, Snapshot, build, prover, subject

snapshot = Snapshot(
    sections=(
        Section('source', 'Main.lean', frozen_prefix),
        subject('goal', checked_signature),
        Section('capabilities', 'remaining', frozen_allowance_text),
    ),
    artifacts=(source_ref, context_ref),
    owners=(owner_version,),
)
renderers = {'model-a': prover.render, 'model-b': my_prover_render}
request = build(snapshot, Options(route.transport, route.model),
                renderer=renderers[route.model])
```

Section bodies are UTF-8 bytes. The caller supplies the exact source prefix,
named definitions, checked dependency interfaces, current candidate, history,
memory, capabilities and diagnostic locations as applicable. `subject` uses the
source layer's telescope renderer. The source layer also supplies `render_context`
for named definitions. No arbitrary object graph or JSON packet is displayed.
Evidence sections arrive in preference order (for example pinned pages first);
the default renderers include whole pages within `evidence_bytes` and report any
omissions. Required mathematical context and candidate fragments are never clipped.
Input and wire limits are 8 MiB; excessive escaped output fails before dispatch.

Application review prompts show the candidate without editing paths, preserving
full definitions, statements, proofs, dependency maps and English plans. They
omit prover memory, previous failures, search roots and prover action menus.
Within the retained evidence window, reviews include successful lookup excerpts
whose names appear in the formal context or candidate, plus research/web-search results tied to the
exact review entry and candidate. General search/probe history is omitted.
Evidence matching is lexical, not a transitive dependency analysis.
Executor trace lines (`MIDAS_THREAD_GUARD`, `MIDAS_RUNTIME_OPEN`, `MIDAS_TRACE`)
are removed from displayed diagnostics for both roles and before new compiler
failures enter prover memory. Compiler feedback reserves error headers within
the existing 8,000-byte allowance, then adds the first five and last five lines
of selected error blocks (the header counts as a line). Selection cycles across
error kinds and, within each kind, distinct file/line locations before revisiting
a location. Diagnostic codes identify kinds when available; otherwise the error
heading is used, ignoring quoted identifiers and numerals while distinguishing
failed tactics. Short blocks remain complete. Excerpts that cannot fit retain
only their headers, and other excerpts can still use the remaining space.
Errors are displayed in their original order regardless of selection order.
Omitted middle lines, details, and any headers that exceed the allowance are
counted explicitly. Compiler and probe feedback silently omit warnings, including
warning counts and probe source excerpts needed only for warnings. Full diagnostics
remain in the original evidence artifact. Prompt diagnostics (including saved
history and feedback) omit a trailing `unapproved axiom: sorryAx` checker error
when a preceding compiler error explains the synthetic sorry. Standalone axiom
rejections remain visible. Failures without error headers retain a
bounded first/last-five-line excerpt after warning removal. Probe errors, information
messages, goals and explicit sorry/metavariable flags remain visible.

The default prover contract returns `{action, memory_update}`; the action covers
proofs, decomposition, joint candidates, revisions, stop requests and helpers. The
reviewer returns `{verdict, rationale, findings, question, urls}`. The researcher
returns `{answer, unresolved}` for mathematical guidance. The web-searcher returns
`{answer, sources, unresolved}` for resources and references, with source locations,
hypotheses, excerpts and summaries. Response interpretation, semantic validation, helper lowering and
budget enforcement belong to the strategy/application layer. Replacing a
schema requires a matching interpreter there; transport never interprets actions.
Both prover and reviewer call either helper directly and resume their original turn
when it finishes. Use action/verdict `research` for guidance and questions; use
`web_search` to find references. Prover research actions contain `question` and no
`urls`; reviewer research verdicts leave `urls` empty. Web-search requests accept
up to six public URLs in addition to the question.
Research snapshots include the formal context and available candidate/evidence,
with the configured input budget applied. The researcher can use sources already
found by the web-searcher. Web-search snapshots supply the formal context,
question, URLs and frozen web-call allowance. Only the web-searcher can browse;
it requires OpenAI Codex. Research supports Codex, Azure and Bedrock Responses.

Store `request.wire` and, for Codex, `request.setup` as immutable artifacts and
pass those references to `provider.ModelRequest` with `request.provider_role`.
Provider roles are `research` for the researcher and `web_search` for the web-searcher. The prompt identity binds the
renderer/contract, exact bytes, artifact references and owner generations. The
provider request separately binds the saved route and invocation limits. Retry
the saved bytes; do not rebuild prompts from current state during retries.

The response schema follows the role wording in the instructions, with a request
for one matching JSON object without Markdown fences. Responses omits `text.format`;
Codex omits `turn/start.outputSchema` and includes the schema in `baseInstructions`.
Strategy still validates responses before accepting actions. The schema and its
instruction header count toward the input budget and cache identity.
Model responses may contain prose or Markdown fences around a single complete
JSON object. Extraction respects strings and escapes; multiple objects, outer
arrays, malformed JSON, duplicate keys and schema violations remain errors.
This applies to prover, reviewer, researcher and web-searcher responses, not provider envelopes.
Default model byte budgets are 8 MiB of output, 16 MiB per transport frame and
64 MiB of events, allowing long responses without the previous 128 KiB cutoff.
These are byte limits, independent of the model's output-token limit.
Wire strings are escaped in bounded byte chunks,
without passing large content through a JSON library. Only small schemas and
transport controls use JSON serialization. Bounded caches reuse exact schemas
and fixed wire prefixes. Explicit Responses caching requires
`Options(..., prompt_cache='explicit', cache_scope=route.scope)`; its key includes
route/model/contract/controls and remains stable across changing turn material.
The checked-in prover and reviewer routes enable this mode. Null leaves caching
to the provider default and emits no explicit boundaries.

Following the [OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
prover requests mark the shared contract, the end of formal context, and each
retained evidence, history and candidate section. Earlier markers remain when more sections
are appended: moving a single marker to the new tail would lose the old lookup
endpoint. The provider can write at most four prefixes per request, while explicit
lookup considers the first two and latest fifty markers. The first two delimit the
contract and formal context in the default prover renderer.

Explicit reviewer requests mark only the shared developer instructions. Source,
goals, dependency interfaces, evidence and candidate text follow that
boundary, avoiding cache-write charges for large review material that is usually
read once. The instruction prefix must still meet the provider's minimum length
to be cached; no padding is added. This policy applies to newly built requests;
retries keep their frozen wire bytes.

In prover requests, retained evidence precedes history and the current candidate,
so changing a draft or memory does not invalidate those evidence prefixes. Memory,
feedback, action availability and the final action request follow the cache
boundaries. Evicting or reordering retained content still invalidates prefixes
from the first change.
The prover action schema has four cached variants for independent research and
web-search availability. Research needs its service; web search also needs web calls. Capabilities report
available mathematical and helper modes, revision availability and offered page
cursors, without attempt counts or stop-review mechanics. Prover stop instructions
require identifying the best next decomposition and its concrete obstacle before
stopping; missing library support alone is insufficient. Import descriptions
distinguish advertised additions from the source imports and their active transitive
dependencies. Review and stop-request limits
remain enforced internally. Unavailable helpers are omitted from the advertised
modes. Exhausting allowances (including
research and web search), gaining a candidate or changing page cursors preserves the prover schema
and cache key. Single-subject `proof` and `decomposition` actions remain in the schema
for joint targets, but capabilities direct the prover to use `candidate` instead.
Reviewer schemas omit unavailable research/web search and cache their four variants.
Strategy admission still checks every response and returns bounded rejection
details in the next prompt so invalid replies can be corrected.
Disabled caching emits no breakpoint annotations.
Codex effort overrides are frozen per turn; output/cache controls remain in its
pinned provider configuration.

Run offline checks and the local performance comparison with:

```sh
PYTHONPATH=new .venv/bin/python -m unittest tests.test_prompt tests.test_presentation
PYTHONPATH=new .venv/bin/python -m tests.benchmark_prompt
```

The benchmark compares small frozen blocks with v2 and separately measures large
raw source input in the new writer. It invokes no Lean, model or network service.
On 2026-10-17, after the probe contract update, the small Codex and Responses
new/v2 ratios were **0.248** and **0.713**; approximately 1.25 MiB of raw source
rendered in **3.9–4.5 ms**. These
measure prompt construction, not model proof success or provider cache hit rates.

## Tools

The nine helper modules in the blueprint are consolidated into six implementation
modules plus `__init__.py`:

| New module | Responsibility |
| --- | --- |
| `tools/protocol.py` | Immutable scoped actions, binary requests, stable identities, bounded stage descriptions and probe-body expansion |
| `tools/search.py` | Frozen source selectors, bounded search projection, hosted LeanExplore transport, worker execution and search receipt admission |
| `tools/bm25.py` | Lexical declaration extraction, frozen-corpus SQLite FTS5 cache and file-scope preference |
| `tools/bm25_score.py` | Compound token fields, identifier budgets and scoped span scoring |
| `tools/plans.py` | Policy-backed planning, exact reciprocal-rank fusion, checker-advisory projection, stage validation and original strategy continuations |
| `tools/research.py` | Mathematical guidance validation and advisory evidence |
| `tools/web_search.py` | Reference validation, web-call bounds, recorded action IDs/types, quote bounds and advisory provenance |
| `tools/evidence.py` | Readable search/probe results, frozen declaration excerpts, staged binary archives, indexed pages and duplicate-delivery keys |

These replace the tools responsibilities of `combined_retrieval`, `discovery`,
`discovery_helpers`, `lookup_helpers`, `lookup_source`, `probe_helpers`,
`research_helpers`, `research_protocol` and `retrieval_helpers`. Durable run SQL,
publication, scheduling and model/compiler execution remain with their owning components.
Research prompt wording and response schemas belong to `prompt/researcher.py` and `prompt/web_searcher.py`.
The original `midas_formalizer` files remain unchanged and are not imported.

Built-in search and package index construction run in a supervised Python child.
The child's address space is capped by the invocation's `memory_mib`; wall time,
cancellation, output capture and termination use the executor supervisor. SQLite
temporary tables may spill to disk. Warm searches reuse package/cache postings;
index construction passes bounded source-reference/path records and verifies
each file in the child without copying package sources. The parent retains only
bounded result excerpts. Custom injected search transports own their isolation.

`HelperRequest` freezes a retrieve, probe, research or web-search action, continuation,
forbidden names and `Scope`. Search and lookup are accepted only as internal
stages; they are absent from the model schema, capabilities and action parser.
For direct inspection, use `retrieve` with `names` and empty `queries` and
`backends`. For discovery, supply up to three queries and two backends; retrieval
merges and ranks the results, then checks the top twelve ranked names plus up to
twelve explicit names. Explicit names appear as standalone lookup results; checked
search hits appear within the numbered search results. Exact local identifier
queries also get checked, with generated declarations shown in their search section.
To inspect other search results, select their names in a names-only request, which
skips search. Use `page` to read more content from an existing result.
Both retrieval forms require the compiler and consume one
retrieval allowance. A names-only request with no allowed names is rejected
before scheduling, so it cannot leave the task waiting on zero jobs.
The scope contains an owner/generation, an environment **hash**, and one artifact
reference for the admitted selection/goal/alias context. No environment object,
toolchain or package inventory is embedded in requests. Model probe cases contain
only `code`. They are expanded once by `expand_probe_bodies` into internal binary
case records containing positional `id` values (`case1`, `case2`, `case3`), generated
`code`, `operation='tactic'`, empty `setup` and null `expected`.
Probe actions hold artifact references to these records; the checker owns their
Lean validation. Continuations retain the complete batch without another charge.

`plan_helper` uses `Policy.helper` for allowance and service checks, charges each
probe request once for its complete batch of one to three cases, and freezes the
web-search call bound. An advertised probe can use any valid batch size, including
when only one request remains. Plans describe work;
they never invoke a checker/model, open SQLite, start a private pool, or retry.
`Plan.admission` places the logical helper charge on the first initial stage.
The controller must reserve that allowance and the initial stage intents atomically.
The checker/provider lower stage descriptions into their exact frozen requests,
preserving the stage key, scope and initial helper charge. Remaining stages and
`continue_retrieval` reserve execution capacity without another helper charge.

Search hints are fused using exact integer reciprocal-rank weights over the bounded
rank domain. Explicit names retain priority. Local queries that are valid Lean
identifiers are sent directly to checked lookup ahead of BM25 names, within the
12-name selection bound, so generated declarations can appear first even when
absent from the lexical index. Equivalent query/result spellings are deduplicated.
Forbidden names and their equivalent escaped spellings, internal Midas names and
invalid identifiers cannot become lookup jobs.
Searches must all have exact, ordered outcomes, including explicit failures.
Names are checked before their availability is reported. Search-driven retrieval
retains authored theorem statements and definition bodies. Lookup always requests
original source for definitions and returns checked theorem signatures without proof
bodies. This also applies to lookup stages in retrieval, including those following
searches. Checked names and types precede source excerpts, including when a generated
declaration's source range points to its parent command. Model retrieve responses
have no `inspect` field.
`finish_helper` validates the expected stages and current scope and returns
advisory evidence with the original strategy continuation. Storage/controller must
commit this result once against current owner versions and retain every search,
compiler and model receipt, including failed stages.

Application prepares a `SourceIndex` from admitted frozen file references once per
environment. Package and attachment preparation build a CPU-only SQLite FTS5
index in the package's flat `search.sqlite` file before publication. Its bytes
count toward the package allowance. LeanEval preparation also ensures that this
index exists before publishing the workspace. The derived index stores its format,
environment hash and frozen source hashes as one corpus key; it does not change
the environment identity or contain an environment inventory.
Builds read verified artifacts with 2 MiB per-file and 512 MiB corpus bounds;
publication is atomic and concurrent builders share a cancellable lock. Warm
queries open package postings read-only, without creating a runtime search cache,
and read only the files supplying displayed excerpts, verifying those source
bytes again. A missing, outdated or corrupt package index falls back to the
application cache, keyed by the same corpus key. Files, postings and ranges use
SQLite/raw bytes, without JSON corpus serialization or a GPU/model dependency.

The index stores nine fields: lexical tokens, adjacent pairs, and adjacent triples,
each with name/documentation/header weights 3/2/1. Tokens retain words and ASCII
camel-case components, without stemming. Underscores and camel boundaries form
compound components; dots separate query units. Repeated equivalent query units
do not receive additional weight.

Each candidate is scored separately. Matching triples are selected before pairs,
with left-to-right ties and no overlapping positions. A selected `k`-component
span receives `k/N` of its identifier's score budget. Uncovered components receive
50% lexical credit within the remaining budget. If no compound matches, that
identifier receives 50% of its mean lexical score; ordinary words retain full
weight. This prevents longer identifiers from gaining weight simply by producing
more compound tokens. All postings are considered before the best 100 candidates
are selected, with scope filtering and corpus-wide IDF. Exact identifier
queries (including `_root_.` spellings) use checked lookup instead of an index
name match; checked declarations appear within their search results, with unindexed
generated declarations shown under that query's heading. Scores
get a 1.5× ranking multiplier only for an explicitly selected file/module;
directories grant no bonus. Ties preserve candidate ordering. Evidence retains
the compound/component score before the file multiplier.

The production scorer uses sparse compound replacements and native SQL fallback.
Component ID filters scan term postings instead of repeatedly seeking common
terms. Temporary scope/scoring tables are confined to a query savepoint and are
removed on success or cancellation without committing a caller's transaction.
The `midas-bm25-source-v3-compound-spans` format changes the corpus key, so older
three-field package indexes fall back to a freshly built disposable cache; newly
prepared packages include the compound index. No package environment ID changes.

Enabled in the production local-search path on 2026-11-03. Validation included
93 offline tests, nine-field equality for all 249,981 frozen corpus entries, and
unchanged top100 order/scores on all 232 frozen queries (23,100 candidate positions).
The real Lean retrieval check passed package-index reuse with a fresh application
cache, generated-name lookup, 12 detailed/50 total results, selected-name followups,
proof continuation, and authenticated resume. Validation artifacts and source
hashes are under `.local/search-production-20261103`. The experiment notes below
describe the pre-integration runs; their statements that production was unchanged
refer to those earlier runs.

Search returns up to 50 results. The first 12 include source excerpts (up to 6000
UTF-8 bytes each); subsequent named hits carry only names in the display. Lookup
can expand any of those names. Theorem and lemma excerpts retain documentation,
attributes and statements, ending before the proof. Definitions, abbreviations
and instances retain their bodies. If a theorem header boundary is lexically
uncertain, search retains its name for checked lookup and omits the source excerpt.
The derived index format is versioned so older full-proof excerpt ranges rebuild.
Anonymous declarations retain excerpts because
they cannot be looked up by name. When excerpts overflow the first evidence page,
a compact ranked name list comes first and details follow through ordinary paging.
Extraction is lexical, not an elaborated declaration inventory: macro-generated
names are not inferred, and anonymous instances appear as source hints. Thus the
frozen-source corpus differs from the standalone LeanExplore experiment corpus.

Dotted modules, paths and stems resolve against the frozen index; ambiguous
selectors require a qualified root. Frozen project sources support both
lookup excerpts and discovery. `project` is advertised alongside dependency roots;
selectors can use `project`, explicit project paths, or unambiguous file/module
names such as `ChallengeDeps` and `Submission.Helpers`. Exact directories take
priority over file stems, so `Submission` selects `project/Submission/` when that
directory exists; use `Submission.lean` for the source file itself. Hosted search
uses the fixed LeanExplore endpoint, rejects redirects and bounds input at 5 MB, with cancellation/deadline
checks between reads. `SearchService` implements the existing reserved-worker
callback and authenticates saved request/result bytes and receipt seals.

`admit_checked` consumes a checker-admitted advisory handoff and its exact compiler
payload hash; it checks lookup queries and probe case IDs/operations. The controller
must first bind that compiler request to the planned stage. Checker retains the
per-case start/completion protocol. `admit_research` and `admit_web_search` consume
admitted provider results bound by the controller to their respective stages.
Research accepts bounded answers and unresolved questions without web events.
Web search validates sources and bounds any recorded web actions; an answer can be
admitted without browsing. Source entries contain URL, title, location, excerpt,
summary and hypotheses, with no access flag or access checks. The cumulative 25-word
quote bound per URL still applies. Quotations remain provider-reported.
None of these tools creates proof authority.

Failed research and web-search calls retain any received raw output in their
archive provenance. Feedback includes the original question, failure reason,
saved timeout, requested URLs, and available output marked incomplete and
unverified. Truncated JSON is presented as text without parsing. The output is
paged; displays above 4 MiB are explicitly clipped while the full raw artifact
remains available. Empty outputs add no partial-output section.

Requests, results and archives use shallow binary records. The bounded external
wire decoder is shared in `domain/wire.py`; provider imports that same decoder.
Search/research wire parsing and large source/evidence handling make no JSON-library
calls. `prepare_archive` stages fixed pages of at most 16,000 bytes and a bounded
index. Guidance is projected to plain text before paging. Web-search results contain
the answer, sources and unresolved questions. Sources use numbered
labels; source IDs and web-action metadata stay in the original evidence and are
not presented in prompts. Search and probe records
are also projected to plain text before paging, so the
model sees declaration names, authored excerpts and source locations, or case IDs,
generated code, diagnostics, goals and exploratory status. Probe diagnostics, each
logged message, each goal and inspected expressions/types retain their first five
and last five lines when longer than ten lines, with an explicit omitted-line count.
Generated code and diagnostic locations remain intact; original results remain in
archive provenance. Truncation happens before paging so more errors fit on a page.
Combined retrieval shows explicitly requested lookups first, then search results
with automatically checked declarations embedded in their numbered entries.
Search titles remain declaration names. Checked details replace the indexed
excerpt beneath the source location instead of adding a separate lookup entry.
The indexed source remains a fallback when lookup source is unavailable.
New search displays omit declarations whose type or definition has already reached
a prover prompt, including source-prefix and inherited definitions. Names alone and
failed lookups do not count. Deduplication preserves the original ranks and detail
allowance without fetching replacements, so a search may display fewer than 50 hits.
Explicit name lookups remain available. The seen names survive resume and fork
within the same owner/context; paged details count only when the start of their
type or source is included in the prompt, after the configured evidence window
is applied.
A flat declaration-to-record index preserves review access
after pagination. Original
artifacts remain archive provenance. Container descriptors preserve object/array
shape for other structured evidence. Text fragments carry
field paths, UTF-8 byte offsets and total byte lengths;
they require reassembly before use as complete source. Each `read_page` call reads and
verifies one indexed page, with no archive-wide decoding or earlier-page scan.
The controller supplies an indexed duplicate-delivery check and rechecks the returned
delivery key and scope when committing. Scope changes invalidate cursors.
Delivered evidence pages and result archives remain saved. The `page` action fetches
additional undelivered pages using advertised cursors. There is no `recall` action.
Results use stable names such as `tool-result-1`, numbered by saved result order
within each obligation. The cursor `tool-result-1:2` requests page 2. Names also
identify evidence pins and remain stable across prompt truncation and resume;
archive hashes remain internal for integrity checks.
The 16,000-byte storage page bound still applies.

`prover.input_token_budget` in `config.json` controls the prompt's rolling evidence
window. Its default `null` includes all delivered pages; a positive integer such
as `100000` targets that many input tokens per request, including instructions,
schema, source, candidate, and evidence. This is separate from the cumulative
`run.max_input_tokens` budget. The equivalent setting is available on reviewer
and research/web-search routes. Set the input budget below the model's context limit to leave
room for output.

Goal and dependency types use Lean's readable display, captured during source
inspection and checked binding. This avoids expanding implicit arguments and
typeclass instances into large prompt expressions. Binding displays are retained
as raw artifacts and reused for all four role prompts; no extra
compiler invocation is needed per model turn. Structural expression graphs still
determine obligation identities, proof checking, and export. Readable text does
not replace those graphs or serve as proof evidence.

Validation on 2026-10-23: 199 focused Python tests and the real Lean checker suite
passed. Replaying the six saved Hadwiger bindings reduced the reconstructed review
request from 2,127,581 to 38,423 bytes (98.2%). The largest dependency display fell
from roughly 1.21 MB to 158 bytes, retaining its existential linear-map type.
Regression checks cover implicit and instance binders, existential binder types,
named definition contexts, and statements expressed using tactics. This measures
prompt bytes, not a new model invocation or an observed billing reduction.

The latest known input-token usage calibrates a prompt-text byte estimate for the
same owner, role, and provider route. Cached tokens are already included in input
usage. The small sample survives resume; missing/zero usage leaves it unchanged.
Before any sample, the estimate starts at one token per UTF-8 byte. Selection
leaves 10% of the configured budget unused for estimation error, drops the oldest
whole evidence pages, and reports omissions in the prompt. Required material and
the newest evidence page are preserved; if these alone exceed the estimate, prompt
preparation fails. No historical prompts or complete tool results are decoded to
estimate tokens. Existing prompt byte/section bounds still apply, and provider
context limits can still be exceeded because token density is only estimated.
Artifact publication and orphaned staging recovery remain storage responsibilities.

BM25 integration validation: offline contracts cover direct identifier lookup
priority, scope-before-candidate selection, file-only preference, raw score
roundtrips, cache reuse/invalidation and concurrent builds, 12/50 delivery, paged
overflow, and tail lookup. A real Lean 4.35.0-rc3 application check passed search,
generated recursor lookup appearing first, checked tail source lookup, proof
continuation and resume.
On the experimental Mathlib checkout (`aae61ae`), building 249,981 lexical
declarations from 8,264 files took 22.6 seconds and produced a 63 MiB index. Warm
simplicial-nerve search took about 8 ms versus 2.2 seconds for v2's scan of the
same 1,090-file scope. These are local search timings, not proof throughput.
Its 25 results and first 12 details fit in a single page (9.1 KB of display text).

```sh
# From new/; offline tests and source-only benchmark:
../.venv/bin/python -B -m unittest tests.test_search tests.test_source tests.test_tools tests.test_application tests.test_prompt tests.test_strategy -q
../.venv/bin/python -B -m tests.benchmark_search /path/to/mathlib-checkout
# Requires root AGENTS.md permission for Lean/bubblewrap:
../.venv/bin/python -B -m tests.check_search_lean /absolute/path/to/toolchain/bin/lean
```

`tests.benchmark_search_pretrained` compares binary SentencePiece tokenizers with
the lexical baseline, either replacing or appending tokens. Run the module with
`--help` for its source corpus, reference index, query/judgment TSV and model-file
arguments. Its optional experimental dependencies are `sentencepiece==0.2.1` and
`protobuf==6.33.0`; production search does not depend on them. It writes SQLite/TSV,
checks corpus identity and repeated ranking stability, and reports index size,
token counts, retrieval latency and recovery of previously judged direct results.
Unjudged hits remain unjudged. Different pretrained models do not isolate the
effect of vocabulary size. The dependency-free benchmark guards run with
`python -B -m unittest tests.test_search_pretrained`.

`tests.benchmark_search_long_tokens` tests adding two/three adjacent identifier
components and custom corpus-trained BPE pieces. The BPE variants use 8,192 and
32,768 vocabulary entries, either adding all pieces or only pieces at least four
characters long. All variants retain baseline lexical tokens. Training uses only
source declarations, documentation and statement headers; queries and judgments
are evaluation inputs. It reuses the extracted `corpus.sqlite` and frozen query
and target TSVs from the earlier benchmark, and writes to a fresh output directory:

```bash
PYTHONPATH=new /path/to/sentencepiece-venv/bin/python -B -m tests.benchmark_search_long_tokens \
  --corpus /path/to/corpus.sqlite --queries /path/to/queries.tsv \
  --targets /path/to/targets.tsv --output /path/to/new-output-directory
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_long_tokens -q
```

Pass `--vocab-sizes` with no values for the dependency-free compound experiment.
The output includes model files, input hashes, build costs, repeated query timings,
rankings and paired target recovery, including an `eq`/`dvd`/`pi` query subset.
These are source-only development experiments, with global scope, fixed BM25
weights and candidate limits, and no Lean execution. Known-target recovery does
not measure complete relevance or proof success. Production tokenization is unchanged.

On 2026-11-03, six variants were replayed over 249,981 declarations and 200 frozen
queries (138 with known indexed direct targets). Compound tokens increased
success@1 from 74 to 82 and success@50 from 120 to 123, with success@12 unchanged
at 110; median warm retrieval rose from 23.12 to 23.85 ms and index size from
55.40 to 68.01 MiB. Adding only BPE pieces of at least four characters increased
success@5 on the 17 target-bearing `eq`/`dvd`/`pi` queries from 11 to 14, but had
mixed overall recovery. The 32,768-entry version reached 111/138 at top 12 and
119/138 at top 50, at 24.99 ms. These results support further experimentation,
not an unconditional replacement: compound tokens also moved the direct target
for `δ toOrderHom succAbove` from rank 3 to 47. All 200 baseline rankings matched
the earlier experiment and repeated rankings were stable; 29 offline tests passed.

`tests.benchmark_search_token_weights` extends that experiment using the same
models and frozen inputs. It sweeps minimum added BPE lengths 1–6 and compound
weights 1×, 2×, 4× and 8×. Lexical and compound tokens occupy separate FTS fields
with weights `(3,2,1)` and `(3w,2w,w)`; all weights share one index and unchanged
document lengths. These weights affect term frequency before BM25 saturation,
so they are not linear multipliers of the final score. Weighting applies before
the candidate cutoff. The 1× configuration must reproduce the previous compound
rankings, and reused baseline/BPE configurations must reproduce their old rankings.

```bash
PYTHONPATH=new /path/to/sentencepiece-venv/bin/python -B -m tests.benchmark_search_token_weights \
  --previous /path/to/long-token-output --output /path/to/new-sweep-output
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_token_weights -q
```

`--minimums`, `--weights` and `--trials` override the sweep. Output includes
per-query ranks, paired gains/losses, latency, build costs and index/input hashes.
The length threshold counts underscores as characters, consistently with the
earlier experiment; all original lexical tokens remain searchable.

The 2026-11-03 threshold sweep completed 10,200 retrieval executions. For the
32,768-entry model, minimum length 2 gave top-1/5/12/50 recovery of
78/103/111/121 versus 76/104/111/119 at minimum 4, with warm medians of
24.33 versus 23.63 ms. Thus four characters is not an established optimum.
Uniform compound weight 8× increased top-5 recovery from 101 to 104 versus 1×,
with top-12 unchanged at 110 and top-50 decreasing from 123 to 122.
Their warm medians were 23.06 and 23.00 ms. The dropped known target does not
establish that its newly promoted replacement is irrelevant; labels are incomplete.

`tests.benchmark_search_compound_lengths` further separates two-component and
three-component compounds into independent field groups. Name/documentation/
statement weights are `(3,2,1)` for lexical tokens, `(3p,2p,p)` for pairs, and
`(3t,2t,t)` for triples. Splitting the fields preserves token counts and document
lengths. Triples receive their own weight while their constituent pairs also
remain indexed. The default sweep compares pair/triple weights `1:2`, `1:4`,
`2:4`, `2:8` with equal-weight controls `1:1`, `2:2`, `4:4`, `8:8`.

```bash
PYTHONPATH=new .venv/bin/python -B -m tests.benchmark_search_compound_lengths \
  --previous /path/to/token-weight-output --output /path/to/new-length-output
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_compound_lengths -q
```

This sweep requires no SentencePiece dependency. `--weights` accepts distinct
positive `pair:triple` values; `--trials` sets alternating replay passes. Equal
weights are checked against the previous sweep, and repeated rankings must match.

On 2026-11-03, the independent pair/triple sweep completed 5,400 executions;
all controls and repeats matched, and 34 offline tests passed. Only 33 queries
contained triples, including 29 with known targets. Pair/triple weights `2:8`
improved top-5 recovery from 102 to 103 versus `2:2`, with top-12/top-50 unchanged
at 110/122 and warm medians of 23.38/23.33 ms. The other tested triple-only
increases (`1:2`, `1:4`, `2:4`) changed individual ranks but no measured cutoff
counts. The shared nine-field index was 72.37 MiB, built in 18.83 seconds.
These results support independent configuration but do not establish optimal
weights; queries without triples were unchanged when only triple weight changed.

`tests.benchmark_search_identifier_budgets` tests the overlapping-token bias
directly. `compound-budget` divides each query unit's added-compound scores by
its total number of generated compounds; lexical weights stay at one.
`identifier-budget` instead gives its lexical and compound tokens one shared
budget, dividing all their scores by the total generated-token count. Both
denominators include unmatched tokens. Units retain underscores and camelCase;
dots, whitespace and punctuation are boundaries. Equivalent repeated units are
deduplicated, and tokens shared by different units accumulate their contributions.

```bash
PYTHONPATH=new .venv/bin/python -B -m tests.benchmark_search_identifier_budgets \
  --previous /path/to/compound-length-output --output /path/to/new-budget-output
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_identifier_budgets -q
```

The experiment reuses the nine-field index with unit pair/triple weights.
Normalization multiplies BM25 score contributions after term-frequency
saturation. Terms with equal coefficients share one FTS query; SQL sums those
weighted contributions across all matches before selecting the top 100. There
is no per-group candidate cutoff. A common coefficient uses native FTS ranking.
Query coefficients are recorded as exact fractions in `query-weights.tsv`.

On 2026-11-03, 2,400 executions were stable and reproduced both previous controls;
37 offline tests passed. Across 138 queries with known targets, unnormalized
compounds gave top-1/5/12/50 recovery of 82/101/110/123. Compound-only budgets
gave 83/103/111/123; full identifier budgets gave 79/105/114/124. The latter
added four top-12 successes with no losses versus unnormalized compounds, but
lost three net rank-1 successes. For `δ toOrderHom succAbove`, the known direct
target moved from rank 47 to 4 with compound-only normalization and to 2 with
full normalization. Warm medians were 24.07, 23.58 and 24.32 ms respectively;
p95 was 112.17, 110.05 and 122.27 ms. These are development results over incomplete
target sets; production search remains unchanged. Budgets remove amplification
from token count while preserving BM25's IDF and term-frequency differences.

`tests.benchmark_search_compound_fallback` tests compound-first scoring for each
candidate and query unit independently. If any generated compound matches, the
unit contributes its mean compound BM25 score and no lexical score. Otherwise
it contributes `discount * mean lexical score`. Units that generate no compounds
retain their ordinary mean lexical score. Means divide by all generated tokens
in their channel, including unmatched tokens, preserving the normalization.
Pairs and triples have equal weights, and a partial pair match selects the
compound branch. A compound match in one result does not disable component
fallback in other results or for other query units.

```bash
PYTHONPATH=new .venv/bin/python -B -m tests.benchmark_search_compound_fallback \
  --previous /path/to/identifier-budget-output --output /path/to/new-fallback-output
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_compound_fallback -q
```

The default discounts are `0`, `0.1`, `0.25`, `0.5`, and `1`; override them with
`--discounts`. Discount 1 isolates branch selection without discounting fallback;
discount 0 removes component-only contributions for compound-capable units.
SQL groups all matching postings by candidate and query unit before selecting
the top 100; there is no earlier candidate cutoff. The existing index is reused.

On 2026-11-03, 4,200 executions reproduced both controls and stable repeats;
40 offline tests passed. Against full identifier normalization's top-1/5/12/50
recovery of 79/105/114/124, fallback weight 0.5 gave 84/108/115/123. It added one
top-12 success (`indepFun coordinates pi measure`, rank 16 to 6) with no losses
at that cutoff, while the known `orderIsoFin` target dropped from the top 50.
Weights 0, 0.1, 0.25, 0.5 and 1 gave top-12 counts 114, 114, 114, 115 and 115,
and top-50 counts 122, 122, 123, 123 and 122. All fallback variants reached 84
at rank 1 and 108 within top 5. The 0.5 prototype's warm median/p95 were
31.76/165.01 ms versus 24.88/122.45 ms for normalization; zero fallback was
13.39/70.62 ms because component-only postings can be omitted. This is tuning
on the same incomplete development labels, not held-out proof validation.
Production search remains unchanged.

`tests.benchmark_search_uncovered` compares that 50% fallback with non-overlapping
compound spans. Matching triples are selected before pairs, with left-to-right
ties. Each selected span of length `k` receives `k/N` of its identifier's budget;
covered components receive no additional credit. `spans-fallback` gives uncovered
components `0.5/N` lexical credit; `spans-only` gives them zero. Both retain 50%
mean lexical fallback when no compound matches. This is greedy span selection,
not maximum-weight interval selection. Scoring covers all postings before top100
and reuses the existing index.

```bash
PYTHONPATH=new .venv/bin/python -B -m tests.benchmark_search_uncovered \
  --previous /path/to/compound-fallback-output --output /path/to/new-span-output
PYTHONPATH=new .venv/bin/python -B -m tests.benchmark_search_uncovered \
  --previous /path/to/compound-fallback-output --output /path/to/new-fresh-output \
  --queries /path/to/frozen-queries.tsv --targets /path/to/frozen-targets.tsv
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_uncovered -q
```

On 2026-11-03, 2,088 executions covered the archived 200 queries and 32 fresh
authored queries, with three variants and three trials. Controls and repeated
rankings passed; 43 offline search tests passed. On the 138 archived queries with
targets, baseline / spans-only / spans-fallback recovered **84/108/115/123**,
**84/106/115/123**, and **85/108/115/124** at top1/5/12/50. Partial credit restores
`orderIsoFin` at rank41, while top12 recovery is unchanged.

Fresh queries and intent were frozen before target lookup and scorer runs. Their
37 indexed targets span 27 queries. Baseline / spans-only / spans-fallback give
**13/18/20/23**, **13/18/22/23**, and **13/19/22/24** at top1/5/12/50. Both new
variants improve `lcm_dvd_iff` from rank19 to12 and quotient membership from19
to7/to3, with no top12 losses. Partial credit avoids the ablation's coprimality
top50 loss, but that query still regresses from23 to48 versus baseline.

Source review of 408 pooled query/declaration cards, with scorer names and ranks
hidden and judgments frozen before comparative analysis, found a direct top12
result for **22/32**, **24/32**, and **24/32** queries. Direct slots increased
from31 to33 out of384; direct-or-support slots rose from149 to157/to158. No direct
top12 result was lost; one query lost a supporting result. One truncated source
header remained unknown. Five queries lacked frozen indexed targets and were
excluded from known-target counts, but retained in top12 review. This small
authored set and same-assistant source review are not independent human labels,
live-traffic evaluation, or Lean validation.

Warm median/p95 over all32 fresh queries were **40.56/97.93**, **65.34/161.24**,
and **81.03/196.26 ms**. On the archived target-bearing subset they were
31.00/160.09, 41.42/249.79, and 45.29/306.41 ms. The partial-credit scorer is the
preferred quality candidate, but its Python SQLite aggregate needs profiling
before production integration; these measurements do not establish v2 performance
parity. Production search remains unchanged. Full local results and the frozen
review are under `.local/search-uncovered-{development,holdout,fresh-inputs}-20261103`;
the holdout directory contains `report.txt` and reproducible `analyze.py`.

`tests.benchmark_search_uncovered_fast` implements the same span rule with sparse
compound replacements. It selects spans only for documents with compound matches
and asks FTS for uncovered-component scores restricted to those document IDs.
BM25 statistics still come from the full index. Ordinary lexical fallback runs in
native SQL; a connection-local temporary table replaces fallback contributions
for matching compound units. That table is dropped after scoring. All matching
postings are considered before the candidate cutoff, and the index is unchanged.

`tests.profile_search_uncovered` interleaves the previous compound-first scorer,
the frozen span reference, and the optimized scorer. It uses production SQLite
temporary-file settings and deadline polling, checks the frozen top50 results,
compares complete top100 ordering and scores, and profiles after timing. The first
trial warms caches; three measured trials follow by default. Score comparisons
allow floating-point roundoff from native BM25 summation.

```bash
PYTHONPATH=new .venv/bin/python -B -m tests.profile_search_uncovered \
  --references /path/to/development-span-output /path/to/fresh-span-output \
  --output /path/to/new-profile-output
PYTHONPATH=new .venv/bin/python -B -m unittest tests.test_search_uncovered_fast -q
```

Profiling on 2026-11-03 found 849,299 span-aggregate objects for the 32 fresh
queries, although only 7,520 groups had compound matches. Sparse replacement
removes those aggregate callbacks. A second bottleneck came from FTS seeking
separately for each ID in an uncovered-component filter. Using `+rowid IN (...)`
keeps a term posting scan followed by filtering; it preserves full-index BM25
statistics and avoids repeated seeks for common terms such as `of` and `iff`.

The final interleaved replay made 2,784 retrievals. Top 100 ordering was identical
on all 232 queries (23,100 candidate positions), all top 50 results matched the
frozen reference, and repeated rankings were stable. Maximum score roundoff was
`3.56e-15`. The 45 offline search tests passed, including all-score comparisons
for overlapping/repeated spans, camelCase, empty/stopword-only queries, and
component filtering across multiple batches.

Warm median/p95 in ms, measured in the same run:

| Query set | Previous 50% compound-first | Frozen span reference | Optimized spans |
| --- | --- | --- | --- |
| All 200 archived queries | 33.23 / 172.13 | 42.48 / 303.60 | 29.33 / 146.05 |
| All 32 fresh queries | 44.31 / 125.92 | 81.34 / 257.27 | 34.57 / 120.00 |

On the archived 138-query target-bearing subset, optimized median/p95 were
26.68/155.49 ms. Fresh-process peak RSS over the 32 fresh queries was 35.58 MiB
for optimized spans versus 38.54 MiB for the reference. Full-index scans still
dominate broad queries: `orderIsoOfFin` improved from 326.39 to 148.86 ms, and
`IsCompact.exists_isMaxOn` from 482.86 to 222.55 ms. These are offline retrieval
measurements, excluding index construction and full tool-call execution; they do
not establish end-to-end parity with v2. Production remains unchanged. Results,
profiles, query plans, source hashes and `report.txt` are under
`.local/search-uncovered-optimized-filter-20261103`.

Validation of body-only probes and readable evidence on 2026-10-17: **144 offline
tests passed**, plus the real Lean application integration check. Coverage includes
wrapper rejection through comments, generated UTF-8 byte limits, source excerpts,
diagnostic locations, large Unicode pages, two-case continuation and inherited
checked aliases. Re-rendering the saved Morley search/probe evidence reduced its
displayed size from **8,489 to 3,519 bytes (58.5%)** while retaining names, source
content, diagnostics and goals. Its successful probe reconstructs exactly from the
body; its original `run_elab` wrapper is rejected before compiler dispatch.
Three-case body expansion measured **0.877×** v2's time in alternating local samples.

Validation on 2026-10-15: **24 distinct offline tools tests and 4 affected provider
wire tests passed**, with focused reruns after changes. They cover request/receipt
binding, allowance reuse, search integrity and limits, exact rank ordering, failure
continuations, checker handoffs, research claims, and 1.6 MB Unicode evidence paging
without JSON calls. No Lean, live hosted search or model calls were run.

Seven alternating local benchmark samples measured the following median ratios
against v2: **0.149×** for 1,200-hint fusion with cached names, **0.563×** with cold
name validation, **0.714×** for matching source, **0.514×** for nonmatching source,
and **0.023×** for the first page of 110 KB evidence. The paging comparison gives
v2 a free archive read; the new path includes an artifact read and verification.
These measure local helper work, not network/compiler or end-to-end run latency.

```sh
python3 -B -m unittest tests.test_tools -v
python3 -B -m tests.benchmark_tools
```

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

`prover`, `reviewer`, `research` and `web_search` each have a `ModelRoute` section with
`provider`, `model`, `transport`, `account`, `endpoint`, `credential_env`,
`prompt_cache`, `reasoning_effort` and `prices`. The checked-in example configures the prover and
reviewer with Azure Responses and both helper roles with OpenAI Codex. `Config()`
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
stricter transport requirements. The `model`, `research_model` and `web_search_model` sections still
hold invocation limits. Each role owns its prices (`config.reviewer.prices`, for
example); there are no separate top-level price sections. `prompt_cache` may be
`explicit`, `disabled` or null. Route and price changes affect the config identity
and cannot be applied through
`increase`, which only permits the existing prospective limit changes.

Set `reasoning_effort` on any configured role (`prover`, `reviewer`, `research`,
or `web_search`) to select its effort independently (for example, `"high"`).
Responses routes send `reasoning.effort`; Codex routes send the explicit `effort`
override in [the app-server turn request](https://learn.chatgpt.com/docs/app-server#start-a-turn).
An omitted or null value uses the provider's default, including the Codex
configuration default for Codex routes. The model must support the selected level.
Explicit effort is saved in the run configuration and wire request, preserved on
retry/resume, and participates in the prompt identity and Responses prompt cache key.

Omitted settings use defaults. Omitted backlog stop thresholds scale with model
concurrency; explicit thresholds remain exact. Prices are integer **micro-USD per
million tokens**: the example's $2/$0.10/$10/$2.50 rates become
`2000000`/`100000`/`10000000`/`2500000`. A null or omitted `prices` means price
is unknown, while an explicit zero rate means free. Cache-write pricing is
optional; explicit write pricing requires write telemetry when it affects cost.

Application provider construction applies each request role's frozen prices
before its worker result and provider receipt are saved. Roles sharing a route
still use their own rates. An already reported cost, including zero, takes
precedence; scripted responses remain free. Failed or cancelled calls with known
token usage are priced too, and each retry retains its own charge. Resume reads
the saved costs without recalculating them. The cache diagnostic uses the same
provider construction. Status shows the known USD subtotal and flags unknown
costs; JSON exposes the existing usage, known-usage and unknown-report fields.
Pricing touches only the small usage metadata, without serializing model output.

`checker.max_heartbeat` and `export.max_heartbeat` set Lean's `maxHeartbeats`
option in user-facing heartbeat units. Both default to `2000000`; `0` disables
the heartbeat limit while wall-time and memory limits still apply. The shared
`local` process profile also carries this field, though search jobs do not use
Lean. `limits()` freezes the value into `ResourceLimits`; policy admission rejects
compiler jobs above a finite configured limit, including unlimited requests.

`checker.max_rec_depth` and `export.max_rec_depth` set Lean's `maxRecDepth`.
Both default to `1000` and require a positive integer. The shared `local` profile
also carries the field. The limit is saved in each request and survives recovery;
compiler admission rejects requests exceeding their configured profile.
Recursion exhaustion has failure type `recursion_depth` and follows the same
local recovery policy as timeout/OOM. Strategy uses `mathematics.elaboration`
for its revision allowance, without spending a mathematical attempt. Lean's
diagnostic output is retained, including the configured limit; no special model
prompt instructions are added.

`Policy` and `Config` are immutable. `config.increase(...)` produces a new snapshot
for prospective run, mathematical, process/export, scheduling capacity and research/web-search
invocation timeout increases. Helper invocation timeouts can increase up to 900000 ms;
routes, retry controls and helper allowances remain frozen.
It does not reset usage, move the
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
helper leave those fields unset. Probe units count requests, not individual cases. Research and web-search requests that
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
inside `proof_state` and `joint_targets` belong to storage and its controller
integration. File reading formerly in `submission` likewise belongs to callers.

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

Binary graph canonical validation and compiler request validation reuse successful
admissions of identical immutable bytes in bounded process-local caches. Module
inventory validation uses the same approach. Decoding still returns a private
mutable graph and enforces the caller's size limit; artifact reads and reservation,
environment and dependency bindings are checked on every use. Caches retain only
bytes and admission flags, never decoded graphs or environment objects.

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

Saved selections now use **`midas-selection-v4` binary**, so selection bytes and
selection IDs change. The format begins with `MIDASSEL` and byte `0x04`, followed by
flat fields prefixed with unsigned little-endian 64-bit byte lengths. Source and
signature data and readable type text remain raw bytes, without JSON or hex expansion. Structural constant
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
another thread are rejected. Storage supplies the actual read/staging capabilities
and owns publication and staging cleanup. Completed workspace files survive
executor shutdown for controller-coordinated publication, recovery and retirement.

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

Worker profiling follow-up on 2026-10-18 optimized the repeated `/proc` discovery
scan: directory enumeration uses `os.scandir`, and process identities use one
bounded `os.read` without allocating a Python file object, parsing only through
the start-time field. Each descriptor closes immediately, including on failed
reads; descriptors are not cached across scans. The 20 ms
reuse limit, fresh scans, pidfd identity checks and memory sampling are unchanged.
Seven alternating benchmark samples measured **2.38 ms CPU per fresh scan versus
5.23 ms for v2**, **0.999×** v2 wall time for 1 MiB capture, and **0.887×** for
a fresh Python child. Against the earlier unbuffered-file implementation, the
raw read reduced fresh-scan CPU from **2.90 to 2.35 ms** (18.8%). All **45
executor/domain execution tests** passed, including malformed process records,
non-UTF-8 process names and descriptor cleanup on read failure.

## Environment

Eight implementation modules plus `__init__.py` consolidate the environment-owned
parts of the twelve modules assigned by the cleanup blueprint:

| Module | Responsibility |
| --- | --- |
| `inventory.py` | Bounded no-follow scans, stable streaming hashes/copies, flat binary records, file and directory validation |
| `manifest.py` | Pinned Lean versions, immutable profile settings, canonical manifests, module conflicts, hash-only source bindings, bounded manifest cache |
| `package.py` | Trusted installation probes, frozen runtime closure, preparation/preflight, atomic publication, shared toolchain admission/relocation, generated inputs and launch recipes |
| `dependencies.py` | Pinned Git/Lake closure, complete source provenance, compiled modules/sidecars, metadata revalidation and isolated source replay |
| `options.py` | Fresh isolated Lake configuration evaluation, typed bounded compiler options and small module setup documents |
| `assets.py` | Committed literal `include_str` inputs and their source-relative paths |
| `archive.py` | Bounded verification configuration, tool pins, committed benchmark archives, target metadata, relocation checks |
| `attachment.py` | Namespace-scoped library attachment, retained original pins, import preflight and new environment identity |

These replace environment ownership in `environment`, `toolchains`, `packaging`,
`shared_package`, `shared_toolchain`, `problem`, `attachment`,
`dependency_admission`, `dependency_modules`, `dependency_compiler`,
`verification_config`, and `leaneval_admission`. They do not import v2.
Run bindings, artifact publication and run-cache retirement from `problem` and
`shared_package` belong to storage/application integration. Checker
resource compilation and proof receipt admission belong to checker. Lake's
freshness/hash gates and independent source audits belong to environment and
checker/export. Benchmark workspace layout, comparator execution and final LeanEval
acceptance belong to the separate CLI wrapper; provenance or successful source
replay alone does not report a verified export.

`Environment.prepare(destination, config, worker_context)` consumes a
`PackageConfig` with one pinned release, one installed toolchain, explicit
project/dependency roots, selected project files and ordered module search paths.
It never downloads or builds missing dependencies. The caller supplies an existing
executor reservation; installation probes, preflight, native builds and replay
all use that context's cumulative process limits and cancellation. There is no
second process runner or scheduler inside environment.

Preparation references the installed toolchain and dependency directories, and
copies only the project and compiler runtime. Missing configured files or search
directories are rejected before large inventories are scanned. It checks
source stability, records exact file hashes/modes/directories, freezes the runtime
closure for Lean and native build tools, and checks the reported compiler version
and selected sources under bubblewrap. Only successful preflight publishes the
package, using an atomic rename that cannot replace an existing destination.
Referenced dependency bytes, copied bytes, runtime files and package metadata
consume the input allowance; the toolchain is inventoried separately. Internal
file aliases in referenced inputs
are allowed; external links, directory links and special files are rejected.

The package contains `environment.bin`, `preflight.bin`, a small `location.json`,
and local `project/` and `runtime/` snapshots. The application package command also
adds the derived BM25 `search.sqlite` index before publication. The locator records the toolchain
path and a flat dependency-name-to-directory map, capped at 16 KiB. No dependency
tree, hardlink farm or shared copy is created. Its manifest is a flat binary record stream beginning
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
`references={name: directory, ...}` supplies a replacement dependency location map
to `Environment.restore`; each directory must match the saved content inventory.

Local snapshots and referenced dependencies receive exact content checks, including
added files, modes and directories. Dependencies are mounted read-only for Lean;
the original directories must remain stable during a run. Shared toolchains retain v2's
default metadata admission and optional content policy
(`MIDAS_VERIFY_TOOLCHAIN=1`, or `validate(hash_toolchain=True)`). Successful
toolchain admissions are bounded, process-local and keyed by location, exact
toolchain inventory and policy. `validate(fresh=True)` repeats admission;
dependency source replay requires content admission before and after execution.
File digest reuse requires identical device/inode/mode/size/mtime/ctime stamps.
The process-local LRU retains at most **262,144** entries, containing only these
stamps and digest strings. The previous 32,768-entry limit caused repeated full
scans of the Morley environment to evict almost every digest before reuse.
With approximately 171,000 entries retained, a repeated full validation took
**5.42 CPU seconds versus 13.31** with the previous limit, using identical content
and metadata checks. The fixture's retained cache has a conservative size estimate
of **74 MiB**; the cache contains no file contents. All **43 environment tests**
passed, including eviction, reset, same-size changes and replacement with a
preserved modification time. Root validation retains its second metadata pass:
per-file checks alone cannot detect an earlier file changing while a later file
is read. That mutation window has a regression test.
Caches and service admission reset across forks.

An environment service owns stable roots for its lifetime, matching v2's
`EnvironmentVerifier` contract. Repeated jobs reuse that admitted service;
explicit `validate()` always rechecks package contents. `selection_inputs()`
returns only the environment hash and authorized source hashes/sizes to domain.
Manifests, installation paths and package inventories stay with environment.

Independent export verification validates the environment before rebuilding and
after the final audit. The intermediate build does not publish admitted proof
evidence, so it no longer repeats full environment validation between those
steps. The checker owns this complete boundary for both source and compiled
libraries; the rebuild helper checks generated files within it. Focused checker
and export tests reject frozen-input changes before rebuilding, during rebuilding
and during the final audit, while confirming only two complete validations.

The authorized Morley replay on 2026-10-18 consumed all 27 saved model responses
and matched all 85 original operation outcomes, finishing with verified export.
Against the preceding profiled replay, wall time fell from **316.55 to 280.44
seconds**, worker CPU from **83.25 to 48.36 seconds**, and export-verification wall
time from **86.01 to 57.02 seconds**. Export validation used **14.41 CPU seconds**
instead of 42.94, and digest misses fell from 514,076 to 17,925. Process-discovery
CPU fell from **33.90 to 28.04 seconds**. These single-run comparisons include
selected-function timing overhead and exclude model API latency. Both use the
same direct-API driver; its duplicate startup restoration is already avoided by
the ordinary CLI's shared admitted environment handle.

`materialize()` accepts exact `AuthorizedInput` records, copies verified bytes into
a new private directory, and rejects path conflicts or frozen-module shadowing,
including compiled sidecars. `launch()` returns an executor `LaunchSpec` with
explicit read-only roots and an optional separate writable build directory.
Generated inputs are checked before each recipe is issued. The installed
bubblewrap hash is also checked at execution; missing isolation never enables a
host fallback. Trusted bootstrap inspection uses an explicit Linux system runtime
under bubblewrap; compiler jobs use the frozen runtime.

Dependency closure admission compares checkouts against the workspace root lock,
committed Lake metadata, mapping membership and source/module paths. Like Lake,
it resolves transitive dependencies using the root lock; dependency development
locks may pin older versions, but cannot introduce names absent from the root
lock. Development toolchain metadata is provenance, so it may differ from the
workspace compiler.
Module admission checks complete committed Lean source bytes, required modules,
unambiguous source mappings, sidecars and module conflicts. It rechecks metadata
and HEAD after scanning; its source-only profile also rejects native libraries.
Replay resolves the required import closure with empty placeholders, removes all
placeholders, compiles in dependency order without prepared objects mounted, and
compares exact generated sidecars. Results bind the environment hash, module
inventory hash, import graph, build order and explicit options.
`configure_modules(environment, closure, modules, context)` follows module inventory
admission. The environment's `lean/Options.lean` helper evaluates each package's
pinned Lake configuration once using the installed toolchain's Lake runtime,
without fetching or building dependencies. Configuration caches stay in the private
workspace; module source mappings must match the admitted sources. Library options
include Lake's package/library precedence and build type. Extra compiler arguments
and native plugins remain unsupported by this capture path.
Typed options cross the helper boundary in a flat binary stream. Each module's
canonical option pairs contribute to its inventory identity; equivalent option
sets share one immutable tuple. There are at most 64 options per module, and a
conservative escaped-size bound is checked before producing small setup JSON.
Lake dependency replay supplies that setup with admitted package/module names and
compiler options. Source library rows retain the package name and options for
independent export rebuilds; package directories, inventories and toolchains remain
in environment. Neither path reads cached Lake setup JSON or imports its artifact
paths or plugins. Successfully prepared
LeanEval workspaces retain the replayed source library for later starts/resumes;
fresh workspace preparation repeats replay.
Literal `include_str` paths (including `/` chains) are admitted against committed
Git objects, checked for changes and staged with the required source modules.
Only these referenced assets enter the source library and standalone export;
dependency directories and compiled caches are not copied. Independent rebuilding
checks asset hashes again and rejects conflicting paths.

Validation on 2026-10-16 for package identity, assets and compiler options:
**77 focused Python tests**, the real environment checks, and the complete export
and standalone recheck suite passed. On Mathlib commit
`5e0c4e5239cb0a2d86d68a884bf52cfd963fce22` with Lean 4.35.0-rc3,
the `Mathlib.Order.Hom.Bounded` regression matched the admitted artifacts for all
**381 required modules**, first in source replay and then in independent export
rebuilding. Capturing options for 399 cached modules took **9.90 seconds**;
package preparation, admission, replay and independent rebuilding together took
**505.53 seconds**. This is a targeted dependency regression, not a complete Morley
run or a comparison against v2.

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
# Regression against a checkout with the Mathlib.Order.Hom.Bounded cache closure:
python3 -B -m tests.check_mathlib_options_lean /absolute/path/to/toolchain/bin/lean /absolute/path/to/benchmark
```

## Checker

Eight implementation modules plus `__init__.py` separate compiler preparation,
execution and evidence admission:

| Module | Responsibility |
| --- | --- |
| `requests.py` | Immutable compiler requests, exact source/candidate/dependency bindings, closed signatures, lookup prefixes and probe cases |
| `evidence.py` | Strict binary certificates, native frames, binding/replay receipts and advisory result validation |
| `runtime.py` | Exact installed resource hashes, isolated compilation, atomic private runtime cache and explicit fresh/retained recipes |
| `inspection.py` | Binary Lean declaration inventories and frozen selection construction |
| `proofs.py` | Rendering admitted earlier definition proofs into their original source scopes |
| `native.py` | Raw request attachments and a flat, hash-bound compiled-declaration cache |
| `rebuild.py` | Independent dependency rebuilding and generated-file validation |
| `service.py` | One executor callback and staging path, cumulative process limits, probe progress and trusted receipt admission |

These replace the checker-owned portions of `check_protocol`, `checker_runtime`,
`checking`, `extraction`, `interface_lookup`, `probe_runtime`, `proof_execution`,
`proof_jobs`, `source_cache`, and `source_proof_admission`. No implementation imports
v2. SQL, task publication, graph updates and scheduling stay with their respective
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

Each checker packet receives `max_heartbeat` from the reserved job limits.
Fresh, interpreted and retained workers use that value for elaboration and
probes; retained workers read it separately for each request. Source inspection,
runtime compilation, environment preflights and dependency replay receive the
reserved limit as well. Heartbeat limits are execution settings and do not
change the environment identity.

`max_rec_depth` follows the same propagation path. The checker recognizes Lean's
tagged recursion-depth exception, including errors recovered into the message
log before a later axiom audit fails. Ordinary error text mentioning recursion
does not change its failure classification. Failed compiler stderr is exposed
through `WorkerResult.diagnostics` as well as the phase's artifact reference.

Recursion-depth validation: **410 offline tests passed**. Lean 4.35.0-rc3 checks
passed in retained, fresh and interpreted workers: the same nested proof exhausts
`max_rec_depth=256` and succeeds at `2048`; ordinary diagnostics mentioning
recursion retain their original classification. The controller benchmark remained
below v2 at **0.978×** lifecycle time and **0.448–0.514×** idle time.

Heartbeat validation: **117 focused Python tests passed**. Lean 4.35.0-rc3
integration checks passed for fresh, retained and interpreted execution,
including `max_heartbeat=12345`, unlimited (`0`) execution and a looping probe
interrupted at the configured limit.

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
Elaboration errors stop checking before the axiom audit, so recovered synthetic
sorry terms do not produce a second, misleading `sorryAx` failure. Explicit
sorry and other unapproved axioms still fail the audit. Expected checker
rejections retain their own messages instead of the frontend's generic
`unexpected internal error` prefix; recursion-depth failures keep their typed
classification.
Only `propext`, `Classical.choice` and `Quot.sound` are permitted global axioms.
Conditional assumptions remain explicit and cannot become a direct proof.
Closed definition dependencies require matching admitted proofs from the same
joint submission, and are rendered in a flat, explicit dependency order.

Compiled declarations travel as raw attachments in the request file. Only the
fresh or forked checker child materializes them, outside the import search path;
the import parent reads the packet header and retains only imported environments.
The producer and immediate replay therefore share the same import parent without
loosening the executor's immutable-dependency compatibility checks.

Successful proof and assembly jobs populate a flat `declarations` directory in
the checker cache. Keys bind the environment/runtime hashes, source prefix,
logical path, offset, assumptions, imports, declaration source/signature and
dependency keys. Renaming a normalized root for assembly preserves reuse. Cache
files contain a raw checksum and compiled bytes, published atomically; missing,
corrupt or oversized entries are rebuilt from durable source. Every cache hit
still passes declaration validation, axiom auditing and independent kernel replay.
Independent integration/export source audits continue to elaborate their source.

Validation on 2026-10-18: 39 focused Python checker/cache/export tests passed,
along with the existing real Lean checker checks and six new native-reuse
regressions. The latter verify import-parent identity across producer/replay,
cached assembly without tactic execution, corruption recovery, reuse after
rebuilding, malformed-native rejection and a healthy request after rejection.
A Morley saved-action replay started with an empty declaration cache and prepared
checker binaries. All 85 operation outcomes matched the source run, all 27 saved
model outputs were consumed, and it finished `export_verified` with exit 0.
Median immediate kernel replay fell from 4.103 to 0.317 seconds; assembly's Lean
phase fell from 47.702 to 22.058 seconds, reusing two of six declarations. Total
replay wall time fell from 280.441 to 234.351 seconds (16.4%). These are single-run
comparisons with the prior v2.1 replay, not statistical or v2 throughput claims.
Timings and cache/import-parent counters are retained in
`.local/morley-native-replay-20261018/` at the repository root.

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
are inventoried and hash-checked before use. Source inputs remain in private
executor workspaces for storage cleanup. Native request files live in child
scratch space, which the retained runtime clears after every request.

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
python3 -B -m unittest tests.test_checker_native -v
python3 -B -m tests.benchmark_checker
# Requires permission for Lean/bubblewrap:
python3 -B -m tests.check_checker_lean /absolute/path/to/toolchain/bin/lean
python3 -B -m tests.check_checker_lean /absolute/path/to/toolchain/bin/lean --native-only
```


## Provider

Six implementation modules plus `__init__.py` own the provider slice:

| Module | Responsibility |
| --- | --- |
| `protocol.py` | Immutable routes, projected transport bounds, artifact-based requests and normalized outcomes |
| `wire.py` | Bounded protocol parsing, small control messages and validation of already-rendered wire bytes |
| `transport.py` | Linear text accumulation, usage/error normalization, cancellation and deterministic scripted responses |
| `responses.py` | One stateless Azure/Bedrock HTTPS Responses invocation with no redirects or retries |
| `codex.py` | One ephemeral app-server turn, effective policy retry-limit verification, tool restrictions and advisory web events |
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
`Provider([(route, transport), ...])`. Limits contain frozen transport bounds and
Codex's internal retry limits; scheduling, invocation recovery and prices stay in
policy. `BearerCredentials` binds an ephemeral secret to one exact route;
`from_environment` reads only its named variable.
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
the saved model with `stream=true`, `store=false` and text messages, without an API
output-format field. Prompt appends the role-specific schema to the developer
instructions. Provider
validates the transport envelope, sends it without rewriting, and returns the
model's response text without whitespace trimming, fence stripping or extracting
JSON from prose. It does not interpret a mathematical candidate. Unsupported
backend settings remain a reported error; provider never rewrites the request to
retry a rejected configuration. Schema adherence is checked by strategy after
generation. Refusals have a distinct outcome, and incomplete streams retain their
partial text and available usage.

For Codex, `wire` contains the frozen `turn/start` `input` text blocks and optional
`effort` override without `outputSchema`; `setup` contains the complete frozen `baseInstructions`, including
the response schema. Provider adds only thread/model/protocol controls, preserving
the saved prompt bytes.
See the [official app-server turn contract](https://learn.chatgpt.com/docs/app-server#start-a-turn).
Policy's `model`,
`research_model` and `web_search_model` sections define `request_max_retries` and `stream_max_retries`,
both defaulting to zero. These are frozen through `Limits.from_config(...)` and
control retries within one Codex turn; `recovery.transport_retries` separately
limits retries of whole invocations. The internal limits apply only to Codex;
Responses transports continue to make one HTTP attempt. Codex verifies that its
effective request/stream retry settings match the saved limits before inference,
disables local tools, and admits bounded web operations only for the OpenAI web-searcher.
Usage notifications before the
turn acknowledgement and before the final thread-read acknowledgement are kept.
On timeout or cancellation after a turn acknowledgement, the adapter sends
`turn/interrupt` and drains bounded text/usage events for up to `shutdown_ms`
(3 seconds by default), before normal process termination. It accepts the interrupt
acknowledgement and matching `turn/completed` in either order, then drains through
a metadata-only `thread/read` acknowledgement. Cleanup uses the same output/event
limits and never changes the original timeout/cancellation to success. Rejected,
malformed or silent interruption falls back to process termination, retaining
already received output and usage. No interrupt is sent without an acknowledged
turn ID. Recovered usage is included in the sealed receipt and normal role-price
calculation; unavailable usage/cost remains unknown.
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
phase, delivery certainty, nullable usage, cache-write and reasoning-token
telemetry, and retry-after metadata. Unknown telemetry remains unknown. Costs and retry decisions stay in
policy. A pre-send cancellation or connection failure is `NOT_SENT`; a write
failure is `UNKNOWN`; an HTTP response is `DELIVERED`, including errors. Closing a
local HTTP stream does not imply cancellation of remote computation.

The report's `outcome.reasoning_tokens` records the provider-reported count from
Responses `usage.output_tokens_details.reasoning_tokens` or Codex
`tokenUsage.total.reasoningOutputTokens`. A reported zero is preserved; absent or
invalid counts remain null. The count survives receipt admission and resume,
including when available on failed or cancelled calls. It is a breakdown of the
existing output-token total, so pricing and budget accounting do not add it again.
Only this numeric count is saved; reasoning text is not retained. Older reports
do not contain a recoverable reasoning-token count.

`worker_payload(result)` gives controller/storage the exact small result bytes to
save. `admit_receipt` requires the controller's `ReceiptVerifier`, verifies those
bytes, the saved request, artifact hashes/limits, report consistency and all event
frames. It returns `AdmittedOutput` with raw/event references, small outcome
metadata and the executor's final status. A stop arriving after transport return
can change execution status while preserving known usage and complete bytes.
These remain advisory results, never proof authority. `ScriptedTransport` supplies
a deterministic saved outcome by exact wire hash through the same staging and
receipt path.

Validation: **36 offline provider tests and 29 policy tests passed**, covering config projection, route/wire
validation, structured JSON output, exact wire bytes, large payloads with JSON
operations disabled, usage/cache telemetry, refusals, partial/truncated streams,
limits, cancellation, HTTP failures, default-zero and configured Codex retries,
effective retry-setting mismatches, receipt tampering, executor
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
