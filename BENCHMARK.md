# Semantic Spec Writer Benchmark

Run: `20260901T122611Z`  
Provider: `codex`  
Model: `gpt-5.6-terra`  
Reasoning effort: `medium`  
Cases: 8  
Repetitions: 3

## Implementation results

| Variant | Runs | Test pass rate | Acceptance pass rate | Task success | Median input tokens | Median uncached input | Median output tokens | Median duration | Median tool calls | Estimated cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Markdown | 24 | 100.00% | 100.00% | 100.00% | 94608.5 | 14659.5 | 2582.0 | 62.794s | 5.0 | n/a |
| Semantic | 24 | 100.00% | 100.00% | 100.00% | 94403.5 | 14790.0 | 2653.5 | 64.291s | 5.0 | n/a |

Median paired input-token reduction: **0.732%**

Median paired uncached-input reduction: **0.994%**

## Corpus totals

Positive delta means the semantic variant used more of the metric.

| Metric | Markdown | Semantic | Semantic delta |
|---|---:|---:|---:|
| Input tokens | 2478823 | 2616753 | +5.564% |
| Uncached input tokens | 360679 | 365745 | +1.405% |
| Output tokens | 64056 | 68064 | +6.257% |
| Agent wall time | 1570.419s | 1667.659s | +6.192% |

## Paired variability

Input-token pairs: semantic lower in 13/24, higher in 11/24; range -73.773% to 41.286%.

Fixture-cluster bootstrap 95% CI for median input-token reduction: **[-19.267%, 2.185%]**.

Uncached-input pairs: semantic lower in 13/24, higher in 11/24; 95% CI **[-30.746%, 13.811%]**.

| Case | Median input-token reduction | Median uncached-input reduction |
|---|---:|---:|
| `cursor-pagination` | -14.106% | -10.791% |
| `email-routing` | 2.185% | 29.252% |
| `feature-flags` | 0.815% | 7.282% |
| `inventory-reservation` | -19.267% | -60.766% |
| `invoice-totals` | 2.677% | -11.279% |
| `order-transitions` | 1.623% | 1.641% |
| `retry-schedule` | 1.079% | -30.746% |
| `webhook-validation` | -43.195% | 13.811% |

## Static document size

| Case | Baseline bytes | Semantic bytes | Byte reduction | Word reduction |
|---|---:|---:|---:|---:|
| `cursor-pagination` | 1923 | 1320 | 31.36% | 44.01% |
| `email-routing` | 2021 | 1364 | 32.51% | 46.84% |
| `feature-flags` | 2129 | 1231 | 42.18% | 53.87% |
| `inventory-reservation` | 1989 | 1615 | 18.80% | 28.11% |
| `invoice-totals` | 1936 | 1603 | 17.20% | 37.46% |
| `order-transitions` | 2030 | 1637 | 19.36% | 39.04% |
| `retry-schedule` | 1695 | 1035 | 38.94% | 48.46% |
| `webhook-validation` | 2065 | 1355 | 34.38% | 49.33% |
| **Median** |  |  | **31.93%** | **45.42%** |

## Interpretation

Static size reduction proves only that the document is shorter. Acceptance results test whether implementation-relevant behavior survived compression. Provider token usage includes the full agent loop, not only the specification text. This run preserves all tested behavior but does not demonstrate an end-to-end token saving when corpus totals and the confidence interval are considered.

## Limitations

- Eight small, synthetic Python fixtures are not representative of every codebase.
- Results cover one model (`gpt-5.6-terra`) and one reasoning effort (`medium`).
- The benchmark excludes the one-time cost of creating or reviewing a semantic spec.
- Acceptance tests are held outside the agent workspace, but the runner does not use a container and therefore cannot prove oracle isolation against a hostile agent.
- More fixtures, models, and repetitions are required before making a general token or latency claim.
