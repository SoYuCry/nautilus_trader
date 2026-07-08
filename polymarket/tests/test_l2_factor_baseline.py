from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


RESEARCH_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-08-pmxt-l2-factor-baseline"
    / "factor_research.py"
)


def _load_factor_research_module() -> Any:
    """Load the dated research script without requiring an importable package name."""
    assert RESEARCH_SCRIPT.exists(), (
        "Expected PMXT L2 factor baseline script at "
        "polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py"
    )
    spec = importlib.util.spec_from_file_location("pmxt_l2_factor_baseline", RESEARCH_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_updates() -> list[dict[str, Any]]:
    return [
        {
            "event_type": "book",
            "timestamp": "2026-07-08T00:00:00Z",
            "asset_id": "YES",
            "bids": [
                {"price": "0.48", "size": "10"},
                {"price": "0.47", "size": "20"},
                {"price": "0.46", "size": "30"},
                {"price": "0.45", "size": "40"},
                {"price": "0.44", "size": "50"},
            ],
            "asks": [
                {"price": "0.51", "size": "8"},
                {"price": "0.52", "size": "12"},
                {"price": "0.53", "size": "99"},
                {"price": "0.54", "size": "16"},
                {"price": "0.55", "size": "24"},
                {"price": "0.56", "size": "32"},
            ],
        },
        {
            "event_type": "price_change",
            "timestamp": "2026-07-08T00:00:01Z",
            "asset_id": "YES",
            "side": "BUY",
            "price": "0.49",
            "size": "5",
        },
        {
            "event_type": "price_change",
            "timestamp": "2026-07-08T00:00:02Z",
            "asset_id": "YES",
            "side": "SELL",
            "price": "0.53",
            "size": "0",
        },
        {
            "event_type": "last_trade_price",
            "timestamp": "2026-07-08T00:00:03Z",
            "asset_id": "YES",
            "side": "BUY",
            "price": "0.52",
            "size": "4",
        },
        {
            "event_type": "tick_size_change",
            "timestamp": "2026-07-08T00:00:04Z",
            "asset_id": "YES",
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
        },
    ]


def _label_updates() -> list[dict[str, Any]]:
    return [
        {
            "event_type": "book",
            "timestamp": "2026-07-08T01:00:00Z",
            "asset_id": "YES",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "10"}],
        },
        {
            "event_type": "price_change",
            "timestamp": "2026-07-08T01:00:01Z",
            "asset_id": "YES",
            "side": "BUY",
            "price": "0.50",
            "size": "10",
        },
        {
            "event_type": "price_change",
            "timestamp": "2026-07-08T01:00:02Z",
            "asset_id": "YES",
            "side": "SELL",
            "price": "0.70",
            "size": "10",
        },
    ]


@pytest.fixture
def factor_research() -> Any:
    return _load_factor_research_module()


def test_reconstruct_l2_book_applies_snapshot_deltas_trades_and_tick_regime(
    factor_research: Any,
) -> None:
    rows = factor_research.reconstruct_l2_book(_synthetic_updates())

    after_snapshot = rows[0]
    assert after_snapshot["bid_levels"] == {
        Decimal("0.48"): Decimal(10),
        Decimal("0.47"): Decimal(20),
        Decimal("0.46"): Decimal(30),
        Decimal("0.45"): Decimal(40),
        Decimal("0.44"): Decimal(50),
    }
    assert after_snapshot["ask_levels"][Decimal("0.53")] == Decimal(99)

    after_bid_update = rows[1]
    assert after_bid_update["bid_levels"][Decimal("0.49")] == Decimal(5)

    after_ask_delete = rows[2]
    assert Decimal("0.53") not in after_ask_delete["ask_levels"]
    assert after_ask_delete["ask_levels"][Decimal("0.54")] == Decimal(16)

    after_trade = rows[3]
    assert after_trade["bid_levels"] == after_ask_delete["bid_levels"]
    assert after_trade["ask_levels"] == after_ask_delete["ask_levels"]
    assert after_trade["trade_pressure"] == Decimal(4)

    after_tick_change = rows[4]
    assert after_tick_change["tick_size"] == Decimal("0.001")
    assert after_tick_change["tail_regime"] is True


