from __future__ import annotations

# ruff: noqa: E402, I001

import hashlib
import importlib.util
import math
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket._core.models import DatasetMetadataV1
from polymarket._core.models import L2ReplayStepV1
from polymarket._core.models import L2UpdateV1
from polymarket._core.models import LevelV1
from polymarket._core.models import PolymarketL2DatasetV1
from polymarket.replay_contract import replay_timestamp


RESEARCH_DIR = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-14-pmxt-weather-factor-wave0"
)
FACTOR_PROTOCOL = RESEARCH_DIR / "factor_protocol.py"

YES = "E1-YES"
NO = "E1-NO"
EVENT = "E1"
FLOW_FACTOR_NAMES = {
    "ofi",
    "order_flow_imbalance",
    "trade_pressure",
    "cancel_pressure",
    "message_order_sensitive_flow",
}
BOOK_DERIVED_RANKING_FACTORS = (
    "depth_imbalance_1",
    "depth_imbalance_3",
    "depth_imbalance_5",
    "microprice_minus_mid",
    "spread",
    "top_level_depth",
    "depth_slope",
    "depth_concentration",
    "bid_ask_liquidity_asymmetry",
    "distance_to_zero_one",
)


@pytest.fixture
def factor_protocol() -> Any:
    assert FACTOR_PROTOCOL.exists()
    spec = importlib.util.spec_from_file_location("pmxt_weather_factor_wave0_protocol", FACTOR_PROTOCOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_factor_panel_consumes_dataset_steps_in_canonical_order_without_resorting(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(
                1,
                "2026-07-14T00:00:02Z",
                source_ts="2026-07-14T00:00:00Z",
                bids=[("0.45", "10")],
                asks=[("0.55", "10")],
            ),
            _book_step(
                2,
                "2026-07-14T00:00:03Z",
                source_ts="2026-07-14T00:00:00Z",
                bids=[("0.46", "10")],
                asks=[("0.56", "10")],
            ),
            _book_step(
                3,
                "2026-07-14T00:00:03Z",
                source_ts=None,
                bids=[("0.47", "10")],
                asks=[("0.57", "10")],
            ),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert list(panel["sequence"]) == [1, 2, 3]
    assert list(panel["replay_timestamp"]) == [pd.Timestamp(replay_timestamp(step)) for step in dataset.steps]


@pytest.mark.parametrize("include_labels", [False, True], ids=("without-labels", "with-labels"))
def test_build_factor_panel_rejects_non_monotonic_canonical_replay_clock(
    factor_protocol: Any,
    include_labels: bool,
) -> None:
    dataset = _non_monotonic_dataset()

    with pytest.raises(ValueError, match=r"(?i)(replay|order|clock)"):
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=include_labels)


def test_run_factor_protocol_rejects_non_monotonic_canonical_replay_clock(factor_protocol: Any) -> None:
    with pytest.raises(ValueError, match=r"(?i)(replay|order|clock)"):
        factor_protocol.run_factor_protocol(_non_monotonic_dataset(), horizons_seconds=(30, 120, 600))


def test_registered_pure_l2_factor_math_matches_canonical_definitions(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(
                1,
                "2026-07-14T00:00:00Z",
                bids=[
                    ("0.49", "5"),
                    ("0.48", "10"),
                    ("0.47", "20"),
                    ("0.46", "30"),
                    ("0.45", "40"),
                ],
                asks=[
                    ("0.51", "8"),
                    ("0.52", "12"),
                    ("0.53", "16"),
                    ("0.54", "24"),
                    ("0.55", "32"),
                ],
            ),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)
    row = panel.iloc[0]

    assert bool(row["ranking_observation"]) is True
    assert row["bid1"] == pytest.approx(0.49)
    assert row["ask1"] == pytest.approx(0.51)
    assert row["mid"] == pytest.approx(0.50)
    assert row["spread"] == pytest.approx(0.02)
    assert row["depth_imbalance_1"] == pytest.approx((5 - 8) / (5 + 8))
    assert row["depth_imbalance_3"] == pytest.approx((35 - 36) / (35 + 36))
    assert row["depth_imbalance_5"] == pytest.approx((105 - 92) / (105 + 92))
    assert row["microprice_minus_mid"] == pytest.approx(((0.51 * 5) + (0.49 * 8)) / 13 - 0.50)
    assert row["top_level_depth"] == pytest.approx(13)
    assert row["depth_slope"] == pytest.approx(243 / 644)
    assert row["depth_concentration"] == pytest.approx(65 / 966)
    assert row["bid_ask_liquidity_asymmetry"] == pytest.approx(-19 / 65)
    assert row["distance_to_zero_one"] == pytest.approx(0.50)
    assert row["tick_size_regime"] == pytest.approx(0.01)
    assert pd.api.types.is_bool_dtype(panel["actual_mutation"])
    assert pd.api.types.is_bool_dtype(panel["ranking_observation"])


def test_price_change_insert_change_and_delete_rebuild_local_l2_bbo_while_ignoring_row_bbo(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(
                1,
                "2026-07-14T00:00:00Z",
                bids=[("0.48", "10"), ("0.47", "20")],
                asks=[("0.52", "10"), ("0.53", "20")],
                best_bid="0.01",
                best_ask="0.99",
            ),
            _price_step(2, "2026-07-14T00:00:01Z", "BUY", "0.49", "5"),
            _price_step(3, "2026-07-14T00:00:02Z", "SELL", "0.51", "7"),
            _price_step(4, "2026-07-14T00:00:03Z", "BUY", "0.49", "0"),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert list(panel["bid1"]) == pytest.approx([0.48, 0.49, 0.49, 0.48])
    assert list(panel["ask1"]) == pytest.approx([0.52, 0.52, 0.51, 0.51])
    assert panel.loc[3, "mid"] == pytest.approx(0.495)
    assert panel.loc[3, "spread"] == pytest.approx(0.03)


def test_atomic_multi_update_revert_is_not_an_actual_book_mutation(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _multi_update_step(
                2,
                "2026-07-14T00:00:10Z",
                (
                    _price_update("BUY", "0.40", "11"),
                    _price_update("BUY", "0.40", "10"),
                ),
            ),
            _tick_step(3, "2026-07-14T00:00:20Z", old="0.01", new="0.001"),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert list(panel["bid1"]) == pytest.approx([0.40, 0.40, 0.40])
    assert list(panel["ask1"]) == pytest.approx([0.60, 0.60, 0.60])
    assert list(panel["top_level_depth"]) == pytest.approx([20.0, 20.0, 20.0])
    assert list(panel["actual_mutation"]) == [True, False, False]
    assert list(panel["ranking_observation"]) == [True, False, False]
    assert list(panel["book_staleness_seconds"]) == pytest.approx([0.0, 10.0, 20.0])
    assert list(panel["book_update_intensity"]) == pytest.approx([1 / 30, 1 / 30, 1 / 30])


def test_panel_identity_keeps_dataset_event_market_and_token_boundaries(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", market="condition-A", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:01Z", market="condition-B", bids=[("0.20", "10")], asks=[("0.40", "10")]),
        ],
        dataset_id="dataset-wave0-boundaries",
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert list(panel["event_id"]) == ["dataset-wave0-boundaries", "dataset-wave0-boundaries"]
    assert list(panel["market"]) == ["condition-A", "condition-B"]
    assert list(panel["token_id"]) == [YES, YES]


def test_labels_do_not_cross_markets_when_the_same_asset_id_is_reused(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", market="condition-A", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:01Z", market="condition-B", bids=[("0.20", "10")], asks=[("0.40", "10")]),
            _book_step(3, "2026-07-14T00:00:30Z", market="condition-B", bids=[("0.25", "10")], asks=[("0.45", "10")]),
            _book_step(4, "2026-07-14T00:00:31Z", market="condition-A", bids=[("0.50", "10")], asks=[("0.70", "10")]),
        ],
        dataset_id="dataset-wave0-boundaries",
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]

    assert anchor["label_matched_timestamp_30s"] == pd.Timestamp("2026-07-14T00:00:31Z")
    assert anchor["future_mid_30s"] == pytest.approx(0.60)
    assert anchor["future_mid_return_30s"] == pytest.approx(0.10)
    assert anchor["next_nonzero_mid_move"] == pytest.approx(0.10)
    assert anchor["next_nonzero_mid_move_matched_sequence"] == 4


