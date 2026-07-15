# PMXT 天气 Event-level 补充实验需求

日期：2026-07-15

## 核心决策

当前已跑一半的 36 Event token-level 实验继续跑完，但降级命名为 `token-level fixed-horizon baseline`。

- 完成 36 Event 后停止。
- 不扩到 100 Event 或 441 Event。
- 不扩因子池。
- 不作 Alpha 声明。
- 复用同一批 replay / anchor 数据追加 event-level 分析。
- 不重新下载数据。
- 不重做可复用 replay。
- 所有新增分析仍使用相同 36 Event，做配对比较。
- 分层必须保留：`clean` / `degraded` / `city` / `date`。

## 三个市场特点

1. 每个天气 Event 生命周期约 3 天，最后一天最活跃。
2. 每个 Event 约 11 个互斥气温 market，但通常只有 2-3 个活跃 market。
3. 11 个 market 共同构成一个 Event 概率分布，单 token 回报不是唯一研究对象。

## 旧实验为何仍有价值

旧 token-level fixed-horizon baseline 仍保留，因为它提供：

- 相同 36 Event 上的可复现基准。
- 单 token 局部盘口压力、spread、zero rate、IC、crossing markout 的对照。
- 新 event-level 分析的配对比较锚点。
- 判断旧单 token 因子是否可作为 event-level 局部特征的证据。

旧实验不再用于：

- Alpha 声明。
- 扩 Event 数。
- 扩因子池。
- 直接推进 Nautilus 策略回测。

## 新旧关系

- 旧实验：`token-level fixed-horizon baseline`。
- 新实验：在同一批 36 Event replay / anchor 数据上追加 event-level 结构分析。
- 新实验优先回答 lifecycle、dynamic active set、Event probability distribution 三个结构问题。
- crossing markout 仅作为盘口穿越后的标记收益观察，不等同 PnL。
- native parity 只阻塞 Nautilus 回测，不阻塞纯因子结构分析；正式回测前必须补齐 native parity。

## 研究问题

必须回答：

1. 主要研究窗口在哪个 lifecycle 阶段。
2. active set 应固定 Top3，还是使用动态 gate。
3. 单 token 因子是否保留为 event-level 的局部特征。
4. 下一阶段走 event-level unified、lifecycle gate，还是分 regime。
5. 是否扩 100 Event 并进入 Nautilus 策略回测。

## 复用原则与 no-lookahead

- 复用现有 replay / anchor 数据。
- 不重新下载。
- 不重做可复用 replay。
- active set 只能使用当前及历史可见数据定义。
- 禁止使用未来成交、未来 BBO、未来排名、未来结算或未来活跃状态。
- 每个指标必须能说明 timestamp 对齐方式。
- 所有新增分析使用相同 36 Event 做配对比较。

## Pilot skipped token 要求

Pilot 的 22 skipped tokens 原因是 `no ranking observations`，且集中于 clean 事件。

后续报告必须包含：

- 总 token 分母。
- 进入分析 token 数。
- skipped token 数。
- 分析 / 跳过比例。
- skipped token 的 `clean` / `degraded` cohort 分布。
- skipped token 的 `city` / `date` 分布。
- 是否因 skipped 集中于 clean 事件导致偏差。

## 补充分析 1：Lifecycle

### 分桶

按 Event 到期前时间分桶：

| Bucket | 定义 |
| --- | --- |
| `>24h` | 距到期大于 24 小时 |
| `6-24h` | 距到期 6 到 24 小时 |
| `1-6h` | 距到期 1 到 6 小时 |
| `<1h` | 距到期小于 1 小时 |

### 字段

- `event_id`
- `market_id`
- `token_id`
- `city`
- `date`
- `cohort`
- `timestamp`
- `time_to_expiry`
- `lifecycle_bucket`
- `updates`
- `trades`
- `active_market_count`
- `best_bid`
- `best_ask`
- `mid`
- `spread`
- `zero_rate`
- `factor_value`
- `forward_return`
- `ic`
- `crossing_markout`
- `coverage`

### 指标

- updates count / rate。
- trades count / rate。
- active market count。
- spread median / p90。
- zero rate。
- IC。
- crossing markout。
- coverage。

### 图表

- 必须：lifecycle activity curve。
- 表：按 bucket 汇总 updates、trades、active market count、spread、zero rate、IC、crossing markout、coverage。
- 表：按 bucket x clean/degraded。
- 表：按 bucket x city/date。

### 判定标准

- 找出因子在哪个 lifecycle 阶段有效。
- 若有效性只集中于单日期或单城市，判为证据不足。
- 若某 bucket Event/cell 不足，停止对应 bucket 结论。
- 若 crossing 全部为负，不进入策略化解释。

## 补充分析 2：Dynamic Active Set

### 定义

每个 Event 约 11 个互斥气温 market，但通常只有 2-3 个活跃。active set 只能用当前及历史可见数据定义。

候选动态 gate 字段：

- rolling mutations。
- rolling trades。
- 有效 BBO。
- spread。
- mid 范围。
- Top-K 概率。
- Top-K 活跃度。

