# SK hynix cross-market perpetual premium

Run `python scripts/hynix_premium.py` from the repository with matplotlib installed.
Downloads Binance USD-M hourly trade candles from SKHY's July 10, 2026 listing.
The first hourly candle contains only the period after the 15:50 UTC launch.
Outputs raw responses, aligned CSV, summary JSON, PNG and SVG to this directory.

## Definition

US ADR premium (%) = `100 * (10 * SKHYUSDT.close / SKHYNIXUSDT.close - 1)`.
Positive values mean the US ADR perpetual is more expensive on an equivalent-share basis.
Ten ADRs represent one Korean ordinary share. Both contract prices are in USDT;
do not apply another KRW conversion to the Korean contract's USDT price.

Only exactly matching completed candle timestamps are compared. Missing observations
are not forward filled; chart gaps remain visible. Chart timestamps are candle end
boundaries in UTC. Hourly last trades are indicative, not simultaneous executable quotes.

Binance introduced USDT/KRW conversion for Korean equity perpetuals on August 7,
2026 at 02:00 UTC. The plot marks this event. The full series is the quoted
perpetual trade-price ratio, not a reconstructed spot ADR premium at historical FX.
The branch was created from the existing research HEAD (42b7fb839e), which was
already merged through PR #5; it was not created from a freshly fetched develop.

## Interpretation

This is a descriptive spread chart, not evidence of profitable mean reversion.
Perpetual basis, asynchronous underlying trading hours, funding, fees, bid/ask spread,
slippage and ADR conversion constraints can affect convergence and realized returns.
Before a strategy backtest, add both funding histories and executable quotes, and
test mean reversion out of sample with time-of-day and weekend segmentation.

## Rough return calculation

Run `python scripts/hynix_returns.py` after the chart script. Optional
`--cached-funding` uses saved funding files, subject to coverage checks.
Assume 10,000 USDT initial capital and 5,000 USDT entry notional per leg:
short US ADR, long Korean perpetual, fixed quantities until closing.
This is equal-dollar positioning, not the 10:1 equal-share hedge; these hedge
choices have different exposure when the underlying price moves.
Assume 5bp fee plus 2bp slippage on each execution of each leg (illustrative,
not a claim about the user's fee tier). Costs use actual entry and exit notionals.

Scenario returns hold the Korean price unchanged and move the ADR to the target
premium. For premium p0 -> p1, gross P&L = 5000*(p0-p1)/(1+p0).
If the Korean price changes by factor g, equal-dollar gross P&L is multiplied
by g, and exit costs also change. Scenarios exclude unknown future funding.
Recent funding estimates assume each leg stays at 5,000 USDT notional; they
describe realized rate history and are not a prediction of future carry.

The illustrative historical strategy uses the prior 168 hourly observations'
mean and sample standard deviation, enters when the current premium z-score
is at least 2, exits when z <= 0 or after approximately 7 days, and executes at
the next hourly open. Only one position at a time; remaining positions are
closed at the sample end. Fixed 5,000 USDT per leg per entry, no compounding.
Actual funding rate times and mark prices determine signed funding cashflows,
including any special events returned by the API. Boundary convention excludes
entry-time funding and includes exit-time funding.
This is an in-sample illustrative replay with no parameter search, no tick-level
execution model, no liquidation model and no claim of validated expected return.

## Primary sources

- [Binance SKHYUSDT launch](https://www.binance.com/en/support/announcement/detail/6fe9f6dc91544df88dc24ad41211feff)
- [Binance SKHYNIXUSDT launch](https://www.binance.com/en/support/announcement/detail/ed2c6a6160c64eb397b534af90b9d66a)
- [Citi depositary: ORD:DR = 1:10](https://depositaryreceipts.citi.com/adr/guides/pgm_dispabook.aspx?cusip=78392B206&pageId=15&subpageID=111)
- [Binance TradFi pricing and FX regime](https://www.binance.com/en/support/faq/detail/fe7dcdf24f1943d98b368f5f9f744398)
- Public market-data endpoint: `https://fapi.binance.com/fapi/v1/klines`.
