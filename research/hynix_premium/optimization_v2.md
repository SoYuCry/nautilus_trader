# V2: confirmation and slower averaging

Four variants were specified before running their minute results:

1. Existing baseline.
2. Wait at least 5 minutes after the regular open; require deviation >= 1pp,
   a 0.2pp reversal from the event's observed running extreme, and a favorable
   three-minute change before entry.
3. Variant 2 plus 1.5pp adverse spacing per add from first entry, minimum
   30 minutes between adds, and the same reversal/three-minute confirmation
   before averaging. Reset the observed position extreme at each fill.
4. Variant 3 with net target reduced from 0.5% to 0.3% of cumulative entry
   gross notional. No further variants were tried after September results.

Everything else stays unchanged: 10,000 USDT capital, 1,000 per leg per layer,
maximum five layers, one basket, no time/spread stop, actual historical funding,
5bp fee plus 2bp slippage per execution, completed-minute signals and next open
execution. Pending fills occur irrespective of the next open's price movement.

Selection uses Aug 20-31 only, including terminal open P&L, with score
`net P&L - 0.5 * maximum equity drawdown`. This score is a research preference,
not a statistical confidence measure. Variant 3 was selected. Its selection
period had only one completed basket, so evidence is extremely limited.

| Selection period | Net USDT | Max drawdown USDT | Closed baskets |
|---|---:|---:|---:|
| Baseline | -36.94 | 267.85 | 0 |
| Confirmation | -29.62 | 251.25 | 0 |
| Confirmation + spaced adds | +52.33 | 205.01 | 1 |
| Confirmation + spaced adds + smaller target | +40.35 | 205.01 | 2 |

## Full Aug 20–Sep 11 accounting

| Metric | Baseline | Selected V2 |
|---|---:|---:|
| Realized net USDT | 102.77 | 81.52 |
| Remaining open net USDT | -71.13 | -66.20 |
| Total net USDT | 31.64 | 15.32 |
| Return on 10,000 initial capital | 0.316% | 0.153% |
| Maximum equity drawdown USDT | 267.85 | 205.01 |
| Longest holding | 14.02 days | 10.97 days |
| Closed baskets | 4 | 2 |

## September check

Reset to cash on Sep 1 and apply the selected configuration unchanged through
Sep 11. Baseline net -9.41 USDT; V2 net -56.08. Maximum equity drawdown 107.38
and 89.01 respectively. This fresh-account comparison avoids attributing August
position decisions to September-only signals. The full-period comparison above
separately preserves carried positions.

September had already been examined in the conversation. This is a chronological
diagnostic, not an untouched holdout. The improvement in drawdown does not imply
improved profitability. Keep V2 as a documented experiment, not a validated upgrade.
The strategy still spends days waiting for recovery and does not yet isolate
temporary opening lead/lag from a persistent repricing of the premium.

Verification: default configuration exactly reproduces the original Aug 20
results. V2 order prefixes are invariant under future-price truncation at five
cutoffs. All runs reconcile price P&L, funding, paid fees and estimated exit costs.

Run `python scripts/hynix_optimize_v2.py` to reproduce the selection and reports.
