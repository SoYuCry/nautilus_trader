from __future__ import annotations

# ruff: noqa: E402, I001

import hashlib
import importlib.util
import math
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
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


def test_build_factor_panel_rejects_non_monotonic_canonical_replay_clock(
    factor_protocol: Any,
) -> None:
    dataset = _dataset(
        [
            _book_step(1, "2026-07-14T00:00:02Z", bids=[("0.45", "10")], asks=[("0.55", "10")]),
            _book_step(2, "2026-07-14T00:00:01Z", bids=[("0.44", "10")], asks=[("0.56", "10")]),
        ],
    )

    with pytest.raises(ValueError, match=r"(?i)(replay|order|clock)"):
        factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False)


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

    row = factor_protocol.build_factor_panel(dataset, horizons_seconds=(30,), include_labels=False).iloc[0]

    assert row["ranking_observation"] is True
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
    assert panel.loc[panel["sequence"] == 2, "tick_size_regime"].iloc[0] == pytest.approx(0.001)
    assert panel.loc[panel["sequence"] == 3, "tick_size_regime"].iloc[0] == pytest.approx(0.001)
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
    assert panel.loc[4, "book_update_intensity"] == pytest.approx(2 / 30)
    assert panel.loc[6, "book_update_intensity"] == pytest.approx(2 / 30)


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

    assert panel.loc[panel["sequence"] == 2, "ranking_observation"].iloc[0] is False
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
    token_id: str = YES,
    best_bid: str | None = None,
    best_ask: str | None = None,
    source_ts: str | None = "",
) -> L2ReplayStepV1:
    update = L2UpdateV1(
        event_type="book",
        market=EVENT,
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
    token_id: str = YES,
) -> L2ReplayStepV1:
    update = L2UpdateV1(
        event_type="price_change",
        market=EVENT,
        asset_id=token_id,
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        size=Decimal(size),
        best_bid=Decimal("0.01"),
        best_ask=Decimal("0.99"),
    )
    return _step(sequence, ts, update)


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


def _dataset(steps: list[L2ReplayStepV1] | tuple[L2ReplayStepV1, ...]) -> PolymarketL2DatasetV1:
    return PolymarketL2DatasetV1(
        metadata=DatasetMetadataV1(
            dataset_id="synthetic-wave0-l2-factors",
            adapter_name="pmxt_event_v1",
            adapter_version="test",
            source_type="synthetic",
        ),
        steps=tuple(steps),
    )


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
