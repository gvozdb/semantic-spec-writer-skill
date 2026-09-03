# Benchmark

This benchmark compares a conventional Markdown specification with an equivalent
Semantic Spec Writer `.spec.ctx` document.

It measures four different things:

1. **Static compression**: document bytes, words, and optional tokenizer counts.
2. **Context loading**: provider-reported input tokens for paired fixed-output,
   zero-tool reads.
3. **Implementation performance**: acceptance pass rate, provider-reported token
   usage, duration, tool calls, and optional estimated API cost.
4. **Lifecycle cost**: semantic-spec authoring tokens plus repeated implementation
   tokens and measured break-even reuse.

An additional execution-packet experiment uses three arms on multi-file cases:

1. conventional Markdown;
2. Semantic v1 with a compact edit scope;
3. compiled Packet v3 with file-owned `do` actions, source anchors, a stale-route hash, and bounded execution.

Packet v3 versus Semantic v1 is the primary comparison. Markdown is secondary;
otherwise ordinary semantic compression could be mistaken for a packet benefit.

Static compression alone is not evidence that a specification is useful. The
implementation track checks whether an agent can still produce behavior that
passes deterministic tests.

## Controls

- Both variants describe the same task and use the same starter workspace.
- Runs are paired by case and repetition, then shuffled with a recorded seed.
  Arm order rotates independently within every fixture, so each arm appears
  first equally often when repetitions are divisible by the arm count. With
  three repetitions and two arms, the unavoidable residual imbalance is only
  2-to-1, with its direction deterministically varied by fixture and seed.
- Every run gets a fresh isolated workspace containing only starter files.
- Implementation agents receive only the starter repository and run with
  `workspace-write`. `case.json` and hidden `tests.json` are descriptor-captured
  with that run's fixture snapshot, then materialized only in a private grader
  directory after provider execution. They never enter provider workspaces.
  Each hidden solution call then runs separately in a network-disabled, read-only
  Codex sandbox that can read the workspace runtime but not `tests.json`.
- Visible verification runs on a disposable copy after the harness restores the
  declared `verification_files` from the immutable fixture. Agent edits cannot
  weaken the smoke test that determines task success.
- Untrusted grading and verification are additionally wrapped in Linux PID,
  mount, and network namespaces via `unshare`. The private PID namespace prevents
  `/proc` from exposing the parent grader or host-process environments. These
  runs fail closed when `unshare` is unavailable.
