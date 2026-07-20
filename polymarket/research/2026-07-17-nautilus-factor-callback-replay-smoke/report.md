# Nautilus 因子回调回放验证报告

日期：2026-07-17

## 结论

这次验证通过。

在 1 个合成 case 和 3 个真实天气 Event 的 YES/NO 共 6 个 token 上：

- direct research replay 的顺序完整进入 Nautilus native data pipeline；
- Nautilus 策略收到的 callback 顺序与 native input 的 `ts_init` 顺序一致；
- 策略从 Nautilus 原生 `OrderBook` 读取的 `bid1` / `ask1` 与 direct replay 一致；
- 策略在 callback 内重算的 `depth_imbalance_1` 与 direct replay 一致；
- direct replay 判定的实际盘口 mutation 与 Nautilus callback 后的完整盘口状态变化一致；
- no-op update 没有被误算成新的 ranking observation 或信号；
- 阈值为 `±0.10` 的简单信号触发序列完全一致。

因此，当前可以把已经通过 PMXT quality gate 的单 token 数据送入 Nautilus，并相信策略 callback 看到的**回放顺序、BBO 和一档 imbalance**与研究侧口径一致。

这比此前的 ordering / convertibility smoke 多证明了一层：不只是“数据对象能进入引擎”，而是“策略在引擎 callback 中确实能复现研究因子”。

## 覆盖范围

| Event | Leg | Direct rows | Native callbacks | Ranking observations | Signal triggers | 结果 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Karachi 2026-06-06 | YES | 22,658 | 22,657 | 15,348 | 13,922 | PASS |
| Karachi 2026-06-06 | NO | 22,667 | 22,666 | 15,335 | 13,907 | PASS |
| Busan 2026-06-06 | YES | 24,471 | 24,470 | 18,121 | 16,952 | PASS |
| Busan 2026-06-06 | NO | 24,491 | 24,490 | 18,067 | 16,904 | PASS |
| Qingdao 2026-06-06 | YES | 16,550 | 16,548 | 8,547 | 6,977 | PASS |
| Qingdao 2026-06-06 | NO | 16,549 | 16,547 | 8,548 | 6,977 | PASS |
| **合计** |  | **127,386** | **127,378** | **83,966** | **75,639** | **6/6 PASS** |

完整结果：[`outputs/factor_callback_smoke.json`](outputs/factor_callback_smoke.json)

结果签名：`5218fba04be010213c721f5bb3587f11ccb11ce831548ecd29dab17695c7fb36`

## 验证方法

### Direct 侧

复用现有 `factor_protocol.build_factor_panel(..., include_labels=False)`：

- 不计算 future return；
- 不读取未来数据；
- 每个 replay step 记录 sequence、实际 mutation、BBO 和 `depth_imbalance_1`；
- 只有实际改变盘口且 BBO 有效的 step 才是 ranking observation。

### Nautilus 侧

同一份 dataset 经 `convert_dataset_to_nautilus` 转为原生 `OrderBookDeltas` / `TradeTick`，送入 `BacktestEngine`。

探针策略继承 Nautilus 原生 `Strategy`，在 `on_order_book_deltas` 中：

1. 从 `self.cache.order_book(...)` 读取引擎已经应用 callback 后的盘口；
2. 读取 best bid/ask price 与 size；
3. 重算 `depth_imbalance_1`；
4. 对完整 bids/asks 状态做签名，独立判断这次 callback 是否真的改变盘口；
5. 生成简单阈值信号。

最后按 source sequence 对齐 direct 与 callback 结果。

## 仍然没有证明什么

本实验不是完整回测正确性证明，尤其没有证明：

- PMXT tied rows 的 deterministic fallback 等于真实 WebSocket 顺序；
- 全部 227 Event 都可转换；
- L2 每一档在所有异常数据下都保持一致；
- fill、partial fill、queue、fee、PnL、settlement 正确；
- Beijing 缺失 tick transition 可以安全推断。

Beijing YES/NO 仍应保持严格失败；它是数据契约问题，不应通过本实验静默绕过。

## 当前判断

对当前因子研究最关键的问题已经回答：

> 因子研究采用的 replay 顺序进入 Nautilus 后，策略 callback 能看到一致的盘口状态并复现同一个 `depth_imbalance_1` 信号序列。

所以下一步不需要继续扩写 parity 框架。可以开始把候选因子策略接入 Nautilus 做成交、费用和 PnL 层验证；若扩大数据覆盖，则先按小批量 Event 做 convertibility gate，而不是一次性跑全量。
