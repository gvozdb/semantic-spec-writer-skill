# Semantic Spec Writer

[![Benchmark fixtures](https://github.com/gvozdb/semantic-spec-writer-skill/actions/workflows/benchmark.yml/badge.svg)](https://github.com/gvozdb/semantic-spec-writer-skill/actions/workflows/benchmark.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![Semantic Spec Writer: verbose Markdown compressed into a structured semantic specification](assets/social-preview.png)

Semantic Spec Writer turns plans, technical specifications, architecture notes, and acceptance criteria into a compact semantic format for LLMs.

It writes `.plan.ctx` and `.spec.ctx` files. Each file is instruction-complete,
so an implementation agent can read it without installing the skill. Repository
execution packets are intentionally bound to the validated repository snapshot.

## What it does

- converts verbose Markdown into compact `rules`, `flows`, `tasks`, and `acceptance` blocks;
- preserves exact file names, API routes, tables, fields, commands, and error codes;
- moves unresolved requirements into `open_questions` instead of inventing answers;
- allows short explanations as comment lines starting with `#`;
- keeps domain vocabulary local to each specification;
- compiles repository handoffs into file-owned `do` actions with validated source anchors and stale-route detection;
- compiles Packet v3 plus exact routed source into sealed Capsule v6 execution context with an edit-before-verify gate and sealed next-action focus.

## Benefits

- reduces the document size that agents and humans need to inspect;
- makes ambiguity visible before implementation starts;
- links requirements to tasks and acceptance criteria;
- replaces broad repository discovery with a bounded, stale-checked edit route;
- can remove downstream discovery and read turns by supplying routed source before execution;
- stays readable for both humans and LLMs;
- lets implementation agents read the output directly.

## Comparison

| Approach | Document size | Requirement precision | Implementation readiness | Main risk |
|---|---:|---:|---:|---|
| Regular Markdown | Usually larger | Depends on the author | Medium | Key details get buried in prose |
| Short summary | Smaller | Low to medium | Low | Conditions and edge cases get dropped |
| Semantic Spec Writer | Smaller in the benchmark | High when `acceptance` is complete | High | Too much compression without review |

Actual token savings depend on the source language, document structure, and tokenizer. Measure savings on real documents rather than comparing file sizes in bytes.
New lifecycle reports therefore recompute only byte/word document compression
from attested bytes and omit document-token reduction unless tokenizer identity
and counts become independently verifiable. Tokenizer-derived figures remain
only in the clearly labelled historical component results.

## Benchmark

The public benchmark compares what a user actually chooses between:

- **Ordinary Markdown:** give the complete task to the implementation agent.
  Preparation costs zero model calls.
- **Semantic Spec Writer workflow:** author a repository-bound handoff with the
  skill, compile its exact source context deterministically, then implement it.

Every skill-authoring and implementation input/output token is counted. The
protocol, model, corpus, retries, seed, repetitions, metric, and release gates
were committed and pushed before the only real v2 run:
[`benchmarks/capsule-lifecycle-v2.prereg.json`](benchmarks/capsule-lifecycle-v2.prereg.json).

### Full-workflow result

The `gpt-5.6-terra`, medium-reasoning v2 run **failed the publication gate**.
The skill reduced implementation tokens, but its authoring cost erased that
benefit at one and three uses.

| Measure | Ordinary Markdown | Skill workflow |
|---|---:|---:|
| Authoring model calls | 0 | 5 |
| Authoring model tokens | 0 | 1,454,228 |
| Successful implementations | 6/9 | 6/9 |
| Hidden tests passed | 94.67% | 96.00% |
| Acceptance checks passed | 87.88% | 90.91% |
| Routed action gate | n/a | 6/9 |
| Implementation model tokens | 1,222,545 | 631,763 (-48.32%) |
| Three-use lifecycle tokens | 1,222,545 | 2,085,991 (+70.63%) |
| Average one-use tokens per task | 135,838 | 554,939 (+308.53%) |

The skill won **0/9** one-use token pairs. The one-use fixture-cluster 95%
confidence interval was entirely negative: `[-358.964%, -277.637%]`. The
measured implementation savings would amortize the authoring bill only after an
estimated **8 full-corpus uses**; this is not a product claim because three skill
runs failed quality and action-sequence gates.

Plainly: the current sealed-handoff mode is useful as an experiment, but it is
not a token-saving default for one-off or three-use work. No favorable benchmark
was published. Both preregistered failures are recorded in
[`benchmarks/FAILED_RUNS.md`](benchmarks/FAILED_RUNS.md). The complete
privacy-redacted v2 [failed report](benchmarks/results/failed/gpt-5.6-terra-medium-20260903-markdown-capsule-lifecycle-v2/CAPSULE.md)
and [result JSON](benchmarks/results/failed/gpt-5.6-terra-medium-20260903-markdown-capsule-lifecycle-v2/capsule-r3.json)
remain available for independent recalculation.

### Existing semantic-format result

An older eight-task component benchmark compared already-authored semantic specs
with Markdown. Both variants completed 48/48 implementations. Semantic specs used
9.29% fewer full-loop input tokens, 7.31% fewer uncached input tokens, and 5.84%
fewer output tokens. Including conversion cost, total-input break-even was 13
full-corpus reuses. This is a narrower reuse result, not the failed full-workflow
claim above.

The repository retains older routing experiments for regression history, but
their internal format comparisons are not used to market the skill to users.
Those legacy result directories contain raw telemetry and are **unsanitized,
non-secret-only** artifacts.

```bash
python3 benchmarks/benchmark.py validate
python3 benchmarks/benchmark.py static --check
python3 benchmarks/handoff.py validate
python3 -m unittest discover -s benchmarks/tests
```

See the [implementation report](BENCHMARK.md),
[context report](CONTEXT_BENCHMARK.md),
[lifecycle report](LIFECYCLE_BENCHMARK.md),
[historical routing report](HANDOFF_BENCHMARK.md),
[historical generated artifacts - unsanitized, non-secret-only](benchmarks/results/published/gpt-5.6-terra-medium-20260901-generated/),
[historical routing artifacts - unsanitized, non-secret-only](benchmarks/results/published/gpt-5.6-terra-medium-20260902-execution-packet/),
and [methodology and commands](benchmarks/README.md).

## Project layout

```text
semantic-spec-writer-skill/
|-- README.md
|-- LICENSE
|-- benchmarks/
`-- skills/
    `-- semantic-spec-writer/
        `-- SKILL.md
```

Run all commands below from the `semantic-spec-writer-skill` directory.

## Quick install

Install from GitHub with the [Agent Skills CLI](https://www.skills.sh/docs/cli):

```bash
npx skills add gvozdb/semantic-spec-writer-skill
```

The manual Codex and Claude Code instructions below remain available when a
third-party installer is not desired.

## Install in Codex

Codex loads personal skills from `$HOME/.agents/skills` and project skills from `.agents/skills`.

Install for all projects:

```bash
mkdir -p "$HOME/.agents/skills/semantic-spec-writer"
cp -R skills/semantic-spec-writer/. "$HOME/.agents/skills/semantic-spec-writer/"
```

Install in one project:

```bash
mkdir -p /path/to/project/.agents/skills/semantic-spec-writer
cp -R skills/semantic-spec-writer/. /path/to/project/.agents/skills/semantic-spec-writer/
```

Check the installation and invoke the skill:

```text
/skills
$semantic-spec-writer Convert docs/feature.md into an implementation-ready .spec.ctx file
```

Codex usually detects skill changes automatically. Restart Codex if the skill doesn't appear in `/skills`.

[Official Codex Skills documentation](https://developers.openai.com/codex/skills)

## Install in Claude Code

Claude Code loads personal skills from `$HOME/.claude/skills` and project skills from `.claude/skills`.

Install for all projects:

```bash
mkdir -p "$HOME/.claude/skills/semantic-spec-writer"
cp -R skills/semantic-spec-writer/. "$HOME/.claude/skills/semantic-spec-writer/"
```

Install in one project:

```bash
mkdir -p /path/to/project/.claude/skills/semantic-spec-writer
cp -R skills/semantic-spec-writer/. /path/to/project/.claude/skills/semantic-spec-writer/
```

Check the installation and invoke the skill:

```text
/skills
/semantic-spec-writer Convert docs/feature.md into an implementation-ready .spec.ctx file
```

Claude Code watches existing skill directories for changes. Restart Claude Code if `.claude/skills` was created after the current session started.

[Official Claude Code Skills documentation](https://docs.anthropic.com/en/docs/claude-code/skills)

## Usage

Example prompts:

```text
Convert this technical specification to a compact .spec.ctx file.
Create an implementation plan as .plan.ctx from this issue and the current codebase.
Rewrite docs/auth.md as a self-contained semantic spec without inventing requirements.
Create a repository execution packet for this issue with the smallest complete edit route.
Compile this validated execution packet into a Capsule v6 handoff.
```

## Repository execution packets

For a coding handoff, the skill can compile requirements into a task-specific
route instead of appending a file list to a complete spec. Every existing or new
target owns one or more `do` actions. A SHA-256 basis over routed files detects
stale packets before implementation. Packet v3 also carries a bounded execution
loop: one routed read, one implementation pass, one declared verification, then
stop on success.

Validate a packet and its optional context budget:

```bash
python3 skills/semantic-spec-writer/scripts/check_execution_packet.py \
  /path/to/repository /path/to/task.spec.ctx \
  --encoding o200k_base \
  --max-context-tokens 4000
```

Use `--print-basis` while authoring, then place the returned
`route-sha256:<hash>` under `basis` and run the normal validation once.

The separate three-arm benchmark compares Markdown, Semantic v1, and compiled
Packet v3 on the same multi-file tasks. Packet v3 versus Semantic v1 is the
primary comparison; see [methodology and commands](benchmarks/README.md).

For a costly handoff or repeated execution, compile the validated packet and
its routed source into Capsule v6:

```bash
python3 skills/semantic-spec-writer/scripts/context_capsule.py build \
  /path/to/repository /path/to/task.spec.ctx /secure/path/task.capsule.ctx

python3 skills/semantic-spec-writer/scripts/context_capsule.py check \
  /path/to/repository /secure/path/task.capsule.ctx \
  --packet /path/to/task.spec.ctx
```

Capsules copy source code. Store them with repository-equivalent access
controls. Use Packet v3 directly when one small task does not justify the
Capsule's larger initial context. Secure CLI publication is Linux/POSIX-only
and fails closed when descriptor-relative no-follow or atomic publication
primitives are unavailable.

Semantic compression is lossy. Before handing a document to an implementation agent, review `acceptance`, exact identifiers, and `open_questions`.