- Codex runs use `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, and
  `--sandbox workspace-write`.
- Provider subprocesses receive an allowlisted environment that excludes API
  keys and unrelated server variables. Codex reuses its local authentication.
- Current-harness JSON results record environment versions, model, seed,
  aggregate usage totals, acceptance results, verification status,
  command-category telemetry, and privacy-redacted failure metadata. They do
  not persist provider-controlled prompts, raw command strings, messages,
  stdout/stderr, or grader failure prose.
- Exact specification bytes are embedded as canonical base64 task-document
  attestations. Credibility checks decode those bytes and recompute document
  hashes, prompt hashes, metrics, and static rows; a plausible-looking digest
  or metric object alone is not evidence.
- Fixture, starter, specification, prompt, and verification hashes are captured
  before the first arm, checked between arms, and revalidated when rendering a
  report. Missing, duplicate, stale, or partially instrumented runs fail closed.
- A publishable run requires every pair and at least three repetitions. The
  report labels smaller or mock runs as smoke tests.
- Generated-spec runs keep every failed attempt and include all retry usage in
  authoring cost. `--max-attempts` is bounded; no result is silently discarded.
- Capsule source hashes come from its parsed, sealed source frames. All Capsule
  arms receive workspaces materialized from the same validated in-memory starter
  snapshot, so replacing a fixture path between validation and copy cannot alter
  provider input.
- Claim-eligible Capsule runs start from a clean Git worktree and record the
  exact commit plus required harness/builder Git tree modes, object types, and
  blob identities, including the isolated release launcher. Release validation
  requires each tree entry to retain the attested mode/type/OID and bytes at the
  run commit, current `HEAD`, and live worktree. It parses NUL-delimited Git raw
  diff/status records, rejects hidden index flags, and includes untracked and
  ignored paths. A descendant `HEAD` may change only `README.md`,
  `benchmarks/README.md`, and one new non-executable release set consisting of
  `CAPSULE_BENCHMARK.md` plus `capsule-r3.json` and `CAPSULE.md` inside one safe
  `benchmarks/results/published/<run>/` directory. Deletes, copies, renames,
  type/mode changes, symlinks, bytecode caches, and every other path fail closed.
- Token savings are reported as product benefits only when measured
  implementation quality is preserved.

The benchmark does not claim that one small fixture set represents every
codebase, model, or specification style. Context and implementation reports
measure reuse of an already created semantic specification. The lifecycle
report adds the complete recorded one-time conversion cost.

Privacy-by-default applies to artifacts generated by the current harness. The
linked published result directories are legacy historical artifacts that retain
raw telemetry; label and handle them as **unsanitized, non-secret-only**
reference material, not as privacy-safe artifacts.

## Requirements

- Python 3.11 or newer
- Git
- Codex CLI and working authentication for real agent runs
- Atomic publication is Linux-specific and requires `O_TMPFILE`, `/proc/self/fd`, `linkat`,
  `renameat2(RENAME_EXCHANGE)`, descriptor-relative `openat`/`fstatat`/
  `unlinkat`, and `fcntl` advisory locking. Result checkpoints and atomic report
  output fail closed rather than use a pathname-temporary fallback when these
  facilities or a supported filesystem are unavailable.

No Python packages are required for fixture validation or implementation runs.
Tokenizer-aware conversion and static checks additionally require:

```bash
python3 -m venv /tmp/semantic-spec-tokenizer
/tmp/semantic-spec-tokenizer/bin/pip install -r benchmarks/requirements-tokenizer.txt
```

## Validate fixtures

```bash
python3 benchmarks/benchmark.py validate
python3 benchmarks/benchmark.py static --check
python3 -m unittest discover -s benchmarks/tests -v
```

Validate the separate multi-file packet suite:

```bash
python3 benchmarks/handoff.py validate
python3 benchmarks/handoff.py static --token-encoding o200k_base
```

Validation proves that every reference implementation passes, every starter
implementation fails, and each semantic document is structurally valid.

Run a tokenizer-aware check against generated specs:

```bash
/tmp/semantic-spec-tokenizer/bin/python benchmarks/benchmark.py static \
  --semantic-dir benchmarks/results/generated/specs \
  --token-encoding o200k_base \
  --check
```

## Run a free local smoke test

The mock provider copies reference implementations into isolated workspaces. It
tests benchmark mechanics, not model quality.

```bash
python3 benchmarks/benchmark.py run \
  --provider mock \
  --repetitions 1 \
  --output /tmp/semantic-spec-smoke.json

python3 benchmarks/benchmark.py report \
  /tmp/semantic-spec-smoke.json
```

## Run the real benchmark

Specify the model explicitly when results will be published:

```bash
python3 benchmarks/benchmark.py run \
  --provider codex \
  --model YOUR_MODEL \
  --reasoning-effort medium \
  --repetitions 3 \
  --timeout-seconds 600 \
  --output benchmarks/results/run.json

python3 benchmarks/benchmark.py report \
  benchmarks/results/run.json \
  --output BENCHMARK.md
```

Limit a development run to one or more cases:

```bash
python3 benchmarks/benchmark.py run \
  --provider codex \
  --model YOUR_MODEL \
  --case email-routing \
  --repetitions 1
```

Run the three-arm execution-packet benchmark independently:

```bash
python3 benchmarks/handoff.py run \
  --provider codex \
  --model YOUR_MODEL \
  --reasoning-effort medium \
  --repetitions 3 \
  --output benchmarks/results/handoff-r3.json

python3 benchmarks/handoff.py report \
  benchmarks/results/handoff-r3.json \
  --output HANDOFF_BENCHMARK.md
```

This suite stores command hashes and lengths, not raw command strings. It
classifies coarse discovery, read, and verification events. A command event can
contain multiple or indirectly scripted operations, so these counts are
directional telemetry rather than a filesystem-access audit.

Published `gpt-5.6-terra`, medium-reasoning Packet v3 run:

- 27/27 complete, unique arm runs with no provider or verification failures;
- Packet v3 task success: 9/9; Semantic v1 task success: 6/9;
- Packet v3 used 12.15% fewer uncached-input tokens across all runs;
- the strict equal-success comparison covered 6/9 pairs and its 95% fixture
  confidence interval crossed zero.

See the [rendered report](../HANDOFF_BENCHMARK.md) and
[historical raw result — unsanitized, non-secret-only](results/published/gpt-5.6-terra-medium-20260902-execution-packet/handoff-r3.json).

Run the public two-arm ordinary Markdown versus Capsule v6 benchmark independently:

```bash
python3 -B benchmarks/handoff.py static --comparison markdown-capsule-v6

