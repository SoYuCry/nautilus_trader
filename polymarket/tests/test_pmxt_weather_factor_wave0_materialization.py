from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_HELPERS = Path(__file__).with_name("test_pmxt_weather_factor_wave0_protocol.py")
RESEARCH_DIR = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-14-pmxt-weather-factor-wave0"
)
MATERIALIZATION_PROTOCOL = RESEARCH_DIR / "materialization_protocol.py"
FACTOR_PROTOCOL = RESEARCH_DIR / "factor_protocol.py"
PROTOCOL_JSON = RESEARCH_DIR / "protocol.json"
INVENTORY_DIR = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-13-pmxt-wave-minus1-inventory"
    / "outputs"
)
BENCHMARK_DIR = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-14-pmxt-wave-minus1-materialization-benchmark"
    / "outputs"
)

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
EXPECTED_HORIZONS = (30, 120, 600)
SOURCE_EVENT_SLUG = "highest-temperature-in-hong-kong-on-june-10-2026"
SOURCE_MARKET_TOKEN = "E1-YES"  # noqa: S105 - synthetic Polymarket token id, not a password.


@pytest.fixture(scope="module")
def protocol_helpers() -> Any:
    spec = importlib.util.spec_from_file_location("pmxt_weather_factor_wave0_test_helpers", TEST_HELPERS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def materialization_protocol() -> Any:
    assert MATERIALIZATION_PROTOCOL.exists(), (
        "Expected G003 Phase 1 RED tests to drive creation of "
        "polymarket/research/2026-07-14-pmxt-weather-factor-wave0/materialization_protocol.py"
    )
    spec = importlib.util.spec_from_file_location("pmxt_weather_factor_wave0_materialization", MATERIALIZATION_PROTOCOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def factor_protocol() -> Any:
    spec = importlib.util.spec_from_file_location("pmxt_weather_factor_wave0_protocol_for_materialization", FACTOR_PROTOCOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def synthetic_dataset(protocol_helpers: Any) -> Any:
    return protocol_helpers._base_rows(event_id=SOURCE_EVENT_SLUG, token_id=SOURCE_MARKET_TOKEN)


def test_m1_public_factor_result_matches_factor_protocol_oracle_exactly(
    materialization_protocol: Any,
    factor_protocol: Any,
    synthetic_dataset: Any,
) -> None:
    """M1 is only a public wrapper around run_factor_protocol; it is the oracle."""
    expected = factor_protocol.run_factor_protocol(synthetic_dataset, horizons_seconds=EXPECTED_HORIZONS)

    actual = materialization_protocol.materialize_m1_factor_result(
        synthetic_dataset,
        horizons_seconds=EXPECTED_HORIZONS,
        event_slug=SOURCE_EVENT_SLUG,
        token_id=SOURCE_MARKET_TOKEN,
    )

    _assert_factor_results_equal(actual, expected)
    assert _result_get(actual, "mode") == "M1"
    assert _result_get(actual, "oracle") == "factor_protocol.run_factor_protocol"


def test_m1_never_reads_m2_or_m3_when_building_oracle(
    monkeypatch: pytest.MonkeyPatch,
    materialization_protocol: Any,
    synthetic_dataset: Any,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("M1 oracle must not read M2 cache or M3 traces")

    for name in ("read_m2_cache", "read_m3_trace", "load_materialized_result"):
        if hasattr(materialization_protocol, name):
            monkeypatch.setattr(materialization_protocol, name, forbidden)

    result = materialization_protocol.materialize_m1_factor_result(
        synthetic_dataset,
        horizons_seconds=EXPECTED_HORIZONS,
        event_slug=SOURCE_EVENT_SLUG,
        token_id=SOURCE_MARKET_TOKEN,
    )

    assert _result_get(result, "mode") == "M1"
    assert _result_get(result, "deterministic_digest")


def test_canonical_result_round_trip_preserves_panel_candidate_shortlist_dtypes_index_order_nulls_and_digest(
    materialization_protocol: Any,
    factor_protocol: Any,
    synthetic_dataset: Any,
    tmp_path: Path,
) -> None:
    """Exact parity means no tolerance, no dtype relaxation, and no row/column reordering."""
    m1 = factor_protocol.run_factor_protocol(synthetic_dataset, horizons_seconds=EXPECTED_HORIZONS)

    round_tripped = materialization_protocol.canonical_factor_result_round_trip(
        m1,
        tmp_path / "canonical-result",
    )

    _assert_factor_results_equal(round_tripped, m1)
    assert materialization_protocol.factor_result_digest(round_tripped) == materialization_protocol.factor_result_digest(m1)


def test_strict_parity_rejects_value_null_dtype_index_order_and_digest_drift(
    materialization_protocol: Any,
    factor_protocol: Any,
    synthetic_dataset: Any,
) -> None:
    base = factor_protocol.run_factor_protocol(synthetic_dataset, horizons_seconds=EXPECTED_HORIZONS)

    mutations = [
        ("panel_value", lambda r: _mutate_frame_value(r, "panel")),
        ("panel_null", lambda r: _mutate_frame_null(r, "panel")),
        ("panel_dtype", lambda r: _mutate_frame_dtype(r, "panel")),
        ("panel_order", lambda r: _mutate_frame_order(r, "panel")),
        ("panel_index", lambda r: _mutate_frame_index(r, "panel")),
        ("candidate_value", lambda r: _mutate_frame_value(r, "candidate_table")),
        ("shortlist_value", lambda r: _mutate_frame_value(r, "primary_shortlist")),
        ("digest", _mutate_digest),
    ]

    for mutation_name, mutate in mutations:
        candidate = _copy_factor_result(base)
        mutate(candidate)
        with pytest.raises(AssertionError, match=mutation_name.split("_")[0]):
            materialization_protocol.assert_factor_result_exact_parity(candidate, base)


def test_m2_cache_identity_manifest_and_warm_read_are_exact_and_source_bound(
    materialization_protocol: Any,
    factor_protocol: Any,
    synthetic_dataset: Any,
    tmp_path: Path,
) -> None:
    m1 = factor_protocol.run_factor_protocol(synthetic_dataset, horizons_seconds=EXPECTED_HORIZONS)
    identity = _synthetic_cache_identity(materialization_protocol, synthetic_dataset)

    commit = materialization_protocol.write_m2_cache_atomic(
        m1,
        cache_dir=tmp_path,
        cache_identity=identity,
        cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
    )
    assert _result_get(commit, "commit_status") == "committed"
    manifest = _read_json(Path(_result_get(commit, "manifest_path")))
    _assert_cache_manifest_is_complete(manifest, identity)

    warm = materialization_protocol.read_m2_cache_or_recompute(
        cache_dir=tmp_path,
        cache_identity=identity,
        m1_factory=lambda: pytest.fail("warm M2 read must not recompute through M1"),
    )
    assert _result_get(warm, "cache_status") == "warm_validated"
    _assert_factor_results_equal(_result_get(warm, "result"), m1)


@pytest.mark.parametrize(
    ("corruption", "expected_status"),
    [
        ("interrupted", "partial_recomputed_via_m1"),
        ("corrupt_manifest", "invalid_manifest_recomputed_via_m1"),
        ("bitflip", "payload_hash_mismatch_recomputed_via_m1"),
        ("truncate", "payload_size_mismatch_recomputed_via_m1"),
        ("stale_protocol", "identity_mismatch_recomputed_via_m1"),
    ],
)
def test_m2_invalid_or_partial_cache_never_returns_stale_data_and_recovers_via_m1(
    materialization_protocol: Any,
    factor_protocol: Any,
    synthetic_dataset: Any,
    tmp_path: Path,
    corruption: str,
    expected_status: str,
) -> None:
    original = factor_protocol.run_factor_protocol(synthetic_dataset, horizons_seconds=EXPECTED_HORIZONS)
    identity = _synthetic_cache_identity(materialization_protocol, synthetic_dataset)
    materialization_protocol.write_m2_cache_atomic(
        original,
        cache_dir=tmp_path,
        cache_identity=identity,
        cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
    )
    materialization_protocol.inject_m2_cache_fault(tmp_path, corruption)

    recompute_calls = 0

    def recompute() -> Any:
        nonlocal recompute_calls
        recompute_calls += 1
        return original

    recovered = materialization_protocol.read_m2_cache_or_recompute(
        cache_dir=tmp_path,
        cache_identity=identity,
        m1_factory=recompute,
    )

    assert recompute_calls == 1
    assert _result_get(recovered, "cache_status") == expected_status
    assert _result_get(recovered, "recovery_mode") == "M1"
    _assert_factor_results_equal(_result_get(recovered, "result"), original)


def test_cache_identity_change_recomputes_only_affected_event_token(
    materialization_protocol: Any,
    factor_protocol: Any,
    synthetic_dataset: Any,
    tmp_path: Path,
) -> None:
    original = factor_protocol.run_factor_protocol(synthetic_dataset, horizons_seconds=EXPECTED_HORIZONS)
    identity = _synthetic_cache_identity(materialization_protocol, synthetic_dataset)
    materialization_protocol.write_m2_cache_atomic(
        original,
        cache_dir=tmp_path / "event-token-a",
        cache_identity=identity,
        cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
    )
    materialization_protocol.write_m2_cache_atomic(
        original,
        cache_dir=tmp_path / "event-token-b",
        cache_identity={**identity, "token_id": "E1-NO"},
        cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
    )

    calls = {"a": 0, "b": 0}

    recovered_a = materialization_protocol.read_m2_cache_or_recompute(
        cache_dir=tmp_path / "event-token-a",
        cache_identity={**identity, "protocol_json_sha256": "0" * 64},
        m1_factory=lambda: calls.__setitem__("a", calls["a"] + 1) or original,
    )
    recovered_b = materialization_protocol.read_m2_cache_or_recompute(
        cache_dir=tmp_path / "event-token-b",
        cache_identity={**identity, "token_id": "E1-NO"},
        m1_factory=lambda: calls.__setitem__("b", calls["b"] + 1) or original,
    )

    assert _result_get(recovered_a, "cache_status") == "identity_mismatch_recomputed_via_m1"
    assert _result_get(recovered_b, "cache_status") == "warm_validated"
    assert calls == {"a": 1, "b": 0}


def test_m3_is_rejected_as_execution_mode_and_allowed_only_for_audit(
    materialization_protocol: Any,
) -> None:
    with pytest.raises(ValueError, match=r"(?i)M3.*audit"):
        materialization_protocol.resolve_execution_mode("M3")

    audit = materialization_protocol.resolve_audit_mode("M3")
    assert _result_get(audit, "mode") == "M3"
    assert _result_get(audit, "status") == "audit_only"


def test_g003_dry_run_reads_only_tracked_inventory_artifacts_and_has_exact_totals(
    monkeypatch: pytest.MonkeyPatch,
    materialization_protocol: Any,
    tmp_path: Path,
) -> None:
    allowed_parquet = {
        (INVENTORY_DIR / "event_inventory.parquet").resolve(),
        (INVENTORY_DIR / "token_inventory.parquet").resolve(),
    }
    read_parquet_paths: list[Path] = []
    original_read_parquet = pd.read_parquet

    def guarded_read_parquet(path: object, *args: object, **kwargs: object) -> pd.DataFrame:
        resolved = Path(path).resolve()
        read_parquet_paths.append(resolved)
        assert resolved in allowed_parquet, f"dry-run must not read source parquet {resolved}"
        return original_read_parquet(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded_read_parquet)

    manifest = _build_dry_run_manifest(materialization_protocol, tmp_path / "dry-run.json")

    assert set(read_parquet_paths) == allowed_parquet
    _assert_dry_run_totals(manifest)
    assert _manifest_digest(manifest) == _manifest_digest(_build_dry_run_manifest(materialization_protocol, tmp_path / "dry-run-2.json"))


def test_g003_dry_run_has_18_unique_row_balanced_shards_and_event_level_resume_units(
    materialization_protocol: Any,
    tmp_path: Path,
) -> None:
    manifest = _build_dry_run_manifest(materialization_protocol, tmp_path / "dry-run.json")
    shards = _manifest_shards(manifest)
    events = _manifest_events(manifest)

    assert len(shards) == EXPECTED_SHARDS
    event_ids = [_event_field(event, "task_id") for event in events]
    assert len(event_ids) == EXPECTED_TOTALS["events"]
    assert len(event_ids) == len(set(event_ids))

    shard_event_ids = [
        _event_field(event, "task_id")
        for shard in shards
        for event in _result_get(shard, "events")
    ]
    assert sorted(shard_event_ids) == sorted(event_ids)

    rows_by_shard = [_result_get(shard, "source_rows") for shard in shards]
    largest_event_rows = max(_event_field(event, "source_rows") for event in events)
    assert max(rows_by_shard) - min(rows_by_shard) <= largest_event_rows

    for event in events:
        assert _event_field(event, "failure_isolation_unit") == "event"
        assert _event_field(event, "initial_state") == "pending"
        assert _event_field(event, "expected_mode") == "M1"


def test_g003_dry_run_records_exact_cohorts_resource_envelope_modes_and_single_read_deferral(
    materialization_protocol: Any,
    tmp_path: Path,
) -> None:
    manifest = _build_dry_run_manifest(materialization_protocol, tmp_path / "dry-run.json")

    assert _result_get(manifest, "worker_cap") == EXPECTED_WORKER_CAP
    assert _result_get(manifest, "cache_budget_bytes") == EXPECTED_CACHE_BUDGET_BYTES
    assert _result_get(manifest, "mode_policy") == {
        "M1": "oracle",
        "M2": _result_get(manifest, "mode_policy")["M2"],
        "M3": "audit_only",
    }
    assert _result_get(manifest, "mode_policy")["M2"] in {"disabled_pending_parity", "eligible_not_selected"}
    assert _result_get(manifest, "event_level_single_read") == {
        "enabled": False,
        "status": "deferred_pending_exact_parity",
    }
    assert _result_get(manifest, "cohort_totals") == EXPECTED_COHORT_TOTALS


def test_event_level_failure_isolation_and_resume_are_deterministic(
    materialization_protocol: Any,
    tmp_path: Path,
) -> None:
    manifest = _build_dry_run_manifest(materialization_protocol, tmp_path / "dry-run.json")
    events = _manifest_events(manifest)
    failed_task_id = _event_field(events[len(events) // 2], "task_id")

    failed = materialization_protocol.record_event_failure(
        manifest,
        task_id=failed_task_id,
        reason="synthetic failure",
    )
    resumed_once = materialization_protocol.build_resume_manifest(failed)
    resumed_twice = materialization_protocol.build_resume_manifest(failed)

    failed_events = [
        event
        for event in _manifest_events(failed)
        if _event_field(event, "state") == "failed"
    ]
    assert [_event_field(event, "task_id") for event in failed_events] == [failed_task_id]
    assert len(_manifest_events(resumed_once)) == EXPECTED_TOTALS["events"] - 1
    assert _manifest_digest(resumed_once) == _manifest_digest(resumed_twice)


def test_large_derivatives_are_not_tracked_by_g003_materialization_contract(
    materialization_protocol: Any,
) -> None:
    git = shutil.which("git")
    assert git is not None
    tracked = subprocess.run(  # noqa: S603 - fixed git command with static args in test.
        [
            git,
            "ls-files",
            str(RESEARCH_DIR / "outputs"),
        ],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()

    forbidden_suffixes = (
        ".parquet",
        ".feather",
        ".arrow",
        ".pkl",
        ".pickle",
        ".partial",
    )
    forbidden_fragments = ("/cache", "\\cache", "/shard", "\\shard", "/trace", "\\trace", "/panel", "\\panel")
    assert not [
        path
        for path in tracked
        if path.endswith(forbidden_suffixes) or any(fragment in path for fragment in forbidden_fragments)
    ]
    assert materialization_protocol.large_derivative_tracking_policy() == "untracked"


def _build_dry_run_manifest(module: Any, output_path: Path) -> Any:
    manifest = module.build_g003_dry_run_manifest(
        event_inventory_path=INVENTORY_DIR / "event_inventory.parquet",
        token_inventory_path=INVENTORY_DIR / "token_inventory.parquet",
        inventory_summary_path=INVENTORY_DIR / "inventory_summary.json",
        benchmark_manifest_path=BENCHMARK_DIR / "benchmark_manifest.json",
        benchmark_report_path=BENCHMARK_DIR / "benchmark_report.json",
        cache_resume_tests_path=BENCHMARK_DIR / "cache_resume_tests.json",
        materialization_decision_path=BENCHMARK_DIR / "materialization_decision.json",
        shards=EXPECTED_SHARDS,
        worker_cap=EXPECTED_WORKER_CAP,
        cache_budget_bytes=EXPECTED_CACHE_BUDGET_BYTES,
        output_path=output_path,
    )
    module.validate_g003_dry_run_manifest(manifest)
    return manifest


def _assert_factor_results_equal(left: Any, right: Any) -> None:
    for key in ("panel", "candidate_table", "primary_shortlist"):
        pd.testing.assert_frame_equal(
            _as_frame(_result_get(left, key)),
            _as_frame(_result_get(right, key)),
            check_exact=True,
            check_dtype=True,
            check_index_type=True,
            check_column_type=True,
            check_like=False,
        )
    assert _result_get(left, "deterministic_digest") == _result_get(right, "deterministic_digest")


def _assert_cache_manifest_is_complete(manifest: dict[str, Any], identity: dict[str, Any]) -> None:
    assert manifest["commit_status"] == "committed"
    assert manifest["cache_identity"] == identity
    for key in (
        "payload_files",
        "payload_sha256",
        "payload_bytes",
        "panel_rows",
        "schema_fingerprints",
        "deterministic_digest",
    ):
        assert manifest[key]
    for payload in manifest["payload_files"]:
        assert payload["path"]
        assert payload["size_bytes"] > 0
        assert len(payload["sha256"]) == 64


def _assert_dry_run_totals(manifest: Any) -> None:
    assert _result_get(manifest, "totals") == EXPECTED_TOTALS
    assert len(_manifest_events(manifest)) == EXPECTED_TOTALS["events"]
    assert len(_manifest_shards(manifest)) == EXPECTED_SHARDS


def _synthetic_cache_identity(module: Any, dataset: Any) -> dict[str, Any]:
    return module.build_cache_identity(
        dataset=dataset,
        event_slug=SOURCE_EVENT_SLUG,
        token_id=SOURCE_MARKET_TOKEN,
        source_content_sha256="1" * 64,
        orderbook_identity_sha256="2" * 64,
        adapter_version="PMXTEventV1Adapter",
        replay_ordering_version="O1",
        protocol_json_sha256=_sha256_file(PROTOCOL_JSON),
        factor_code_sha256=_sha256_file(FACTOR_PROTOCOL),
        horizons_seconds=EXPECTED_HORIZONS,
        cohort_metadata_hash="3" * 64,
        mode="M2",
    )


def _manifest_digest(manifest: Any) -> str:
    payload = json.dumps(_to_builtin(manifest), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_events(manifest: Any) -> list[Any]:
    return list(_result_get(manifest, "events"))


def _manifest_shards(manifest: Any) -> list[Any]:
    return list(_result_get(manifest, "shards"))


def _event_field(event: Any, key: str) -> Any:
    if key == "source_rows":
        return _result_get(event, "source_rows")
    return _result_get(event, key)


def _result_get(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result[key]
    return getattr(result, key)


def _as_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value
    return pd.DataFrame(value)


def _copy_factor_result(result: Any) -> Any:
    if isinstance(result, dict):
        copied = copy.deepcopy(result)
        for key in ("panel", "candidate_table", "primary_shortlist"):
            copied[key] = _as_frame(result[key]).copy(deep=True)
        return copied
    copied = copy.copy(result)
    for key in ("panel", "candidate_table", "primary_shortlist"):
        setattr(copied, key, _as_frame(getattr(result, key)).copy(deep=True))
    return copied


def _set_result_value(result: Any, key: str, value: Any) -> None:
    if isinstance(result, dict):
        result[key] = value
    else:
        setattr(result, key, value)


def _mutate_frame_value(result: Any, key: str) -> None:
    frame = _as_frame(_result_get(result, key)).copy(deep=True)
    column = next(column for column in frame.columns if pd.api.types.is_numeric_dtype(frame[column]))
    frame.loc[frame.index[0], column] = frame.loc[frame.index[0], column] + 1
    _set_result_value(result, key, frame)


def _mutate_frame_null(result: Any, key: str) -> None:
    frame = _as_frame(_result_get(result, key)).copy(deep=True)
    frame.loc[frame.index[0], frame.columns[0]] = None
    _set_result_value(result, key, frame)


def _mutate_frame_dtype(result: Any, key: str) -> None:
    frame = _as_frame(_result_get(result, key)).copy(deep=True)
    column = next(column for column in frame.columns if pd.api.types.is_integer_dtype(frame[column]))
    frame[column] = frame[column].astype("float64")
    _set_result_value(result, key, frame)


def _mutate_frame_order(result: Any, key: str) -> None:
    frame = _as_frame(_result_get(result, key)).copy(deep=True)
    _set_result_value(result, key, frame.iloc[::-1])


def _mutate_frame_index(result: Any, key: str) -> None:
    frame = _as_frame(_result_get(result, key)).copy(deep=True)
    frame.index = pd.Index(range(100, 100 + len(frame)))
    _set_result_value(result, key, frame)


def _mutate_digest(result: Any) -> None:
    _set_result_value(result, "deterministic_digest", "0" * 64)


def _to_builtin(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _to_builtin(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_builtin(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _to_builtin(vars(value))
    return value
