# Midas formalizer v2.1

Midas selects unfinished Lean declarations, runs automatic or supplied proofs,
saves progress, and exports independently checked Lean sources. The v2.1
implementation is in [`new/midas`](new/midas); its entry point is
`python -m midas`.

## Setup

Use Python 3.11 or newer. From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install zstandard httpx
cd new
python -B -m midas --help
```

`zstandard` is required for storage. `httpx` is needed for Azure and Bedrock
Responses calls; supplied proofs and scripted model replies do not need it.
All remaining commands assume the virtual environment is active and the working
directory is `new/`. Use `python -B -m midas COMMAND --help` for individual options.

Lean operations require Linux, an installed Lean 4 toolchain, `bubblewrap` at
`/usr/bin/bwrap`, `ldd`, and permission to create Linux namespaces. Run control
uses local Unix sockets. Build project imports and dependencies beforehand;
Midas does not download toolchains or run Lake to build missing dependencies.
The real Lean checks in this tree use **Lean 4.35.0-rc3**.

The checker cache defaults to `~/.cache/midas`. It must be private and owned by
the current user; `--cache DIR` selects another location. A host C compiler
(`cc`) enables the retained import-worker optimization on supported systems.

## Prepare a package

A package freezes the project inputs and records the exact toolchain and
dependency identities. For a small example, create `project/Main.lean`:

```lean
import Lean

theorem example_target : True := by
  sorry
