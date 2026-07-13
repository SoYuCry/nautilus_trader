# PMXT history v11 Shanghai 25C YES smoke

This experiment is a focused smoke configuration for G005 to prove that `pmxt_event_v1` can load one large real PMXT event-history token without full-reading the event parquet.

## Input

- Event path: `C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026`
- Adapter: `pmxt_event_v1`
- Market label: Shanghai highest temperature June 9 2026, 25C YES
- Condition ID: `0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41`
- Token / asset ID: `93683348120137447419301462197228170067558545967622008841151707439369216600718`
- Official payout: `1`

Run with:

```powershell
python -m polymarket.backtest_v1 --config polymarket/research/2026-07-08-pmxt-history-v11/experiment.yml
```

## What this smoke checks

- The adapter reads only the PMXT columns needed for canonical replay conversion.
- The adapter pushes parquet filters for the selected `condition_id` / `asset_id` where the parquet engine supports them.
- The selected 25C YES token can pass the existing data-health and Nautilus-native conversion path with `strategy.enabled: false`.
- Reports are written under the experiment-local `./runs` directory using deterministic `run_id: pmxt-history-v11-shanghai-20260609-25c-yes-smoke-001`.

## Expected PMXT caveats

- Replay clock uses `timestamp_received`; source `timestamp` is diagnostic and may invert relative to receive-time order.
- PMXT `best_bid` / `best_ask` are diagnostic-only audit/proxy fields and are not trusted for filtering or execution truth.
- Original WebSocket message boundaries are not recoverable from the normalized PMXT orderbook parquet; one selected row becomes one canonical replay step.

## G005 success / failure criteria

Success:

- The command completes without timing out on parquet ingestion.
- `data_health_ok` is true in the printed summary.
- The run directory exists under `polymarket/research/2026-07-08-pmxt-history-v11/runs/pmxt-history-v11-shanghai-20260609-25c-yes-smoke-001`.
- `resolved_config.json`, `data_health.json`, `summary.json`, and `run_report.md` are produced.
- The resolved adapter metadata records PMXT assumptions/warnings and the selected source files.

Failure:

- The adapter attempts a full event parquet read before selecting the target token and times out or exhausts memory.
- The selection is empty for the configured condition/token.
- Data-health fails before the Nautilus run.
- Conversion or report generation fails before writing the expected run artifacts.