def test_tick_size_regime_is_explicit_token_state_and_visible_price_gaps_do_not_change_it(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.400", "10")], asks=[("0.425", "10")]),
            _tick_step(2, "2026-07-14T00:00:01Z", old="0.01", new="0.001"),
            _book_step(3, "2026-07-14T00:00:02Z", bids=[("0.401", "10")], asks=[("0.429", "10")]),
            _book_step(
                4,
                "2026-07-14T00:00:03Z",
                token_id=NO,
                bids=[("0.300", "10")],
                asks=[("0.315", "10")],
            ),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert panel.loc[panel["sequence"] == 1, "tick_size_regime"].iloc[0] == pytest.approx(0.01)
    assert panel.loc[panel["sequence"] == 2, "tick_size_regime"].iloc[0] == 0.001
    assert panel.loc[panel["sequence"] == 3, "tick_size_regime"].iloc[0] == 0.001
    assert panel.loc[panel["sequence"] == 4, "tick_size_regime"].iloc[0] == pytest.approx(0.01)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (None, "0.001"),
        ("0.001", "0.001"),
        ("0.01", "0.005"),
    ],
    ids=("missing-old", "old-mismatch", "unsupported-new"),
)
def test_tick_size_change_fails_fast_when_contract_is_not_exact(
    factor_protocol: Any,
    old: str | None,
    new: str,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _tick_step(2, "2026-07-14T00:00:01Z", old=old, new=new),
        ],
    )

    with pytest.raises(ValueError, match=r"(?i)tick"):
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)


@pytest.mark.parametrize(
    ("old", "new", "field_context"),
    [
        ("0.0100000000005", "0.001", "old_tick_size"),
        ("0.01", "0.0010000000005", "new_tick_size"),
    ],
    ids=("near-miss-old", "near-miss-new"),
)
def test_tick_size_change_requires_exact_decimal_contract(
    factor_protocol: Any,
    old: str,
    new: str,
    field_context: str,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _tick_step(2, "2026-07-14T00:00:01Z", old=old, new=new),
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(
        exc_info.value,
        "sequence",
        "2",
        "event_type",
        "tick_size_change",
        "market",
        EVENT,
        "asset_id",
        YES,
        field_context,
    )


def test_duplicate_narrow_tick_size_change_warns_and_is_ignored(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00.000Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _tick_step(2, "2026-07-14T00:00:01.000Z", old="0.01", new="0.001"),
            _tick_step(3, "2026-07-14T00:00:01.001Z", old="0.01", new="0.001"),
        ],
    )

    with pytest.warns(RuntimeWarning, match="duplicate tick_size_change"):
        panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert panel.loc[panel["sequence"] == 2, "tick_size_regime"].iloc[0] == pytest.approx(0.001)
    assert panel.loc[panel["sequence"] == 3, "tick_size_regime"].iloc[0] == pytest.approx(0.001)


def test_long_gap_duplicate_narrow_tick_size_change_remains_strict(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00.000Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _tick_step(2, "2026-07-14T00:00:01.000Z", old="0.01", new="0.001"),
            _tick_step(3, "2026-07-14T00:05:26.489Z", old="0.01", new="0.001"),
        ],
    )

    with pytest.raises(ValueError, match="old_tick_size"):
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)