```

Save `package.json`, replacing the toolchain path and matching its version:

```json
{
  "version": "4.35.0-rc3",
  "toolchain": "/absolute/path/to/lean-toolchain-installation",
  "roots": {"project": "project"},
  "project_inputs": ["project/Main.lean"],
  "search_paths": ["project", "toolchain/lib/lean"]
}
```

`toolchain` names the installation directory containing `bin/lean` and
`bin/leanc`. Toolchain and root paths resolve relative to the configuration
file. `project_inputs` and `search_paths` use logical paths prefixed by a root
name. For Lake projects, include `project/.lake/build/lib/lean` and each
dependency's compiled-module directory in `search_paths`; add the dependencies
as named entries in `roots`. Include every source you want to select in
`project_inputs`. Optional fields are `preflight_imports` (module names) and
`max_bytes` (default 16 GiB).

Project, toolchain, and dependency roots must not overlap. If built dependencies
live inside a checkout's `.lake/packages`, use a separate project snapshot and
reference the dependencies at their original locations.

```sh
python -B -m midas package package --config package.json
```

Use a new destination directory. Preparation checks the compiler version and
project sources in isolation; unfinished proofs are allowed at this stage.

The project and compiler runtime are copied into the package. The installed
toolchain and dependency directories remain referenced in place, so keep them
available and unchanged for runs, resume, export, and verification. The package
stores the environment manifest once; saved work refers to its hash.

To add an already built library, save `attachment.json`:

```json
{
  "name": "extra",
  "path": "/absolute/path/to/ExtraLib",
  "module_paths": [".lake/build/lib/lean"],
  "source_roots": ["."],
  "prefixes": ["ExtraLib"]
}
```

```sh
python -B -m midas package package-with-extra --base package --config attachment.json
```

The attachment must supply source and compiled modules under the specified
namespace prefixes without shadowing existing modules. It creates a new package
identity; select targets again before starting a run with that package.

## Select targets

Inspect all unfinished declarations and save their selection:

```sh
python -B -m midas select package --source Main.lean --output selection.bin
```

`--source` is relative to the packaged project, so use `Main.lean`, not
`project/Main.lean`. The command prints the selection ID, exact target names,
and target IDs. Omit `--output` to inspect without saving a selection.

Use repeatable `--target` flags to select specific declarations:

```sh
python -B -m midas select package --source Main.lean --target example_target
```

Use the fully qualified names printed by `select`, quoting names containing
spaces or Lean guillemets. Without target flags, `select` chooses all unfinished
targets. A run accepts 1–256 selected roots.

## Run automatic proofs

Copy [`new/config.json`](new/config.json) to a local configuration and edit it:

```sh
cp config.json live.json
```

The supplied file contains policy defaults and example Azure prover/reviewer
routes. Set the model, endpoint, credential-variable name, and prices for your
account. The endpoint is the complete Responses URL. Keep credential values in
the named environment variable, outside the JSON.

A smaller `live.json` can omit settings that should use defaults:

```json
{
  "format": "midas-config-v1",
  "run": {"turns": 400, "runtime_ms": 7200000},
  "prover": {
    "provider": "azure",
    "model": "YOUR_MODEL",
    "transport": "azure_responses",
    "endpoint": "https://YOUR_RESOURCE.openai.azure.com/openai/v1/responses",
    "credential_env": "AZURE_OPENAI_API_KEY"
  },
  "reviewer": {
    "provider": "azure",
    "model": "YOUR_MODEL",
    "transport": "azure_responses",
    "endpoint": "https://YOUR_RESOURCE.openai.azure.com/openai/v1/responses",
    "credential_env": "AZURE_OPENAI_API_KEY"
  }
}
```

Configure a reviewer for independent review of decompositions and node stop requests.
Each operation launches a model call with its own instructions, selected by the
operation before dispatch. Both use the configured reviewer model.
The prover and reviewer can call either helper directly:

- `research`: mathematical guidance, proof strategies and answers to questions, without browsing.
- `web_search`: resources and references, with bounded optional web search.

Each helper is disabled when its route is omitted or unconfigured. The researcher
supports the same transports as the prover; the web-searcher requires OpenAI Codex.
`helpers.research` and `helpers.web_search` set independent request allowances;
`helpers.web_calls` bounds web operations per web-search request.
Set `reasoning_effort` independently in any configured model role, including
Codex roles (for example, `"reasoning_effort": "high"` inside `research`). The value
is saved with each request; omitting it or setting it to null uses the provider's
default, including the Codex configuration default for Codex routes.
Failed helper calls return the original question and failure reason so the caller
can revise its request. Timeout feedback also includes the failed call's saved
time limit; web-search feedback preserves the requested URLs. Any received output
is retained and included in paged feedback as incomplete, unverified text, even
when it ends inside JSON. Displays above 4 MiB are explicitly clipped; the complete
received output remains saved in the archive's referenced raw artifact.
On timeout or cancellation, an acknowledged Codex turn receives `turn/interrupt`.
The adapter collects remaining text and usage for up to the role's `shutdown_ms`
(3 seconds by default) before terminating the process. The call retains its failure
status; recovered token usage is priced normally, and missing usage stays unknown.

A prover can return `stop` with a rationale and a nullable statement objection.
The node stays active while the reviewer decides. Acceptance stops the node;
an accepted statement objection also asks its current parents to reconsider their
decompositions. Rejection returns actionable feedback to the prover so it can
continue. `mathematics.stop_requests` defaults to **3 reviewed requests per node**.
The fourth request stops that node immediately without another review. A forced
stop does not approve its statement objection. Set the limit to zero to stop on
the first request. The count survives resume and node revisions; reviews and
review retries do not increment it. Stop requests and decisions use model turns
without spending a proof or decomposition attempt.

| Provider | Transport | Authentication/setup |
| --- | --- | --- |
| `azure` | `azure_responses` | HTTPS Responses endpoint; bearer token from `credential_env` (default `AZURE_OPENAI_API_KEY`). |
| `amazon-bedrock-runtime` | `bedrock_responses` | Regional `https://bedrock-runtime.REGION.amazonaws.com/openai/v1/responses` endpoint; bearer token from `credential_env` (default `AWS_BEARER_TOKEN_BEDROCK`). |
| `openai` | `codex` | Configured app-server executable supplied with `--codex /absolute/path/to/codex`. |

For an app-server route, set `provider`, `model`, and `transport`; omit `endpoint`
and `prompt_cache`. Web search requires this transport. Pass `--codex` again when
resuming a run that uses it. `PATH`, `HOME`, and `CODEX_HOME` are passed to the
process; repeat `--codex-env NAME` for additional required environment variables.

