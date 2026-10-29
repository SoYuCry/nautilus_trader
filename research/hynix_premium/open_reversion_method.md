# One-percentage-point opening-event experiment

## Snapshot result

July 13–September 11, 2026: all 86 eligible opening windows complete. 27 windows
had no qualifying signal. 59 trades, 19 net winners (32.2%). Gross price P&L
-493.86 USDT, historical funding cashflow 0.00, assumed transaction costs 825.46,
net -1,319.32 (-13.19% of 10,000 initial capital). Fixed notionals, no compounding.
17 baseline-reversion exits, 28 stops, 14 timeouts. Exit reason is not equivalent
to profitability after costs. Earlier period net -1,123.04; August 15 onward net
-196.28. Both periods use the same rules. All 59 trades passed future-price
truncation invariance and P&L reconciliation checks.

## Rules

Fixed before inspecting minute results; no threshold optimization.

- Sample: first eligible regular opening after SKHY's launch, through the saved
  hourly snapshot. KR 09:00 KST (00:00 UTC), US 09:30 EDT (13:30 UTC).
- Exclude weekends, KR July 17 / August 17 and US September 7 holidays.
  Only regular opens; Korean NXT and US extended-hours opens are not modeled.
- Require both contracts to have every minute in the full event window.
  Missing data windows are reported explicitly, not counted as no-signal events.
- Baseline: median of 60 completed minute-close premiums immediately before
  the opening. Freeze it for that event.
- During the first 60 minutes, enter on the first absolute deviation of at
  least 1 percentage point. Short US / long KR for an upward deviation, reverse
  for a downward deviation. At most one trade per event.
- Signal from completed minute t; execution uses minute t+1 open. This assumes
  immediate next-boundary execution and includes slippage, not measured latency.
- Equal entry notionals: 5,000 USDT per leg, 10,000 initial capital; fixed
  quantities per trade, no compounding or leverage amplification.
- Exit when close premium is within 0.1 percentage point of the baseline
  (or crosses it); stop if it moves another 1 percentage point adversely from
  the actual entry premium; time exit after 120 minutes. Execute at next open.
  Minute intrabar stop crossings are not modeled; actual stop fills may differ.
- Costs: assumed 5bp fee + 2bp slippage on both entry/exit notionals on both legs.
- Funding: saved realized per-leg rates and mark prices, signed by direction;
  entry boundary excluded and exit boundary included.
- Report earlier/later periods split at August 15; no tuning on either period.
  This is a chronological diagnostic, not a truly untouched holdout: prior
  conversation already examined aggregate history.
- Trade-close drawdown omits intratrade mark-to-market and liquidation risk.

The series is a quoted perpetual-price ratio. No historical FX reconstruction
or independent index-price classification is included in this quick experiment.
No historical bid/ask execution is available in these candle files.

Run `python scripts/hynix_open_reversion.py`. Completed minute windows are cached,
so rerunning retries failed downloads without re-fetching complete windows.

Holiday references:
- https://english.visitkorea.or.kr/svc/contents/infoHtmlView.do?vcontsId=140038
- https://global.seoul.go.kr/web/news/news/bordContDetail.do?brd_no=2&lang=EN&mode=W&post_no=58BBD7AEB9AD018EE063C0A8A0239714
- https://www.nyse.com/trade/hours-calendars
