# PMXT Weather Cross-Event Factor Research

目标：回应固定单 event / 固定 300s horizon 的问题，把天气 PMXT 因子研究扩展为多 event、多 token，并区分两类 label：

1. `wall_clock_*s`：固定秒数，只作为诊断；天气市场早期/末期交易频率差异很大，不能单独作为结论。
2. `valid_obs_*`：向后第 N 个 valid book observation，用于近似事件时间 / 活跃度归一化 horizon。

运行：

```powershell
python polymarket/research/2026-07-09-pmxt-weather-cross-event-factor/weather_cross_event_factor.py `
  --config polymarket/research/2026-07-09-pmxt-weather-cross-event-factor/experiment.yml
```

边界：

- 仍然是 PMXT exploratory factor research，不是 Nautilus 回测，不算成交、fee、PnL。
- `ofi_30s` / `trade_pressure_30s` 继续标记为 flow_clock_sensitive。
- 默认分析两个本地 weather event 中所有 YES token。
