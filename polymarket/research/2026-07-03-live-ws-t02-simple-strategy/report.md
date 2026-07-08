# Live WS T02 round-trip 策略回测报告

生成时间：2026-07-03  
实验目录：`polymarket/research/2026-07-03-live-ws-t02-simple-strategy/`

## 1. 目的

这次不是验证策略收益，而是用真实 live raw WebSocket 抓包跑一个最小的 Nautilus-native round-trip 策略，确认：

1. `python -m polymarket.backtest_v1` 主入口会自动先跑 data-health，不需要每次手动先跑；
2. live raw WS 数据能转成当前 `live_ws_v1` 合约，且不通过排序静默修复；
3. `PolymarketL2DatasetV1 -> OrderBookDeltas / TradeTick -> BacktestEngine -> Strategy -> fills/positions/account report` 链路能在真实 live 数据上跑通；
4. 成交、持仓、账户报告同时有 CSV 和 txt；
5. fee 已接入 Nautilus 的 Polymarket fee model；
6. 自动生成 run-level markdown report，后续策略实验可以复用同样的输出结构。

## 2. 输入数据

原始抓包：

```text
C:/Projects/PolyReaper/research/2026-06-25-polymarket-raw-ws-ordering-capture/data/raw/ws_capture_20260626T022520Z.ndjson
```

时间窗口：

```text
2026-06-26T02:25:28.635472Z ~ 2026-06-26T03:10:20.191538Z
北京时间约 2026-06-26 10:25:28 ~ 11:10:20
```

本次先用 private helper 转成 `live_ws_v1`：

```powershell
python -m polymarket._tools.normalize_live_ws_v1 `
  --input C:/Projects/PolyReaper/research/2026-06-25-polymarket-raw-ws-ordering-capture/data/raw/ws_capture_20260626T022520Z.ndjson `
  --output polymarket/research/2026-07-03-live-ws-t02-simple-strategy/data/live_ws_v1_20260626T022520Z.ndjson
```

normalizer 没有排序，也没有用 source timestamp 伪造 receive time。它只做格式转换：

- 支持 `raw_text`；
- 把首条 JSON array 形式的初始 book 拆成多条 replay-readable row；
- `PONG` 作为 control message 保留给 adapter 显式跳过；
- 跳过 `best_bid_ask` / `new_market` 这类当前回测不用的非 L2 事件。

转换摘要：

```text
rows_written: 2088
control_messages: 268
skipped_unsupported_messages: 260
split_array_messages: 1
```

摘要文件：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/normalization_summary.json
```

实际 normalized `.ndjson` 和 `runs/` 不提交，只保留生成命令与摘要。

## 3. 本次选择的 token

```text
condition_id: 0x024b68f77bfc019341ee3db8f57c103334e4b9430bba4746d8c94aafd8b36fee
token_id:     9894510651052373088408067031031513212801618531203062911959630395716258202132
instrument:   0x024b68f77bfc019341ee3db8f57c103334e4b9430bba4746d8c94aafd8b36fee-9894510651052373088408067031031513212801618531203062911959630395716258202132.POLYMARKET
```

选择原因：这条 token 在 T02 抓包中既有 L2 book / price_change，也有真实 `last_trade_price` prints，适合做第一版 live-data smoke test。

## 4. 策略

策略文件：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/strategy_take_best_ask_exit_after.py
```

策略逻辑：

1. `on_start` 订阅该 instrument 的 L2 MBP deltas；
2. 第一次在 cache 里看到 best ask 后，提交 market buy，数量 1；
3. buy fill 后等待 `exit_after_seconds=60`；
4. 之后第一次看到可用 best bid 时，提交 market sell 平仓。

这只是链路验证策略，不是 alpha。目标是制造一组可解释的 round-trip 成交，用来检查 BacktestEngine、fee、CSV report 和 closed position。

## 5. fee 设置

配置文件中设置：

```yaml
instrument:
  maker_fee: "0"
  taker_fee: "0.05"
fees:
  enabled: true
  maker_rebates_enabled: false
```

当前只接入 fee，不建模 reward / rebate。fee 由 Nautilus 的 `PolymarketFeeModel` 根据 fill price、qty、instrument `taker_fee` 计算。

## 6. 运行命令

本地源码 checkout 没有编译 Nautilus `.pyd`，所以使用已安装 wheel 的 native runtime：

