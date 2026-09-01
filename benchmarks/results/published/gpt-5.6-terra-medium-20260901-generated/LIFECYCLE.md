# Semantic Spec Writer Lifecycle Benchmark

Generation run: `20260901T184036Z`  
Implementation run: `20260901T191440Z`  
Generation model: `gpt-5.6-terra`  
Implementation model: `gpt-5.6-terra`  
Cases: 8  
Implementation repetitions: 3

## Quality

Generated valid specs: **8/8**

| Variant | Task success | Acceptance pass rate |
|---|---:|---:|
| Markdown | 100.00% | 100.00% |
| Generated semantic | 100.00% | 100.00% |

## Token lifecycle

Per-reuse values cover one implementation of every case. Authoring creates one semantic spec per case.

| Metric | Authoring | Markdown per reuse | Semantic per reuse | Break-even reuse |
|---|---:|---:|---:|---:|
| Input tokens | 1100282.0 | 946275.7 | 858364.0 | 13 |
| Uncached input tokens | 139002.0 | 116921.0 | 108369.3 | 17 |
| Output tokens | 18562.0 | 22219.0 | 20921.7 | 15 |

## Reuse scenarios

Positive semantic delta means generated semantic specs used more tokens, including authoring.

| Reuses | Metric | Markdown lifecycle | Semantic lifecycle | Semantic delta |
|---:|---|---:|---:|---:|
| 1 | Input tokens | 946275.7 | 1958646.0 | +106.985% |
| 1 | Uncached input tokens | 116921.0 | 247371.3 | +111.571% |
| 1 | Output tokens | 22219.0 | 39483.7 | +77.702% |
| 5 | Input tokens | 4731378.3 | 5392102.0 | +13.965% |
| 5 | Uncached input tokens | 584605.0 | 680848.7 | +16.463% |
| 5 | Output tokens | 111095.0 | 123170.3 | +10.869% |
| 10 | Input tokens | 9462756.7 | 9683922.0 | +2.337% |
| 10 | Uncached input tokens | 1169210.0 | 1222695.3 | +4.574% |
| 10 | Output tokens | 222190.0 | 227778.7 | +2.515% |
| 25 | Input tokens | 23656891.7 | 22559382.0 | -4.639% |
| 25 | Uncached input tokens | 2923025.0 | 2848235.3 | -2.559% |
| 25 | Output tokens | 555475.0 | 541603.7 | -2.497% |

## Generated document size

| Case | Baseline tokens | Semantic tokens | Token reduction | Byte reduction | Word reduction |
|---|---:|---:|---:|---:|---:|
| `cursor-pagination` | 420 | 300 | 28.57% | 34.22% | 48.87% |
| `email-routing` | 485 | 333 | 31.34% | 30.48% | 41.20% |
| `feature-flags` | 486 | 349 | 28.19% | 37.11% | 44.41% |
| `inventory-reservation` | 440 | 413 | 6.14% | 5.63% | 18.51% |
| `invoice-totals` | 459 | 445 | 3.05% | 14.82% | 38.14% |
| `order-transitions` | 490 | 351 | 28.37% | 25.47% | 39.04% |
| `retry-schedule` | 385 | 274 | 28.83% | 32.15% | 42.31% |
| `webhook-validation` | 452 | 336 | 25.66% | 31.33% | 39.60% |
| **Median** |  |  | **28.28%** | **30.91%** | **40.40%** |

Tokenizer: `o200k_base`.

## Interpretation

This report separates the one-time cost of creating semantic specs from the cost of implementing them repeatedly. Measured quality was preserved. Break-even exists only for metrics where per-reuse savings repay the complete recorded authoring cost.

## Limitations

- One generated artifact per case is reused across implementation repetitions.
- Provider token totals include the complete agent loop and may vary between runs.
- Input, cached input, and output have different prices; token counts are not a cost estimate.
- The benchmark still needs realistic multi-file fixtures before supporting a broad product claim.
