# Polymarket 数据合同版本与 v2 展望笔记

日期：2026-07-04  
状态：research note；用于后续 IT 验收、工程规划和可能的汇报素材，不是 boss report。

## 1. 结论先行

`v1 / v2` 不应该按“代码重构次数”划分，也不应该因为 NautilusTrader 回测引擎固定就取消版本概念。

更合适的定义是：

```text
v1 = 第一版可验收的数据合同 + 当前回测框架必须能消费的最小真实市场信息
v2 = 当 v1 的时间语义、市场规则语义、撮合/结算近似被策略研究打穿时，再升级的数据/建模合同
```

因此，版本号管的是 **数据合同和建模语义**，不是 git branch。

## 2. 对 v1 范围的修正

之前把 `tick_size_change` 和 `settlement` 放得偏远了。这个判断需要修正。

更合理的是：

> v1 数据合同就应该要求 IT 保存 tick size change 和 settlement/final result。

原因：

1. tick size change 是 Polymarket 官方机制，不是研究增强项；
2. settlement/final result 是预测市场最终 PnL 的基础，不是策略锦上添花；
3. 即使第一批策略主要做短线 round-trip，不持有到期，也不能让这些字段事后人工补；
4. 如果 v1 不要求保存，后面再补历史会很痛苦，甚至补不回来。

所以 v1 的目标应分成两层：

```text
v1 data support：必须存、能验收、能 mock、能落盘、能被 data_health / resolved_config 记录。
v1 execution support：当前回测至少能正确处理或显式 fail，逐步补齐真实使用。
```

也就是说：

- 数据侧 v1：tick size / settlement 必须交付；
- 回测侧 v1：先支持 mock/validation/config 记录，再逐步支持把 settlement 转成系统生成的平仓成交。

## 3. tick size change：v1 应该怎么支持

### 3.1 IT 必须保存什么

tick size change 应作为 replay event 保存：

```json
{
  "sequence": 10001,
  "timestamp_received": "2026-06-26T02:55:00.123Z",
  "timestamp": "2026-06-26T02:55:00.100Z",
  "updates": [
    {
      "event_type": "tick_size_change",
      "market": "0x...",
      "asset_id": "123...",
      "old_tick_size": "0.01",
      "new_tick_size": "0.001"
    }
  ]
}
```

同时 market metadata 里应有初始 tick size：

```json
{
  "condition_id": "0x...",
  "token_id": "123...",
  "minimum_tick_size": "0.01",
  "tick_size_source": "clob_market_info.minimum_tick_size"
}
```

### 3.2 当前代码状态

当前代码已经有：

- `L2UpdateV1(event_type="tick_size_change")`
- `old_tick_size`
- `new_tick_size`
- `live_ws_v1` adapter 能读 `tick_size_change`
- `pmxt_parquet_v1` 也认识 `tick_size_change`

但 Nautilus-native bridge 当前默认：

```text
fail_on_tick_size_change = true
```

遇到 tick size change 会 fail fast。

这不是数据合同不支持，而是 execution 还没完整支持动态 instrument tick epoch。

### 3.3 复杂度评估

#### A. 数据存储 / mock / 验收

复杂度：低。

需要做：

1. mock 一条 `tick_size_change` step；
2. data_health 检查 old/new tick size 能 parse；
3. data_health / resolved_config 里记录 tick size change count；
4. 验收时确认该事件没有被丢弃。

这块很适合立刻放进 v1。

#### B. 回测 replay 完整支持

复杂度：中等。

难点是 Nautilus instrument 的 `price_increment` 是 instrument 属性。真实 Polymarket 是同一个 token 在某个时点后 minimum price increment 变化：

```text
0.01 -> 0.001
```

需要决定回测侧怎么表达：

1. instrument epoch：同一个 instrument 在时间线上有不同 price_increment；
2. 或者统一用最细 tick precision，比如全程 0.001，但额外验证早期订单不能用 0.001；
3. 或者 tick size 只用于策略/order validation，不改变 historical book replay 精度。

最稳妥是 instrument epoch，但工程量最大。短期 v1 可以先做：

```text
数据存储 + mock + 验收 + data_health 记录 + 遇到真实动态 tick 时显式失败
```

然后 v1 后续小版本再加：

```text
replay uses max precision + strategy order tick validation
```

如果这个实现不破坏 v1 数据合同，可以不升 v2。

## 4. settlement / final result：v1 应该怎么支持

### 4.1 IT 必须保存什么

settlement 不应该作为普通盘口 update 存在，而应该进入 market metadata / resolution metadata。

建议字段：

```json
{
  "condition_id": "0x...",
  "resolution_status": "resolved",
  "resolution_time": "2026-06-10T12:00:00Z",
  "oracle": "UMA",
  "voided": false,
  "tokens": [
    {
      "token_id": "123...",
      "outcome": "Yes",
      "payout": "1",
      "winner": true
    },
    {
      "token_id": "456...",
      "outcome": "No",
      "payout": "0",
      "winner": false
    }
  ],
  "resolution_source": "gamma_or_clob_or_settlement_snapshot",
  "as_of_time": "2026-06-10T12:05:00Z"
}
```

关键是不要只存：

```text
final_result = Yes
```

最好直接存 token-level payout：

```text
token_id -> payout in {0, 1} or refund/void rule
```

因为后续有 void、争议、特殊裁决、多 outcome、negRisk 等情况。

### 4.2 当前代码状态

当前 Polymarket research backtest 还没有 settlement PnL。现在主要验证：

```text
order -> fill -> position -> account report
```

如果策略平仓，已经够用；如果持有到期，还需要 settlement。

Nautilus 本身有 venue-level `settlement_prices` 配置概念，但 Polymarket binary payout 仍需要我们把 token payout 正确映射成 instrument settlement price，并避免策略在回测开始时偷看 resolution metadata。

