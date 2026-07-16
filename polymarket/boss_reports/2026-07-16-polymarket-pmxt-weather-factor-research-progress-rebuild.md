# 20260716 - Polymarket PMXT 天气因子研究进展（完整数据重跑）

> 数据重建、6 Event pilot 和 36 Event 结构实验均已完成。本轮是因子与市场结构研究，不是 PnL 回测；crossing markout 也不是实际成交收益。

## TLDR

1. PMXT 天气数据已经重新收口。新目录包含 227 个完整生命周期 Event、5 个日期、49 个城市、2,497 个二元 Market、4,994 个 outcome token，共 707,045,334 行；missing hour 和 bad file 均为 0。
2. 两套实验已经用 `events-rebuild` 完整重跑：6 Event pilot 用于检查因子、label 和实验流程，36 Event 用于验证 token baseline、lifecycle、dynamic active set 和 Event probability distribution。
3. `depth_imbalance_1` 的预测方向稳定。36 Event 中 30s / 120s / 600s median IC 分别为 0.069 / 0.084 / 0.104，三个 horizon 的 positive Event 均为 100%。但 crossing 分别为 -0.0090 / -0.0086 / -0.0083，不能直接转成 taker 策略。
4. 天气 Event 最有研究价值的窗口是结束前 6—24h，而不是旧实验认为的 1—6h。该窗口的更新、成交和 120s IC 都更高；进入 1—6h 后更新量断崖下降，<1h 已没有可用方向性 label。
5. Dynamic active set 能提高统计信号，但没有改善执行：IC 从 0.053 提高到 0.083，zero rate 从 0.855 降到 0.741，crossing 却从 -0.0064 恶化到 -0.0115。因此 active set 暂时只作为状态特征，不作为交易 gate。
6. Event distribution pressure 是目前更值得继续研究的方向：median Event IC 为 0.074，36 个 Event 全部为正，90% bootstrap CI 为 [0.070, 0.079]，5 组 leave-one-date-out 均保持正向。但它仍是弱结构信号，不是已验证 Alpha。
7. 概率和异常大部分来自 outcome 不完整、报价陈旧或不同步。修复短间隔重复 tick 通知、恢复23个 token 后，原始 `|sum(mid)-1| > 0.10` 已从上一轮的13.6%降至4.7%；严格控制完整性、新鲜度、spread 和时间同步后进一步降至1.8%。残余偏离需要逐 Event 核验，不能直接叫套利。
8. Tick-size 边界已经收敛：23个 source-time 间隔不超过8ms的重复 `0.01 → 0.001` 通知按幂等告警跳过；Wuhan 的325.489秒长间隔重复继续严格失败。396个 token 最终394个进入分析、1个 inactive token 被跳过、1个严格失败。研究结论没有发生实质变化。
9. direct replay → Nautilus native parity 因当前环境缺少 `nautilus_trader.core.data`，仍是 NOT RUN。完成 parity 前不进入正式策略回测。

## 1. PMXT 问题回顾与数据重建

### 1.1 问题来自哪里

原始链路是：

```text
Polymarket WebSocket
  -> PMXT 监听和归档
  -> PMXT hourly parquet
  -> PolyReaper 下载并按 Event 摘取
  -> PMXT adapter / replay contract
  -> factor research / Nautilus research backtest
```

旧数据中确认过两段持续缺失：`2026-06-05 09:00—2026-06-06 23:00 UTC` 共38小时，`2026-06-11 04:00—2026-06-12 00:00 UTC` 共20小时；另有一个 `2026-06-04T14` Parquet 文件曾无法读取。下载记录显示最终缺失的58个小时文件在重试后仍由 PMXT archive 返回404。

PMXT Discord 历史讨论也确认过同类问题：public archive 不是 100% coverage，历史上发生过 WebSocket / Polymarket 连接中断、部分市场未被 tracking，以及6月11日后新市场监听失败。社区还报告过 timestamp 并列和 deeper L2 replay 不一致。因此旧 PMXT 数据可以用于研究，但不能默认当成无缺口、严格有序的交易所真值流。

PolyReaper 负责下载和按 Event 摘取，并不是这些历史缺口的来源。

### 1.2 新数据口径

新数据由 PolyReaper 重新生成，路径为：

```text
C:\Projects\PolyReaper\data\curated\polymarket\events-rebuild\
```

