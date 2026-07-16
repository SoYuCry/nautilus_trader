# 20260716 - Polymarket PMXT 天气因子研究进展（完整数据重跑）

> 状态：工作稿。数据 inventory 和 10 Event 单因子 smoke 已完成；36 Event 的 lifecycle / active set / Event distribution、crossing 和 Nautilus parity 尚未重跑。旧数据口径报告已归档，不把旧实验数字直接搬到本报告。

## TLDR

1. PMXT 原始 archive 的问题已经查清：历史小时文件存在 404、损坏、监听中断和顺序歧义。PolyReaper 只是下载和摘取 Event，不是缺口的来源。
2. 数据层已重新收口。新目录只保留生命周期完整的天气 Event：227 Event、5 个日期、49 个城市、2,497 个二元 Market、4,994 个 outcome token，共 707,045,334 行；227 个 Event 均为 `fullyCovered=true`，没有 missing hour 和 bad file，不再设置 `degraded_robustness` 数据组。
3. 新数据上的第一轮 10 Event smoke 仍复现了 `depth_imbalance_1` 的正方向，但强度低于旧实验：30s / 120s / 600s median Event IC 分别为 0.0622 / 0.0691 / 0.0896，10 个 Event 均为正。
4. 这还不足以替换旧报告的全部研究结论。当前只验证了单因子方向，没有重新验证 lifecycle、dynamic active set、Event probability distribution、crossing 和 Nautilus 回测。
5. 下一步不是继续扩因子，而是先完成同 Event 配对实验、36 Event 全流程重跑、7 个 tick-size 冲突分类和 native parity。完成这些以后，才能判断旧报告中“1-6h 主窗口”和“Event 内结构信号”是否保留。

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

旧数据中确认过两段持续缺失：`2026-06-05 09:00—2026-06-06 23:00 UTC` 共 38 小时，`2026-06-11 04:00—2026-06-12 00:00 UTC` 共 20 小时；另有一个 `2026-06-04T14` Parquet 文件曾无法读取。下载记录显示最终缺失的 58 个小时文件在重试后仍由 PMXT archive 返回 404。

PMXT Discord 历史讨论也确认过同类问题：public archive 不是 100% coverage，历史上发生过 WebSocket / Polymarket 连接中断、部分市场未被 tracking，以及 6 月 11 日后新市场监听失败。社区还报告过 timestamp 并列和 deeper L2 replay 不一致。因此旧 PMXT 数据可以用于研究，但不能默认当成无缺口、严格有序的交易所真值流。

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
| 6 月 6 日 | 49 |
| 6 月 7 日 | 49 |
| 6 月 8 日 | 49 |
| 6 月 9 日 | 49 |
| 6 月 10 日 | 31 |

本轮不再使用 `degraded_robustness` 作为数据组。数据不满足完整性门槛时，应在数据层进入 rejected / quarantine 清单，而不是进入正式因子样本后再做解释。

## 2. 因子和 Label 口径

### 2.1 因子范围

旧报告定义的 Wave 0 因子池保持不变：

- depth imbalance：`depth_imbalance_1`、`depth_imbalance_3`、`depth_imbalance_5`；
- 深度形状：`top_level_depth`、`depth_concentration`、`depth_slope`；
- 价格压力：`microprice_minus_mid`；
- 流动性不对称：`bid_ask_liquidity_asymmetry`；
- 状态变量：`spread`、`distance_to_zero_one`、`tick_size_regime`；
- 诊断字段：`book_staleness_seconds`、`book_update_intensity`。

OFI、trade pressure、cancel pressure 依赖更严格的消息顺序，本轮仍不进入正式主因子池。

当前新数据只重跑了 `depth_imbalance_1`。其他因子和组合结果尚未生成。

### 2.2 Label 构造

Label 仍沿用旧实验定义：

- 固定时间 horizon 为 30s、120s、600s；
- anchor 必须满足 `actual_mutation AND valid_book`；
- 在 `t+h` 之后寻找第一个同 token L2 mutation timestamp；
- 使用该 timestamp 最后一次 mutation 的 future mid；
- future mutation 不存在、future book 无效或跨越明确数据 barrier 时不生成有效 label；
- zero-return 单独统计，不静默删除。

本轮仅使用 YES token，先在 Event 内对 token 等权，再对 Event 等权，避免 YES / NO 机械互补造成伪重复样本。

## 3. 新数据上的 10 Event 单因子结果

本轮首先运行小批量 smoke，不是确认性实验。选取规则是按 `rows_written` 从小到大取前 10 个完整 Event，因此样本存在明确的低行数选择偏差。

