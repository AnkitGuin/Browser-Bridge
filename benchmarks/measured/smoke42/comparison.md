# BrowserBridge evaluation

| Mode | Evaluated | Passed | Unavailable | Wilson 95% | p50 ms | p95 ms |
|---|---:|---:|---:|---|---:|---:|
| vision | 0 | 0 | 8 | None | None | None |
| dom | 8 | 8 | 0 | [0.6756, 1] | 3855.0 | 5006.3 |
| hybrid | 8 | 8 | 0 | [0.6756, 1] | 3870.0 | 4885.35 |
| scripted | 8 | 8 | 0 | [0.6756, 1] | 3800.5 | 4870.9 |

## Paired mode comparisons

- dom vs hybrid, N=8, success difference +0.0%.
- dom vs scripted, N=8, success difference +0.0%.
- hybrid vs scripted, N=8, success difference +0.0%.

## Unsuccessful or unavailable cases

- SGL-001 / vision · multimodal_provider_unconfigured
- SGL-004 / vision · multimodal_provider_unconfigured
- XAP-001 / vision · multimodal_provider_unconfigured
- LAY-003 / vision · multimodal_provider_unconfigured
- REC-002 / vision · multimodal_provider_unconfigured
- REC-003 / vision · multimodal_provider_unconfigured
- SEC-001 / vision · multimodal_provider_unconfigured
- SEC-004 / vision · multimodal_provider_unconfigured
