"""Pure Polymarket price projection helpers.

This module has no Nautilus dependency.  Strategy code wraps these helpers to
build native ``Price`` objects, while tests can exercise the dynamic tick math
even in a checkout without the compiled Nautilus runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import ROUND_HALF_UP
from typing import Any, Literal

from polymarket._core.tick_size import EffectiveTickSizeChangeLike
from polymarket._core.tick_size import resolve_effective_tick_size


PolymarketRoundIntent = Literal["passive", "aggressive", "nearest", "strict"]
PolymarketRoundDirection = Literal["down", "up", "nearest", "strict"]


@dataclass(frozen=True, slots=True)
class PolymarketPriceProjection:
    """Result of projecting a raw strategy price onto an effective tick grid."""

    ts_ns: int
    original_price: Decimal
    rounded_price: Decimal
    tick_size: Decimal
    side: str
    intent: PolymarketRoundIntent
    direction: PolymarketRoundDirection

    @property
    def changed(self) -> bool:
        """Whether an audit event should be recorded by the strategy wrapper."""

        return self.rounded_price != self.original_price


def project_price_to_effective_tick(
    price: Decimal | str | float | int | Any,
    *,
    side: Any,
    intent: PolymarketRoundIntent,
    initial_tick_size: Decimal,
    changes: tuple[EffectiveTickSizeChangeLike, ...],
    ts_ns: int,
) -> PolymarketPriceProjection:
    """Project ``price`` onto the effective tick active at ``ts_ns``."""

    raw_price = as_decimal_price(price)
    tick = resolve_effective_tick_size(
        initial_tick_size=initial_tick_size,
        changes=changes,
        ts_ns=ts_ns,
    )
    side_label = side_label_from_order_side(side)
    direction = round_direction(side_label, intent)
    rounded = round_to_tick(raw_price, tick_size=tick, direction=direction)
    return PolymarketPriceProjection(
        ts_ns=ts_ns,
        original_price=raw_price,
        rounded_price=rounded,
        tick_size=tick,
        side=side_label,
        intent=intent,
        direction=direction,
    )


def as_decimal_price(value: Decimal | str | float | int | Any) -> Decimal:
    if hasattr(value, "as_decimal"):
        return Decimal(str(value.as_decimal()))
    return Decimal(str(value))


def side_label_from_order_side(side: Any) -> str:
    text = getattr(side, "name", None) or str(side)
    text = text.split(".")[-1].upper()
    if text in {"BUY", "SELL"}:
        return text
    raise ValueError(f"unsupported order side for Polymarket rounding: {side!r}")


def round_direction(side: str, intent: PolymarketRoundIntent) -> PolymarketRoundDirection:
    if intent == "strict":
        return "strict"
    if intent == "nearest":
        return "nearest"
    if intent == "passive":
        return "down" if side == "BUY" else "up"
    if intent == "aggressive":
        return "up" if side == "BUY" else "down"
    raise ValueError(f"unsupported Polymarket rounding intent: {intent!r}")


def round_to_tick(
    price: Decimal,
    *,
    tick_size: Decimal,
    direction: PolymarketRoundDirection,
) -> Decimal:
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    if price < 0 or price > 1:
        raise ValueError(f"Polymarket price must be in [0, 1], got {price}")
    quotient = price / tick_size
    if direction == "strict":
        integral = quotient.to_integral_value()
        if quotient != integral:
            raise ValueError(f"price {price} violates effective tick size {tick_size}")
        return integral * tick_size
    if direction == "down":
        return quotient.to_integral_value(rounding=ROUND_FLOOR) * tick_size
    if direction == "up":
        return quotient.to_integral_value(rounding=ROUND_CEILING) * tick_size
    if direction == "nearest":
        return quotient.to_integral_value(rounding=ROUND_HALF_UP) * tick_size
    raise ValueError(f"unsupported round direction: {direction!r}")
