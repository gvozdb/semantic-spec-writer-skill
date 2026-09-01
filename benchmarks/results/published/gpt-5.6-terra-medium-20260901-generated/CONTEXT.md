# Semantic Spec Writer Context Benchmark

Run: `20260901T190859Z`  
Provider: `codex`  
Model: `gpt-5.6-terra`  
Reasoning effort: `medium`  
Cases: 8  
Repetitions: 3  
Semantic source: `generated`

## Results

Every run loads one document, performs no implementation, uses no tools, and returns the same fixed output.

| Variant | Successful reads | Median input | Median uncached input | Median output | Total input | Total uncached input |
|---|---:|---:|---:|---:|---:|---:|
| Markdown | 24/24 | 14602.0 | 4618.0 | 5.0 | 350306 | 110690 |
| Semantic | 24/24 | 14492.5 | 4508.5 | 5.0 | 347858 | 108242 |

Semantic input saved across the run: **2448 tokens**.

Semantic input saved per complete corpus read: **816.0 tokens**.

Median paired input-token reduction: **0.794%**.

Fixture-cluster bootstrap 95% CI: **[0.185%, 0.949%]**.

| Case | Median input-token reduction | Median uncached-input reduction |
|---|---:|---:|
| `cursor-pagination` | 0.824% | 2.617% |
| `email-routing` | 1.039% | 3.273% |
| `feature-flags` | 0.936% | 2.945% |
| `inventory-reservation` | 0.185% | 0.585% |
| `invoice-totals` | 0.096% | 0.302% |
| `order-transitions` | 0.949% | 2.985% |
| `retry-schedule` | 0.763% | 2.435% |
| `webhook-validation` | 0.794% | 2.512% |

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

This run supports a context-loading token reduction for this corpus and setup. It does not imply that a complete implementation run will use fewer tokens.

## Limits

- Shared system and tool instructions remain in both variants; paired deltas isolate the document contribution under this setup.
- Fixed-output reads measure context loading, not comprehension or implementation quality.
- Implementation quality and full-loop usage must be measured by the separate implementation benchmark.
- Results apply to this corpus, model, and reasoning effort.