| horizon | Event | median Event IC | positive Event | median hit rate | median top-bottom |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 30s | 10 | 0.0622 | 100% | 0.582 | 0.00042 |
| 120s | 10 | 0.0691 | 100% | 0.566 | 0.00050 |
| 600s | 10 | 0.0896 | 100% | 0.570 | 0.00183 |

与旧数据小实验对照：

| horizon | 旧数据 median IC | rebuild median IC | 变化 |
| ---: | ---: | ---: | ---: |
| 30s | 0.0868 | 0.0622 | -0.0246 |
| 120s | 0.1115 | 0.0691 | -0.0424 |
| 600s | 0.1326 | 0.0896 | -0.0430 |

方向一致性比旧样本更整齐，但 IC 和 top-bottom 整体下降。因为新旧实验选中的 Event 不相同，这个差异不能直接解释成“清理数据后 Alpha 下降”；它同时混入了样本选择变化。要回答数据清理本身造成了什么影响，必须在同一组 Event 上做配对重跑。

输出位置：

```text
polymarket/research/2026-07-15-pmxt-weather-factor-wave0-rebuild/
  outputs/depth_imbalance_1_rebuild_10/
```

## 4. 当前仍然面对的三个研究问题

完整数据解决了“中间缺小时”的数据风险，但没有自动解决天气 Event 的市场结构问题。

### 4.1 Lifecycle

天气 Event 通常持续约三天，临近结算时更新和成交明显增加。相同的 120s label 在早期可能没有新信息，在临近结算时可能跨过大量盘口变化。旧实验认为 1—6h 是主要价格发现窗口，但新数据尚未重跑，当前只能保留为待验证假设。

### 4.2 Outcome 活跃度不均

一个 Event 包含约 11 个温度 outcome，通常只有靠近当前概率中心的少数 outcome 活跃。把所有 outcome 无差别放在一起会混入 stale / inactive market。旧实验的 dynamic active set 提高了 IC、但恶化了 crossing；新数据尚未验证这一结论。

### 4.3 研究单位

天气市场本质上是一个 Event probability distribution，而不是 11 个互不相关的二元 Market。旧实验中 Event distribution pressure 存在弱正向结构信号，但仍未证明可交易。新数据需要重新检验该信号是否仍然跨日期稳定。

## 5. Lifecycle 结果：等待新数据重跑

旧报告中的 1—6h 结论来自包含 degraded 数据的 36 Event 实验。本报告不直接复用旧图和旧数字。

新实验至少需要重新输出：

- 各 lifecycle bucket 的 updates/min、trades/min 和 active market count；
- spread、120s IC、zero rate；
- 每个日期和城市的分布，而不只是全样本中位数；
- `<1h` 是否仍应单列 settlement regime。

当前状态：**pending**。

## 6. Active set 结果：等待新数据重跑

active 的定义保持为：有效 BBO，并满足当前概率 Top3、过去 30 分钟有成交、或过去 30 分钟 mutation count 排名前三之一。dynamic 表示每分钟用当时及过去信息重新计算，不是永久 token 名单。

需要在新数据上重新比较：

| scope | IC | zero | crossing | Top3 mass |
| --- | ---: | ---: | ---: | ---: |
| all_market | pending | pending | pending | pending |
| dynamic_active | pending | pending | pending | pending |

只有 IC、zero 和 crossing 同时改善，active set 才能升级成交易 gate；否则仍只作为状态特征和研究分层。

## 7. Event probability distribution：等待新数据重跑

需要重新生成 T-48 / T-24 / T-6 / T-1 probability snapshots，并复核：

- distribution pressure 对 future implied-temperature move 的 Event IC；
- positive Event 比例和 bootstrap 区间；
- leave-one-date-out 稳定性；
- `sum(mid)` 异常尾部是否在完整数据中显著收敛；
- crossing 是否仍全部为负。

当前状态：**pending**。在结果完成前，不延续旧报告的 Event IC 0.073，也不称为 Alpha。

## 8. 当前可以下的结论

| 结论 | 证据 | 当前动作 |
| --- | --- | --- |
| 数据层可以不再使用 degraded cohort | 227 Event 全部 fully covered；missing hour=0；bad file=0 | 正式实验只消费完整 inventory |
| `depth_imbalance_1` 正方向在新数据小样本中复现 | 10 Event 的 30s / 120s / 600s median IC 均为正，positive Event=100% | 保留为 baseline，不升级为 Alpha |
| 新数据上的信号强度低于旧小实验 | 120s IC 从 0.1115 降到 0.0691 | 做同 Event 配对实验，拆分数据与样本效应 |
| 旧的 lifecycle / active set / distribution 结论尚未被新数据确认 | 新目录尚无 36 Event 输出 | 不在本报告中引用旧图作为新结论 |
| 还不能进入策略收益汇报 | 没有新 crossing、native parity 和 Nautilus 策略回测 | 先补执行诊断和 parity |

