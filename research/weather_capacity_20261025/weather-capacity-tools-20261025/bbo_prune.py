"""Apply deletion-only carried-BBO constraints to native atomic book batches."""

from bisect import bisect_left, bisect_right, insort
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from nautilus_trader.model import (
    BookAction,
    BookOrder,
    OrderBookDelta,
    OrderBookDeltas,
    OrderSide,
    Quantity,
    RecordFlag,
)

RULE = "carried_bbo_delete_only/v1"


def valid_quote(bid: Any, ask: Any) -> tuple[Decimal, Decimal] | None:
    """Reject missing, nonfinite, crossed or out-of-range BBO fields."""
    try:
        if bid is None or ask is None or bid == "" or ask == "":
            return None
        bid, ask = Decimal(str(bid)), Decimal(str(ask))
        if bid.is_finite() and ask.is_finite() and 0 <= bid < ask <= 1:
            return bid, ask
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


class PrunedBook:
    """Maintain observed depth and append only inferred contradictory-level deletes."""

    def __init__(self) -> None:
        self.levels: dict[tuple[OrderSide, Decimal], BookOrder] = {}
        self._prices: dict[OrderSide, list[Decimal]] = {
            OrderSide.BUY: [],
            OrderSide.SELL: [],
        }
        self.counts: Counter = Counter()

    def apply(
        self, batch: OrderBookDeltas, quote: tuple[Any, Any] | None
    ) -> OrderBookDeltas:
        """Preserve timestamps, sequence, quantities and one final F_LAST marker."""
        deltas = batch.deltas
        if not deltas or not deltas[-1].flags & int(RecordFlag.F_LAST):
            raise ValueError("Native batch has no F_LAST")
        for delta in deltas:
            if delta.action == BookAction.CLEAR:
                self.levels.clear()
                for prices in self._prices.values():
                    prices.clear()
            else:
                key = delta.order.side, Decimal(str(delta.order.price))
                prices = self._prices[key[0]]
                if (
                    delta.action == BookAction.DELETE
                    or delta.order.size.as_decimal() == 0
                ):
                    if key in self.levels:
                        del self.levels[key]
                        prices.pop(bisect_left(prices, key[1]))
                else:
                    if key not in self.levels:
                        insort(prices, key[1])
                    self.levels[key] = delta.order
        self.counts["batches"] += 1
        if quote is None:
            self.counts["snapshot_without_trailing_quote"] += 1
            return batch
        parsed = valid_quote(*quote)
        if parsed is None:
            self.counts["invalid_or_incomplete_quote_batches"] += 1
            return batch
        bid, ask = parsed
        self.counts["valid_quote_batches"] += 1
        bids, asks = self._prices[OrderSide.BUY], self._prices[OrderSide.SELL]
        bid_cut, ask_cut = bisect_right(bids, bid), bisect_left(asks, ask)
        removed = [(OrderSide.BUY, p) for p in bids[bid_cut:]] + [
            (OrderSide.SELL, p) for p in asks[:ask_cut]
        ]
        del bids[bid_cut:]
        del asks[:ask_cut]
        self.counts["pruned_batches"] += bool(removed)
        self.counts["deleted_levels"] += len(removed)
        if removed:
            last = deltas[-1]
            deltas[-1] = OrderBookDelta(
                last.instrument_id,
                last.action,
                last.order,
                last.flags & ~int(RecordFlag.F_LAST),
                last.sequence,
                last.ts_event,
                last.ts_init,
            )
            for n, key in enumerate(removed):
                order = self.levels.pop(key)
                deltas.append(
                    OrderBookDelta(
                        last.instrument_id,
                        BookAction.DELETE,
                        BookOrder(
                            order.side,
                            order.price,
                            Quantity(0, order.size.precision),
                            0,
                        ),
                        int(RecordFlag.F_LAST) if n == len(removed) - 1 else 0,
                        last.sequence,
                        last.ts_event,
                        last.ts_init,
                    )
                )
            batch = OrderBookDeltas(last.instrument_id, deltas)
        actual_bid = bids[-1] if bids else None
        actual_ask = asks[0] if asks else None
        self.counts["missing_carried_bid_level"] += (
            OrderSide.BUY,
            bid,
        ) not in self.levels
        self.counts["missing_carried_ask_level"] += (
            OrderSide.SELL,
            ask,
        ) not in self.levels
        self.counts["empty_boundary_comparisons"] += (
            actual_bid is None or actual_ask is None
        )
        normalized = (
            actual_bid if actual_bid is not None else Decimal(0),
            actual_ask if actual_ask is not None else Decimal(1),
        )
        self.counts["remaining_bbo_mismatches"] += normalized != parsed
        return batch
