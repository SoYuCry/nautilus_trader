from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "research" / "2026-07-14-pmxt-wave-minus1-materialization-benchmark" / "benchmark_materialization.py"
spec = importlib.util.spec_from_file_location("pmxt_materialization_benchmark", MODULE_PATH)
bench = importlib.util.module_from_spec(spec)
assert spec
assert spec.loader
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)

COND = "0xabc"
ASSET = "token-yes"
OTHER = "token-no"


def _ts(sec: int) -> str:
    return f"2026-01-01T00:00:{sec:02d}.000Z"


def _rows() -> list[dict[str, object]]:
    return [
        {"event_type": "price_change", "market": COND, "asset_id": ASSET, "timestamp": _ts(3), "timestamp_received": _ts(0), "side": "BUY", "price": "0.40", "size": "3", "bids": None, "asks": None, "best_bid": "0.99", "best_ask": "0.01", "old_tick_size": None, "new_tick_size": None, "outcome": "POISON", "pnl": 99},
        {"event_type": "book", "market": COND, "asset_id": ASSET, "timestamp": _ts(1), "timestamp_received": _ts(1), "side": None, "price": None, "size": None, "bids": json.dumps([["0.30", "10"], ["0.20", "5"]]), "asks": json.dumps([["0.70", "9"]]), "best_bid": "0.99", "best_ask": "0.01", "old_tick_size": None, "new_tick_size": None, "settlement": "POISON"},
        {"event_type": "price_change", "market": COND, "asset_id": ASSET, "timestamp": _ts(2), "timestamp_received": _ts(2), "side": "SELL", "price": "0.70", "size": "0", "bids": None, "asks": None, "best_bid": "0.99", "best_ask": "0.01", "old_tick_size": None, "new_tick_size": None},
        {"event_type": "last_trade_price", "market": COND, "asset_id": ASSET, "timestamp": _ts(2), "timestamp_received": _ts(2), "side": "BUY", "price": "0.31", "size": "2", "bids": None, "asks": None, "best_bid": None, "best_ask": None, "old_tick_size": None, "new_tick_size": None},
        {"event_type": "tick_size_change", "market": COND, "asset_id": ASSET, "timestamp": _ts(2), "timestamp_received": _ts(2), "side": None, "price": None, "size": None, "bids": None, "asks": None, "best_bid": None, "best_ask": None, "old_tick_size": "0.01", "new_tick_size": "0.001"},
        {"event_type": "price_change", "market": COND, "asset_id": ASSET, "timestamp": _ts(4), "timestamp_received": _ts(4), "side": "BUY", "price": "0.40", "size": "0", "bids": None, "asks": None, "best_bid": None, "best_ask": None, "old_tick_size": None, "new_tick_size": None},
        {"event_type": "book", "market": COND, "asset_id": OTHER, "timestamp": _ts(0), "timestamp_received": _ts(0), "side": None, "price": None, "size": None, "bids": json.dumps([["0.1", "1"]]), "asks": json.dumps([["0.9", "1"]]), "best_bid": None, "best_ask": None, "old_tick_size": None, "new_tick_size": None},
        {"event_type": "price_change", "market": COND, "asset_id": ASSET, "timestamp": None, "timestamp_received": _ts(5), "side": "SELL", "price": "0.80", "size": "4", "bids": None, "asks": None, "best_bid": None, "best_ask": None, "old_tick_size": None, "new_tick_size": None},
    ]


def _event(tmp_path: Path) -> object:
    event_dir = tmp_path / "event"
    event_dir.mkdir()
    parquet = event_dir / "orderbook.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(_rows()), preserve_index=False), parquet, row_group_size=3)
    return bench.EventSpec("synthetic", "synthetic-event", "2026-01-01", "Testville", event_dir, parquet, COND, ASSET, "source-v1", len(_rows()))


