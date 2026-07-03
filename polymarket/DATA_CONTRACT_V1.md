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

The current Nautilus bridge fails fast by default on this event until we add an
instrument-epoch model.

## Hard requirements

1. Do not omit explicit receive time.
2. Do not use source `timestamp` as a substitute for `timestamp_received`.
3. Do not sort source rows to hide receive-time inversions.
4. Preserve atomic message/update boundaries.
5. Keep source timestamp pathologies visible for diagnostics:
   - future source time;
   - source timestamp inversion;
   - late delivery / large delay;
   - missing source timestamp coverage.
6. Treat PMXT-derived formats as temporary research inputs, not the target
   contract.

## Current temporary ingress paths

- `live_ws_v1`: current preferred local raw WebSocket capture path.
- `normalize_live_ws_v1.py`: patch/normalizer for capture wrappers into the
  strict live WS contract shape.
- `pmxt_*`: legacy/questionable research inputs only.

