# live raw WS 数据健康检查与回测入口收束

日期：2026-07-03

## 结论

本轮把回测入口收束成一个原则：

> 回测按本地 `timestamp_received` 回放；如果 receive 顺序有倒序，入口直接失败，不靠排序把问题“修好”。

原因是我们目前优先相信本地 raw WS capture 的接收顺序。Polymarket source
`timestamp` 仍然会记录、检查和报告，但它不再决定 replay 顺序，也不作为
Nautilus `ts_event` 的回放时钟，避免 source timestamp 未来时间或倒序带来
look-ahead 风险。

## 已实现的工程契约

这里的“契约”不是为了长期兼容各种数据源，而是我们希望最终让 IT / 数据侧直接
交付的标准形状。当前 raw capture、PMXT 或其它临时文件如果不符合，就在进入回测
前用外置脚本补丁式转换成这个形状。

1. `python -m polymarket.backtest_v1` 在跑 Nautilus 前先生成
   `data_health.json`。
2. `timestamp_received` 倒序、local `sequence` 非严格递增是 hard error，入口
   直接失败。
3. source `timestamp` 倒序、未来时间、秒级晚到先作为 diagnostics 暴露，用来
   评估严重程度。
4. `live_ws_v1` 不再用 source `timestamp` 伪装 receive time；没有显式 receive
   time 就失败。
5. 新增独立归一化脚本：
   `python -m polymarket._tools.normalize_live_ws_v1`，把现有 capture wrapper
   转成 `live_ws_v1` 可读格式。
6. PMXT adapter 本轮不继续展开；PMXT 仍是 legacy/questionable 数据源。

## 为什么还保留 Nautilus 内部 sort

Nautilus `BacktestEngine` 要求 `add_data(..., sort=False)` 后调用
`engine.sort_data()` 才能把分开加入的 `OrderBookDeltas` 和 `TradeTick` 合成一个
可运行流。

这里的 sort 不是对 source 数据做纠错排序：

- source adapter 输出顺序不被修改；
- pre-run health gate 已经证明 `timestamp_received` 单调；
- Nautilus data 的 `ts_init` / `ts_event` 都来自 `timestamp_received`；
- 因此内部 sort 只是把不同 Nautilus data type 按同一 receive clock 同步。

如果以后发现 `timestamp_received` 本身不可信，应该回到数据层检查，而不是在
adapter 或 runner 里改排序规则。

## 未来时间怎么观察

`data_health.json` 中会有：

- `future_source_time_count`
- `max_future_source_time_ms`
- `source_delay_over_threshold_count`
- `max_source_delay_ms`
- `source_time_inversion_count`
- `source_timestamp_missing_step_count`
- `source_timestamp_missing_update_count`

这些字段用于回答：

1. source timestamp 是否经常晚于本地 receive time；
2. 晚到是否只是几十毫秒级时钟偏差，还是秒级问题；
3. source timestamp 在同一个 `event_type + asset_id` 内是否倒序。
4. source timestamp 覆盖率是否足够，避免把“缺失所以没法比较”误读成“没有倒序”。

当前策略是“报告但不自动修正”。如果未来发现严重问题，再决定是否在数据采集
或上游 schema 中补更强的 message/order 证据。

## 使用方式

单独检查：

```powershell
python -m polymarket.data_health --ndjson path/to/live_ws_v1.ndjson --output data_health.json
```

归一化现有抓包：

```powershell
python -m polymarket._tools.normalize_live_ws_v1 `
  --input path/to/raw_capture.ndjson `
  --output path/to/live_ws_v1.ndjson
```

跑回测：

```powershell
python -m polymarket.backtest_v1 --config polymarket/research/<topic>/experiment.yml
```

每个 run 目录会保存：

- `original_config.yml`
- `resolved_config.json`
- `data_health.json`
- Nautilus native reports
