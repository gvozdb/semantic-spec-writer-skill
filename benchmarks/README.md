# Benchmark

This benchmark compares a conventional Markdown specification with an equivalent
Semantic Spec Writer `.spec.ctx` document.

It measures two different things:

1. **Static compression**: document bytes, words, characters, and lines.
2. **Implementation performance**: acceptance pass rate, provider-reported token
   usage, duration, tool calls, and optional estimated API cost.

Static compression alone is not evidence that a specification is useful. The
implementation track checks whether an agent can still produce behavior that
passes deterministic tests.

## Controls

- Both variants describe the same task and use the same starter workspace.
- Runs are paired by case and repetition, then shuffled with a recorded seed.
- Every run gets a fresh isolated workspace containing only starter files.
- Graders and reference solutions remain outside the agent workspace. They are
  held out by convention, not protected by a container, so the result records
  that oracle exposure is possible.
- Codex runs use `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, and
  `--sandbox workspace-write`.
- Provider subprocesses receive an allowlisted environment that excludes API
  keys and unrelated server variables. Codex reuses its local authentication.
- The JSON result records environment versions, model, seed, raw usage totals,
  acceptance results, and failures.
- A publishable run requires every pair and at least three repetitions. The
  report labels smaller or mock runs as smoke tests.

The benchmark does not claim that one small fixture set represents every
codebase, model, or specification style. It measures reuse of an already
created semantic specification and does not include the one-time conversion
cost.

## Requirements

- Python 3.11 or newer
- Git
- Codex CLI and working authentication for real agent runs

No Python packages are required.

## Validate fixtures

```bash
python3 benchmarks/benchmark.py validate
python3 benchmarks/benchmark.py static --check
python3 -m unittest discover -s benchmarks/tests -v
```

Validation proves that every reference implementation passes, every starter
implementation fails, and each semantic document is structurally valid.

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

Use `--keep-workspaces` only when failed implementations need inspection.
Without it, generated workspaces are removed after the run.

Result and report paths are not overwritten unless `--force` is passed.

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