使用 `highest-temperature-event-batch-v3` 合同。每个 Event 从 `captureStartAt` 到 `captureEndAt` 检查小时文件覆盖，只把完整生命周期 Event 纳入正式 inventory。

| 项目 | 新 inventory |
| --- | ---: |
| Event | 227 |
| 日期 | 5（6/6—6/10） |
| 城市 | 49 |
| 二元 Market | 2,497 |
| outcome token | 4,994 |
| rows | 707,045,334 |
| 单 Event rows 中位数 | 3,047,589 |
| fully covered | 227 / 227 |
| missing hour | 0 |
| bad file | 0 |

日期分布：

| Event 日期 | Event 数 |
| --- | ---: |
| 6月6日 | 49 |
| 6月7日 | 49 |
| 6月8日 | 49 |
| 6月9日 | 49 |
| 6月10日 | 31 |

实验产物中仍保留了 `29 clean / 7 degraded` 的旧字段名，但这里的7个 Event 只是观察到 `postCloseValidation.rowsObserved > 0`，并没有 missing hour 或 bad file。它们应解释为关闭时间 metadata 诊断，而不是较差数据组。本报告不使用该标签做质量分层结论。

## 2. 实验设计

### 2.1 两套实验

第一套是6 Event pilot，作用是检查：

- `depth_imbalance_1`、`microprice_minus_mid` 等基础因子；
- fixed-time label 在不同 activity 和 price bucket 下的表现；
- valid_obs label 是否值得进入下一轮；
- direct replay → Nautilus native parity 是否具备运行条件。

第二套是36 Event 结构实验。样本覆盖5个日期和36个城市，按日期及 source rows/activity 分位确定性抽取，作用是检查：

- token-level fixed-horizon baseline；
- lifecycle；
- dynamic active set；
- Event probability distribution；
- bootstrap、leave-one-date-out 和概率和异常尾部。

最终36 Event 产物记录的 replay 累计处理约2.61 CPUh，本轮增量聚合约12.5分钟。完整相关重跑实际约29分钟，部分阶段并行并复用了未受影响 token 的缓存。所有输入均已确认来自 `events-rebuild`，没有混用旧 `events` 目录。

### 2.2 因子范围

Wave 0 因子池包括：

- depth imbalance：`depth_imbalance_1`、`depth_imbalance_3`、`depth_imbalance_5`；
- 深度形状：`top_level_depth`、`depth_concentration`、`depth_slope`；
- 价格压力：`microprice_minus_mid`；
- 流动性不对称：`bid_ask_liquidity_asymmetry`；
- 状态变量：`spread`、`distance_to_zero_one`、`tick_size_regime`；
- 诊断字段：`book_staleness_seconds`、`book_update_intensity`。

6 Event pilot 对因子池做了初筛；36 Event 主实验保留 `depth_imbalance_1` 和 `microprice_minus_mid` 作为 baseline。OFI、trade pressure、cancel pressure 更依赖严格消息顺序，本轮仍不进入正式主因子池。

### 2.3 Label 口径

固定时间 label 的构造是：

- anchor 必须满足 `actual_mutation AND valid_book`；
- 在 `t+h` 之后寻找第一个同 token L2 mutation timestamp；
- 使用该 timestamp 最后一次 mutation 的 future mid；
- future mutation 不存在、future book 无效或跨越明确数据 barrier 时不生成有效 label；
- zero-return 单独统计，不静默删除。

Pilot 使用30s、120s、600s；36 Event baseline 扩展到30s、60s、120s、300s、600s、900s。主结论按 Event 等权，避免少数高频 Event 支配结果。

valid_obs10 / 50 / 200 只作诊断。obs200 相比 obs10 将 zero rate 降低23.7%，elapsed P90 为999.8秒，已经达到 amendment 候选门槛，但本轮没有改写预注册主 label。

## 3. Token baseline

### 3.1 6 Event pilot

Pilot 中相对较好的候选仍是：

1. `depth_imbalance_1`；
2. `microprice_minus_mid`；
3. `bid_ask_liquidity_asymmetry`；
4. `depth_imbalance_3`。

其中 `depth_imbalance_1` 的30s / 120s / 600s median Event IC 为0.053 / 0.076 / 0.079。Pilot 的作用是确认流程和方向，不用于 Alpha 声明。

### 3.2 36 Event baseline

