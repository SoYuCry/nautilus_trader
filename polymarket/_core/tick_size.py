"""Polymarket effective tick-size timeline helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class EffectiveTickSizeChangeLike(Protocol):
    """Minimal shape required to resolve an effective tick-size timeline."""

    effective_from_ts_init: int
    new_tick_size: Decimal


def resolve_effective_tick_size(
    *,
    initial_tick_size: Decimal,
    changes: tuple[EffectiveTickSizeChangeLike, ...],
    ts_ns: int,
) -> Decimal:
    """Return the effective Polymarket tick at ``ts_ns``.

    The boundary rule is intentionally shared by strategy helpers and the
    runner submit guard so they cannot drift: a change is active when
    ``ts_ns >= effective_from_ts_init``.
    """

    tick = initial_tick_size
    for change in changes:
        if ts_ns >= change.effective_from_ts_init:
            tick = change.new_tick_size
        else:
            break
    return tick
