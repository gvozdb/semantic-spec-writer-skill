# Semantic Context Capsule Benchmark

- Run: `20260903T000807Z`
- Provider: `codex`
- Model: `gpt-5.6-terra`
- Reasoning effort: `medium`
- Cases: 3
- Repetitions: 3
- Packet version: 3
- Capsule version: 6
- Execution profile: `capsule-v6-next-action-1`
- Telemetry attestation: `none`

## Quality and usage

| Arm | Runs | Task success | Tests | Total tokens | Input | Uncached input | Output | Wall time | Tool calls | Commands | Discovery | Reads | Pre-edit discovery | Pre-edit reads | Pre-edit verify | Verify |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Packet v3 | 9 | 100.00% | 100.00% | 727365 | 708598 | 105462 | 18767 | 434.703s | 35 | 25 | 21 | 20 | 13 | 12 | 1 | 11 |
| Capsule v6 | 9 | 100.00% | 100.00% | 454192 | 443383 | 85751 | 10809 | 278.693s | 18 | 9 | 0 | 0 | 0 | 0 | 0 | 9 |

Routed action gate: **9/9** Capsule v6 runs.

## Primary comparison: Capsule v6 vs Packet v3

Only jointly successful Packet v3/Capsule v6 pairs contribute to paired totals, medians, and confidence intervals.

| Metric | Packet v3 paired total | Capsule v6 paired total | Capsule delta | Paired median reduction | 95% fixture CI |
|---|---:|---:|---:|---:|---:|
| Total model tokens | 727365.0 | 454192.0 | -37.56% | 38.174% | [23.282%, 38.629%] |
| Input tokens | 708598.0 | 443383.0 | -37.43% | 37.959% | [22.857%, 38.443%] |
| Uncached input | 105462.0 | 85751.0 | -18.69% | 23.907% | [12.936%, 29.428%] |
| Output tokens | 18767.0 | 10809.0 | -42.40% | 42.487% | [36.284%, 44.774%] |
| Wall time | 434.703 | 278.693 | -35.89% | 34.603% | [26.635%, 34.723%] |
| Tool calls | 35.0 | 18.0 | -48.57% | 50.0% | [33.333%, 50.000%] |
| Shell commands | 25.0 | 9.0 | -64.00% | 66.667% | [50.000%, 66.667%] |
| Discovery command events | 21.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Read command events | 20.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Verification command events | 11.0 | 9.0 | -18.18% | 0.0% | [0.000%, 0.000%] |
| Pre-edit discovery events | 13.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Pre-edit read events | 12.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Pre-edit verification events | 1.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |

Primary comparison coverage: **9/9** jointly successful pairs; **9/9** have paired total-token telemetry; **9/9** have paired uncached-input telemetry.

## All-run deltas

| Metric | Packet v3 total | Capsule v6 total | Capsule delta |
|---|---:|---:|---:|
| Total model tokens | 727365 | 454192 | -37.56% |
| Input tokens | 708598 | 443383 | -37.43% |
| Uncached input | 105462 | 85751 | -18.69% |
| Output tokens | 18767 | 10809 | -42.40% |
| Wall time | 434.703 | 278.693 | -35.89% |
| Tool calls | 35 | 18 | -48.57% |
| Shell commands | 25 | 9 | -64.00% |
| Discovery command events | 21 | 0 | -100.00% |
| Read command events | 20 | 0 | -100.00% |
| Verification command events | 11 | 9 | -18.18% |
| Pre-edit discovery events | 13 | 0 | -100.00% |
| Pre-edit read events | 12 | 0 | -100.00% |
| Pre-edit verification events | 1 | 0 | -100.00% |

## Input cache break-even

Capsule v6 input is cheaper for every cached-input unit price from 0% to 100% of uncached input. Output-token savings are excluded from this conservative comparison.

## Capsule static size

| Case | Packet v3 bytes | Capsule v6 bytes | Capsule v6 size overhead |
|---|---:|---:|---:|---:|
| `refund-ledger` | 1861 | 5355 | +187.75% |
| `tenant-settings` | 1202 | 4173 | +247.17% |
| `webhook-dispatch` | 1670 | 5385 | +222.46% |
| **Median** |  |  |  | **+222.46%** |

## Verdict

This suite supports a measured token-saving result for Capsule v6 versus Packet v3 on this corpus while preserving behavior in every fixture. Provider telemetry remains self-reported; this is not a universal guarantee.

## Limits

- Three synthetic Python fixtures are enough to reject a weak design, not enough for a broad product claim.
- Provider token telemetry and grades are self-reported and are not independently attested; the Git commit is the publication trust boundary.
- Command classification is directional telemetry, not a filesystem-access audit. One Codex event may contain multiple or indirectly scripted operations.
- Pre-edit classification is claim-eligible only when file-change event paths confirm a routed or target edit; missing or pathless events fail closed.
- The reported fixture-cluster interval bootstraps per-fixture median reductions; it is not an interval for the displayed all-run aggregate reduction.
- Capsule v6 embeds Packet v3 and its routed source snapshot; this comparison excludes authoring cost and reuse break-even.
- Static Capsule size excludes the host prompt; measured model usage includes the complete rendered prompt.
- Hidden tests and hidden expected outputs stay outside the solution process; visible smoke assertions are restored from immutable fixtures for every arm.
- Results apply only to the recorded model, reasoning effort, repository shapes, and cache behavior.
