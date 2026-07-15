# PMXT 天气因子下一阶段实验需求

日期：2026-07-15

## 1. 背景与目标

PMXT 当前目标是在 Polymarket 天气类市场中，形成一套可复用、可审计、可扩张的统一因子实验框架，并通过 Nautilus 回测 replay 验证因子在事件级别、时间级别、价格区间级别和活动状态级别的稳健性。

本阶段不是为了直接上线交易，而是为了回答三个实验问题：

1. 统一因子是否能在不同天气 Event、不同活跃度、不同生命周期阶段中保持可解释的信息量。
2. 固定时间 label、价格边界混合、tick/spread/activity/lifecycle 混杂是否导致历史结论不可迁移。
3. 下一阶段是否具备从 6-event pilot 扩张到 36-event batch、100-event batch、最终 441-event full batch 的条件。

本需求面向实验执行人。所有实验必须遵守 no-lookahead、Event 等权、Event bootstrap、direct replay parity 和预注册判定规则；任何实验后新增规则都必须标注为 amendment，不能混入原始预注册结论。

## 2. 当前已知问题

### 2.1 固定时间 label 的信息量不均

天气 Event 的活跃度、价格接近边界程度、市场生命周期和盘口质量差异很大。固定时间 label 在不同 Event 中并不等价：

- 高活跃、临近结算或临近天气阈值的 Event 中，短期 label 可能包含较强信息。
- 低活跃、宽 spread、长时间无成交的 Event 中，同样的 label 可能主要反映 stale quote、tick 粒度或盘口缺失。
- 同一 label horizon 在 early/mid/late lifecycle 中可能衡量完全不同的市场状态。

因此，本阶段必须显式分层 activity、label horizon、valid_obs 和 lifecycle，不能把所有观测简单池化后直接解释。

### 2.2 <0.04 / >0.96 价格混合问题

当前样本可能混合了极端价格区间：

- `<0.04` 的 YES 价格通常接近极低概率状态。
- `>0.96` 的 YES 价格通常接近极高概率状态。

这些区间的 tick、spread、盘口深度、可成交性、生命周期和结算接近程度都可能与中间价格区间完全不同。若直接混合，因子表现可能只是价格边界或市场状态的副产物，而非可迁移的信息。

下一阶段必须把价格分为五档区间，并在 E2 中控制 tick、spread、activity、lifecycle 和 quality。

### 2.3 tick / spread / activity / lifecycle 混杂问题

因子与收益 label 的关系可能被以下变量混杂：

- tick size 与最小报价单位。
- bid-ask spread 与可执行性。
- order book activity 与 quote 更新频率。
- Event 生命周期阶段，包括 early、mid、late、near-resolution。
- 市场质量，包括 missing book、zero depth、stale quote、异常宽 spread、重复价格状态。

所有实验必须把这些变量作为控制项、分层项或质量诊断项处理，并在报告中明确说明哪些结论经过控制，哪些只是诊断观察。

## 3. 管理决策

本阶段采用分阶段推进：

1. 先执行 6-event pilot，验证 replay、因子计算、label、图表、审计字段和报告模板。
2. pilot 通过后执行 36-event batch。
3. 36-event batch 达到扩张门槛后，推进 100-event batch。
4. 100-event batch 达到扩张门槛后，推进 441-event full batch。

管理层需要的输出不是完整技术日志，而是一页 scorecard 和最多四张关键图，说明：

- 是否继续扩张。
- 哪些因子或 regime 保留。
- 哪些 gate 进入下一轮。
- 哪些结果只作为 diagnostic，不进入决策。
- 是否存在 replay parity、coverage、runtime 或 event failure 风险。

## 4. valid_obs 规则与预注册 amendment

valid_obs 在本阶段只作为 diagnostic-only，不能直接作为主要结论过滤器，除非通过预注册 amendment。

默认要求：

- 主结论使用预注册样本定义。
- valid_obs10、valid_obs50、valid_obs200 均产出诊断结果。
- valid_obs 对 zero rate、coverage、elapsed time 和 IC 的影响必须报告。
- valid_obs 不得在事后挑选最优门槛来改写主结论。

若 pilot 或 36-event batch 显示 valid_obs 是必要质量门槛，必须新增 amendment：

- 明确 amendment 创建日期。
- 明确触发原因。
- 明确新门槛。
- 明确 amendment 后的结果与原预注册结果分开展示。

valid_obs 可进入下一阶段 gate 的最低要求：

- valid_obs 过滤后 zero rate 下降 `>=15pp`。
- obs200 的 elapsed P90 `<=1800s`。

## 5. 数据与样本设计

