# PMXT 天气因子 6-event pilot

> 因子研究，不是成交/PnL 回测；crossing markout 仅为诊断。主结论 Event 等权，置信区间按 Event bootstrap。

## 样本与运行

| event | city | quality | source rows | elapsed | token failures |
| --- | --- | --- | ---: | ---: | ---: |
| `highest-temperature-in-karachi-on-june-6-2026` | Karachi | clean | 1,115,044 | 218.1s | 0 |
| `highest-temperature-in-busan-on-june-6-2026` | Busan | clean | 1,279,940 | 254.7s | 0 |
| `highest-temperature-in-qingdao-on-june-6-2026` | Qingdao | clean | 1,346,866 | 266.8s | 0 |
| `highest-temperature-in-beijing-on-june-6-2026` | Beijing | degraded | 1,198,718 | 198.7s | 0 |
| `highest-temperature-in-taipei-on-june-6-2026` | Taipei | degraded | 1,484,448 | 261.0s | 0 |
| `highest-temperature-in-jeddah-on-june-10-2026` | Jeddah | degraded | 1,525,174 | 217.8s | 0 |

- 总 wall time：461.2s；event failure：0/6；真实异常 token：0；无有效 ranking observation 而跳过的 token：0。

## E1 主因子

| factor | horizon | coverage | zero | median event IC | positive event | top-bottom | activity IC diff | gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| depth_imbalance_1 | 30s | 0.995 | 0.763 | 0.053 | 1.000 | 0.00047 | 0.031 | PASS |
| depth_imbalance_1 | 120s | 0.988 | 0.671 | 0.076 | 1.000 | 0.00085 | 0.039 | PASS |
| depth_imbalance_1 | 600s | 0.972 | 0.529 | 0.079 | 1.000 | 0.00154 | 0.073 | FAIL |
| microprice_minus_mid | 30s | 0.995 | 0.763 | 0.051 | 1.000 | 0.00019 | 0.039 | PASS |
| microprice_minus_mid | 120s | 0.988 | 0.671 | 0.062 | 0.833 | 0.00018 | 0.053 | FAIL |
| microprice_minus_mid | 600s | 0.972 | 0.529 | 0.076 | 0.667 | 0.00042 | 0.091 | FAIL |

## 基础因子表现（附录诊断）

| factor | 120s median event IC | positive event |
| --- | ---: | ---: |
| depth_imbalance_1 | 0.076 | 1.000 |
| microprice_minus_mid | 0.062 | 0.833 |
| bid_ask_liquidity_asymmetry | 0.061 | 1.000 |
| depth_imbalance_3 | 0.039 | 1.000 |
| depth_imbalance_5 | 0.012 | 1.000 |
| depth_slope | 0.007 | 0.667 |
| depth_concentration | 0.005 | 0.667 |
| top_level_depth | 0.002 | 0.667 |
| tick_size_regime | -0.011 | 0.167 |
| spread | -0.117 | 0.000 |
| distance_to_zero_one | -0.138 | 0.000 |

## E2 价格区间

pilot 只有 6 个 Event，所有 price-bucket cell 均小于正式门槛 8/12 Event，因此只作描述性诊断，不用于正式 gate。

| factor | price bucket | events | 120s IC | zero | crossing markout（非 PnL） |
| --- | --- | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | (0.80,0.96] | 6 | 0.125 | 0.306 | -0.01751 |
| depth_imbalance_1 | (0.96,1] | 6 | 0.039 | 0.408 | -0.00662 |
| depth_imbalance_1 | [0,0.04) | 6 | 0.105 | 0.734 | -0.00295 |
| depth_imbalance_1 | [0.04,0.20) | 6 | 0.200 | 0.305 | -0.02630 |
| depth_imbalance_1 | [0.20,0.80] | 6 | 0.138 | 0.203 | -0.07663 |
| microprice_minus_mid | (0.80,0.96] | 6 | 0.029 | 0.306 | -0.01768 |
| microprice_minus_mid | (0.96,1] | 6 | 0.067 | 0.408 | -0.00661 |
| microprice_minus_mid | [0,0.04) | 6 | 0.086 | 0.734 | -0.00296 |
| microprice_minus_mid | [0.04,0.20) | 6 | 0.171 | 0.305 | -0.02810 |
| microprice_minus_mid | [0.20,0.80] | 6 | 0.106 | 0.203 | -0.07652 |

## valid_obs

valid_obs10/50/200 仅作诊断，不改写预注册主结论。

| factor | valid_obs | coverage | zero | median IC | elapsed P90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | 10 | 0.999 | 0.827 | 0.065 | 50.2s |
| depth_imbalance_1 | 50 | 0.997 | 0.729 | 0.082 | 329.4s |
| depth_imbalance_1 | 200 | 0.987 | 0.590 | 0.106 | 999.8s |
| microprice_minus_mid | 10 | 0.999 | 0.827 | 0.055 | 50.2s |
| microprice_minus_mid | 50 | 0.997 | 0.729 | 0.073 | 329.4s |
| microprice_minus_mid | 200 | 0.987 | 0.590 | 0.076 | 999.8s |

obs200 相比 obs10 将 zero rate 降低 23.7%，elapsed P90=999.8s，满足 amendment 候选门槛；本轮仍保持 diagnostic-only，尚未正式采用 amendment。

## direct replay parity

ORDERING / CONVERTIBILITY SMOKE FAIL：8 条中 6 条完成转换且 direct replay 时间与 Nautilus `ts_init` 各自单调；Beijing YES/NO 两条在有效 tick 仍为 `0.01` 时出现 `0.001` 价格，按严格精度校验失败。当前没有逐事件比较两边完整语义输出，因此不能声明 direct replay 与 Nautilus 输出完全一致。
具体记录见 `compact/pilot_6_event/direct_native_parity.json`。

## E3

- 因子统计路径建议：**U0**。
- 扩张决策：**NO-GO（先修复并重跑 Beijing parity）**。
- 6-event pilot 只验证流程和方向，不宣称发现可交易 alpha。