Nautilus Polymarket adapter 里也已经有类似防偷看的思路：resolved market 的 `winner` 等 resolution 字段不能直接塞进 strategy 可读的 `instrument.info`，应该单独给 post-backtest analytics / settlement PnL 用。

### 4.3 复杂度评估

#### A. 数据存储 / mock / 验收

复杂度：低。

需要做：

1. `MarketMetadataV1` 或 companion metadata 增加 resolution fields；
2. mock 一个 resolved market；
3. 验收检查 token-level payout 是否完整；
4. resolved_config / data_health 里记录 settlement metadata 是否可 join。

这块也应该进入 v1。

#### B. settlement 自动转成系统平仓成交

复杂度：低到中。

这里不需要单独做一个 settlement report。更合理的是：

```text
如果策略在 resolution 前已经平仓：settlement metadata 只作为验收字段，不参与 PnL。
如果策略持有到期：在 resolution_time 生成系统 settlement fill，把 open position 按 token payout 自动平仓。
```

也就是说，settlement 是最终结算输入，不是一个独立研究报告主题。

注意这里说的“成交”不是 Polymarket 市场上的 `TradeTick`，而是回测系统内部的 synthetic settlement execution/fill：

```text
condition_id = C
YES token payout = 1
NO token payout  = 0

resolution_time 到达时：
- 对 YES instrument 的未平仓仓位，生成一条 settlement fill，price = 1，fee = 0；
- 对 NO instrument 的未平仓仓位，生成一条 settlement fill，price = 0，fee = 0。
```

如果一个 event 回测同时加载 YES/NO 两个 token，就会自然表现成“两条 settlement 成交/平仓事件”。
如果当前 run 只加载单个 selected token，则只对该 token 的 open position 生成对应 settlement fill。

优点：

- 不侵入 Nautilus engine；
- 不影响短线策略；
- 能解决“持有到期应如何计入最终收益”的问题；
- 不会让策略在运行中偷看结果。

#### C. engine-native settlement / 自动 close position

复杂度：中等。

如果要让 Nautilus engine 在 resolution time 自动结算，可能要：

1. 给 venue 配 `settlement_prices`；
2. 确认 BinaryOption 是否按预期 close；
3. 确认 settlement event time；
4. 确认 account / position / realized PnL report 和 Polymarket payout 一致；
5. 防止策略读取未来 resolution metadata。

这块建议作为 v1 的增强项，不要阻塞第一批数据验收。

## 5. 修正后的 v1 / v2 边界

### 5.1 v1 必须包含

v1 数据合同必须包含：

| 模块 | v1 要求 |
| --- | --- |
| L2 replay | book / price_change / trade |
| replay clock | `timestamp_received` |
| atomic step | 保留 WS message/update boundary |
| data health | 不排序修复，显式检查倒序/未来/晚到 |
| fee | fee rate + source，使用 Polymarket fee formula |
| tick size | 初始 tick size + tick_size_change events |
| settlement | token-level payout / winner / resolution metadata |
| run artifact | data_health + resolved_config 记录 fee source、tick count、settlement metadata join 状态 |

### 5.2 v1 可以分阶段实现

```text
v1.0：L2 replay + fee + data health + metadata storage
v1.1：tick size mock/validation/data_health 记录
v1.2：settlement mock/validation/synthetic settlement fills
v1.3：dynamic tick precision replay policy
v1.4：engine-native settlement if Nautilus path验证通过
```

这些都是 v1 内部增强，只要不破坏数据合同，不需要叫 v2。

### 5.3 v2 真正的动力

v2 应该留给更深的语义变化：

1. 多时间轴：collector receive / source timestamp / strategy visible time / order effective time 分离；
2. delay model：taker delay、cancel-before-fill、delayed order lifecycle 进入撮合模型；
3. queue / fill model：L2 盘口下的排队估计、部分成交概率模型；
4. event ontology：互斥、穷尽、蕴含、组合事件、negRisk 进入核心 schema；
5. metadata timeline：market metadata 从单 snapshot 变成 as-of event stream；
6. 跨 market / event replay：不再只按单 instrument 跑，而是一个 event 下多个相关 market 联合回放。

也就是说，v2 的动力不是 tick size 和 settlement 本身；这两个应进 v1。

v2 的动力是：

> 交易可见时间、动态规则、跨市场语义和撮合近似进入核心合同，导致 v1 的单 instrument L2 replay + metadata snapshot 模型不够了。

## 6. 对 IT 的直接要求

给 IT 的简化表达可以是：

```text
第一版交付不要只给盘口。除了 L2 replay events，还必须给 market metadata。
metadata 里至少要有 fee、初始 tick size、tick_size_change 保留、resolution/final payout。
这些不是策略增强项，而是预测市场回测的基础事实。
```

如果 live WS 本身没有 settlement，就从其他官方/API/结算源做同一 condition/token 的 metadata snapshot。它不要求和每条 WS 同频，但必须能通过 `condition_id + token_id` join，并带 `as_of_time` 和 `source`。

## 7. 当前工程建议

短期不要直接大改 engine。建议顺序：

1. 先把文档和验收标准改成：v1 必须存 tick size 和 settlement；
2. 加 mock 数据，覆盖 tick_size_change 和 resolved settlement metadata；
3. data_health / resolved_config 增加 coverage 检查；
4. settlement 先作为 synthetic settlement fills / 最终平仓输入，不单独做 settlement report；不急着 engine-native close；
5. tick size 先做 validation / data_health 记录，再决定是 instrument epoch 还是 max-precision replay policy；
6. 等真实 IT 数据来后，再决定是否需要 v1.1/v1.2 代码实现。
