# Compiled-runtime validation evidence (2026-07-13, round 2)

This checkout has no compiled Nautilus build and no Rust toolchain, so
runtime-gated tests skip locally. Validation ran out-of-tree against the
official wheel. This document pins the exact environment so the result is
reproducible; it is workstation evidence, not a CI merge gate (source-build CI
remains an open item).

## Environment

- Source branch: `feature/pmxt-replay-contract-review-fixes` (the commit that
  adds this file; polymarket/ code identical to the committed tree)
- Baseline for skew triage: `ecd258ba28` (pre-replay-contract polymarket code)
- Python: 3.13.13 (miniconda, win-amd64)
- Runtime under test: pip wheel `nautilus_trader==1.229.0`
- Source tree version (not built): 1.231.0-dev
- Method: copy `polymarket/` into a directory outside the repository (so the
  unbuilt source tree does not shadow site-packages), run pytest/backtests
  there.

## Commands and results

Round 3 (same environment, final code): `python -m pytest polymarket/tests -q`
→ **132 passed, 2 failed** (the same two skew failures; the new
ts_init-audit bridge test passes). The strategy backtest and the contract
consistency check were regenerated with the final code; committed artifacts
include `raw_nautilus/` (small) and an `OMITTED_ARTIFACTS.json` manifest
pinning the uncommitted 84MB `data_health.json` by size and sha256.

Round 2:

```text
python -m pytest polymarket/tests -q
  -> 131 passed, 2 failed

python -m polymarket.backtest_v1 \
  --config polymarket/research/2026-07-13-pmxt-research-backtest-v1/experiment_with_strategy.yml
  -> completed; artifacts committed under runs/pmxt-research-strategy-001/

python -m polymarket.backtest_v1 \
  --config polymarket/research/2026-07-03-live-ws-t02-simple-strategy/experiment.yml
  -> strict_capture regression: 2 fills, outputs marked strict_capture_receive_time
```

## The 2 failures are wheel-version skew, not regressions

`test_runner_settlement_metadata_closes_open_position_without_trade_tick` and
`test_runner_strategy_base_uses_post_tick_change_precision` fail identically
when the **pre-change** polymarket code (`git archive ecd258ba28`) runs against
the same 1.229.0 wheel (settlement `EXPIRATION` fill representation and
post-tick-change price handling changed between 1.229 and this 1.231-dev
source). They pass on neither side of the diff, so they cannot be caused by it.

## Real PMXT research backtest observations (334,501 steps)

- Without `replay.allow_ambiguous_ties: true`: run fails closed
  (strategy enabled + 9,433 ordering-ambiguous rows).
- With explicit acceptance: BUY 1 @ 0.81 (taker fee 0.00769 pUSD), official
  settlement payout=1 close; fills.csv time-ordered, every row marked
  `pmxt_research` / `pmxt_research_reconstructed_order_ambiguous_ties`.
- Health gate: `raw_health_ok=false` (1,389 receive-time inversions, mode
  diagnostics) with `mode_health_gate_passed=true`, `replay_clock_verified=true`,
  `blocking_codes=[]`.
- Claim boundary: `claim_scope=plumbing_only`,
  `ambiguous_ties_sensitivity_status=not_run`,
  `performance_claims_allowed=false`.
- Synthetic ts_init audit: policy `synthetic_monotonic_source_time`;
  6,237 events serialized by +1ns; max synthetic offset 4ns.
- Triple sort-key verification: 6,237 tied pairs checked;
  `tie_breaker_verified=true`, `source_row_index_available=true`.
- Input hashes (also recorded in resolved_config.json/summary.json):
  orderbook.parquet sha256 `a07ff5185b0cb2ac...` (29,163,845 bytes).
