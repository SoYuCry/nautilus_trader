# 20260715 - Polymarket PMXT 天气因子研究进展

## TLDR

1. 解决历史数据回测问题，用上了 PXMT （之前是因为时间不准，没用，本来打算直接丢掉，上次开会之后发现还是要用。然后还兼容了 nautilus的回放，保证因子研究和回测引擎的一致性）
2. 发现因子研究和回测有点独立，又搭了一下因子研究
3. 本来想做盘口单 token 的因子，后来感觉这样不是正确的
4. 发现了一些关键的问题：时间和活跃度哪些
5. 



1. PMXT 已进入统一 pipeline，441 Event inventory 已经可作为天气研究样本池；这解决的是数据合同、回放和复现问题，不是 441 Event 全量 alpha 结论。
2. 最初从单 Market / 单 Token 盘口因子开始是合理的：`depth_imbalance`、`microprice_minus_mid` 加固定 30/120/600/900s label，可以最快验证 replay、因子、标签和 markout 链路。
3. 36 Event 实验把问题从“某个盘口因子有没有 IC”推进到“研究单位是否应该升级”：不同 lifecycle、不同 outcome 活跃度、固定时间 label 的信息量差异，都会扭曲单 token 结论。
4. 当前没有发现可直接交易的 alpha。1-6h 是主要价格发现窗口，dynamic active set 改善预测但恶化 crossing，Event probability distribution 呈现跨日期稳定的弱结构信号，但所有 crossing 仍为负，不能升级为 Alpha。
5. 概率和异常尾部已完成缓存归因：绝大多数极端尾部随完整性、spread 与同步过滤消失；严格过滤后仍有 205 snapshots / 11 Events 偏差 >0.10，需要逐 Event 报价语义核验，不能解释为套利。建议不扩 100、不进入 Nautilus 策略回测、不扩大单 token 因子池。

## 1. 当前状态：PMXT 已进入统一 pipeline，441 Event inventory 是样本池

这轮最重要的工程进展没有变：PMXT 已经接入统一研究链路。同一套 adapter / replay contract 现在既能跑因子研究，也能进入 Nautilus `BacktestEngine` 做 research backtest；两边消费同一份 step order / source clock，不再是两套各自解释数据的链路。

```text
PMXT event data
  -> PMXT adapter / replay contract
  -> factor research
  -> PolymarketL2DatasetV1
  -> Nautilus native data
  -> BacktestEngine
```

从历史数据中提取了更多天气的 Event 做测试。（接下来是这些数据展示，441 啊 9 date 啊这些）

当前天气 inventory：

| 项目 | 数量 |
| --- | ---: |
| events | 441 |
| cities | 49 |
| dates | 9 |
| markets | 4,851 |
| tokens | 9,702 |
| rows_written_total | 747,185,591 |

（这里要不要描述数据的缺失和可用性，或者说 evalatuion 的指标，然后大概描述一下数据的质量）

（如果提数据质量，那么这句话就可以省了）这组数字的含义要说清楚：它是 inventory / 可用样本池，说明我们已经有一个统一合同下的天气事件面板，可以抽样、分层、复现和扩展实验。它不表示 441 events 已经全部完成全量因子 materialization，也不表示已经有 441-event 的确认性因子结论。

（删掉，傻逼啊，早就说过了，强调不能干的事干鸡毛）Nautilus 侧已经能跑通 research backtest，但目前意义是 plumbing / research 验证，不是收益证明。PMXT replay 是可复现 research replay，不是撮合级 truth，也不是 L3 queue / 真实交易所消息顺序。

## 2. 为什么先从单 Token 盘口因子开始
（不需要讲为什么，标题直接写但盘口因子结果，然后列计算了哪些因子，什么结果。然后最后抛出来一个问题，这样不太对劲啊，不同时间，不同 token 完全含义不一样。而且越接近 settle，越一致，又是另一种市场状态）

