---
name: semantic-spec-writer
description: Write or rewrite plans, technical specs, product specs, architecture notes, implementation plans, and acceptance criteria as compact semantic specifications for LLM implementation. Use when a user asks to create, compress, formalize, structure, or convert planning/technical Markdown into a token-efficient, implementation-ready format.
---

# Semantic Spec Writer

Write plans and technical specs as compact semantic specs. The output must be readable by an implementation agent without this skill.

## Core rules

- First line must be `plan` or `spec`.
- Use `.plan.ctx` for plans and `.spec.ctx` for specs unless the user gives another file convention.
- Keep the file self-describing: define local terms before using abbreviations.
- Use only blocks that are useful for the current doc. Do not force domain-specific blocks.
- Create custom blocks when the task needs them. Keep names obvious.
- Prefer compact semantic structure over prose.
- Preserve meaning. Put unknowns in `open_questions`, not in invented requirements.
- Use the user's language for human text. Keep compact keys in English when useful.
- Keep the first line plain: `plan` or `spec`. Do not add suffixes or meta-rules for the language itself.

## Comments

Free-form text is allowed only in the rare case where fields cannot preserve critical nuance.

Rules:

- Start every free-form line with `#`.
- Put the comment on a new line under the exact item it explains.
- Keep it short: 1 to 3 lines.
- Do not use comments instead of `rules`, `flows`, `tasks`, `acceptance`, or `open_questions`.

Example:

```txt
R4: Payment.status=settled -> set Invoice.status=paid
# "settled" means bank settlement confirmed, not payment intent created.
```

## Minimal syntax

Use readable, obvious DSL. Do not rely on hidden semantics.

Common patterns:

```txt
!X                    false or missing
X=V                   equals
X!=V                  not equals
X in [A,B]            one of
A AND B               both
A OR B                either
condition -> action   rule
R1? stop              if R1 matches, stop flow
```

Examples:

```txt
R1: !U.auth -> deny 401
R2: U.role!=admin -> deny 403
R3: U.auth AND U.role=admin -> allow /admin
```

Use block form only when one line is not enough:

```txt
R4:
  when: Payment.status=settled
  then:
    - set Invoice.status=paid
    - emit invoice.paid
  src: S2.P4
```

## IDs and tags

Use stable IDs where references matter:

```txt
G1 goal
NG1 non-goal
T1 task
R1 rule
F1 flow
A1 acceptance
Q1 open question
```

Optional tags are allowed when they reduce ambiguity:

```txt
R1 [must,p0]: !U.auth -> deny 401
Q1 [blocking]: auth method unknown
T3 [risk]: migrate existing sessions
```

## Terms

Declare local terms and abbreviations near the top.

```txt
terms:
  U: User
  P: Project
  owner: U.id=P.owner_id
```

If a domain needs special concepts, define them here or in an obvious custom block. Do not bake every possible domain concept into the language.

## Plan shape

Use this for implementation plans, refactors, migrations, roadmaps, and task breakdowns.

```txt
plan
meta:
  file: feature.plan.ctx
  title:
  status: draft
  source:

terms:

goal:
  G1:

non_goals:
  NG1:

facts:
  C1:

assumptions:
  AS1:

tasks:
  T1:
    do:
    depends_on:
    files:
    risk:
    verify:

order:
  - T1

acceptance:
  A1:

open_questions:
  Q1:
```

## Spec shape

Use this for feature specs, product specs, technical specs, architecture notes, and behavior contracts.

```txt
spec
meta:
  file: feature.spec.ctx
  title:
  status: draft
  source:

terms:

goal:
  G1:

non_goals:
  NG1:

entities:
  E1 User:
    id:
    email:

rules:
  R1: !U.auth -> deny 401

flows:
  F1 login:
    1: validate input.email,input.password
    2: R1? stop
    3: create Session
    4: return Session

tasks:
  T1:
    do:
    verify:

acceptance:
  A1:
    given:
    when:
    then:

open_questions:
  Q1:
```

## Optional patterns

Use these only when the content needs them.

```txt
inv:
  INV1: User.email unique

pre:
  PRE1: User.exists AND !Session.exists

post:
  POST1: Session.user_id=User.id

state:
  Order:
    draft -> paid: payment.settled
    paid -> refunded: refund.done

trace:
  R1 -> T2 -> A1
```

Custom examples:

```txt
api:
  POST /login:
    input: email,password
    ok: Session
    err: 400,401

db:
  users.email: unique index

events:
  user.logged_in:
    after: Session created
```

## Conversion workflow

When converting Markdown into `.ctx`:

1. Keep only meaning-bearing content.
2. Convert requirements into `rules`, `flows`, `tasks`, `acceptance`, or custom blocks.
3. Merge duplicates.
4. Keep exact names for files, APIs, DB tables, fields, env vars, roles, statuses, routes, commands, and error codes.
5. Put unclear parts into `open_questions`.
6. Use `#` comments only for critical nuance that cannot fit into fields.

## Quality gate

Before final output, check:

- First line is `plan` or `spec`.
- No useless mandatory blocks.
- Terms are defined before abbreviations.
- Functional behavior appears as rules, flows, tasks, acceptance, or custom blocks.
- `acceptance` exists for implementation-facing docs.
- `open_questions` exists, even if empty: `open_questions: []`.
- Free text appears only as short `#` comments.
- Vague words like `improve`, `support`, `handle`, `optimize`, `fix` are replaced with observable behavior or moved to `open_questions`.

## Response style

Output the semantic spec first. Add notes only when assumptions or open questions materially affect implementation.
