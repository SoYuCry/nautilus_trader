"""Mock strategy for validating Polymarket -> Nautilus execution plumbing.

This is intentionally not an alpha strategy. It submits exactly one market BUY
after the first usable L2 book arrives, so the mock backtest should produce one
fill and one open position if Nautilus BacktestEngine wiring is correct.
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
    """Configuration for ``TakeBestAskOnce``."""

    instrument_id: InstrumentId
    quantity: Decimal


class TakeBestAskOnce(Strategy):
    """Submit one market BUY once the selected Polymarket book has an ask."""

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
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
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

    def on_reset(self) -> None:
        self.instrument = None
        self.submitted = False
