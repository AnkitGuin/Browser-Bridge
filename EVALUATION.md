# Evaluation

The measured runs used Python 3.12, PostgreSQL 18.6, Redis 8.2.3 and headless Chromium 153 on x86-64. Both suites used seed 42 and a fresh synthetic tenant per case and mode.

## Smoke comparison

| Mode | Passed | Evaluated | Unavailable | Wall time p95 |
|---|---:|---:|---:|---:|
| Scripted | 8 | 8 | 0 | 4.87 s |
| DOM | 8 | 8 | 0 | 5.01 s |
| Hybrid, without a provider | 8 | 8 | 0 | 4.89 s |
| Vision | — | 0 | 8 | — |

The 95% Wilson interval for each evaluated smoke mode is 67.6%–100%. These eight cases provide a functional comparison under matched fixtures. A live multimodal provider is needed for a visual comparison.

## Full 120-case run

| Measure | Hybrid without a provider |
|---|---:|
| Grader success | 115 / 120 (95.8%) |
| 95% Wilson interval | 90.6%–98.2% |
| Development split | 34 / 35 |
| Repository-held-out split | 81 / 85 |
| Recovery opportunities completed | 11 / 11 |
| Unauthorized effects | 0 / 15 forbidden-action opportunities |
| Duplicate effects | 0 / 324 committed effects |
| Verified claims | 324 / 324 claimed mutations |
| Unintended state changes | 0 / 120 runs |
| Wall time p50 / p95 | 2.86 s / 5.89 s |
| Browser actions p50 / p95 | 25 / 40 |

All five failures used the masked accessible-label variant. The credential-free agent could not ground the escalation control and left the intended writes uncommitted. The report includes each failed case and all grading predicates.

- [Smoke JSON](benchmarks/measured/smoke42/results.json) · [CSV](benchmarks/measured/smoke42/cases.csv) · [Summary](benchmarks/measured/smoke42/comparison.md)
- [Full Hybrid JSON](benchmarks/measured/local120/results.json) · [CSV](benchmarks/measured/local120/cases.csv) · [Summary](benchmarks/measured/local120/comparison.md)

See [evaluation methodology](docs/evaluation-methodology.md) for denominators, split construction, fault grading and the scope of the perception comparison.
