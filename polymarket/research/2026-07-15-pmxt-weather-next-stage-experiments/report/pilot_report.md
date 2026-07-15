# PMXT 天气因子 6-event pilot

> 因子研究，不是成交/PnL 回测；crossing markout 仅为诊断。主结论 Event 等权，置信区间按 Event bootstrap。

## 样本与运行

| event | city | quality | source rows | elapsed | token failures |
| --- | --- | --- | ---: | ---: | ---: |
| `highest-temperature-in-chicago-on-june-4-2026` | Chicago | clean | 17,690 | 10.3s | 0 |
| `highest-temperature-in-denver-on-june-4-2026` | Denver | clean | 44,972 | 14.5s | 0 |
| `highest-temperature-in-seattle-on-june-5-2026` | Seattle | clean | 249,331 | 140.5s | 0 |
| `highest-temperature-in-buenos-aires-on-june-12-2026` | Buenos Aires | degraded | 20,447 | 64.2s | 0 |
| `highest-temperature-in-mexico-city-on-june-12-2026` | Mexico City | degraded | 20,649 | 61.2s | 0 |
| `highest-temperature-in-houston-on-june-12-2026` | Houston | degraded | 21,812 | 57.6s | 0 |

- 总 wall time：226.2s；event failure：0/6；真实异常 token：0；无有效 ranking observation 而跳过的 token：22。

## E1 主因子

| factor | horizon | coverage | zero | median event IC | positive event | top-bottom | activity IC diff | gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| depth_imbalance_1 | 30s | 0.926 | 0.892 | 0.049 | 0.833 | 0.00012 | 0.142 | FAIL |
| depth_imbalance_1 | 120s | 0.904 | 0.826 | 0.075 | 0.667 | 0.00017 | 0.180 | FAIL |
| depth_imbalance_1 | 600s | 0.816 | 0.709 | 0.153 | 0.833 | 0.00038 | 0.119 | FAIL |
| microprice_minus_mid | 30s | 0.926 | 0.892 | 0.064 | 0.833 | -0.00006 | 0.116 | FAIL |
| microprice_minus_mid | 120s | 0.904 | 0.826 | 0.093 | 0.833 | 0.00007 | 0.150 | FAIL |
| microprice_minus_mid | 600s | 0.816 | 0.709 | 0.146 | 1.000 | 0.00010 | 0.236 | FAIL |

## 基础因子表现（附录诊断）

| factor | 120s median event IC | positive event |
| --- | ---: | ---: |
| bid_ask_liquidity_asymmetry | 0.143 | 1.000 |
| microprice_minus_mid | 0.093 | 0.833 |
| depth_imbalance_1 | 0.075 | 0.667 |
| top_level_depth | 0.062 | 0.667 |
| depth_imbalance_3 | 0.059 | 0.667 |
| depth_concentration | 0.053 | 0.667 |
| depth_imbalance_5 | 0.013 | 0.500 |
| spread | -0.033 | 0.333 |
| depth_slope | -0.085 | 0.333 |
| tick_size_regime | -0.093 | 0.000 |
| distance_to_zero_one | -0.413 | 0.167 |

## E2 价格区间

pilot 只有 6 个 Event，所有 price-bucket cell 均小于正式门槛 8/12 Event，因此只作描述性诊断，不用于正式 gate。

| factor | price bucket | events | 120s IC | zero | crossing markout（非 PnL） |
| --- | --- | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | (0.96,1] | 3 | 0.310 | 0.925 | -0.00213 |
| depth_imbalance_1 | [0,0.04) | 6 | 0.099 | 0.915 | -0.00297 |
| depth_imbalance_1 | [0.04,0.20) | 3 | -0.033 | 0.491 | -0.05608 |
| depth_imbalance_1 | [0.20,0.80] | 3 | 0.205 | 0.359 | -0.15860 |
| microprice_minus_mid | (0.96,1] | 3 | 0.284 | 0.925 | -0.00213 |
| microprice_minus_mid | [0,0.04) | 6 | 0.142 | 0.915 | -0.00297 |
| microprice_minus_mid | [0.04,0.20) | 3 | -0.006 | 0.491 | -0.05605 |
| microprice_minus_mid | [0.20,0.80] | 3 | 0.140 | 0.359 | -0.15851 |

## valid_obs

valid_obs10/50/200 仅作诊断，不改写预注册主结论。

| factor | valid_obs | coverage | zero | median IC | elapsed P90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | 10 | 0.983 | 0.838 | 0.062 | 269.3s |
| depth_imbalance_1 | 50 | 0.918 | 0.654 | 0.097 | 861.8s |
| depth_imbalance_1 | 200 | 0.761 | 0.424 | 0.217 | 2869.9s |
| microprice_minus_mid | 10 | 0.983 | 0.838 | 0.076 | 269.3s |
| microprice_minus_mid | 50 | 0.918 | 0.654 | 0.119 | 861.8s |
| microprice_minus_mid | 200 | 0.761 | 0.424 | 0.212 | 2869.9s |

obs200 虽显著降低 zero rate，但 elapsed P90 超过 1800s，因此不满足 amendment 候选门槛。

## direct replay parity

FAIL：ValueError("price violates effective tick size: sequence=1, asset_id='90126329302086190846098290501947516698603202176436371691446950377026992913400', price=0.0170, effective_tick_size=0.01")；按预注册规则暂不扩张。
具体记录见 `compact/pilot_6_event/direct_native_parity.json`。

## E3

- 因子统计路径建议：**U1 / R1（pilot 仅诊断，进入 36-event 前需确认 regime）**。
- 扩张决策：**NO-GO（先补 parity）**。
- 6-event pilot 只验证流程和方向，不宣称发现可交易 alpha。