CAPSULE_RUN_NAME=your-model-medium-yyyymmdd-markdown-capsule-v6

python3 -B benchmarks/handoff.py run \
  --comparison markdown-capsule-v6 \
  --provider codex \
  --model YOUR_MODEL \
  --reasoning-effort medium \
  --repetitions 3 \
  --timeout-seconds 600 \
  --output "benchmarks/results/published/${CAPSULE_RUN_NAME}/capsule-r3.json"

python3 -B benchmarks/handoff.py report \
  "benchmarks/results/published/${CAPSULE_RUN_NAME}/capsule-r3.json" \
  --output CAPSULE_BENCHMARK.md

python3 -B benchmarks/handoff.py report \
  "benchmarks/results/published/${CAPSULE_RUN_NAME}/capsule-r3.json" \
  --output "benchmarks/results/published/${CAPSULE_RUN_NAME}/CAPSULE.md"
```

Both arms use the same captured fixture, clean starter repository, hidden
grader, model, reasoning effort, timeout, and counterbalanced run order. The
ordinary Markdown arm receives the exact existing `baseline.md` through the
generic implementation prompt, without embedded source or Capsule controls.
The Capsule arm receives its sealed routed source and action protocol. This is
an end-to-end execution-handoff comparison after both artifacts exist, not a
serialization-only compression claim. Capsule authoring cost and reuse
break-even remain outside this run, and historical Packet measurements are
never pooled into it.

The Capsule report distinguishes total discovery/read events from classified
pre-edit events, measures combined input plus output tokens, and reports an
input-only cache-price break-even without hardcoding provider prices. Current
Schema-v5 evidence records the exact `capsule-v6-next-action-1` execution profile.
A Capsule
run is claim-eligible only when one file-change operation contains every routed
edit, the exact declared verification runs alone exactly once, no other tool
call occurs, the sealed tail next-action record matches those routes and names
only the first file-change action, no competing host instruction follows the
artifact, and the provider attempt count is one. Provider telemetry and
grades remain self-reported; a separate
secure routed-file comparison makes no-edit and partial-edit outcomes fail
regardless of reported status or verification exit. Publication also requires
preserved quality, complete paired telemetry, and a positive total-token
fixture-cluster confidence-interval lower bound. Git history is the publication
trust boundary. Static overhead in the run report is byte-only, which is
reproducible without an optional tokenizer package. Do not publish an exact
tokenizer claim unless the result schema records and credibility-checks the
encoding, package identity, per-arm counts, and aggregate.

Capsule evidence is published separately from the clean code-stage commit. Use
a fresh checkout or dedicated worktree with no ignored files, run the benchmark
at that commit, then generate only the release files above in a descendant
commit or the current worktree. The run name is restricted to 1–128 lowercase
ASCII letters, digits, dots, underscores, and hyphens, beginning and ending with
an alphanumeric character. Validate the exact three-file publication with:

```bash
capsule_release_validate() {
  set -o pipefail
  env -i PATH="$PATH" LC_ALL=C GIT_NO_REPLACE_OBJECTS=1 GIT_OPTIONAL_LOCKS=0 \
    git --no-pager cat-file blob HEAD:benchmarks/validate_capsule_release.py | \
    python3 -I /dev/stdin "$@"
}

capsule_release_validate \
  "benchmarks/results/published/${CAPSULE_RUN_NAME}/capsule-r3.json" \
  CAPSULE_BENCHMARK.md