| factor | horizon | Event | median IC | positive Event | zero | crossing | coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| depth_imbalance_1 | 30s | 36 | 0.069 | 1.000 | 0.784 | -0.0090 | 0.998 |
| depth_imbalance_1 | 120s | 36 | 0.084 | 1.000 | 0.691 | -0.0086 | 0.996 |
| depth_imbalance_1 | 600s | 36 | 0.104 | 1.000 | 0.552 | -0.0083 | 0.986 |
| microprice_minus_mid | 30s | 36 | 0.061 | 0.972 | 0.784 | -0.0090 | 0.998 |
| microprice_minus_mid | 120s | 36 | 0.068 | 1.000 | 0.691 | -0.0086 | 0.996 |
| microprice_minus_mid | 600s | 36 | 0.068 | 1.000 | 0.552 | -0.0083 | 0.986 |

这组结果说明盘口不平衡对未来 mid 的排序方向很稳定，而且 horizon 越长，zero rate 越低。但所有 crossing 都是负数：如果看到正信号后直接跨 ask 买入，或者看到负信号后直接打 bid 卖出，预测幅度不足以覆盖当下 spread。

因此目前可以把 token baseline 当作 Event 内部的局部状态特征，不能直接变成 taker 策略。

### 3.3 Tick-size 重跑的影响

原24个失败 token 并不属于同一种风险：

- 23个 token 的第二次 `0.01 → 0.001` 通知与第一次只相隔0—8ms；
- replay 现在对 source timestamp 间隔不超过10ms的同方向重复转换输出 warning，并把第二次通知作为 idempotent no-op 跳过；
- Wuhan 的重复通知相隔325.489秒，超过10ms边界，仍然严格失败。

边界修复后的结果是：

| 实验 | 修复前 | 修复后 |
| --- | ---: | ---: |
| 10 Event 单因子 | 7个失败 token | 110个 YES token，失败0 |
| 6 Event pilot | 1个失败 token | 失败0 |
| 36 Event | 24 token / 20 Event 受影响 | 1 token / 1 Event 受影响 |

重跑后 `depth_imbalance_1` 的120s IC 从约0.082变为0.084，600s 从约0.103变为0.104；distribution IC 从约0.076变为0.074。短重复主要造成样本缺失，没有改变研究方向或执行结论。

当前边界已经由测试锁定：短重复通过并告警，325.489秒长重复严格失败；因子协议相关测试77项通过。Nautilus bridge 的2项测试因本机缺少编译 runtime 被跳过，属于仍待完成的 parity 边界。

## 4. 三个天气市场结构问题

### 4.1 生命周期不均匀

天气 Event 持续约三天，但信息和交易并不是均匀到达。相同的120s label 在早期、主要价格发现阶段和接近结束时，代表的市场变化完全不同。

### 4.2 Outcome 活跃度不均

一个 Event 通常包含约11个温度 outcome，只有靠近当前概率中心的少数 outcome 持续活跃。把所有 outcome 无差别混在一起，会把真实信号和 stale / inactive market 混合。

### 4.3 Event 才是完整研究单位

这些 outcome 共同描述同一条温度概率分布，不是11个彼此独立的二元 Market。单 token 因子适合描述局部盘口状态，但更完整的研究对象应该是 Event 内的概率质量如何移动、集中和重新定价。

## 5. Lifecycle：主要窗口是6—24h

| bucket | Event | updates/min | trades/min | active markets | spread | 120s IC | zero | crossing | coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| >24h | 36 | 365.43 | 0.12 | 4.88 | 0.0080 | 0.080 | 0.734 | -0.0076 | 0.999 |
| 6—24h | 36 | 597.22 | 0.43 | 4.74 | 0.0030 | 0.099 | 0.636 | -0.0105 | 0.990 |
| 1—6h | 35 | 0.11 | 0.04 | 3.10 | 0.0010 | 0.065 | 0.962 | -0.0009 | 0.822 |
| <1h | 21 | 0.00 | 0.05 | 3.00 | 0.0010 | N/A | N/A | N/A | 0.000 |

本轮推翻了旧模板写死的“1—6h 主窗口”。6—24h 的更新和成交最集中，120s IC 最高，zero rate 也低于其他窗口。进入1—6h 后，订单簿更新量已经断崖下降；<1h 没有可用的方向性 label，只能作为关闭/结算边界诊断。

![Lifecycle activity](assets/2026-07-16-weather-rebuild/01-lifecycle-activity.png)

