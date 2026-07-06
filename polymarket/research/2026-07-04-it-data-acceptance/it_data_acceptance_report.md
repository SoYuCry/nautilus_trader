# Polymarket IT 数据交付验收草案

日期：2026-07-04  
位置：`polymarket/research/2026-07-04-it-data-acceptance/`  
状态：工程对接草案，不是 boss report。

## 0. 目的

这份文档用于和 IT / 数据侧对齐 Polymarket 回测数据交付标准。目标不是让数据侧适配任意研究想法，而是先固定一套能支撑当前 Nautilus-native 回测框架的最小可靠数据合同。

当前回测框架已经有两个核心数据边界：

1. 行情回放数据：`PolymarketL2DatasetV1`
2. 市场元数据快照：`MarketMetadataV1`

验收时要回答两个问题：

1. 数据能不能不看未来、按真实接收顺序回放盘口和成交？
2. 回测时需要的市场规则、费用、tick size、结算和语义信息有没有随数据一起落盘，而不是事后靠人工补？

## 1. 当前回测框架真正需要什么

### 1.1 行情回放数据

当前框架消费的是：

```text
PolymarketL2DatasetV1
  metadata
    market_metadata[]
  steps[]
    sequence
    timestamp_received
    timestamp
    updates[]
```

其中：

- `step` 是原子回放单元；
- 对 live raw WebSocket 来说，一条 WS message 就是一个 step；
- 一个 step 里可以有多个 update；
- 回测时钟使用 `timestamp_received`；
- Polymarket/source `timestamp` 只做诊断，不用于排序和回放。

这样设计的原因：

1. Polymarket 一条 `price_change` message 里可能有多个 `price_changes[]`；
2. 如果把它们扁平化成行，并丢掉 message boundary，会导致回放语义不清；
3. PMXT 上遇到的 BBO mismatch，很大一部分就是 row-level 校验和 message-level/bucket-level 语义混在一起；
4. receive order 才是本地能证明的回测顺序，source timestamp 可能有未来时间、晚到、倒序或时钟偏差。

### 1.2 市场元数据快照

行情数据本身不够。回测还需要市场元数据，例如：

```text
MarketMetadataV1
  condition_id
  token_id
  maker_fee
  taker_fee
  fee_source
  category
  minimum_tick_size
  resolution_status
  token payout
```

当前最先使用的是 `taker_fee`。Polymarket fee 不是简单 notional 费，而是：

```text
fee = qty * fee_rate * price * (1 - price)
```

所以数据侧不需要帮我们算每笔 fee，但必须提供当时这个 market 的有效 `fee_rate`，最好来自 `feeSchedule.rate` 或等价官方字段。

同理，tick size 和 settlement 也应该属于 v1 数据合同：

- tick size change 是官方市场机制，不是研究增强项；
- settlement / final payout 是预测市场最终 PnL 的基础，不是后续锦上添花。

## 2. 建议 IT 交付目录组织

推荐按 event / condition / 时间窗口组织，不建议只按单 token 裸文件散落。

建议形态：

```text
polymarket_delivery/
  manifest.json
  events/
    <event_slug_or_event_id>/
      event_metadata.json
      markets/
        <condition_id>/
          market_metadata.json
          resolution_metadata.json
          replay/
            2026-06-26T02.ndjson
            2026-06-26T03.ndjson
```

如果数据量较大，可以压缩：

```text
2026-06-26T02.ndjson.zst
```

或者使用支持嵌套结构的 parquet，但必须保留 step/message boundary，不能只给扁平化 row 后的 price/size。

### 2.1 为什么不建议只按 token 切

短期单 token 策略可以跑，但长期研究会需要：

- YES/NO 互补；
- 同一个 condition 下两个 outcome 的联合盘口；
- 同一个 event 下多个 mutually exclusive markets；
- 跨 market 的一致性/套利/因子研究。

如果一开始只按 token 分散存储，会很难恢复 event-level 语义和同一条 WS message 的原子边界。