```

The code-stage test suite does not require a stale or not-yet-published Capsule
directory. CI executes the launcher's exact `HEAD` blob through the external
bootstrap above before any repository Python module or test. Direct execution
of the live launcher is rejected because a file cannot securely attest its own
control flow.
It accepts exactly two states: no current Capsule artifacts (the clean stage-1
code commit), or one root report paired with one safe directory containing only
`capsule-r3.json` and the byte-identical `CAPSULE.md`. The `-I` flag removes the
repository and script directory from Python's import search path; the launcher
then performs a stdlib-only Git/filesystem preflight. Even the empty stage-1
state rejects repository files or bytecode caches that could shadow standard
library imports. For stage 2, Python modules execute from the already verified
Git-blob bytes rather than reopening live repository paths; the Packet checker
is materialized in a private temporary directory. Stage-2 validation never
executes fixture graders or verification commands: those run while producing
the benchmark result; publication replays structural, snapshot, provenance,
privacy, and exact-rendering checks over a private fixture tree materialized
from the attested Git commit. It never reopens live fixture paths. Lone,
duplicate, nested, symlinked, executable, or pre-attestation artifacts fail.
Complete pairs are checked for ancestry, historical schema, code revision,
credibility, privacy schema, and rendering drift. The privacy schema is
fail-closed at every nested provider boundary:
unknown/raw keys are rejected rather than ignored. Direct
`benchmarks/handoff.py validate-release` and
`python3 benchmarks/validate_capsule_release.py` invocations are intentionally
rejected.

Use `--keep-workspaces` only when failed implementations need inspection.
Without it, generated workspaces are removed after the run.

Result and report paths are not overwritten unless `--force` is passed. Report
CLIs pin and parse each input inode before rendering, then protect those exact
inode identities during atomic publication; direct, symlink, hard-link, and
read-then-path-swap aliases are rejected without clobbering the inputs.

## Generate and benchmark the real skill output

The curated `.spec.ctx` fixtures test the format. This track tests what the
skill actually generates from the Markdown fixtures and records every attempt:

```bash
/tmp/semantic-spec-tokenizer/bin/python benchmarks/lifecycle.py generate \
  --provider codex \
  --model YOUR_MODEL \
  --reasoning-effort medium \
  --token-encoding o200k_base \
  --max-attempts 2 \
  --output benchmarks/results/generated
```

Measure isolated context loading:

```bash
python3 benchmarks/context.py run \
  --provider codex \
  --model YOUR_MODEL \
  --reasoning-effort medium \
  --semantic-dir benchmarks/results/generated/specs \
  --repetitions 3 \
  --output benchmarks/results/generated/context-r3.json
```

Measure implementation quality and the complete agent loop:

```bash
python3 benchmarks/benchmark.py run \
  --provider codex \
  --model YOUR_MODEL \
  --reasoning-effort medium \
  --semantic-dir benchmarks/results/generated/specs \
  --repetitions 3 \
  --output benchmarks/results/generated/implementation-r3.json
```

Render all reports:

```bash
python3 benchmarks/context.py report \
  benchmarks/results/generated/context-r3.json \
  --output CONTEXT_BENCHMARK.md

python3 benchmarks/benchmark.py report \
  benchmarks/results/generated/implementation-r3.json \
  --output BENCHMARK.md

python3 benchmarks/lifecycle.py report \
  benchmarks/results/generated/generation.json \
  benchmarks/results/generated/implementation-r3.json \
  --output LIFECYCLE_BENCHMARK.md
```

Current lifecycle reports recompute generated-document byte and word counts
from the attested source/spec bytes. They deliberately make no generated-
document tokenizer-reduction claim: `conversion_check` token fields are not
independent evidence because the tokenizer package/version is not attested.
The immutable historical lifecycle artifact retains its original rendering.

## Optional cost estimate

Pricing is never hardcoded because model prices and billing modes change. Pass
an explicit JSON file with USD rates per one million tokens:

```json
{
  "input": 0,
  "cached_input": 0,
  "cache_write_input": 0,
  "output": 0
}
```

```bash
python3 benchmarks/benchmark.py run \
  --provider codex \
  --model YOUR_MODEL \
  --repetitions 3 \
  --pricing-file /path/to/pricing.json
```

Replace zeroes with rates from the provider used for that run. Subscription
quota consumption is not equivalent to an API cost estimate.

## Adding a case

Each case contains:

```text
case-name/
|-- case.json
|-- baseline.md
|-- semantic.spec.ctx
|-- tests.json
|-- starter/
|   `-- solution.py
`-- reference/
    `-- solution.py
```

Keep the variants semantically equivalent. Add requirements only when the
grader can verify them deterministically. Do not pad baseline prose to inflate
compression numbers.
