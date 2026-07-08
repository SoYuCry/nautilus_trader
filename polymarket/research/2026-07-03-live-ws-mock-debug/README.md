# live_ws mock debug data

这个目录用于手工 debug 当前 Polymarket live_ws 回测链路。

## 文件

- `mock_capture_clean.ndjson`
  - pre-contract wrapper 格式；正常样本。
  - 包含 `book`、`price_change`、`last_trade_price`、`tick_size_change`。
- `mock_live_ws_v1_clean.ndjson`
  - 由 normalizer 生成的严格 `live_ws_v1` 输入。
- `mock_capture_future_source_warning.ndjson`
  - 故意让 source `timestamp` 晚于 `received_at` 120ms；`data_health` 应该 warning 但 `ok=true`。
- `mock_capture_receive_inversion_bad.ndjson`
  - 故意让第二条 `received_at` 早于第一条；`data_health` 应该 fail。
- `experiment.yml`
  - 指向 clean normalized 数据的 backtest 配置；`strategy.enabled=false`，只验证数据能进 Nautilus `BacktestEngine`。
- `strategy_take_best_ask.py`
  - mock 专用策略；不是 alpha，只在第一轮可用盘口后提交一次 market BUY。
- `experiment_with_strategy.yml`
  - 指向 clean normalized 数据并启用 `TakeBestAskOnce`，用于验证 order / fill / position 报告链路。

## 推荐 debug 顺序

```powershell
python -m polymarket._tools.normalize_live_ws_v1 `
  --input polymarket/research/2026-07-03-live-ws-mock-debug/mock_capture_clean.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_clean.ndjson

python -m polymarket.data_health `
  --ndjson polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_clean.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/data_health_clean.json

python -m polymarket.backtest_v1 `
  --config polymarket/research/2026-07-03-live-ws-mock-debug/experiment.yml

python -m polymarket.backtest_v1 `
  --config polymarket/research/2026-07-03-live-ws-mock-debug/experiment_with_strategy.yml
```

如果本地没有 Nautilus compiled runtime，完整 `backtest_v1` 可能跑不起来；这时先 debug 到 normalizer / adapter / data_health / nautilus_native conversion。

`experiment_with_strategy.yml` 的预期结果：

- `summary.json`: `data_count=4`，`order_book_deltas_count=3`，`trade_ticks_count=1`。
- `fills_report.txt`: 1 笔 `MARKET BUY`，数量 `1.000000`，均价 `0.6`。
- `positions_report.txt`: 1 个 open long position。
- `tick_size_change` 不进入 Nautilus data，但会记录到 `skipped_updates` / `tick_size_changes`。

### missing source timestamp warning

```powershell
python -m polymarket._tools.normalize_live_ws_v1 `
  --input polymarket/research/2026-07-03-live-ws-mock-debug/mock_capture_missing_source_timestamp.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_missing_source_timestamp.ndjson

python -m polymarket.data_health `
  --ndjson polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_missing_source_timestamp.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/data_health_missing_source_timestamp.json
```

预期：`ok=true`，但 `issues` 里有 `missing_source_timestamp` warning，并且 summary 里会出现：

- `source_timestamp_missing_step_count`
- `source_timestamp_missing_update_count`