可以接受按 token 做衍生视图，但原始交付最好仍然保留 event/condition/time-window 口径。

## 3. 行情文件标准

### 3.1 一行一个 step

推荐 NDJSON：一行一个 replay step。

示例：

```json
{
  "sequence": 123,
  "timestamp_received": "2026-06-26T02:25:28.635472Z",
  "timestamp": "2026-06-26T02:25:28.600Z",
  "updates": [
    {
      "event_type": "price_change",
      "market": "0x...",
      "asset_id": "123...",
      "side": "BUY",
      "price": "0.40",
      "size": "100"
    },
    {
      "event_type": "price_change",
      "market": "0x...",
      "asset_id": "123...",
      "side": "SELL",
      "price": "0.60",
      "size": "80"
    }
  ]
}
```

### 3.2 必须字段

#### step-level

| 字段 | 必须 | 说明 |
| --- | --- | --- |
| `sequence` | 是 | 本地采集顺序，严格递增。 |
| `timestamp_received` | 是 | collector 收到消息的 UTC 时间，回测时钟。 |
| `timestamp` | 强烈建议 | Polymarket/source timestamp，只用于诊断。 |
| `updates` | 是 | 一个或多个原子 update。 |

#### update: `book`

| 字段 | 必须 | 说明 |
| --- | --- | --- |
| `event_type` | 是 | `book` |
| `market` | 是 | condition id |
| `asset_id` | 是 | token id |
| `bids` | 是 | `[price, size]` levels |
| `asks` | 是 | `[price, size]` levels |

#### update: `price_change`

| 字段 | 必须 | 说明 |
| --- | --- | --- |
| `event_type` | 是 | `price_change` |
| `market` | 是 | condition id |
| `asset_id` | 是 | token id |
| `side` | 是 | `BUY` / `SELL` |
| `price` | 是 | price level |
| `size` | 是 | updated size；`0` 表示删除该价位 |

#### update: `trade`

| 字段 | 必须 | 说明 |
| --- | --- | --- |
| `event_type` | 是 | `trade`，由 `last_trade_price` 等价映射 |
| `market` | 是 | condition id |
| `asset_id` | 是 | token id |
| `side` | 是 | aggressor side，如果源数据可得 |
| `price` | 是 | trade price |
| `size` | 是 | trade size |

#### update: `tick_size_change`

`tick_size_change` 是 v1 必须保存的官方市场事件。

| 字段 | 必须 | 说明 |
| --- | --- | --- |
| `event_type` | 是 | `tick_size_change` |
| `market` | 是 | condition id |
| `asset_id` | 是 | token id |
| `old_tick_size` | 是 | 旧 minimum price increment |
| `new_tick_size` | 是 | 新 minimum price increment |

当前回测 bridge 默认遇到 `tick_size_change` 会 fail fast，因为还没有完整 instrument epoch model。但数据侧仍然必须保留该事件，不能丢。

## 4. 市场元数据标准

建议每个 condition 至少一个 `market_metadata.json`；resolved 后应有 `resolution_metadata.json` 或等价字段。

示例：

```json
{
  "condition_id": "0x...",
  "event_id": "...",
  "event_slug": "highest-temperature-in-shanghai-on-june-9-2026",
  "question": "...",
  "tokens": [
    {"token_id": "123...", "outcome": "Yes"},
    {"token_id": "456...", "outcome": "No"}
  ],
  "minimum_tick_size": "0.01",
  "feeSchedule": {"rate": "0.05"},
  "maker_fee": "0",
  "taker_fee": "0.05",
  "fee_source": "clob_market_info.feeSchedule.rate",
  "category": "weather",
  "delay_ms": 0,
  "delay_source": "clob_market_info",
  "resolution_status": "unresolved",
  "as_of_time": "2026-06-26T02:25:00Z"
}
```

resolved 后 token-level payout 示例：

