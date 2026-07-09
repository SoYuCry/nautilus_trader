# PMXT L2 Imbalance Factor Research

第一篇 PMXT 历史盘口因子研究。

目标：验证盘口 imbalance / microprice / OFI 等 L2 因子和未来 mid-return label 之间是否有稳定统计关系。

运行：

```powershell
python polymarket/research/2026-07-09-pmxt-l2-imbalance-factor/imbalance_research.py `
  --config polymarket/research/2026-07-09-pmxt-l2-imbalance-factor/experiment.yml
```

稳健性检查：

```powershell
python polymarket/research/2026-07-09-pmxt-l2-imbalance-factor/robustness_research.py `
  --config polymarket/research/2026-07-09-pmxt-l2-imbalance-factor/experiment.yml
```

边界：

- 输入来自 baseline `factor_panel.parquet`。
- 只做 exploratory factor research。
- 不计算成交、手续费、排队、现金、仓位、PnL。
- 不把 future mid-return 解读成可交易收益。

当前解释：

- `imbalance_research.py` 是宽松扫描，用来找候选。
- `robustness_research.py` 是降噪筛选，用来检查 IC 是否被单 event 趋势、重叠 label、时间漂移或 flow 时钟失序污染。
- `ofi_30s` / `trade_pressure_30s` 暂时标为 `flow_clock_sensitive`，上游时钟失序修复前不升级为策略候选。
- 当前 clean book 候选只有 `depth_imbalance_5 @ 300s`；仍必须跨 event 复验后，才考虑进入 Nautilus 策略回测。
