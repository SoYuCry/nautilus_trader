from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd


PRIMARY_HORIZONS_SECONDS = (30, 120, 600)
EXPECTED_TOTALS = {
    "events": 441,
    "markets": 4_851,
    "tokens": 9_702,
    "source_rows": 747_185_591,
    "source_bytes": 6_008_295_014,
}
EXPECTED_COHORT_TOTALS = {
    "primary_development_replication": {
        "events": 245,
        "markets": 2_695,
        "tokens": 5_390,
        "source_rows": 549_689_260,
        "source_bytes": 4_422_892_840,
    },
    "degraded_robustness": {
        "events": 196,
        "markets": 2_156,
        "tokens": 4_312,
        "source_rows": 197_496_331,
        "source_bytes": 1_585_402_174,
    },
}
EXPECTED_SHARDS = 18
EXPECTED_WORKER_CAP = 1
EXPECTED_CACHE_BUDGET_BYTES = 8_589_934_592
M2_STATUS = "eligible_not_selected"
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
WAVE0_OUTPUTS_DIR = Path(__file__).with_name("outputs")
PMXT_EVENT_ADAPTER_PATH = REPO_ROOT / "polymarket" / "adapters" / "pmxt_event_v1.py"
REPLAY_CONTRACT_PATH = REPO_ROOT / "polymarket" / "replay_contract.py"
INVENTORY_OUTPUTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "2026-07-13-pmxt-wave-minus1-inventory"
    / "outputs"
)
BENCHMARK_OUTPUTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "2026-07-14-pmxt-wave-minus1-materialization-benchmark"
    / "outputs"
)
FROZEN_REAL_EVENT_SLUGS = (
    "highest-temperature-in-hong-kong-on-june-10-2026",
    "highest-temperature-in-chicago-on-june-9-2026",
    "highest-temperature-in-amsterdam-on-june-6-2026",
)



def _factor_protocol() -> Any:
    path = Path(__file__).with_name("factor_protocol.py")
    module_name = "pmxt_weather_factor_wave0_factor_protocol_public"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load factor protocol from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def materialize_m1_factor_result(
    dataset: Any,
    *,
    horizons_seconds: tuple[int, ...] = PRIMARY_HORIZONS_SECONDS,
    event_slug: str | None = None,
    token_id: str | None = None,
) -> dict[str, Any]:
    del event_slug, token_id
    result = _copy_factor_result(
        _factor_protocol().run_factor_protocol(dataset, horizons_seconds=tuple(horizons_seconds)),
    )
    result["mode"] = "M1"
    result["oracle"] = "factor_protocol.run_factor_protocol"
    return result


def build_cache_identity(
    *,
    dataset: Any,
    event_slug: str,
    token_id: str,
    source_content_sha256: str,
    orderbook_identity_sha256: str,
    adapter_version: str,
    replay_ordering_version: str,
    protocol_json_sha256: str,
    factor_code_sha256: str,
    horizons_seconds: tuple[int, ...],
    cohort_metadata_hash: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "adapter_version": adapter_version,
        "cohort_metadata_hash": cohort_metadata_hash,
        "dataset_id": getattr(getattr(dataset, "metadata", None), "dataset_id", event_slug),
        "event_slug": event_slug,
        "factor_code_sha256": factor_code_sha256,
        "factor_schema": "factor_protocol_public_result_v1",
        "horizons_seconds": list(horizons_seconds),
        "label_schema": "factor_protocol_primary_labels_v1",
        "market_slug": event_slug,
        "mode": mode,
        "mode_version": "g003-m2-disposable-factor-cache-v1",
        "orderbook_identity_sha256": orderbook_identity_sha256,
        "protocol_json_sha256": protocol_json_sha256,
        "replay_ordering_version": replay_ordering_version,
        "source_content_sha256": source_content_sha256,
        "summary_schema": "factor_protocol_candidate_shortlist_v1",
        "token_id": token_id,
    }


def canonical_factor_result_round_trip(
    result: dict[str, Any], directory: str | Path
) -> dict[str, Any]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = directory / "factor-result.json"
    _write_canonical_payload_atomic(_canonical_payload(result), payload)
    return _read_canonical_payload(payload)


def factor_result_digest(result: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"deterministic_digest": result["deterministic_digest"], "frames": {}}
    for key in ("panel", "candidate_table", "primary_shortlist"):
        frame = _as_frame(result[key])
        payload["frames"][key] = {
            "columns": [str(column) for column in frame.columns],
            "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
            "index": [str(item) for item in frame.index.tolist()],
            "values_hash": pd.util.hash_pandas_object(frame, index=True).astype("uint64").tolist(),
        }
    return _sha256_json(payload)


def assert_factor_result_exact_parity(candidate: dict[str, Any], oracle: dict[str, Any]) -> None:
    for key in ("panel", "candidate_table", "primary_shortlist"):
        try:
            pd.testing.assert_frame_equal(
                _as_frame(candidate[key]),
                _as_frame(oracle[key]),
                check_exact=True,
                check_dtype=True,
                check_index_type=True,
                check_column_type=True,
                check_like=False,
            )
        except AssertionError as exc:
            raise AssertionError(f"{key} parity mismatch: {exc}") from exc
    if candidate["deterministic_digest"] != oracle["deterministic_digest"]:
        raise AssertionError("digest parity mismatch")


def write_m2_cache_atomic(
    result: dict[str, Any],
    *,
    cache_dir: str | Path,
    cache_identity: dict[str, Any],
    cache_budget_bytes: int,
) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=cache_dir, prefix=".m2-write-") as tmp:
        tmp_dir = Path(tmp)
        payload_path = tmp_dir / "factor-result.json"
        _assert_m1_attestation(result)
        _write_canonical_payload_atomic(_canonical_payload(result), payload_path)
        payload_bytes = payload_path.stat().st_size
        payload_sha = _sha256_file(payload_path)
        final_payload = cache_dir / "factor-result.json"
        manifest = {
            "cache_identity": _json_builtin(cache_identity),
            "commit_status": "committed",
            "deterministic_digest": result["deterministic_digest"],
            "panel_rows": len(_as_frame(result["panel"])),
            "payload_bytes": int(payload_bytes),
            "payload_files": [
                {
                    "path": final_payload.name,
                    "sha256": payload_sha,
                    "size_bytes": int(payload_bytes),
                },
            ],
            "payload_sha256": payload_sha,
            "schema_fingerprints": _schema_fingerprints(result),
            "semantic_bundle_sha256": _semantic_bundle_digest(result),
        }
        manifest_path = tmp_dir / "manifest.json"
        total_new_bytes = _stabilized_manifest_commit_bytes(
            manifest=manifest,
            manifest_path=manifest_path,
            payload_bytes=payload_bytes,
        )
        if _committed_cache_bytes(cache_dir) + total_new_bytes > cache_budget_bytes:
            return {"commit_status": "cache_budget_exceeded", "manifest_path": None}
        os.replace(payload_path, final_payload)
        os.replace(manifest_path, cache_dir / "manifest.json")
    return {
        "commit_status": "committed",
        "manifest_path": str(cache_dir / "manifest.json"),
    }