def test_streaming_one_pass_parity_and_semantics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = _event(tmp_path)
    monkeypatch.setattr(pd, "read_parquet", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full pandas parquet read forbidden")))
    calls = 0
    original = bench._stream_selected_rows

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bench, "_stream_selected_rows", counted)
    out = tmp_path / "out"
    m1 = bench.run_mode(event, bench.Mode.M1, output_dir=out)
    m2 = bench.run_mode(event, bench.Mode.M2, output_dir=out, config={"anchor_every_rows": 2})
    m3 = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert calls == 3
    assert m1.source_pass_count == m2.source_pass_count == m3.source_pass_count == 1
    assert m1.parity_digest == m2.parity_digest == m3.parity_digest
    assert m1.row_count == 7
    assert m1.retained_primitive_record_count == 0
    assert m2.retained_primitive_record_count == 0
    assert m3.retained_primitive_record_count == m3.row_count
    payload = json.loads(m3.artifact_path.read_text(encoding="utf-8"))
    trace = payload["trace"]
    assert [r["source_row_index"] for r in trace] == [1, 2, 3, 4, 0, 5, 7]
    assert trace[0]["best_bid"] == "0.30"
    assert trace[0]["best_ask"] == "0.70"
    assert trace[1]["best_ask"] is None
    assert trace[2]["event_type"] == "trade"
    assert trace[3]["tick_size"] == "0.001"
    assert trace[-1]["timestamp"] == trace[-1]["timestamp_received"]
    assert json.loads(m2.artifact_path.read_text(encoding="utf-8"))["metadata"]["source_pass_count"] == 1


def test_cold_and_warm_timers_cover_commit_and_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = _event(tmp_path)
    out = tmp_path / "out"
    original_write = bench._write_cache_payload

    def slow_write(*args: object, **kwargs: object) -> object:
        time.sleep(0.02)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(bench, "_write_cache_payload", slow_write)
    cold = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert cold.cold_elapsed_seconds >= 0.02
    original_read = bench._read_cache_payload

    def slow_read(*args: object, **kwargs: object) -> object:
        time.sleep(0.02)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(bench, "_read_cache_payload", slow_read)
    warm = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert warm.warm_elapsed_seconds >= 0.02
    assert warm.source_pass_count == 0


def test_cache_identity_invalidators_and_corruption_resume(tmp_path: Path) -> None:
    event = _event(tmp_path)
    base = bench.DEFAULT_CACHE_CONTRACT
    base_key = bench._cache_key(event, bench.Mode.M2, {"anchor_every_rows": 2}, contract=base)
    for field in ("protocol_hash", "ordering_domain", "ordering_version", "kernel_schema", "kernel_version", "label_schema", "label_schema_version"):
        assert bench._cache_key(event, bench.Mode.M2, {"anchor_every_rows": 2}, contract=replace(base, **{field: getattr(base, field) + "-changed"})) != base_key
    assert bench._cache_key(replace(event, source_hash="path-size-mtime-identity-v2"), bench.Mode.M2, {"anchor_every_rows": 2}) == base_key
    for changed in (
        replace(event, event_slug="other-event"),
        replace(event, condition_id="0xdef"), replace(event, asset_id="other-token"),
    ):
        assert bench._cache_key(changed, bench.Mode.M2, {"anchor_every_rows": 2}) != base_key
    assert bench._cache_key(event, bench.Mode.M3, {"anchor_every_rows": 2}) != base_key
    assert bench._cache_key(event, bench.Mode.M2, {"anchor_every_rows": 3}) != base_key

    out = tmp_path / "out"
    with pytest.raises(bench.IntentionalInterruption):
        bench.run_mode(event, bench.Mode.M3, output_dir=out, config={"fail_after_rows": 3})
    resumed = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert resumed.resume_behavior["ignored_partial_artifacts"] >= 1
    checks = bench.run_cache_resume_tests(event, tmp_path / "checks")
    for key in ("bitflip_detected", "truncate_detected", "manifest_missing_detected", "manifest_corrupt_detected", "partial_reuse_prevented"):
        assert checks[key] is True


