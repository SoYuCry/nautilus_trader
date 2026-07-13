from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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


def test_factor_boundary_metadata_degrades_on_clock_and_book_warnings(factor_research: Any) -> None:
    health = SimpleNamespace(
        ok=True,
        issues=[
            SimpleNamespace(severity="warning", code="source_time_inversion"),
            SimpleNamespace(severity="warning", code="source_delay_over_threshold"),
        ],
        summary=SimpleNamespace(
            source_time_inversion_count=1,
            source_delay_over_threshold_count=1,
            future_source_time_count=0,
            source_timestamp_missing_step_count=0,
        ),
    )
    panel = factor_research.pd.DataFrame({"book_validity": ["valid", "crossed"]})

    metadata = factor_research.build_trust_metadata(health, panel)

    assert metadata["data_tier"] == "TIER1_EXPLORATORY"
    assert metadata["run_grade"] == "TIER1_CAUTION_PMXT_QUALITY"
    assert metadata["replay_clock"] == "timestamp"
    assert metadata["ordering_key"] == "timestamp,timestamp_received,_original_row_index"
    assert metadata["causality"] == "pmxt_source_time_ordered_not_exchange_sequence"
    assert metadata["execution_claims_allowed"] is False
    assert metadata["source_time_policy"] == "primary_sort_key"
    assert metadata["source_time_diagnostics"]["source_time_inversion_count"] == 1
    assert metadata["book_validity_counts"]["crossed"] == 1
    assert metadata["not_for_pnl"] is True
    assert metadata["diagnostic_non_causal"] is True

    health_without_clock_warnings = SimpleNamespace(
        ok=True,
        issues=[],
        summary=SimpleNamespace(
            source_time_inversion_count=0,
            source_delay_over_threshold_count=0,
            future_source_time_count=0,
            source_timestamp_missing_step_count=0,
        ),
    )
    book_only_metadata = factor_research.build_trust_metadata(health_without_clock_warnings, panel)
    assert book_only_metadata["run_grade"] == "TIER1_CAUTION_PMXT_QUALITY"


def test_factor_metadata_json_and_report_include_claim_boundary(
    factor_research: Any,
    tmp_path: Path,
) -> None:
    health = SimpleNamespace(
        ok=True,
        issues=[],
        summary=SimpleNamespace(
            source_time_inversion_count=0,
            source_delay_over_threshold_count=0,
            future_source_time_count=0,
            source_timestamp_missing_step_count=0,
        ),
        to_dict=lambda: {"ok": True, "summary": {}, "issues": [], "assumptions": []},
    )
    metadata = factor_research.build_trust_metadata(
        health,
        factor_research.pd.DataFrame({"book_validity": ["valid"]}),
    )
    path = factor_research.write_run_metadata(tmp_path, metadata, health)

    loaded = factor_research.json.loads(path.read_text(encoding="utf-8"))
    assert loaded["trust_metadata"]["execution_claims_allowed"] is False
    assert loaded["trust_metadata"]["run_grade"] == "TIER1_OK_TIMESTAMP_ORDERED"

    summary = SimpleNamespace(
        rows_loaded=1,
        factor_rows=1,
        analysis_rows=1,
        first_timestamp_received="2026-07-08T00:00:00Z",
        last_timestamp_received="2026-07-08T00:00:00Z",
        first_replay_timestamp="2026-07-08T00:00:00Z",
        last_replay_timestamp="2026-07-08T00:00:00Z",
        replay_order_ok=True,
        health_warning_count=0,
        health_error_count=0,
        valid_book_rows=1,
        locked_book_rows=0,
        crossed_book_rows=0,
        missing_book_rows=0,
        panel_path=str(tmp_path / "factor_panel.csv"),
        panel_format="csv",
    )
    run_summary = factor_research.RunSummary(
        config_path=str(tmp_path / "experiment.yml"),
        output_dir=str(tmp_path),
        rows_loaded=1,
        factor_rows=1,
        analysis_rows=1,
        first_timestamp_received="2026-07-08T00:00:00Z",
        last_timestamp_received="2026-07-08T00:00:00Z",
        first_replay_timestamp="2026-07-08T00:00:00Z",
        last_replay_timestamp="2026-07-08T00:00:00Z",
        replay_order_ok=True,
        health_warning_count=0,
        health_error_count=0,
        valid_book_rows=1,
        locked_book_rows=0,
        crossed_book_rows=0,
        missing_book_rows=0,
        panel_path=str(tmp_path / "factor_panel.csv"),
        panel_format="csv",
        factor_summary_path=str(tmp_path / "factor_summary.csv"),
        quantile_returns_path=str(tmp_path / "quantile_returns.csv"),
        book_validity_summary_path=str(tmp_path / "book_validity_summary.csv"),
        spread_summary_path=str(tmp_path / "spread_summary.csv"),
        label_slippage_summary_path=str(tmp_path / "label_slippage_summary.csv"),
        input_hashes_path=str(tmp_path / "input_hashes.json"),
        run_metadata_path=str(path),
        trust_metadata=metadata,
        report_path=str(tmp_path / "report.md"),
    )
    run_summary_json = factor_research.asdict(run_summary)
    assert run_summary_json["trust_metadata"]["execution_claims_allowed"] is False
    assert run_summary_json["trust_metadata"]["run_grade"] == "TIER1_OK_TIMESTAMP_ORDERED"

    report_path = tmp_path / "report.md"
    factor_research.write_report(
        report_path,
        summary,
        {"input": {}, "labels": {"horizons_seconds": [60]}},
        health,
        factor_research.pd.DataFrame(),
        factor_research.pd.DataFrame(),
        factor_research.pd.DataFrame({"book_validity": ["valid"], "count": [1], "share": [1.0]}),
        factor_research.pd.DataFrame(),
        factor_research.pd.DataFrame(),
        [],
        trust_metadata=metadata,
    )
    report_text = report_path.read_text(encoding="utf-8")
    assert "TIER1_EXPLORATORY" in report_text
    assert "TIER1_OK_TIMESTAMP_ORDERED" in report_text
    assert "execution_claims_allowed" in report_text
    assert "false" in report_text


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


