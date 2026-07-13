# PMXT 数据质量边界与回测修正说明

生成时间：2026-07-10

这份说明用于把 PMXT Discord 证据和 PolyReaper 数据契约同步到当前回测 / 研究代码。它不是 boss report，也不是策略结果报告。

## 1. 已确认边界

### PMXT 不是严格撮合级 truth

PMXT v2 parquet 可以作为历史盘口候选源，用于：

- 粗 BBO / spread / depth / activity 统计；
- event 级可视化；
- 因子候选筛选；
- adapter / pipeline smoke test。

但它不能在未过质量门控时直接用于：

- 严格逐事件 replay；
- 精确 candle close；
- maker fill / queue priority / adverse selection 结论；
- Nautilus PnL / fee / cash / position 级别的正式回测。

当前主入口仍然拒绝 `pmxt_event_v1` 进入 Nautilus PnL/fill runner，这是刻意保留的边界。

### 排序口径

PMXT 专用 adapter 现在使用：

```text
1. timestamp
2. timestamp_received
3. original row index as stable deterministic fallback
```

这里的 fallback 只保证可复现，不证明是真实 WebSocket/message 顺序。
如果出现 tied group，必须带着 ambiguity flag 解读结果。

### tied group 定义

按下列 key 分组：

```text
market, asset_id, timestamp, timestamp_received
```

如果同组多行在这些字段上存在差异：

```text
event_type, price, size, side, best_bid, best_ask, bids, asks
```

则标为 ordering ambiguous。

## 2. 已落到代码里的修正

### `polymarket/adapters/pmxt_event_v1.py`

- 从 receive-time 排序改为 source-time baseline 排序：`timestamp, timestamp_received, _original_row_index`。
- 增加 PMXT source-quality manifest：
  - `coverageStatus`
  - `orderingStatus`
  - `snapshotReplayStatus`
  - `metadataJoinStatus`
  - `orderingAmbiguousGroups`
  - `orderingAmbiguousRows`
  - `tiedTimestampGroups`
  - `tiedTimestampRows`
- 增加 tied group 检测。
- 明确写入 warning：stable fallback 只保证 deterministic，不保证真实交易所顺序。

### `polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py`

- 信任边界从 receive-time-causal 改为 PMXT timestamp-ordered exploratory。
- 新增 `replay_timestamp`：优先用 source `timestamp`，缺失时才 fallback 到 `timestamp_received`。
- 因子 rolling window、future label 都改为基于 `replay_timestamp`。
- 通用 `data_health` 里的 `receive_time_inversion` 对 PMXT 研究不再是硬 blocker；它保留为诊断。
- `sequence_inversion` 等非 PMXT 排序问题仍然是 blocker。

### `polymarket/research/2026-07-10-pmxt-weather-maker-diagnostics/maker_diagnostics.py`

- probe 抽样、trade arrays、future mid arrays、fill window、markout clock 全部改为基于 `replay_timestamp`。
- 保留 `timestamp_received` 字段用于审计，但不再把它当 PMXT 主回放钟。

## 3. 当前仍未解决的问题

1. **tie-order sensitivity test 还没有完整自动化**  
   现在已经能检测 tied/ambiguous groups，但尚未对关键 tied group 做 fallback/reverse/random order 的 replay 差异比较。

2. **snapshot replay check 仍是 not_run**  
   还没有系统地验证 `book + price_change` 到下一次 `book` snapshot 是否能对齐。

3. **coverageStatus 仍是 not_run**  
   当前 adapter 只对单个 event/token load 成功负责，不证明 PMXT archive 对目标 universe/window 覆盖完整。

4. **PMXT 仍不进入 Nautilus PnL/fill runner**  
   这不是缺功能，而是边界约束：目前 PMXT 更适合因子探索和可视化，不适合正式成交/PnL 回测。
   > 2026-07-13 更新：无条件拒绝已改为显式 mode gate。默认 `strict_capture`
   > 仍拒绝 PMXT；config 显式声明 `replay: {mode: pmxt_research}` 时允许进入
   > Nautilus 原生 BacktestEngine 的 research backtest，输出带
   > `pmxt_research_reconstructed_order*` 可信度标记与非撮合级声明。
   > 见 `polymarket/replay_contract.py` 与
   > `polymarket/research/2026-07-13-pmxt-research-backtest-v1/`。

## 4. 当前可以如何用 PMXT

可以：

- 做 book 因子探索；
- 做跨 event 复验；
- 做 maker fill proxy / markout diagnostic；
- 做数据质量暴露和候选市场筛选。

不可以直接声称：

- 策略 PnL；
- 真实 queue priority；
- 真实 maker fill；
- 严格逐消息 replay；
- 可交易收益。

一句话：

> PMXT 现在被降级为“外部候选历史源 + exploratory research input”。任何基于 PMXT 的结论都必须携带 coverage / ordering / snapshot / metadata quality flags。