def test_calculate_l2_factors_emits_top_of_book_depth_and_boundary_metrics(
    factor_research: Any,
) -> None:
    states = factor_research.reconstruct_l2_book(_synthetic_updates())
    factors = factor_research.calculate_l2_factors(states)
    row = factors.iloc[4]

    assert row["bid1"] == pytest.approx(0.49)
    assert row["ask1"] == pytest.approx(0.51)
    assert row["mid"] == pytest.approx(0.50)
    assert row["spread"] == pytest.approx(0.02)
    assert row["microprice"] == pytest.approx(((0.51 * 5) + (0.49 * 8)) / (5 + 8))
    assert row["depth_imbalance_1"] == pytest.approx((5 - 8) / (5 + 8))
    assert row["depth_imbalance_3"] == pytest.approx((35 - 36) / (35 + 36))
    assert row["depth_imbalance_5"] == pytest.approx((105 - 92) / (105 + 92))
    assert row["distance_to_boundary"] == pytest.approx(0.5)
    assert bool(row["tail_regime"]) is True
    assert row["book_validity"] == "valid"
    assert bool(row["is_valid_book"]) is True


def test_labels_use_reconstructed_future_top_of_book_without_fee_or_tradeable_profit(
    factor_research: Any,
) -> None:
    dataset = factor_research.build_l2_factor_dataset(
        _label_updates(),
        horizon_rows=2,
    )

    first = dataset.iloc[0]
    assert first["bid1"] == pytest.approx(0.40)
    assert first["ask1"] == pytest.approx(0.60)
    assert first["mid"] == pytest.approx(0.50)

    assert first["future_bid1"] == pytest.approx(0.50)
    assert first["future_ask1"] == pytest.approx(0.60)
    assert first["future_mid_return"] == pytest.approx(0.05)
    assert "microprice_minus_mid" in dataset.columns
    assert_no_execution_columns(dataset.columns)


def test_label_asof_uses_last_state_for_duplicate_receive_timestamp(
    factor_research: Any,
) -> None:
    panel = factor_research.pd.DataFrame(
        {
            "timestamp_received": factor_research.pd.to_datetime(
                [
                    "2026-07-08T00:00:00Z",
                    "2026-07-08T00:00:01Z",
                    "2026-07-08T00:00:01Z",
                    "2026-07-08T00:00:02Z",
                ],
            ),
            "sequence": [1, 2, 3, 4],
            "mid": [0.50, 0.51, 0.55, 0.56],
            "bid1": [0.49, 0.50, 0.54, 0.55],
            "ask1": [0.51, 0.52, 0.56, 0.57],
        },
    )

    labelled = factor_research.add_labels(panel, {"labels": {"horizons_seconds": [1]}})

    assert labelled.loc[0, "future_mid_1s"] == pytest.approx(0.55)
    assert labelled.loc[0, "future_bid_1s"] == pytest.approx(0.54)
    assert labelled.loc[0, "future_ask_1s"] == pytest.approx(0.56)
    assert labelled.loc[0, "future_book_validity_1s"] == "valid"
    assert labelled.loc[0, "label_slippage_seconds_1s"] == pytest.approx(0.0)
    assert_no_execution_columns(labelled.columns)


def test_book_validity_classifies_missing_locked_and_crossed_books(
    factor_research: Any,
) -> None:
    assert factor_research.classify_book_validity(float("nan"), 0.51) == "missing"
    assert factor_research.classify_book_validity(0.50, 0.50) == "locked"
    assert factor_research.classify_book_validity(0.51, 0.50) == "crossed"
    assert factor_research.classify_book_validity(0.49, 0.50) == "valid"


def assert_no_execution_columns(columns: Any) -> None:
    banned_exact = {"taker_fee", "fee_sensitivity", "pnl", "fill"}
    banned_fragments = (
        "edge",
        "execution",
        "executable",
        "fee",
        "fill",
        "long_edge",
        "pnl",
        "profit",
        "short_edge",
        "tradeable",
    )
    names = [str(column).lower() for column in columns]
    assert banned_exact.isdisjoint(names)
    assert not any(fragment in name for name in names for fragment in banned_fragments)