```json
{
  "condition_id": "0x...",
  "resolution_status": "resolved",
  "resolution_time": "2026-06-10T12:00:00Z",
  "oracle": "UMA",
  "voided": false,
  "tokens": [
    {"token_id": "123...", "outcome": "Yes", "payout": "1", "winner": true},
    {"token_id": "456...", "outcome": "No", "payout": "0", "winner": false}
  ],
  "resolution_source": "gamma_or_clob_or_settlement_snapshot",
  "as_of_time": "2026-06-10T12:05:00Z"
}
```

注意：不要只存 `final_result = Yes`。最好直接存 `token_id -> payout`，因为后面可能遇到 void、争议、特殊裁决、多 outcome、negRisk 等情况。

### 4.1 P0 元数据

P0 是没有就会影响回测真实性、join 或最终 PnL 的字段：

| 字段 | 说明 |
| --- | --- |
| `condition_id` | CLOB market id / condition id。 |
| `token_id` | outcome token id。 |
| `outcome` | Yes / No 或具体 outcome label。 |
| `event_id` / `event_slug` | event-level join 和研究分组。 |
| `question` / `description` | 人类可读语义。 |
| `minimum_tick_size` | 初始 tick size。 |
| `tick_size_change` coverage | 官方动态 tick 事件是否保留；事件本身在 replay stream，metadata/report 记录覆盖情况。 |
| `feeSchedule.rate` 或 `taker_fee` | Polymarket fee model 必需。 |
| `fee_source` | 证明 fee 从哪里来。 |
| `resolution_status` | unresolved / resolved / void / disputed 等。 |
| `token payout` | settlement/final result；resolved 后必须能按 token_id 得到 payout。 |
| `as_of_time` | 元数据快照时间。 |

### 4.2 P1 元数据

P1 是会影响更真实回测或研究解释，但第一版可以先不进入核心执行路径的字段：

| 字段 | 说明 |
| --- | --- |
| `delay_ms` / delay config | 市价单 delay / taker delay 建模。 |
| `category` / tags | fee schedule、研究分组。 |
| `end_time` / `expiration` | 持仓期限、资金占用。 |
| `resolution_rules` | 裁决文本和 dispute 解释；P0 先要求 token payout，完整 rules 可后补。 |
| `neg_risk` / related markets | 组合市场、互斥关系研究。 |

### 4.3 P2 元数据

P2 是策略研究会用，但第一阶段不是硬依赖：

| 字段 | 说明 |
| --- | --- |
| external event data | 体育比分、天气实况、宏观数据等。 |
| reward / rebate pool details | 做市收益建模。当前暂不建模 reward/rebate。 |

## 5. 验收检查项

数据到手后，至少跑以下检查。

### 5.1 文件结构检查

- 是否有 manifest；
- 是否能从 event 定位到 condition；
- 是否能从 condition 定位到 token；
- 行情文件和 metadata 文件是否能 join；
- resolution metadata 是否能 join；
- 时间窗口是否连续；
- 是否存在重复文件、空文件、缺小时。

### 5.2 行情回放检查

硬失败：

1. `sequence` 不严格递增；
2. `timestamp_received` 倒序；
3. 缺 `timestamp_received`；
4. 缺 `updates`；
5. 遇到未知 `event_type` 但没有显式记录；
6. `price_change` 缺 `price/size/side`；
7. `book` 缺 bids/asks；
8. `tick_size_change` 缺 old/new tick size；
9. `size < 0`；
10. price 不在 `[0, 1]`；
11. `market + asset_id` 无法和 metadata join。

警告但不一定失败：

1. source `timestamp` 缺失；
2. source `timestamp` 倒序；
3. source `timestamp` 晚于 `timestamp_received`；
4. source delay 过大；
5. book snapshot 和本地 replay book 不一致；
6. trade print 和当时 BBO 对不上。

### 5.3 元数据检查

硬失败：