一开始从单 Market / 单 Token 盘口因子切入是合理的。原因很简单：这是最小闭环。

`depth_imbalance`、`microprice_minus_mid` 这类 L2 因子，配合固定 30/120/600/900s label，可以最快回答几个基础问题：
（不用说这几个基础问题，或者简单说，我给老板汇报 老板都是老行家了，这里简单带过）

- PMXT replay 后的盘口状态是否可稳定复现；
- 因子、标签、聚合、报告链路是否跑通；
- 单 token 的方向信息是否至少能在 mid-to-mid markout 上留下痕迹；
- 固定时间 label 在天气市场里会遇到什么样的 zero-return 问题。

36 Event 结果显示，单 token baseline 有方向信息，但不能转成可交易结论。`depth_imbalance_1` 在 120s 的 median IC 为 0.085，900s 为 0.106；但所有 horizon 的 crossing 中位数都是负数，120s 为 -0.0090，900s 为 -0.0087。方向信息不等于可执行收益。

（这里可以，但是交代清楚一下，这个实验的数据范围是？全部的 441 个的结果融合起来？以及说，我记得不是还有一个什么300s之后没找到就会有什么问题的那个吗，还有什么 0 的问题，也修过一次。可以体现一下工作量）

| factor | horizon | median IC | zero | crossing |
| --- | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | 120s | 0.085 | 0.691 | -0.0090 |
| depth_imbalance_1 | 900s | 0.106 | 0.459 | -0.0087 |
| microprice_minus_mid | 120s | 0.058 | 0.691 | -0.0090 |
| microprice_minus_mid | 900s | 0.065 | 0.459 | -0.0087 |

这不是坏结果。它说明单 token 因子适合作为局部盘口特征和 fixed-horizon baseline，但不应继续把主线押在“单 token 固定时间 + 直接 taker”上。

## 3. 实验暴露的三项结构问题

（对这里没问题，是应该在单 token 的讨论之后，开始聊这个问题）

第一，约 3 天生命周期里的不同阶段不是同一种市场。

早期、6-24h、1-6h、临近结算这几个阶段，交易密度、spread、活跃 outcome、价格发现行为都不同。把它们混成同质样本，会让一个 IC 数字同时包含早期冷启动、主要价格发现、临近结算收敛和 stale quote 清理。

第二，一个 Event 内约 11 个 outcome 的活跃度不同。

主要概率质量、更新和成交都集中在少数 outcome。静态使用全部 outcome 会稀释信号，也会把 inactive / stale outcome 纳入判断。dynamic active set 的实验验证了这个方向有预测价值，但执行指标没有同步改善。

第三，固定时间 label 在不同活跃度下包含的信息量不同。

同样是 120s，早期可能几乎没有新信息，临近结算可能跨过大量盘口更新和成交。zero rate 会随 horizon 变化，horizon 越长 zero 通常越低；但这不等于可以直接把 horizon 拉长做策略。之前的 valid_obs pilot 能降低 zero，但 `obs200` 的 P90 elapsed 约 2870 秒，时间跨度太长，容易把不同 regime 混在一起，所以暂不升级为主标签。

这三点把下一步问题从“哪个 L2 因子更强”改成了三个更基础的验证问题：

1. 主要价格发现窗口到底在哪里；
2. dynamic active set 是否能同时改善预测和执行；
3. 研究单位是否应该从 token 升级到 Event probability distribution。

## 4. 36 Event 设计与审计边界

本轮不是随便挑几个图看趋势，而是做了可审计的 36 Event 设计。

| 项目 | 数量 / 口径 |
| --- | --- |
| Event | 36 |
| 日期 | 9 |
| 城市 | 36 |
| clean / degraded | 20 / 16 |
| token 总数 | 396 |
| 进入分析 | 366 |
| skipped | 24 no ranking observations |
| failed | 6 PMXT tick 状态冲突 |