```sh
python -B -m midas start run --package package --selection selection.bin \
  --config live.json --output bundle
```

Live execution is the default. To select and start in one command instead:

```sh
python -B -m midas start another-run --package package --source Main.lean \
  --all-unfinished --config live.json --output another-bundle
```

Starting from `--source` requires `--target` or `--all-unfinished`. A saved
`--selection` already contains that scope and cannot be combined with those flags.

Runs execute in the foreground. Progress goes to stderr and the final status to
stdout. In a terminal, elapsed-time-only updates overwrite the last progress
frame; status changes add new rows. Redirected progress remains line-based.
`--quiet` suppresses progress; `--json` emits bounded JSON records, one
per line. Successful runs independently verify their export even without
`--output`; supplying `--output` also publishes the bundle to that directory.

Custom role instructions use repeatable `--instructions ROLE=FILE`, with roles
`prover`, `reviewer`, `stop-reviewer`, `researcher`, and `web-searcher`.
`reviewer` overrides decomposition review; `stop-reviewer` overrides stop review.
Each file must be UTF-8 and at most 16 KiB. Instructions and configuration are
saved with the run and preserved on resume and fork.

Live retrieval can query the public LeanExplore service. It sends search queries;
returned names are checked in the run's Lean environment before use.

## Supply your own proof

Supplied execution uses the same compiler and export checks without model calls.
Candidate files use the binary `encode_submission` format. For the
`example_target` above, save this as `make_candidate.py`:

```python
from pathlib import Path
import sys
from uuid import uuid4

from midas.domain import encode_submission

payload = encode_submission({
    "format": "midas-scoped-candidates",
    "submission_id": str(uuid4()),
    "selection_id": sys.argv[1],
    "definitions": [],
    "entries": [{
        "key": "proof",
        "subject": {"kind": "target", "name": "example_target"},
        "proof": "by trivial",
        "dependencies": [],
        "review": None,
        "revision_of": None,
        "definition_scope": {"statement": 0, "proof": 0}
    }]
})
with Path("candidate.bin").open("xb") as output:
    output.write(payload)
```

Replace `sel_...` with the complete selection ID printed by `select`:

```sh
python -B make_candidate.py 'sel_...'
python -B -m midas start supplied-run --package package --selection selection.bin \
  --candidates candidate.bin --output supplied-bundle
```

To create an unresolved run and submit later:

```sh
python -B -m midas start manual-run --package package --selection selection.bin \
  --workflow supplied
python -B -m midas submit manual-run --candidates candidate.bin --output manual-bundle
```

An idle unresolved run exits **1** and remains resumable. `submit` takes over a
saved run; stop an active controller first. With multiple possible owners, pass
`--owner OBLIGATION_ID` from `status --nodes`. Submissions are capped at 8 MiB.
Conditional candidates require explicit supplied approval and still undergo
compiler checks. The full candidate schema is in
[`submission.py`](new/midas/domain/submission.py).

## Use scripted model replies

`--script` reads a small manifest of raw reply files, resolved relative to the
manifest. For example, save `script.json`:

```json
[{"role": "prover", "file": "reply.txt"}]
```

For the same `example_target`, `reply.txt` can contain:

```json
{
  "action": {
    "mode": "proof",
    "rationale": "True has a constructor.",
    "proof_body": "by trivial",
    "proof": "Apply the constructor of True.",
    "proof_departures": "",
    "definitions": [],
    "imports": []
  },
  "memory_update": null
}
```

```sh
python -B -m midas start scripted-run --package package --selection selection.bin \
  --config live.json --script script.json --output scripted-bundle
```

A configured prover route is still required, but replies come from the files
without model credentials or model API calls. Manifest roles are `prover`,
`reviewer`, `research`, and `web_search`. Replies are consumed in order within each role;
retries reuse the saved reply. The manifest is capped at 16 KiB and 512 entries;
reply files are capped at 4 MiB each and 64 MiB combined. The configured model
output limit also applies (128 KiB by default).

## Inspect, stop, resume, and fork

