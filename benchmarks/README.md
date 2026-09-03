# Benchmark

The primary public benchmark compares ordinary Markdown with the complete
Semantic Spec Writer workflow. Markdown receives the complete source task with
zero preparation calls. The skill arm pays every authoring input/output token,
deterministic context compilation, and every implementation input/output token.

It measures four different things:

1. **Static compression**: document bytes, words, and optional tokenizer counts.
2. **Context loading**: provider-reported input tokens for paired fixed-output,
   zero-tool reads.
3. **Implementation performance**: acceptance pass rate, provider-reported token
   usage, duration, tool calls, and optional estimated API cost.
4. **Lifecycle cost**: semantic-spec authoring tokens plus repeated implementation
   tokens and measured break-even reuse.

Historical component tracks also measure already-authored semantic documents
and an internal three-arm routing experiment:

1. conventional Markdown;
2. Semantic v1 with a compact edit scope;
3. compiled Packet v3 with file-owned `do` actions, source anchors, a stale-route hash, and bounded execution.

Those component tracks remain useful for regression diagnosis. They are not the
public full-workflow claim and are never pooled into it.

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
- Exact selected specification bytes are embedded as canonical base64
  task-document attestations. Credibility checks decode those bytes and
  recompute document hashes, prompt hashes, metrics, and static rows; a
  plausible-looking digest or metric object alone is not evidence. Rejected
  authoring output is omitted; only its hash, size metrics, deterministic
  validation codes, and fully charged provider usage survive.
- Fixture, starter, specification, prompt, and verification hashes are captured
  before the first arm, checked between arms, and revalidated when rendering a
  report. Missing, duplicate, stale, or partially instrumented runs fail closed.
- A publishable run requires every pair and at least three repetitions. The
  report labels smaller or mock runs as smoke tests.
- Generated-spec runs keep every failed attempt and include all retry usage in
  authoring cost. `--max-attempts` is bounded; no result is silently discarded.
- The public Capsule lifecycle run permits at most two authoring attempts per
  fixture. Every attempt's input/output usage is embedded in the same result and
  charged in full before any token-saving claim is evaluated.
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
- Capsule publication uses all complete pairs, including Markdown quality
  failures, and is bound to the committed preregistration. Post-result reruns,
  arm filtering, and threshold changes are outside the protocol.

The benchmark does not claim that one small fixture set represents every
codebase, model, or specification style. Historical context and implementation
reports measure reuse of an already created semantic specification. The public
Capsule lifecycle report includes its complete recorded authoring cost.

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

Exercise the complete Capsule authoring and implementation pipeline without a
paid model call:

```bash
smoke_dir=$(mktemp -d)
python3 -B benchmarks/handoff.py run \
  --comparison markdown-capsule-lifecycle-v2 \
  --provider mock \
  --repetitions 1 \
  --seed 20260901 \
  --timeout-seconds 30 \
  --output "$smoke_dir/result.json"
python3 -B benchmarks/handoff.py report "$smoke_dir/result.json"
```

Mock output must remain non-publishable. It validates mechanics, not model
quality or token savings.

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

Run the preregistered ordinary Markdown versus Capsule v6 lifecycle benchmark:

```bash
CAPSULE_RUN_NAME=gpt-5.6-terra-medium-20260903-markdown-capsule-lifecycle-v2
CAPSULE_RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/capsule-lifecycle-v2.XXXXXX")

python3 -B benchmarks/handoff.py run \
  --comparison markdown-capsule-lifecycle-v2 \
  --provider codex \
  --model gpt-5.6-terra \
  --reasoning-effort medium \
  --repetitions 3 \
  --seed 20260901 \
  --timeout-seconds 600 \
  --output "${CAPSULE_RUN_DIR}/capsule-r3.json"

python3 -B benchmarks/handoff.py report \
  "${CAPSULE_RUN_DIR}/capsule-r3.json" \
  --output "${CAPSULE_RUN_DIR}/CAPSULE.md"
```

Both arms start from the same complete `baseline.md`, captured fixture, clean
starter repository, hidden grader, model, reasoning effort, timeout, and
counterbalanced implementation schedule. Markdown receives that source task
directly and pays zero preparation-model tokens. Capsule pays for a bounded
authoring process of at most two attempts per fixture through the published
skill, then a deterministic compiler seals the generated route and exact source.
The second attempt runs only after deterministic validation rejects the first.
Every authoring input/output token and every implementation input/output token
is included. Historical Packet measurements are never pooled into this result.

The primary one-use comparison charges the complete fixture-specific authoring
bill to every Capsule pair. The measured three-use comparison charges authoring
once and reuses each handoff for three independent implementations. Quality
failures remain in the primary population instead of being filtered out. This
is conservative for Capsule because Markdown receives a complete human-readable
task without paying for its creation.

The Capsule report distinguishes total discovery/read events from classified
pre-edit events and measures combined input plus output tokens. Capsule v6
evidence embeds the exact preregistration, generated handoffs, authoring usage,
and execution profile. Publication requires one valid handoff per
fixture, every Capsule hidden test and acceptance check, no pairwise quality
loss, the exact one-edit/one-verification action sequence, fewer one-use total
tokens in every pair, a positive fixture-cluster interval, lower measured
three-use total tokens, and zero terminal authoring, provider, verification,
provenance, privacy, or implementation errors. Provider usage and duration
remain self-reported. Static overhead is byte-auditable; no tokenizer-derived
document claim is published.

The exact corpus, model, seed, timeout, repetitions, metric, bootstrap, gates,
and no-rerun rule are committed in
[`capsule-lifecycle-v2.prereg.json`](capsule-lifecycle-v2.prereg.json). The real
run command refuses any mismatch. Do not rerun or weaken a threshold after
observing an unfavorable result.

The only real v2 run failed the publication gate. Capsule implementation used
48.32% fewer tokens, but five charged authoring calls raised the measured
three-use lifecycle total from 1,222,545 Markdown tokens to 2,085,991 Capsule
tokens (+70.63%). It won 0/9 one-use token pairs, completed only 6/9 routed
action gates, and did not improve task success (6/9 for both arms). It was not
rerun or installed under `results/published`; see [`FAILED_RUNS.md`](FAILED_RUNS.md)
and the privacy-redacted [failed report](results/failed/gpt-5.6-terra-medium-20260903-markdown-capsule-lifecycle-v2/CAPSULE.md).

Stop if the report says `Non-publishable run`. For a passing run only, publish
the exact result and report bytes:

```bash
install -d -m 0755 "benchmarks/results/published/${CAPSULE_RUN_NAME}"
install -m 0644 "${CAPSULE_RUN_DIR}/capsule-r3.json" \
  "benchmarks/results/published/${CAPSULE_RUN_NAME}/capsule-r3.json"
install -m 0644 "${CAPSULE_RUN_DIR}/CAPSULE.md" CAPSULE_BENCHMARK.md
install -m 0644 "${CAPSULE_RUN_DIR}/CAPSULE.md" \
  "benchmarks/results/published/${CAPSULE_RUN_NAME}/CAPSULE.md"
```

The 2026-09-03 v1 run failed its authoring gate before implementation and was
not rerun. No Capsule savings claim was published. The immutable outcome and
evidence limitation are recorded in [`FAILED_RUNS.md`](FAILED_RUNS.md).

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