1. 缺 `condition_id`；
2. 缺 `token_id`；
3. 缺 fee source 且实验要求 fee-aware；
4. `taker_fee` 不能 parse 成 decimal；
5. tick size 不能 parse 成 decimal；
6. resolved market 缺 token-level payout；
7. 同一 `condition_id + token_id` 出现冲突 metadata，且无 as-of 版本说明。

警告：

1. category 缺失；
2. delay 缺失；
3. resolution rules 缺失。

## 6. 为什么不靠排序修复数据

回测的核心是不能看未来。如果发现 `timestamp_received` 倒序，不能简单 sort 一下继续跑，因为 sort 会改变本地可观测到的信息顺序。

正确做法是：

1. 按文件原始顺序读取；
2. 检查 `sequence` 和 `timestamp_received`；
3. 如果倒序，标记为数据问题；
4. 由数据侧解释 collector、buffer、flush、partition 或 merge 逻辑；
5. 只有明确知道某个字段不是原始接收顺序，而是导出顺序，才能定义新的 replay order 字段。

## 7. 关于 v1 / v2 版本号

### 7.1 当前是否需要 v1

建议保留一个轻量的 `v1`，但不要把它理解成产品发布版本。

它的含义是：

> 当前回测框架接受的第一版数据合同。

也就是：

- `PolymarketL2DatasetV1`
- `L2ReplayStepV1`
- `L2UpdateV1`
- `MarketMetadataV1`
- `live_ws_v1`

这比只用 git branch 管理更清楚，因为数据合同可能会被 IT、研究脚本、历史实验、回测入口同时引用。branch 名只能管理代码状态，不能解释某份历史数据到底符合哪一版合同。

### 7.2 为什么不是只用 branch

如果只靠 branch，例如：

```text
feature/polymarket-v1
feature/polymarket-v2
```

会有几个问题：

1. 历史数据文件本身不知道自己属于哪个合同；
2. 同一个代码分支可能要读取旧实验数据；
3. IT 交付和研究实验不一定跟代码分支同步；
4. 报告里需要说明本次实验用的数据 contract，而不是只说明 git branch。

所以建议：

- branch 管代码开发生命周期；
- schema/version 管数据合同；
- report 记录本次使用的 contract version。

### 7.3 对 v1 范围的修正

`tick_size_change` 和 `settlement/final payout` 不应该被放到遥远 v2。它们是预测市场基础事实，v1 数据合同就应该要求 IT 保存。

更准确的分层是：

```text
v1 data support：必须存、能验收、能 mock、能被 data_health / resolved_config 记录。
v1 execution support：当前回测至少能正确处理或显式 fail，逐步补齐真实使用。
```

因此：

- tick size change：v1 必须存 replay event；当前 bridge 默认 fail fast，后续 v1 内补 mock/validation/data_health 记录，再决定 instrument epoch 或 max-precision replay policy。
- settlement：v1 必须存 token-level payout / winner / resolution metadata；它应被回测侧转成系统生成的 settlement fills，用来按 payout 自动平仓；不需要单独做 settlement report，后续再验证是否接 Nautilus engine-native settlement。

### 7.4 我们现在有哪些假设和近似，会驱动 v2

虽然 Nautilus 回测引擎是固定的，但我们仍然有很多工程假设和近似。不过需要区分：

```text
v1 必须保存的基础事实：tick size change、settlement/final payout。
v2 才需要升级的深层语义：多时间轴、delay/cancel/fill model、跨市场 ontology、metadata timeline。
```

当前 v1 假设与后续增强：