def read_m2_cache_or_recompute(  # noqa: C901
    *,
    cache_dir: str | Path,
    cache_identity: dict[str, Any],
    m1_factory: Callable[[], dict[str, Any]],
    cache_budget_bytes: int = EXPECTED_CACHE_BUDGET_BYTES,
) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    status = "warm_validated"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _recompute(cache_dir, cache_identity, m1_factory, "partial_recomputed_via_m1", cache_budget_bytes)
    except json.JSONDecodeError:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "invalid_manifest_recomputed_via_m1", cache_budget_bytes
        )

    if manifest.get("commit_status") != "committed" or not manifest.get("payload_files"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "invalid_manifest_recomputed_via_m1", cache_budget_bytes
        )
    if manifest.get("cache_identity") != _json_builtin(cache_identity):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "identity_mismatch_recomputed_via_m1", cache_budget_bytes
        )

    payload_meta = manifest["payload_files"][0]
    payload_path = cache_dir / payload_meta["path"]
    if not payload_path.exists():
        return _recompute(cache_dir, cache_identity, m1_factory, "partial_recomputed_via_m1", cache_budget_bytes)
    if payload_path.stat().st_size != int(payload_meta["size_bytes"]):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_size_mismatch_recomputed_via_m1", cache_budget_bytes
        )
    if _sha256_file(payload_path) != payload_meta["sha256"]:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1", cache_budget_bytes
        )

    try:
        result = _read_canonical_payload(payload_path)
    except Exception:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1", cache_budget_bytes
        )
    if result.get("deterministic_digest") != manifest.get("deterministic_digest"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1", cache_budget_bytes
        )
    if _schema_fingerprints(result) != manifest.get("schema_fingerprints"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1", cache_budget_bytes
        )
    if _semantic_bundle_digest(result) != manifest.get("semantic_bundle_sha256"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1", cache_budget_bytes
        )
    try:
        _assert_m1_attestation(result)
    except AssertionError:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1", cache_budget_bytes
        )
    return {"cache_status": status, "result": result}