> 图1：不同 lifecycle 使用独立量纲展示。6—24h 的订单簿更新和成交最集中；1—6h 后活动迅速下降。

需要注意：lifecycle 目前以 metadata `event_end` 为锚点。由于1—6h 的更新量下降过于明显，进入下一轮前还要核验 `event_end`、实际停止交易时间、当地自然日结束和 resolution time 的关系，避免把 metadata 时差解释成市场行为。

## 6. Dynamic active set：预测改善，执行恶化

active 的定义为：存在有效 BBO，并满足当前概率 Top3、过去30分钟有成交、或过去30分钟 mutation count 排名前三之一。它只使用当时和过去信息，没有使用未来信息。

| scope | Event | IC | zero | crossing | Top3 updates | Top3 trades | Top3 probability mass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| all_market | 36 | 0.053 | 0.855 | -0.0064 | 0.489 | 0.722 | 0.875 |
| dynamic_active | 36 | 0.083 | 0.741 | -0.0115 | 0.686 | 0.722 | 0.944 |

Dynamic active set 确实集中到了更活跃、更有信息量的 outcome，但这些 outcome 同时也更难通过直接跨价获得收益。它可以继续作为 activity regime、仓位筛选或模型输入，暂时不升级为硬交易 gate。

![Active-set trade-off](assets/2026-07-16-weather-rebuild/02-active-set-tradeoff.png)

> 图2：Dynamic active set 提高 IC、降低 zero rate，但 crossing 更负。预测质量改善没有转化成 taker 可执行性。

## 7. Event probability distribution

### 7.1 Distribution pressure

Event 内先把各 outcome mid 归一化为概率分布，再观察盘口压力是否指向未来 implied-temperature mean 的移动。

| 指标 | 结果 |
| --- | ---: |
| median Event IC | 0.074 |
| positive Event | 36 / 36 |
| Event bootstrap 90% CI | [0.070, 0.079] |
| `|sum(mid)-1| <= 0.10` 子样本 IC | 0.072 |
| 子样本 bootstrap 90% CI | [0.066, 0.079] |
| leave-one-date-out | 5 / 5 保持正向 |
| leave-one-date-out median IC 范围 | 0.073—0.077 |

这说明 distribution pressure 并不是由单个日期或城市偶然贡献出来的。它目前可以称为“跨日期稳定的弱结构信号”，但仍不能称为可交易 Alpha，因为没有正 crossing，也没有成交容量和费用验证。

![Distribution signal forest](assets/2026-07-16-weather-rebuild/03-distribution-signal-forest.png)

> 图3：36个 Event 的 distribution pressure IC 全部为正。信号跨城市和日期存在，但量级仍然较弱。图中颜色沿用实验产物旧字段；橙色只表示观察到 post-close rows，不代表缺文件或较差数据质量。

![Representative Event distribution](assets/2026-07-16-weather-rebuild/04-representative-event-distribution.png)

> 图4：同一 Event 从 T-48 到 T-1 的概率分布变化示例。研究对象是整条概率分布如何迁移和收敛，而不是把11个 outcome 当成互不相关的市场。

### 7.2 概率和异常尾部

原始 Event snapshot 中，`|sum(mid)-1| > 0.10` 占4.7%，P95 为0.097，最大达到4.370。进一步控制 outcome 完整性、BBO 新鲜度、spread 和同一分钟的 source-time 同步后：

| filter | snapshots | Event | P95 | `>0.10` | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raw | 109,767 | 36 | 0.097 | 4.7% | 4.370 |
| Complete outcomes | 101,379 | 34 | 0.086 | 3.6% | 4.370 |
| Complete + fresh | 30,481 | 34 | 0.086 | 3.1% | 4.370 |
| Complete + fresh + spread + sync | 25,898 | 34 | 0.080 | 1.8% | 0.249 |

结论是：绝大多数极端偏离不是稳定套利，而是缺少 outcome、报价陈旧、spread 太大或不同步造成的截面错觉。恢复23个短重复 token 后，raw 异常占比本身已从13.6%下降到4.7%，说明上一轮相当一部分异常确实来自 outcome vector 不完整。严格过滤后仍有460个 snapshot、22个 Event 超过0.10，需要逐 Event 检查报价语义和可成交数量。

当前缓存只有 BBO 价格，没有每条腿可验证的 BBO size 和 fee rate，因此不能计算多腿最小容量，也不能把残余偏离称为套利机会。

