"""Native-free tests for Polymarket effective tick price projection."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from polymarket._core.price_rounding import project_price_to_effective_tick
from polymarket._core.price_rounding import round_to_tick


@dataclass(frozen=True)
class Change:
    effective_from_ts_init: int
    new_tick_size: Decimal


def project(price: str, *, side: str, intent: str, ts_ns: int = 100):
    return project_price_to_effective_tick(
        Decimal(price),
        side=side,
        intent=intent,
        initial_tick_size=Decimal("0.01"),
        changes=(Change(effective_from_ts_init=200, new_tick_size=Decimal("0.001")),),
        ts_ns=ts_ns,
    )


def test_round_to_tick_rejects_invalid_price_bounds_without_nautilus_runtime() -> None:
    with pytest.raises(ValueError, match=r"Polymarket price must be in \[0, 1\]"):
        round_to_tick(Decimal("1.001"), tick_size=Decimal("0.01"), direction="nearest")


def test_project_price_uses_initial_effective_tick_direction_rules() -> None:
    assert project("0.501", side="BUY", intent="passive").rounded_price == Decimal("0.50")
    assert project("0.501", side="SELL", intent="passive").rounded_price == Decimal("0.51")
    assert project("0.501", side="BUY", intent="aggressive").rounded_price == Decimal("0.51")
    assert project("0.501", side="SELL", intent="aggressive").rounded_price == Decimal("0.50")
    assert project("0.505", side="BUY", intent="nearest").rounded_price == Decimal("0.51")


def test_project_price_switches_to_fine_tick_only_after_tick_change_boundary() -> None:
    with pytest.raises(ValueError, match=r"violates effective tick size.*0\.01"):
        project("0.501", side="BUY", intent="strict", ts_ns=199)

    fine_projection = project("0.501", side="BUY", intent="strict", ts_ns=200)

    assert fine_projection.rounded_price == Decimal("0.501")
    assert fine_projection.tick_size == Decimal("0.001")
    assert fine_projection.changed is False


def test_projection_carries_audit_metadata_without_nautilus_runtime() -> None:
    projection = project("0.6014", side="BUY", intent="aggressive", ts_ns=201)

    assert projection.ts_ns == 201
    assert projection.original_price == Decimal("0.6014")
    assert projection.rounded_price == Decimal("0.602")
    assert projection.tick_size == Decimal("0.001")
    assert projection.side == "BUY"
    assert projection.intent == "aggressive"
    assert projection.direction == "up"
    assert projection.changed is True