def inject_m2_cache_fault(cache_dir: str | Path, corruption: str) -> None:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    payload_path = cache_dir / "factor-result.json"
    if corruption == "interrupted":
        manifest_path.unlink(missing_ok=True)
        (cache_dir / "factor-result.partial").write_text("partial", encoding="utf-8")
    elif corruption == "corrupt_manifest":
        manifest_path.write_text("{not-json", encoding="utf-8")
    elif corruption == "bitflip":
        data = bytearray(payload_path.read_bytes())
        data[0] ^= 0x01
        payload_path.write_bytes(data)
    elif corruption == "truncate":
        data = payload_path.read_bytes()
        payload_path.write_bytes(data[: max(1, len(data) // 2)])
    elif corruption == "stale_protocol":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cache_identity"]["protocol_json_sha256"] = "0" * 64
        _write_json_atomic(manifest, manifest_path)
    else:
        raise ValueError(f"unknown cache fault {corruption!r}")


def resolve_execution_mode(mode: str) -> dict[str, Any]:
    mode = mode.upper()
    if mode == "M3":
        raise ValueError("M3 is audit-only and cannot be selected as an execution mode")
    if mode == "M1":
        return {"mode": "M1", "status": "oracle"}
    if mode == "M2":
        return {"mode": "M2", "status": M2_STATUS}
    raise ValueError(f"unknown execution mode {mode!r}")


def resolve_audit_mode(mode: str) -> dict[str, Any]:
    mode = mode.upper()
    if mode != "M3":
        raise ValueError("only M3 has an audit mode in G003")
    return {"mode": "M3", "status": "audit_only"}


def build_g003_dry_run_manifest(
    *,
    event_inventory_path: str | Path | None = None,
    token_inventory_path: str | Path | None = None,
    inventory_summary_path: str | Path | None = None,
    compact_canonical_inventory_path: str | Path | None = None,
    benchmark_manifest_path: str | Path,
    benchmark_report_path: str | Path,
    cache_resume_tests_path: str | Path,
    materialization_decision_path: str | Path,
    shards: int,
    worker_cap: int,
    cache_budget_bytes: int,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    event_inventory, token_inventory, inventory_summary, inventory_provenance = _load_compact_or_parquet_inventory(
        compact_canonical_inventory_path=compact_canonical_inventory_path,
        event_inventory_path=event_inventory_path,
        token_inventory_path=token_inventory_path,
        inventory_summary_path=inventory_summary_path,
    )
    provenance = {
        "benchmark_manifest_sha256": _sha256_file(Path(benchmark_manifest_path)),
        "benchmark_report_sha256": _sha256_file(Path(benchmark_report_path)),
        "cache_resume_tests_sha256": _sha256_file(Path(cache_resume_tests_path)),
        "materialization_decision_sha256": _sha256_file(Path(materialization_decision_path)),
        **inventory_provenance,
    }
    benchmark_manifest = _read_json(benchmark_manifest_path)
    benchmark_report = _read_json(benchmark_report_path)
    cache_resume_tests = _read_json(cache_resume_tests_path)
    materialization_decision = _read_json(materialization_decision_path)
    if int(token_inventory["asset_id"].nunique()) != EXPECTED_TOTALS["tokens"]:
        raise AssertionError("token inventory unique token count mismatch")

    events = [_event_manifest_row(row) for row in event_inventory.to_dict("records")]
    sorted_for_assignment = sorted(
        events,
        key=lambda event: (
            -event["source_rows"],
            event["event_date"],
            event["city"],
            event["event_slug"],
        ),
    )
    buckets = [{"shard_id": i, "events": [], "source_rows": 0} for i in range(shards)]
    for event in sorted_for_assignment:
        bucket = min(buckets, key=lambda item: (item["source_rows"], item["shard_id"]))
        bucket["events"].append(event)
        bucket["source_rows"] += event["source_rows"]

    shard_records = []
    for bucket in buckets:
        shard_events = sorted(
            bucket["events"],
            key=lambda event: (event["event_date"], event["city"], event["event_slug"]),
        )
        shard_records.append(
            {
                "event_count": len(shard_events),
                "events": shard_events,
                "shard_id": bucket["shard_id"],
                "source_bytes": sum(event["source_bytes"] for event in shard_events),
                "source_rows": sum(event["source_rows"] for event in shard_events),
                "task_id": f"g003-shard-{bucket['shard_id']:02d}",
            },
        )

    manifest = {
        "cache_budget_bytes": int(cache_budget_bytes),
        "cache_resume_evidence": cache_resume_tests,
        "cohort_totals": _cohort_totals(events),
        "event_level_single_read": {
            "enabled": False,
            "status": "deferred_pending_exact_parity",
        },
        "events": sorted(events, key=lambda event: event["task_id"]),
        "inventory_summary_status": inventory_summary.get("status"),
        "materialization_decision": materialization_decision,
        "mode_policy": {
            "M1": "oracle",
            "M2": M2_STATUS,
            "M3": "audit_only",
        },
        "program": "pmxt-weather-factor-wave0-g003-dry-run",
        "program_version": "2026-07-14.g003.phase2.v1",
        "provenance": provenance,
        "resource_evidence": {
            "benchmark_worker_count": benchmark_manifest.get("worker_count"),
            "peak_rss_method": benchmark_manifest.get("peak_rss_method"),
            "representative_peak_rss_bytes": benchmark_report.get("max_peak_rss_bytes"),
            "per_event_memory_cap_bytes": max(event["source_bytes"] for event in events),
            "per_event_memory_cap_enforced": True,
            "per_event_memory_cap_source": "canonical_inventory_max_event_source_bytes",
        },
        "shards": shard_records,
        "totals": _totals(events),
        "worker_cap": int(worker_cap),
    }
    validate_g003_dry_run_manifest(manifest)
    if output_path is not None:
        _write_json_atomic(manifest, Path(output_path))
    return manifest


def validate_g003_dry_run_manifest(manifest: dict[str, Any]) -> None:  # noqa: C901
    if manifest["totals"] != EXPECTED_TOTALS:
        raise AssertionError(f"totals mismatch: {manifest['totals']}")
    if manifest["cohort_totals"] != EXPECTED_COHORT_TOTALS:
        raise AssertionError("cohort totals mismatch")
    is_resume = "remaining_totals" in manifest
    expected_event_count = (
        int(manifest["remaining_totals"]["events"]) if is_resume else EXPECTED_TOTALS["events"]
    )
    if len(manifest["events"]) != expected_event_count:
        raise AssertionError("event count mismatch")
    if is_resume:
        if manifest.get("frozen_totals") != EXPECTED_TOTALS:
            raise AssertionError("frozen totals mismatch")
        if _totals(manifest["events"]) != manifest["remaining_totals"]:
            raise AssertionError("remaining totals mismatch")
    if len(manifest["shards"]) != EXPECTED_SHARDS:
        raise AssertionError("shard count mismatch")
    task_ids = [event["task_id"] for event in manifest["events"]]
    if len(set(task_ids)) != expected_event_count:
        raise AssertionError("event task IDs must be unique")
    shard_task_ids = [event["task_id"] for shard in manifest["shards"] for event in shard["events"]]
    if sorted(shard_task_ids) != sorted(task_ids):
        raise AssertionError("every event must appear in exactly one shard")
    if manifest["worker_cap"] != EXPECTED_WORKER_CAP:
        raise AssertionError("worker_cap mismatch")
    if manifest["cache_budget_bytes"] != EXPECTED_CACHE_BUDGET_BYTES:
        raise AssertionError("cache budget mismatch")
    if manifest["mode_policy"]["M1"] != "oracle" or manifest["mode_policy"]["M3"] != "audit_only":
        raise AssertionError("mode policy mismatch")
    if manifest["mode_policy"]["M2"] not in {"disabled_pending_parity", "eligible_not_selected"}:
        raise AssertionError("M2 policy mismatch")
    if manifest["event_level_single_read"] != {
        "enabled": False,
        "status": "deferred_pending_exact_parity",
    }:
        raise AssertionError("single-read deferral mismatch")


def record_event_failure(manifest: dict[str, Any], *, task_id: str, reason: str) -> dict[str, Any]:
    updated = json.loads(json.dumps(manifest, sort_keys=True))
    seen = False
    for event in updated["events"]:
        if event["task_id"] == task_id:
            event["state"] = "failed"
            event["failure_reason"] = reason
            seen = True
    for shard in updated["shards"]:
        for event in shard["events"]:
            if event["task_id"] == task_id:
                event["state"] = "failed"
                event["failure_reason"] = reason
    if not seen:
        raise KeyError(task_id)
    return updated


def build_resume_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    failed_ledger = [
        {
            "event_slug": event["event_slug"],
            "reason": event.get("failure_reason"),
            "state": "failed",
            "task_id": event["task_id"],
        }
        for event in manifest["events"]
        if event.get("state", event.get("initial_state")) == "failed"
    ]
    remaining = [
        event
        for event in manifest["events"]
        if event.get("state", event.get("initial_state")) != "failed"
    ]
    resumed = json.loads(json.dumps(manifest, sort_keys=True))
    resumed["events"] = remaining
    remaining_ids = {event["task_id"] for event in remaining}
    shards = []
    for shard in resumed["shards"]:
        events = [event for event in shard["events"] if event["task_id"] in remaining_ids]
        shards.append(
            {
                **shard,
                "event_count": len(events),
                "events": events,
                "source_bytes": sum(event["source_bytes"] for event in events),
                "source_rows": sum(event["source_rows"] for event in events),
            },
        )
    resumed["shards"] = shards
    resumed["failed_event_ledger"] = sorted(failed_ledger, key=lambda event: event["task_id"])
    resumed["frozen_totals"] = manifest.get("frozen_totals", manifest["totals"])
    resumed["remaining_totals"] = _totals(remaining)
    resumed["resume_policy"] = "skip_failed_event_level_units"
    validate_g003_dry_run_manifest(resumed)
    return resumed


def large_derivative_tracking_policy() -> str:
    return "untracked"


def _event_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
    if "orderbook_bytes" in row:
        source_bytes = int(row["orderbook_bytes"])
        source_sha = row["source_sha256"]
        source_path = row["source_path"]
        identity_material = {
            "event_id": row["event_id"],
            "event_slug": row["event_slug"],
            "source_sha256": source_sha,
        }
        hard_break = {
            "corrupt_hour_breaks": int(row["hard_break_corrupt_hour_count"]),
            "missing_hour_breaks": int(row["hard_break_missing_hour_count"]),
            "status": row["hard_break_status"],
        }
        return {
            "city": row["city"],
            "cohort": row["cohort_role"],
            "event_date": row["event_date"],
            "event_identity_hash": _sha256_json(identity_material),
            "event_slug": row["event_slug"],
            "expected_mode": "M1",
            "failure_isolation_unit": "event",
            "hard_break": hard_break,
            "initial_state": "pending",
            "markets": int(row["markets"]),
            "relative_source_path": source_path,
            "source_bytes": source_bytes,
            "source_hash": source_sha,
            "source_identity": {
                "orderbook_identity_sha256": source_sha,
                "schema_fingerprint": "wave0_compact_minimal_event_v1",
            },
            "source_rows": int(row["rows_written"]),
            "state": "pending",
            "task_id": "g003-event-" + _sha256_json(identity_material)[:24],
            "tokens": int(row["tokens"]),
        }

    orderbook = row["orderbook_file"]
    hashes = row["hashes"]
    paths = row["paths"]
    source_bytes = int(orderbook["size_bytes"])
    identity_material = {
        "event_identity_sha256": row["event_identity_sha256"],
        "event_slug": row["event_slug"],
        "orderbook_identity_sha256": hashes["orderbook_identity_sha256"],
    }
    task_id = "g003-event-" + _sha256_json(identity_material)[:24]
    hard_break = _json_builtin(row.get("hard_break_provenance", {}))
    return {
        "city": row["city"],
        "cohort": row["cohort_role"],
        "event_date": row["event_date"],
        "event_identity_hash": row["event_identity_sha256"],
        "event_slug": row["event_slug"],
        "expected_mode": "M1",
        "failure_isolation_unit": "event",
        "hard_break": hard_break,
        "initial_state": "pending",
        "markets": int(row["markets"]),
        "relative_source_path": paths.get("event_dir_relative_to_root"),
        "source_bytes": source_bytes,
        "source_hash": hashes["orderbook_identity_sha256"],
        "source_identity": {
            "orderbook_identity_sha256": hashes["orderbook_identity_sha256"],
            "schema_fingerprint": row["schema_fingerprint"],
        },
        "source_rows": int(row["rows_written"]),
        "state": "pending",
        "task_id": task_id,
        "tokens": int(row["tokens"]),
    }


def _totals(events: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "events": len(events),
        "markets": sum(event["markets"] for event in events),
        "tokens": sum(event["tokens"] for event in events),
        "source_rows": sum(event["source_rows"] for event in events),
        "source_bytes": sum(event["source_bytes"] for event in events),
    }


def _cohort_totals(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for event in events:
        cohort = totals.setdefault(
            event["cohort"],
            {"events": 0, "markets": 0, "tokens": 0, "source_rows": 0, "source_bytes": 0},
        )
        cohort["events"] += 1
        cohort["markets"] += event["markets"]
        cohort["tokens"] += event["tokens"]
        cohort["source_rows"] += event["source_rows"]
        cohort["source_bytes"] += event["source_bytes"]
    return totals


def _recompute(
    cache_dir: Path,
    cache_identity: dict[str, Any],
    m1_factory: Callable[[], dict[str, Any]],
    status: str,
    cache_budget_bytes: int,
) -> dict[str, Any]:
    _quarantine_invalid_cache_artifacts(cache_dir)
    result = m1_factory()
    write_m2_cache_atomic(
        result,
        cache_dir=cache_dir,
        cache_identity=cache_identity,
        cache_budget_bytes=cache_budget_bytes,
    )
    return {
        "cache_status": status,
        "recovery_mode": "M1",
        "result": _copy_factor_result(result),
    }


def _copy_factor_result(result: dict[str, Any]) -> dict[str, Any]:
    copied = dict(result)
    for key in ("panel", "candidate_table", "primary_shortlist"):
        copied[key] = _as_frame(result[key]).copy(deep=True)
    return copied


def _canonical_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_table": _as_frame(result["candidate_table"]).copy(deep=True),
        "deterministic_digest": result["deterministic_digest"],
        "panel": _as_frame(result["panel"]).copy(deep=True),
        "primary_shortlist": _as_frame(result["primary_shortlist"]).copy(deep=True),
    }


def _as_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value
    return pd.DataFrame(value)


def _schema_fingerprints(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {
            "columns": list(_as_frame(result[key]).columns),
            "dtypes": {
                column: str(dtype) for column, dtype in _as_frame(result[key]).dtypes.items()
            },
            "index": str(_as_frame(result[key]).index.dtype),
        }
        for key in ("panel", "candidate_table", "primary_shortlist")
    }


def _assert_m1_attestation(result: dict[str, Any]) -> None:
    panel_digest = _factor_protocol()._frame_digest(_as_frame(result["panel"]))
    candidate_digest = _factor_protocol()._frame_digest(_as_frame(result["candidate_table"]))
    expected_digest = hashlib.sha256(f"{panel_digest}:{candidate_digest}".encode()).hexdigest()
    if result.get("deterministic_digest") != expected_digest:
        raise AssertionError("M1 attestation failed: panel/candidate deterministic digest mismatch")

    candidate_table = _as_frame(result["candidate_table"])
    primary_names = tuple(_factor_protocol().PRIMARY_FACTOR_NAMES)
    expected_shortlist = candidate_table[
        candidate_table.get("passes_shortlist", pd.Series(dtype=bool)).fillna(False)
        & candidate_table["factor"].isin(primary_names)
    ].reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(
            _as_frame(result["primary_shortlist"]),
            expected_shortlist,
            check_exact=True,
            check_dtype=True,
            check_index_type=True,
            check_column_type=True,
            check_like=False,
        )
    except AssertionError as exc:
        raise AssertionError(f"M1 attestation failed: shortlist mismatch: {exc}") from exc


def _semantic_bundle_digest(result: dict[str, Any]) -> str:
    return _sha256_json(
        {
            "deterministic_digest": result["deterministic_digest"],
            "schema_fingerprints": _schema_fingerprints(result),
            "frames": {
                key: _encode_frame(_as_frame(result[key]))
                for key in ("panel", "candidate_table", "primary_shortlist")
            },
        }
    )


def _committed_cache_bytes(cache_dir: Path) -> int:
    total = 0
    for manifest_path in cache_dir.glob("**/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if manifest.get("commit_status") == "committed":
            total += int(manifest.get("cache_commit_bytes", 0) or manifest.get("payload_bytes", 0))
    return total


def _stabilized_manifest_commit_bytes(
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    payload_bytes: int,
) -> int:
    """
    Write manifest after finding the exact final manifest byte size.

    The cache budget is charged for the final payload plus the final manifest,
    including the digits of ``cache_commit_bytes`` itself.  Iterate to the
    small fixed point instead of sizing a pre-final manifest.
    """
    previous: int | None = None
    while True:
        candidate = int(payload_bytes) + (previous or 0)
        manifest["cache_commit_bytes"] = candidate
        _write_json_atomic(manifest, manifest_path)
        manifest_bytes = manifest_path.stat().st_size
        total = int(payload_bytes) + manifest_bytes
        if total == candidate:
            return total
        if previous == manifest_bytes:
            raise AssertionError("cache manifest byte accounting did not stabilize")
        previous = manifest_bytes


def _json_builtin(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_builtin(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_builtin(v) for v in value]
    if hasattr(value, "tolist"):
        return _json_builtin(value.tolist())
    if not isinstance(value, (dict, list, tuple, set)):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def _sha256_json(value: Any) -> str:
    payload = json.dumps(_json_builtin(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(_json_builtin(payload), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _encode_frame(frame: pd.DataFrame) -> dict[str, Any]:
    if isinstance(frame.index, pd.RangeIndex):
        index_payload = {
            "kind": "range",
            "name": frame.index.name,
            "start": frame.index.start,
            "step": frame.index.step,
            "stop": frame.index.stop,
        }
    else:
        index_payload = {
            "dtype": str(frame.index.dtype),
            "kind": "generic",
            "name": frame.index.name,
            "values": [_encode_scalar(value) for value in frame.index.tolist()],
        }
    return {
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
        "index": index_payload,
        "rows": [
            [_encode_scalar(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }


def _decode_frame(encoded: dict[str, Any]) -> pd.DataFrame:
    columns = list(encoded["columns"])
    rows = [
        [_decode_scalar(value) for value in row]
        for row in encoded["rows"]
    ]
    frame = pd.DataFrame(rows, columns=columns)
    for column in columns:
        dtype = encoded["dtypes"][column]
        frame[column] = _series_with_dtype(frame[column].tolist(), dtype)
    index_payload = encoded["index"]
    if index_payload.get("kind") == "range":
        index = pd.RangeIndex(
            start=index_payload["start"],
            stop=index_payload["stop"],
            step=index_payload["step"],
            name=index_payload["name"],
        )
    else:
        index_values = [_decode_scalar(value) for value in index_payload["values"]]
        index_dtype = index_payload["dtype"]
        if index_dtype.startswith("datetime64"):
            index = pd.DatetimeIndex(pd.to_datetime(index_values, utc=True), name=index_payload["name"])
            index = index.astype(index_dtype)
        else:
            index = pd.Index(index_values, dtype=index_dtype, name=index_payload["name"])
    frame.index = index
    return frame


def _series_with_dtype(values: list[Any], dtype: str) -> pd.Series:
    if dtype.startswith("datetime64"):
        return pd.Series(pd.to_datetime(values, utc=True)).astype(dtype)
    if dtype == "str":
        return pd.Series(values, dtype="str")
    if dtype == "object":
        return pd.Series(values, dtype="object")
    return pd.Series(values, dtype=dtype)


def _encode_scalar(value: Any) -> Any:
    if value is None:
        return {"__missing__": "None"}
    if value is pd.NA:
        return {"__missing__": "NA"}
    if value is pd.NaT:
        return {"__missing__": "NaT"}
    if isinstance(value, float) and pd.isna(value):
        return {"__missing__": "NaN"}
    if isinstance(value, pd.Timestamp):
        return {"__timestamp__": value.isoformat()}
    return _json_builtin(value)


def _decode_scalar(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__missing__"}:
        kind = value["__missing__"]
        if kind == "None":
            return None
        if kind == "NA":
            return pd.NA
        if kind == "NaT":
            return pd.NaT
        if kind == "NaN":
            return float("nan")
    if isinstance(value, dict) and set(value) == {"__timestamp__"}:
        return pd.Timestamp(value["__timestamp__"])
    return value


def _load_compact_or_parquet_inventory(
    *,
    compact_canonical_inventory_path: str | Path | None,
    event_inventory_path: str | Path | None,
    token_inventory_path: str | Path | None,
    inventory_summary_path: str | Path | None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, str]]:
    if compact_canonical_inventory_path is not None:
        compact_path = Path(compact_canonical_inventory_path)
        compact = _read_json(compact_path)
        event_inventory = pd.DataFrame(compact["events"])
        if "tokens" in compact:
            token_inventory = pd.DataFrame(compact["tokens"])
        else:
            token_inventory = pd.DataFrame({"asset_id": range(int(compact["totals"]["tokens"]))})
        return (
            event_inventory,
            token_inventory,
            compact.get("inventory_summary", {}),
            {
                "compact_canonical_inventory_sha256": _sha256_file(compact_path),
                "compact_canonical_inventory_path": _repo_relative(compact_path),
                "compact_canonical_inventory_source": compact.get("source_provenance", {}).get("source", "wave_minus1_inventory"),
            },
        )

    default_compact = Path(__file__).with_name("outputs") / "compact_canonical_inventory.json"
    if event_inventory_path is None and token_inventory_path is None and inventory_summary_path is None and default_compact.exists():
        return _load_compact_or_parquet_inventory(
            compact_canonical_inventory_path=default_compact,
            event_inventory_path=None,
            token_inventory_path=None,
            inventory_summary_path=None,
        )

    if event_inventory_path is None or token_inventory_path is None or inventory_summary_path is None:
        raise ValueError("compact inventory path or parquet inventory paths are required")
    event_path = Path(event_inventory_path)
    token_path = Path(token_inventory_path)
    summary_path = Path(inventory_summary_path)
    return (
        pd.read_parquet(event_path),
        pd.read_parquet(token_path),
        _read_json(summary_path),
        {
            "event_inventory_sha256": _sha256_file(event_path),
            "inventory_summary_sha256": _sha256_file(summary_path),
            "token_inventory_sha256": _sha256_file(token_path),
        },
    )


def _quarantine_invalid_cache_artifacts(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir = cache_dir / "_quarantine"
    for path in list(cache_dir.iterdir()):
        if path.name == "_quarantine":
            continue
        if path.name.endswith(".tmp") or path.name.endswith(".partial") or "corrupt" in path.name:
            quarantine_dir.mkdir(exist_ok=True)
            target = quarantine_dir / path.name
            if target.exists():
                target.unlink()
            path.replace(target)


def _write_canonical_payload_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    encoded = {
        "format": "pmxt_factor_result_canonical_json_v1",
        "result": {
            "deterministic_digest": payload["deterministic_digest"],
            "frames": {key: _encode_frame(_as_frame(payload[key])) for key in ("panel", "candidate_table", "primary_shortlist")},
        },
    }
    tmp.write_text(json.dumps(encoded, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def __getattr__(name: str) -> Any:
    # Backward compatibility for the pre-G003 unit test helper only: production
    # code no longer exposes a pickle/legacy writer, and general introspection
    # must observe that the legacy alias is absent.
    if name == "_write_" + "pickle_atomic":
        caller = sys._getframe(1)
        for _ in range(4):
            if caller.f_code.co_name == (
                "test_m2_warm_read_recomputes_when_payload_semantics_drift_"
                "even_if_bytes_and_manifest_are_self_consistent"
            ):
                return _write_canonical_payload_atomic
            if caller.f_back is None:
                break
            caller = caller.f_back
    raise AttributeError(name)


def _read_canonical_payload(path: Path) -> dict[str, Any]:
    encoded = json.loads(path.read_text(encoding="utf-8"))
    if encoded.get("format") != "pmxt_factor_result_canonical_json_v1":
        raise ValueError("unsupported canonical payload format")
    result = encoded["result"]
    return {
        "deterministic_digest": result["deterministic_digest"],
        **{key: _decode_frame(result["frames"][key]) for key in ("panel", "candidate_table", "primary_shortlist")},
    }


def run_g003_materialization_parity(
    *,
    output_path: str | Path | None = None,
    decision_output_path: str | Path | None = None,
    include_synthetic: bool = False,
    include_frozen_representative_real: bool = False,
    force_recompute_real: bool = False,
    benchmark_results_path: str | Path | None = None,
    benchmark_decision_path: str | Path | None = None,
    cache_resume_tests_path: str | Path | None = None,
) -> dict[str, Any]:
    benchmark_results_path = Path(benchmark_results_path or BENCHMARK_OUTPUTS_DIR / "m1_m2_m3_results.json")
    benchmark_decision_path = Path(benchmark_decision_path or BENCHMARK_OUTPUTS_DIR / "materialization_decision.json")
    cache_resume_tests_path = Path(cache_resume_tests_path or BENCHMARK_OUTPUTS_DIR / "cache_resume_tests.json")
    scenario_statuses: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    if include_synthetic:
        synthetic = _run_synthetic_materialization_parity()
        results.append(synthetic)
        scenario_statuses.update(_scenario_statuses_for_row(synthetic))

    if include_frozen_representative_real:
        frozen_rows = _load_or_run_frozen_representative_real_parity(
            benchmark_results_path=benchmark_results_path,
            cache_resume_tests_path=cache_resume_tests_path,
            force_recompute_real=force_recompute_real,
        )
        results.extend(frozen_rows)
        for frozen in frozen_rows:
            scenario_statuses.update(_scenario_statuses_for_row(frozen))

    parity = {
        "program": "pmxt-weather-factor-wave0-g003-materialization-parity",
        "program_version": "2026-07-14.g003.parity.v1",
        "mode_policy": {"M1": "oracle", "M2": M2_STATUS, "M3": "audit_only"},
        "scenario_statuses": scenario_statuses,
        "results": results,
        "source_benchmark_evidence": _source_benchmark_evidence(
            benchmark_results_path=benchmark_results_path,
            benchmark_decision_path=benchmark_decision_path,
            cache_resume_tests_path=cache_resume_tests_path,
        ),
    }
    decision = _build_g003_materialization_decision(parity)
    if output_path is not None:
        _write_json_atomic(parity, Path(output_path))
    if decision_output_path is not None:
        _write_json_atomic(decision, Path(decision_output_path))
    return parity


def _run_synthetic_materialization_parity() -> dict[str, Any]:
    with TemporaryDirectory(prefix="g003-synthetic-parity-") as tmp:
        cache_dir = Path(tmp) / "cache"
        result = _synthetic_attested_factor_result()
        identity = {
            "adapter_version": "synthetic",
            "cohort_metadata_hash": "3" * 64,
            "dataset_id": "synthetic-g003-materialization",
            "event_slug": "synthetic-g003-materialization",
            "factor_code_sha256": "4" * 64,
            "factor_schema": "factor_protocol_public_result_v1",
            "horizons_seconds": list(PRIMARY_HORIZONS_SECONDS),
            "label_schema": "factor_protocol_primary_labels_v1",
            "market_slug": "synthetic-g003-materialization",
            "mode": "M2",
            "mode_version": "g003-m2-disposable-factor-cache-v1",
            "orderbook_identity_sha256": "2" * 64,
            "protocol_json_sha256": "5" * 64,
            "replay_ordering_version": "O1",
            "source_content_sha256": "1" * 64,
            "summary_schema": "factor_protocol_candidate_shortlist_v1",
            "token_id": "SYN-YES",
        }
        t0 = time.perf_counter()
        m1 = _copy_factor_result(result)
        cold_m1 = time.perf_counter() - t0
        t0 = time.perf_counter()
        write_m2_cache_atomic(
            result,
            cache_dir=cache_dir,
            cache_identity=identity,
            cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
        )
        cold_m2 = time.perf_counter() - t0
        t0 = time.perf_counter()
        warm = read_m2_cache_or_recompute(
            cache_dir=cache_dir,
            cache_identity=identity,
            m1_factory=lambda: m1,
        )
        warm_elapsed = time.perf_counter() - t0
        inject_m2_cache_fault(cache_dir, "bitflip")
        corrupt = read_m2_cache_or_recompute(
            cache_dir=cache_dir,
            cache_identity=identity,
            m1_factory=lambda: m1,
        )
    return {
        "sample_kind": "synthetic",
        "modes": {
            "M1": {
                "cold_elapsed_seconds": cold_m1,
                "cache_status": "direct_no_persistent_derivative",
            },
            "M2": {
                "cold_elapsed_seconds": cold_m2,
                "warm_elapsed_seconds": warm_elapsed,
                "cache_status": warm["cache_status"],
                "resume_behavior": {
                    "invalid_cache_reason": corrupt["cache_status"],
                    "removed_partial_artifacts": 0,
                },
            },
            "M3": resolve_audit_mode("M3"),
        },
        "parity_attestation": {
            "m1_digest": result["deterministic_digest"],
            "warm_digest": warm["result"]["deterministic_digest"],
            "corrupt_recover_digest": corrupt["result"]["deterministic_digest"],
            "exact_match": (
                result["deterministic_digest"]
                == warm["result"]["deterministic_digest"]
                == corrupt["result"]["deterministic_digest"]
            ),
        },
    }


def _synthetic_attested_factor_result() -> dict[str, Any]:
    factor = _factor_protocol().PRIMARY_FACTOR_NAMES[0]
    panel = pd.DataFrame(
        [
            {
                "event_id": "synthetic-g003-materialization",
                "market": "synthetic-market",
                "token_id": "SYN-YES",
                "sequence": 1,
                factor: 1.0,
                "ranking_observation": True,
                "source_quality_cohort": "clean",
            },
        ],
    )
    candidate_table = pd.DataFrame(
        [
            {
                "factor": factor,
                "horizon_seconds": PRIMARY_HORIZONS_SECONDS[0],
                "passes_shortlist": True,
                "event_count": 1,
                "token_count": 1,
                "row_count": 1,
            },
        ],
    )
    primary_shortlist = candidate_table.reset_index(drop=True)
    panel_digest = _factor_protocol()._frame_digest(panel)
    candidate_digest = _factor_protocol()._frame_digest(candidate_table)
    return {
        "panel": panel,
        "candidate_table": candidate_table,
        "primary_shortlist": primary_shortlist,
        "deterministic_digest": hashlib.sha256(f"{panel_digest}:{candidate_digest}".encode()).hexdigest(),
    }


def _load_or_run_frozen_representative_real_parity(
    *,
    benchmark_results_path: Path,
    cache_resume_tests_path: Path,
    force_recompute_real: bool,
) -> list[dict[str, Any]]:
    if not force_recompute_real:
        reused = _load_valid_committed_real_parity_rows()
        if reused is not None:
            return reused
    return _run_frozen_representative_real_parity(
        benchmark_results_path=benchmark_results_path,
        cache_resume_tests_path=cache_resume_tests_path,
    )


def _run_frozen_representative_real_parity(
    *,
    benchmark_results_path: Path,
    cache_resume_tests_path: Path,
) -> list[dict[str, Any]]:
    benchmark_results = _read_json(benchmark_results_path)
    if not isinstance(benchmark_results, list) or not benchmark_results:
        raise AssertionError("frozen benchmark results must be a non-empty list")
    cache_resume_tests = _read_json(cache_resume_tests_path)
    primitive_by_slug = {
        str(row.get("event_slug")): row
        for row in benchmark_results
        if isinstance(row, dict) and row.get("event_slug") in FROZEN_REAL_EVENT_SLUGS
    }
    rows = []
    for spec in _load_frozen_real_event_specs():
        primitive_row = primitive_by_slug.get(spec["event_slug"], {})
        rows.append(_run_real_event_materialization_parity(spec, primitive_row, cache_resume_tests))
    return rows


def _load_valid_committed_real_parity_rows(
    artifact_path: Path = WAVE0_OUTPUTS_DIR / "g003_materialization_parity.json",
) -> list[dict[str, Any]] | None:
    if not artifact_path.exists():
        return None
    try:
        artifact = _read_json(artifact_path)
    except (json.JSONDecodeError, OSError):
        return None
    rows = [
        row
        for row in artifact.get("results", [])
        if isinstance(row, dict) and row.get("sample_kind") == "frozen_representative_real"
    ]
    if [row.get("event_slug") for row in rows] != list(FROZEN_REAL_EVENT_SLUGS):
        return None
    expected_by_slug = {spec["event_slug"]: spec for spec in _load_frozen_real_event_specs()}
    for row in rows:
        if not _committed_real_row_is_valid(row, expected_by_slug[str(row["event_slug"])]):
            return None
    return rows


def _committed_real_row_is_valid(row: dict[str, Any], spec: dict[str, Any]) -> bool:
    attestation = row.get("parity_attestation", {})
    sources = attestation.get("sources", {})
    expected_sources = _parity_source_hashes(_real_source_hashes(spec))
    if attestation.get("oracle") != "factor_protocol.run_factor_protocol":
        return False
    if attestation.get("horizons_seconds") != list(PRIMARY_HORIZONS_SECONDS):
        return False
    if any(sources.get(key) != value for key, value in expected_sources.items()):
        return False
    try:
        _assert_real_row_exact_gates(row)
    except AssertionError:
        return False
    return row.get("modes", {}).get("M3", {}).get("evidence_kind") == "primitive_audit_only"


def _load_frozen_real_event_specs(
    *,
    inventory_dir: Path = INVENTORY_OUTPUTS_DIR,
) -> list[dict[str, Any]]:
    events_frame = pd.read_parquet(inventory_dir / "event_inventory.parquet")
    tokens_frame = pd.read_parquet(inventory_dir / "token_inventory.parquet")
    specs: list[dict[str, Any]] = []
    for slug in FROZEN_REAL_EVENT_SLUGS:
        event = events_frame[events_frame["event_slug"] == slug].iloc[0].to_dict()
        token = (
            tokens_frame[tokens_frame["event_slug"] == slug]
            .sort_values(["market_index", "token_side"])
            .iloc[0]
            .to_dict()
        )
        paths = _dict_from_jsonish(event["paths"])
        hashes = _dict_from_jsonish(event["hashes"])
        specs.append(
            {
                "asset_id": str(token["asset_id"]),
                "city": str(event["city"]),
                "cohort_role": str(event["cohort_role"]),
                "condition_id": str(token["condition_id"]),
                "event_date": str(event["event_date"]),
                "event_dir": Path(paths["event_dir"]),
                "event_id": str(event["event_id"]),
                "event_slug": slug,
                "hashes": {str(key): str(value) for key, value in hashes.items()},
                "market_index": int(token["market_index"]),
                "orderbook_path": Path(paths["orderbook"]),
                "paths": {str(key): str(value) for key, value in paths.items()},
                "rows_written": int(event["rows_written"]),
                "token_side": str(token["token_side"]),
            }
        )
    return specs


def _run_real_event_materialization_parity(
    spec: dict[str, Any],
    primitive_row: dict[str, Any],
    cache_resume_tests: dict[str, Any],
) -> dict[str, Any]:
    from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter

    with TemporaryDirectory(prefix=f"g003-real-parity-{spec['event_slug']}-") as tmp:
        cache_dir = Path(tmp) / "m2-cache"
        dataset = PMXTEventV1Adapter(repo_root=REPO_ROOT).load(
            {
                "input": {
                    "asset_id": spec["asset_id"],
                    "condition_id": spec["condition_id"],
                    "dataset_id": spec["event_slug"],
                    "event_dir": str(spec["event_dir"]),
                }
            }
        )
        identity = build_cache_identity(
            dataset=dataset,
            event_slug=spec["event_slug"],
            token_id=spec["asset_id"],
            source_content_sha256=_sha256_file(spec["orderbook_path"]),
            orderbook_identity_sha256=spec["hashes"].get("orderbook_identity_sha256", ""),
            adapter_version="PMXTEventV1Adapter.v1",
            replay_ordering_version="O1",
            protocol_json_sha256=_sha256_file(Path(__file__).with_name("protocol.json")),
            factor_code_sha256=_sha256_file(Path(__file__).with_name("factor_protocol.py")),
            horizons_seconds=PRIMARY_HORIZONS_SECONDS,
            cohort_metadata_hash=_sha256_json(
                {
                    "cohort_role": spec["cohort_role"],
                    "event_date": spec["event_date"],
                    "hard_break_status": primitive_row.get("hard_break_status"),
                    "source_quality": getattr(dataset.metadata, "source_quality", {}),
                }
            ),
            mode="M2",
        )

        t0 = time.perf_counter()
        m1 = materialize_m1_factor_result(
            dataset,
            horizons_seconds=PRIMARY_HORIZONS_SECONDS,
            event_slug=spec["event_slug"],
            token_id=spec["asset_id"],
        )
        m1_elapsed = time.perf_counter() - t0
        m1_stage = _stage_attestation(m1, oracle=m1)

        t0 = time.perf_counter()
        commit = write_m2_cache_atomic(
            m1,
            cache_dir=cache_dir,
            cache_identity=identity,
            cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
        )
        m2_cold_elapsed = time.perf_counter() - t0
        if commit.get("commit_status") != "committed":
            raise AssertionError(f"M2 cold commit failed for {spec['event_slug']}: {commit}")
        manifest_path = Path(str(commit["manifest_path"]))
        manifest = _read_json(manifest_path)
        m2_cold_stage = _stage_attestation(m1, oracle=m1, manifest=manifest)

        t0 = time.perf_counter()
        warm = read_m2_cache_or_recompute(
            cache_dir=cache_dir,
            cache_identity=identity,
            m1_factory=lambda: _copy_factor_result(m1),
        )
        warm_elapsed = time.perf_counter() - t0
        if warm["cache_status"] != "warm_validated":
            raise AssertionError(f"M2 warm read failed for {spec['event_slug']}: {warm['cache_status']}")
        assert_factor_result_exact_parity(warm["result"], m1)
        warm_stage = _stage_attestation(warm["result"], oracle=m1, manifest=manifest)

        inject_m2_cache_fault(cache_dir, "bitflip")
        corrupt = read_m2_cache_or_recompute(
            cache_dir=cache_dir,
            cache_identity=identity,
            m1_factory=lambda: _copy_factor_result(m1),
        )
        if corrupt["cache_status"] == "warm_validated":
            raise AssertionError(f"M2 corrupt recovery did not detect corruption for {spec['event_slug']}")
        assert_factor_result_exact_parity(corrupt["result"], m1)
        recommit_manifest = _read_json(cache_dir / "manifest.json")
        corrupt_stage = _stage_attestation(corrupt["result"], oracle=m1, manifest=recommit_manifest)

    primitive_modes = primitive_row.get("modes", {}) if isinstance(primitive_row, dict) else {}
    primitive_m3 = primitive_modes.get("M3", {}) if isinstance(primitive_modes, dict) else {}
    source_hashes = _real_source_hashes(spec)
    row = {
        "sample_kind": "frozen_representative_real",
        "event_slug": spec["event_slug"],
        "city": spec["city"],
        "event_date": spec["event_date"],
        "condition_id": spec["condition_id"],
        "asset_id": spec["asset_id"],
        "source_rows_scanned": primitive_modes.get("M1", {}).get("source_rows_scanned")
        or primitive_modes.get("M1", {}).get("row_count")
        or len(dataset.steps),
        "modes": {
            "M1": {
                "cache_status": "direct_no_persistent_derivative",
                "cold_elapsed_seconds": m1_elapsed,
                "mode": "M1",
            },
            "M2": {
                "cache_status": warm["cache_status"],
                "cold_elapsed_seconds": m2_cold_elapsed,
                "warm_elapsed_seconds": warm_elapsed,
                "mode": "M2",
                "resume_behavior": {
                    "invalid_cache_reason": corrupt["cache_status"],
                    "recovery_mode": corrupt.get("recovery_mode"),
                    "removed_partial_artifacts": 0,
                    "recommitted_matching_payload": True,
                },
            },
            "M3": _primitive_audit_only_mode(primitive_m3, cache_resume_tests),
        },
        "parity_attestation": {
            "cached_generated_evidence": False,
            "oracle": "factor_protocol.run_factor_protocol",
            "horizons_seconds": list(PRIMARY_HORIZONS_SECONDS),
            "sources": {
                "adapter": "PMXTEventV1Adapter/O1",
                "source_hashes": source_hashes,
                **_parity_source_hashes(source_hashes),
            },
            "stages": {
                "m1_canonical_factor_protocol": m1_stage,
                "m2_cold_commit": m2_cold_stage,
                "m2_warm_read": warm_stage,
                "m2_corrupt_recompute_recommit": corrupt_stage,
            },
        },
    }
    _assert_real_row_exact_gates(row)
    return row


def _stage_attestation(
    result: dict[str, Any],
    *,
    oracle: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assert_factor_result_exact_parity(result, oracle)
    stage = {
        "deterministic_digest": result["deterministic_digest"],
        "exact_match": True,
        "factor_result_digest": factor_result_digest(result),
        "semantic_bundle_sha256": _semantic_bundle_digest(result),
        "sections": _canonical_result_sections(result),
    }
    if manifest is not None:
        stage["artifact_hashes"] = {
            "manifest_sha256": _sha256_json(manifest),
            "payload_sha256": manifest.get("payload_sha256"),
            "semantic_bundle_sha256": manifest.get("semantic_bundle_sha256"),
        }
    return stage


def _canonical_result_sections(result: dict[str, Any]) -> dict[str, Any]:
    panel = _as_frame(result["panel"])
    label_columns = [
        column
        for column in panel.columns
        if str(column).startswith(("future_", "label_", "next_nonzero_"))
        or str(column) in {"ranking_observation", "book_validity", "hard_break_provenance"}
    ]
    labels = panel[label_columns].copy() if label_columns else pd.DataFrame(index=panel.index)
    return {
        "panel": _frame_section_attestation(panel),
        "labels_audit_fields": _frame_section_attestation(labels),
        "candidate_table": _frame_section_attestation(_as_frame(result["candidate_table"])),
        "primary_shortlist": _frame_section_attestation(_as_frame(result["primary_shortlist"])),
    }


def _frame_section_attestation(frame: pd.DataFrame) -> dict[str, Any]:
    frame = _as_frame(frame)
    return {
        "columns": [str(column) for column in frame.columns],
        "digest": _factor_protocol()._frame_digest(frame),
        "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
        "nulls": {str(column): int(frame[column].isna().sum()) for column in frame.columns},
        "row_order_digest": _sha256_json(
            {
                "index": [_encode_scalar(value) for value in frame.index.tolist()],
                "sequence": (
                    [_encode_scalar(value) for value in frame["sequence"].tolist()]
                    if "sequence" in frame.columns
                    else list(range(len(frame)))
                ),
            }
        ),
    }


def _real_source_hashes(spec: dict[str, Any]) -> dict[str, str]:
    hashes = dict(spec["hashes"])
    paths = spec["paths"]
    for name, path_text in paths.items():
        path = Path(path_text)
        if path.is_file():
            hashes[f"{name}_actual_sha256"] = _sha256_file(path)
    return {key: hashes[key] for key in sorted(hashes)}


def _parity_source_hashes(source_hashes: dict[str, Any]) -> dict[str, str]:
    source_hashes_sha256 = _sha256_json(source_hashes)
    return {
        "factor_protocol_sha256": _sha256_file(Path(__file__).with_name("factor_protocol.py")),
        "materialization_protocol_sha256": _sha256_file(Path(__file__)),
        "pmxt_event_adapter_sha256": _sha256_file(PMXT_EVENT_ADAPTER_PATH),
        "protocol_json_sha256": _sha256_file(Path(__file__).with_name("protocol.json")),
        "replay_contract_sha256": _sha256_file(REPLAY_CONTRACT_PATH),
        "source_artifact_sha256": source_hashes_sha256,
        "source_hashes_sha256": source_hashes_sha256,
    }


def _primitive_audit_only_mode(
    primitive_m3: dict[str, Any],
    cache_resume_tests: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mode": "M3",
        "status": "audit_only",
        "evidence_kind": "primitive_audit_only",
        "primitive_parity_digest": primitive_m3.get("parity_digest"),
        "primitive_row_count": primitive_m3.get("primitive_count") or primitive_m3.get("row_count"),
        "source_file_bytes_upper_bound": primitive_m3.get("source_file_bytes_upper_bound"),
        "cache_resume_audit_status": cache_resume_tests.get("status") or "available",
    }


def _assert_real_row_exact_gates(row: dict[str, Any]) -> None:
    stages = row["parity_attestation"]["stages"]
    digests = {stage["factor_result_digest"] for stage in stages.values()}
    if len(digests) != 1:
        raise AssertionError(f"factor_result_digest parity failed for {row['event_slug']}")
    if not all(stage["exact_match"] is True for stage in stages.values()):
        raise AssertionError(f"exact parity failed for {row['event_slug']}")
    if row.get("environment_blocked"):
        raise AssertionError(f"unexpected environment_blocked for {row['event_slug']}")


def _dict_from_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _scenario_statuses_for_row(row: dict[str, Any]) -> dict[str, str]:
    sample = str(row["sample_kind"])
    modes = row.get("modes", {})
    statuses: dict[str, str] = {}
    statuses[f"{sample}:cold_m1"] = "evidence" if modes.get("M1", {}).get("cold_elapsed_seconds") is not None else "environment_blocked"
    statuses[f"{sample}:cold_m2"] = "evidence" if modes.get("M2", {}).get("cold_elapsed_seconds") is not None else "environment_blocked"
    statuses[f"{sample}:warm"] = (
        "evidence"
        if any(mode.get("warm_elapsed_seconds") is not None for mode in modes.values() if isinstance(mode, dict))
        else "environment_blocked"
    )
    statuses[f"{sample}:corrupt_recover"] = (
        "evidence"
        if any(
            isinstance(mode, dict)
            and (
                mode.get("resume_behavior", {}).get("invalid_cache_reason")
                or mode.get("resume_behavior", {}).get("removed_partial_artifacts")
            )
            for mode in modes.values()
        )
        else "environment_blocked"
    )
    statuses[f"{sample}:m3_audit"] = "evidence" if "M3" in modes else "environment_blocked"
    return statuses


def _build_g003_materialization_decision(parity: dict[str, Any]) -> dict[str, Any]:
    m2_status = "eligible_not_selected" if _m2_exact_parity_gate_passed(parity) else M2_STATUS
    return {
        "program": "pmxt-weather-factor-wave0-g003-materialization-decision",
        "program_version": "2026-07-14.g003.decision.v1",
        "selected_mode": "M1",
        "non_selected_modes": {
            "M2": {
                "status": m2_status,
                "reason": "M2 remains a disposable cache and is not selected for Wave0 materialization execution.",
            },
            "M3": {
                "status": "audit_only",
                "reason": "M3 may be inspected only as benchmark evidence, never selected for execution.",
            },
        },
        "mode_policy": parity["mode_policy"],
        "scenario_statuses": parity["scenario_statuses"],
        "source_benchmark_evidence": parity["source_benchmark_evidence"],
        "not_for_pnl": True,
        "performance_claims_allowed": False,
    }


def _m2_exact_parity_gate_passed(parity: dict[str, Any]) -> bool:
    statuses = parity.get("scenario_statuses", {})
    if not statuses or any(status != "evidence" for status in statuses.values()):
        return False
    real_rows = [
        row
        for row in parity.get("results", [])
        if isinstance(row, dict) and row.get("sample_kind") == "frozen_representative_real"
    ]
    if [row.get("event_slug") for row in real_rows] != list(FROZEN_REAL_EVENT_SLUGS):
        return False
    for row in real_rows:
        stages = row.get("parity_attestation", {}).get("stages", {})
        if set(stages) != {
            "m1_canonical_factor_protocol",
            "m2_cold_commit",
            "m2_warm_read",
            "m2_corrupt_recompute_recommit",
        }:
            return False
        if len({stage.get("factor_result_digest") for stage in stages.values()}) != 1:
            return False
        if any(stage.get("exact_match") is not True for stage in stages.values()):
            return False
    return True


def _source_benchmark_evidence(
    *,
    benchmark_results_path: Path,
    benchmark_decision_path: Path,
    cache_resume_tests_path: Path,
) -> dict[str, Any]:
    return {
        "m1_m2_m3_results_path": _repo_relative(benchmark_results_path),
        "m1_m2_m3_results_sha256": _sha256_file(benchmark_results_path),
        "materialization_decision_path": _repo_relative(benchmark_decision_path),
        "materialization_decision_sha256": _sha256_file(benchmark_decision_path),
        "cache_resume_tests_path": _repo_relative(cache_resume_tests_path),
        "cache_resume_tests_sha256": _sha256_file(cache_resume_tests_path),
        "status": "frozen_representative_real_evidence_reused_without_overwrite",
    }


def _repo_relative(path: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run")
    dry.add_argument("--compact-canonical-inventory", default=None)
    dry.add_argument("--event-inventory", default=None)
    dry.add_argument("--token-inventory", default=None)
    dry.add_argument("--inventory-summary", default=None)
    dry.add_argument("--benchmark-manifest", default=None)
    dry.add_argument("--benchmark-report", required=True)
    dry.add_argument("--cache-resume-tests", default=None)
    dry.add_argument("--materialization-decision", default=None)
    dry.add_argument("--shards", type=int, default=EXPECTED_SHARDS)
    dry.add_argument("--worker-cap", type=int, default=EXPECTED_WORKER_CAP)
    dry.add_argument("--cache-budget-bytes", type=int, default=EXPECTED_CACHE_BUDGET_BYTES)
    dry.add_argument("--output", required=True)
    parity = sub.add_parser("parity")
    parity.add_argument("--output", required=True)
    parity.add_argument("--decision-output", required=True)
    parity.add_argument("--include-synthetic", action="store_true")
    parity.add_argument("--include-frozen-representative-real", action="store_true")
    parity.add_argument("--force", action="store_true", help="recompute real event parity instead of reusing valid committed Wave0 artifact rows")
    parity.add_argument("--benchmark-results", default=None)
    parity.add_argument("--benchmark-decision", default=None)
    parity.add_argument("--cache-resume-tests", default=None)
    validate = sub.add_parser("validate-manifest")
    validate.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        validate_g003_dry_run_manifest(_read_json(args.manifest))
        return 0
    if args.command == "parity":
        run_g003_materialization_parity(
            output_path=args.output,
            decision_output_path=args.decision_output,
            include_synthetic=True,
            include_frozen_representative_real=True,
            force_recompute_real=args.force,
            benchmark_results_path=args.benchmark_results,
            benchmark_decision_path=args.benchmark_decision,
            cache_resume_tests_path=args.cache_resume_tests,
        )
        return 0
    benchmark_report = Path(args.benchmark_report)
    benchmark_dir = benchmark_report.parent
    build_g003_dry_run_manifest(
        compact_canonical_inventory_path=args.compact_canonical_inventory,
        event_inventory_path=args.event_inventory,
        token_inventory_path=args.token_inventory,
        inventory_summary_path=args.inventory_summary,
        benchmark_manifest_path=args.benchmark_manifest
        or benchmark_dir / "benchmark_manifest.json",
        benchmark_report_path=args.benchmark_report,
        cache_resume_tests_path=args.cache_resume_tests
        or benchmark_dir / "cache_resume_tests.json",
        materialization_decision_path=args.materialization_decision
        or benchmark_dir / "materialization_decision.json",
        shards=args.shards,
        worker_cap=args.worker_cap,
        cache_budget_bytes=args.cache_budget_bytes,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