def test_empty_canonical_step_fails_fast_with_sequence_context(factor_protocol: Any) -> None:
    clock = _dt("2026-07-14T00:00:00Z")
    dataset = _dataset(
        [
            L2ReplayStepV1(
                sequence=1,
                timestamp_received=clock,
                timestamp=clock,
                updates=(),
                source_row_index=1,
            ),
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(exc_info.value, "sequence", "1", "updates")


def test_canonical_dataset_rejects_duck_typed_replay_step_with_index_and_type_context(
    factor_protocol: Any,
) -> None:
    valid_step = _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")])
    duck_step = SimpleNamespace(
        sequence=valid_step.sequence,
        timestamp_received=valid_step.timestamp_received,
        timestamp=valid_step.timestamp,
        updates=valid_step.updates,
        source_row_index=valid_step.source_row_index,
    )
    dataset = _dataset([valid_step])
    object.__setattr__(dataset, "steps", (duck_step,))

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(exc_info.value, "steps[0]", "type", "L2ReplayStepV1")


def test_canonical_step_rejects_duck_typed_update_with_sequence_update_index_and_type_context(
    factor_protocol: Any,
) -> None:
    valid_update = _price_update("BUY", "0.40", "10")
    duck_update = SimpleNamespace(
        event_type=valid_update.event_type,
        market=valid_update.market,
        asset_id=valid_update.asset_id,
        side=valid_update.side,
        price=valid_update.price,
        size=valid_update.size,
        bids=valid_update.bids,
        asks=valid_update.asks,
        best_bid=valid_update.best_bid,
        best_ask=valid_update.best_ask,
        old_tick_size=valid_update.old_tick_size,
        new_tick_size=valid_update.new_tick_size,
    )
    step = _step(7, "2026-07-14T00:00:00Z", valid_update)
    object.__setattr__(step, "updates", (duck_update,))
    dataset = _dataset([step])

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(exc_info.value, "sequence", "7", "updates[0]", "type", "L2UpdateV1")


@pytest.mark.parametrize(
    ("case", "field_context"),
    [
        ("unsupported-event-type", "event_type"),
        ("price-change-invalid-side", "side"),
        ("price-change-missing-price", "price"),
        ("price-change-nonfinite-price", "price"),
        ("price-change-price-below-zero", "price"),
        ("price-change-price-above-one", "price"),
        ("price-change-missing-size", "size"),
        ("price-change-nonfinite-size", "size"),
        ("price-change-negative-size", "size"),
        ("snapshot-nonfinite-price", "bids"),
        ("snapshot-price-below-zero", "bids"),
        ("snapshot-price-above-one", "asks"),
        ("snapshot-zero-size", "bids"),
        ("snapshot-negative-size", "asks"),
        ("trade-missing-side", "side"),
        ("trade-invalid-side", "side"),
        ("trade-missing-price", "price"),
        ("trade-nonfinite-price", "price"),
        ("trade-price-above-one", "price"),
        ("trade-missing-size", "size"),
        ("trade-nonpositive-size", "size"),
    ],
)
def test_malformed_canonical_update_fails_fast_with_update_context(
    factor_protocol: Any,
    case: str,
    field_context: str,
) -> None:
    update = _malformed_update(case)
    dataset = _dataset([_step(1, "2026-07-14T00:00:00Z", update)])

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(
        exc_info.value,
        "sequence",
        "1",
        "event_type",
        str(update.event_type),
        "market",
        update.market,
        "asset_id",
        update.asset_id,
        field_context,
    )


@pytest.mark.parametrize(
    ("case", "field_context"),
    [
        ("snapshot-level-not-level-v1", "bids"),
        ("snapshot-price-string", "bids[0].price"),
        ("snapshot-size-float", "bids[0].size"),
        ("price-change-price-string", "price"),
        ("price-change-size-float", "size"),
        ("trade-price-float", "price"),
        ("trade-size-string", "size"),
        ("tick-old-string", "old_tick_size"),
        ("tick-new-float", "new_tick_size"),
    ],
)
def test_canonical_numeric_fields_require_runtime_decimal_and_level_types(
    factor_protocol: Any,
    case: str,
    field_context: str,
) -> None:
    update = _runtime_type_violation_update(case)
    dataset = _dataset([_step(1, "2026-07-14T00:00:00Z", update)])

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(
        exc_info.value,
        "sequence",
        "1",
        "event_type",
        str(update.event_type),
        "market",
        EVENT,
        "asset_id",
        YES,
        field_context,
    )


@pytest.mark.parametrize("duplicate_side", ["bids", "asks"], ids=("duplicate-bid", "duplicate-ask"))
def test_snapshot_duplicate_prices_fail_fast_with_full_context(
    factor_protocol: Any,
    duplicate_side: str,
) -> None:
    bids = [("0.40", "10"), ("0.40", "11")] if duplicate_side == "bids" else [("0.40", "10")]
    asks = [("0.60", "10"), ("0.60", "11")] if duplicate_side == "asks" else [("0.60", "10")]
    dataset = _dataset([_book_step(1, "2026-07-14T00:00:00Z", bids=bids, asks=asks)])

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(
        exc_info.value,
        "sequence",
        "1",
        "event_type",
        "book",
        "market",
        EVENT,
        "asset_id",
        YES,
        duplicate_side,
        "duplicate",
        "price",
    )


@pytest.mark.parametrize(
    ("case", "field_context"),
    [
        ("snapshot-negative-subnormal-price", "bids[0].price"),
        ("price-change-above-one-precision-price", "price"),
    ],
    ids=("snapshot-negative-subnormal-price", "price-change-above-one-precision-price"),
)
def test_decimal_price_bounds_fail_closed_before_ranking(
    factor_protocol: Any,
    case: str,
    field_context: str,
) -> None:
    step = (
        _book_step(
            1,
            "2026-07-14T00:00:00Z",
            bids=[("-1E-1000", "10")],
            asks=[("0.60", "10")],
        )
        if case == "snapshot-negative-subnormal-price"
        else _price_step(1, "2026-07-14T00:00:00Z", "BUY", "1.0000000000000000000000001", "10")
    )
    dataset = _dataset([step])

    with pytest.raises(ValueError) as exc_info:
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    _assert_error_context(
        exc_info.value,
        "sequence",
        "1",
        "event_type",
        "book" if case == "snapshot-negative-subnormal-price" else "price_change",
        "market",
        EVENT,
        "asset_id",
        YES,
        field_context,
    )


def test_zero_size_price_change_remains_a_valid_delete(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _price_step(2, "2026-07-14T00:00:01Z", "BUY", "0.40", "0"),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)
    deleted = panel.loc[panel["sequence"] == 2].iloc[0]

    assert bool(deleted["actual_mutation"]) is True
    assert bool(deleted["ranking_observation"]) is False
    assert deleted["book_validity"] == "missing"
    assert _is_missing(deleted["bid1"])


def test_staleness_resets_only_on_actual_book_mutation_and_update_intensity_counts_mutations(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _trade_step(1, "2026-07-14T00:00:00Z"),
            _book_step(2, "2026-07-14T00:00:01Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(3, "2026-07-14T00:00:10Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _price_step(4, "2026-07-14T00:00:20Z", "BUY", "0.40", "10"),
            _price_step(5, "2026-07-14T00:00:31Z", "BUY", "0.41", "5"),
            _tick_step(6, "2026-07-14T00:00:40Z", old="0.01", new="0.001"),
            _price_step(7, "2026-07-14T00:01:01Z", "SELL", "0.60", "0"),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert _is_missing(panel.loc[0, "book_staleness_seconds"])
    assert panel.loc[1, "book_staleness_seconds"] == pytest.approx(0.0)
    assert panel.loc[2, "book_staleness_seconds"] == pytest.approx(9.0)
    assert panel.loc[3, "book_staleness_seconds"] == pytest.approx(19.0)
    assert panel.loc[4, "book_staleness_seconds"] == pytest.approx(0.0)
    assert panel.loc[5, "book_staleness_seconds"] == pytest.approx(9.0)
    assert panel.loc[6, "book_staleness_seconds"] == pytest.approx(0.0)
    assert panel.loc[4, "book_update_intensity"] == pytest.approx(1 / 30)
    assert panel.loc[6, "book_update_intensity"] == pytest.approx(1 / 30)
    assert pd.api.types.is_bool_dtype(panel["actual_mutation"])
    assert pd.api.types.is_bool_dtype(panel["ranking_observation"])
    assert list(panel["actual_mutation"]) == [False, True, False, False, True, False, True]
    assert list(panel["ranking_observation"]) == [False, True, False, False, True, False, False]


def test_update_intensity_is_token_local_and_uses_strict_left_thirty_second_boundary(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:01Z", token_id=NO, bids=[("0.30", "10")], asks=[("0.70", "10")]),
            _price_step(3, "2026-07-14T00:00:30Z", "BUY", "0.41", "10"),
            _price_step(4, "2026-07-14T00:00:31Z", "BUY", "0.42", "10"),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    # At t=30s, the t=0 mutation is excluded by the strict (t-30s, t] window.
    assert panel.loc[panel["sequence"] == 3, "book_update_intensity"].iloc[0] == pytest.approx(1 / 30)
    # At t=31s, both t=30 and t=31 mutations are included for YES only.
    assert panel.loc[panel["sequence"] == 4, "book_update_intensity"].iloc[0] == pytest.approx(2 / 30)
    assert panel.loc[panel["sequence"] == 2, "book_update_intensity"].iloc[0] == pytest.approx(1 / 30)


def test_update_intensity_is_a_bounded_rolling_window_over_long_histories(
    factor_protocol: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = [
        _book_step(
            second + 1,
            f"2026-07-14T00:{second // 60:02d}:{second % 60:02d}Z",
            bids=[("0.40", str(10 + second % 2))],
            asks=[("0.60", str(10 + second % 2))],
        )
        for second in range(181)
    ]
    steps.append(
        _book_step(
            182,
            "2026-07-14T01:00:00Z",
            bids=[("0.40", "12")],
            asks=[("0.60", "12")],
        ),
    )
    comparison_count = [0]
    monkeypatch.setattr(factor_protocol, "pd", _PandasClockProbe(pd, comparison_count))

    panel = factor_protocol.build_factor_panel(_dataset(steps), horizons_seconds=(30,), include_labels=False)

    assert panel.loc[panel["sequence"] == 31, "book_update_intensity"].iloc[0] == pytest.approx(1.0)
    assert panel.loc[panel["sequence"] == 91, "book_update_intensity"].iloc[0] == pytest.approx(1.0)
    assert panel.loc[panel["sequence"] == 181, "book_update_intensity"].iloc[0] == pytest.approx(1.0)
    assert panel.loc[panel["sequence"] == 182, "book_update_intensity"].iloc[0] == pytest.approx(1 / 30)
    assert panel["book_update_intensity"].max() <= 1.0
    assert comparison_count[0] <= 80 * len(steps)


def test_incremental_depth_processing_has_bounded_near_linear_sorted_item_work(
    factor_protocol: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sorted_item_count = _install_sorted_item_probe(factor_protocol, monkeypatch)
    work_by_depth: dict[int, int] = {}

    for depth in (100, 200, 400):
        before = sorted_item_count[0]
        panel = factor_protocol.build_factor_panel(
            _incremental_depth_dataset(depth),
            horizons_seconds=(30,),
            include_labels=False,
        )
        work_by_depth[depth] = sorted_item_count[0] - before
        assert len(panel) == depth + 1
        assert panel.iloc[-1]["bid1"] < panel.iloc[-1]["ask1"]

    assert work_by_depth[200] <= 2.5 * work_by_depth[100], work_by_depth
    assert work_by_depth[400] <= 2.5 * work_by_depth[200], work_by_depth
    for depth, item_work in work_by_depth.items():
        assert item_work <= 32 * (depth + 1), work_by_depth


def test_trade_tick_and_noop_rows_do_not_rescan_the_full_deep_book(
    factor_protocol: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sorted_item_count = _install_sorted_item_probe(factor_protocol, monkeypatch)

    factor_protocol.build_factor_panel(
        _incremental_depth_dataset(400),
        horizons_seconds=(30,),
        include_labels=False,
    )
    mutation_only_work = sorted_item_count[0]

    factor_protocol.build_factor_panel(
        _incremental_depth_dataset(400, include_nonmutation_rows=True),
        horizons_seconds=(30,),
        include_labels=False,
    )
    work_with_nonmutations = sorted_item_count[0] - mutation_only_work

    nonmutation_overhead = work_with_nonmutations - mutation_only_work
    assert nonmutation_overhead <= 3 * 40, {
        "mutation_only": mutation_only_work,
        "with_trade_tick_noop": work_with_nonmutations,
        "nonmutation_overhead": nonmutation_overhead,
    }


def test_invalid_locked_crossed_one_sided_and_empty_books_are_not_ranking_observations(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[], asks=[]),
            _book_step(2, "2026-07-14T00:00:01Z", bids=[("0.50", "10")], asks=[]),
            _book_step(3, "2026-07-14T00:00:02Z", bids=[("0.50", "10")], asks=[("0.50", "10")]),
            _book_step(4, "2026-07-14T00:00:03Z", bids=[("0.51", "10")], asks=[("0.50", "10")]),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)

    assert list(panel["book_validity"]) == ["missing", "missing", "locked", "crossed"]
    assert list(panel["ranking_observation"]) == [False, False, False, False]
    for _, row in panel.iterrows():
        assert _is_missing(row["mid"])
        assert _is_missing(row["spread"])
        for factor in BOOK_DERIVED_RANKING_FACTORS:
            assert _is_missing(row[factor]), factor
        assert not _is_missing(row["tick_size_regime"])
        assert not _is_missing(row["book_update_intensity"])


def test_known_archive_gap_censor_reason_is_exact_hard_break_provenance(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:30Z", bids=[("0.45", "10")], asks=[("0.65", "10")]),
        ],
        source_quality={
            "knownArchiveGaps": [
                {
                    "market": EVENT,
                    "asset_id": YES,
                    "start": "2026-07-14T00:00:10Z",
                    "end": "2026-07-14T00:00:20Z",
                    "provenance": "pmxt_archive_gap",
                },
            ],
        },
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]

    assert anchor["label_censor_reason_30s"] == "hard_break:pmxt_archive_gap"
    assert anchor["next_nonzero_mid_move_censor_reason"] == "hard_break:pmxt_archive_gap"


def test_fixed_horizon_missing_future_mutation_sets_missing_future_mid_censor_reason(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]

    assert anchor["label_censor_reason_30s"] == "missing_future_mid"
    assert _is_missing(anchor["future_mid_30s"])
    assert _is_missing(anchor["future_mid_return_30s"])


def test_fixed_horizon_invalid_matched_book_sets_invalid_future_book_censor_reason(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _price_step(2, "2026-07-14T00:00:30Z", "BUY", "0.70", "10"),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]
    invalid = panel.loc[panel["sequence"] == 2].iloc[0]

    assert bool(invalid["actual_mutation"]) is True
    assert invalid["book_validity"] == "crossed"
    assert anchor["future_book_validity_30s"] == "crossed"
    assert anchor["label_censor_reason_30s"] == "invalid_future_book"
    assert _is_missing(anchor["future_mid_30s"])
    assert _is_missing(anchor["future_mid_return_30s"])


def test_next_nonzero_label_is_censored_by_first_future_invalid_actual_mutation(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _price_step(2, "2026-07-14T00:00:10Z", "BUY", "0.70", "10"),
            _book_step(3, "2026-07-14T00:00:20Z", bids=[("0.45", "10")], asks=[("0.65", "10")]),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]
    invalid = panel.loc[panel["sequence"] == 2].iloc[0]

    assert bool(invalid["actual_mutation"]) is True
    assert bool(invalid["ranking_observation"]) is False
    assert invalid["book_validity"] == "crossed"
    assert _is_missing(anchor["next_nonzero_mid_move"])
    assert _is_missing(anchor["next_nonzero_mid_move_direction"])
    assert _is_missing(anchor["next_nonzero_mid_move_matched_sequence"])
    assert anchor["next_nonzero_mid_move_censor_reason"] == "invalid_future_book"


def test_next_nonzero_label_sets_missing_future_mid_when_no_later_nonzero_move_exists(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:10Z", bids=[("0.40", "11")], asks=[("0.60", "9")]),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]
    same_mid = panel.loc[panel["sequence"] == 2].iloc[0]

    assert bool(same_mid["actual_mutation"]) is True
    assert bool(same_mid["ranking_observation"]) is True
    assert same_mid["mid"] == pytest.approx(anchor["mid"])
    assert _is_missing(anchor["next_nonzero_mid_move"])
    assert _is_missing(anchor["next_nonzero_mid_move_direction"])
    assert _is_missing(anchor["next_nonzero_mid_move_matched_sequence"])
    assert anchor["next_nonzero_mid_move_censor_reason"] == "missing_future_mid"


def test_next_nonzero_label_skips_valid_same_mid_mutations_before_valid_changed_mid(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:10Z", bids=[("0.40", "11")], asks=[("0.60", "9")]),
            _book_step(3, "2026-07-14T00:00:20Z", bids=[("0.45", "10")], asks=[("0.65", "10")]),
        ],
    )

    panel = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=True)
    anchor = panel.loc[panel["sequence"] == 1].iloc[0]
    same_mid = panel.loc[panel["sequence"] == 2].iloc[0]

    assert bool(same_mid["actual_mutation"]) is True
    assert bool(same_mid["ranking_observation"]) is True
    assert same_mid["mid"] == pytest.approx(anchor["mid"])
    assert anchor["next_nonzero_mid_move"] == pytest.approx(0.05)
    assert anchor["next_nonzero_mid_move_direction"] == 1
    assert anchor["next_nonzero_mid_move_matched_sequence"] == 3


@pytest.mark.parametrize(
    "column",
    [
        "future_mid_30s",
        "future_mid_return_30s",
        "next_nonzero_mid_move",
        "next_nonzero_mid_move_direction",
    ],
)
def test_numeric_label_outputs_use_numeric_dtypes(factor_protocol: Any, column: str) -> None:
    panel = factor_protocol.build_factor_panel(
        _typed_label_dataset(),
        horizons_seconds=(30,),
        include_labels=True,
    )

    assert pd.api.types.is_numeric_dtype(panel[column]), (column, panel[column].dtype)
    assert not pd.api.types.is_object_dtype(panel[column]), (column, panel[column].dtype)


def test_label_matched_timestamp_uses_utc_datetime_dtype(factor_protocol: Any) -> None:
    panel = factor_protocol.build_factor_panel(
        _typed_label_dataset(),
        horizons_seconds=(30,),
        include_labels=True,
    )

    assert str(panel["label_matched_timestamp_30s"].dtype) == "datetime64[ns, UTC]"


def test_next_nonzero_matched_sequence_uses_nullable_integer_dtype(factor_protocol: Any) -> None:
    panel = factor_protocol.build_factor_panel(
        _typed_label_dataset(),
        horizons_seconds=(30,),
        include_labels=True,
    )

    assert str(panel["next_nonzero_mid_move_matched_sequence"].dtype) == "Int64"


def test_candidate_metrics_use_ranking_observations_only(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:15Z", bids=[("0.90", "1000")], asks=[("0.80", "1")]),
            _book_step(3, "2026-07-14T00:00:30Z", bids=[("0.41", "11")], asks=[("0.61", "10")]),
            _book_step(4, "2026-07-14T00:01:00Z", bids=[("0.42", "12")], asks=[("0.62", "10")]),
        ],
    )

    result = factor_protocol.run_factor_protocol(dataset, horizons_seconds=(30, 120, 600))
    panel = _result_frame(result, "panel")
    candidate_table = _result_frame(result, "candidate_table")

    assert bool(panel.loc[panel["sequence"] == 2, "ranking_observation"].iloc[0]) is False
    depth_imbalance_rows = candidate_table[candidate_table["factor"] == "depth_imbalance_1"]
    row_counts = dict(zip(depth_imbalance_rows["horizon_seconds"], depth_imbalance_rows["row_count"], strict=True))
    assert row_counts[30] == 2
    assert row_counts[120] == 0
    assert row_counts[600] == 0


def test_trade_only_flow_steps_do_not_change_ranking_anchors_candidates_or_shortlist(
    factor_protocol: Any,
) -> None:
    base = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _price_step(3, "2026-07-14T00:00:30Z", "BUY", "0.41", "11"),
            _price_step(5, "2026-07-14T00:02:00Z", "SELL", "0.59", "12"),
            _price_step(7, "2026-07-14T00:10:00Z", "BUY", "0.42", "13"),
        ],
    )
    with_flow = _dataset(
        [
            base.steps[0],
            _trade_step(2, "2026-07-14T00:00:10Z", size="999"),
            base.steps[1],
            _trade_step(4, "2026-07-14T00:01:00Z", size="777"),
            base.steps[2],
            _trade_step(6, "2026-07-14T00:05:00Z", size="555"),
            base.steps[3],
        ],
    )

    base_result = factor_protocol.run_factor_protocol(base, horizons_seconds=(30, 120, 600))
    flow_result = factor_protocol.run_factor_protocol(with_flow, horizons_seconds=(30, 120, 600))
    base_panel = _result_frame(base_result, "panel")
    flow_panel = _result_frame(flow_result, "panel")
    flow_trade_rows = flow_panel[flow_panel["event_type"] == "trade"]
    flow_ranking_panel = flow_panel[flow_panel["ranking_observation"]].reset_index(drop=True)

    assert len(flow_trade_rows) == 3
    assert list(flow_trade_rows["ranking_observation"]) == [False, False, False]
    pd.testing.assert_frame_equal(
        _stable_frame(base_panel[_factor_and_label_columns(base_panel)]),
        _stable_frame(flow_ranking_panel[_factor_and_label_columns(base_panel)]),
        check_dtype=False,
    )
    _assert_digest_equal(_result_frame(base_result, "candidate_table"), _result_frame(flow_result, "candidate_table"))
    _assert_digest_equal(_result_frame(base_result, "primary_shortlist"), _result_frame(flow_result, "primary_shortlist"))
    assert FLOW_FACTOR_NAMES.isdisjoint(set(factor_protocol.PRIMARY_FACTOR_NAMES))
    assert FLOW_FACTOR_NAMES.isdisjoint(set(_result_frame(flow_result, "candidate_table")["factor"]))


def test_prefix_factor_values_are_unchanged_by_future_invalid_tick_and_flow_steps(
    factor_protocol: Any,
) -> None:
    prefix_steps = (
        _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
        _price_step(2, "2026-07-14T00:00:01Z", "BUY", "0.41", "5"),
    )
    future_poison = (
        _book_step(3, "2026-07-14T00:00:30Z", bids=[("0.90", "1")], asks=[("0.80", "100")]),
        _tick_step(4, "2026-07-14T00:00:31Z", old="0.01", new="0.001"),
        _trade_step(5, "2026-07-14T00:00:32Z", size="999"),
    )

    prefix = factor_protocol.build_factor_panel(_dataset(prefix_steps), horizons_seconds=(30,), include_labels=True)
    appended = factor_protocol.build_factor_panel(
        _dataset(prefix_steps + future_poison),
        horizons_seconds=(30,),
        include_labels=True,
    )
    appended_prefix = appended[appended["sequence"].isin(prefix["sequence"])].reset_index(drop=True)

    pd.testing.assert_frame_equal(
        prefix[_current_factor_columns(prefix)].reset_index(drop=True),
        appended_prefix[_current_factor_columns(prefix)].reset_index(drop=True),
        check_dtype=False,
    )


def test_factor_protocol_is_deterministic_for_canonical_dataset_order(factor_protocol: Any) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _price_step(2, "2026-07-14T00:00:00Z", "BUY", "0.41", "11"),
            _price_step(3, "2026-07-14T00:00:30Z", "SELL", "0.59", "12"),
            _price_step(4, "2026-07-14T00:02:00Z", "BUY", "0.42", "13"),
            _price_step(5, "2026-07-14T00:10:00Z", "SELL", "0.58", "14"),
        ],
    )

    first = factor_protocol.run_factor_protocol(dataset, horizons_seconds=(30, 120, 600))
    second = factor_protocol.run_factor_protocol(dataset, horizons_seconds=(30, 120, 600))

    _assert_digest_equal(_result_frame(first, "panel"), _result_frame(second, "panel"))
    _assert_digest_equal(_result_frame(first, "candidate_table"), _result_frame(second, "candidate_table"))
    assert _result_get(first, "deterministic_digest") == _result_get(second, "deterministic_digest")