### 5.1 6-event pilot

pilot 目标是验证流程，不做强统计结论。

pilot 必须覆盖：

- 6 个天气 Event。
- 尽量覆盖不同城市、不同天气类型、不同活跃度和不同生命周期。
- 全部使用 YES leg。
- 至少包含低、中、高 activity 分层。
- 至少包含 clean 与 degraded 两类质量状态。

pilot 通过条件：

- replay 能稳定完成。
- direct replay parity 通过。
- 因子、label、audit fields、compact 输出完整。
- 图表与 report 能自动生成。
- 单次 pilot 总 runtime `<=6h`；若超过 6h，必须先优化后再进入 36-event batch。

### 5.2 36-event batch

36-event batch 是第一轮正式判定样本。

样本结构：

- 9 个日期 × 每日 4 个 Event，共 36 个 Event。
- 预期约 20 个 clean Event、16 个 degraded Event。
- low / medium / high activity 三档分层。
- 覆盖 `>=24` 个城市。
- 主样本全部使用 YES leg。
- 另设 4 个 Event 做 YES/NO parity 检查。

质量要求：

- clean/degraded 定义必须在执行前固定。
- activity 分层必须在执行前固定。
- degraded Event 不能简单删除，必须保留用于稳健性和质量诊断。
- 每个 Event 必须独立生成审计字段、compact 和 replay parity 记录。

runtime 要求：

- 若 36-event batch 预计 `>24 CPUh`，必须先优化 replay、cache 或因子计算路径，再执行完整 batch。

### 5.3 100-event 与 441-event 扩张门槛

从 36-event batch 扩张到 100-event batch 的最低条件：

- 关键指标达到预注册阈值，或有清晰 regime/gate 解释。
- Event-level 结果不由少数 Event 主导。
- coverage、zero、event fail rate 和 runtime 均达标。
- direct replay parity 无系统性偏差。
- valid_obs 若要作为 gate，必须已有 amendment。

从 100-event batch 扩张到 441-event full batch 的最低条件：

- 100-event 结果与 36-event 方向一致。
- Event bootstrap 置信区间不显示结论完全由抽样噪声驱动。
- low/medium/high activity 与 clean/degraded 分层下无严重反向。
- runtime、存储、报告生成和审计可承受 full batch。

## 6. 通用实验原则

所有实验必须遵守以下原则：

- Event 等权：主结论按 Event 等权聚合，不能让高频 Event 主导。
- no-lookahead：因子、label、gate、quality state 均不得使用未来信息。
- Event bootstrap：不以 observation bootstrap 替代主置信区间。
- direct replay parity：Nautilus replay 输出必须与 direct replay 的关键状态一致。
- 预注册优先：新增解释、过滤、门槛必须作为 amendment 或 diagnostic 标注。
- cell size 限制：任意分层 cell `<8` 个 Event 时，不作正式结论，只作描述性诊断。
- event failure stop：若 `>5%` Event fail，停止扩张，先修复数据或 replay 管道。

## 7. 实验 E1：Activity × Label

### 7.1 目的

检验统一因子在不同 activity 和 label horizon 下的信息量是否稳定，识别固定时间 label 在天气 Event 中的信息量不均问题。

### 7.2 因子

主因子：

- `depth_imbalance_1`
- `microprice_minus_mid`

其他统一因子可作为附录诊断，但不能稀释主判定。

### 7.3 Label horizon

主 label window：

- wall 30s
- wall 120s
- wall 600s

兼容与诊断 label：

- wall 60s
- wall 300s
- wall 900s

60/300/900 仅作为诊断与敏感性分析，主结论以 30/120/600 为准。

### 7.4 valid_obs 分层

输出：

- valid_obs10
- valid_obs50
- valid_obs200

valid_obs 只作为 diagnostic-only，除非 amendment 生效。

### 7.5 Activity 与 mutation 分档

必须按 30 秒 mutation 进行分档。分档至少包含：

- low activity
- medium activity
- high activity

分档规则必须在 batch 执行前固定，并写入 compact。

### 7.6 指标

E1 必须输出以下指标：

- coverage
- zero rate
- IC
- positive event rate
- top-bottom spread 或 top-bottom label difference
- elapsed time 分布，包括 P50/P90
- activity IC 差异
- valid_obs 对 coverage、zero、IC、elapsed 的影响

判定阈值：

- coverage `>=70%`
- zero rate `<=80%`
- activity IC 差 `<=0.05`
- positive-event rate `>=0.60`
- valid_obs zero 下降 `>=15pp` 且 obs200 elapsed P90 `<=1800s` 时，valid_obs 可进入 amendment 候选

