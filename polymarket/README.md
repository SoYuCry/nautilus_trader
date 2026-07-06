# Polymarket research/backtest v1

This directory is a repo-local Polymarket research framework.  It is not a
production NautilusTrader adapter package.

The hard boundary is:

```text
source data -> polymarket/adapters -> private source model
            -> private Nautilus bridge -> Nautilus OrderBookDeltas/TradeTick/InstrumentClose
            -> nautilus_trader.backtest.engine.BacktestEngine
```

Backtest execution, order simulation, fills, positions, cash, and reports are
handled by NautilusTrader's native `BacktestEngine`.

Important framing: the private source model in `_core/` is the data contract we want to require from the
IT/data feed, not a long-term compatibility abstraction.  If a current source
does not match it, use a temporary normalizer/patch script to convert into this
shape before backtesting.  Once the IT feed is fixed, the backtest stack should
expect that fixed shape directly.

## Boundaries

- `DATA_CONTRACT_V1.md` describes the Polymarket L2 data shape we should ask
  IT/data to deliver.
- `_core/` holds private implementation details: the source dataclasses and Nautilus-native bridge. Day-to-day users should not need to edit it.
- `adapters/` are temporary ingress shims for current pre-contract files.  They
  should patch inputs into the required contract, not become a broad
  compatibility layer.
- `backtest_v1.py` is the single v1 run/backtest entry point.  It runs data-health first, constructs Nautilus `BacktestEngine`, and writes reports.
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

- `live_ws_v1`: preferred current path for local raw WebSocket captures.
- `live_event_bundle_v1`: provisional data-team event bundle boundary.  The
  final IT feed should be coordinated against `DATA_CONTRACT_V1.md`.

Historical PMXT shims were removed from the runnable entry point for this
live-data-first pass.  PMXT parquet remains a research reference only because
it lacks raw WebSocket message boundaries and source timestamps may invert.

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
- missing source timestamps are counted explicitly, so a low
  `source_time_inversion_count` is not mistaken for good source-time coverage.

The Nautilus bridge also uses `timestamp_received` as the replay clock.  Source
`timestamp` is kept for diagnostics/provenance and must not create look-ahead
ordering.

Standalone health check:

```powershell
python -m polymarket.data_health --ndjson polymarket/tests/fixtures/live_ws_minimal.ndjson
```

Temporary helper for old capture wrappers into strict `live_ws_v1` NDJSON. This lives under `_tools/` because it should disappear once IT delivers the final contract:

```powershell
python -m polymarket._tools.normalize_live_ws_v1 `
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
- Historical `tick_size_change` is supported as an effective tick-size
  timeline.  The Nautilus instrument uses Polymarket's static `0.001`
  expression precision by rule, not by scanning future data.  The effective
  trading tick starts at `0.01` and switches only when a `tick_size_change`
  event arrives.  The runner installs a strategy submit-time guard so orders are
  rejected if their price is illegal under the effective tick at the strategy
  clock time.  That guard is local to the strategy instance and covers
  `submit_order`, `submit_order_list`, and `modify_order`; it does not patch
  Nautilus core classes.
- Strategy code may inherit `polymarket.strategy.PolymarketStrategyBase` to
  explicitly project model prices onto the current effective Polymarket tick
  before creating limit orders.  This does not replace Nautilus
  `instrument.make_price`; Nautilus still handles static `0.001` price
  expression, while the Polymarket base handles dynamic legality:
  `passive BUY` rounds down, `passive SELL` rounds up, `aggressive BUY` rounds
  up, `aggressive SELL` rounds down, `nearest` rounds symmetrically, and
  `strict` rejects off-grid prices.  The runner guard remains a fail-fast
  backstop for strategies that bypass the helper.  Order-price rounding events
  created through `make_polymarket_price` are written into
  `resolved_config.json` under `strategy.polymarket_price_rounding` for audit.
- Settlement is reported as `official`, `inferred`, or `open`.  Official
  resolved metadata is converted into Nautilus `InstrumentClose` plus venue
  `settlement_prices`; inferred settlement is explicitly marked as a
  terminal-price guess; open mode leaves final positions unclosed.  Settlement
  is a system close event and is not represented as a market `TradeTick`.
- Strategy code must subclass `nautilus_trader.trading.strategy.Strategy`;
  `PolymarketStrategyBase` is just a Polymarket-specialized subclass of that
  native Nautilus base.  In runner-managed backtests, the runner injects the
  converted tick-size timeline before `engine.add_strategy`; the base class's
  standalone `0.01` default is only a safe Polymarket v1 initial assumption for
  isolated helper tests or manual construction.

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
`runs/<run_id>/` outputs.  Runs write `original_config.yml`, `resolved_config.json`, `data_health.json`, CSV reports, txt previews, and `run_report.md`.  Any strategy script must subclass
`nautilus_trader.trading.strategy.Strategy`; this package stays focused on
adapters, Nautilus-native conversion, and report post-processing.