## 8. 发现、证据和当前决策

| 发现 | 证据 | 当前决策 |
| --- | --- | --- |
| PMXT 天气数据可以进入统一研究链路 | 227 Event 完整覆盖；因子和36 Event结构实验均从 `events-rebuild` 消费 | 保留 PMXT research mode，继续使用统一 replay contract |
| 单 token 盘口方向稳定 | `depth_imbalance_1` 30s / 120s / 600s positive Event 均为100% | 作为 Event 内局部特征，不直接做 taker |
| 6—24h 是主要研究窗口 | updates、trades、120s IC 最高 | 下一轮重点研究该窗口，同时核验 lifecycle 锚点 |
| active outcome 更有预测信息 | IC 0.053 → 0.083，zero 0.855 → 0.741 | 作为状态特征，不作为交易 gate |
| Event distribution 存在稳定弱结构 | IC 0.074；bootstrap 和5组日期留一均为正 | 作为下一轮主方向 |
| crossing 全部为负 | token、lifecycle 和 active-set 结果均为负 | 停止直接 taker 路线 |
| 原始概率和异常多数不可交易 | 短重复修复后 raw 为4.7%，严格过滤后为1.8% | 核验残余22个 Event，不宣传套利 |
| 短 tick 重复主要造成样本缺失 | 23个 token 恢复后主要 IC 与执行结论基本不变 | 保留告警和10ms边界，不再整 token 丢弃 |
| 还不能进入正式 Nautilus 回测 | Wuhan 保留1个严格失败；native parity NOT RUN | 核验长重复并修复运行环境 |

## 9. 当前风险和信任边界

1. **Wuhan 长间隔 tick 重复**：396个 token 中394个进入分析、1个因无 ranking observation 被跳过、1个严格失败。唯一失败是 Wuhan market index 4：第二次 `0.01 → 0.001` 与第一次相隔325.489秒，不能按短时传输抖动忽略。程序继续 fail closed，因此 Wuhan 的 Event probability vector 仍不完整。
2. **Nautilus parity 未运行**：当前 Python 环境缺少 `nautilus_trader.core.data` 编译扩展。因此本轮只证明 factor/event-level replay 结果，没有证明同一批 rebuild 数据经过 native bridge 后完全一致。
3. **crossing 不是 PnL**：当前 crossing 只比较未来 mid 与当下 bid/ask，没有手续费、延迟、盘口数量、排队和 partial fill。
4. **lifecycle 锚点需要核验**：6—24h 到1—6h 的更新量下降非常大。当前以 metadata `event_end` 为锚，仍需和实际停止交易、当地自然日及 resolution time 对齐。
5. **post-close 标签不能叫 degraded**：实验中7个 Event 的旧标签只表示存在 post-close rows，不表示缺文件或坏数据；后续产物应改名为 closure diagnostic。
6. **日期只有5个**：bootstrap 和 leave-one-date-out 支持当前样本稳定，但不能替代更长日期跨度。
7. **新旧结果不是严格配对**：旧实验和 rebuild 的日期、Event 组成不同，不能把数值变化全部归因于数据清理。
8. **runner 默认路径仍需收口**：本次运行通过显式 inventory 验证只消费了 `events-rebuild`，但 `run_pilot.py` 和 `run_event_level.py` 的代码默认值仍指向旧 inventory。后续必须改成 rebuild inventory 并在启动时 fail closed，避免误跑旧数据。

## 10. 还需要补的实验

### P0：进入 Nautilus 策略回测之前

1. **Wuhan 长重复收口**
   - 保留当前超过10ms严格失败的边界；
   - 核验 Wuhan 两次通知之间的 book、price 和其他 tick 状态；
   - 判断它是延迟重复、源端重放，还是确实缺失了中间状态转换；
   - 在没有证据前不扩大10ms容忍窗口。

2. **direct replay → Nautilus native parity**
   - 在可导入 `nautilus_trader.core.data` 的编译环境运行；
   - 固定 Event / token 核对 mutation 数、replay clock、BBO、TradeTick、tick timeline 和 settlement；
   - parity 通过后再进入 BacktestEngine 策略回测。

3. **lifecycle 锚点审计**
   - 对比 metadata `event_end`、实际最后活跃时间、当地自然日结束和 resolution time；
   - 画出相对不同锚点的 updates/trades 曲线；
   - 确认6—24h 是真实价格发现窗口，而不是 metadata 时差。

