# depth_imbalance_1：10 个 clean 天气 event 小批量测试

> 研究性因子检验，不是策略回测；不包含成交、queue、fee 或 PnL。每个二元 market 只取 YES token，避免 YES/NO 机械互补造成伪重复样本。

## 样本

| event | source rows | elapsed | failures |
| --- | ---: | ---: | ---: |
| `highest-temperature-in-karachi-on-june-6-2026` | 1,115,044 | 257.1s | 0 |
| `highest-temperature-in-beijing-on-june-6-2026` | 1,198,718 | 252.1s | 0 |
| `highest-temperature-in-busan-on-june-6-2026` | 1,279,940 | 284.5s | 0 |
| `highest-temperature-in-qingdao-on-june-6-2026` | 1,346,866 | 288.7s | 0 |
| `highest-temperature-in-karachi-on-june-7-2026` | 1,471,986 | 190.6s | 0 |
| `highest-temperature-in-taipei-on-june-6-2026` | 1,484,448 | 203.9s | 0 |
| `highest-temperature-in-shenzhen-on-june-6-2026` | 1,491,905 | 197.2s | 0 |
| `highest-temperature-in-jeddah-on-june-10-2026` | 1,525,174 | 187.6s | 0 |
| `highest-temperature-in-wuhan-on-june-6-2026` | 1,529,173 | 174.8s | 0 |
| `highest-temperature-in-singapore-on-june-6-2026` | 1,548,645 | 175.4s | 0 |

## Event-equal 结果

| horizon | events with IC | median event IC | positive event share | median hit rate | median top-bottom |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 30s | 10 | 0.0619 | 1.000 | 0.584 | 0.00043 |
| 120s | 10 | 0.0651 | 1.000 | 0.568 | 0.00051 |
| 600s | 10 | 0.0901 | 1.000 | 0.566 | 0.00178 |

## 运行说明

- 因子：`depth_imbalance_1`。
- horizons：30s, 120s, 600s。
- event 内 token 等权；headline 再按 event 等权。
- 样本按严格 clean 条件筛选后取 source rows 最少的 10 个 event，因此这是快速 smoke batch，不是随机样本或确认集。
- 各 event CPU wall-time 合计：2211.9s；并行后的实际总耗时见 `run_summary.json`。
