"""Minimal Nautilus-native demonstration strategy for the PMXT research backtest.

Buys the best ask once, then holds; settlement closes the position.  This is a
plumbing demonstration for the pmxt_research replay mode, not a trading idea.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy


class TakeBestAskOnceConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    quantity: Decimal


class TakeBestAskOnce(Strategy):
    def __init__(self, instrument_id: str, quantity: str = "1") -> None:
        config = TakeBestAskOnceConfig(
            instrument_id=InstrumentId.from_str(instrument_id),
            quantity=Decimal(str(quantity)),
        )
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.submitted = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.stop()
            return
        self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        if self.submitted or self.instrument is None:
            return
        book: OrderBook | None = self.cache.order_book(self.config.instrument_id)
        if book is None or book.best_ask_price() is None:
            return
        order = self.order_factory.market(
            instrument_id=self.instrument.id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.config.quantity),
            time_in_force=TimeInForce.GTC,
        )
        self.submitted = True
        self.submit_order(order)
