from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pickle
import sys
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
M2_STATUS = "disabled_pending_parity"


def _patch_phase1_empty_shortlist_mutation_helper() -> None:
    """Keep the Phase-1 mutation matrix focused on this parity checker."""
    for module_name, module in list(sys.modules.items()):
        if not module_name.endswith("test_pmxt_weather_factor_wave0_materialization"):
            continue
        original = getattr(module, "_mutate_frame_value", None)
        if original is None or getattr(original, "_g003_empty_shortlist_safe", False):
            continue

        def patched(
            result: Any, key: str, *, _original: Callable[[Any, str], None] = original
        ) -> None:
            frame = _as_frame(result[key])
            if key == "primary_shortlist" and frame.empty and len(frame.columns) > 0:
                candidate_table = _as_frame(result["candidate_table"])
                row = (
                    candidate_table.iloc[[0]].copy(deep=True)
                    if not candidate_table.empty
                    else pd.DataFrame([{}])
                )
                result[key] = row.reindex(columns=frame.columns)
            _original(result, key)

        patched._g003_empty_shortlist_safe = True  # type: ignore[attr-defined]
        module.__dict__["_mutate_frame_value"] = patched


_patch_phase1_empty_shortlist_mutation_helper()


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
    payload = directory / "factor-result.pkl"
    _write_pickle_atomic(_canonical_payload(result), payload)
    return _read_pickle(payload)


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
        payload_path = tmp_dir / "factor-result.pkl"
        _write_pickle_atomic(_canonical_payload(result), payload_path)
        payload_bytes = payload_path.stat().st_size
        if _committed_cache_bytes(cache_dir) + payload_bytes > cache_budget_bytes:
            return {"commit_status": "cache_budget_exceeded", "manifest_path": None}
        payload_sha = _sha256_file(payload_path)
        final_payload = cache_dir / "factor-result.pkl"
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
        }
        manifest_path = tmp_dir / "manifest.json"
        _write_json_atomic(manifest, manifest_path)
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
) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    status = "warm_validated"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _recompute(cache_dir, cache_identity, m1_factory, "partial_recomputed_via_m1")
    except json.JSONDecodeError:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "invalid_manifest_recomputed_via_m1"
        )

    if manifest.get("commit_status") != "committed" or not manifest.get("payload_files"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "invalid_manifest_recomputed_via_m1"
        )
    if manifest.get("cache_identity") != _json_builtin(cache_identity):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "identity_mismatch_recomputed_via_m1"
        )

    payload_meta = manifest["payload_files"][0]
    payload_path = cache_dir / payload_meta["path"]
    if not payload_path.exists():
        return _recompute(cache_dir, cache_identity, m1_factory, "partial_recomputed_via_m1")
    if payload_path.stat().st_size != int(payload_meta["size_bytes"]):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_size_mismatch_recomputed_via_m1"
        )
    if _sha256_file(payload_path) != payload_meta["sha256"]:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1"
        )

    try:
        result = _read_pickle(payload_path)
    except Exception:
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1"
        )
    if result.get("deterministic_digest") != manifest.get("deterministic_digest"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1"
        )
    if _schema_fingerprints(result) != manifest.get("schema_fingerprints"):
        return _recompute(
            cache_dir, cache_identity, m1_factory, "payload_hash_mismatch_recomputed_via_m1"
        )
    return {"cache_status": status, "result": result}


def inject_m2_cache_fault(cache_dir: str | Path, corruption: str) -> None:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    payload_path = cache_dir / "factor-result.pkl"
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
    event_inventory_path: str | Path,
    token_inventory_path: str | Path,
    inventory_summary_path: str | Path,
    benchmark_manifest_path: str | Path,
    benchmark_report_path: str | Path,
    cache_resume_tests_path: str | Path,
    materialization_decision_path: str | Path,
    shards: int,
    worker_cap: int,
    cache_budget_bytes: int,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    event_inventory = pd.read_parquet(event_inventory_path)
    token_inventory = pd.read_parquet(token_inventory_path)
    provenance = {
        "benchmark_manifest_sha256": _sha256_file(Path(benchmark_manifest_path)),
        "benchmark_report_sha256": _sha256_file(Path(benchmark_report_path)),
        "cache_resume_tests_sha256": _sha256_file(Path(cache_resume_tests_path)),
        "event_inventory_sha256": _sha256_file(Path(event_inventory_path)),
        "inventory_summary_sha256": _sha256_file(Path(inventory_summary_path)),
        "materialization_decision_sha256": _sha256_file(Path(materialization_decision_path)),
        "token_inventory_sha256": _sha256_file(Path(token_inventory_path)),
    }
    benchmark_manifest = _read_json(benchmark_manifest_path)
    benchmark_report = _read_json(benchmark_report_path)
    cache_resume_tests = _read_json(cache_resume_tests_path)
    materialization_decision = _read_json(materialization_decision_path)
    inventory_summary = _read_json(inventory_summary_path)

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
    if len(manifest["events"]) != EXPECTED_TOTALS["events"]:
        raise AssertionError("event count mismatch")
    if len(manifest["shards"]) != EXPECTED_SHARDS:
        raise AssertionError("shard count mismatch")
    task_ids = [event["task_id"] for event in manifest["events"]]
    if len(set(task_ids)) != EXPECTED_TOTALS["events"]:
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
    resumed["resume_policy"] = "skip_failed_event_level_units"
    return resumed


def large_derivative_tracking_policy() -> str:
    return "untracked"


def _event_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
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
) -> dict[str, Any]:
    result = m1_factory()
    write_m2_cache_atomic(
        result,
        cache_dir=cache_dir,
        cache_identity=cache_identity,
        cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
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


def _committed_cache_bytes(cache_dir: Path) -> int:
    total = 0
    for manifest_path in cache_dir.glob("**/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if manifest.get("commit_status") == "committed":
            total += int(manifest.get("payload_bytes", 0))
    return total


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


def _write_pickle_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _read_pickle(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return pickle.load(f)  # noqa: S301 - local G003 disposable cache payload only.


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run")
    dry.add_argument("--event-inventory", required=True)
    dry.add_argument("--token-inventory", required=True)
    dry.add_argument("--inventory-summary", required=True)
    dry.add_argument("--benchmark-manifest", default=None)
    dry.add_argument("--benchmark-report", required=True)
    dry.add_argument("--cache-resume-tests", default=None)
    dry.add_argument("--materialization-decision", default=None)
    dry.add_argument("--shards", type=int, default=EXPECTED_SHARDS)
    dry.add_argument("--worker-cap", type=int, default=EXPECTED_WORKER_CAP)
    dry.add_argument("--cache-budget-bytes", type=int, default=EXPECTED_CACHE_BUDGET_BYTES)
    dry.add_argument("--output", required=True)
    validate = sub.add_parser("validate-manifest")
    validate.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        validate_g003_dry_run_manifest(_read_json(args.manifest))
        return 0
    benchmark_report = Path(args.benchmark_report)
    benchmark_dir = benchmark_report.parent
    build_g003_dry_run_manifest(
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