### 7.7 图表

E1 必须生成：

- Activity × Label heatmap
- Event-level IC forest plot
- valid_obs 分层 box plot
- elapsed 或 zero rate ECDF

### 7.8 判定

保留条件：

- 主因子在 30/120/600 至少一个 horizon 达到 coverage、zero、positive-event 和 activity IC 差阈值。
- Event bootstrap 不显示结果仅由 1-2 个 Event 驱动。

进入 gate 条件：

- 因子只在特定 activity 或 valid_obs regime 下有效，但该 regime 可 no-lookahead 定义。

删除条件：

- coverage 长期不足。
- zero rate 过高且 valid_obs 无法改善。
- positive-event rate `<0.60`。
- activity IC 差 `>0.05` 且无法通过 regime 解释。

## 8. 实验 E2：五档价格区间与混杂控制

### 8.1 目的

检验因子表现是否由价格边界、tick、spread、activity、lifecycle 或 quality 状态驱动，尤其处理 `<0.04` 与 `>0.96` 极端价格混合问题。

### 8.2 五档价格区间

YES 价格按五档分区：

- `[0.00, 0.04)`
- `[0.04, 0.20)`
- `[0.20, 0.80]`
- `(0.80, 0.96]`
- `(0.96, 1.00]`

价格边界分析要求：

- 每个关键价格边界 cell 需要 `>=12` 个 Event 才能作正式判定。
- cell `<8` 个 Event 时不作结论，只作描述性诊断。

### 8.3 控制变量

E2 必须控制或分层：

- tick size
- bid-ask spread
- activity
- lifecycle
- quality state，包括 clean/degraded、stale quote、zero depth、missing book、wide spread

### 8.4 指标

状态指标：

- coverage by price bucket
- zero rate by price bucket
- spread distribution
- tick distribution
- activity distribution
- lifecycle distribution
- quality state distribution

因子指标：

- IC by price bucket
- positive-event rate by price bucket
- Event-level top-bottom
- crossing markout

crossing markout 只用于诊断可执行性与方向性，必须注明不是实际 PnL，不得作为真实收益或交易回测结论。

### 8.5 图表

E2 必须生成：

- price bucket × factor IC heatmap
- price bucket 状态分布图
- crossing markout by price bucket
- clean/degraded 分层 forest plot

### 8.6 判定

保留条件：

- 因子在中间价格区间或多个价格区间均有稳定方向。
- 控制 tick/spread/activity/lifecycle/quality 后结论仍成立。

gate 条件：

- 因子只在特定价格区间有效，且该区间可 no-lookahead 定义。
- `<0.04` 或 `>0.96` 极端区间表现不同，但能通过明确 regime 隔离。

regime 条件：

- 若极端价格区间和中间价格区间方向不同，必须进入 R1 分 regime，而不是混合作为统一结论。

删除条件：

- 因子表现完全由价格区间或 spread/tick 状态解释。
- 关键价格边界不足 `>=12` Event 且无法扩样。
- crossing markout 与 IC 方向长期冲突且无法解释。

## 9. 实验 E3：统一因子、gate 与 regime 决策

### 9.1 目的

在 E1 与 E2 基础上，决定下一阶段采用统一模型、统一模型加 gate、分 regime，或停止该因子线。

### 9.2 候选路径

U0：统一因子

- 所有合格 Event 使用同一因子定义。
- 不引入 activity、price 或 quality gate。
- 适用于 E1/E2 显示跨分层稳定的情况。

U1：统一因子 + gate

- 因子定义保持统一。
- 进入 no-lookahead gate，例如 activity gate、valid_obs amendment gate、quality gate 或 price bucket gate。
- 适用于因子方向一致但质量或状态分层显著影响可用性的情况。

R1：分 regime

- 明确拆分 activity、price、lifecycle 或 quality regime。
- 每个 regime 独立报告指标。
- 适用于不同 regime 方向、强度或可执行性显著不同的情况。

Stop：停止该因子线

- 因子无法满足 coverage、zero、positive-event、activity IC 差或 Event bootstrap 稳健性要求。
- 或 replay parity、event fail、runtime 风险无法修复。

### 9.3 判定规则

选择 U0 的条件：

- coverage `>=70%`
- zero rate `<=80%`
- activity IC 差 `<=0.05`
- positive-event rate `>=0.60`
- E2 中价格分层无严重反向

选择 U1 的条件：

- 原始统一样本未完全达标，但 no-lookahead gate 后达标。
- gate 不是事后挑选；若使用 valid_obs，必须有 amendment。

选择 R1 的条件：