def _book_step(
    sequence: int,
    ts: str,
    *,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    market: str = EVENT,
    token_id: str = YES,
    best_bid: str | None = None,
    best_ask: str | None = None,
    source_ts: str | None = "",
) -> L2ReplayStepV1:
    update = L2UpdateV1(
        event_type="book",
        market=market,
        asset_id=token_id,
        bids=tuple(_level(price, size) for price, size in bids),
        asks=tuple(_level(price, size) for price, size in asks),
        best_bid=Decimal(best_bid) if best_bid is not None else None,
        best_ask=Decimal(best_ask) if best_ask is not None else None,
    )
    return _step(sequence, ts, update, source_ts=source_ts)


def _price_step(
    sequence: int,
    ts: str,
    side: str,
    price: str,
    size: str,
    *,
    market: str = EVENT,
    token_id: str = YES,
) -> L2ReplayStepV1:
    return _step(
        sequence,
        ts,
        _price_update(side, price, size, market=market, token_id=token_id),
    )


def _price_update(
    side: str,
    price: str,
    size: str,
    *,
    market: str = EVENT,
    token_id: str = YES,
) -> L2UpdateV1:
    return L2UpdateV1(
        event_type="price_change",
        market=market,
        asset_id=token_id,
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        size=Decimal(size),
        best_bid=Decimal("0.01"),
        best_ask=Decimal("0.99"),
    )