对齐规则也固定下来：Event grid 为 1 分钟；每个 token 只用该时刻之前最后一个有效盘口，采用 backward as-of；active gate 只使用当时及过去 30 分钟信息，保持 prefix-causal。

24 个 skipped 主要来自 inactive market 无 ranking observations，且偏向早期 clean 日期和 Lucknow 等城市；因此 token baseline 只代表能形成 ranking 的较活跃 token。6 个失败 token 来自 PMXT `tick_size_change` old tick 与回放状态冲突，未静默修补。这一点很重要：数据异常没有被自动抹平，后面要分类处理。

## 5. 结果一：Lifecycle 指向 1-6h 主窗口

![Lifecycle activity curve](../research/2026-07-15-pmxt-weather-next-stage-experiments/event_level/lifecycle/lifecycle_activity_curve.png)

| bucket | trades/min | spread | 120s IC | zero |
| --- | ---: | ---: | ---: | ---: |
| >24h | 0.08 | 0.010 | 0.085 | 0.724 |
| 6-24h | 0.26 | 0.006 | 0.111 | 0.709 |
| 1-6h | 0.37 | 0.003 | 0.132 | 0.681 |
| <1h | 0.13 | 0.002 | 0.089 | 0.748 |

1-6h 是当前最像主要价格发现的窗口：trades/min 最高，spread 已经明显收窄，120s IC 最高，zero 也最低。<1h 虽然 spread 更窄，但交易活跃度和 IC 回落，zero 反而上升，更像 settlement regime，而不是统一价格发现窗口。

这个结论直接影响后续实验设计：下一轮不应把所有生命周期等价混合，也不应因为临近结算 spread 窄就默认更可交易。

## 6. 结果二：Active set 改善预测，但不改善执行

![All market vs active set comparison](../research/2026-07-15-pmxt-weather-next-stage-experiments/event_level/active_set/all_market_vs_active_set_comparison.png)

| scope | IC | zero | crossing | Top3 mass |
| --- | ---: | ---: | ---: | ---: |
| all_market | 0.057 | 0.812 | -0.0074 | 0.835 |
| dynamic_active | 0.079 | 0.718 | -0.0110 | 0.929 |

dynamic active set 的方向是对的：IC 从 0.057 提升到 0.079，zero 从 0.812 降到 0.718，Top3 mass 从 0.835 升到 0.929，说明主要概率和价格发现确实集中在少数 outcome。

但它没有通过交易前置条件。crossing 从 -0.0074 恶化到 -0.0110，说明预测改善没有转化为更好的直接执行空间。当前决策是：active set 只作为状态特征和研究分层，不作为交易 gate。

## 7. 结果三：Event 概率分布有弱结构信号，但不是 Alpha

![Representative event snapshots](../research/2026-07-15-pmxt-weather-next-stage-experiments/event_level/distribution/representative_event_snapshots.png)

![Distribution signal forest](../research/2026-07-15-pmxt-weather-next-stage-experiments/event_level/distribution/distribution_signal_forest.png)

| scope | Event IC | positive Event |
| --- | ---: | ---: |
| all | 0.073 | 0.889 |
| clean | 0.075 | 0.950 |
| degraded | 0.069 | 0.812 |
| sum deviation <= 0.10 | 0.064 | 0.833 |

Event probability distribution 的结果比单 token 更接近真实问题。pressure 对 future implied-temperature move 有跨日期稳定的弱结构信号，不只是跨 cohort 弱信号。Raw median Event IC 为 0.073，Event bootstrap 90% CI 为 [0.052, 0.083]；限制到 `|sum(mid)-1| <= 0.10` 后，median Event IC 为 0.064，90% CI 为 [0.051, 0.077]，positive Event 从 0.889 降到 0.833，但信号没有消失。9 组 leave-one-date-out 全部为正，median IC 范围为 0.061-0.075；负 IC Event 有 4 个。

