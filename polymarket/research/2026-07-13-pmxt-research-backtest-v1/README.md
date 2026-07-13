# PMXT research backtest v1 (shared replay contract)

日期：2026-07-13

这个实验目录是 PMXT research replay contract 的最小可复现实验：同一份 PMXT
event 数据，用同一套排序语义，先做因子研究，再进入 Nautilus 原生
`BacktestEngine` research backtest。

## 共享 replay contract（pmxt_research mode）

单一定义在 `polymarket/replay_contract.py`：

- 输入：PMXT event 目录（`orderbook.parquet` + `event_index.json` +
  `gamma_event.raw.json` + `manifest.json`），按 `condition_id + asset_id`
  选择单 token。
- 排序 key：`timestamp, timestamp_received, _original_row_index`（稳定
  mergesort；tie-breaker 依次是 receive time、物理行序）。
- replay clock：source `timestamp`，缺失时回退 `timestamp_received`。
- 去重：不去重；tied group 内容不同的行被标记为 ordering ambiguous。
- `receive_time_inversion` 在此 mode 下是诊断不是 blocker；
  `sequence_inversion` 等其他 error 仍然 blocker。
- 声明：重建顺序是确定性的，但**不是**交易所真实消息顺序，不是 L3 queue
  truth；fill/PnL 是重建 L2 replay 上的研究结果，不是撮合级证据。

默认 `strict_capture` mode 仍然拒绝 `pmxt_event_v1`；只有 config 显式写
`replay: {mode: pmxt_research}` 才放行，且输出全部带可信度标记。

## 运行命令（仓库根目录）

1. Contract 一致性检查（无需编译版 Nautilus runtime）：

```powershell
python polymarket/research/2026-07-13-pmxt-research-backtest-v1/check_contract_consistency.py `
  --config polymarket/research/2026-07-13-pmxt-research-backtest-v1/experiment.yml
```

2. 数据 replay smoke backtest（需要编译版 Nautilus runtime）：

```powershell
python -m polymarket.backtest_v1 --config polymarket/research/2026-07-13-pmxt-research-backtest-v1/experiment.yml
```

3. 带最小策略的 research backtest（需要编译版 Nautilus runtime）：

```powershell
python -m polymarket.backtest_v1 --config polymarket/research/2026-07-13-pmxt-research-backtest-v1/experiment_with_strategy.yml
```

## 2026-07-13 contract 检查结果（真实数据）

对 `highest-temperature-in-shanghai-on-june-9-2026` 的 25°C YES token
（与 2026-07-08 因子 baseline 完全相同的输入）：

- steps: 334,501；两次独立 load 的 replay 顺序完全一致；
- contract replay clock 非递减、sequence 严格递增；
- `receive_time_inversion` 诊断计数 1,389（pmxt_research mode 下非 blocker）；
- ordering ambiguous ties：4,694 组 / 9,433 行（credibility 会降级为
  `pmxt_research_reconstructed_order_ambiguous_ties`）；
- 因子 baseline `build_factor_panel` 的行顺序与 replay clock 与 backtest
  将消费的 step 流逐行一致。

完整机器可读结果见 `contract_consistency_result.json`。

## 信任边界

本目录任何 run 的输出都必须按 `run_report.md` 顶部的
"Replay trust boundary" 段解读：

- `replay_mode: pmxt_research`；
- `execution_claims_allowed: false`；
- 不可以把 fills/PnL 当作可交易收益或真实 queue/maker fill 证据；
- 与 strict capture run 的结果不可直接混排比较。