def _trade_step(sequence: int, ts: str, *, size: str = "1", token_id: str = YES) -> L2ReplayStepV1:
    update = L2UpdateV1(
        event_type="trade",
        market=EVENT,
        asset_id=token_id,
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal(size),
    )
    return _step(sequence, ts, update)


def _tick_step(sequence: int, ts: str, *, old: str | None, new: str | None, token_id: str = YES) -> L2ReplayStepV1:
    update = L2UpdateV1(
        event_type="tick_size_change",
        market=EVENT,
        asset_id=token_id,
        old_tick_size=Decimal(old) if old is not None else None,
        new_tick_size=Decimal(new) if new is not None else None,
    )
    return _step(sequence, ts, update)


def _step(
    sequence: int,
    ts: str,
    update: L2UpdateV1,
    *,
    source_ts: str | None = "",
) -> L2ReplayStepV1:
    clock = _dt(ts)
    return L2ReplayStepV1(
        sequence=sequence,
        timestamp_received=clock,
        timestamp=clock if source_ts == "" else _dt(source_ts) if source_ts is not None else None,
        updates=(update,),
        source_row_index=sequence,
    )


def _multi_update_step(
    sequence: int,
    ts: str,
    updates: tuple[L2UpdateV1, ...],
) -> L2ReplayStepV1:
    clock = _dt(ts)
    return L2ReplayStepV1(
        sequence=sequence,
        timestamp_received=clock,
        timestamp=clock,
        updates=updates,
        source_row_index=sequence,
    )


