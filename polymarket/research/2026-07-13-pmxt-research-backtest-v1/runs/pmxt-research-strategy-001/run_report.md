# Polymarket backtest run report

## Replay trust boundary

- Replay mode: `pmxt_research`
- Replay clock: `pmxt_replay_timestamp`
- Ordering key: `timestamp,timestamp_received,_original_row_index`
- Tie-breaker: `timestamp_received,_original_row_index (stable mergesort)`
- Data credibility: `pmxt_research_reconstructed_order_ambiguous_ties`
- Adapter: `pmxt_event_v1`
- Ordering ambiguous ties: `true` (explicitly accepted: `true`)
- Execution claims allowed: `false`
- Matching-level truth: `false`
- Boundary: PMXT research backtest: replay order is the deterministic reconstructed PMXT ordering (timestamp, timestamp_received, original row index), not true exchange/message order and not L3 queue truth. Fills, positions, and PnL are research results computed by Nautilus BacktestEngine on a reconstructed L2 replay; they are not matching-level evidence and must not be read as production execution predictions.

## Result summary

| metric | value |
| --- | --- |
| fills | 2 |
| starting_balance | 10000.000000 pUSD |
| ending_balance | 10000.182310 pUSD |
| net_cash_change | 0.18231 pUSD |
| total_fees | 0.00769 pUSD |
| final_position | flat |

## Replay summary

- Engine: `nautilus_trader.backtest.engine.BacktestEngine`
- Nautilus data count: `334501`
- OrderBookDeltas count: `333883`
- TradeTick count: `617`
- InstrumentClose count: `1`
- Settlement mode: `official`
- Settlement enabled: `true`
- Settlement reason: official resolution metadata supplied payout
- Settlement modes: `official` uses resolution metadata; `inferred` is a clearly marked terminal-price guess; `open` leaves final positions unclosed.
- Data health ok: `false`
- Receive-time inversions: `1389`
- Sequence inversions: `0`
- Source-time inversions: `0`
- Future source-time count: `0`

## Instrument

- Alias: `Yes`
- Outcome: `Yes`
- condition_id: `0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41`
- token_id: `93683348120137447419301462197228170067558545967622008841151707439369216600718`
- instrument_id: `0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41-93683348120137447419301462197228170067558545967622008841151707439369216600718.POLYMARKET`

## Fees

- Fee model enabled: `true`
- Fee model: `PolymarketFeeModel`
- Maker rebates enabled: `false`
- Require explicit fee metadata: `false`
- Instrument maker fee: `0`
- Instrument taker fee: `0.05`
- Fee source: `gamma_event.raw.market.feeSchedule.rate`
- Total fees from `raw_nautilus/fills.csv`: `0.00769 pUSD`
- Fee total source column: `commissions`
- Fee warning: none
- Fee total warning: none

## Fills summary

| ts_event | strategy | instrument | side | order_type | quantity | avg_px | fee | fee_pusd | gross_cashflow_pusd | net_cashflow_pusd | liquidity_side | status | replay_mode | data_credibility |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-09 16:42:19+00:00 | TakeBestAskOnce | Yes | SELL | MARKET | 1 | 1 | 0 pUSD | 0 | 1 | 1 | TAKER | FILLED | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |
| 2026-06-07 04:35:40.259000+00:00 | TakeBestAskOnce | Yes | BUY | MARKET | 1 | 0.81 | 0.00769 pUSD | 0.00769 | -0.81 | -0.81769 | TAKER | FILLED | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |

## Positions summary

| strategy | instrument | entry | side | quantity | peak_qty | avg_px_open | avg_px_close | commissions | realized_pnl | realized_return | ts_opened | ts_closed | is_snapshot | replay_mode | data_credibility |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TakeBestAskOnce | Yes | BUY | FLAT | 0.000000 | 1.000000 | 0.81 | 1.0 | ['0.007690 pUSD'] | 0.182310 pUSD | 0.23457 | 2026-06-07 04:35:40.259000+00:00 | 2026-06-09 16:42:19+00:00 | False | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |

## Account summary

| ts_event | total | locked | free | currency | reported | replay_mode | data_credibility |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-07 04:34:30.072000+00:00 | 10000.000000 | 0.000000 | 10000.000000 | pUSD | True | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |
| 2026-06-07 04:35:40.259000+00:00 | 9999.182310 | 0.000000 | 9999.182310 | pUSD | False | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |
| 2026-06-09 16:42:19+00:00 | 9999.182310 | 0.000000 | 9999.182310 | pUSD | False | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |
| 2026-06-09 16:42:19+00:00 | 10000.182310 | 0.000000 | 10000.182310 | pUSD | False | pmxt_research | pmxt_research_reconstructed_order_ambiguous_ties |

## Report files

- `summary.json`
- `data_health.json`
- `fills.csv`
- `positions.csv`
- `account.csv`
- `raw_nautilus/fills.csv`
- `raw_nautilus/positions.csv`
- `raw_nautilus/account.csv`

## Notes

- `TradeTick count` is selected-token `last_trade_price` converted into Nautilus `TradeTick`; it is not strategy fill count.
- Settlement uses Nautilus `InstrumentClose` + venue `settlement_prices`; it is not converted into a market `TradeTick`.
- Strategy fills are recorded in `fills.csv`; raw Nautilus order fields are retained under `raw_nautilus/` for audit.
- Fees use Nautilus' Polymarket fee model when enabled and read the instrument `maker_fee` / `taker_fee` fields.
