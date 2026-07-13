# Polymarket L2 data contract v1

This is the format the backtest stack expects.  It is not meant to be a
permanent multi-source compatibility layer.  If an interim file, PMXT extract,
or raw capture differs from this contract, use a one-off normalizer/patch script
to convert it before backtesting.  Once the IT/data feed is agreed, we should ask
for this shape directly.

## Required replay model

The canonical in-code representation is the private dataclass model `polymarket._core.models.PolymarketL2DatasetV1`.
Conceptually it is:

```text
dataset
  metadata
    market_metadata[]
  steps[]
    sequence
    timestamp_received
    timestamp
    updates[]
```

One `step` is one atomic replay unit.  For live raw WebSocket captures, that
means one raw WebSocket message.  For future IT delivery, the feed should
preserve an equivalent atomic message/update boundary instead of flattening it
into ambiguous rows.

## Market metadata snapshot

Replay events are not enough for realistic backtests.  The live data delivery
should also include a market-level metadata snapshot captured around the same
collection window.  This metadata is dataset-level audit/config input; it is
not replayed as an order-book event.

Required for fee-aware backtests:

| Field | Required | Meaning |
| --- | --- | --- |
| `condition_id` | yes | Polymarket condition id / CLOB market id. |
| `token_id` | preferred | Outcome token id. If omitted, metadata applies to the whole condition. |
| `outcome` | preferred | Human-readable outcome label, e.g. `Yes` / `No`. |
| `maker_fee` | yes | Effective maker fee rate. Usually `0`. |
| `taker_fee` | yes | Effective taker fee rate / `feeSchedule.rate` decimal fraction, e.g. `0.05`. |
| `fee_source` | yes | Source of the fee value, e.g. `clob_market_info.feeSchedule.rate`. |
| `category` | optional | Market category/tag used to audit fee schedules. |
| `minimum_tick_size` | preferred | Audit copy of the market minimum tick at capture time. The v1 replay rule still starts from Polymarket's standard `0.01` effective tick and changes only on `tick_size_change`. |
| `tick_size_source` | preferred | Source of the tick value, e.g. `clob_market_info.minimum_tick_size` or `market_metadata`. |
| `resolution_status` | required when resolved | Settlement / UMA resolution status. |
| `resolution_time` | required when resolved | UTC time when the token payout becomes known. |
| `token_payout` | required when resolved | Per-token settlement payout in `[0, 1]`; winner is usually `1`, loser `0`. |
| `winner` | optional | Boolean token winner flag used to audit `token_payout`. |
| `resolution_source` | required when resolved | Source of settlement fields, e.g. API snapshot or resolution file. |

Example sidecar shape accepted by the current `live_ws_v1` adapter:

```json
{
  "markets": [
    {
      "condition_id": "0x...",
      "minimum_tick_size": "0.01",
      "maker_fee": "0",
      "fee_source": "clob_market_info.feeSchedule.rate",
      "feeSchedule": {"rate": "0.05"},
      "category": "weather",
      "resolution_status": "resolved",
      "resolution_time": "2026-06-26T03:00:00Z",
      "resolution_source": "clob/gamma resolution snapshot",
      "tokens": [
        {"token_id": "123...", "outcome": "Yes", "payout": "1", "winner": true},
        {"token_id": "456...", "outcome": "No", "payout": "0", "winner": false}
      ]
    }
  ]
}
```

If this metadata is missing, the runner can still use explicit
`instrument.maker_fee` / `instrument.taker_fee` overrides in the experiment
config, but those should be treated as manual overrides rather than the target
production data contract.

## Step fields

| Field | Required | Meaning |
| --- | --- | --- |
| `sequence` | yes | Local replay sequence. Must be strictly increasing inside the dataset. |
| `timestamp_received` | yes | Collector receive time in UTC. This is the replay clock. Must be non-decreasing in file/order. |
| `timestamp` | optional but preferred | Polymarket/source event timestamp. Used for diagnostics only, not for replay ordering. |
| `updates` | yes | One or more L2/trade/tick-size updates inside the atomic step. |

## Update types

### `book`

Snapshot for one `market + asset_id`.

Required:

- `event_type = "book"`
- `market` / condition id
- `asset_id` / token id
- `bids[]`
- `asks[]`

Each level is:

- `price`
- `size`

### `price_change`

Incremental L2 price-level update.

Required:

- `event_type = "price_change"`
- `market`
- `asset_id`
- `side`: `BUY` or `SELL`
- `price`
- `size`

`size = 0` means delete that price level.

### `trade`

Trade print, mapped from Polymarket `last_trade_price`.

Required:

- `event_type = "trade"`
- `market`
- `asset_id`
- `side`
- `price`
- `size`

### `tick_size_change`

Historical tick-size transition.

Required:

- `event_type = "tick_size_change"`
- `market`
- `asset_id`
- `old_tick_size`
- `new_tick_size`