def _dataset(
    steps: list[L2ReplayStepV1] | tuple[L2ReplayStepV1, ...],
    *,
    dataset_id: str = "synthetic-wave0-l2-factors",
    source_quality: dict[str, Any] | None = None,
) -> PolymarketL2DatasetV1:
    return PolymarketL2DatasetV1(
        metadata=DatasetMetadataV1(
            dataset_id=dataset_id,
            adapter_name="pmxt_event_v1",
            adapter_version="test",
            source_type="synthetic",
            source_quality=source_quality or {},
        ),
        steps=tuple(steps),
    )


def _non_monotonic_dataset() -> PolymarketL2DatasetV1:
    return _dataset(
        [
            _book_step(1, "2026-07-14T00:00:02Z", bids=[("0.45", "10")], asks=[("0.55", "10")]),
            _book_step(2, "2026-07-14T00:00:01Z", bids=[("0.44", "10")], asks=[("0.56", "10")]),
        ],
    )


def _typed_label_dataset() -> PolymarketL2DatasetV1:
    return _dataset(
        [
            _book_step(1, "2026-07-14T00:00:00Z", bids=[("0.40", "10")], asks=[("0.60", "10")]),
            _book_step(2, "2026-07-14T00:00:30Z", bids=[("0.45", "11")], asks=[("0.65", "9")]),
        ],
    )


