# Polymarket research/backtest v1

This directory is a repo-local Polymarket research framework.  It is not a
production NautilusTrader adapter package.

The hard boundary is:

```text
source data -> polymarket/adapters -> polymarket/models.py
            -> polymarket/nautilus_native.py -> Nautilus OrderBookDeltas/TradeTick
            -> nautilus_trader.backtest.engine.BacktestEngine
```

Backtest execution, order simulation, fills, positions, cash, and reports are
handled by NautilusTrader's native `BacktestEngine`.

## Boundaries

- `adapters/` own source compatibility for PMXT parquet, PMXT curated event
  folders, local live raw WebSocket captures, and future event bundles.
- `models.py` defines the canonical source-normalization model used between
  adapters and the Nautilus-native bridge.
- `nautilus_native.py` converts the canonical source model into Nautilus native
  `OrderBookDeltas` and `TradeTick` objects.
- `backtest_v1.py` is the single v1 run/backtest entry point.  It constructs and
  runs `nautilus_trader.backtest.engine.BacktestEngine`.
- `research/` contains data/WS studies and future Nautilus-native experiments.
- `ideas/` contains long-horizon brainstorms.
- `boss_reports/` contains polished decision reports.

Documented CLI form:

```powershell
python -m polymarket.backtest_v1 --config polymarket/research/<topic>/experiment.yml
```

Run commands from the repository root so `polymarket.*` absolute imports are
stable.

## Source status

- `pmxt_parquet_v1`: legacy/questionable.  PMXT parquet lacks raw WebSocket
  message boundaries and source timestamps may invert.
- `pmxt_event_v1`: legacy/questionable because it is derived from PMXT data.
- `live_ws_v1`: preferred current path for local raw WebSocket captures.
- `live_event_bundle_v1`: future data-team event bundle boundary.

## Current replay support boundary

- L2 snapshots (`book`) are converted into Nautilus snapshot
  `OrderBookDeltas`.
- L2 incremental updates (`price_change`) are converted into Nautilus
  `OrderBookDeltas`; raw WebSocket message boundaries are preserved by the live
  adapter when available.
- Trade prints (`last_trade_price`) are converted into Nautilus `TradeTick`.
- Historical `tick_size_change` is fail-fast by default.  Live Nautilus
  Polymarket data code supports instrument updates, but this historical bridge
  still needs an explicit instrument-epoch model before dynamic tick size can be
  claimed as backtest-supported.
- Strategy code must subclass `nautilus_trader.trading.strategy.Strategy`.

## Validation gap

This checkout can run source-level and adapter contract tests without the
compiled Nautilus runtime.  Native bridge and runner execution tests require a
built Nautilus environment because they import compiled modules such as
`nautilus_trader.core.data`.

Required built-runtime check:

```powershell
python -m pytest polymarket\tests\test_nautilus_native_bridge.py polymarket\tests\test_backtest_v1_native_runner.py -q
```

## Experiment convention

Each future `research/<date-topic>/` directory may own its `experiment.yml`,
optional Nautilus-native `strategy.py`, `report.md`, and representative
`runs/<run_id>/` outputs.  Runs must write both `original_config.yml` and
`resolved_config.json`.  Any strategy script must subclass
`nautilus_trader.trading.strategy.Strategy`; this package stays focused on
adapters, Nautilus-native conversion, and report post-processing.
