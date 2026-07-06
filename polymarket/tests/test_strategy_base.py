"""Tests for Polymarket strategy-layer tick helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest


pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")

from nautilus_trader.config import StrategyConfig  # noqa: E402
from nautilus_trader.model.enums import OrderSide  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402

from polymarket.strategy import PolymarketStrategyBase  # noqa: E402


class DummyPolymarketConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId


class DummyPolymarketStrategy(PolymarketStrategyBase):
    def __init__(self) -> None:
        super().__init__(
            DummyPolymarketConfig(
                instrument_id=InstrumentId.from_str("condition-yes.POLYMARKET"),
            ),
        )


def test_strategy_base_rounds_by_initial_effective_tick() -> None:
    strategy = DummyPolymarketStrategy()

    assert strategy.current_effective_tick_size(ts_ns=100) == Decimal("0.01")
    assert (
        strategy.round_price_to_current_tick(
            Decimal("0.501"),
            side=OrderSide.BUY,
            intent="passive",
            ts_ns=100,
        )
        == Decimal("0.50")
    )
    assert (
        strategy.round_price_to_current_tick(
            Decimal("0.501"),
            side=OrderSide.SELL,
            intent="passive",
            ts_ns=100,
        )
        == Decimal("0.51")
    )
    assert (
        strategy.round_price_to_current_tick(
            Decimal("0.501"),
            side=OrderSide.BUY,
            intent="aggressive",
            ts_ns=100,
        )
        == Decimal("0.51")
    )
    assert (
        strategy.round_price_to_current_tick(
            Decimal("0.501"),
            side=OrderSide.SELL,
            intent="aggressive",
            ts_ns=100,
        )
        == Decimal("0.50")
    )


def test_strategy_base_tick_change_allows_fine_polymarket_tail_prices() -> None:
    strategy = DummyPolymarketStrategy()
    strategy.set_polymarket_tick_timeline(
        initial_tick_size=Decimal("0.01"),
        changes=[
            {
                "sequence": 2,
                "effective_from_ts_init": 200,
                "old_tick_size": "0.01",
                "new_tick_size": "0.001",
            },
        ],
    )

    assert strategy.current_effective_tick_size(ts_ns=199) == Decimal("0.01")
    assert strategy.current_effective_tick_size(ts_ns=200) == Decimal("0.001")

    with pytest.raises(ValueError, match=r"violates effective tick size"):
        strategy.round_price_to_current_tick(
            Decimal("0.501"),
            side="BUY",
            intent="strict",
            ts_ns=199,
        )
    assert (
        strategy.round_price_to_current_tick(
            Decimal("0.501"),
            side="BUY",
            intent="strict",
            ts_ns=200,
        )
        == Decimal("0.501")
    )


def test_strategy_base_round_helper_is_pure_until_price_construction() -> None:
    strategy = DummyPolymarketStrategy()

    rounded = strategy.round_price_to_current_tick(
        Decimal("0.505"),
        side="BUY",
        intent="nearest",
        ts_ns=123,
    )

    assert rounded == Decimal("0.51")
    assert strategy.polymarket_price_rounding_events == ()
