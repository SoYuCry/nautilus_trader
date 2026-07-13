# PMXT weather maker diagnostics

This is a research-only diagnostic for the `depth_imbalance_1` signal found in
the cross-event weather factor scan.

It does **not** claim executable PnL. Instead it asks the next question:

> If the signal is used as a maker/quote-skew signal, do hypothetical passive
> fills have positive post-fill markout after crude queue assumptions?

Key boundaries:

- data source: curated PMXT event parquet;
- replay clock: `timestamp_received`;
- probe sampling: last valid book state per fixed receive-time interval;
- fill proxy: future trades touching/crossing the submitted best bid/ask price;
- queue proxy: required trade volume = displayed top size x queue fraction + order size;
- markout: future mid-price after the simulated fill;
- no fees, rebates, cash, inventory, Nautilus order state, or executable PnL.

Run:

```powershell
python polymarket\research\2026-07-10-pmxt-weather-maker-diagnostics\maker_diagnostics.py `
  --config polymarket\research\2026-07-10-pmxt-weather-maker-diagnostics\experiment.yml
```
