# Beijing Market 缺失 tick-size transition 诊断

日期：2026-07-17

## 结论

Beijing parity 失败不是偶发单条坏价格。对应 condition 的行情在约 19 小时内持续使用 `0.001` 价格精度，但 event 数据中没有任何 `tick_size_change` 事件。

更准确的判断是：市场已经进入 `0.001` tick regime，但 PMXT/event-level 数据缺失了 `0.01 -> 0.001` 状态转换通知。当前严格回放继续保留失败，不静默推断转换时间。

## 数据定位

- Event：`highest-temperature-in-beijing-on-june-6-2026`
- condition：`0x7533f09cc0855c57941a1003d3541138e970bbe93e4a0d74f51f147e46476118`
- 数据：`C:\Projects\PolyReaper\data\curated\polymarket\events-rebuild\highest-temperature-in-beijing-on-june-6-2026\orderbook.parquet`
- condition 总行数：193,440
- `tick_size_change`：YES 0 条，NO 0 条

## 持续性证据

| Leg | 显式三位小数 price rows | 含三位小数价位的 book snapshots | 三位小数时间范围 |
| --- | ---: | ---: | --- |
| YES | 12,376 | 309 | 2026-06-05 21:35:26 UTC ～ 2026-06-06 16:40:28 UTC |
| NO | 12,224 | 318 | 2026-06-05 21:35:26 UTC ～ 2026-06-06 16:40:28 UTC |

首次出现的 YES/NO 价格互为补集，例如：

- YES：`0.984`、`0.985`、`0.989`、`0.997`
- NO：`0.016`、`0.015`、`0.011`、`0.003`

两个 token 同时进入三位小数精度，且持续数万行，支持“缺失 regime transition”而不是“单条脏报价”的判断。

## 精度边界检查

按 `(timestamp, timestamp_received, stable row ordinal)` 重排后，YES/NO 两个 token 呈现同一个清晰边界：

- 边界前最后几次 book snapshot 只包含 `0.01` 网格价格；
- `2026-06-05 21:35:26.130 UTC`，YES/NO 的 book snapshot 同时首次出现 `0.001` 网格价格；
- YES 边界前最近的 book snapshot 为 `21:29:40.098 UTC`，仍为 `0.01` 网格；
- NO 边界前最近的 book snapshot 为 `21:23:49.138 UTC`，仍为 `0.01` 网格；
- 边界后的 309 个 YES book snapshot 和 318 个 NO book snapshot 全部包含非整分价格，没有发现重新退回纯 `0.01` 网格的 snapshot；
- 非整分 price row 在边界后的每一个小时都持续出现，最长相邻空档约 29.9 分钟，没有超过一小时的中断；
- 数据中虽然仍会出现 `0.46`、`0.55` 等整分价格，但这些价格同样合法于 `0.001` tick，不能据此解释为 tick size 已切回 `0.01`。

因此，当前样本更符合“一次 `0.01 -> 0.001` transition 丢失，之后持续处于 `0.001` regime”，没有看到 `0.01 / 0.001` 状态反复抖动的证据。

这一结论仍是从盘口网格反推的诊断证据，不等价于恢复了缺失的官方事件。严格回放在明确推断规则落地前仍应失败并报告该缺口。

## 当前严格失败机制

```text
初始有效 tick = 0.01
数据中没有 tick_size_change
回放状态仍为 0.01
出现 0.984 / 0.003 / 0.001 等价格
Nautilus 转换前精度校验严格失败
```

## 当前 parity 检查的真实边界

2026-07-17 补跑了 4 Event × YES/NO 共 8 条检查：

- 6 条完成转换，并且 direct replay 时间与 Nautilus `ts_init` 各自单调；
- Beijing YES/NO 两条在精度校验处失败；
- 当前检查**没有**逐事件比较 direct replay 与 Nautilus native 输出的价格、数量、book action、BBO 或最终订单簿状态；
- 因而 6 条通过只能称为 `ordering / convertibility smoke pass`，不能称为“完整语义输出完全一致”。

完整语义 parity 仍需增加统一归一化输出，并至少比较：事件映射、时间戳、book delta action、价格/数量、TradeTick、逐步 BBO/深度状态和最终 book state。

原始检查结果：`compact/pilot_6_event/direct_native_parity.json`。
