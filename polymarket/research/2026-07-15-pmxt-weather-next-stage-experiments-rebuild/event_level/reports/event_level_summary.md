# PMXT 天气 Event-level 36 Event 实验

> 这是因子结构研究，不是 PnL 回测。token 旧实验改名为 `token-level fixed-horizon baseline`；crossing markout 不是成交收益。

## 样本与可审计性

- Event：36；日期：5；城市：36；clean/degraded：29/7。
- token 总数：396；进入分析：394；skipped：1；失败：1。
- skipped clean/degraded：1/0；city={'Hong Kong': 1}；date={'2026-06-06': 1}。
- skipped token 为无 ranking observation 的 inactive market；其 city/date 分布已在上一行完整列出，不作额外集中性推断。
- 失败 token：1，涉及 1 个 Event；没有整个 Event 失效。明细见 `compact/batch_36_event/token_failures.csv`。23 个 source-time 间隔不超过 8ms 的重复 tick-size 通知已按告警跳过；Wuhan 的约 325s 长间隔重复仍被严格拒绝。
- 对齐：Event grid 为 1 分钟；每个 token 只用该时刻之前最后一个有效盘口（backward as-of），active gate 仅使用当时及过去 30 分钟信息。

## Token-level fixed-horizon baseline

| factor | horizon | events | median IC | positive Event | zero | crossing | coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | 30s | 36 | 0.069 | 1.000 | 0.784 | -0.0090 | 0.998 |
| depth_imbalance_1 | 60s | 36 | 0.074 | 1.000 | 0.745 | -0.0089 | 0.997 |
| depth_imbalance_1 | 120s | 36 | 0.084 | 1.000 | 0.691 | -0.0086 | 0.996 |
| depth_imbalance_1 | 300s | 36 | 0.091 | 1.000 | 0.617 | -0.0084 | 0.992 |
| depth_imbalance_1 | 600s | 36 | 0.104 | 1.000 | 0.552 | -0.0083 | 0.986 |
| depth_imbalance_1 | 900s | 36 | 0.111 | 1.000 | 0.507 | -0.0081 | 0.981 |
| microprice_minus_mid | 30s | 36 | 0.061 | 0.972 | 0.784 | -0.0090 | 0.998 |
| microprice_minus_mid | 60s | 36 | 0.064 | 1.000 | 0.745 | -0.0089 | 0.997 |
| microprice_minus_mid | 120s | 36 | 0.068 | 1.000 | 0.691 | -0.0086 | 0.996 |
| microprice_minus_mid | 300s | 36 | 0.066 | 1.000 | 0.617 | -0.0084 | 0.992 |
| microprice_minus_mid | 600s | 36 | 0.068 | 1.000 | 0.552 | -0.0083 | 0.986 |
| microprice_minus_mid | 900s | 36 | 0.071 | 0.917 | 0.507 | -0.0081 | 0.981 |

所有 crossing 中位数只作诊断；若为负，不作策略化解释。

## Lifecycle

| bucket | events | updates/min | trades/min | active markets | spread | 120s IC | zero | crossing | coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| >24h | 36 | 365.43 | 0.12 | 4.88 | 0.0080 | 0.080 | 0.734 | -0.0076 | 0.999 |
| 6-24h | 36 | 597.22 | 0.43 | 4.74 | 0.0030 | 0.099 | 0.636 | -0.0105 | 0.990 |
| 1-6h | 35 | 0.11 | 0.04 | 3.10 | 0.0010 | 0.065 | 0.962 | -0.0009 | 0.822 |
| <1h | 21 | 0.00 | 0.05 | 3.00 | 0.0010 | nan | nan | nan | 0.000 |

## Active set

| scope | events | IC | zero | crossing | Top3 updates | Top3 trades | Top3 mass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| all_market | 36 | 0.053 | 0.855 | -0.0064 | 0.489 | 0.722 | 0.875 |
| dynamic_active | 36 | 0.083 | 0.741 | -0.0115 | 0.686 | 0.722 | 0.944 |

## Event probability distribution

- |sum(mid)-1|：P90=0.072，P95=0.097，最大=4.370，超过 0.10 的观测占 4.7%；异常尾部不隐藏。
- pressure → future implied-temperature move：Event IC 中位数 0.074，正 Event 比例 1.000。
- 仅作敏感性检查的 |sum deviation|<=0.10 子样本：IC 中位数 0.072，正 Event 比例 1.000；未替代全样本结论。
- cohort 稳健性：clean IC=0.073（positive=1.000），degraded IC=0.078（positive=1.000）。
- |pressure| → entropy contraction：Event IC 中位数 0.017，正 Event 比例 0.806。

## 决策

- 主要研究窗口：**6-24h**（120s IC 最高）；<1h coverage 为零，不能用于方向性结论。所有可计算 bucket crossing 均负，不进入策略化解释。
- Active set：**暂不采用动态 gate**。它将 IC 从 0.053 提高到 0.083、zero 从 0.855 降到 0.741，但 crossing 从 -0.0064 恶化到 -0.0115；Top3 只作描述字段。
- Distribution：pressure 对未来均值移动有跨 cohort 的弱正结构信号，但 sum deviation 有明显异常尾部；只保留候选结构特征，不能称 Alpha。
- 单 token 因子：仅保留为 event-level 局部特征与 fixed-horizon baseline。
- 扩 100：本轮按需求停止，不扩。
- Nautilus：native parity 已运行 8 条，6 条通过；Beijing YES/NO 两条因有效 tick 为 `0.01` 时出现 `0.001` 价格而失败，不能进入正式策略回测。

累计 Event replay 处理时间：2.61 CPUh；本次缓存聚合耗时：12.5 分钟。
