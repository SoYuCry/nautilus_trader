from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


RESEARCH_DIR = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-14-pmxt-weather-factor-wave0"
)
FACTOR_PROTOCOL = RESEARCH_DIR / "factor_protocol.py"
PROTOCOL_JSON = RESEARCH_DIR / "protocol.json"

# Market outcome token identifier used in synthetic Polymarket rows; this is
# not a password/secret, and keeping it as a constant avoids Bandit S106's
# keyword-argument false positive on `token_id=...`.
MARKET_TOKEN_E1_YES = "E1-YES"  # noqa: S105 - Polymarket market token id, not a password/secret.

BANNED_OUTCOME_COLUMNS = {
    "outcome",
    "winner",
    "settlement",
    "settled",
    "resolved",
    "resolution",
    "pnl",
    "profit",
    "cash",
    "position",
    "fill",
    "fee",
    "future_return",
    "strategy_signal",
}

STRICT_CANDIDATE_EVIDENCE_COLUMNS = (
    "block_bootstrap_interval_excludes_zero",
    "widest_spread_independent",
    "coverage_complete",
    "failure_manifest_complete",
    "canonical_hashes_complete",
)


@pytest.fixture
def factor_protocol() -> Any:
    """Load the Wave 0 protocol by path so the dated research dir need not be a package."""
    assert FACTOR_PROTOCOL.exists(), (
        "Expected Wave 0 factor protocol at "
        "polymarket/research/2026-07-14-pmxt-weather-factor-wave0/factor_protocol.py"
    )
    spec = importlib.util.spec_from_file_location("pmxt_weather_factor_wave0_protocol", FACTOR_PROTOCOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preregistered_protocol() -> dict[str, Any]:
    assert PROTOCOL_JSON.exists(), (
        "Expected Wave 0 preregistration protocol at "
        "polymarket/research/2026-07-14-pmxt-weather-factor-wave0/protocol.json"
    )
    return json.loads(PROTOCOL_JSON.read_text(encoding="utf-8"))


def test_factor_name_registries_match_preregistered_protocol_exactly(
    factor_protocol: Any,
    preregistered_protocol: dict[str, Any],
) -> None:
    factors = preregistered_protocol["factors"]

    assert tuple(factor_protocol.PRIMARY_FACTOR_NAMES) == tuple(factors["ranking_eligible"])
    assert tuple(factor_protocol.DIAGNOSTIC_FACTOR_NAMES) == tuple(factors["diagnostic_only"])


def test_primary_horizon_registry_matches_preregistered_wall_clock_labels_exactly(
    factor_protocol: Any,
    preregistered_protocol: dict[str, Any],
) -> None:
    expected = (30, 120, 600)
    protocol_horizons = tuple(
        label["seconds"]
        for label in preregistered_protocol["labels"]["primary"]
        if label["type"] == "elapsed_wall_clock_seconds"
    )

    assert expected == factor_protocol.PRIMARY_HORIZONS_SECONDS
    assert protocol_horizons == expected


@pytest.mark.parametrize(
    "horizons_seconds",
    [
        (30, 30, 120),
        (30, 120),
        (30, 120, 900),
    ],
    ids=("duplicate", "missing-primary", "non-primary"),
)
def test_run_factor_protocol_rejects_noncanonical_primary_horizons(
    factor_protocol: Any,
    horizons_seconds: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match=r"(?i)horizon"):
        factor_protocol.run_factor_protocol(
            _base_rows(event_id="E1", token_id=MARKET_TOKEN_E1_YES),
            horizons_seconds=horizons_seconds,
        )


def test_prefix_factor_values_are_unchanged_by_appended_future_poison_rows(factor_protocol: Any) -> None:
    """
    Required API:
      build_factor_panel(rows, *, horizons_seconds, include_labels=True) -> DataFrame

    Current/prefix factor values must be prefix-causal. Appending future rows with
    poisoned outcome/PnL/future-return fields may create future labels for earlier
    rows, but it must not alter current factor values, quality flags, or cohort
    assignment for the original prefix.
    """
    prefix = _base_rows(event_id="E1", token_id=MARKET_TOKEN_E1_YES)
    poisoned_future = _poison_future_rows(event_id="E1", token_id=MARKET_TOKEN_E1_YES)

    prefix_panel = factor_protocol.build_factor_panel(prefix, horizons_seconds=(30, 120, 600), include_labels=True)
    appended_panel = factor_protocol.build_factor_panel(
        prefix + poisoned_future,
        horizons_seconds=(30, 120, 600),
        include_labels=True,
    )

    prefix_only = appended_panel[appended_panel["sequence"].isin(prefix_panel["sequence"])].reset_index(drop=True)
    _assert_frames_equal_on_columns(prefix_panel, prefix_only, _current_factor_and_quality_columns(prefix_panel))


def test_outcome_settlement_and_pnl_columns_do_not_affect_factors_quality_or_candidates(
    factor_protocol: Any,
) -> None:
    """
    Required API:
      run_factor_protocol(rows, *, horizons_seconds) -> mapping/object with:
        panel: DataFrame
        candidate_table: DataFrame
        primary_shortlist: DataFrame or list[dict]

    Outcome/winner/settlement/PnL-like fields are forbidden inputs for current
    factors, data quality classification, and candidate gates.
    """
    rows = _multi_event_rows()
    poisoned = copy.deepcopy(rows)
    for index, row in enumerate(poisoned):
        row.update(
            {
                "outcome": "YES" if index % 2 else "NO",
                "winner": row["token_id"],
                "settlement": 1.0 if index % 2 else 0.0,
                "settled": True,
                "pnl": 10_000 - index,
                "profit": -999 + index,
                "fee": 123,
                "fill": "TAKER",
                "position": index,
                "future_return": 999 if index % 2 else -999,
                "strategy_signal": "leak",
            },
        )

    clean_result = factor_protocol.run_factor_protocol(rows, horizons_seconds=(30, 120, 600))
    poisoned_result = factor_protocol.run_factor_protocol(poisoned, horizons_seconds=(30, 120, 600))

    clean_panel = _result_frame(clean_result, "panel")
    poisoned_panel = _result_frame(poisoned_result, "panel")
    _assert_frames_equal_on_columns(clean_panel, poisoned_panel, _current_factor_and_quality_columns(clean_panel))
    _assert_no_banned_columns(clean_panel.columns)

    _assert_digest_equal(_result_frame(clean_result, "candidate_table"), _result_frame(poisoned_result, "candidate_table"))
    _assert_digest_equal(_as_frame(_result_get(clean_result, "primary_shortlist")), _as_frame(_result_get(poisoned_result, "primary_shortlist")))


def test_bbo_is_reconstructed_from_local_l2_not_pmxt_row_best_bid_best_ask(factor_protocol: Any) -> None:
    rows = [
        _book_row(
            sequence=1,
            ts="2026-07-14T00:00:00Z",
            event_id="E1",
            token_id=MARKET_TOKEN_E1_YES,
            bids=[("0.49", "11"), ("0.48", "20")],
            asks=[("0.51", "13"), ("0.52", "30")],
            best_bid="0.01",
            best_ask="0.99",
        ),
    ]

    panel = factor_protocol.build_factor_panel(rows, horizons_seconds=(30, 120, 600), include_labels=False)

    assert panel.loc[0, "bid1"] == pytest.approx(0.49)
    assert panel.loc[0, "ask1"] == pytest.approx(0.51)
    assert panel.loc[0, "mid"] == pytest.approx(0.50)
    assert panel.loc[0, "spread"] == pytest.approx(0.02)


def test_wall_clock_labels_handle_30_120_600_duplicate_timestamps_and_invalid_books(
    factor_protocol: Any,
) -> None:
    rows = [
        _book_row(1, "2026-07-14T00:00:00Z", "E1", "E1-YES", [("0.40", "10")], [("0.60", "10")]),
        _book_row(2, "2026-07-14T00:00:00Z", "E1", "E1-YES", [("0.45", "10")], [("0.55", "10")]),
        _book_row(3, "2026-07-14T00:00:30Z", "E1", "E1-YES", [("0.61", "10")], [("0.59", "10")]),
        _book_row(4, "2026-07-14T00:02:00Z", "E1", "E1-YES", [("0.50", "10")], [("0.70", "10")]),
        _book_row(5, "2026-07-14T00:02:00Z", "E1", "E1-YES", [("0.60", "10")], [("0.80", "10")]),
        _book_row(6, "2026-07-14T00:02:00Z", "E1", "E1-YES", [("0.65", "10")], [("0.85", "10")]),
        _book_row(7, "2026-07-14T00:10:00Z", "E1", "E1-YES", [("0.55", "10")], [("0.65", "10")]),
    ]

    panel = factor_protocol.build_factor_panel(rows, horizons_seconds=(30, 120, 600), include_labels=True)
    first = panel.loc[panel["sequence"] == 1].iloc[0]

    assert first["label_matched_timestamp_30s"] == pd.Timestamp("2026-07-14T00:00:30Z")
    assert first["future_book_validity_30s"] == "crossed"
    assert _is_missing(first["future_mid_return_30s"])

    # Duplicate timestamp groups use the last reconstructed state at that clock
    # before applying elapsed wall-clock horizons.
    duplicate_last = panel.loc[panel["sequence"] == 2].iloc[0]
    assert duplicate_last["mid"] == pytest.approx(0.50)
    assert duplicate_last["label_matched_timestamp_120s"] == pd.Timestamp("2026-07-14T00:02:00Z")
    assert duplicate_last["future_mid_120s"] == pytest.approx(0.75)
    assert duplicate_last["future_mid_return_120s"] == pytest.approx(0.25)
    assert duplicate_last["future_mid_600s"] == pytest.approx(0.60)
    assert duplicate_last["future_mid_return_600s"] == pytest.approx(0.10)
    assert "future_mid_return_30_rows" not in panel.columns
    assert "future_mid_return_valid_observations_30" not in panel.columns


def test_missing_source_timestamp_falls_back_to_received_time_and_sequence_tie_break(
    factor_protocol: Any,
) -> None:
    rows = [
        _book_row(3, "2026-07-14T00:00:30Z", "E1", "E1-YES", [("0.60", "10")], [("0.80", "10")], source_ts=None),
        _book_row(2, "2026-07-14T00:00:00Z", "E1", "E1-YES", [("0.50", "10")], [("0.60", "10")], source_ts=None),
        _book_row(1, "2026-07-14T00:00:00Z", "E1", "E1-YES", [("0.40", "10")], [("0.50", "10")], source_ts=None),
    ]

    panel = factor_protocol.build_factor_panel(rows, horizons_seconds=(30,), include_labels=True)

    assert list(panel["sequence"]) == [1, 2, 3]
    assert list(panel["timestamp"]) == [
        pd.Timestamp("2026-07-14T00:00:00Z"),
        pd.Timestamp("2026-07-14T00:00:00Z"),
        pd.Timestamp("2026-07-14T00:00:30Z"),
    ]
    first = panel.loc[panel["sequence"] == 1].iloc[0]
    assert first["label_matched_timestamp_30s"] == pd.Timestamp("2026-07-14T00:00:30Z")
    assert first["future_mid_30s"] == pytest.approx(0.70)
    assert first["future_mid_return_30s"] == pytest.approx(0.25)


def test_next_nonzero_mid_move_skips_zero_moves_and_invalid_books(factor_protocol: Any) -> None:
    rows = [
        _book_row(1, "2026-07-14T00:00:00Z", "E1", "E1-YES", [("0.45", "10")], [("0.55", "10")]),
        _book_row(2, "2026-07-14T00:00:01Z", "E1", "E1-YES", [("0.46", "9")], [("0.54", "9")]),
        _book_row(3, "2026-07-14T00:00:02Z", "E1", "E1-YES", [("0.70", "10")], [("0.60", "10")]),
        _book_row(4, "2026-07-14T00:00:03Z", "E1", "E1-YES", [("0.50", "10")], [("0.60", "10")]),
    ]

    panel = factor_protocol.build_factor_panel(rows, horizons_seconds=(30, 120, 600), include_labels=True)
    first = panel.loc[panel["sequence"] == 1].iloc[0]

    assert first["mid"] == pytest.approx(0.50)
    assert first["next_nonzero_mid_move"] == pytest.approx(0.05)
    assert first["next_nonzero_mid_move_direction"] == 1
    assert first["next_nonzero_mid_move_matched_sequence"] == 4


def test_valid_observation_count_is_diagnostic_only_and_cannot_enter_primary_shortlist(
    factor_protocol: Any,
    preregistered_protocol: dict[str, Any],
) -> None:
    rows = _multi_event_rows()
    valid_observation_factor = "valid_observation_counts"

    result = factor_protocol.run_factor_protocol(rows, horizons_seconds=(30, 120, 600))
    candidate_table = _result_frame(result, "candidate_table")
    shortlist = _as_frame(_result_get(result, "primary_shortlist"))

    primary_factor_names = set(factor_protocol.PRIMARY_FACTOR_NAMES)
    diagnostic_factor_names = set(factor_protocol.DIAGNOSTIC_FACTOR_NAMES)
    assert valid_observation_factor in preregistered_protocol["factors"]["diagnostic_only"]
    assert valid_observation_factor in diagnostic_factor_names
    assert valid_observation_factor not in primary_factor_names
    assert valid_observation_factor in set(candidate_table["factor"])
    assert valid_observation_factor not in set(shortlist.get("factor", pd.Series(dtype=str)))


def test_aggregation_is_row_to_token_to_event_to_global_equal_weight_not_row_weighted(
    factor_protocol: Any,
) -> None:
    """
    Required API:
      aggregate_factor_metrics(token_rows) -> DataFrame with one row per
      factor/horizon and equal-weight token->event->global metrics.
    """
    token_metrics = pd.DataFrame(
        [
            {"event_id": "A", "token_id": "A-YES", "factor": "depth_imbalance_1", "horizon_seconds": 30, "token_ic": 1.0, "row_count": 10_000},
            {"event_id": "A", "token_id": "A-NO", "factor": "depth_imbalance_1", "horizon_seconds": 30, "token_ic": -1.0, "row_count": 10_000},
            {"event_id": "B", "token_id": "B-YES", "factor": "depth_imbalance_1", "horizon_seconds": 30, "token_ic": -0.5, "row_count": 10},
        ],
    )

    global_metrics = factor_protocol.aggregate_factor_metrics(token_metrics)
    metric = global_metrics.query("factor == 'depth_imbalance_1' and horizon_seconds == 30").iloc[0]

    assert metric["event_count"] == 2
    assert metric["token_count"] == 3
    assert metric["global_event_equal_ic"] == pytest.approx(-0.25)
    assert metric["row_weighted_ic_audit"] == pytest.approx((10_000 - 10_000 - 5) / 20_010)
    assert metric["global_event_equal_ic"] != pytest.approx(metric["row_weighted_ic_audit"])


def test_clean_and_degraded_cohorts_are_separate_and_sign_reversal_blocks_candidates(
    factor_protocol: Any,
) -> None:
    cohort_metrics = pd.DataFrame(
        [
            {"factor": "depth_imbalance_1", "horizon_seconds": 30, "cohort": "clean", "event_ic": 0.20, "positive_event_share": 0.70},
            {"factor": "depth_imbalance_1", "horizon_seconds": 120, "cohort": "clean", "event_ic": 0.18, "positive_event_share": 0.70},
            {"factor": "depth_imbalance_1", "horizon_seconds": 600, "cohort": "clean", "event_ic": 0.16, "positive_event_share": 0.70},
            {"factor": "depth_imbalance_1", "horizon_seconds": 30, "cohort": "degraded", "event_ic": -0.15, "positive_event_share": 0.30},
        ],
    )

    gated = factor_protocol.apply_candidate_gates(cohort_metrics)

    row = gated[gated["factor"] == "depth_imbalance_1"].iloc[0]
    assert row["clean_positive_event_share"] == pytest.approx(0.70)
    assert bool(row["clean_degraded_sign_reversal"]) is True
    assert bool(row["passes_shortlist"]) is False


def test_candidate_gate_fails_closed_without_required_evidence_and_passes_only_when_complete(
    factor_protocol: Any,
) -> None:
    missing_evidence_metrics = pd.DataFrame(
        [
            {"factor": "depth_imbalance_1", "horizon_seconds": 30, "cohort": "clean", "event_ic": 0.20, "positive_event_share": 0.70},
            {"factor": "depth_imbalance_1", "horizon_seconds": 120, "cohort": "clean", "event_ic": 0.18, "positive_event_share": 0.70},
            {"factor": "depth_imbalance_1", "horizon_seconds": 600, "cohort": "clean", "event_ic": 0.16, "positive_event_share": 0.70},
            {"factor": "depth_imbalance_1", "horizon_seconds": 30, "cohort": "degraded", "event_ic": 0.12, "positive_event_share": 0.65},
        ],
    )

    missing_evidence = factor_protocol.apply_candidate_gates(missing_evidence_metrics)

    missing_row = missing_evidence[missing_evidence["factor"] == "depth_imbalance_1"].iloc[0]
    assert bool(missing_row["same_sign_two_primary_horizons"]) is True
    assert bool(missing_row["clean_degraded_sign_reversal"]) is False
    assert missing_row["clean_positive_event_share"] >= 0.60
    assert bool(missing_row["passes_shortlist"]) is False

    complete_evidence_metrics = missing_evidence_metrics.assign(
        block_bootstrap_interval_excludes_zero=True,
        widest_spread_independent=True,
        coverage_complete=True,
        failure_manifest_complete=True,
        canonical_hashes_complete=True,
    )

    complete_evidence = factor_protocol.apply_candidate_gates(complete_evidence_metrics)

    complete_row = complete_evidence[complete_evidence["factor"] == "depth_imbalance_1"].iloc[0]
    assert bool(complete_row["block_bootstrap_interval_excludes_zero"]) is True
    assert bool(complete_row["widest_spread_independent"]) is True
    assert bool(complete_row["coverage_complete"]) is True
    assert bool(complete_row["failure_manifest_complete"]) is True
    assert bool(complete_row["canonical_hashes_complete"]) is True
    assert bool(complete_row["passes_shortlist"]) is True


@pytest.mark.parametrize("evidence_column", STRICT_CANDIDATE_EVIDENCE_COLUMNS)
@pytest.mark.parametrize(
    "evidence_values",
    [(True, pd.NA), (True, False)],
    ids=("unknown-is-not-complete", "explicit-false"),
)
def test_candidate_gate_rejects_incomplete_or_negative_explicit_evidence(
    factor_protocol: Any,
    evidence_column: str,
    evidence_values: tuple[bool, bool | Any],
) -> None:
    rows = []
    for index, horizon_seconds in enumerate((30, 120)):
        evidence = dict.fromkeys(STRICT_CANDIDATE_EVIDENCE_COLUMNS, True)
        evidence[evidence_column] = evidence_values[index]
        rows.append(
            {
                "factor": "depth_imbalance_1",
                "horizon_seconds": horizon_seconds,
                "cohort": "clean",
                "event_ic": 0.20 - index * 0.02,
                "positive_event_share": 0.70,
                **evidence,
            },
        )

    gated = factor_protocol.apply_candidate_gates(pd.DataFrame(rows))
    row = gated.loc[gated["factor"] == "depth_imbalance_1"].iloc[0]

    assert bool(row[evidence_column]) is False
    assert bool(row["passes_shortlist"]) is False


@pytest.mark.parametrize(
    ("horizons_seconds", "expected"),
    [
        ((30, 30), False),
        ((30, 900), False),
        ((30, 120), True),
    ],
    ids=("duplicate-30s", "non-primary-900s", "two-distinct-primary"),
)
def test_same_sign_gate_counts_distinct_primary_horizons_only(
    factor_protocol: Any,
    horizons_seconds: tuple[int, int],
    expected: bool,
) -> None:
    metrics = pd.DataFrame(
        [
            {
                "factor": "depth_imbalance_1",
                "horizon_seconds": horizon_seconds,
                "cohort": "clean",
                "event_ic": 0.20 - index * 0.02,
                "positive_event_share": 0.70,
            }
            for index, horizon_seconds in enumerate(horizons_seconds)
        ],
    )

    gated = factor_protocol.apply_candidate_gates(metrics)
    row = gated.loc[gated["factor"] == "depth_imbalance_1"].iloc[0]

    assert bool(row["same_sign_two_primary_horizons"]) is expected


def test_protocol_rerun_is_deterministic_for_panel_candidates_and_digest(factor_protocol: Any) -> None:
    rows = _multi_event_rows()

    first = factor_protocol.run_factor_protocol(rows, horizons_seconds=(30, 120, 600))
    second = factor_protocol.run_factor_protocol(list(reversed(rows)), horizons_seconds=(30, 120, 600))

    _assert_digest_equal(_result_frame(first, "panel"), _result_frame(second, "panel"))
    _assert_digest_equal(_result_frame(first, "candidate_table"), _result_frame(second, "candidate_table"))
    assert _result_get(first, "deterministic_digest") == _result_get(second, "deterministic_digest")


def _base_rows(*, event_id: str, token_id: str) -> list[dict[str, Any]]:
    return [
        _book_row(1, "2026-07-14T00:00:00Z", event_id, token_id, [("0.45", "10"), ("0.44", "8")], [("0.55", "12"), ("0.56", "9")]),
        _price_row(2, "2026-07-14T00:00:10Z", event_id, token_id, "BUY", "0.46", "11"),
        _price_row(3, "2026-07-14T00:00:20Z", event_id, token_id, "SELL", "0.54", "13"),
    ]


def _poison_future_rows(*, event_id: str, token_id: str) -> list[dict[str, Any]]:
    rows = [
        _book_row(4, "2026-07-14T00:00:30Z", event_id, token_id, [("0.90", "100")], [("0.91", "100")]),
        _book_row(5, "2026-07-14T00:02:00Z", event_id, token_id, [("0.10", "100")], [("0.11", "100")]),
        _book_row(6, "2026-07-14T00:10:00Z", event_id, token_id, [("0.80", "100")], [("0.81", "100")]),
    ]
    for row in rows:
        row.update({field: f"poison-{field}" for field in BANNED_OUTCOME_COLUMNS})
    return rows


def _multi_event_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sequence = 1
    for event_id, token_ids, base_bid in [
        ("A", ["A-YES", "A-NO"], 0.40),
        ("B", ["B-YES"], 0.55),
        ("C", ["C-YES", "C-NO"], 0.30),
    ]:
        for token_id in token_ids:
            for offset, size in [(0, 10), (30, 11), (120, 12), (600, 13)]:
                bid = base_bid + offset / 10_000
                ask = bid + 0.10
                rows.append(
                    _book_row(
                        sequence,
                        f"2026-07-14T00:{offset // 60:02d}:{offset % 60:02d}Z",
                        event_id,
                        token_id,
                        [(f"{bid:.2f}", str(size))],
                        [(f"{ask:.2f}", str(size + 1))],
                        source_quality_cohort="clean" if event_id != "C" else "degraded",
                    ),
                )
                sequence += 1
    return rows


def _book_row(
    sequence: int,
    ts: str,
    event_id: str,
    token_id: str,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    *,
    best_bid: str | None = None,
    best_ask: str | None = None,
    source_quality_cohort: str = "clean",
    source_ts: str | None = "",
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "timestamp": ts if source_ts == "" else source_ts,
        "timestamp_received": ts,
        "event_id": event_id,
        "market": event_id,
        "token_id": token_id,
        "asset_id": token_id,
        "event_type": "book",
        "bids": [{"price": price, "size": size} for price, size in bids],
        "asks": [{"price": price, "size": size} for price, size in asks],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "source_quality_cohort": source_quality_cohort,
    }


def _price_row(sequence: int, ts: str, event_id: str, token_id: str, side: str, price: str, size: str) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "timestamp": ts,
        "timestamp_received": ts,
        "event_id": event_id,
        "market": event_id,
        "token_id": token_id,
        "asset_id": token_id,
        "event_type": "price_change",
        "side": side,
        "price": price,
        "size": size,
        "best_bid": "0.02",
        "best_ask": "0.98",
        "source_quality_cohort": "clean",
    }