The v1 Nautilus bridge applies this as an effective tick-size timeline.  The
Nautilus instrument always uses Polymarket's static expression precision
`0.001`; this is a rule, not something inferred by scanning future data.  The
effective trading tick starts at `0.01` and changes only when a
`tick_size_change` event arrives, normally `0.01 -> 0.001`.  Source updates and
strategy order prices are validated against the effective tick at replay time.
If the event says `old_tick_size` does not match the current effective tick, or
`new_tick_size` does not match the v1 fine precision, conversion fails instead
of silently guessing.

### Resolution / settlement

Settlement is metadata, not a market-data update.  The target feed should
provide per-token payout in `market_metadata[]` once a market is resolved.  The
runner maps it to Nautilus `InstrumentClose` plus venue `settlement_prices`, so
open positions are closed by the engine's settlement path.  Do not encode
settlement as a synthetic `trade` / `TradeTick`.

The runner reports one of three settlement modes at the top of each run report:

- `official`: official/resolved metadata supplies a payout.  Resolved metadata
  is fail-fast: `resolution_time` and per-token `token_payout` or `winner` must
  be present, and `winner` must agree with a 0/1 payout.
- `inferred`: no official settlement metadata exists, but terminal market data
  clearly converges to 0 or 1.  The runner may add a clearly marked inferred
  `InstrumentClose` for research convenience and records the terminal
  bid/ask/last-trade evidence.
- `open`: no official result and no safe terminal-price inference.  No
  `InstrumentClose` is generated; reports leave final positions open.

## Hard requirements

1. Do not omit explicit receive time.
2. Do not use source `timestamp` as a substitute for `timestamp_received`.
3. Do not sort source rows to hide receive-time inversions.  (The PMXT
   research mode sorts on its declared contract key, but the resulting
   receive-time inversions stay fully visible in `data_health.json` as
   diagnostics and the output is labelled `pmxt_research`, never strict
   capture.)
4. Preserve atomic message/update boundaries.
5. Keep source timestamp pathologies visible for diagnostics:
   - future source time;
   - source timestamp inversion;
   - late delivery / large delay;
   - missing source timestamp coverage.
6. Treat PMXT-derived formats as temporary research inputs, not the target
   contract.
7. Preserve tick-size transition events and resolved per-token payout metadata
   when available.  Do not force the backtest config to hard-code these values.

## Replay trust modes

`polymarket/replay_contract.py` is the single shared definition of replay-order
semantics.  Every runner config resolves to one of two modes:

### `strict_capture` (default)

For raw WebSocket or equivalent captures that preserve a real local receive
clock and message boundaries.

- Replay clock: `timestamp_received`, in capture order.
- `receive_time_inversion` and `sequence_inversion` are hard failures.
- PMXT exploratory data is refused in this mode.

### `pmxt_research` (explicit opt-in only)

For curated PMXT event directories loaded through `pmxt_event_v1`.  Selected by
writing `replay: {mode: pmxt_research}` in the experiment config; it applies
only to the PMXT adapter, and non-PMXT adapters are refused in this mode.

- Ordering key: stable mergesort on
  `timestamp, timestamp_received, _original_row_index` (the adapter's sort).
- Replay clock: source `timestamp` with `timestamp_received` fallback — the
  same `replay_timestamp` used by PMXT factor research, so factor panels and
  Nautilus research backtests replay the identical step stream.
- No dedup; tied timestamp groups with differing content are flagged as
  ordering-ambiguous in `source_quality` and downgrade the run's
  `data_credibility`.
- `receive_time_inversion` is a diagnostic, not a blocker; all other health
  errors still block.  The runner additionally verifies the contract clock is
  non-decreasing before conversion.
- The reconstructed order is deterministic and reproducible but is **not**
  exchange message order and not L3 queue truth.  Fills/PnL from this mode are
  research results on a reconstructed L2 replay, never matching-level evidence.
- `resolved_config.json`, `summary.json`, and `run_report.md` carry a
  `replay` provenance block (mode, clock, ordering key, tie-breaker,
  data-credibility grade, disclaimer) so outputs cannot masquerade as strict
  capture runs.

## Current temporary ingress paths

- `live_ws_v1`: current preferred local raw WebSocket capture path.
- `normalize_live_ws_v1.py`: patch/normalizer for capture wrappers into the
  strict live WS contract shape.
- `pmxt_event_v1`: temporary research/history ingress for curated PMXT event
  directories.  It requires `event_dir`, `condition_id`, and `asset_id`; filters
  a single selected token; sorts on the shared PMXT research ordering key
  (`timestamp, timestamp_received, _original_row_index`) so the replay clock is
  the source `timestamp` with receive-time fallback; and preserves PMXT
  `best_bid` / `best_ask` only as diagnostic/reference fields, not
  authoritative filtering or execution data.  PMXT event/message boundaries
  are not recoverable from the row format.  The adapter records assumptions and
  warnings so runner `resolved_config.json` exposes the caveats under
  `adapter.assumptions` and `adapter.warnings`.  Backtests require the explicit
  `pmxt_research` replay mode described above.

