"""Tests for the Polymarket -> Nautilus native data bridge.

The accepted bridge is:

    Polymarket source adapter -> Nautilus OrderBookDeltas/TradeTick/InstrumentClose

and execution simulation must be delegated to NautilusTrader's BacktestEngine.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest


pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")

from nautilus_trader.model.data import InstrumentClose  # noqa: E402
from nautilus_trader.model.data import OrderBookDeltas  # noqa: E402
from nautilus_trader.model.data import TradeTick  # noqa: E402
from nautilus_trader.model.instruments import BinaryOption  # noqa: E402

from polymarket._core.models import DatasetMetadataV1  # noqa: E402
from polymarket._core.models import L2ReplayStepV1  # noqa: E402
from polymarket._core.models import L2UpdateV1  # noqa: E402
from polymarket._core.models import LevelV1  # noqa: E402
from polymarket._core.models import MarketMetadataV1  # noqa: E402
from polymarket._core.models import PolymarketL2DatasetV1  # noqa: E402
from polymarket._core.nautilus_native import convert_dataset_to_nautilus  # noqa: E402
from polymarket._core.nautilus_native import EffectiveTickSizeChangeV1  # noqa: E402
from polymarket._core.nautilus_native import install_effective_tick_size_order_guard  # noqa: E402
from polymarket._core.nautilus_native import load_binary_option_from_config  # noqa: E402
from polymarket._core.nautilus_native import datetime_to_nanos  # noqa: E402


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


def dataset_with_market_metadata(steps: list[L2ReplayStepV1]) -> PolymarketL2DatasetV1:
    return PolymarketL2DatasetV1(
        metadata=DatasetMetadataV1(
            dataset_id="synthetic-nautilus-native",
            adapter_name="synthetic",
            adapter_version="v1",
            source_type="test",
            market_metadata=(
                MarketMetadataV1(
                    condition_id="condition",
                    token_id="yes",
                    maker_fee=Decimal("0"),
                    taker_fee=Decimal("0.05"),
                    fee_source="clob_market_info.feeSchedule.rate",
                ),
            ),
        ),
        steps=tuple(steps),
    )


def dataset_with_resolution_metadata(
    steps: list[L2ReplayStepV1],
    *,
    resolution_time: datetime | None = None,
    token_payout: Decimal | None = Decimal("1"),
    winner: bool | None = True,
    resolution_status: str | None = "resolved",
) -> PolymarketL2DatasetV1:
    return PolymarketL2DatasetV1(
        metadata=DatasetMetadataV1(
            dataset_id="synthetic-nautilus-native",
            adapter_name="synthetic",
            adapter_version="v1",
            source_type="test",
            market_metadata=(
                MarketMetadataV1(
                    condition_id="condition",
                    token_id="yes",
                    maker_fee=Decimal("0"),
                    taker_fee=Decimal("0.05"),
                    fee_source="clob_market_info.feeSchedule.rate",
                    minimum_tick_size=Decimal("0.01"),
                    resolution_status=resolution_status,
                    resolution_time=resolution_time or ts(10),
                    token_payout=token_payout,
                    winner=winner,
                    resolution_source="test_resolution_metadata",
                ),
            ),
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


def test_binary_option_uses_dataset_fee_metadata_when_config_omits_fee() -> None:
    data = dataset_with_market_metadata([step(1, [book()])])

    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    assert instrument.maker_fee == Decimal("0")
    assert instrument.taker_fee == Decimal("0.05")
    assert instrument.info["fee_source"] == "clob_market_info.feeSchedule.rate"


def test_binary_option_uses_polymarket_static_fine_price_increment_without_future_scan() -> None:
    data = dataset([step(1, [book()])])

    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    assert str(instrument.price_increment) == "0.001"
    assert instrument.price_precision == 3


def test_binary_option_uses_fine_price_increment_when_dataset_has_tick_size_change() -> None:
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

    assert str(instrument.price_increment) == "0.001"
    assert instrument.price_precision == 3


def test_binary_option_rejects_coarse_config_price_increment_when_tick_change_needs_finer_precision() -> None:
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

    with pytest.raises(ValueError, match="price_increment is too coarse.*required=0.001"):
        load_binary_option_from_config(
            {"price_increment": "0.01"},
            dataset=data,
            selected_asset_id="yes",
        )


def test_binary_option_rejects_coarse_dict_path_price_increment_when_tick_change_needs_finer_precision(
    tmp_path: Path,
) -> None:
    fine_data = dataset([step(1, [book()])])
    coarse_instrument = load_binary_option_from_config({}, dataset=fine_data, selected_asset_id="yes")
    instrument_dict = BinaryOption.to_dict(coarse_instrument)
    instrument_dict["price_increment"] = "0.01"
    instrument_dict["price_precision"] = 2
    dict_path = tmp_path / "instrument.json"
    dict_path.write_text(
        json.dumps(instrument_dict),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="price_increment is too coarse.*required=0.001"):
        load_binary_option_from_config(
            {"dict_path": str(dict_path)},
            dataset=fine_data,
            selected_asset_id="yes",
        )


def test_binary_option_dict_path_can_be_resolved_against_config_base_dir(tmp_path: Path) -> None:
    data = dataset([step(1, [book()])])
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")
    config_dir = tmp_path / "experiment"
    config_dir.mkdir()
    dict_dir = config_dir / "instruments"
    dict_dir.mkdir()
    (dict_dir / "instrument.json").write_text(
        json.dumps(BinaryOption.to_dict(instrument)),
        encoding="utf-8",
    )

    loaded = load_binary_option_from_config(
        {"dict_path": "instruments/instrument.json"},
        dataset=data,
        selected_asset_id="yes",
        config_base_dir=config_dir,
    )

    assert loaded.id == instrument.id


def test_bridge_rejects_subpenny_price_before_tick_size_change() -> None:
    data = dataset(
        [
            step(
                1,
                [
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.501"),
                        size=Decimal("10"),
                    ),
                ],
            ),
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

    with pytest.raises(ValueError, match=r"effective tick size.*sequence=1.*price=0\.501.*0\.01"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_accepts_subpenny_price_after_tick_size_change() -> None:
    data = dataset(
        [
            step(
                1,
                [
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.50"),
                        size=Decimal("10"),
                    ),
                ],
            ),
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
            step(
                3,
                [
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.501"),
                        size=Decimal("10"),
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
    )

    assert str(converted.instrument.price_increment) == "0.001"
    assert converted.tick_size_changes == (("0.01", "0.001"),)
    assert converted.effective_tick_size_changes[0].new_tick_size == Decimal("0.001")
    assert converted.skipped_updates == ("tick_size_change 0.01->0.001 (timeline_applied)",)
    assert [type(item) for item in converted.data] == [
        OrderBookDeltas,
        OrderBookDeltas,
    ]


def test_bridge_applies_tick_size_change_before_later_updates_in_same_step() -> None:
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
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.501"),
                        size=Decimal("10"),
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert converted.effective_tick_size_changes[0].sequence == 2
    assert [type(item) for item in converted.data] == [OrderBookDeltas, OrderBookDeltas]


def test_bridge_same_step_tick_change_effective_time_follows_pre_tick_flush() -> None:
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
                        price=Decimal("0.50"),
                        size=Decimal("10"),
                    ),
                    L2UpdateV1(
                        event_type="tick_size_change",
                        market="condition",
                        asset_id="yes",
                        old_tick_size=Decimal("0.01"),
                        new_tick_size=Decimal("0.001"),
                    ),
                    L2UpdateV1(
                        event_type="price_change",
                        market="condition",
                        asset_id="yes",
                        side="BUY",
                        price=Decimal("0.501"),
                        size=Decimal("10"),
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert [type(item) for item in converted.data] == [
        OrderBookDeltas,
        OrderBookDeltas,
        OrderBookDeltas,
    ]
    change = converted.effective_tick_size_changes[0]
    assert converted.data[1].ts_init < change.effective_from_ts_init
    assert converted.data[2].ts_init == change.effective_from_ts_init


def test_bridge_rejects_subpenny_update_before_later_tick_size_change_in_same_step() -> None:
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
                        price=Decimal("0.501"),
                        size=Decimal("10"),
                    ),
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

    with pytest.raises(ValueError, match=r"effective tick size.*sequence=2.*price=0\.501.*0\.01"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_effective_tick_guard_rejects_invalid_modify_order_price_before_tick_change() -> None:
    class DummyClock:
        def __init__(self, ts_now: int) -> None:
            self.ts_now = ts_now

        def timestamp_ns(self) -> int:
            return self.ts_now

    class DummyOrder:
        instrument_id = "condition-yes.POLYMARKET"

    class DummyStrategy:
        def __init__(self) -> None:
            self.clock = DummyClock(datetime_to_nanos(ts(1)))
            self.modified: list[Decimal] = []

        def submit_order(self, order) -> None:
            return None

        def submit_order_list(self, order_list) -> None:
            return None

        def modify_order(self, order, quantity=None, price=None, trigger_price=None, client_id=None, params=None) -> None:
            self.modified.append(price)

    strategy = DummyStrategy()
    guard_info = install_effective_tick_size_order_guard(
        strategy,
        initial_tick_size=Decimal("0.01"),
        changes=(
            EffectiveTickSizeChangeV1(
                sequence=2,
                effective_from_ts_init=datetime_to_nanos(ts(2)),
                old_tick_size=Decimal("0.01"),
                new_tick_size=Decimal("0.001"),
            ),
        ),
    )

    assert "modify_order" in guard_info["guarded_methods"]
    with pytest.raises(ValueError, match=r"price violates effective tick size.*0\.501.*0\.01"):
        strategy.modify_order(DummyOrder(), price=Decimal("0.501"))

    strategy.clock.ts_now = datetime_to_nanos(ts(3))
    strategy.modify_order(DummyOrder(), price=Decimal("0.501"))
    assert strategy.modified == [Decimal("0.501")]


def test_effective_tick_guard_rejects_invalid_submit_order_list_price_before_tick_change() -> None:
    class DummyClock:
        def __init__(self, ts_now: int) -> None:
            self.ts_now = ts_now

        def timestamp_ns(self) -> int:
            return self.ts_now

    class DummyOrder:
        instrument_id = "condition-yes.POLYMARKET"
        price = Decimal("0.501")
        trigger_price = None

    class DummyOrderList:
        orders = (DummyOrder(),)

    class DummyStrategy:
        def __init__(self) -> None:
            self.clock = DummyClock(datetime_to_nanos(ts(1)))
            self.submitted_order_lists: list[DummyOrderList] = []

        def submit_order(self, order) -> None:
            return None

        def submit_order_list(self, order_list) -> None:
            self.submitted_order_lists.append(order_list)

        def modify_order(self, order, quantity=None, price=None, trigger_price=None, client_id=None, params=None) -> None:
            return None

    strategy = DummyStrategy()
    guard_info = install_effective_tick_size_order_guard(
        strategy,
        initial_tick_size=Decimal("0.01"),
        changes=(
            EffectiveTickSizeChangeV1(
                sequence=2,
                effective_from_ts_init=datetime_to_nanos(ts(2)),
                old_tick_size=Decimal("0.01"),
                new_tick_size=Decimal("0.001"),
            ),
        ),
    )

    assert "submit_order_list" in guard_info["guarded_methods"]
    with pytest.raises(ValueError, match=r"price violates effective tick size.*0\.501.*0\.01"):
        strategy.submit_order_list(DummyOrderList())

    strategy.clock.ts_now = datetime_to_nanos(ts(3))
    order_list = DummyOrderList()
    strategy.submit_order_list(order_list)
    assert strategy.submitted_order_lists == [order_list]


def test_bridge_converts_resolution_metadata_to_instrument_close_not_trade_tick() -> None:
    data = dataset_with_resolution_metadata([step(1, [book()])])
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert [type(item) for item in converted.data] == [OrderBookDeltas, InstrumentClose]
    assert not any(isinstance(item, TradeTick) for item in converted.data)
    close = converted.data[-1]
    assert isinstance(close, InstrumentClose)
    assert str(close.close_price) == "1.000"
    assert converted.settlement is not None
    assert converted.settlement.payout == Decimal("1")
    assert converted.settlement.mode == "official"
    assert converted.settlement_mode == "official"


def test_bridge_rejects_settlement_time_before_last_replay_receive_time() -> None:
    data = dataset_with_resolution_metadata([step(2, [book()])], resolution_time=ts(1))
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    with pytest.raises(ValueError, match="settlement time is before the last replay"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_rejects_resolved_metadata_without_payout_or_winner() -> None:
    data = dataset_with_resolution_metadata(
        [step(1, [book()])],
        token_payout=None,
        winner=None,
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    with pytest.raises(ValueError, match="requires token_payout or winner"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_rejects_winner_payout_mismatch() -> None:
    data = dataset_with_resolution_metadata(
        [step(1, [book()])],
        token_payout=Decimal("1"),
        winner=False,
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    with pytest.raises(ValueError, match="winner and payout disagree"):
        convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")


def test_bridge_infers_settlement_from_terminal_market_convergence() -> None:
    data = dataset(
        [
            step(
                1,
                [
                    L2UpdateV1(
                        event_type="book",
                        market="condition",
                        asset_id="yes",
                        bids=(LevelV1(Decimal("0.99"), Decimal("100")),),
                        asks=(LevelV1(Decimal("1.00"), Decimal("100")),),
                    ),
                ],
            ),
        ],
    )
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert converted.settlement is not None
    assert converted.settlement.mode == "inferred"
    assert converted.settlement.payout == Decimal("1")
    assert converted.settlement_mode == "inferred"
    assert [type(item) for item in converted.data] == [OrderBookDeltas, InstrumentClose]


def test_bridge_leaves_settlement_open_when_terminal_market_does_not_converge() -> None:
    data = dataset([step(1, [book()])])
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    assert converted.settlement is None
    assert converted.settlement_mode == "open"
    assert "did not clearly converge" in converted.settlement_reason
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



def test_ts_init_audit_counts_tick_size_change_and_instrument_close_serialization() -> None:
    # book, tick_size_change, and price_change all share one timestamp, and the
    # official resolution time equals that same timestamp: every synthetic +1ns
    # serialization, including the tick timeline and InstrumentClose, must be
    # counted by the audit.
    same_ts = ts(5)
    shared_step_kwargs = {"timestamp_received": same_ts, "timestamp": same_ts}
    steps = [
        L2ReplayStepV1(sequence=1, updates=(book(),), **shared_step_kwargs),
        L2ReplayStepV1(
            sequence=2,
            updates=(
                L2UpdateV1(
                    event_type="tick_size_change",
                    market="condition",
                    asset_id="yes",
                    old_tick_size=Decimal("0.01"),
                    new_tick_size=Decimal("0.001"),
                ),
            ),
            **shared_step_kwargs,
        ),
        L2ReplayStepV1(
            sequence=3,
            updates=(
                L2UpdateV1(
                    event_type="price_change",
                    market="condition",
                    asset_id="yes",
                    side="BUY",
                    price=Decimal("0.411"),
                    size=Decimal(50),
                ),
            ),
            **shared_step_kwargs,
        ),
    ]
    data = dataset_with_resolution_metadata(steps, resolution_time=same_ts)
    instrument = load_binary_option_from_config({}, dataset=data, selected_asset_id="yes")

    converted = convert_dataset_to_nautilus(data, instrument=instrument, selected_asset_id="yes")

    audit = converted.ts_init_audit
    closes = [item for item in converted.data if isinstance(item, InstrumentClose)]
    assert len(closes) == 1
    # Serialized: tick timeline effective-from, the second book event, and the
    # equal-timestamp InstrumentClose (first event needs no adjustment).
    assert audit["adjusted_event_count"] >= 3
    assert audit["max_synthetic_offset_ns"] >= 1
    assert audit["ts_init_policy"] == "synthetic_monotonic_receive_time"
    # The close itself was serialized after the last replay event.
    last_replay_ts_init = max(
        item.ts_init for item in converted.data if not isinstance(item, InstrumentClose)
    )
    assert closes[0].ts_init > last_replay_ts_init