4. **post-close rows 敏感性**
   - 删除 clean/degraded 命名；
   - 比较保留 post-close rows 与严格截断后的结果；
   - 若差异不显著，只保留为数据诊断字段。

5. **运行入口固定新 inventory**
   - 将 pilot 和 event-level runner 的默认路径改为 `events-rebuild` inventory；
   - 启动时记录并校验数据根路径、inventory hash、missing hour 和 bad file；
   - 如果输入指向旧 inventory，直接拒绝运行。

### P1：下一轮研究抓手

6. **Event 内结构信号**
   - 以6—24h 为主窗口；
   - 研究 leader-lag、probability mass flow、stale outcome 和局部盘口压力向相邻 outcome 的传导；
   - active set 作为解释变量，不作为预设交易 gate。

7. **可执行边界**
   - 分 passive / taker；
   - 加入 BBO size、fee、延迟、queue 和 partial fill；
   - 当前 crossing 全负，因此 taker 分支只做反证，不作为主路线。

8. **残余概率和偏离核验**
   - 逐一检查严格过滤后仍异常的10个 Event；
   - 核对 outcome 是否完备、价格是否来自同一时点、是否有足够可成交数量；
   - 只有 fee 后且容量可验证时，才升级为套利研究。

### P2：结果稳定后

9. 扩展到100+ Event 或更长滚动日期；
10. 做同 Event 旧数据/rebuild 配对，量化数据清理影响；
11. 天气结构稳定后再扩到其他 Event 类型；
12. 跨 Event 因子继续后置。

## 11. 研究方向讨论

### 11.1 先研究市场内部，而不是先做天气基本面

天气数据源、预报模型和城市气象差异当然可能提供信息，但如果现在直接从这些外部数据入手，研究很容易收窄成一个天气专项。我们的目标是寻找能够迁移到更多 Polymarket Event 的方法，因此下一阶段优先研究 **endogenous market alpha**：只使用市场自身产生的价格、盘口、成交和不同 outcome 之间的关系，判断市场内部的信息如何传播和重新定价。

这不等于永远不做基本面。更合理的顺序是：

```text
先确认市场内部存在稳定、可执行的结构
  -> 再引入外部信息判断它是否提供增量
  -> 最后比较纯市场模型与市场+基本面模型
```

天气 Event 适合作为第一个研究载体，因为它同时具备多个互斥 outcome、明确生命周期和最终结算。但最终方法不应该依赖“这是天气市场”才能成立。

### 11.2 方向一：Event 内概率质量迁移

当前最有证据支持的方向是 Event probability distribution。单个 outcome 的盘口压力不仅可能推动自身价格，也可能意味着概率质量正在向相邻温度区间迁移。

下一步可以研究：

- implied-temperature mean、variance、entropy 的变化；
- 概率质量从低温 outcome 向高温 outcome，或反方向的迁移速度；
- 某个 outcome 出现压力后，相邻 outcome 是否按固定次序响应；
- 分布收窄、扩宽和整体平移是否对应不同市场状态；
- distribution pressure 在6—24h 主窗口是否显著强于其他窗口。

这一方向比继续堆单 token 因子更重要，因为它直接利用了 Polymarket 多 outcome Event 的结构，也是当前 median Event IC 0.074 最自然的延伸。

### 11.3 方向二：Event 内相对价值和 stale outcome

同一个天气 Event 的 outcome 应组成一条相对连续、近似单峰的概率分布。如果某个 outcome 更新较慢，而相邻 outcome 已经发生变化，它可能成为短暂的 stale leg。

可以重点检查：

- outcome A 的盘口或成交是否领先相邻 outcome B；
- 相邻 outcome 之间的价格变化是否存在稳定 lead-lag；
- 某个 outcome 的报价是否偏离相邻 outcome 隐含的局部曲线；
- 概率中心移动时，哪些 outcome 通常先动、哪些通常后动；
- 严格同步后仍存在的概率和偏离，是否来自可识别的 stale outcome。

这里的目标不是看到 `sum(mid) != 1` 就做多腿套利，而是先确定偏离来自真实的异步定价，还是缺腿、陈旧报价和宽 spread。

### 11.4 方向三：盘口信号如何转成被动执行

`depth_imbalance_1` 的方向稳定，但 crossing 全负，说明“看对方向”并不等于“可以直接跨价成交”。因此执行研究应该从 taker 转向 passive / maker：