These commands inspect committed progress, including while a run is active:

```sh
python -B -m midas status run
python -B -m midas status run --nodes --state UNRESOLVED --limit 100 --json
python -B -m midas status run --owner OBLIGATION_ID
```

Node states are `UNRESOLVED`, `CONDITIONAL`, and `VERIFIED`. The progress summary
counts current targets, dependencies, pending submission entries, and running work; other saved nodes
appear separately as `discarded` and do not inflate `waiting`. `--nodes` still
includes those historical nodes for inspection. JSON summaries expose the separate
`discarded_nodes` count.

`--roots` restricts results to selected roots; `--after ID` uses an exclusive
obligation-ID cursor.
Page sizes range from 1 to 1000. JSON pages include a revision and next cursor.
Status reads do not start compilers or require provider credentials.

Owner details contain reasons and artifact references. Inspect a reference or
stream its verified bytes to a new file outside the run directory:

```sh
python -B -m midas artifact run ARTIFACT_ID
python -B -m midas artifact run ARTIFACT_ID --output evidence.bin
```

Artifact extraction defaults to a 64 MiB cap; use `--max-bytes` to change it.

```sh
python -B -m midas stop run
python -B -m midas resume run --output bundle
```

`stop`, Ctrl-C, and SIGTERM request cooperative cancellation. Wait for the active
command to exit before resuming or submitting. Resume uses the saved selection,
instructions, workflow, and configuration; supply the same live credentials again.
If the package moved, pass `--package /new/path/to/package` with the same
environment identity. `MIDAS_SHARED_TOOLCHAIN` can relocate the matching installed
toolchain.

When no further work can run because the model-turn budget is exhausted, the
controller exits with `search_blocked / model_turn_budget` instead of idling.
Running work and remaining compiler/export checks can finish before that exit.

To increase allowances, save `increase.json` using new total limits:

```json
{
  "run": {"turns": 800, "runtime_ms": 14400000},
  "mathematics": {"direct": 10}
}
```

```sh
python -B -m midas resume run --raise-limits increase.json --output bundle
```

Supported increases include `run.turns`, `run.runtime_ms`, mathematical and process/export
limits, scheduling capacity, and `research_model.invocation_ms` or
`web_search_model.invocation_ms` (up to 900000 ms each). At least one value must
increase; none may decrease. Usage and elapsed
time do not reset, and already prepared requests retain their original bounds.
`submit` and `export` also accept `--raise-limits`.

Fork to try a different configuration or workflow while retaining reusable proof
evidence and the same selection:

```sh
python -B -m midas fork run forked-run --config live.json
python -B -m midas resume forked-run --output forked-bundle
```

Fork creates a checkpoint without starting execution. Local usage starts fresh;
the parent is unchanged. The child keeps the same environment identity and
selection. `--workflow live|scripted|supplied` selects its workflow.

## Export and verify

Publish a saved run, then independently recheck its bundle:

```sh
python -B -m midas export run exported-bundle
python -B -m midas verify exported-bundle --package package \
  --evidence-output verification.bin
```

Bundles contain `src/` Lean sources, `lean-toolchain`, `request.bin`, `audit.bin`,
and `manifest.bin`. Export audits the complete source, including target signatures
and non-target declarations. Strict export rejects unfinished declarations left
outside the selected targets as well as unresolved selected work.

