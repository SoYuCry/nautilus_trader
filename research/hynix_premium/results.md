# Initial estimate — 2026-09-11 09:00 UTC

Full listing history: 1,506 aligned hourly observations, starting with the partial
July 10 15:50–16:00 UTC launch candle. No missing hourly observations.
Quoted contract premium: latest 40.03%, median 33.38%, range 13.92%–42.61%.
The August 7 FX methodology change is marked; no historical spot/FX reconstruction
is claimed for the earlier contract-price ratio.

## Forward scenarios (not forecasts)

10,000 USDT capital, 5,000 short SKHY and 5,000 long SKHYNIX at entry.
Korean leg price unchanged; positions held at fixed quantities. Deduct 5bp fees
and 2bp slippage per execution per leg. Future funding excluded here.

| Exit premium | P&L after transaction costs | Return on capital |
|---|---:|---:|
| 35% | +165.83 USDT | +1.66% |
| Full-sample median 33.38% | +223.78 USDT | +2.24% |
| 30% | +344.48 USDT | +3.44% |
| 0% | +1,416.41 USDT | +14.16% |
| 50% | -370.14 USDT | -3.70% |

At constant 5,000 USDT per leg, the recent 7 days of realized funding imply a
17.96 USDT expense, versus 51.51 USDT over the last 30 days. Future rates can
change. If the last 7-day average persisted for a 7-day convergence to the
full-sample median, the scenario would net approximately 205.82 USDT (2.06%).
The historical median is a scenario target, not an established equilibrium.

## Illustrative historical replay

Prior 168 hourly premiums define rolling mean and standard deviation. Enter at
z >= 2; exit at z <= 0, after approximately 7 days, or at sample end. Next-bar
open execution; no overlapping positions. See README for funding boundary rules.

- 7 trades, 4 positive after costs, including final sample-end liquidation.
- Price P&L: +226.42 USDT.
- Actual funding cashflows at historical mark prices: -107.51 USDT.
- Assumed fees and slippage: -99.96 USDT.
- Net: +18.95 USDT, or +0.19% of initial capital, no compounding.

This is a small in-sample illustration across a methodology change. No optimized
parameters, out-of-sample validation, executable bid/ask or liquidation analysis.
It does not support a reliable expected return or annualized-return estimate.
Rolling z can fall because its reference mean rises, even while the position loses.