def _current_factor_and_quality_columns(frame: pd.DataFrame) -> list[str]:
    forbidden_prefixes = ("future_", "label_", "next_nonzero_")
    columns = []
    for column in frame.columns:
        name = str(column)
        if name.startswith(forbidden_prefixes):
            continue
        if name.lower() in BANNED_OUTCOME_COLUMNS:
            continue
        if any(fragment in name.lower() for fragment in ("outcome", "winner", "settlement", "pnl", "profit", "fill", "fee")):
            continue
        columns.append(name)
    return columns


def _result_get(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result[key]
    return getattr(result, key)


def _result_frame(result: Any, key: str) -> pd.DataFrame:
    return _as_frame(_result_get(result, key))


def _as_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame(value)


def _assert_frames_equal_on_columns(left: pd.DataFrame, right: pd.DataFrame, columns: list[str]) -> None:
    pd.testing.assert_frame_equal(
        _stable_frame(left[columns]),
        _stable_frame(right[columns]),
        check_dtype=False,
        check_like=True,
    )


def _assert_digest_equal(left: pd.DataFrame, right: pd.DataFrame) -> None:
    assert _frame_digest(left) == _frame_digest(right)


def _frame_digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(_stable_frame(frame).to_json(orient="split", date_format="iso", default_handler=str).encode()).hexdigest()


def _stable_frame(frame: pd.DataFrame) -> pd.DataFrame:
    stable = frame.copy()
    sort_columns = [column for column in ("event_id", "token_id", "sequence", "factor", "horizon_seconds", "cohort") if column in stable.columns]
    if sort_columns:
        stable = stable.sort_values(sort_columns, kind="mergesort")
    return stable.reset_index(drop=True)


def _assert_no_banned_columns(columns: Any) -> None:
    names = [str(column).lower() for column in columns]
    assert BANNED_OUTCOME_COLUMNS.isdisjoint(names)
    assert not any(fragment in name for name in names for fragment in ("outcome", "winner", "settlement", "pnl", "profit", "fee", "fill"))


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or bool(pd.isna(value))
