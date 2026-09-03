# Ordinary Markdown vs Capsule v6 Full-Lifecycle Benchmark

- Run: `20260903T102315Z`
- Provider/model: `codex` / `gpt-5.6-terra`
- Reasoning effort: `medium`
- Cases: 3
- Implementation repetitions: 3
- Authoring attempts: up to 2 per fixture; all calls charged
- Claim protocol: `markdown-capsule-full-lifecycle-dominance-v2`
- Preregistration SHA-256: `2e3ce5b0c4d3a2e3f0d616487593faf23226f03c97ef75f81a7c84edcf59d8b1`
- Markdown baseline: source task used directly, with zero preparation model calls
- Capsule workflow: source task -> bounded skill authoring -> deterministic sealed context -> implementation

> Non-publishable run. The preregistered full-lifecycle dominance gate did not pass.

## Quality

Authored and compiled Capsule handoffs: **3/3** using **5** model call(s).  
Capsule routed action gate: **6/9**.

| Workflow | Successful implementations | Tests | Acceptance |
|---|---:|---:|---:|
| Ordinary Markdown | 6/9 | 94.67% | 87.88% |
| Capsule v6 | 6/9 | 96.00% | 90.91% |

## Token bill

Authoring is charged only to Capsule. Markdown receives the complete source task directly, which favors the baseline.

| Stage | Ordinary Markdown | Capsule v6 | Capsule delta |
|---|---:|---:|---:|
| Authoring (3 artifacts / 5 calls) | 0 | 1454228 | n/a |
| Implementation (9 runs/arm) | 1222545 | 631763 | -48.32% |
| Measured workload: each artifact reused 3 times | 1222545 | 2085991 | +70.63% |

## One-use lifecycle

Every Capsule pair below includes the full authoring cost for that fixture plus one implementation. Failed Markdown implementations remain in this primary comparison.

| Metric | Markdown average/task | Capsule average/task | Capsule reduction | Pair wins | 95% fixture CI |
|---|---:|---:|---:|---:|---:|
| Total model tokens | 135838.333 | 554938.556 | -308.53% | 0/9 | [-358.964%, -277.637%] |
| Input tokens | 132804.778 | 545657.333 | -310.87% | 0/9 | [-363.066%, -280.310%] |
| Uncached input | 20790.556 | 80903.556 | -289.14% | 0/9 | [-471.720%, -141.305%] |
| Output tokens | 3033.556 | 9281.222 | -205.95% | 0/9 | [-274.524%, -150.257%] |
| Agent wall time | 74.067s | 226.107s | -205.27% | 0/9 | [-234.544%, -143.229%] |
| Tool calls | 6.778 | 21 | -209.84% | 0/9 | [-250.000%, -177.778%] |
| Shell commands | 5.667 | 14.667 | -158.82% | 0/9 | [-200.000%, -140.000%] |

One-use total-token result: **-308.53% fewer** tokens; median reduction **-292.62%**; fixture-cluster 95% CI **[-358.964%, -277.637%]**.

Strict equal-success coverage: **6/9** pairs. This is secondary; the primary outcome comparison retains failed baseline attempts.

## Reuse

| Full-corpus uses | Markdown tokens | Capsule tokens including authoring | Capsule delta |
|---:|---:|---:|---:|
| 1 | 407515 | 1664816 | +308.53% |
| 3 | 1222545 | 2085991 | +70.63% |
| 5 | 2037575 | 2507166 | +23.05% |
| 10 | 4075150 | 3560105 | -12.64% |

Measured 3-use workload: **70.63% more** total model tokens. Break-even: **8 full-corpus uses**.

## Static size

| Case | Ordinary Markdown bytes | Capsule v6 bytes | Capsule v6 size overhead |
|---|---:|---:|---:|---:|
| `refund-ledger` | 2511 | 5979 | +138.11% |
| `tenant-settings` | 1793 | 5486 | +205.97% |
| `webhook-dispatch` | 2100 | 6581 | +213.38% |
| **Median** |  |  |  | **+205.97%** |

## Verdict

Claim rejected: the full authoring and implementation provenance is not credible; Capsule v6 did not pass every test without losing pairwise quality; Capsule v6 used fewer one-use lifecycle tokens in only 0/9 pairs; the one-use lifecycle token 95% fixture CI is not above 0%; Capsule did not reduce measured three-use lifecycle tokens; the routed action gate passed only 6/9 Capsule runs; recorded implementation errors occurred.

## Limits

- Three synthetic multi-file Python fixtures can validate this workflow, not establish a universal model-independent claim.
- There is one bounded authoring process per fixture (up to 2 attempts); implementation is repeated 3 times per arm.
- Provider token and agent-duration telemetry are self-reported. Deterministic Capsule compilation uses no model tokens and is excluded from agent wall time.
- Capsule static bytes can exceed Markdown because it embeds routed source. This benchmark tests total task execution, not file compression.
- Hidden tests remain outside authoring and implementation workspaces. Visible checks are restored from immutable fixtures.
- Results apply only to the recorded model, reasoning effort, fixtures, and cache behavior.
