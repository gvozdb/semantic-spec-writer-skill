---
name: semantic-spec-writer
description: Write or rewrite plans, technical specs, architecture notes, implementation plans, acceptance criteria, repository-grounded coding handoffs, and sealed source capsules as compact execution context for LLM agents. Use when a user asks to create, compress, formalize, structure, convert, compile, or hand off planning and technical work.
---

# Semantic Spec Writer

Produce `.plan.ctx` and `.spec.ctx` files that an implementation agent can use without loading this skill.

## Mode

- New document: write the final semantic artifact directly from the request and grounded repository facts. Do not create a prose draft first.
- Existing document: convert it only as requested, preserving source meaning and identifiers. The final artifact must be shorter than the source.
- Handoff or repeated use: optimize for independent execution by another agent or later turn.
- Repository execution packet: when the user wants a coding handoff and the codebase is available, read [references/execution-packets.md](references/execution-packets.md). Ground a minimal read/edit route before writing the packet.
- Context capsule: when a validated Packet v3 will be executed in another turn or agent, read [references/context-capsules.md](references/context-capsules.md). Compile the packet plus its exact routed source only when removing downstream model/tool turns justifies the larger initial artifact.

Do not claim that conversion saves total tokens for a one-off implementation. It adds an authoring step; its economy comes from a smaller reusable context and less implementation ambiguity.

## Target

Optimize the whole implementation loop, not character count alone:

- preserve every behavior, constraint, edge case, and exact identifier;
- remove narrative, repetition, decorative metadata, and generic advice;
- expose the shortest reliable path from requirement to edit to verification;
- prefer direct language over shorthand that requires interpretation.

A smaller file is useful only when it remains faster to execute correctly. When converting an existing document, plan the compact structure before writing and avoid expanding the source. Do not add tool loops or repeated rewrites only to chase file size.

## Token discipline

- Read each supplied source once unless a missing fact requires a targeted lookup.
- Inspect only files needed to ground scope, interfaces, and verification.
- Write one final artifact. Make it shorter in the initial write. For conversion, run `scripts/check_conversion.py SOURCE OUTPUT` from this skill directory once. When the target tokenizer is known and `tiktoken` is available, pass `--encoding NAME`. If the check fails, remove redundancy once and run it once more; never continue a size-chasing loop. Do not emit an outline, draft, conversion notes, size report, or explanation unless requested.
- Do not copy repository facts that the implementation agent can obtain from the exact target file without search. Include facts that prevent a wrong edit or extra discovery.
- Prefer exact paths and one runnable verification command over generic implementation advice.
- For an execution packet, spend authoring effort only on facts that remove downstream discovery or prevent a wrong edit. Do not turn the packet into a repository dump.
- Give execution packets the canonical bounded downstream loop from the reference: one routed read, one implementation pass, one declared verification, then stop on success. Expansion requires a concrete contradiction or failure.
- For a Capsule v5 handoff, validate the trusted Packet v3 first, build once, and check it against both the current repository and original packet before execution. Build only for pending changes: its sealed control treats source frames as current pre-edit data and permits exactly two tool calls: one atomic file-change operation containing every routed edit, then the exact `V1` alone once. Do not rebuild or reread the capsule in a loop.

## Format