inactive market 只标状态，不永久删除。

### 字段

- `event_id`
- `market_id`
- `timestamp`
- `is_active`
- `active_reason`
- `rolling_mutations`
- `rolling_trades`
- `has_valid_bbo`
- `spread`
- `mid`
- `mid_rank`
- `activity_rank`
- `probability_rank`
- `top1_flag`
- `top3_flag`
- `inactive_flag`

### 指标

- Top1 占 updates 比例。
- Top3 占 updates 比例。
- Top1 占 trades 比例。
- Top3 占 trades 比例。
- Top1 概率质量比例。
- Top3 概率质量比例。
- all-market 因子 IC / zero rate / crossing markout。
- active-set 因子 IC / zero rate / crossing markout。

### 图表

- 必须：active market count + Top3 concentration。
- 必须：all-market vs active-set IC / zero / crossing 对照。

### 判定标准

- 若 Top3 覆盖主要 updates、trades、probability mass，优先动态 Top3 / gate。
- 若 Top3 覆盖不足，保留 all-market event distribution。
- 若 active set 使用未来数据，结果作废。
- 若 active-set 相对 all-market 只改善 zero rate、不改善 IC 或 crossing，不能声明有效。

## 补充分析 3：Event Probability Distribution

### 定义

每个 Event 的 11 个 market 作为联合概率分布。

处理要求：

- 使用有效 mid。
- 对有效 mid 做归一化。
- 审计 sum deviation。
- 不隐藏概率和偏差过大的样本。

### 字段

- `event_id`
- `market_id`
- `timestamp`
- `raw_mid`
- `valid_mid`
- `normalized_probability`
- `sum_mid`
- `sum_deviation`
- `temperature_bin`
- `implied_temperature`
- `distribution_variance`
- `distribution_width`
- `entropy`
- `top3_probability_mass`
- `active_market_count`
- `probability_mass_flow`
- `pressure_factor`
- `future_mean_move`
- `future_entropy_change`

### 指标

- implied temperature。
- variance / width。
- entropy。
- Top3 probability mass。
- active market count。
- probability mass flow。
- 盘口压力对分布均值移动的预测。
- 盘口压力对熵收缩的预测。

### 图表

- 必须：代表 Event 分布快照，至少 T-48 / T-24 / T-6 / T-1。
- 可选：mass-flow heatmap。

### 判定标准

- 研究目标是盘口压力是否预测分布均值移动或熵收缩，而不只是预测单 token 回报。
- 若概率和偏差过大，停止分布结论。
- 若结果由单日期或单城市驱动，停止推广。

## 交付目录

建议新增输出放在：

```text
polymarket/research/2026-07-15-pmxt-weather-next-stage-experiments/event_level/
```

必须交付：

```text
event_level/
  lifecycle/
    lifecycle_activity_curve.png
    lifecycle_metrics.csv
    lifecycle_by_cohort_city_date.csv
  active_set/
    active_market_count_top3_concentration.png
    active_set_metrics.csv
    all_market_vs_active_set_comparison.png
  distribution/
    representative_event_snapshots.png
    distribution_metrics.csv
    mass_flow_heatmap.png
  reports/
    event_level_summary.md
    boss_scorecard.md
```

## Stop / 证据不足条件

出现任一条件必须停止对应结论：

- 阶段 / cell Event 不足。
- 概率和偏差过大。
- active set 使用未来数据。
- 单日期 / 城市驱动。
- crossing 全部为负。
- 旧新结果无法复现。
- skipped token 分布导致 cohort 偏差无法解释。
- native parity 未补齐时，不得进入 Nautilus 正式回测。

## 决策输出

最终报告必须明确给出：

| 决策 | 可选项 | 输出 |
| --- | --- | --- |
| 主要研究窗口 | `>24h` / `6-24h` / `1-6h` / `<1h` | 写明证据 |
| Active set | 固定 Top3 / 动态 gate / all-market | 写明 no-lookahead 定义 |
| 单 token 因子 | 保留局部特征 / 删除 / 仅 baseline | 写明依据 |
| 下一阶段 | event-level unified / lifecycle gate / 分 regime | 写明依据 |
| 是否扩 100 | 是 / 否 / 暂缓 | 写明条件 |
| 是否 Nautilus 回测 | 是 / 否 / native parity 后再评估 | 写明阻塞项 |

## 老板一页 Scorecard 模板

```markdown
# PMXT Weather Event-level Scorecard

日期：2026-07-15

## 结构发现

- Lifecycle 主窗口：
- Active set 结论：
- Distribution 结论：
- 单 token baseline 角色：

## 证据

| 项目 | 结果 | 证据 | 风险 |
| --- | --- | --- | --- |
| Lifecycle |  |  |  |
| Active set |  |  |  |
| Distribution |  |  |  |
| Baseline reproducibility |  |  |  |

## 研究调整

- 保留：
- 停止：
- 改名：
- 不声明：

## 下一步

- 是否扩 100：
- 是否进入 Nautilus：
- native parity 状态：
- Stop condition：
```
