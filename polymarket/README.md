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

Important framing: `models.py` is the data contract we want to require from the
IT/data feed, not a long-term compatibility abstraction.  If a current source
does not match it, use a temporary normalizer/patch script to convert into this
shape before backtesting.  Once the IT feed is fixed, the backtest stack should
expect that fixed shape directly.

## Boundaries

- `DATA_CONTRACT_V1.md` describes the Polymarket L2 data shape we should ask
  IT/data to deliver.
- `models.py` implements that required data contract in Python dataclasses.
- `adapters/` are temporary ingress shims for current pre-contract files.  They
  should patch inputs into the required contract, not become a broad
  compatibility layer.
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

These are current ingress paths, not equal long-term targets:

- `pmxt_parquet_v1`: legacy/questionable.  PMXT parquet lacks raw WebSocket
  message boundaries and source timestamps may invert.
- `pmxt_event_v1`: legacy/questionable because it is derived from PMXT data.
- `live_ws_v1`: preferred current path for local raw WebSocket captures.
- `live_event_bundle_v1`: provisional data-team event bundle boundary.  The
  final IT feed should be coordinated against `DATA_CONTRACT_V1.md`.

## Data-health gate before backtest

`python -m polymarket.backtest_v1` now runs a mandatory data-health check after
adapter loading and before Nautilus conversion/engine execution:

- `timestamp_received` must be non-decreasing in adapter output order.
- local replay `sequence` must be strictly increasing.
- failures stop the run and write `data_health.json`; they are not repaired by
  sorting.
- source timestamp inversions and future source timestamps are reported as
  diagnostics so we can judge severity, but replay chronology remains
  `timestamp_received`.

The Nautilus bridge also uses `timestamp_received` as the replay clock.  Source
`timestamp` is kept for diagnostics/provenance and must not create look-ahead
ordering.

Standalone health check:

```powershell
python -m polymarket.data_health --ndjson polymarket/tests/fixtures/live_ws_minimal.ndjson
```

Normalize a capture wrapper into strict `live_ws_v1` NDJSON:

```powershell
python -m polymarket.scripts.normalize_live_ws_v1 `
  --input path/to/raw_capture.ndjson `
  --output path/to/live_ws_v1.ndjson
```

`live_ws_v1` requires a real receive timestamp
(`recv_wall_time_utc`, `timestamp_received`, or `received_at`).  It refuses to
substitute Polymarket source `timestamp` as receive time.

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
