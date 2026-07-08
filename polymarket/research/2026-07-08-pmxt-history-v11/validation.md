# PMXT history v11 smoke validation

This is a research smoke validation note for Ultragoal G005 evidence. It is not a boss report.

## Validation commands

| Check | Result | Notes |
| --- | --- | --- |
| `python -m pytest polymarket\tests\test_pmxt_event_adapter.py polymarket\tests\test_data_health.py polymarket\tests\test_fees.py -q` | 20 passed, 1 skipped | Skip reason: Nautilus compiled runtime not built. |
| `python -m pytest polymarket\tests\test_nautilus_native_bridge.py polymarket\tests\test_backtest_v1_native_runner.py -q` | 4 skipped | Skipped because `nautilus_trader.core.data` is missing / compiled Nautilus runtime not built. |
| `python -m polymarket.backtest_v1 --config polymarket/research/2026-07-08-pmxt-history-v11/experiment.yml` | Failed at import | `ModuleNotFoundError: No module named 'nautilus_trader.core.data'`. Classified as an environment validation gap, not a PMXT adapter failure. |

## Real June 9 event smoke

Pure adapter/data-health smoke passed on the real June 9 event.

- Steps: 334501
- Update counts:
  - `price_change`: 332608
  - `book`: 1275
  - `trade`: 617
  - `tick_size_change`: 1
- `data_health_ok`: true
- `receive_time_inversion_count`: 0
- `source_time_inversion_count`: 89057
- `source_delay_over_threshold_count`: 177530
- Fee metadata: `taker_fee`: 0.05
- Settlement payout: 1

## Classification

The pure PMXT adapter/data-health path validated successfully. Native Nautilus checks remain blocked by the local environment because the compiled Nautilus runtime is not built and `nautilus_trader.core.data` is unavailable.
