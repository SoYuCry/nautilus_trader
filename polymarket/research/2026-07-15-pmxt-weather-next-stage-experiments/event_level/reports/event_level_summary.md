# PMXT 天气 Event-level 36 Event 实验

> 这是因子结构研究，不是 PnL 回测。token 旧实验改名为 `token-level fixed-horizon baseline`；crossing markout 不是成交收益。

## 样本与可审计性

- Event：36；日期：9；城市：36；clean/degraded：20/16。
- token 总数：396；进入分析：366；skipped：24；失败：6。
- skipped clean/degraded：17/7；city={'Lucknow': 8, 'Chengdu': 3, 'Los Angeles': 3, 'Hong Kong': 3, 'London': 2, 'Atlanta': 2, 'NYC': 2, 'Denver': 1}；date={'2026-06-04': 13, '2026-06-05': 4, '2026-06-07': 7}。
- skipped 明显集中于早期 clean 日期，尤其 Lucknow；原因是 inactive market 无 ranking observation，因此 token baseline 的 clean 样本偏向活跃 market，只能作局部特征对照。
- 失败 token：6；明细见 `compact/batch_36_event/token_failures.csv`。主要原因是 PMXT tick_size_change 的 old tick 与回放状态冲突，未自动修补。
- 对齐：Event grid 为 1 分钟；每个 token 只用该时刻之前最后一个有效盘口（backward as-of），active gate 仅使用当时及过去 30 分钟信息。

## Token-level fixed-horizon baseline

| factor | horizon | events | median IC | positive Event | zero | crossing | coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | 30s | 36 | 0.077 | 1.000 | 0.795 | -0.0090 | 0.998 |
| depth_imbalance_1 | 60s | 36 | 0.077 | 0.972 | 0.751 | -0.0090 | 0.997 |
| depth_imbalance_1 | 120s | 36 | 0.085 | 0.972 | 0.691 | -0.0090 | 0.995 |
| depth_imbalance_1 | 300s | 36 | 0.087 | 0.972 | 0.607 | -0.0090 | 0.990 |
| depth_imbalance_1 | 600s | 36 | 0.093 | 0.972 | 0.518 | -0.0088 | 0.981 |
| depth_imbalance_1 | 900s | 36 | 0.106 | 0.944 | 0.459 | -0.0087 | 0.977 |
| microprice_minus_mid | 30s | 36 | 0.049 | 1.000 | 0.795 | -0.0090 | 0.998 |
| microprice_minus_mid | 60s | 36 | 0.056 | 0.944 | 0.751 | -0.0090 | 0.997 |
| microprice_minus_mid | 120s | 36 | 0.058 | 0.944 | 0.691 | -0.0090 | 0.995 |
| microprice_minus_mid | 300s | 36 | 0.062 | 0.917 | 0.607 | -0.0090 | 0.990 |
| microprice_minus_mid | 600s | 36 | 0.054 | 0.917 | 0.518 | -0.0088 | 0.981 |
| microprice_minus_mid | 900s | 36 | 0.065 | 0.889 | 0.459 | -0.0087 | 0.977 |

所有 crossing 中位数只作诊断；若为负，不作策略化解释。

## Lifecycle

| bucket | events | updates/min | trades/min | active markets | spread | 120s IC | zero | crossing | coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| >24h | 28 | 351.95 | 0.08 | 4.87 | 0.0100 | 0.085 | 0.724 | -0.0099 | 0.998 |
| 6-24h | 28 | 480.06 | 0.26 | 5.01 | 0.0060 | 0.111 | 0.709 | -0.0071 | 0.995 |
| 1-6h | 24 | 605.20 | 0.37 | 4.57 | 0.0030 | 0.132 | 0.681 | -0.0074 | 0.990 |
| <1h | 19 | 249.55 | 0.13 | 4.00 | 0.0020 | 0.089 | 0.748 | -0.0067 | 0.948 |

## Active set

| scope | events | IC | zero | crossing | Top3 updates | Top3 trades | Top3 mass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| all_market | 36 | 0.057 | 0.812 | -0.0074 | 0.436 | 0.681 | 0.835 |
| dynamic_active | 36 | 0.079 | 0.718 | -0.0110 | 0.645 | 0.681 | 0.929 |

## Event probability distribution

- |sum(mid)-1|：P90=0.082，P95=0.135，最大=3.935，超过 0.10 的观测占 7.2%；异常尾部不隐藏。
- pressure → future implied-temperature move：Event IC 中位数 0.073，正 Event 比例 0.889。
- 仅作敏感性检查的 |sum deviation|<=0.10 子样本：IC 中位数 0.064，正 Event 比例 0.833；未替代全样本结论。
- cohort 稳健性：clean IC=0.075（positive=0.950），degraded IC=0.069（positive=0.812）。
- |pressure| → entropy contraction：Event IC 中位数 0.011，正 Event 比例 0.694。

## 决策

- 主要研究窗口：**1-6h**（活跃度和 120s IC 均最高，覆盖 24 Event）；<1h 只覆盖 19 Event，活跃度与 IC 均回落，保留为临近结算 regime。所有 bucket crossing 均负，不进入策略化解释。
- Active set：**暂不采用动态 gate**。它改善 IC/zero，但 crossing 从 -0.0074 恶化到 -0.0110，不满足预设的同时改善条件；Top3 只作描述字段。
- Distribution：pressure 对未来均值移动有跨 cohort 的弱正结构信号，但 sum deviation 有明显异常尾部；只保留候选结构特征，不能称 Alpha。
- 单 token 因子：仅保留为 event-level 局部特征与 fixed-horizon baseline。
- 扩 100：本轮按需求停止，不扩。
- Nautilus：native parity 未补齐，不能进入正式策略回测。

累计 Event replay 处理时间：3.72 CPUh；本次缓存聚合耗时：4.0 分钟。