这只能说明结构特征值得继续研究，不能称为 Alpha。所有 crossing 仍然为负，且概率和质量边界仍然限制策略解释。缓存补充实验对概率和尾部做了敏感性归因：

| filter | snapshots / Events | P95 | >0.10 | max |
| --- | ---: | ---: | ---: | ---: |
| Raw | 60,281 / 36 | 0.135 | 7.2% | 3.935 |
| Complete outcomes | 38,105 / 23 | 0.085 | 2.9% | 2.495 |
| Complete + fresh + spread + sync | 14,616 / 22 | 0.077 | 1.4% | 0.180 |

绝大多数极端尾部随完整性、spread 与同步过滤消失；严格过滤后仍有 205 snapshots / 11 Events 的偏差 >0.10，集中于少数 Event。残余尾部需要逐 Event 报价语义核验，不能直接解释为套利，也不能带着这批数据直接进入策略回测。

## 8. 发现 - 证据 - 决策

（可以的，这里做一个简单的总结）

| 发现 | 证据 | 决策 |
| --- | --- | --- |
| PMXT 工程链路已统一 | 同一 adapter / replay contract 可服务因子研究和 Nautilus research backtest；441 Event inventory 已建立 | 保留统一 pipeline，441 Event 作为样本池，不宣称全量因子完成 |
| 单 token fixed-horizon 有方向信息但不可执行 | `depth_imbalance_1` 120s IC 0.085、900s IC 0.106；所有 crossing 为负 | 不扩大单 token 因子池，不进入直接 taker 分支 |
| 1-6h 是主要价格发现窗口 | trades/min 0.37、spread 0.003、120s IC 0.132、zero 0.681 | 后续优先围绕 1-6h 设计实验；<1h 单列 settlement regime |
| active set 能提升预测但恶化 crossing | IC 0.057 -> 0.079，zero 0.812 -> 0.718，crossing -0.0074 -> -0.0110 | active set 只作状态特征和分层，不作交易 gate |
| Event probability distribution 有跨日期稳定的弱结构信号 | Raw median Event IC 0.073，90% CI [0.052, 0.083]；sum deviation <= 0.10 后 median 0.064，90% CI [0.051, 0.077]；9 组 leave-one-date-out 全部正向，范围 0.061-0.075；4 个负 IC Event；所有 crossing 仍为负 | 保留为下一阶段 Event 内结构研究方向，不称 Alpha |
| 概率和异常尾部已完成缓存归因，但残余需逐 Event 核验 | Raw 为 60,281 / 36，P95 0.135，>0.10 占 7.2%，max 3.935；Complete outcomes 后为 38,105 / 23，P95 0.085，>0.10 占 2.9%；Complete + fresh + spread + sync 后为 14,616 / 22，P95 0.077，>0.10 占 1.4%，max 0.180；严格过滤后仍有 205 snapshots / 11 Events >0.10 | Phase1 做残余 Event 报价语义核验与 native parity，再谈回测；不能把尾部解释为套利 |

## 9. 数据与信任边界

（可以引出一个问题，就是我们的 alpha 是什么，到底干嘛的。我们到底考不考虑赚外生的 alpha 比如从天气的数据源下手，答案是暂时不考虑。然后可以给出 roadmap）

本批结果有几个边界必须写在前面。

- 概率和异常尾部已完成缓存归因：Raw 口径下 `|sum(mid)-1|` P95 为 0.135，最大 3.935，超过 0.10 的观测占 7.2%；Complete outcomes 后 P95 降到 0.085，>0.10 降到 2.9%；Complete + fresh + spread + sync 后 P95 降到 0.077，>0.10 降到 1.4%，max 降到 0.180。绝大多数极端尾部随完整性、spread 与同步过滤消失；严格过滤后仍有 205 snapshots / 11 Events 偏差 >0.10，集中于少数 Event，需要逐 Event 报价语义核验，不能解释为套利。
- 24 个 skipped 偏向早期 clean 日期和 Lucknow 等城市。token baseline 只代表可形成 ranking 的较活跃 token，不能当成全部 token 的无偏估计。
- 6 个 PMXT tick 状态冲突失败 token 尚待分类。当前没有静默修补，因此结论没有掩盖这类异常，但也不能忽略它们。
- crossing 不是 PnL。它只是一个更接近执行约束的诊断指标，不包含真实 bid/ask 路径、fees、队列、成交概率、容量、持仓和撤单逻辑。
- 本批次 native parity 未补齐，不进入 Nautilus 策略回测。research backtest plumbing 跑通不等于策略可回测，更不等于收益证明。

