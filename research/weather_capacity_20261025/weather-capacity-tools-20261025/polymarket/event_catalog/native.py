"""Native Catalog encoding helpers for semantic order-book batches."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model import OrderBookDelta, OrderBookDeltas, RecordFlag


def initial_book_probabilities_catalog(
    root: Path,
    *,
    start_ns: int,
) -> dict[tuple[str, str], float | None]:
    """Return book probabilities as of the requested source-clock boundary."""
    from nautilus_trader.model import BookType, OrderBook
    from nautilus_trader.persistence import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(root))
    result = {}
    for instrument in catalog.instruments():
        key = (instrument.info["condition_id"], instrument.info["token_id"])
        book = OrderBook(instrument.id, BookType.L2_MBP)
        result[key] = None
        for batch in restore_order_book_batches(
            catalog.query_order_book_deltas([str(instrument.id)])
        ):
            if batch.ts_event > start_ns:
                break
            book.apply_deltas(batch)
        bid, ask = book.best_bid_price(), book.best_ask_price()
        prices = [Decimal(str(p)) for p in (bid, ask) if p is not None]
        result[key] = float(sum(prices) / len(prices)) if prices else None
    return result


def flatten_order_book_batches(
    batches: Iterable[OrderBookDeltas],
) -> list[OrderBookDelta]:
    """Flatten callback batches for the v2 Catalog while preserving ``F_LAST``."""
    return [delta for batch in batches for delta in batch.deltas]


def restore_order_book_batches(
    deltas: Iterable[OrderBookDelta],
) -> list[OrderBookDeltas]:
    """Restore callback batches from a v2 Catalog's flat delta rows."""
    result: list[OrderBookDeltas] = []
    pending: list[OrderBookDelta] = []
    instrument_id = None
    for delta in deltas:
        if instrument_id is None:
            instrument_id = delta.instrument_id
        elif delta.instrument_id != instrument_id:
            raise ValueError("flat order-book stream contains more than one instrument")
        if pending and delta.ts_init != pending[0].ts_init:
            raise ValueError("order-book batch timestamp changed before RecordFlag.F_LAST")
        pending.append(delta)
        if delta.flags & int(RecordFlag.F_LAST):
            result.append(OrderBookDeltas(instrument_id, pending))
            pending = []
    if pending:
        raise ValueError(
            f"order-book stream for {instrument_id} ends without RecordFlag.F_LAST",
        )
    return result
