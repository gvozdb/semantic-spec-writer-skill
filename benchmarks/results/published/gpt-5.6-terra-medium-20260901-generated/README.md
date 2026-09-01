# Published Generated-Spec Benchmark

Model: `gpt-5.6-terra`  
Reasoning effort: `medium`  
Tokenizer: `o200k_base`  
Cases: 8  
Implementation repetitions: 3

The generation run used the repository skill with tree SHA-256
`90a58a0347de13c239558f017123bc020508c2eac1434f8911acfae519d2d7dc`.
That hash matches the skill committed with the benchmark harness.
The context and implementation runs record repository commit
`c9ed7ffe9b5b2dfb96bd4ab6c40cc9994e28641e`.

Contents:

- `generation.json`: every generation attempt, including the rejected first
  invoice attempt and its full token/tool accounting;
- `attempts/`: all generated attempt artifacts;
- `specs/`: the selected generated semantic specifications;
- `context-r3.json`: 48 paired fixed-output, zero-tool context reads;
- `implementation-r3.json`: 48 paired isolated implementation runs;
- `CONTEXT.md`, `IMPLEMENTATION.md`, `LIFECYCLE.md`: rendered reports.

The raw JSON files are authoritative. Reports can be regenerated with the
commands documented in `benchmarks/README.md`.
