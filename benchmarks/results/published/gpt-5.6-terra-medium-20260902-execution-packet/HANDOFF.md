# Execution Packet Benchmark

- Run: `20260902T003708Z`
- Provider: `codex`
- Model: `gpt-5.6-terra`
- Reasoning effort: `medium`
- Cases: 3
- Repetitions: 3

## Quality and usage

| Arm | Runs | Task success | Tests | Input tokens | Uncached input | Output tokens | Wall time | Discovery | Reads | Verify | Commands | Tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Markdown | 9 | 66.67% | 92.00% | 1237706 | 177866 | 32130 | 724.758s | 13 | 22 | 21 | 51 | 61 |
| Semantic v1 | 9 | 66.67% | 94.67% | 708429 | 120653 | 19509 | 462.362s | 11 | 14 | 10 | 25 | 35 |
| Packet v3 | 9 | 100.00% | 100.00% | 690958 | 105998 | 19233 | 451.832s | 12 | 13 | 10 | 25 | 35 |

## Primary comparison: Packet v3 vs Semantic v1

| Metric | Semantic v1 | Packet v3 | Packet delta | Paired median reduction | 95% fixture CI |
|---|---:|---:|---:|---:|---:|
| Input tokens | 465099.0 | 447476.0 | -3.79% | -0.364% | [-24.893%, 0.566%] |
| Uncached input | 82379.0 | 70644.0 | -14.25% | 8.356% | [-7.717%, 31.202%] |
| Shell commands | 16.0 | 16.0 | +0.00% | 0.0% | [-50.000%, 0.000%] |
| Discovery command events | 7.0 | 6.0 | -14.29% | 0.0% | [0.000%, 0.000%] |
| Read command events | 8.0 | 7.0 | -12.50% | 0.0% | [0.000%, 0.000%] |
| Verification command events | 7.0 | 7.0 | +0.00% | 0.0% | [0.000%, 0.000%] |

Packet used fewer uncached-input tokens in **3/6** paired runs.
Primary comparison coverage: **6/9** jointly successful pairs.
Only pairs where both Semantic v1 and Packet v3 passed every acceptance group are included in the primary usage comparison.

## Static artifacts

| Case | Markdown bytes | Semantic v1 bytes | Packet v3 bytes | Packet vs v1 |
|---|---:|---:|---:|---:|
| `refund-ledger` | 2511 | 1994 | 1861 | +6.67% |
| `tenant-settings` | 1793 | 1289 | 1202 | +6.75% |
| `webhook-dispatch` | 2100 | 1728 | 1670 | +3.36% |
| **Median** |  |  |  | **+6.67%** |

## Verdict

Packet v3 improved measured task success (9/9 versus 6/9) and used 12.15% fewer uncached-input tokens across all runs. The strict equal-success token comparison covered only 6/9 pairs, so this establishes a quality gain for this suite. The all-run token reduction is an observed result, not an isolated or model-independent token-saving claim.

## Limits

- Three synthetic Python fixtures are enough to reject a weak design, not enough for a broad product claim.
- Command executions are coarse Codex events; one event may contain multiple shell operations.
- Curated artifacts isolate execution behavior but exclude packet-authoring cost and reuse break-even.
- Hidden tests and hidden expected outputs stay outside the solution process; visible smoke assertions are restored from immutable fixtures for every arm. Implementation agents use workspace-write; hidden solution calls use a network-disabled read-only sandbox. This is not a full VM boundary.
- Results apply only to the recorded model, reasoning effort, repository shapes, and cache behavior.
