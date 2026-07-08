"""Pure tests for Polymarket effective tick-size timeline helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket._core.tick_size import resolve_effective_tick_size


@dataclass(frozen=True)
class Change:
    effective_from_ts_init: int
    new_tick_size: Decimal


def test_resolve_effective_tick_size_uses_same_boundary_rule_for_all_callers() -> None:
    changes = (
        Change(effective_from_ts_init=200, new_tick_size=Decimal("0.001")),
    )

    assert (
        resolve_effective_tick_size(
            initial_tick_size=Decimal("0.01"),
            changes=changes,
            ts_ns=199,
        )
        == Decimal("0.01")
    )
    assert (
        resolve_effective_tick_size(
            initial_tick_size=Decimal("0.01"),
            changes=changes,
            ts_ns=200,
        )
        == Decimal("0.001")
    )