def test_label_asof_prefers_replay_timestamp_when_pmxt_receive_time_is_non_monotonic(
    factor_research: Any,
) -> None:
    panel = factor_research.pd.DataFrame(
        {
            "timestamp_received": factor_research.pd.to_datetime(
                [
                    "2026-07-08T00:00:03Z",
                    "2026-07-08T00:00:01Z",
                    "2026-07-08T00:00:02Z",
                ],
            ),
            "replay_timestamp": factor_research.pd.to_datetime(
                [
                    "2026-07-08T00:00:00Z",
                    "2026-07-08T00:00:01Z",
                    "2026-07-08T00:00:02Z",
                ],
            ),
            "sequence": [1, 2, 3],
            "mid": [0.50, 0.55, 0.60],
            "bid1": [0.49, 0.54, 0.59],
            "ask1": [0.51, 0.56, 0.61],
        },
    )

    labelled = factor_research.add_labels(panel, {"labels": {"horizons_seconds": [1]}})

    assert labelled.loc[0, "future_mid_1s"] == pytest.approx(0.55)
    assert labelled.loc[0, "label_matched_timestamp_1s"] == factor_research.pd.Timestamp("2026-07-08T00:00:01Z")
    assert_no_execution_columns(labelled.columns)


def test_pmxt_research_health_gate_allows_receive_time_inversion_only(factor_research: Any) -> None:
    receive_inversion = SimpleNamespace(severity="error", code="receive_time_inversion")
    sequence_inversion = SimpleNamespace(severity="error", code="sequence_inversion")

    assert factor_research.pmxt_research_blocking_health_issues(
        SimpleNamespace(issues=[receive_inversion]),
    ) == []
    assert factor_research.pmxt_research_blocking_health_issues(
        SimpleNamespace(issues=[receive_inversion, sequence_inversion]),
    ) == [sequence_inversion]


def test_factor_summary_timestamp_received_audit_span_uses_min_max_not_replay_order(
    factor_research: Any,
) -> None:
    raw = factor_research.pd.DataFrame(
        {
            "timestamp_received": factor_research.pd.to_datetime(
                [
                    "2026-07-08T00:10:00Z",
                    "2026-07-08T00:01:00Z",
                    "2026-07-08T00:05:00Z",
                ],
            ),
            "replay_timestamp": factor_research.pd.to_datetime(
                [
                    "2026-07-08T00:00:02Z",
                    "2026-07-08T00:00:00Z",
                    "2026-07-08T00:00:01Z",
                ],
            ),
            "book_validity": ["valid", "valid", "valid"],
        },
    )
    dataset = SimpleNamespace(
        metadata=SimpleNamespace(
            dataset_id="synthetic",
            adapter_name="pmxt_event_v1",
            adapter_version="v1",
            source_quality={},
        ),
        steps=[object(), object(), object()],
    )
    health = SimpleNamespace(
        ok=True,
        issues=[],
        summary=SimpleNamespace(
            source_time_inversion_count=0,
            source_delay_over_threshold_count=0,
            future_source_time_count=0,
            source_timestamp_missing_step_count=0,
        ),
    )

    summary = factor_research.build_factor_summary(raw, dataset, health, {"labels": {"horizons_seconds": []}}, raw_panel=raw)
    metrics = dict(zip(summary["metric"], summary["value"], strict=True))

    assert metrics["first_timestamp_received"] == "2026-07-08T00:01:00+00:00"
    assert metrics["last_timestamp_received"] == "2026-07-08T00:10:00+00:00"
    assert metrics["first_replay_timestamp"] == "2026-07-08T00:00:00+00:00"
    assert metrics["last_replay_timestamp"] == "2026-07-08T00:00:02+00:00"


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