Ordinary CLI runs use the source export profile, which verifies with the toolchain
alone. Packaging additional imports does not automatically capture a replayable
export library. For projects needing non-toolchain imports at export, LeanEval
prepares that library, or the application API accepts an explicitly prepared
library and its reader (see [implementation notes](new/README.md#export)).

To publish incomplete work explicitly:

```sh
python -B -m midas export manual-run partial-bundle --allow-sorry
```

This permits `sorry` in a checked partial bundle and leaves its proof state
incomplete. `verify` honors the bundle's saved partial mode; successful verification
of such a bundle does not mean every theorem is proved. LeanEval finalization
rejects partial bundles.

Publication never replaces conflicting output. An identical existing bundle is
accepted; use a new directory for a changed result. Selection, candidate, and
evidence output files must also be new. Source and evidence payloads stay raw or
binary; `--json` is for bounded summaries.

## Configuration and exit codes

Policy JSON uses `"format": "midas-config-v1"` and is capped at 16 KiB. Omitted
settings use defaults; unknown fields and duplicate keys are rejected. See
[`config.json`](new/config.json) for the full example and
[`policy/config.py`](new/midas/policy/config.py) for field definitions.

| Section | Controls |
| --- | --- |
| `run` | Model turns, runtime, intent count, optional token and cost budgets. |
| `scheduling` | Model concurrency, shared compiler/search CPU slots, LeanExplore request concurrency, compiler memory pool, backlog thresholds. |
| `mathematics`, `helpers` | Proof attempts, repairs, backtracks, research, web search, retrieval, and probes. |
| `checker`, `local`, `export` | Wall time, memory, byte limits; checker/export heartbeats and recursion depth. |
| `model`, `research_model`, `web_search_model`, `recovery` | Invocation/transport limits and retry policy. |
| `prover`, `reviewer`, `research`, `web_search` | Model routes and optional prices. |

Durations use milliseconds; memory uses MiB. Prices are integer micro-USD per
million tokens; `run.max_cost_microusd` is a total micro-USD budget. Model calls
automatically calculate cost from the calling role's `prices` when the provider
does not report a cost. Provider-reported costs, including zero, take precedence.
Cached reads and writes are partitioned out of total input tokens, and each call
rounds up once to a whole micro-USD. Omitted `cache_write` prices charge writes at
the ordinary input rate.

Omitted prices or missing telemetry needed for the calculation leave cost unknown.
For example, a distinct `cache_write` rate requires write-token telemetry; omit
that rate when writes should use the ordinary input rate. Saved receipts retain
the calculated cost across resume. Retries count each execution's consumed usage;
scripted calls remain free. `status` displays the known USD subtotal plus
`+ unknown` when any charged call has unknown cost. Its JSON output includes
`usage.cost_microusd`, `known_usage.cost_microusd`, and
`unknown_reports.cost_microusd`. The explicit cache diagnostic uses the same pricing.

`checker.memory_mib` and `export.memory_mib` must not exceed
`scheduling.compiler_memory_mib`.

| Exit code | Meaning |
| --- | --- |
| `0` | Command succeeded; strict run completion requires verified export. A successful status query only means the query succeeded. |
| `1` | Run incomplete/blocked, or standalone verification failed. |
| `2` | Invalid arguments, setup failure, or recovery error. |
| `130` | Cooperative stop/cancellation. |

The explicit cache diagnostic makes four Responses calls using a configured
prover and writes a separate diagnostic run:

```sh
python -B -m midas cache-probe cache-diagnostic --config live.json
```

## LeanEval

`python -B -m midas.leaneval` accepts ordinary Midas commands and adds `prepare`,
`finalize`, and `verify-submission`. It also adds `start --workspace`.

Before preparation, provide the benchmark checkout at a pinned Git revision,
the exact generated starter project, built dependencies and local helper modules,
and the four verifier tools: `landrun`, `lean4export`, `comparator`, and `nanoda_bin`.
Save its package settings as `benchmark-package.json` using the package format
above, including `project/Submission.lean` in `project_inputs` and all required
compiled-module directories in `search_paths`.
Compiled helpers need Lean's `.olean` sidecars; a normal Lake build supplies them.

Save `verification.json`, replacing all paths and pins:

```json
{
  "format": "midas-verification-config-v2",
  "benchmark": "/absolute/path/to/benchmark-checkout",
  "revision": "REPLACE_WITH_40_LOWERCASE_HEX_DIGITS",
  "problem": "sample",
  "tools": {
    "landrun": {"path": "/tools/landrun", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"},
    "lean4export": {"path": "/tools/lean4export", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"},
    "comparator": {"path": "/tools/comparator", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"},
    "nanoda_bin": {"path": "/tools/nanoda_bin", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"}
  },
  "dependencies": [],
  "allow_archived_fixture": false
}
```

The config is capped at 16 KiB; paths resolve relative to it. Each required
dependency needs an entry with `name`, `path`, `module_paths`, and `source_roots`;
its name must match the frozen package root. Use the benchmark commit and tool
SHA-256 hashes as the pins. `dependencies: []` applies only when none are needed.

```sh
python -B -m midas.leaneval package benchmark-package --config benchmark-package.json
python -B -m midas.leaneval prepare workspace --package benchmark-package \
  --verification-config verification.json
python -B -m midas.leaneval start benchmark-run --workspace workspace \
  --config live.json --output benchmark-bundle
python -B -m midas.leaneval status benchmark-run
python -B -m midas.leaneval resume benchmark-run --output benchmark-bundle
python -B -m midas.leaneval finalize benchmark-bundle result --workspace workspace \
  --verification-config verification.json --runtime /absolute/path/to/regular-usr
python -B -m midas.leaneval verify-submission result --workspace workspace \
  --verification-config verification.json --runtime /absolute/path/to/regular-usr \
  --evidence-output benchmark-verification.bin
```

`--workspace` supplies the package, complete benchmark target selection (including
definitions), and export library. Do not combine it with package/source/selection
or target flags. It also supports ordinary scripted and supplied execution.

LeanEval accepts attached packages. Use the same attachment configuration described
above, then declare it when preparing the workspace. `--import` advertises a module
that candidates may import; repeat it for up to 32 modules, including modules from
the benchmark dependencies when needed.

```sh
python -B -m midas.leaneval package package-with-extra --base benchmark-package \
  --config attachment.json
python -B -m midas.leaneval prepare workspace-with-extra --package package-with-extra \
  --verification-config verification.json --attachment attachment.json --import ExtraLib
python -B -m midas.leaneval start benchmark-run --workspace workspace-with-extra \
  --config live.json --output benchmark-bundle
```

Repeat package attachment and `--attachment` for multiple libraries. The starter
and benchmark dependency pins remain exact. Attached objects are trusted as frozen
compiled dependencies during proof checking and ordinary export. Finalization
copies their frozen sources and literal `include_str` assets into the submission,
adds local Lake library targets, and rebuilds them through the benchmark harness
before checking Lean, nanoda, and the comparator. Attachments cannot replace
benchmark inputs or existing modules. Keep the attached roots available and unchanged.

`scheduling.compiler_concurrency` bounds the combined number of active Lean jobs
and frozen-source searches. `scheduling.network_concurrency` separately bounds
LeanExplore API requests; model calls use `model_concurrency`. Compiler memory and
retained import-worker memory still use `compiler_memory_mib`. Searches retain their
per-job `local` limits. The former `local_concurrency` setting has been removed.

By default, preparation trusts frozen compiled dependency files, including
Mathlib, while preserving local helper sources for export rebuilds. Add
`prepare --replay-dependencies` to rebuild dependency sources as well. This choice
is saved with the workspace. Keep referenced dependencies available and unchanged;
Midas does not fetch or build missing external dependencies during preparation.

`finalize` takes an ordinary exported bundle. It independently audits the source,
then runs the archived `lake test` harness and requires acceptance from both Lean
and nanoda plus the comparator. `--runtime` supplies a regular-file tree mounted
as `/usr`, containing an executable `bin/sh`, Git, the dynamic loader, and required
native libraries. Sources and dependencies are read-only during this check.

Results contain `bundle/`, `submission/`, `verification.bin`, `manifest.bin`, and,
for verified active problems, `judge-submission/PROBLEM/`. Explicitly allowed
archived fixtures do not produce judge-ready output. Use a new result directory
for a new verification report.

`verify-submission` performs fresh auditing and comparator execution, including
checks of the submission and judge copies. It needs the workspace and frozen
package; `--package` allows an identical relocated package. The original benchmark
checkout is needed only for preparation. Ordinary `verify` checks a source bundle;
it does not perform the LeanEval submission checks.

More detailed component and validation notes are in
[`new/README.md`](new/README.md).
