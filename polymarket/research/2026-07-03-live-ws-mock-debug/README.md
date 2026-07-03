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
  - 指向 clean normalized 数据的 backtest 配置。

## 推荐 debug 顺序

```powershell
python -m polymarket.scripts.normalize_live_ws_v1 `
  --input polymarket/research/2026-07-03-live-ws-mock-debug/mock_capture_clean.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_clean.ndjson

python -m polymarket.data_health `
  --ndjson polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_clean.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/data_health_clean.json

python -m polymarket.backtest_v1 `
  --config polymarket/research/2026-07-03-live-ws-mock-debug/experiment.yml
```

如果本地没有 Nautilus compiled runtime，完整 `backtest_v1` 可能跑不起来；这时先 debug 到 normalizer / adapter / data_health / nautilus_native conversion。

### missing source timestamp warning

```powershell
python -m polymarket.scripts.normalize_live_ws_v1 `
  --input polymarket/research/2026-07-03-live-ws-mock-debug/mock_capture_missing_source_timestamp.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_missing_source_timestamp.ndjson

python -m polymarket.data_health `
  --ndjson polymarket/research/2026-07-03-live-ws-mock-debug/mock_live_ws_v1_missing_source_timestamp.ndjson `
  --output polymarket/research/2026-07-03-live-ws-mock-debug/data_health_missing_source_timestamp.json
```

预期：`ok=true`，但 `issues` 里有 `missing_source_timestamp` warning，并且 summary 里会出现：

- `source_timestamp_missing_step_count`
- `source_timestamp_missing_update_count`
