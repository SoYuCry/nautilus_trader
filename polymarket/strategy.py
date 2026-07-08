"""Polymarket strategy helpers on top of NautilusTrader ``Strategy``.

Nautilus instruments carry static price precision.  Polymarket adds a separate
time-varying effective tick rule: selected binary option tokens are normally
tradable on ``0.01`` ticks, then may switch to ``0.001`` near the tails.  This
module keeps that Polymarket rule in strategy space without mutating Nautilus
core ``Instrument.make_price`` behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price
from nautilus_trader.trading.strategy import Strategy

from polymarket._core.price_rounding import PolymarketRoundDirection
from polymarket._core.price_rounding import PolymarketRoundIntent
from polymarket._core.price_rounding import project_price_to_effective_tick
from polymarket._core.tick_size import resolve_effective_tick_size


@dataclass(frozen=True, slots=True)
class PolymarketTickSizeChange:
    """Effective tick-size transition visible to strategy code."""

    effective_from_ts_init: int
    new_tick_size: Decimal
    old_tick_size: Decimal | None = None
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class PolymarketPriceRoundingEvent:
    """Audit event for one strategy-level price projection onto a tick grid."""

    ts_ns: int
    original_price: Decimal
    rounded_price: Decimal
    tick_size: Decimal
    side: str
    intent: PolymarketRoundIntent
    direction: PolymarketRoundDirection


class PolymarketStrategyBase(Strategy):
    """Nautilus strategy base with explicit Polymarket dynamic-tick helpers.

    Concrete research strategies still run as normal Nautilus strategies.  The
    extra methods here only help strategy authors project continuous model
    prices onto the effective Polymarket tick grid before constructing limit
    orders.  The runner-level submit guard remains the fail-fast backstop for
    strategies that bypass these helpers.
    """

    _polymarket_initial_tick_size: Decimal
    _polymarket_tick_size_changes: tuple[PolymarketTickSizeChange, ...]
    _polymarket_rounding_events: list[PolymarketPriceRoundingEvent]

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._polymarket_initial_tick_size = Decimal("0.01")
        self._polymarket_tick_size_changes = ()
        self._polymarket_rounding_events = []

    def set_polymarket_tick_timeline(
        self,
        *,
        initial_tick_size: Decimal | str,
        changes: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        """Install the effective tick-size timeline supplied by the runner."""

        initial = Decimal(str(initial_tick_size))
        if initial <= 0:
            raise ValueError(f"initial_tick_size must be positive, got {initial}")
        parsed = tuple(
            sorted(
                (_coerce_tick_size_change(item) for item in changes),
                key=lambda item: item.effective_from_ts_init,
            ),
        )
        for change in parsed:
            if change.new_tick_size <= 0:
                raise ValueError(f"new_tick_size must be positive, got {change.new_tick_size}")
        self._polymarket_initial_tick_size = initial
        self._polymarket_tick_size_changes = parsed
        self._polymarket_rounding_events.clear()

    def current_effective_tick_size(self, ts_ns: int | None = None) -> Decimal:
        """Return the effective Polymarket tick at ``ts_ns`` or strategy time."""

        if ts_ns is None:
            ts_ns = int(self.clock.timestamp_ns())
        return resolve_effective_tick_size(
            initial_tick_size=self._polymarket_initial_tick_size,
            changes=self._polymarket_tick_size_changes,
            ts_ns=ts_ns,
        )

    def round_price_to_current_tick(
        self,
        price: Decimal | str | float | int | Price,
        *,
        side: OrderSide | str,
        intent: PolymarketRoundIntent = "passive",
        ts_ns: int | None = None,
    ) -> Decimal:
        """Project ``price`` onto the current effective tick grid.

        ``passive`` keeps the order from crossing more aggressively than the
        raw strategy intent: BUY rounds down, SELL rounds up. ``aggressive`` is
        the opposite. ``nearest`` is symmetric research convenience. ``strict``
        rejects off-grid prices.
        """

        if ts_ns is None:
            ts_ns = int(self.clock.timestamp_ns())
        projection = project_price_to_effective_tick(
            price,
            side=side,
            intent=intent,
            initial_tick_size=self._polymarket_initial_tick_size,
            changes=self._polymarket_tick_size_changes,
            ts_ns=ts_ns,
        )
        return projection.rounded_price

    def make_polymarket_price(
        self,
        price: Decimal | str | float | int | Price,
        *,
        side: OrderSide | str,
        intent: PolymarketRoundIntent = "passive",
        ts_ns: int | None = None,
        instrument_id: Any | None = None,
    ) -> Price:
        """Round by current effective tick, then build a Nautilus ``Price``."""

        resolved_instrument_id = instrument_id or getattr(self.config, "instrument_id", None)
        if resolved_instrument_id is None:
            raise RuntimeError(
                "make_polymarket_price requires instrument_id or config.instrument_id",
            )
        instrument = self.cache.instrument(resolved_instrument_id)
        if instrument is None:
            raise RuntimeError(f"instrument not available in cache: {resolved_instrument_id}")
        if ts_ns is None:
            ts_ns = int(self.clock.timestamp_ns())
        projection = project_price_to_effective_tick(
            price,
            side=side,
            intent=intent,
            initial_tick_size=self._polymarket_initial_tick_size,
            changes=self._polymarket_tick_size_changes,
            ts_ns=ts_ns,
        )
        if projection.changed:
            self._polymarket_rounding_events.append(
                PolymarketPriceRoundingEvent(
                    ts_ns=projection.ts_ns,
                    original_price=projection.original_price,
                    rounded_price=projection.rounded_price,
                    tick_size=projection.tick_size,
                    side=projection.side,
                    intent=projection.intent,
                    direction=projection.direction,
                ),
            )
        return instrument.make_price(projection.rounded_price)

    @property
    def polymarket_price_rounding_events(self) -> tuple[PolymarketPriceRoundingEvent, ...]:
        """Return recorded strategy-level price rounding events."""

        return tuple(self._polymarket_rounding_events)


def _coerce_tick_size_change(value: Any) -> PolymarketTickSizeChange:
    if isinstance(value, PolymarketTickSizeChange):
        return value
    effective_from = getattr(value, "effective_from_ts_init", None)
    new_tick = getattr(value, "new_tick_size", None)
    old_tick = getattr(value, "old_tick_size", None)
    sequence = getattr(value, "sequence", None)
    if isinstance(value, dict):
        effective_from = value.get("effective_from_ts_init", effective_from)
        new_tick = value.get("new_tick_size", new_tick)
        old_tick = value.get("old_tick_size", old_tick)
        sequence = value.get("sequence", sequence)
    if effective_from is None or new_tick is None:
        raise ValueError("tick-size change requires effective_from_ts_init and new_tick_size")
    return PolymarketTickSizeChange(
        effective_from_ts_init=int(effective_from),
        new_tick_size=Decimal(str(new_tick)),
        old_tick_size=Decimal(str(old_tick)) if old_tick is not None else None,
        sequence=int(sequence) if sequence is not None else None,
    )

