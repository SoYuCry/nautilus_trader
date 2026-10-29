# Finite-capital averaging down: fixed first experiment

Starting capital 10,000 USDT. Enter on a 1pp opening-event deviation from the
frozen pre-open 60-minute median, in either direction. Use 1,000 USDT per leg.
Only one active basket; ignore new entries while holding it. No time/spread stop.
While the basket is losing, add 1,000 per leg at each additional adverse 1pp
deviation from its first actual entry premium, up to five total layers.
No doubling. Gaps can trigger only one additional layer per minute.

Exit the whole basket when estimated P&L after entry costs, realized funding,
and estimated exit costs is at least 0.5% of cumulative entry gross notional:
10 USDT with one layer, 50 USDT with five. These are not returns on total capital.
Minute-close signals execute at the next minute open; exit proceeds may differ
from the signal estimate. Costs are 5bp assumed fee plus 2bp slippage per fill.
Funding is applied at actual settlement times and per-leg historical mark prices.
At a funding boundary, cashflows are applied before orders at that boundary.

Five layers cap cumulative entry gross notional at 10,000 USDT. Market-valued
gross exposure can exceed this as prices change. No additional capital is injected.
No exchange-specific margin tiers, mark-price intraminute liquidation, bid/ask
execution, or borrow/conversion model is included. Estimated equity uses trade
prices, includes funding and an allowance for exit costs, and is sampled each
minute. It is not an exchange liquidation-price calculation.

## Result through September 11, 2026 09:00 UTC

Continuous data: 87,000 aligned completed minutes per leg, starting July 12 23:00.
Initial order July 13 00:01 UTC: short SKHY / long SKHYNIX at 17.3534% premium.
Added at 00:06, 00:35, 01:07 and 01:56 UTC, then held the full five-layer basket.
No profit exits. One basket remains open after 1,448.98 hours (60.37 days).

- Realized net P&L: 0.
- Open price P&L: -844.74 USDT.
- Accrued funding: -247.42 USDT.
- Paid entry costs: -7.00 USDT.
- Estimated exit costs: -7.51 USDT.
- Net if closed at terminal prices: -1,106.67 USDT (-11.07% of initial capital).
- Worst minute-close open net P&L / equity drawdown: -1,142.56 USDT.
- Minimum estimated equity: 8,857.44 USDT.
- Maximum market-valued gross notional: 11,244.80 USDT.

This outcome is specific to the frozen rules and sample start; parameters were
not retuned after seeing it. The early listing regime subsequently had a much
higher quoted premium. One long unresolved position is not an estimate of a
general strategy's win rate. Closed-trade-only performance would hide the loss.

Checks: all 41,624 overlapping cached event bars matched combined archive/REST
open and close prices; truncation at four historical cutoffs preserved all earlier
entry/add orders; independent fixed-quantity position accounting reconciled.

Reproduce: `python scripts/hynix_minute_history.py`, then
`python scripts/hynix_average_down.py`. Funding snapshot files must cover the
same period. Outputs: average_down_summary.json, average_down_orders.csv,
average_down_cycles.csv and average_down_equity.csv.
