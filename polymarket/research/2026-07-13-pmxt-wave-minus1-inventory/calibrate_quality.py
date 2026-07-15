"""
Outcome-blind Wave -1 systems calibration for PMXT inventory artifacts.

Consumes G001 inventory artifacts and bounded timestamp-only scans.  It never
uses outcomes, factor values/effects, future return direction/magnitude,
settlement, strategy decisions, fills, fees, positions, cash, or PnL.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any


try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore[assignment]


PROGRAM = "pmxt-wave-minus1-quality-calibration"
VERSION = "2026-07-13.g003.v1"
PROTOCOL_VERSION = "wave-minus1-g003-outcome-blind-systems-calibration-v1"
MAX_AMBIGUITY_TOKEN_BUDGET = 64
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = BASE_DIR / "outputs"
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs"
G002_REAL_ORDERING_ARTIFACT = BASE_DIR.parent / "2026-07-13-pmxt-wave-minus1-ordering-parity/outputs/real_curated_smoke_parity.json"
CURRENT_PANEL_STATUS = "not_confirmatory"
CURRENT_DATES = {
    "2026-06-04",
    "2026-06-05",
    "2026-06-06",
    "2026-06-07",
    "2026-06-08",
    "2026-06-09",
    "2026-06-10",
    "2026-06-11",
    "2026-06-12",
}
DISALLOWED_CURRENT_DATE_ROLES = {
    "test",
    "holdout",
    "confirmatory",
    "future_confirmed",
    "futureconfirmed",
}

PROHIBITED_ALIASES = {
    "cash",
    "cash_balance",
    "cash_balances",
    "cashbalance",
    "cashbalances",
    "decision",
    "decisions",
    "direction",
    "drawdown",
    "drawdowns",
    "fee",
    "fees",
    "fill",
    "fills",
    "final_price",
    "final_prices",
    "finalprice",
    "finalprices",
    "future_return",
    "future_returns",
    "futurereturn",
    "futurereturns",
    "hit_rate",
    "hit_rates",
    "hitrate",
    "hitrates",
    "ic",
    "label",
    "labels",
    "magnitude",
    "outcome",
    "outcomes",
    "outcome_price",
    "outcome_prices",
    "outcomeprice",
    "outcomeprices",
    "p_l",
    "pl",
    "pnl",
    "pnls",
    "position",
    "positions",
    "prediction",
    "predictions",
    "profit",
    "profit_loss",
    "profitloss",
    "profitandloss",
    "realized_pnl",
    "realizedpnl",
    "resolved",
    "resolution",
    "resolutions",
    "result",
    "return",
    "returns",
    "settled",
    "settlement",
    "settlements",
    "settlement_status",
    "settlementstatus",
    "sharpe",
    "sharpes",
    "signal",
    "signals",
    "strategy",
    "strategy_decision",
    "strategy_decisions",
    "strategy_result",
    "strategy_results",
    "strategydecision",
    "strategydecisions",
    "strategyresults",
    "uma_resolution_status",
    "uma_resolution_statuses",
    "umaresolutionstatus",
    "umaresolutionstatuses",
    "unrealized_pnl",
    "unrealizedpnl",
    "winner",
    "winners",
}

ALLOW_ROOTS = {
    "event_rows",
    "summary",
    "synthetic_benchmark_metrics",
    "timestamp_samples",
    "token_rows",
}

ALLOWED_KEYS = ALLOW_ROOTS | {
    "action",
    "asset_id",
    "bad_files_count",
    "city",
    "cohort_role",
    "corrupt_hour_breaks",
    "contract_version",
    "contract_versions",
    "current_confirmatory_label",
    "date_text",
    "condition_id",
    "event_date",
    "event_count",
    "event_end",
    "event_dir",
    "event_dir_relative_to_root",
    "event_identity_sha256",
    "event_id",
    "event_local_corrupt_hour_count",
    "event_local_corrupt_hours",
    "event_local_missing_hour_count",
    "event_local_missing_hours",
    "event_slug",
    "event_start",
    "event_title",
    "event_type_counts",
    "event_window_semantics",
    "event_index",
    "event_index_sha256",
    "gamma_raw",
    "gamma_raw_sha256",
    "hard_break_provenance",
    "has_hard_break",
    "hashes",
    "manifest",
    "manifest_sha256",
    "market_index",
    "markets",
    "missing_hour_breaks",
    "mtime_ns",
    "natural_staleness_policy",
    "max_parallel_workers",
    "nonintersecting_corrupt_hours",
    "nonintersecting_missing_hours",
    "orderbook_file",
    "orderbook",
    "orderbook_identity_sha256",
    "ordering_domain",
    "path",
    "paths",
    "performance_claims_allowed",
    "program",
    "program_version",
    "raw_corrupt_hours",
    "raw_missing_hours",
    "rows_written",
    "rows_written_total",
    "rows_written_total_from_manifest",
    "schema_contract_available",
    "schema_fingerprint",
    "schema_fingerprints",
    "schema_text_sha256",
    "size_bytes",
    "source_files_scanned_count",
    "source_files_used_count",
    "summary",
    "timestamp",
    "timestamp_column_available",
    "timestamp_received",
    "timestamp_samples",
    "token_rows",
    "token_side",
    "tokens",
    "0",
    "1",
    "2",
    "3",
    "4",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timestamp-sample-events", type=int, default=5)
    parser.add_argument("--ambiguity-token-budget", type=int, default=MAX_AMBIGUITY_TOKEN_BUDGET)
    parser.add_argument("--defer-ambiguity-scan-reason")
    args = parser.parse_args(argv)
    result = build_calibration(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        timestamp_sample_events=args.timestamp_sample_events,
        ambiguity_token_budget=args.ambiguity_token_budget,
        ambiguity_deferred_reason=args.defer_ambiguity_scan_reason,
    )
    print(json.dumps(result["quality_calibration"], indent=2, sort_keys=True))
    return 0


def build_calibration(
    *,
    input_dir: Path = DEFAULT_INPUT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    timestamp_sample_events: int = 5,
    ambiguity_token_budget: int = MAX_AMBIGUITY_TOKEN_BUDGET,
    ambiguity_deferred_reason: str | None = None,
) -> dict[str, Any]:
    validate_ambiguity_budget(ambiguity_token_budget)
    summary = read_json(input_dir / "inventory_summary.json")
    event_rows = read_table(input_dir / "event_inventory.parquet")
    token_rows = read_table(input_dir / "token_inventory.parquet")
    assert_current_panel_roles(event_rows)

    raw = {
        "summary": summary,
        "event_rows": event_rows,
        "token_rows": token_rows,
        "synthetic_benchmark_metrics": frozen_synthetic_benchmark_metrics(),
    }
    ambiguity_scan_cache: dict[tuple[str, str], dict[str, Any]] = {}
    sanitized = sanitize(raw)
    decisions = make_decisions(
        sanitized,
        ambiguity_token_budget=ambiguity_token_budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )
    timestamp_samples = bounded_timestamp_samples(
        decisions["representative_sample"],
        event_rows,
        timestamp_sample_events=timestamp_sample_events,
    )
    sanitized = sanitize({**raw, "timestamp_samples": timestamp_samples})
    decisions = make_decisions(
        sanitized,
        ambiguity_token_budget=ambiguity_token_budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )
    invariance = invariance_report(
        raw,
        ambiguity_token_budget=ambiguity_token_budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )

    quality = quality_artifact(summary, event_rows, decisions, timestamp_sample_events)
    sample = decisions["representative_sample"]
    protocol = protocol_candidate(summary, decisions)

    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "quality_calibration.json", quality)
    atomic_write_json(output_dir / "representative_sample.json", sample)
    atomic_write_json(output_dir / "outcome_blind_invariance.json", invariance)
    atomic_write_json(output_dir / "protocol-candidate-g003.json", protocol)
    return {
        "quality_calibration": quality,
        "representative_sample": sample,
        "outcome_blind_invariance": invariance,
        "protocol_candidate": protocol,
    }


def sanitize(value: Any) -> dict[str, Any]:
    sanitized, allowed, prohibited = _sanitize(value, path=())
    if not isinstance(sanitized, dict):
        sanitized = {}
    return {
        "data": sanitized,
        "allowed_input_paths": sorted(allowed),
        "prohibited_paths_detected": sorted(prohibited),
        "sanitized_input_hash": stable_hash(sanitized),
    }


def _sanitize(value: Any, *, path: tuple[str, ...]) -> tuple[Any, set[str], set[str]]:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        allowed: set[str] = set()
        prohibited: set[str] = set()
        for key, item in value.items():
            key_s = str(key)
            child_path = (*path, key_s)
            if is_prohibited_key(key_s):
                prohibited.add(path_text(child_path))
                continue
            if not path and key_s not in ALLOW_ROOTS:
                continue
            if path and not key_s.isdigit() and key_s not in ALLOWED_KEYS:
                prohibited |= collect_prohibited_paths(item, path=child_path)
                continue
            child, child_allowed, child_prohibited = _sanitize(item, path=child_path)
            out[key_s] = child
            allowed |= child_allowed
            prohibited |= child_prohibited
            allowed.add(path_text(child_path))
        return out, allowed, prohibited
    if isinstance(value, list):
        out_list = []
        allowed = set()
        prohibited = set()
        for idx, item in enumerate(value):
            child, child_allowed, child_prohibited = _sanitize(item, path=(*path, str(idx)))
            out_list.append(child)
            allowed |= child_allowed
            prohibited |= child_prohibited
        return out_list, allowed, prohibited
    return normalize(value), {path_text(path)} if path else set(), set()


def collect_prohibited_paths(value: Any, *, path: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_s = str(key)
            child_path = (*path, key_s)
            if is_prohibited_key(key_s):
                found.add(path_text(child_path))
            found |= collect_prohibited_paths(item, path=child_path)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found |= collect_prohibited_paths(item, path=(*path, str(idx)))
    return found


def is_prohibited_key(key: str) -> bool:
    normalized = "".join(ch for ch in key.casefold() if ch.isalnum() or ch == "_")
    compact = normalized.replace("_", "")
    return normalized in PROHIBITED_ALIASES or compact in PROHIBITED_ALIASES


def make_decisions(
    sanitized: dict[str, Any],
    *,
    ambiguity_token_budget: int,
    ambiguity_scan_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ambiguity_deferred_reason: str | None = None,
) -> dict[str, Any]:
    data = sanitized["data"]
    events = list(data.get("event_rows") or [])
    tokens = list(data.get("token_rows") or [])
    summary = dict(data.get("summary") or {})
    timestamps = list(data.get("timestamp_samples") or [])
    metrics = dict(data.get("synthetic_benchmark_metrics") or {})

    surfaces = {
        "quality_thresholds": quality_thresholds(summary, events),
        "event_strata": event_strata(events),
        "representative_sample": representative_sample(
            events,
            tokens,
            ambiguity_token_budget,
            ambiguity_scan_cache=ambiguity_scan_cache,
            ambiguity_deferred_reason=ambiguity_deferred_reason,
        ),
        "worker_resource_budgets": worker_resource_budgets(summary, events),
        "label_match_rules": label_match_rules(timestamps),
        "materialization_decision_function": materialization_decision(metrics),
    }
    return {
        name: with_surface_audit(name, payload, sanitized)
        for name, payload in surfaces.items()
    } | {"decision_output_hash": stable_hash(surfaces)}


def with_surface_audit(name: str, payload: dict[str, Any], sanitized: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(payload)
    payload.update(
        {
            "protocol_version": PROTOCOL_VERSION,
            "allowed_input_paths": compact_paths(sanitized["allowed_input_paths"]),
            "prohibited_paths_detected": compact_paths(sanitized["prohibited_paths_detected"]),
            "sanitized_input_hash": sanitized["sanitized_input_hash"],
            "decision_surface": name,
            "decision_output_hash": stable_hash(payload),
        },
    )
    return payload


def quality_thresholds(summary: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scope": "inventory_artifacts_only",
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "min_events_for_current_panel": int(summary.get("event_count") or len(events)),
        "require_single_schema_fingerprint": len(summary.get("schema_fingerprints") or []) == 1,
        "require_single_contract_version": len(summary.get("contract_versions") or []) == 1,
        "hard_break_rule": "externally_evidenced_intersecting_missing_or_corrupt_only",
        "hard_break_event_count": sum(has_hard_break(row) for row in events),
        "natural_staleness_action": "record_age_only_no_reset_no_censor_without_external_intersection",
        "deferred_metrics": [
            "full_747M_timestamp_distribution_not_scanned_by_G003",
            "natural_staleness_thresholds_deferred_outcome_blind_policy_only",
        ],
    }


def event_strata(events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(int(row.get("rows_written") or 0) for row in events)
    return {
        "scope": "event_inventory_rows",
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "cohort_role_counts": dict(sorted(Counter(str(row.get("cohort_role")) for row in events).items())),
        "date_counts": dict(sorted(Counter(str(row.get("event_date")) for row in events).items())),
        "hard_break_counts": {
            "events_with_intersecting_missing": sum(int(row.get("event_local_missing_hour_count") or 0) > 0 for row in events),
            "events_with_intersecting_corrupt": sum(int(row.get("event_local_corrupt_hour_count") or 0) > 0 for row in events),
            "events_with_any_hard_break": sum(has_hard_break(row) for row in events),
        },
        "rows_written": {
            "min": rows[0] if rows else 0,
            "median_lower": rows[(len(rows) - 1) // 2] if rows else 0,
            "max": rows[-1] if rows else 0,
            "total_from_inventory": sum(rows),
        },
    }


def representative_sample(
    events: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    budget: int,
    *,
    ambiguity_scan_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ambiguity_deferred_reason: str | None = None,
) -> dict[str, Any]:
    selections: list[dict[str, Any]] = []
    role_map: dict[str, list[str]] = {}

    def add(role: str, row: dict[str, Any] | None, reason: str, fields: list[str]) -> None:
        if row is None:
            selections.append({"role": role, "status": "deferred", "reason": reason, "input_fields": fields})
            return
        slug = str(row.get("event_slug"))
        role_map.setdefault(slug, []).append(role)
        selections.append(
            {
                "role": role,
                "status": "selected",
                "event_slug": slug,
                "event_date": row.get("event_date"),
                "city": row.get("city"),
                "event_end": row.get("event_end"),
                "reason": reason,
                "input_fields": fields,
                "deterministic_tie_break": "metric order then event_date, city, event_slug; never filesystem order",
            },
        )

    add(
        "largest_event",
        first_sorted(events, key=lambda row: (-int(row.get("rows_written") or 0), *sort_key(row))),
        "largest rows_written from G001 manifest inventory",
        ["rows_written", "event_date", "city", "event_slug"],
    )
    add(
        "median_volume_event",
        median_volume(events),
        "lower median rows_written from sorted G001 manifest inventory",
        ["rows_written", "event_date", "city", "event_slug"],
    )
    add(
        "degraded_event_local_missing_case",
        first_sorted(
            [row for row in events if row.get("cohort_role") == "degraded_robustness" and has_hard_break(row)],
            key=sort_key,
        ),
        "first degraded event with externally evidenced intersecting missing/corrupt hours",
        ["cohort_role", "event_local_missing_hour_count", "event_local_corrupt_hour_count", "event_date", "city", "event_slug"],
    )
    ambiguity = ambiguity_candidate(
        events,
        tokens,
        budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )
    add(
        "ambiguity_heavy_within_bounded_o1_scan",
        next((row for row in events if row.get("event_slug") == ambiguity.get("event_slug")), None),
        "bounded deterministic O1 ordering ambiguity discovery from timestamp/update-type parquet scan",
        ["condition_id", "asset_id", "event_slug", "orderbook_file.path", "hashes"],
    )
    selections[-1]["selected_token"] = ambiguity
    complete = complete_date(events)
    if complete:
        selections.append(
            {
                "role": "one_complete_date",
                "status": "selected",
                "event_date": complete["event_date"],
                "event_count": complete["event_count"],
                "event_slugs": complete["event_slugs"],
                "reason": "earliest full-count date with zero intersecting missing/corrupt hours",
                "input_fields": ["event_date", "city", "event_local_missing_hour_count", "event_local_corrupt_hour_count"],
                "deterministic_tie_break": "event_date asc",
            },
        )
    else:
        selections.append({"role": "one_complete_date", "status": "deferred", "reason": "no complete clean date", "input_fields": []})

    return {
        "program": PROGRAM,
        "program_version": VERSION,
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "selection_rule_status": "frozen_outcome_blind",
        "selections": selections,
        "role_overlaps": {slug: roles for slug, roles in sorted(role_map.items()) if len(roles) > 1},
        "ambiguity_discovery_scope": {
            "source": "bounded token_inventory candidates plus event parquet timestamp/update-type columns",
            "candidate_budget_tokens": budget,
            "candidate_budget_hard_max_tokens": MAX_AMBIGUITY_TOKEN_BUDGET,
            "event_read_policy": "each candidate event parquet read once",
            "full_inventory": "deferred_not_global_heaviest",
        },
    }


def worker_resource_budgets(summary: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    total_size = sum(int((row.get("orderbook_file") or {}).get("size_bytes") or 0) for row in events if isinstance(row.get("orderbook_file"), dict))
    count = int(summary.get("event_count") or len(events))
    return {
        "scope": "resource_measurements_only",
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "event_count": count,
        "rows_written_total_from_manifest": int(summary.get("rows_written_total") or 0),
        "orderbook_size_bytes_total_from_metadata": total_size,
        "recommended_max_parallel_workers": max(1, min(8, math.ceil(count / 64))) if count else 1,
        "full_raw_scan_policy": "deferred_not_claimed_by_G003",
    }


def label_match_rules(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scope": "bounded_representative_timestamp_only_sample" if samples else "rule_only_no_raw_timestamp_scan",
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "target_time": "event_end",
        "valid_observation": "future timestamp_received/timestamp exists after target; no price/return sign/value read",
        "censor_categories": [
            "future_observation_available",
            "no_future_timestamp_in_bounded_scan",
            "timestamp_scan_unavailable_or_deferred",
        ],
        "sample_count": len(samples),
        "future_observation_available_count": sum(item.get("censor_category") == "future_observation_available" for item in samples),
        "full_panel_label_match_distribution": "deferred",
    }


def materialization_decision(metrics: dict[str, Any]) -> dict[str, Any]:
    rows = int(metrics.get("rows_written_total_from_manifest") or 0)
    schema = bool(metrics.get("schema_contract_available"))
    workers = int(metrics.get("max_parallel_workers") or 0)
    feasible = rows > 0 and schema and workers > 0
    return {
        "scope": "frozen_synthetic_benchmark_metrics_only",
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "decision": "deferred",
        "reason": "materialization evidence intentionally insufficient for G003; actual decision belongs to G004",
        "synthetic_feasibility_check": feasible,
        "input_metric_names": sorted(metrics),
    }


def bounded_timestamp_samples(sample: dict[str, Any], events: list[dict[str, Any]], *, timestamp_sample_events: int) -> list[dict[str, Any]]:
    if pd is None or timestamp_sample_events <= 0:
        return []
    by_slug = {row.get("event_slug"): row for row in events}
    out = []
    slugs: list[str] = []
    for selection in sample.get("selections", []):
        slug = selection.get("event_slug")
        if slug and slug not in slugs:
            slugs.append(slug)
    for slug in slugs[:timestamp_sample_events]:
        row = by_slug.get(slug) or {}
        path = Path((row.get("orderbook_file") or {}).get("path", ""))
        out.append(timestamp_probe(str(slug), str(row.get("event_end") or ""), path))
    return out


def timestamp_probe(slug: str, event_end: str, path: Path) -> dict[str, Any]:
    base = {"event_slug": slug, "columns_requested": ["timestamp_received", "timestamp"], "scope": "timestamp_columns_only"}
    if not event_end or not path.exists() or pd is None:
        return {**base, "censor_category": "timestamp_scan_unavailable_or_deferred"}
    try:
        frame = pd.read_parquet(path, columns=["timestamp_received", "timestamp"])
    except Exception as exc:  # pragma: no cover
        return {**base, "censor_category": "timestamp_scan_unavailable_or_deferred", "error_type": type(exc).__name__}
    target = parse_utc(event_end)
    future = []
    for col in ("timestamp_received", "timestamp"):
        series = pd.to_datetime(frame[col], utc=True, errors="coerce")
        valid = series[series > target]
        if not valid.empty:
            future.append(valid.min().to_pydatetime())
    if not future:
        return {**base, "target_time": event_end, "censor_category": "no_future_timestamp_in_bounded_scan"}
    match = min(future)
    return {
        **base,
        "target_time": event_end,
        "censor_category": "future_observation_available",
        "matching_slippage_seconds": int((match - target).total_seconds()),
        "matched_timestamp": match.isoformat().replace("+00:00", "Z"),
    }


def invariance_report(
    raw: dict[str, Any],
    *,
    ambiguity_token_budget: int,
    ambiguity_scan_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ambiguity_deferred_reason: str | None = None,
) -> dict[str, Any]:
    base = make_decisions(
        sanitize(raw),
        ambiguity_token_budget=ambiguity_token_budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )
    poisoned_raw = poison(copy.deepcopy(raw))
    poisoned = make_decisions(
        sanitize(poisoned_raw),
        ambiguity_token_budget=ambiguity_token_budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )
    removed = make_decisions(
        sanitize(remove_prohibited(copy.deepcopy(poisoned_raw))),
        ambiguity_token_budget=ambiguity_token_budget,
        ambiguity_scan_cache=ambiguity_scan_cache,
        ambiguity_deferred_reason=ambiguity_deferred_reason,
    )
    surfaces = [
        "quality_thresholds",
        "event_strata",
        "representative_sample",
        "worker_resource_budgets",
        "label_match_rules",
        "materialization_decision_function",
    ]
    comparable = {
        surface: scrub_audit(base[surface]) == scrub_audit(poisoned[surface]) == scrub_audit(removed[surface])
        for surface in surfaces
    }
    return {
        "program": PROGRAM,
        "program_version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "comparison": "baseline_vs_poisoned_vs_removed",
        "surface_equivalence_except_audit": comparable,
        "all_surfaces_equivalent_except_audit": all(comparable.values()),
        "baseline_decision_output_hash": base["decision_output_hash"],
        "poisoned_decision_output_hash": poisoned["decision_output_hash"],
        "removed_decision_output_hash": removed["decision_output_hash"],
        "prohibited_paths_detected": compact_paths(sanitize(poisoned_raw)["prohibited_paths_detected"]),
        "allowed_input_paths": compact_paths(sanitize(raw)["allowed_input_paths"]),
        "sanitized_input_hash": sanitize(raw)["sanitized_input_hash"],
        "decision_output_hash": stable_hash({surface: scrub_audit(base[surface]) for surface in surfaces}),
    }


def scrub_audit(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "allowed_input_paths",
            "prohibited_paths_detected",
            "sanitized_input_hash",
            "decision_output_hash",
        }
    }


def quality_artifact(summary: dict[str, Any], events: list[dict[str, Any]], decisions: dict[str, Any], timestamp_budget: int) -> dict[str, Any]:
    return {
        "program": PROGRAM,
        "program_version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "source_program": summary.get("program"),
        "status": "calibrated_outcome_blind_systems_only",
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "quality_thresholds": decisions["quality_thresholds"],
        "event_strata": decisions["event_strata"],
        "worker_resource_budgets": decisions["worker_resource_budgets"],
        "label_match_rules": decisions["label_match_rules"],
        "hard_break_rule": "externally evidenced intersecting missing/corrupt source hours only",
        "natural_staleness_rule": "natural no-update age is not automatic reset/censor",
        "scope": {
            "event_inventory_rows": len(events),
            "rows_written_total_from_manifest": summary.get("rows_written_total"),
            "raw_747M_scan_claimed_complete": False,
            "bounded_timestamp_sample_event_budget": timestamp_budget,
        },
        "deferred_metrics": decisions["quality_thresholds"]["deferred_metrics"],
        "allowed_input_paths": decisions["quality_thresholds"]["allowed_input_paths"],
        "prohibited_paths_detected": decisions["quality_thresholds"]["prohibited_paths_detected"],
        "sanitized_input_hash": decisions["quality_thresholds"]["sanitized_input_hash"],
        "decision_output_hash": stable_hash(decisions),
    }


def protocol_candidate(summary: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "program": PROGRAM,
        "program_version": VERSION,
        "source_program": summary.get("program"),
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": CURRENT_PANEL_STATUS,
        "selector_status": "frozen_without_M1_M2_M3_choice",
        "materialization_decision_function": decisions["materialization_decision_function"],
        "representative_sample_hash": stable_hash(scrub_audit(decisions["representative_sample"])),
        "quality_threshold_hash": stable_hash(scrub_audit(decisions["quality_thresholds"])),
        "factor_promotion_rules": [],
        "strategy_or_pnl_reports": [],
        "confirmation_requirement": "future_unseen_cohort_required",
        "allowed_input_paths": decisions["materialization_decision_function"]["allowed_input_paths"],
        "prohibited_paths_detected": decisions["materialization_decision_function"]["prohibited_paths_detected"],
        "sanitized_input_hash": decisions["materialization_decision_function"]["sanitized_input_hash"],
        "decision_output_hash": stable_hash(scrub_audit(decisions["materialization_decision_function"])),
    }


def poison(value: Any) -> Any:
    if isinstance(value, dict):
        original_values = list(value.values())
        value.update(
            {
                "outcomePrices": [0.01, 0.99],
                "outcomes": ["poison"],
                "umaResolutionStatus": "resolved",
                "UmaResolutionStatuses": ["resolved"],
                "resolved": True,
                "winner": "YES",
                "winners": ["YES"],
                "finalPrice": 1.0,
                "Final_Prices": [1.0],
                "PnLs": [123],
                "strategy_decisions": {"buy": True},
                "strategyDecision": {"buy": True},
                "strategy_results": {"profit": 1},
                "cash_balance": 10_000,
                "cashBalances": [10_000],
                "realized_pnl": 123,
                "unrealized_pnl": 456,
                "profit": 789,
                "profitLoss": 789,
                "future_returns": [1.0],
                "settlements": ["done"],
                "settlement_status": "done",
                "resolutions": ["YES"],
                "hit_rate": 1.0,
                "hit_rates": [1.0],
                "ic": 0.9,
                "sharpe": 99,
                "sharpes": [99],
                "signals": [1],
                "labels": ["up"],
                "predictions": ["up"],
                "drawdowns": [0],
                "nested": {"fills": [{"price": 0.5}], "fees": 1, "positions": 2},
            },
        )
        for item in original_values:
            poison(item)
    elif isinstance(value, list):
        for item in value:
            poison(item)
    return value


def remove_prohibited(value: Any) -> Any:
    sanitized, _, _ = _sanitize(value, path=())
    return sanitized


def assert_current_panel_roles(events: list[dict[str, Any]]) -> None:
    for row in events:
        if row.get("event_date") not in CURRENT_DATES:
            continue
        role = str(row.get("cohort_role") or "").casefold()
        compact = role.replace("_", "")
        if role in DISALLOWED_CURRENT_DATE_ROLES or compact in DISALLOWED_CURRENT_DATE_ROLES:
            raise ValueError(f"current date {row.get('event_date')} has disallowed role {role!r}")


def first_sorted(rows: list[dict[str, Any]], *, key: Any) -> dict[str, Any] | None:
    return sorted(rows, key=key)[0] if rows else None


def median_volume(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(events, key=lambda row: (int(row.get("rows_written") or 0), *sort_key(row)))
    return ordered[(len(ordered) - 1) // 2] if ordered else None


def complete_date(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    counts = Counter(str(row.get("event_date")) for row in events)
    max_count = max(counts.values())
    for date in sorted(counts):
        rows = sorted([row for row in events if row.get("event_date") == date], key=sort_key)
        if len(rows) == max_count and not any(has_hard_break(row) for row in rows):
            return {"event_date": date, "event_count": len(rows), "event_slugs": [row.get("event_slug") for row in rows]}
    return None


def ambiguity_candidate(
    events: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    budget: int,
    *,
    ambiguity_scan_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ambiguity_deferred_reason: str | None = None,
) -> dict[str, Any]:
    validate_ambiguity_budget(budget)
    candidates = sorted(
        tokens,
        key=lambda row: (
            str(row.get("event_slug") or ""),
            str(row.get("condition_id") or ""),
            str(row.get("asset_id") or ""),
            int(row.get("market_index") or 0),
            str(row.get("token_side") or ""),
        ),
    )[: max(0, budget)]
    scope = {
        "candidate_budget_tokens": budget,
        "candidate_budget_hard_max_tokens": MAX_AMBIGUITY_TOKEN_BUDGET,
        "candidate_order_key": ["event_slug", "condition_id", "asset_id", "market_index", "token_side"],
        "candidate_boundary": "bounded_deterministic_budget_not_global_heaviest",
        "event_read_policy": "each candidate event parquet read once; necessary identifier/timestamp/update-type columns only",
        "selection_order": [
            "orderingAmbiguousRows desc",
            "orderingAmbiguousGroups desc",
            "stableSortKey asc",
        ],
    }
    if ambiguity_deferred_reason:
        return {
            **scope,
            "status": "deferred",
            "reason": ambiguity_deferred_reason,
            "candidate_tokens_considered": len(candidates),
            "candidate_events_considered": len({str(row.get("event_slug") or "") for row in candidates}),
            "scanned_tokens": 0,
            "scanned_events": 0,
            "external_real_ordering_evidence": g002_real_ordering_reference(),
        }
    if not candidates:
        return {
            **scope,
            "status": "deferred",
            "candidate_tokens_considered": 0,
            "scanned_tokens": 0,
            "scanned_events": 0,
            "reason": "no token candidates",
        }

    events_by_slug = {str(row.get("event_slug")): row for row in events}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for token in candidates:
        grouped.setdefault(str(token.get("event_slug")), []).append(token)

    event_results = [
        scan_event_ordering_ambiguity(
            events_by_slug.get(slug, {}),
            event_tokens,
            ambiguity_scan_cache=ambiguity_scan_cache,
        )
        for slug, event_tokens in sorted(grouped.items())
    ]
    token_results = [item for event in event_results for item in event["token_results"]]
    if not token_results:
        return {
            **scope,
            "status": "deferred",
            "candidate_tokens_considered": len(candidates),
            "candidate_events_considered": len(event_results),
            "scanned_tokens": 0,
            "scanned_events": sum(event["status"] == "scanned" for event in event_results),
            "reason": "no readable event parquet candidates",
            "event_scan_summaries": event_results,
        }
    best = sorted(
        token_results,
        key=lambda row: (
            -int(row["orderingAmbiguousRows"]),
            -int(row["orderingAmbiguousGroups"]),
            str(row["stableSortKey"]),
        ),
    )[0]
    return {
        **scope,
        **best,
        "status": "selected",
        "candidate_tokens_considered": len(candidates),
        "candidate_events_considered": len(event_results),
        "scanned_tokens": len(token_results),
        "scanned_events": sum(event["status"] == "scanned" for event in event_results),
        "event_scan_summaries": event_results,
    }


def scan_event_ordering_ambiguity(  # noqa: C901
    event_row: dict[str, Any],
    tokens: list[dict[str, Any]],
    *,
    ambiguity_scan_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    slug = str(event_row.get("event_slug") or tokens[0].get("event_slug") or "")
    path = Path((event_row.get("orderbook_file") or {}).get("path") or "")
    source_hash = (event_row.get("hashes") or {}).get("orderbook_identity_sha256") or stable_hash(event_row.get("orderbook_file") or {})
    base = {
        "event_slug": slug,
        "source_event_path": str(path) if str(path) != "." else "",
        "source_orderbook_identity_hash": source_hash,
        "token_count": len(tokens),
        "columns_requested": ["market", "asset_id", "timestamp", "timestamp_received", "event_type"],
        "physical_event_parquet_reads": 0,
        "read_reused_from_calibration_cache": False,
        "token_results": [],
    }
    if pd is None or not path.exists():
        return {**base, "status": "deferred", "reason": "event parquet unavailable"}

    cache_key = (str(path), str(source_hash))
    cached = ambiguity_scan_cache.get(cache_key) if ambiguity_scan_cache is not None else None
    if cached is None:
        try:
            frame = pd.read_parquet(path, columns=base["columns_requested"])
        except Exception as exc:  # pragma: no cover - depends on local parquet engine/files
            if ambiguity_scan_cache is not None:
                ambiguity_scan_cache[cache_key] = {"error_type": type(exc).__name__}
            return {**base, "status": "deferred", "reason": type(exc).__name__}
        base["physical_event_parquet_reads"] = 1
        if ambiguity_scan_cache is not None:
            ambiguity_scan_cache[cache_key] = {"frame": frame, "physical_event_parquet_reads": 1}
    elif "frame" not in cached:
        return {**base, "status": "deferred", "reason": str(cached["error_type"])}
    else:
        frame = cached["frame"]
        base["physical_event_parquet_reads"] = int(cached["physical_event_parquet_reads"])
        base["read_reused_from_calibration_cache"] = True

    frame = frame.reset_index(drop=True)
    for token in tokens:
        condition_id = str(token.get("condition_id") or "")
        asset_id = str(token.get("asset_id") or "")
        rows = frame[
            (frame["market"].map(normalize_cell) == condition_id)
            & (frame["asset_id"].map(normalize_cell) == asset_id)
        ]
        base_token = {
            "event_slug": slug,
            "condition_id": condition_id,
            "asset_id": asset_id,
            "stableSortKey": f"{slug}|{condition_id}|{asset_id}",
            "source_event_path": str(path),
            "source_orderbook_identity_hash": source_hash,
            "orderingAmbiguousGroups": 0,
            "orderingAmbiguousRows": 0,
            "tiedTimestampGroups": 0,
            "tiedTimestampRows": 0,
            "examples": [],
        }
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for physical_idx, row in rows.iterrows():
            market = normalize_cell(row.get("market"))
            row_asset_id = normalize_cell(row.get("asset_id"))
            timestamp = normalize_cell(row.get("timestamp"))
            received = normalize_cell(row.get("timestamp_received"))
            source_timestamp = timestamp or received
            full_tie = (market, row_asset_id, source_timestamp, received)
            signature = stable_hash(
                {
                    "market": market,
                    "asset_id": row_asset_id,
                    "timestamp": timestamp,
                    "source_timestamp_fallback": source_timestamp,
                    "timestamp_received": received,
                    "event_type": normalize_cell(row.get("event_type")),
                },
            )
            groups.setdefault(full_tie, []).append(
                {
                    "source_row_index": int(physical_idx),
                    "fullTie": list(full_tie),
                    "rowContentSignature": signature,
                    "event_type": normalize_cell(row.get("event_type")),
                },
            )
        for full_tie in sorted(groups):
            group_rows = groups[full_tie]
            if len(group_rows) <= 1:
                continue
            base_token["tiedTimestampGroups"] += 1
            base_token["tiedTimestampRows"] += len(group_rows)
            if len({row["rowContentSignature"] for row in group_rows}) > 1:
                base_token["orderingAmbiguousGroups"] += 1
                base_token["orderingAmbiguousRows"] += len(group_rows)
                if len(base_token["examples"]) < 3:
                    base_token["examples"].append(
                        {
                            "fullTie": group_rows[0]["fullTie"],
                            "source_row_index_examples": [row["source_row_index"] for row in group_rows[:5]],
                            "rowContentSignature_examples": sorted({row["rowContentSignature"] for row in group_rows})[:5],
                        },
                    )
        base["token_results"].append(base_token)
    return {**base, "status": "scanned", "rows_read": len(frame)}


def validate_ambiguity_budget(budget: int) -> None:
    if budget < 0 or budget > MAX_AMBIGUITY_TOKEN_BUDGET:
        raise ValueError(f"ambiguity token budget must be between 0 and {MAX_AMBIGUITY_TOKEN_BUDGET}")


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if pd is not None:
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return str(value)


def has_hard_break(row: dict[str, Any]) -> bool:
    return int(row.get("event_local_missing_hour_count") or 0) > 0 or int(row.get("event_local_corrupt_hour_count") or 0) > 0


def sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("event_date") or ""), str(row.get("city") or ""), str(row.get("event_slug") or ""))


def frozen_synthetic_benchmark_metrics() -> dict[str, Any]:
    return {
        "max_parallel_workers": 1,
        "rows_written_total_from_manifest": 1,
        "schema_contract_available": True,
        "timestamp_column_available": True,
    }


def read_table(path: Path) -> list[dict[str, Any]]:
    if pd is None:
        raise RuntimeError("pandas is required for parquet inventory artifacts")
    return [normalize(row) for row in pd.read_parquet(path).to_dict(orient="records")]


def read_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected JSON object: {path}")
    return loaded


def normalize(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(item) for item in value]
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return value


def parse_utc(value: str) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def g002_real_ordering_reference() -> dict[str, Any]:
    digest = None
    if G002_REAL_ORDERING_ARTIFACT.exists():
        hasher = hashlib.sha256()
        with G002_REAL_ORDERING_ARTIFACT.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    return {
        "artifact_path": str(G002_REAL_ORDERING_ARTIFACT.relative_to(BASE_DIR.parent)),
        "artifact_sha256": digest,
        "use": "reference_only_no_G003_metric_import",
        "scope": "G002 bounded real curated ordering smoke; not global ambiguity calibration",
    }


def path_text(path: tuple[str, ...]) -> str:
    return "$" if not path else "$." + ".".join(path)


def compact_paths(paths: list[str]) -> list[str]:
    compacted = {
        ".".join("[]" if part.isdigit() else part for part in path.split("."))
        for path in paths
    }
    return sorted(compacted)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        tmp = handle.name
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
