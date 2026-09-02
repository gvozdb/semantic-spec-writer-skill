# Semantic Context Capsule Benchmark

- Run: `20260902T190231Z`
- Provider: `codex`
- Model: `gpt-5.6-terra`
- Reasoning effort: `medium`
- Cases: 3
- Repetitions: 3
- Packet version: 3
- Capsule version: 4
- Telemetry attestation: `none`

## Quality and usage

| Arm | Runs | Task success | Tests | Total tokens | Input | Uncached input | Output | Wall time | Tool calls | Commands | Discovery | Reads | Pre-edit discovery | Pre-edit reads | Pre-edit verify | Verify |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Packet v3 | 9 | 100.00% | 100.00% | 755096 | 733762 | 102210 | 21334 | 529.207s | 36 | 26 | 22 | 22 | 12 | 12 | 0 | 11 |
| Capsule v4 | 9 | 100.00% | 100.00% | 558917 | 546977 | 90785 | 11940 | 450.742s | 25 | 15 | 0 | 0 | 0 | 0 | 0 | 11 |

## Primary comparison: Capsule v4 vs Packet v3

Only jointly successful Packet v3/Capsule v4 pairs contribute to paired totals, medians, and confidence intervals.

| Metric | Packet v3 paired total | Capsule v4 paired total | Capsule delta | Paired median reduction | 95% fixture CI |
|---|---:|---:|---:|---:|---:|
| Total model tokens | 755096.0 | 558917.0 | -25.98% | 35.551% | [20.925%, 40.706%] |
| Input tokens | 733762.0 | 546977.0 | -25.46% | 35.143% | [20.414%, 40.342%] |
| Uncached input | 102210.0 | 90785.0 | -11.18% | 24.896% | [-21.843%, 25.503%] |
| Output tokens | 21334.0 | 11940.0 | -44.03% | 44.575% | [36.574%, 47.812%] |
| Wall time | 529.207 | 450.742 | -14.83% | 30.363% | [26.793%, 42.592%] |
| Tool calls | 36.0 | 25.0 | -30.56% | 40.0% | [25.000%, 50.000%] |
| Shell commands | 26.0 | 15.0 | -42.31% | 50.0% | [33.333%, 50.000%] |
| Discovery command events | 22.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Read command events | 22.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Verification command events | 11.0 | 11.0 | +0.00% | 0.0% | [0.000%, 0.000%] |
| Pre-edit discovery events | 12.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Pre-edit read events | 12.0 | 0.0 | -100.00% | 100.0% | [100.000%, 100.000%] |
| Pre-edit verification events | 0.0 | 0.0 | n/a | n/a | n/a |

Primary comparison coverage: **9/9** jointly successful pairs; **9/9** have paired total-token telemetry; **9/9** have paired uncached-input telemetry.

## All-run deltas

| Metric | Packet v3 total | Capsule v4 total | Capsule delta |
|---|---:|---:|---:|
| Total model tokens | 755096 | 558917 | -25.98% |
| Input tokens | 733762 | 546977 | -25.46% |
| Uncached input | 102210 | 90785 | -11.18% |
| Output tokens | 21334 | 11940 | -44.03% |
| Wall time | 529.207 | 450.742 | -14.83% |
| Tool calls | 36 | 25 | -30.56% |
| Shell commands | 26 | 15 | -42.31% |
| Discovery command events | 22 | 0 | -100.00% |
| Read command events | 22 | 0 | -100.00% |
| Verification command events | 11 | 11 | +0.00% |
| Pre-edit discovery events | 12 | 0 | -100.00% |
| Pre-edit read events | 12 | 0 | -100.00% |
| Pre-edit verification events | 0 | 0 | n/a |

## Input cache break-even

Capsule v4 input is cheaper for every cached-input unit price from 0% to 100% of uncached input. Output-token savings are excluded from this conservative comparison.

## Capsule static size

| Case | Packet v3 bytes | Capsule v4 bytes | Capsule v4 size overhead |
|---|---:|---:|---:|---:|
| `refund-ledger` | 1861 | 3617 | +94.36% |
| `tenant-settings` | 1202 | 2947 | +145.17% |
| `webhook-dispatch` | 1670 | 3665 | +119.46% |
| **Median** |  |  |  | **+119.46%** |

## Verdict

Observed full-run result: Capsule v4 used **25.98% fewer** total model tokens versus Packet v3. The suite cannot establish a product token-saving claim across every fixture because provider telemetry and grades are not independently attested.

## Limits

- Three synthetic Python fixtures are enough to reject a weak design, not enough for a broad product claim.
- Provider token telemetry and grades are self-reported and are not independently attested; the Git commit is the publication trust boundary.
- Command classification is directional telemetry, not a filesystem-access audit. One Codex event may contain multiple or indirectly scripted operations.
- Pre-edit classification is claim-eligible only when file-change event paths confirm a routed or target edit; missing or pathless events fail closed.
- The reported fixture-cluster interval bootstraps per-fixture median reductions; it is not an interval for the displayed all-run aggregate reduction.
- Capsule v4 embeds Packet v3 and its routed source snapshot; this comparison excludes authoring cost and reuse break-even.
- Hidden tests and hidden expected outputs stay outside the solution process; visible smoke assertions are restored from immutable fixtures for every arm.
- Results apply only to the recorded model, reasoning effort, repository shapes, and cache behavior.