def _malformed_update(case: str) -> L2UpdateV1:
    updates = {
        "unsupported-event-type": L2UpdateV1(
            event_type="cancel",  # type: ignore[arg-type]
            market=EVENT,
            asset_id=YES,
        ),
        "price-change-invalid-side": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="buy",  # type: ignore[arg-type]
            price=Decimal("0.40"),
            size=Decimal(10),
        ),
        "price-change-missing-price": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            size=Decimal(10),
        ),
        "price-change-nonfinite-price": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("NaN"),
            size=Decimal(10),
        ),
        "price-change-price-below-zero": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("-0.01"),
            size=Decimal(10),
        ),
        "price-change-price-above-one": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("1.01"),
            size=Decimal(10),
        ),
        "price-change-missing-size": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.40"),
        ),
        "price-change-nonfinite-size": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.40"),
            size=Decimal("NaN"),
        ),
        "price-change-negative-size": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.40"),
            size=Decimal(-1),
        ),
        "snapshot-nonfinite-price": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price=Decimal("NaN"), size=Decimal(10)),),
            asks=(LevelV1(price=Decimal("0.60"), size=Decimal(10)),),
        ),
        "snapshot-price-below-zero": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price=Decimal("-0.01"), size=Decimal(10)),),
            asks=(LevelV1(price=Decimal("0.60"), size=Decimal(10)),),
        ),
        "snapshot-price-above-one": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price=Decimal("0.40"), size=Decimal(10)),),
            asks=(LevelV1(price=Decimal("1.01"), size=Decimal(10)),),
        ),
        "snapshot-zero-size": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price=Decimal("0.40"), size=Decimal(0)),),
            asks=(LevelV1(price=Decimal("0.60"), size=Decimal(10)),),
        ),
        "snapshot-negative-size": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price=Decimal("0.40"), size=Decimal(10)),),
            asks=(LevelV1(price=Decimal("0.60"), size=Decimal(-1)),),
        ),
        "trade-missing-side": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            price=Decimal("0.50"),
            size=Decimal(1),
        ),
        "trade-invalid-side": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="HOLD",  # type: ignore[arg-type]
            price=Decimal("0.50"),
            size=Decimal(1),
        ),
        "trade-missing-price": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            size=Decimal(1),
        ),
        "trade-nonfinite-price": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("Infinity"),
            size=Decimal(1),
        ),
        "trade-price-above-one": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("1.01"),
            size=Decimal(1),
        ),
        "trade-missing-size": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.50"),
        ),
        "trade-nonpositive-size": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.50"),
            size=Decimal(0),
        ),
    }
    return updates[case]


