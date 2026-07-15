from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/calibrate_quality.py"
REAL_INPUT_DIR = REPO_ROOT / "polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs"


def _load_module():
    spec = importlib.util.spec_from_file_location("pmxt_wave_minus1_quality_calibration", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calibrate = _load_module()


def _event(
    slug: str,
    *,
    date: str = "2026-06-04",
    city: str = "Amsterdam",
    rows: int = 10,
    cohort_role: str = "primary_development_replication",
    missing: int = 0,
    corrupt: int = 0,
) -> dict[str, Any]:
    return {
        "event_slug": slug,
        "event_date": date,
        "city": city,
        "cohort_role": cohort_role,
        "event_start": f"{date}T00:00:00Z",
        "event_end": f"{date}T12:00:00Z",
        "rows_written": rows,
        "markets": 1,
        "tokens": 2,
        "contract_version": "contract-a",
        "schema_fingerprint": "schema-a",
        "event_local_missing_hour_count": missing,
        "event_local_corrupt_hour_count": corrupt,
        "orderbook_file": {"size_bytes": rows * 100, "path": "missing.parquet"},
    }


def _raw_inputs() -> dict[str, Any]:
    events = [
        _event("a", rows=30, city="A"),
        _event("b", rows=20, city="B"),
        _event("c", date="2026-06-06", city="C", rows=10, cohort_role="degraded_robustness", missing=1),
    ]
    return {
        "summary": {
            "event_count": 3,
            "rows_written_total": 60,
            "schema_fingerprints": ["schema-a"],
            "contract_versions": ["contract-a"],
        },
        "event_rows": events,
        "token_rows": [
            {
                "event_slug": "a",
                "event_date": "2026-06-04",
                "city": "A",
                "condition_id": "condition-a",
                "market_index": 0,
                "token_side": "yes",
                "asset_id": "asset-a",
            },
        ],
        "synthetic_benchmark_metrics": calibrate.frozen_synthetic_benchmark_metrics(),
        "timestamp_samples": [],
    }


def _surface_outputs(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    decisions = calibrate.make_decisions(calibrate.sanitize(raw), ambiguity_token_budget=16)
    return {
        name: calibrate.scrub_audit(decisions[name])
        for name in [
            "quality_thresholds",
            "event_strata",
            "representative_sample",
            "worker_resource_budgets",
            "label_match_rules",
            "materialization_decision_function",
        ]
    }


def test_path_level_recursive_sanitizer_detects_aliases_and_removes_nested_poison() -> None:
    raw = _raw_inputs()
    raw["event_rows"][0].update(
        {
            "outcomePrices": [0.1, 0.9],
            "outcomes": ["yes"],
            "umaResolutionStatus": "resolved",
            "UmaResolutionStatuses": ["resolved"],
            "resolved": True,
            "winner": "yes",
            "winners": ["yes"],
            "finalPrice": 1,
            "Final_Prices": [1],
            "final_prices": [1],
            "PnLs": [99],
            "cash_balance": 10,
            "cashBalances": [10],
            "realized_pnl": 2,
            "unrealized_pnl": 3,
            "profit": 4,
            "profitLoss": 5,
            "future_returns": [0.1],
            "settlements": ["yes"],
            "settlement_status": "done",
            "resolutions": ["yes"],
            "hit_rate": 1,
            "hit_rates": [1],
            "ic": 0.5,
            "sharpe": 10,
            "sharpes": [10],
            "signals": [1],
            "strategyDecision": {"buy": True},
            "strategy_decisions": {"buy": True},
            "strategy_results": {"pnl": 1},
            "labels": ["up"],
            "predictions": ["up"],
            "drawdowns": [0.1],
            "nested": {"fills": [], "fees": 1, "positions": 2},
        },
    )

    sanitized = calibrate.sanitize(raw)

    prohibited = set(sanitized["prohibited_paths_detected"])
    assert "$.event_rows.0.outcomePrices" in prohibited
    assert "$.event_rows.0.outcomes" in prohibited
    assert "$.event_rows.0.umaResolutionStatus" in prohibited
    assert "$.event_rows.0.UmaResolutionStatuses" in prohibited
    assert "$.event_rows.0.resolved" in prohibited
    assert "$.event_rows.0.winner" in prohibited
    assert "$.event_rows.0.winners" in prohibited
    assert "$.event_rows.0.finalPrice" in prohibited
    assert "$.event_rows.0.Final_Prices" in prohibited
    assert "$.event_rows.0.final_prices" in prohibited
    assert "$.event_rows.0.PnLs" in prohibited
    assert "$.event_rows.0.cash_balance" in prohibited
    assert "$.event_rows.0.cashBalances" in prohibited
    assert "$.event_rows.0.realized_pnl" in prohibited
    assert "$.event_rows.0.unrealized_pnl" in prohibited
    assert "$.event_rows.0.profit" in prohibited
    assert "$.event_rows.0.profitLoss" in prohibited
    assert "$.event_rows.0.future_returns" in prohibited
    assert "$.event_rows.0.settlements" in prohibited
    assert "$.event_rows.0.settlement_status" in prohibited
    assert "$.event_rows.0.resolutions" in prohibited
    assert "$.event_rows.0.hit_rate" in prohibited
    assert "$.event_rows.0.hit_rates" in prohibited
    assert "$.event_rows.0.ic" in prohibited
    assert "$.event_rows.0.sharpe" in prohibited
    assert "$.event_rows.0.sharpes" in prohibited
    assert "$.event_rows.0.signals" in prohibited
    assert "$.event_rows.0.strategyDecision" in prohibited
    assert "$.event_rows.0.strategy_decisions" in prohibited
    assert "$.event_rows.0.strategy_results" in prohibited
    assert "$.event_rows.0.labels" in prohibited
    assert "$.event_rows.0.predictions" in prohibited
    assert "$.event_rows.0.drawdowns" in prohibited
    assert "$.event_rows.0.nested.fills" in prohibited
    assert "$.event_rows.0.nested.fees" in prohibited
    assert "$.event_rows.0.nested.positions" in prohibited
    assert "outcomePrices" not in json.dumps(sanitized["data"])


def test_legal_provenance_paths_date_and_condition_id_are_preserved() -> None:
    raw = _raw_inputs()
    raw["event_rows"][0].update(
        {
            "date_text": "June 4",
            "hard_break_provenance": {
                "has_hard_break": False,
                "missing_hour_breaks": [],
                "corrupt_hour_breaks": [],
                "action": "external_intersection_only",
            },
            "natural_staleness_policy": "age_only",
            "paths": {
                "event_dir": "event-dir",
                "event_dir_relative_to_root": "relative-event-dir",
                "event_index": "event-index.json",
                "gamma_raw": "gamma.raw.json",
                "manifest": "manifest.json",
                "orderbook": "orderbook.parquet",
            },
        },
    )

    sanitized = calibrate.sanitize(raw)
    event = sanitized["data"]["event_rows"][0]

    assert event["date_text"] == "June 4"
    assert event["hard_break_provenance"]["action"] == "external_intersection_only"
    assert event["natural_staleness_policy"] == "age_only"
    assert event["paths"]["event_dir_relative_to_root"] == "relative-event-dir"
    assert sanitized["data"]["token_rows"][0]["condition_id"] == "condition-a"
    assert "$.event_rows.0.hard_break_provenance.action" in sanitized["allowed_input_paths"]
    assert "$.token_rows.0.condition_id" in sanitized["allowed_input_paths"]


def test_bounded_o1_ambiguity_exact_counts_and_reads_each_event_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_path = tmp_path / "event.parquet"
    pd.DataFrame(
        [
            {"market": "condition-a", "asset_id": "asset-a", "timestamp": None, "timestamp_received": "r1", "event_type": "book"},
            {"market": "condition-a", "asset_id": "asset-a", "timestamp": None, "timestamp_received": "r1", "event_type": "price_change"},
            {"market": "condition-a", "asset_id": "asset-a", "timestamp": "t2", "timestamp_received": "r2", "event_type": "book"},
            {"market": "condition-a", "asset_id": "asset-a", "timestamp": "t2", "timestamp_received": "r2", "event_type": "book"},
            {"market": "condition-a", "asset_id": "asset-b", "timestamp": "t3", "timestamp_received": "r3", "event_type": "book"},
        ],
    ).to_parquet(event_path, index=False)
    event = _event("ambiguous", rows=5)
    event["orderbook_file"]["path"] = str(event_path)
    event["hashes"] = {"orderbook_identity_sha256": "synthetic-source-hash"}
    tokens = [
        {"event_slug": "ambiguous", "condition_id": "condition-a", "asset_id": "asset-a", "market_index": 0, "token_side": "yes"},
        {"event_slug": "ambiguous", "condition_id": "condition-a", "asset_id": "asset-b", "market_index": 0, "token_side": "no"},
    ]
    real_read_parquet = calibrate.pd.read_parquet
    read_calls: list[Path] = []

    def counted_read_parquet(path: Path, **kwargs: Any):
        read_calls.append(Path(path))
        return real_read_parquet(path, **kwargs)

    monkeypatch.setattr(calibrate.pd, "read_parquet", counted_read_parquet)
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    result = calibrate.ambiguity_candidate([event], tokens, 2, ambiguity_scan_cache=cache)
    repeated = calibrate.ambiguity_candidate([event], copy.deepcopy(tokens), 2, ambiguity_scan_cache=cache)

    assert read_calls == [event_path]
    assert result["candidate_budget_tokens"] == 2
    assert result["scanned_events"] == 1
    assert result["scanned_tokens"] == 2
    assert result["orderingAmbiguousGroups"] == 1
    assert result["orderingAmbiguousRows"] == 2
    assert result["tiedTimestampGroups"] == 2
    assert result["tiedTimestampRows"] == 4
    assert result["examples"][0]["source_row_index_examples"] == [0, 1]
    assert result["stableSortKey"] == "ambiguous|condition-a|asset-a"
    assert result["source_event_path"] == str(event_path)
    assert result["source_orderbook_identity_hash"] == "synthetic-source-hash"
    assert result["event_scan_summaries"][0]["physical_event_parquet_reads"] == 1
    assert repeated["event_scan_summaries"][0]["physical_event_parquet_reads"] == 1
    assert repeated["event_scan_summaries"][0]["read_reused_from_calibration_cache"] is True
    assert "market_label" not in json.dumps(result)


def test_each_decision_surface_is_invariant_to_poisoned_and_removed_inputs() -> None:
    raw = _raw_inputs()
    baseline = _surface_outputs(raw)
    poisoned_raw = calibrate.poison(json.loads(json.dumps(raw)))
    poisoned = _surface_outputs(poisoned_raw)
    removed = _surface_outputs(calibrate.remove_prohibited(json.loads(json.dumps(poisoned_raw))))

    assert baseline == poisoned == removed
    report = calibrate.invariance_report(raw, ambiguity_token_budget=16)
    assert report["all_surfaces_equivalent_except_audit"] is True
    assert report["prohibited_paths_detected"]


@pytest.mark.parametrize(
    ("surface", "mutate"),
    [
        ("quality_thresholds", lambda raw: raw["summary"].update({"schema_fingerprints": ["a", "b"]})),
        ("event_strata", lambda raw: raw["event_rows"][1].update({"event_date": "2026-06-05"})),
        ("representative_sample", lambda raw: raw["event_rows"][1].update({"rows_written": 99})),
        ("worker_resource_budgets", lambda raw: raw["summary"].update({"event_count": 200})),
        (
            "label_match_rules",
            lambda raw: raw.update(
                {
                    "timestamp_samples": [
                        {"event_slug": "a", "censor_category": "future_observation_available", "matching_slippage_seconds": 1},
                    ],
                },
            ),
        ),
        (
            "materialization_decision_function",
            lambda raw: raw["synthetic_benchmark_metrics"].update({"rows_written_total_from_manifest": 0}),
        ),
    ],
)
def test_legal_input_mutation_changes_corresponding_surface(surface: str, mutate: Any) -> None:
    raw = _raw_inputs()
    baseline = _surface_outputs(raw)[surface]
    mutate(raw)
    changed = _surface_outputs(raw)[surface]

    assert changed != baseline


def test_current_panel_disallows_test_holdout_confirmatory_future_confirmed_roles() -> None:
    for bad_role in ["test", "holdout", "confirmatory", "future_confirmed"]:
        with pytest.raises(ValueError):
            calibrate.assert_current_panel_roles([_event("bad", cohort_role=bad_role)])


def test_build_calibration_writes_audited_artifacts_from_synthetic_inventory(tmp_path: Path) -> None:
    inputs = _raw_inputs()
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "inventory_summary.json").write_text(json.dumps(inputs["summary"]), encoding="utf-8")
    pd.DataFrame(inputs["event_rows"]).to_parquet(input_dir / "event_inventory.parquet", index=False)
    pd.DataFrame(inputs["token_rows"]).to_parquet(input_dir / "token_inventory.parquet", index=False)

    result = calibrate.build_calibration(
        input_dir=input_dir,
        output_dir=tmp_path / "outputs",
        timestamp_sample_events=0,
        ambiguity_token_budget=16,
    )

    for name in [
        "quality_calibration.json",
        "representative_sample.json",
        "outcome_blind_invariance.json",
        "protocol-candidate-g003.json",
    ]:
        assert (tmp_path / "outputs" / name).exists()
    assert result["quality_calibration"]["performance_claims_allowed"] is False
    assert result["quality_calibration"]["current_panel_confirmation_status"] == "not_confirmatory"
    assert result["protocol_candidate"]["materialization_decision_function"]["decision"] == "deferred"
    assert result["quality_calibration"]["allowed_input_paths"]
    assert "decision_output_hash" in result["quality_calibration"]
    for artifact in result.values():
        assert "allowed_input_paths" in artifact
        assert "prohibited_paths_detected" in artifact
        assert "sanitized_input_hash" in artifact
        assert "decision_output_hash" in artifact
        assert artifact["protocol_version"] == calibrate.PROTOCOL_VERSION


@pytest.mark.skipif(not REAL_INPUT_DIR.exists(), reason="real G001 inventory artifacts unavailable")
def test_real_441_inventory_quality_calibration_smoke(tmp_path: Path) -> None:
    result = calibrate.build_calibration(
        input_dir=REAL_INPUT_DIR,
        output_dir=tmp_path / "out",
        timestamp_sample_events=0,
        ambiguity_token_budget=16,
        ambiguity_deferred_reason="real_budget_16_scan_deferred_after_exceeding_180_second_runtime_cap",
    )

    assert result["quality_calibration"]["scope"]["event_inventory_rows"] == 441
    assert result["quality_calibration"]["scope"]["raw_747M_scan_claimed_complete"] is False
    assert result["outcome_blind_invariance"]["all_surfaces_equivalent_except_audit"] is True
    assert result["protocol_candidate"]["materialization_decision_function"]["decision"] == "deferred"
    ambiguity_role = next(
        item
        for item in result["representative_sample"]["selections"]
        if item["role"] == "ambiguity_heavy_within_bounded_o1_scan"
    )
    evidence = ambiguity_role["selected_token"]
    assert evidence["candidate_budget_tokens"] == 16
    assert evidence["candidate_tokens_considered"] == 16
    assert ambiguity_role["status"] == "deferred"
    assert evidence["status"] == "deferred"
    assert evidence["scanned_events"] == 0
    assert evidence["scanned_tokens"] == 0
    assert evidence["candidate_boundary"] == "bounded_deterministic_budget_not_global_heaviest"
    assert evidence["reason"] == "real_budget_16_scan_deferred_after_exceeding_180_second_runtime_cap"
    assert evidence["external_real_ordering_evidence"]["artifact_sha256"]
    assert evidence["external_real_ordering_evidence"]["use"] == "reference_only_no_G003_metric_import"
    assert "market_label" not in json.dumps(evidence)