## 10. 决策：停止单 token 固定时间 + 直接 taker 分支

建议现在做三个明确停止：

1. 不扩 100。36 Event 已经足够暴露结构问题，继续扩大样本只会更快地产生更多同类诊断表，不会自动解决研究单位和执行边界问题。
2. 不进入 Nautilus 策略回测。native parity、残余概率尾部逐 Event 核验、tick 冲突、真实 bid/ask+fee+容量边界都还没补齐。
3. 不扩大单 token 因子池。继续加更多 L2 因子，会把研究资源投入到当前证据最弱的分支。

这不是停止 PMXT weather 项目，而是停止“单 token 固定时间 + 直接 taker”的分支。天气仍然是很好的多结果 Event 试验田：生命周期短、多 outcome 清晰、重复样本多、结算规则明确。真正应该沉淀的是通用 Event alpha 能力，而不是天气专项盘口因子。

## 11. Roadmap：先 Event 内结构性，跨 Event 延后

下一阶段建议按下面顺序推进。

| Phase | 工作 | 目标 |
| --- | --- | --- |
| Phase 1 | 残余数据异常核验与 native parity | 分类 6 个 tick 冲突；逐 Event 核验严格过滤后仍 >0.10 的 205 snapshots / 11 Events 报价语义；补齐 research replay 与 native 数据一致性 |
| Phase 2 | Event 内结构特征 | YES/NO 一致性、多 outcome 概率和、probability mass flow、Outcome 内 leader-lag、stale outcome、lifecycle |
| Phase 3 | 可执行边界 | 用真实 bid/ask、fee、容量、持续时间、成交概率区分观察信号和可执行信号 |
| Phase 4 | Nautilus 多腿回测 | 只有 Phase 1-3 通过后，再把多 outcome order intent 接入 Nautilus |
| Phase 5 | 跨 Event | 跨城市、跨日期、跨主题 leader-lag 和相关 Event 网络，等 Event 内能力稳定后再做 |

天气在这里的角色是多结果 Event 试验田，不是最终业务边界。核心能力应通用于政治、宏观、体育等 Polymarket Event。`implied temperature` 只是 weather domain 插件，可用于解释 ordered weather outcomes，不应成为通用 Event 框架的核心定义。

（这里还要加一个，就是 跟 IT 那边对的数据的进度，本来说张琦说上周能给的，结果这周一交接给了一个实习生，刚对完需求）

## 12. 会议讨论点

建议会议只讨论五个决策点：

1. 是否认可下一阶段主线切到 Event 内结构性，而不是继续扩大单 token taker 因子。
2. 是否认可 1-6h 作为主要价格发现窗口，<1h 单列为 settlement regime。
3. 是否暂停单 token taker 扩张，只把 token baseline 保留为局部特征和对照组。
4. 是否先投入数据异常、native parity 和真实可执行边界，而不是直接进 Nautilus 策略回测。
5. 是否同意只有 Phase 1-3 通过后，再投入多腿 Event-level 回测；跨 Event leader-lag 放到更后面。

当前可以对外说：PMXT 已进入统一 pipeline，441 Event inventory 已建立，36 Event 实验识别出 lifecycle、active set、Event probability distribution 三个关键结构问题，并给出下一阶段路线。

当前不能对外说：已经发现可交易 alpha，或者已有 Nautilus 策略回测收益证明。