def _runtime_type_violation_update(case: str) -> L2UpdateV1:
    valid_ask = (LevelV1(price=Decimal("0.60"), size=Decimal(10)),)
    updates = {
        "snapshot-level-not-level-v1": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(SimpleNamespace(price=Decimal("0.40"), size=Decimal(10)),),  # type: ignore[arg-type]
            asks=valid_ask,
        ),
        "snapshot-price-string": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price="0.40", size=Decimal(10)),),  # type: ignore[arg-type]
            asks=valid_ask,
        ),
        "snapshot-size-float": L2UpdateV1(
            event_type="book",
            market=EVENT,
            asset_id=YES,
            bids=(LevelV1(price=Decimal("0.40"), size=10.0),),  # type: ignore[arg-type]
            asks=valid_ask,
        ),
        "price-change-price-string": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price="0.40",  # type: ignore[arg-type]
            size=Decimal(10),
        ),
        "price-change-size-float": L2UpdateV1(
            event_type="price_change",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.40"),
            size=10.0,  # type: ignore[arg-type]
        ),
        "trade-price-float": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=0.50,  # type: ignore[arg-type]
            size=Decimal(1),
        ),
        "trade-size-string": L2UpdateV1(
            event_type="trade",
            market=EVENT,
            asset_id=YES,
            side="BUY",
            price=Decimal("0.50"),
            size="1",  # type: ignore[arg-type]
        ),
        "tick-old-string": L2UpdateV1(
            event_type="tick_size_change",
            market=EVENT,
            asset_id=YES,
            old_tick_size="0.01",  # type: ignore[arg-type]
            new_tick_size=Decimal("0.001"),
        ),
        "tick-new-float": L2UpdateV1(
            event_type="tick_size_change",
            market=EVENT,
            asset_id=YES,
            old_tick_size=Decimal("0.01"),
            new_tick_size=0.001,  # type: ignore[arg-type]
        ),
    }
    return updates[case]


def _incremental_depth_dataset(
    depth: int,
    *,
    include_nonmutation_rows: bool = False,
) -> PolymarketL2DatasetV1:
    timestamp = "2026-07-14T00:00:00Z"
    steps = [
        _book_step(
            1,
            timestamp,
            bids=[("0.05", "10")],
            asks=[("0.90", "10")],
        ),
    ]
    final_price = Decimal("0.10")
    for position in range(depth):
        final_price = Decimal("0.10") + Decimal(position) / Decimal(1000)
        steps.append(
            _price_step(
                position + 2,
                timestamp,
                "BUY",
                format(final_price, "f"),
                "10",
            ),
        )

    if include_nonmutation_rows:
        sequence = depth + 2
        steps.extend(
            [
                _price_step(sequence, timestamp, "BUY", format(final_price, "f"), "10"),
                _trade_step(sequence + 1, timestamp),
                _tick_step(sequence + 2, timestamp, old="0.01", new="0.001"),
            ],
        )
    return _dataset(steps)


def _install_sorted_item_probe(
    factor_protocol: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    sorted_item_count = [0]
    native_sorted = sorted

    def counting_sorted(
        values: Any,
        /,
        *,
        key: Any = None,
        reverse: bool = False,
    ) -> list[Any]:
        materialized = list(values)
        sorted_item_count[0] += len(materialized)
        return native_sorted(materialized, key=key, reverse=reverse)

    monkeypatch.setattr(factor_protocol, "sorted", counting_sorted, raising=False)
    return sorted_item_count


def _assert_error_context(error: ValueError, *fragments: str) -> None:
    message = str(error).lower()
    for fragment in fragments:
        assert fragment.lower() in message, (fragment, str(error))


class _PandasClockProbe:
    def __init__(self, pandas_module: Any, comparison_count: list[int]) -> None:
        self._pandas = pandas_module
        self._comparison_count = comparison_count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pandas, name)

    def Timestamp(self, value: Any) -> _CountingTimestamp:
        timestamp = self._pandas.Timestamp(value)
        return _CountingTimestamp(timestamp.value / 1_000_000_000, self._comparison_count)

    def Timedelta(self, *args: Any, **kwargs: Any) -> _CountingDuration:
        duration = self._pandas.Timedelta(*args, **kwargs)
        return _CountingDuration(duration.total_seconds())


class _CountingTimestamp:
    def __init__(self, seconds: float, comparison_count: list[int]) -> None:
        self.seconds = seconds
        self.comparison_count = comparison_count

    def __sub__(self, other: _CountingTimestamp | _CountingDuration) -> _CountingTimestamp | _CountingDuration:
        if isinstance(other, _CountingDuration):
            return _CountingTimestamp(self.seconds - other.seconds, self.comparison_count)
        return _CountingDuration(self.seconds - other.seconds)

    def __lt__(self, other: _CountingTimestamp) -> bool:
        self.comparison_count[0] += 1
        return self.seconds < other.seconds

    def __le__(self, other: _CountingTimestamp) -> bool:
        self.comparison_count[0] += 1
        return self.seconds <= other.seconds


class _CountingDuration:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def total_seconds(self) -> float:
        return self.seconds


def _level(price: str, size: str) -> LevelV1:
    return LevelV1(price=Decimal(price), size=Decimal(size))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or bool(pd.isna(value))


def _result_get(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result[key]
    return getattr(result, key)


def _result_frame(result: Any, key: str) -> pd.DataFrame:
    value = _result_get(result, key)
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame(value)


def _assert_digest_equal(left: pd.DataFrame, right: pd.DataFrame) -> None:
    assert _frame_digest(left) == _frame_digest(right)


def _frame_digest(frame: pd.DataFrame) -> str:
    stable = _stable_frame(frame)
    payload = stable.to_json(orient="split", date_format="iso", default_handler=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _stable_frame(frame: pd.DataFrame) -> pd.DataFrame:
    stable = frame.copy()
    sort_columns = [
        column
        for column in ("event_id", "token_id", "sequence", "factor", "horizon_seconds", "cohort")
        if column in stable.columns
    ]
    if sort_columns:
        stable = stable.sort_values(sort_columns, kind="mergesort")
    return stable.reset_index(drop=True)


def _current_factor_columns(frame: pd.DataFrame) -> list[str]:
    forbidden_prefixes = ("future_", "label_", "next_nonzero_")
    return [column for column in frame.columns if not str(column).startswith(forbidden_prefixes)]


def _factor_and_label_columns(frame: pd.DataFrame) -> list[str]:
    factor_columns = set(BOOK_DERIVED_RANKING_FACTORS) | {
        "tick_size_regime",
        "book_staleness_seconds",
        "book_update_intensity",
        "valid_observation_counts",
        "ranking_observation",
    }
    label_prefixes = ("future_", "label_", "next_nonzero_")
    return [
        column
        for column in frame.columns
        if column in factor_columns or str(column).startswith(label_prefixes)
    ]
