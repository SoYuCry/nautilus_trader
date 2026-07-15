# depth_imbalance_1：10 个 clean 天气 event 小批量测试

> 研究性因子检验，不是策略回测；不包含成交、queue、fee 或 PnL。每个二元 market 只取 YES token，避免 YES/NO 机械互补造成伪重复样本。

## 样本

| event | source rows | elapsed | failures |
| --- | ---: | ---: | ---: |
| `highest-temperature-in-chicago-on-june-4-2026` | 17,690 | 7.2s | 0 |
| `highest-temperature-in-denver-on-june-4-2026` | 44,972 | 14.9s | 0 |
| `highest-temperature-in-lucknow-on-june-4-2026` | 390,637 | 109.8s | 0 |
| `highest-temperature-in-ankara-on-june-4-2026` | 406,524 | 109.8s | 0 |
| `highest-temperature-in-mexico-city-on-june-4-2026` | 438,623 | 86.4s | 0 |
| `highest-temperature-in-sao-paulo-on-june-4-2026` | 456,368 | 83.9s | 0 |
| `highest-temperature-in-panama-city-on-june-4-2026` | 527,161 | 100.8s | 0 |
| `highest-temperature-in-cape-town-on-june-4-2026` | 534,214 | 112.2s | 0 |
| `highest-temperature-in-hong-kong-on-june-4-2026` | 536,532 | 120.1s | 0 |
| `highest-temperature-in-seattle-on-june-4-2026` | 570,656 | 115.0s | 0 |

## Event-equal 结果

| horizon | events with IC | median event IC | positive event share | median hit rate | median top-bottom |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 30s | 10 | 0.0868 | 0.900 | 0.573 | 0.00075 |
| 120s | 10 | 0.1115 | 0.900 | 0.570 | 0.00121 |
| 600s | 10 | 0.1326 | 1.000 | 0.597 | 0.00205 |

## 运行说明

- 因子：`depth_imbalance_1`。
- horizons：30s, 120s, 600s。
- event 内 token 等权；headline 再按 event 等权。
- 样本按严格 clean 条件筛选后取 source rows 最少的 10 个 event，因此这是快速 smoke batch，不是随机样本或确认集。
- 各 event CPU wall-time 合计：860.2s；并行后的实际总耗时见 `run_summary.json`。