- 信号出现后挂在 best bid / best ask，观察后续 adverse selection；
- 比较挂单等待时间、成交概率和信号衰减速度；
- 引入 BBO size、queue proxy、partial fill、cancel-before-fill 和 fee；
- 检查6—24h、不同 spread 和不同概率区间下的 maker 条件；
- 比较单腿 maker 与 Event 内相对价值多腿执行。

这一层通过 Nautilus BacktestEngine 统一处理订单、fill、position 和账户状态；研究脚本只负责产生信号与执行假设，保证研究和回测的职责边界一致。

### 11.5 Activity 和 lifecycle 应作为 regime，而不是简单过滤器

Dynamic active set 提高 IC、却恶化 crossing，说明活跃市场不是天然更赚钱。后续不应简单规定“只交易最活跃的三个 outcome”，而应该把以下字段作为 regime 或模型输入：

- 距离 Event 结束的时间；
- 当前活跃 outcome 数；
- Top3 probability mass；
- mutation / trade intensity；
- spread、book staleness 和 tick regime；
- 当前概率中心与目标 outcome 的距离。

模型需要回答的是“这个信号在什么状态下有效、适合 maker 还是应当放弃”，而不是仅做一次静态样本筛选。

### 11.6 暂时后置的方向

以下方向现在不是主线：

- **天气基本面模型**：可能有效，但容易把方法做窄；待市场内结构形成 baseline 后再评估增量。
- **跨 Event 因子**：时间对齐、共同信息源和因果关系更复杂，先把单 Event 内部结构做清楚。
- **继续堆单 token 因子**：当前主要障碍是执行和 Event 结构，不是候选因子数量不足。
- **直接 taker 策略**：现有 crossing 已经给出一致负证据，不应在没有新执行假设时继续投入。
- **立即扩到100+ Event**：Wuhan 长重复、lifecycle 锚点和 native parity 尚未收口，先扩样本只会放大口径问题。

### 11.7 建议会上讨论的三个决策

1. **下一阶段主线是否确定为 Event 内结构性信号**：以 probability mass flow、相邻 outcome lead-lag 和 stale outcome 为核心，跨 Event 延后。
2. **执行路线是否以 maker 为主**：接受更复杂的 queue / fill 校准，还是仍要求寻找能够覆盖 spread 的 taker 信号。
3. **天气市场承担什么角色**：把天气作为通用多 outcome 方法的试验场，还是投入外部天气数据，把它发展成独立垂直策略。

当前建议是：先做 Event 内结构性信号，执行以 maker 验证为主；天气基本面只作为后续增量实验，不作为当前研究主轴。

## 12. Roadmap

```text
Wuhan 长间隔 tick 重复收口
  -> lifecycle 锚点与 post-close 敏感性
  -> direct/native parity
  -> Nautilus research backtest
  -> Event 内结构信号
  -> passive fill / fee / capacity
  -> 扩日期和 Event 类型
```

本轮不扩100 Event，也不进入正式 Nautilus 策略回测。当前更重要的是把 Wuhan 长间隔重复和 lifecycle 时间锚点解释清楚，并证明 rebuild replay 与 Nautilus native data 完全一致。

## 13. 对外口径

当前可以说：

- PMXT 天气数据已经整理成可重复消费的完整 Event inventory，因子研究和 Event-level 研究使用同一套 replay 顺序；
- 单 token 盘口不平衡存在跨 Event 稳定的预测方向，但直接跨价不可执行；
- 天气 Event 的主要研究窗口在结束前6—24h；
- Event probability distribution 存在跨日期稳定的弱结构信号，值得作为下一轮主方向。

当前不能说：

- 已经发现可交易 Alpha；
- 概率和偏离就是套利；
- dynamic active set 可以直接作为交易 gate；
- 已经完成 Nautilus 原生回测或证明策略收益。

## 14. 结果位置

```text
polymarket/research/2026-07-15-pmxt-weather-next-stage-experiments-rebuild/
  README.md
  report/
    pilot_report.md
    boss_scorecard.md
  event_level/reports/
    event_level_summary.md
    cached_supplement_report.md
    boss_scorecard.md
```

旧数据口径报告保存在：

```text
polymarket/boss_reports/archive/
  2026-07-15-polymarket-pmxt-weather-factor-research-progress-pre-rebuild.md
```