## 9. 当前风险和口径问题

1. **10 Event 选择偏差**：按 source rows 最少选取，不是随机样本或确认集。
2. **新旧对比不配对**：当前旧/新 IC 来自不同 Event，无法归因数据清理的影响。
3. **tick-size 冲突**：10 Event 中有 7 个 token 因 `tick_size_change.old_tick_size` 与 replay 当前状态冲突而被拒绝，没有静默修补。
4. **inventory 口径尚未完全统一**：Wave 0 inventory 将 227 Event 全部标为 primary；next-stage 临时 inventory 又因 39 个 Event 观察到 post-close rows，将其标成 degraded。post-close observation 是 metadata / closure-time 诊断，不等于历史数据缺口，不应复用 `degraded_robustness` 名称。
5. **next-stage 脚本默认路径仍指向旧 inventory**：正式重跑前必须显式切换并加入断言，防止重新消费旧 441 Event 数据。
6. **没有新 crossing**：IC 只能说明排序方向，不能说明跨价后可执行。
7. **没有新 Nautilus parity**：因子 replay 与 BacktestEngine 消费顺序尚未在 rebuild 数据上重新核验。

## 10. 还需要补的实验

### P0：开始 36 Event 之前必须完成

1. **统一 rebuild inventory**
   - 唯一正式 inventory 只包含完整 Event；
   - 删除实验代码中的 clean / degraded 分支；
   - post-close rows 单列 `closure_metadata_diagnostic`，不映射为 degraded；
   - runner 启动时断言数据根目录为 `events-rebuild`，missing hour 和 bad file 均为 0。

2. **7 个 tick-size 冲突分类**
   - 判断是 PMXT 消息重复/乱序、初始 tick 推断错误，还是 adapter 状态机问题；
   - 给出每个 token 的事件时间线；
   - 修复后重跑同一 10 Event，不能简单删除失败 token。

3. **同 Event 配对实验**
   - 从旧数据和 rebuild 都存在的 Event 中固定同一批 slug；
   - 使用同一代码、因子、horizon 和权重；
   - 表格展示 rows、有效 label、zero、IC、top-bottom 的逐 Event 差值；
   - 用来回答“信号下降来自数据清理还是样本变化”。

### P1：Boss report 收口所需

4. **36 Event 全流程重跑**
   - 按 5 个日期和 activity quantile 分层；
   - 重跑 `depth_imbalance_1`、`microprice_minus_mid`；
   - 重跑 fixed-time / valid-obs label 诊断；
   - 产出 lifecycle、active set、Event distribution 和概率和尾部结果；
   - 图表沿用旧报告结构，方便新旧口径对照。

5. **执行边界**
   - 重新计算 crossing；
   - 分 lifecycle、spread、activity、price bucket 展示；
   - crossing 仍为负时，不进入直接 taker 分支。

6. **direct replay → Nautilus native parity**
   - 固定一组 Event / token；
   - 核对 mutation 数、replay clock、BBO、TradeTick、tick timeline 和 settlement；
   - parity 通过后再跑 Nautilus 策略回测。

### P2：结果稳定后再做

7. 扩展到 100+ Event 或滚动日期验证；
8. Event 内 leader-lag、probability mass flow 和 stale outcome；
9. passive fill / queue / fee / capacity 模型；
10. 跨 Event 结构因子。

## 11. Roadmap

```text
完整数据 inventory 收口
  -> tick-size 冲突分类
  -> 同 Event 新旧配对
  -> 36 Event 因子与 label 重跑
  -> lifecycle / active set / Event distribution
  -> crossing 执行诊断
  -> direct/native parity
  -> Nautilus research backtest
```

当前 stop condition 很明确：36 Event 新结果、tick-size 冲突和 native parity 没完成之前，不把 10 Event IC 写成策略结论，也不使用旧数据的回测数字填充新报告。

## 12. 对外口径

当前可以说：PMXT 天气历史数据已经重新整理为 227 个完整生命周期 Event，数据层不再把存在缺口的 Event 混入正式样本；单因子方向在 10 Event smoke 中复现，但强度低于旧样本。

当前不能说：旧报告中的 lifecycle、active set 和 Event distribution 结论已经被新数据确认，或者已经有 Nautilus 策略收益证明。