```powershell
$env:PYTHONPATH='C:\Users\xhth\miniconda3\Lib\site-packages;C:\Projects\nautilus_trader'
python -P -m polymarket.backtest_v1 --config polymarket/research/2026-07-03-live-ws-t02-simple-strategy/experiment.yml
```

配置文件：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/experiment.yml
```

## 7. Data-health 结果

主入口自动执行 health gate；不需要手动先跑 `python -m polymarket.data_health`。

回测前 health gate 通过：

```text
data_health_ok: true
step_count: 1820
update_count: 3623
receive_time_inversion_count: 0
sequence_inversion_count: 0
source_timestamp_present_step_count: 1820
source_timestamp_missing_step_count: 0
source_time_inversion_count: 0
future_source_time_count: 0
max_future_source_time_ms: 19.65
source_delay_over_threshold_count: 29
max_source_delay_ms: 6579285.472
```

解释：

- receive time 没有倒序；
- local sequence 没有倒序；
- source timestamp 没有倒序；
- 没有超过 50ms 的 future source time；
- 有 29 条 source delay warning，其中最大值来自初始 book 中明显较旧的 source timestamp。这个是 warning，不阻止回放；本次 replay clock 仍然只按 `timestamp_received`。

完整 health 文件：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/data_health.json
```

## 8. 回测输出

run 目录：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/
```

核心统计：

```text
engine: NautilusTrader BacktestEngine
data_count: 629
order_book_deltas_count: 627
trade_ticks_count: 2
data_health_ok: true
```

`trade_ticks_count: 2` 的意思是：selected token 的 `last_trade_price` 事件有 2 条被转换成 Nautilus `TradeTick`。它不是策略成交数。策略成交数看 `fills_report.csv`。

自动报告：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/run_report.md
```

## 9. 成交在哪里

CSV 成交报告：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/fills_report.csv
```

txt 预览报告：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/fills_report.txt
```

本次成交摘要：

| side | type | qty | avg_px | liquidity | commission | status | ts |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| BUY | MARKET | 1.000000 | 0.49 | TAKER | 0.012490 pUSD | FILLED | 2026-06-26 02:25:28.635472+00:00 |
| SELL | MARKET | 1.000000 | 0.47 | TAKER | 0.012460 pUSD | FILLED | 2026-06-26 02:26:29.010797+00:00 |

## 10. 持仓 / PnL 在哪

CSV 持仓报告：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/positions_report.csv
```

本次 position 已关闭：

```text
side:            FLAT
quantity:        0.000000
avg_px_open:     0.49
avg_px_close:    0.47
commissions:     0.024950 pUSD
realized_return: -0.04082
realized_pnl:    -0.044950 pUSD
```

账户报告：

```text
polymarket/research/2026-07-03-live-ws-t02-simple-strategy/runs/live-t02-round-trip-001/account_report.csv
```

## 11. 结论

这次 live-data round-trip smoke test 说明：

1. data-health 已经集成在当前回测主入口，不需要手动额外跑；
2. 当前 raw live WS 抓包可以通过 `_tools` normalizer 转成 `live_ws_v1`；
3. 真实 live 数据可以转换成 Nautilus 原生 `OrderBookDeltas` / `TradeTick`；
4. Nautilus `BacktestEngine` 能跑策略并生成 fills / positions / account reports；
5. 成交和持仓已经有 CSV 输出；txt 只是 human-readable preview；
6. fee 已接入，reward / rebate 暂不建模；
7. 自动 `run_report.md` 已生成，后续策略实验可以复用同样的 `runs/<run_id>/` 输出结构。

## 12. 限制与下一步

当前限制：

- 这是 smoke strategy，不是 alpha；
- 当前 fill 是 Nautilus backtest fill model 下的 market taker fill，不是 Polymarket L3 queue 复原；
- 没有 settlement / final result；
- `best_bid_ask` / `new_market` 暂时不进入回测数据合同；如后续策略需要这些字段，应先写进 `DATA_CONTRACT_V1.md` 再进入 adapter。

建议下一步：

1. 把自动 report 从纯文本 preview 进化成图表 + BBO 曲线 + 策略事件时间线；
2. 用同一份 T02 live 数据跑 2-3 个极简策略，验证不同策略共用同一 data-health / run / report 结构；
3. 等 IT 数据合同确定后，删除当前 `_tools` 里的临时 normalizer 依赖，把数据入口固定为正式交付格式。
