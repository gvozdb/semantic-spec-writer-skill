# Semantic Spec Writer Benchmark

Run: `20260901T191440Z`  
Provider: `codex`  
Model: `gpt-5.6-terra`  
Reasoning effort: `medium`  
Cases: 8  
Repetitions: 3  
Semantic source: `generated`

## Implementation results

| Variant | Runs | Test pass rate | Acceptance pass rate | Task success | Median input tokens | Median uncached input | Median output tokens | Median duration | Median tool calls | Estimated cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Markdown | 24 | 100.00% | 100.00% | 100.00% | 95174.0 | 14458.0 | 2489.5 | 60.716s | 5.0 | n/a |
| Semantic | 24 | 100.00% | 100.00% | 100.00% | 95748.5 | 12477.0 | 2687.0 | 62.637s | 4.0 | n/a |

Median paired input-token reduction: **1.073%**

Median paired uncached-input reduction: **8.589%**

## Corpus totals

Positive delta means the semantic variant used more of the metric.

| Metric | Markdown | Semantic | Semantic delta |
|---|---:|---:|---:|
| Input tokens | 2838827 | 2575092 | -9.290% |
| Uncached input tokens | 350763 | 325108 | -7.314% |
| Output tokens | 66657 | 62765 | -5.839% |
| Agent wall time | 1605.786s | 1521.454s | -5.252% |

## Paired variability

Input-token pairs: semantic lower in 16/24, higher in 8/24; range -111.721% to 52.732%.

Fixture-cluster bootstrap 95% CI for median input-token reduction: **[0.311%, 36.472%]**.

Uncached-input pairs: semantic lower in 15/24, higher in 9/24; 95% CI **[-37.667%, 26.24%]**.

| Case | Median input-token reduction | Median uncached-input reduction |
|---|---:|---:|
| `cursor-pagination` | 1.131% | 18.369% |
| `email-routing` | 1.015% | 8.120% |
| `feature-flags` | 36.472% | 31.111% |
| `inventory-reservation` | 2.152% | -50.514% |
| `invoice-totals` | -0.654% | -37.667% |
| `order-transitions` | 0.311% | 2.812% |
| `retry-schedule` | 17.366% | 34.111% |
| `webhook-validation` | 40.480% | -3.919% |

## Static document size

| Case | Baseline bytes | Semantic bytes | Byte reduction | Word reduction |
|---|---:|---:|---:|---:|
| `cursor-pagination` | 1923 | 1265 | 34.22% | 48.87% |
| `email-routing` | 2021 | 1405 | 30.48% | 41.20% |
| `feature-flags` | 2129 | 1339 | 37.11% | 44.41% |
| `inventory-reservation` | 1989 | 1877 | 5.63% | 18.51% |
| `invoice-totals` | 1936 | 1649 | 14.82% | 38.14% |
| `order-transitions` | 2030 | 1513 | 25.47% | 39.04% |
| `retry-schedule` | 1695 | 1150 | 32.15% | 42.31% |
| `webhook-validation` | 2065 | 1418 | 31.33% | 39.60% |
| **Median** |  |  | **30.91%** | **40.40%** |

## Interpretation

Static size reduction proves only that the document is shorter. Acceptance results test whether implementation-relevant behavior survived compression. Provider token usage includes the full agent loop, not only the specification text. This corpus shows a statistically supported reduction in total input tokens, but not in uncached input tokens. Cached and uncached usage must not be presented as the same cost result.

## Limitations

- Small, synthetic Python fixtures are not representative of every codebase.
- Results cover one model (`gpt-5.6-terra`) and one reasoning effort (`medium`).
- The benchmark excludes the one-time cost of creating or reviewing a semantic spec.
- Acceptance tests are held outside the agent workspace, but the runner does not use a container and therefore cannot prove oracle isolation against a hostile agent.
- More fixtures, models, and repetitions are required before making a general token or latency claim.