- Start with plain `plan` or `spec`.
- Use `.plan.ctx` for plans and `.spec.ctx` for specs unless the project has another convention.
- Use obvious local block names. They are labels, not reserved language keywords.
- Include only blocks that carry information. Do not emit empty sections.
- Combine target, scope, and contract facts when separate blocks would repeat the same information.
- Preserve file paths, symbols, API routes, fields, tables, env vars, commands, statuses, roles, and error text exactly.
- Preserve field ownership and nesting exactly. Match field notation to the implementation language: for Python JSON/maps use `event["payment_id"]`, not `event.payment_id`. Never invent a `data`, `payload`, or other wrapper that the source does not define.
- Treat `.ctx` as plain text, not Markdown. Do not wrap ordinary identifiers in backticks, bold, or other presentation markup. Use backticks only for runnable commands under `verify`.
- Define a local term only when it removes real repetition. Prefer descriptive terms; avoid one-letter aliases and invented notation.
- Add IDs only when another item references them.
- Use familiar expressions such as `x=true`, `x in [a,b]`, and `condition -> result`. Do not rely on hidden syntax.
- Use a short `#` comment only when structured fields would lose critical nuance.
- Group parallel validations, output fields, and exact errors under one compact mapping instead of repeating the same sentence frame.
- After field ownership is established, use short unambiguous labels in validation and error maps instead of repeating the full owner path.
- In an error map, let the block carry the repeated grammar. Prefer `field: condition | ErrorType "exact message"` over repeating `invalid owner.field -> ErrorType` on every line.
- Keep one independently testable rule per line. A shared mapping may hold related field cases when its grammar is consistent.
- Use `A*` IDs only once, under `acceptance`. Do not reuse acceptance IDs as rule or task IDs.

## Grounding

When a codebase is available, inspect it before writing. Capture known edit scope, existing interfaces, constraints, and runnable verification commands. Include `verify` only for an exact command or existing named check; do not invent verification scenarios. Do not guess file names or architecture.

Unknown decisions belong in `open_questions`. Omit that block when there are no unknowns.

## Information order

Put information in the order an implementation agent needs it. Use only the relevant parts:

1. goal or target, including exact edit scope when known;
2. contracts not already stated by the target;
3. rules, invariants, states, or flows;
4. ordered work only where dependencies matter;
5. verification commands and observable acceptance;
6. unresolved questions.

Exact scope and verification usually save more agent work than aggressive prose compression.

## Compression rules

- State each fact once.
- Merge duplicate requirements.
- Omit a block when its content is already carried by another line.
- Make acceptance reference existing rule, contract, and verification IDs instead of restating their prose.
- Keep acceptance as references only when the referenced blocks already state the observable behavior.
- Keep reasons only when they change an implementation decision.
- Replace vague verbs such as `improve`, `support`, `handle`, `optimize`, and `fix` with observable outcomes.
- Keep rare but consequential edge cases. Never drop them to make the file shorter.
- Do not encode ordinary requirements as formulas merely to reduce words.
- Prefer an exact source-derived integer formula over a longer prose description when it is easier to execute correctly.

## Examples

Implementation spec:

```txt
spec
target: [src/payments.ts,tests/payments.test.ts] :: make POST /payments idempotent by Idempotency-Key
keep: response schema, auth behavior

rules:
  R1: key scope is user_id
  R2: same user + same key + same body -> replay original status and body
  R3: persist result before returning success

errors:
  E1: missing Idempotency-Key -> 400 idempotency_key_required
  E2: same key + different body -> 409 idempotency_conflict

verify:
  V1: `pnpm test tests/payments.test.ts`

acceptance:
  A1: R1,R2,R3,E1,E2 -> V1
```

Parallel validation map:

```txt
validation:
  retries: int>=0 | ValueError "retries must be non-negative"
  timeout: int>0 | ValueError "timeout must be positive"
```

Implementation plan:

```txt
plan
goal: move session storage from memory to Redis without changing API behavior

facts:
  current store: src/session/memory.ts
  contract tests: tests/session.contract.test.ts

tasks:
  T1: add Redis adapter behind existing SessionStore interface
  T2: run contract tests against memory and Redis adapters
  T3: switch composition root after T2 passes

verify:
  V1: `pnpm test tests/session.contract.test.ts`
```

## Quality gate

Before finishing, verify:

- a new implementation agent can act without this skill or unstated context;
- every line changes scope, behavior, implementation order, or verification;
- no requirement is duplicated or replaced by an undefined abbreviation;
- known files, interfaces, and commands are exact;
- acceptance is observable and traces to the stated contract;
- every field still has the same owner and nesting as in the source;
- the result is shorter by removing redundancy, not by obscuring meaning.

Output the semantic document first. Add separate notes only for unresolved decisions that materially block implementation.
