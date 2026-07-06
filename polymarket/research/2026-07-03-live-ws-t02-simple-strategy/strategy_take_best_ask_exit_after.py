"""Minimal live-data round-trip strategy for Polymarket smoke tests.

This is not an alpha strategy.  It buys once when an ask is available and exits
with a market sell after a configurable receive-time delay once a bid is
available.  The point is to exercise Nautilus fills, fees, and closed-position
reporting on real live WebSocket replay data.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument

from polymarket.strategy import PolymarketStrategyBase


class TakeBestAskExitAfterConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    quantity: Decimal
    exit_after_seconds: int


class TakeBestAskExitAfter(PolymarketStrategyBase):
    """Buy once at market, then sell once after ``exit_after_seconds``."""

    def __init__(
        self,
        instrument_id: str,
        quantity: str = "1",
        exit_after_seconds: int | str = 60,
    ) -> None:
        config = TakeBestAskExitAfterConfig(
            instrument_id=InstrumentId.from_str(instrument_id),
            quantity=Decimal(str(quantity)),
            exit_after_seconds=int(exit_after_seconds),
        )
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.entry_submitted = False
        self.entry_filled = False
        self.exit_submitted = False
        self.entry_fill_ts_init: int | None = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.stop()
            return
        self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        if self.instrument is None:
            return
        book: OrderBook | None = self.cache.order_book(self.config.instrument_id)
        if book is None:
            return

        if not self.entry_submitted:
            if book.best_ask_price() is None:
                return
            self.submit_order(
                self.order_factory.market(
                    instrument_id=self.instrument.id,
                    order_side=OrderSide.BUY,
                    quantity=self.instrument.make_qty(self.config.quantity),
                    time_in_force=TimeInForce.GTC,
                ),
            )
            self.entry_submitted = True
            return

        if not self.entry_filled or self.exit_submitted or self.entry_fill_ts_init is None:
            return
        if book.best_bid_price() is None:
            return
        exit_delay_ns = self.config.exit_after_seconds * 1_000_000_000
        if int(deltas.ts_init) < self.entry_fill_ts_init + exit_delay_ns:
            return
        self.submit_order(
            self.order_factory.market(
                instrument_id=self.instrument.id,
                order_side=OrderSide.SELL,
                quantity=self.instrument.make_qty(self.config.quantity),
                time_in_force=TimeInForce.GTC,
            ),
        )
        self.exit_submitted = True

    def on_order_filled(self, event: OrderFilled) -> None:
        if event.order_side == OrderSide.BUY:
            self.entry_filled = True
            self.entry_fill_ts_init = int(event.ts_init)