| 主题 | v1 数据合同 | 当前 execution 状态 | 后续增强 |
| --- | --- | --- | --- |
| replay clock | 使用 `timestamp_received` | 已作为回测时钟 | v2 可能区分 collector receive、source、strategy-visible、order-effective time。 |
| step boundary | 保留 WS message/update boundary | 已支持 step -> Nautilus deltas | 若 IT pipeline 改变 message boundary，需要显式 batch/message id。 |
| L2 depth | book / price_change / trade | 已转 `OrderBookDeltas` / `TradeTick` | 做 maker 高频时需要更强 fill/queue calibration。 |
| tick size change | v1 必须保存事件 | 当前 bridge 默认 fail fast，避免静默错 replay | v1 内先做 mock/validation/data_health 记录，再决定 instrument epoch 或 max-precision policy。 |
| settlement | v1 必须保存 token-level payout | 当前尚未做 settlement fills | resolution_time 生成 synthetic settlement fills 自动平仓；不单独做 settlement report，engine-native settlement 后续验证。 |
| delay | v1 metadata 保存 delay config | 暂未进入 fill model | v2 动力：taker delay、cancel-before-fill、order effective time。 |
| fee | v1 保存 fee rate/source | 已用 Polymarket fee formula | reward/rebate 暂不建模。 |
| trade prints | v1 保存 trade prints | 已转 `TradeTick` | trade 与 book 对齐继续做诊断，不作为撮合真相。 |
| event semantics | v1 保存基本 event/condition/token join | metadata 初步记录 | v2 动力：互斥、穷尽、蕴含、组合事件、negRisk ontology。 |

所以 v2 不是因为 tick size 和 settlement 本身；它们应进入 v1。v2 的真正动力是：更复杂的时间语义、撮合近似和跨市场语义进入核心合同。

### 7.5 建议版本策略

建议保留：

```text
*_v1
```

但控制使用范围：

1. 只给数据合同、adapter、入口命名；
2. 不要每个小工具都滥用版本号；
3. 破坏兼容时再升 v2；
4. 小修补走 git commit，不升版本。

什么时候升 v2：

- step schema 发生破坏性变化；
- replay clock 从单一 `timestamp_received` 扩展为多时间轴；
- metadata 从单 snapshot 变成 as-of event stream / timeline；
- delay/cancel-before-fill/order-effective-time 进入核心撮合合同；
- queue/fill model 需要数据合同表达排队或校准字段；
- 多 market/event 组合关系成为核心 schema；
- 不再兼容 v1 文件。

不建议因为“v1 增加 tick size mock/validation”或“v1 增加 settlement metadata / final PnL 接入”就升 v2；这两个属于 v1 应补齐的基础事实。

什么时候不升版本：

- 修 bug；
- 增加 report 字段；
- 增加非必需 metadata 字段；
- adapter 内部清理；
- strategy 实验变化。

## 8. 最小验收结论模板

数据到手后，可以按下面格式给结论：

```text
验收对象：<event/window/delivery batch>
数据合同：PolymarketL2DatasetV1 + MarketMetadataV1
验收结论：通过 / 条件通过 / 不通过

P0 检查：
- 文件结构：通过/失败
- sequence 严格递增：通过/失败
- timestamp_received 不倒序：通过/失败
- step/message boundary 保留：通过/失败
- L2 book/price_change 字段完整：通过/失败
- trade 字段完整：通过/失败
- tick_size_change 是否被保留/统计：通过/失败
- market metadata 可 join：通过/失败
- fee rate 有来源：通过/失败
- settlement token payout 是否可 join：通过/失败

主要风险：
- ...

是否可以进入回测：是/否
是否只能 smoke test：是/否
需要 IT 修复：...
```

## 9. 当前建议

1. 和 IT 对齐时，先要求他们交付 v1 合同，不要让他们只给扁平化 parquet rows。
2. 行情和 metadata 分开，但必须能通过 `condition_id + token_id` join。
3. `timestamp_received` 和 `sequence` 是硬要求。
4. fee rate、tick size、delay、settlement/result 不要事后人工补，至少先作为 metadata snapshot / replay event 落盘。
5. tick size change 和 token-level settlement payout 应作为 v1 验收内容，而不是 v2 展望。
6. 当前保留 `v1` 命名是有意义的，因为它定义的是数据合同，不是代码分支。
7. 后续等第一批正式 IT 数据到手后，再根据验收结果决定是否收束成 boss report。
