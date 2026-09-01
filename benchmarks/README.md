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
3. compiled Packet v2 with file-owned `do` actions, source anchors, and a stale-route hash.

Packet v2 versus Semantic v1 is the primary comparison. Markdown is secondary;
otherwise ordinary semantic compression could be mistaken for a packet benefit.

Static compression alone is not evidence that a specification is useful. The
implementation track checks whether an agent can still produce behavior that
passes deterministic tests.

## Controls

- Both variants describe the same task and use the same starter workspace.
- Runs are paired by case and repetition, then shuffled with a recorded seed.
- Every run gets a fresh isolated workspace containing only starter files.
- Implementation agents receive only the starter repository and run with
  `workspace-write`. Hidden tests and expected outputs remain in the grader parent.
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
- The JSON result records environment versions, model, seed, raw usage totals,
  acceptance results, exact verification status, command-category telemetry,
  and failures.
- Fixture, starter, specification, prompt, and verification hashes are captured
  before the first arm, checked between arms, and revalidated when rendering a
  report. Missing, duplicate, stale, or partially instrumented runs fail closed.
- A publishable run requires every pair and at least three repetitions. The
  report labels smaller or mock runs as smoke tests.
- Generated-spec runs keep every failed attempt and include all retry usage in
  authoring cost. `--max-attempts` is bounded; no result is silently discarded.
- Token savings are reported as product benefits only when measured
  implementation quality is preserved.

The benchmark does not claim that one small fixture set represents every
codebase, model, or specification style. Context and implementation reports
measure reuse of an already created semantic specification. The lifecycle
report adds the complete recorded one-time conversion cost.

## Requirements

- Python 3.11 or newer
- Git
- Codex CLI and working authentication for real agent runs

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

This suite records raw command strings and classifies coarse discovery, read,
and verification command events. A command event can contain multiple shell
operations, so those counts are directional and are reported separately from
provider token telemetry.

Use `--keep-workspaces` only when failed implementations need inspection.
Without it, generated workspaces are removed after the run.

Result and report paths are not overwritten unless `--force` is passed.

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