def test_cache_identity_uses_source_bytes_not_size_or_mtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = _event(tmp_path)
    out = tmp_path / "out"
    base = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert base.cache_status == "cold_committed"
    warm = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert warm.cache_key == base.cache_key
    assert warm.cache_status == "warm_reused_committed_matching_payload"
    before = event.orderbook_path.stat()
    original = event.orderbook_path.read_bytes()
    changed = bytearray(original)
    changed[len(changed) // 2] ^= 1
    event.orderbook_path.write_bytes(changed)
    os.utime(event.orderbook_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = event.orderbook_path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns

    calls = 0

    def fake_materialize(changed_event: object, mode: object, config: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "scan": bench.SourceScan([], changed_event.orderbook_path.stat().st_size, 0, 0, 0),
            "trace": [],
            "anchors": [],
            "parity_digest": "recomputed-after-source-byte-change",
            "ordering_digest": "recomputed-after-source-byte-change",
            "ordering_checkpoints": [],
            "semantic_checkpoints": [],
            "event_type_counts": {},
            "row_count": 0,
            "final_state": {"bids": [], "asks": [], "tick_size": None},
            "retained_primitive_record_count": 0,
        }

    monkeypatch.setattr(bench, "_materialize_mode", fake_materialize)
    recomputed = bench.run_mode(event, bench.Mode.M3, output_dir=out)
    assert calls == 1
    assert recomputed.cache_key != base.cache_key
    assert recomputed.cache_status == "cold_committed"
    assert recomputed.source_pass_count == 1
    assert recomputed.warm_elapsed_seconds is None
    assert recomputed.parity_digest == "recomputed-after-source-byte-change"
    assert bench._cache_identity(event, bench.Mode.M3, {})["source_content_sha256"] == bench.sha256_file(event.orderbook_path)


def test_metrics_rss_io_oracle_invariance_and_portability(tmp_path: Path) -> None:
    event = _event(tmp_path)
    out = tmp_path / "out"
    report = bench.run_benchmark([event], output_dir=out, include_complete_date=False)
    metrics = report["m1_m2_m3_results"][0]["modes"]["M3"]
    for field in bench.REQUIRED_METRIC_FIELDS:
        assert field in metrics
    assert metrics["source_file_bytes_upper_bound"] == event.orderbook_path.stat().st_size
    assert metrics["source_pass_count"] == 1
    assert "bytes_read" not in metrics
    assert "peak_memory_bytes" not in metrics
    assert metrics["peak_rss_bytes"] > 0
    assert metrics["peak_rss_scope"] == "sampled_process_working_set_during_call"
    oracle = report["parity_oracles"]
    assert oracle["g004_ordering_scope"] == "O1_only"
    assert oracle["m1_audit"][0]["ordering_checkpoints_match"] is True
    assert oracle["m1_audit"][0]["derived_semantics_match"] is True
    mode_audit = oracle["mode_audits"][0]["modes"]
    assert set(mode_audit) == {"M1", "M2", "M3"}
    assert {audit["status"] for audit in mode_audit.values()} == {"pass"}
    assert mode_audit["M2"]["all_anchors_match"] is True
    assert mode_audit["M3"]["exact_trace_match"] is True
    assert oracle["g002_o3_external_evidence"]["scope"] == "external_reference_only_not_rerun_not_claimed_by_g004"
    invariance = report["outcome_blind_invariance_g004"]
    assert invariance["equivalent"] is True
    assert invariance["raw_inputs_distinct"] is True
    assert invariance["raw_hash_unique_count"] == 3
    assert len({run["raw_input_hash"] for run in invariance["runs"]}) == 3
    assert len({run["sanitized_input_hash"] for run in invariance["runs"]}) == 1
    assert len({run["decision_output_hash"] for run in invariance["runs"]}) == 1
    serialized = json.dumps(report)
    assert "C:\\" not in serialized
    assert report["materialization_decision"]["status"] == "deferred"

    failed = deepcopy(report)
    failed["parity_oracles"]["mode_audits"][0]["modes"]["M2"]["status"] = "fail"
    with pytest.raises(ValueError, match="decoded M1/M2/M3 oracle audit"):
        bench.validate_report_schema(failed)


def test_forbidden_columns_not_read_or_emitted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = _event(tmp_path)
    seen_columns: list[str] = []
    original = pq.ParquetFile.iter_batches

    def spy(self: object, *args: object, **kwargs: object) -> object:
        seen_columns.extend(kwargs["columns"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)
    result = bench.run_mode(event, bench.Mode.M3, output_dir=tmp_path / "out")
    assert set(seen_columns) == set(bench.ORDERBOOK_COLUMNS)
    assert not ({"best_bid", "best_ask", "outcome", "settlement", "pnl"} & set(seen_columns))
    payload = result.artifact_path.read_text(encoding="utf-8").lower()
    assert "poison" not in payload


def test_complete_date_manifest_projection_and_g002_boundary(tmp_path: Path) -> None:
    sample = bench.load_representative_sample()
    slugs = sample["complete_date"]["event_slugs"]
    result = bench.complete_date_probe([], expected_task_ids=slugs, representative_m3_to_source_ratio=0.5)
    assert result["status"] == "deferred"
    assert result["expected_task_count"] == result["manifest_feasibility_only_count"] == 49
    assert len(result["task_statuses"]) == 49
    assert [item["task_id"] for item in result["task_statuses"]] == slugs
    assert {item["status"] for item in result["task_statuses"]} == {"manifest_feasibility_only"}
    assert all("measured_rows" not in item and "measured_bytes" not in item for item in result["task_statuses"])
    ambiguity = sample["ambiguity_representative"]
    assert ambiguity["source"] == "G002_bounded_real_ordering_parity_external_representative"
    assert ambiguity["not_global_heaviest"] is True
