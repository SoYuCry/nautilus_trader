"""Tests for the Polymarket -> Nautilus native data bridge.

The accepted bridge is:

    Polymarket source adapter -> Nautilus OrderBookDeltas/TradeTick

and execution simulation must be delegated to NautilusTrader's BacktestEngine.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest


pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")

from nautilus_trader.model.data import OrderBookDeltas  # noqa: E402
from nautilus_trader.model.data import TradeTick  # noqa: E402

from polymarket._core.models import DatasetMetadataV1  # noqa: E402
from polymarket._core.models import L2ReplayStepV1  # noqa: E402
from polymarket._core.models import L2UpdateV1  # noqa: E402
from polymarket._core.models import LevelV1  # noqa: E402
from polymarket._core.models import PolymarketL2DatasetV1  # noqa: E402
from polymarket._core.nautilus_native import convert_dataset_to_nautilus  # noqa: E402
from polymarket._core.nautilus_native import load_binary_option_from_config  # noqa: E402


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def ts(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def step(sequence: int, updates: list[L2UpdateV1]) -> L2ReplayStepV1:
    return L2ReplayStepV1(
        sequence=sequence,
        timestamp_received=ts(sequence),
        timestamp=ts(sequence),
        updates=tuple(updates),
    )


def dataset(steps: list[L2ReplayStepV1]) -> PolymarketL2DatasetV1:
    return PolymarketL2DatasetV1(
        metadata=DatasetMetadataV1(
            dataset_id="synthetic-nautilus-native",
            adapter_name="synthetic",
            adapter_version="v1",
            source_type="test",
        ),
        steps=tuple(steps),
    )


def book(asset_id: str = "yes") -> L2UpdateV1:
    return L2UpdateV1(
        event_type="book",
        market="condition",
        asset_id=asset_id,
        bids=(LevelV1(Decimal("0.40"), Decimal("100")),),
        asks=(LevelV1(Decimal("0.60"), Decimal("100")),),
    )


def test_bridge_emits_nautilus_order_book_deltas_and_trade_ticks() -> None:
    data = dataset(
        [
            step(1, [book()]),
            step(
                2,
                [
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.41"),
                        size=Decimal("50"),
                    ),
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="SELL",
                        price=Decimal("0.60"),
                        size=Decimal("90"),
                    ),
                ],
            ),
            step(
                3,
                [
                    L2UpdateV1(
                        event_type="trade",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.60"),
                        size=Decimal("10"),
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert converted.instrument.id == instrument.id
    assert [type(item) for item in converted.data] == [
        OrderBookDeltas,
        OrderBookDeltas,
        TradeTick,
    ]
    assert all(item.instrument_id == instrument.id for item in converted.data)
    trade_tick = converted.data[2]
    assert isinstance(trade_tick, TradeTick)
    assert trade_tick.ts_init == trade_tick.ts_event


def test_bridge_refuses_to_silently_replay_dynamic_tick_size() -> None:
    data = dataset(
        [
            step(1, [book()]),
            step(
                2,
                [
                    L2UpdateV1(
                        event_type="tick_size_change",
                        market="condition",
                        asset_id="yes",
                        old_tick_size=Decimal("0.01"),
                        new_tick_size=Decimal("0.001"),
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    with pytest.raises(NotImplementedError, match="tick_size_change"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_can_explicitly_skip_tick_size_change_for_smoke_runs() -> None:
    data = dataset(
        [
            step(1, [book()]),
            step(
                2,
                [
                    L2UpdateV1(
                        event_type="tick_size_change",
                        market="condition",
                        asset_id="yes",
                        old_tick_size=Decimal("0.01"),
                        new_tick_size=Decimal("0.001"),
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(
        data,
        instrument=instrument,
        selected_asset_id="yes",
        fail_on_tick_size_change=False,
    )

    assert converted.tick_size_changes == (("0.01", "0.001"),)
    assert converted.skipped_updates == ("tick_size_change 0.01->0.001",)
    assert [type(item) for item in converted.data] == [OrderBookDeltas]


def test_bridge_keeps_nautilus_init_timestamps_monotonic_when_receive_times_tie() -> None:
    data = dataset(
        [
            L2ReplayStepV1(
                sequence=1,
                timestamp_received=BASE,
                timestamp=BASE,
                updates=(book(),),
            ),
            L2ReplayStepV1(
                sequence=2,
                timestamp_received=BASE,
                timestamp=BASE,
                updates=(
                    L2UpdateV1(
                        event_type="trade",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.60"),
                        size=Decimal("10"),
                    ),
                ),
            ),
            L2ReplayStepV1(
                sequence=3,
                timestamp_received=BASE,
                timestamp=BASE,
                updates=(
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.41"),
                        size=Decimal("50"),
                    ),
                ),
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert [type(item) for item in converted.data] == [
        OrderBookDeltas,
        TradeTick,
        OrderBookDeltas,
    ]
    assert [item.ts_init for item in converted.data] == sorted(item.ts_init for item in converted.data)
    assert len({item.ts_init for item in converted.data}) == len(converted.data)


@pytest.mark.parametrize(
    ("price", "size", "missing"),
    [
        (None, Decimal("50"), "price"),
        (Decimal("0.41"), None, "size"),
        (None, None, "price, size"),
    ],
)
def test_bridge_rejects_malformed_price_change_without_silent_skip(
    price: Decimal | None,
    size: Decimal | None,
    missing: str,
) -> None:
    data = dataset(
        [
            step(1, [book()]),
            step(
                2,
                [
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=price,
                        size=size,
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    with pytest.raises(ValueError, match=rf"missing required field\(s\) {missing}.*sequence=2.*asset_id='yes'"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_rejects_selected_asset_that_differs_from_instrument_token() -> None:
    data = dataset([step(1, [book(asset_id="yes")])])
    instrument = load_binary_option_from_config(
        {"condition_id": "condition", "token_id": "no"},
        dataset=data,
        selected_asset_id="yes",
    )

    with pytest.raises(
        ValueError,
        match="selected_asset_id does not match Nautilus instrument token_id.*'yes'.*'no'",
    ):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_rejects_instrument_token_without_matching_dataset_updates() -> None:
    data = dataset([step(1, [book(asset_id="yes")])])
    instrument = load_binary_option_from_config(
        {"condition_id": "condition", "token_id": "no"},
        dataset=data,
    )

    with pytest.raises(
        ValueError,
        match="dataset has no updates for selected Polymarket instrument.*condition.*no",
    ):
        convert_dataset_to_nautilus(data, instrument=instrument)


def test_bridge_rejects_matching_token_under_wrong_condition() -> None:
    data = dataset([step(1, [book(asset_id="yes")])])
    instrument = load_binary_option_from_config(
        {"condition_id": "other_condition", "token_id": "yes"},
        dataset=data,
    )

    with pytest.raises(
        ValueError,
        match="dataset has no updates for selected Polymarket instrument.*other_condition.*yes",
    ):
        convert_dataset_to_nautilus(data, instrument=instrument)