- activity、price、lifecycle 或 quality 分层存在稳定差异。
- 每个核心 regime cell 有足够 Event；cell `<8` 不作正式结论。

选择 Stop 的条件：

- 多数 Event 无正向信息。
- zero/coverage 问题无法通过预注册 gate 解决。
- Event bootstrap 显示不稳健。
- replay parity 或 failure rate 不达标。

## 10. 审计字段与 direct replay parity

每个 Event、每个 replay run、每个 compact 文件必须保留审计字段。

最低审计字段：

- event_id
- market_slug
- date
- city
- weather_type
- yes_no_leg
- replay_run_id
- direct_replay_run_id
- data_snapshot_id
- code_version 或 git commit
- factor_version
- label_version
- amendment_version
- event_quality_state
- activity_bucket
- lifecycle_bucket
- price_bucket
- valid_obs_threshold
- coverage
- zero_rate
- fail_reason
- elapsed_p50
- elapsed_p90

direct replay parity 必须检查：

- book state parity
- mid / microprice parity
- factor timestamp parity
- label alignment parity
- event inclusion parity
- YES/NO parity for selected 4 events

若 parity fail：

- 单个 Event 失败必须记录 fail_reason。
- `>5%` Event fail 时停止 batch 扩张。
- parity fail 不得静默删除。

## 11. 交付目录

所有交付物放在本实验目录下，建议结构：

```text
polymarket/research/2026-07-15-pmxt-weather-next-stage-experiments/
  protocol/
    preregistration.md
    amendments.md
    sample_plan.md
  run/
    pilot_6_event/
    batch_36_event/
    batch_100_event/
    batch_441_event/
  compact/
    pilot_6_event/
    batch_36_event/
    batch_100_event/
    batch_441_event/
  charts/
    e1_activity_label/
    e2_price_buckets/
    e3_regime_decision/
    scorecard/
  report/
    pilot_report.md
    batch_36_report.md
    boss_scorecard.md
```

要求：

- `protocol/` 保存预注册规则、amendment 和样本计划。
- `run/` 保存 replay 与实验运行输出。
- `compact/` 保存可审计的精简结果。
- `charts/` 保存所有图表。
- `report/` 保存实验报告和老板一页 scorecard。

## 12. 老板一页 scorecard

scorecard 必须一页内完成，最多四张图。

必须包含：

- 当前阶段：pilot / 36 / 100 / 441。
- 样本数量：Event 数、城市数、clean/degraded 数、activity 分布。
- 关键阈值是否通过：coverage、zero、activity IC 差、positive-event、valid_obs diagnostic、event fail、runtime。
- E1 结论：保留 / gate / regime / 删除。
- E2 结论：价格区间是否混杂，是否需要 gate 或 regime。
- E3 决策：U0 / U1 / R1 / Stop。
- replay parity 状态。
- 下一步建议。

最多四张图：

- E1 Activity × Label heatmap。
- Event-level IC forest plot。
- E2 price bucket heatmap 或状态分布图。
- E3 decision summary 或 bootstrap interval 图。

## 13. Stop conditions

出现以下任一情况必须停止扩张，先修复或重新评审：

- `>5%` Event fail。
- direct replay parity 出现系统性失败。
- pilot runtime `>6h`。
- 36-event batch 预计 `>24 CPUh` 且未优化。
- coverage 持续 `<70%`。
- zero rate 持续 `>80%`。
- positive-event rate `<0.60`。
- activity IC 差 `>0.05` 且无法解释。
- 关键价格边界不足 `>=12` Event 且无法扩样。
- cell `<8` 却被用于正式结论。
- valid_obs 被事后用作主 gate 但没有 amendment。
- crossing markout 被误写成真实 PnL。
- no-lookahead、Event 等权或 Event bootstrap 被违反。

## 14. 完成标准

本阶段视为完成，必须同时满足：

1. 6-event pilot 完成并通过 replay、compact、charts、report 验证。
2. 若 pilot 通过，36-event batch 完成并满足样本设计要求。
3. E1、E2、E3 均有完整指标、图表和判定。
4. valid_obs 被明确标为 diagnostic-only，或已通过 amendment。
5. direct replay parity 记录完整。
6. Event 等权、no-lookahead、Event bootstrap 已在报告中明确执行。
7. boss scorecard 一页完成，图表不超过四张。
8. stop conditions 均被检查并记录。
9. 对是否进入 100-event batch 给出明确 go / no-go / amend-and-rerun 决策。
10. 所有交付物落在 protocol、run、compact、charts、report 对应目录下。

最终交接时，实验执行人必须能基于本文件独立完成 pilot、36-event batch、扩张判定和报告生成，不需要额外口头解释。
