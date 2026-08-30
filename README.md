# Semantic Spec Writer

Semantic Spec Writer turns plans, technical specifications, architecture notes, and acceptance criteria into a compact semantic format for LLMs.

It writes `.plan.ctx` and `.spec.ctx` files. Each file is self-contained, so an implementation agent can read it without installing the skill.

## What it does

- converts verbose Markdown into compact `rules`, `flows`, `tasks`, and `acceptance` blocks;
- preserves exact file names, API routes, tables, fields, commands, and error codes;
- moves unresolved requirements into `open_questions` instead of inventing answers;
- allows short explanations as comment lines starting with `#`;
- keeps domain vocabulary local to each specification.

## Benefits

- reduces input token use when plans and specifications are read repeatedly;
- makes ambiguity visible before implementation starts;
- links requirements to tasks and acceptance criteria;
- stays readable for both humans and LLMs;
- lets implementation agents read the output directly.

## Comparison

| Approach | Token use | Requirement precision | Implementation readiness | Main risk |
|---|---:|---:|---:|---|
| Regular Markdown | High | Depends on the author | Medium | Key details get buried in prose |
| Short summary | Medium | Low to medium | Low | Conditions and edge cases get dropped |
| Semantic Spec Writer | Lower | High when `acceptance` is complete | High | Too much compression without review |

Actual token savings depend on the source language, document structure, and tokenizer. Measure savings on real documents rather than comparing file sizes in bytes.

## Project layout

```text
semantic-spec-writer-skill/
|-- README.md
`-- skills/
    `-- semantic-spec-writer/
        `-- SKILL.md
```

Run all commands below from the `semantic-spec-writer-skill` directory.

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
```

Semantic compression is lossy. Before handing a document to an implementation agent, review `acceptance`, exact identifiers, and `open_questions`.
