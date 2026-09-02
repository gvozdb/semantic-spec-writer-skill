# Repository Execution Packets

Use this mode for a coding handoff when the current repository can be inspected. Its purpose is to reduce the next agent's repository discovery, not merely shorten prose.

## Packet contract

Keep the normal `spec` or `plan` header and add a grounded route:

```txt
spec
goal: make refund creation idempotent by request key

route:
  read: src/refunds/models.py:1-82::class Refund
  read: src/events/publisher.py:20-64::class EventPublisher
  edit: src/refunds/service.py:35-118::def create_refund
    do: claim scoped key before mutation; replay same payload; reject conflicts
  edit: tests/refunds/test_service.py
    do: cover first request,replay,conflict,and no duplicate event
execution: routed read once -> all do -> V1 once -> stop on pass; expand only on contradiction/failure

basis: route-sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

contracts:
  request key scope: account_id
  same key + same payload -> replay original result without another event

verify:
  V1: `pytest -q tests/refunds/test_service.py`
```

Route entries use `repo/relative/path`, optionally followed by a current line range and an exact source anchor:

```txt
path/to/file.py:START-END::exact source text
```

- `read`: contract or dependency code the implementer must inspect. Omit files whose relevant contract is already complete in the packet.
- `edit`: existing files expected to change. Edit files are implicitly readable; do not repeat them under `read`.
- `create`: new files that do not exist yet.
- `do`: the file-owned change. Every `edit` and `create` target requires at least one `do`; move the relevant contract here instead of duplicating it in a separate task block.
- `expand`: a fail-safe, not permission for routine full-repository search. Expand only after a listed file, import, generated file, or verification failure proves the route incomplete.
- `basis`: SHA-256 over the sorted routed paths and complete bytes of every routed existing file; create targets use a `CREATE` marker. It makes stale packets fail before implementation.
- `execution`: the exact bounded downstream loop. Use `routed read once -> all do -> V1 once -> stop on pass; expand only on contradiction/failure`. This prevents routine repository listing, repeated reads, and redundant checks after the declared verification passes.

Use line ranges only for a handoff against the current snapshot. Use a short exact declaration or unique source fragment as the anchor. Do not use a guessed symbol, stale line range, absolute path, directory, glob, or `..` path.

## Workflow

1. Find the actual entrypoint, implementation boundary, dependent contract, and existing narrow verification command.
2. Read only enough code to establish the route and non-obvious contract.
3. Write the packet directly. Preserve behavior and acceptance exactly; do not copy code that the routed slice already exposes. Include the canonical bounded `execution` line.
4. Run `scripts/check_execution_packet.py REPO PACKET --print-basis`, replace the placeholder basis with the printed value, then run the checker normally. When `tiktoken` is available, add `--encoding NAME`. If the user supplied a context limit, add `--max-context-tokens N`.
5. If validation exposes a stale route, missing verification command, or broken anchor, fix it once. Do not broaden it speculatively.

The checker validates containment, route syntax, file-owned actions, file
existence, ranges, unique anchors, canonical duplicate routing, bounded execution,
exact `Vn` command syntax, the stale-route basis, and an optional token budget. It cannot
prove that the selected files are semantically complete; acceptance and the
project-specific verification command remain the correctness gate.

## Economy rules

- Route the smallest complete set, not every related file.
- Compile requirements into file-owned `do` lines. A packet that appends routing metadata to a complete standalone spec wastes tokens and weakens ownership.
- Prefer one narrow source window over a whole large module when the omitted code cannot affect the change.
- Include signatures, state transitions, persistence ordering, and exact errors when they prevent another lookup or wrong implementation.
- Exclude generic style guidance, directory descriptions, and facts visible in the routed lines.
- For a one-off task, do not promise net savings: packet creation has a cost. The advantage compounds when the packet is reviewed, reused, or handed to another execution turn.
