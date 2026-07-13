from __future__ import annotations

import importlib.util
import json
from collections import Counter
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/inventory.py"
REAL_ROOT = Path("C:/Projects/PolyReaper/data/curated/polymarket/events")


def _load_module():
    spec = importlib.util.spec_from_file_location("pmxt_wave_minus1_inventory", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


def _parse_source_utc(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _source_manifest_totals(root: Path) -> dict[str, Any]:
    rows_written_total = 0
    event_type_counts: Counter[str] = Counter()
    event_local_missing_events = 0
    event_count = 0

    for event_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = json.loads((event_dir / "manifest.json").read_text(encoding="utf-8-sig"))
        event_index = json.loads((event_dir / "event_index.json").read_text(encoding="utf-8-sig"))
        event_start = _parse_source_utc(event_index["startDate"])
        event_end = _parse_source_utc(event_index["endDate"])

        rows_written_total += int(manifest.get("rowsWritten") or 0)
        event_type_counts.update(
            {str(event_type): int(count) for event_type, count in (manifest.get("eventTypeCounts") or {}).items()},
        )
        missing_hours = {str(value)[:13] for value in manifest.get("missingHours") or []}
        if any(
            (hour_start := datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)) < event_end
            and event_start < hour_start + timedelta(hours=1)
            for hour in missing_hours
        ):
            event_local_missing_events += 1
        event_count += 1

    return {
        "event_count": event_count,
        "rows_written_total": rows_written_total,
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "event_local_missing_events": event_local_missing_events,
    }


def _write_bundle(
    root: Path,
    slug: str,
    *,
    city: str = "Amsterdam",
    start: str = "2026-06-04T10:00:00Z",
    end: str = "2026-06-04T12:00:00Z",
    missing_hours: list[str] | None = None,
    known_corrupt_hour: str | None = None,
    contract_version: str = "synthetic-v1",
    schema_fingerprint: str = "schema-1",
    extra_manifest_fields: dict[str, Any] | None = None,
) -> Path:
    event_dir = root / slug
    event_dir.mkdir(parents=True)
    condition_id = f"0x{slug[:8]:0<8}"
    yes_token = f"yes-{slug}"
    no_token = f"no-{slug}"
    (event_dir / "event_index.json").write_text(
        json.dumps(
            {
                "eventSlug": slug,
                "eventId": slug,
                "title": slug,
                "city": city,
                "dateText": "June 4",
                "startDate": start,
                "endDate": end,
                "marketsTotal": 1,
                "markets": [
                    {
                        "index": 0,
                        "conditionId": condition_id,
                        "label": "synthetic",
                        "yesToken": yes_token,
                        "noToken": no_token,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    manifest: dict[str, Any] = {
        "contractVersion": contract_version,
        "eventSlug": slug,
        "orderbookSchemaFingerprint": schema_fingerprint,
        "orderbookSchema": "synthetic-schema",
        "rowsWritten": 3,
        "eventTypeCounts": {"book": 1, "price_change": 2},
        "sourceFilesUsed": [],
        "sourceFilesScanned": [],
        "missingHours": missing_hours or [],
        "badFiles": [],
    }
    if known_corrupt_hour is not None:
        manifest["knownCorruptHour"] = {"hour": known_corrupt_hour, "status": "corrupt"}
        manifest["badFiles"] = [{"hour": known_corrupt_hour, "path": "synthetic", "error": "corrupt"}]
    if extra_manifest_fields:
        manifest.update(extra_manifest_fields)
    (event_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    pd.DataFrame(
        [
            {
                "event_type": "book",
                "market": condition_id,
                "asset_id": yes_token,
                "timestamp_received": start,
                "timestamp": start,
            },
        ],
    ).to_parquet(event_dir / "orderbook.parquet", index=False)
    return event_dir


def test_half_open_missing_hour_intersection_boundaries() -> None:
    event_start = inventory.parse_utc("2026-06-04T10:00:00Z")
    event_end = inventory.parse_utc("2026-06-04T12:00:00Z")

    intervals = inventory.intersect_hours(
        ["2026-06-04T09", "2026-06-04T10", "2026-06-04T11", "2026-06-04T12"],
        event_start,
        event_end,
    )

    assert [item.hour for item in intervals] == ["2026-06-04T10", "2026-06-04T11"]


def test_nonintersecting_missing_and_corrupt_hours_are_provenance_only(tmp_path: Path) -> None:
    event_dir = _write_bundle(
        tmp_path,
        "highest-temperature-in-test-on-june-4-2026",
        missing_hours=["2026-06-04T09", "2026-06-04T12"],
        known_corrupt_hour="2026-06-04T13",
    )

    row, _ = inventory.inventory_one_event(event_dir, root=tmp_path)

    assert row["event_local_missing_hours"] == []
    assert row["event_local_corrupt_hours"] == []
    assert row["nonintersecting_missing_hours"] == ["2026-06-04T09", "2026-06-04T12"]
    assert row["hard_break_provenance"]["has_hard_break"] is False
    assert row["natural_staleness_policy"].startswith("observed_update_gap")


def test_intersecting_missing_and_corrupt_hours_are_hard_break_provenance(tmp_path: Path) -> None:
    event_dir = _write_bundle(
        tmp_path,
        "highest-temperature-in-test-on-june-4-2026",
        missing_hours=["2026-06-04T09", "2026-06-04T10", "2026-06-04T12"],
        known_corrupt_hour="2026-06-04T11",
    )

    row, _ = inventory.inventory_one_event(event_dir, root=tmp_path)

    assert [item["hour"] for item in row["event_local_missing_hours"]] == ["2026-06-04T10"]
    assert [item["hour"] for item in row["event_local_corrupt_hours"]] == ["2026-06-04T11"]
    assert row["hard_break_provenance"]["has_hard_break"] is True
    assert "2026-06-04T09" in row["nonintersecting_missing_hours"]
    assert "2026-06-04T12" in row["nonintersecting_missing_hours"]


def test_synthetic_build_inventory_keeps_current_dates_nonconfirmatory(tmp_path: Path) -> None:
    _write_bundle(tmp_path, "highest-temperature-in-a-on-june-4-2026", city="A")
    _write_bundle(
        tmp_path,
        "highest-temperature-in-b-on-june-6-2026",
        city="B",
        start="2026-06-05T10:00:00Z",
        end="2026-06-06T12:00:00Z",
    )
    result = inventory.build_inventory(root=tmp_path, output_dir=tmp_path / "out", allow_partial=True)

    assert result["summary"]["status"] == "pass"
    assert result["summary"]["performance_claims_allowed"] is False
    assert result["summary"]["current_panel_confirmation_status"] == "not_confirmatory"
    assert {row["cohort_role"] for row in result["event_rows"]} == {
        "primary_development_replication",
        "degraded_robustness",
    }
    event_artifact = tmp_path / "out" / "event_inventory.parquet"
    token_artifact = tmp_path / "out" / "token_inventory.parquet"
    assert pd.read_parquet(event_artifact)["event_slug"].tolist() == [
        "highest-temperature-in-a-on-june-4-2026",
        "highest-temperature-in-b-on-june-6-2026",
    ]
    assert len(pd.read_parquet(token_artifact)) == 4
    assert not (tmp_path / "out" / "event_inventory.json").exists()
    assert not (tmp_path / "out" / "event_inventory.csv").exists()
    assert not (tmp_path / "out" / "token_inventory.json").exists()
    assert not (tmp_path / "out" / "token_inventory.csv").exists()


def test_csv_is_emitted_only_as_parquet_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_path = tmp_path / "event_inventory.parquet"
    monkeypatch.setattr(inventory, "pd", None)

    inventory.write_parquet_fallback(parquet_path, [{"event_slug": "synthetic", "event_type_counts": {"book": 1}}])

    assert not parquet_path.exists()
    assert parquet_path.with_suffix(".csv").exists()
    assert parquet_path.with_suffix(".parquet.unavailable.txt").exists()
    assert not parquet_path.with_suffix(".json").exists()


def test_duplicate_city_date_fails_unless_partial_allowed(tmp_path: Path) -> None:
    _write_bundle(tmp_path, "highest-temperature-in-a-on-june-4-2026", city="Same")
    _write_bundle(tmp_path, "highest-temperature-in-b-on-june-4-2026", city="Same")

    strict = inventory.build_inventory(root=tmp_path, output_dir=tmp_path / "strict")
    partial = inventory.build_inventory(root=tmp_path, output_dir=tmp_path / "partial", allow_partial=True)

    assert strict["summary"]["status"] == "fail"
    assert strict["summary"]["checks"]["no_duplicate_date_city_events"] is False
    assert strict["summary"]["duplicate_date_city_events"] == ["2026-06-04|Same"]
    assert partial["summary"]["status"] == "pass"


def test_schema_and_contract_mismatch_fail_universe_checks(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        "highest-temperature-in-a-on-june-4-2026",
        contract_version="contract-a",
        schema_fingerprint="schema-a",
    )
    _write_bundle(
        tmp_path,
        "highest-temperature-in-b-on-june-5-2026",
        city="B",
        end="2026-06-05T12:00:00Z",
        contract_version="contract-b",
        schema_fingerprint="schema-b",
    )

    result = inventory.build_inventory(root=tmp_path, output_dir=tmp_path / "out", allow_partial=True)

    assert result["summary"]["checks"]["one_contract_version"] is False
    assert result["summary"]["checks"]["one_schema_fingerprint"] is False
    assert result["summary"]["contract_versions"] == ["contract-a", "contract-b"]
    assert result["summary"]["schema_fingerprints"] == ["schema-a", "schema-b"]


def test_deterministic_order_and_identity_hash(tmp_path: Path) -> None:
    root = tmp_path / "events"
    _write_bundle(root, "highest-temperature-in-b-on-june-5-2026", city="B", end="2026-06-05T12:00:00Z")
    _write_bundle(root, "highest-temperature-in-a-on-june-4-2026", city="A")

    first = inventory.build_inventory(root=root, output_dir=tmp_path / "first", allow_partial=True)
    second = inventory.build_inventory(root=root, output_dir=tmp_path / "second", allow_partial=True)

    assert [row["event_slug"] for row in first["event_rows"]] == [
        "highest-temperature-in-a-on-june-4-2026",
        "highest-temperature-in-b-on-june-5-2026",
    ]
    assert [row["event_identity_sha256"] for row in first["event_rows"]] == [
        row["event_identity_sha256"] for row in second["event_rows"]
    ]
    assert (tmp_path / "first" / "inventory_summary.json").read_text(encoding="utf-8").replace(
        first["summary"]["generated_at"],
        "<time>",
    ) == (tmp_path / "second" / "inventory_summary.json").read_text(encoding="utf-8").replace(
        second["summary"]["generated_at"],
        "<time>",
    )


def test_outcome_and_pnl_manifest_fields_do_not_change_quality_outputs(tmp_path: Path) -> None:
    _write_bundle(tmp_path, "highest-temperature-in-a-on-june-4-2026", city="A")
    base = inventory.build_inventory(root=tmp_path, output_dir=tmp_path / "base", allow_partial=True)

    manifest_path = tmp_path / "highest-temperature-in-a-on-june-4-2026" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "outcome": "yes",
            "settlement": {"winner": "yes"},
            "factor_result": {"ic": 1.0},
            "strategy_result": {"orders": 99},
            "fill": {"price": 0.5},
            "fee": 10,
            "position": 100,
            "cash": 200,
            "pnl": 999,
        },
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    perturbed = inventory.build_inventory(root=tmp_path, output_dir=tmp_path / "perturbed", allow_partial=True)

    assert base["quality"] == perturbed["quality"]
    assert base["protocol"]["hard_breaks"] == perturbed["protocol"]["hard_breaks"]
    assert base["event_rows"][0]["event_local_missing_hours"] == perturbed["event_rows"][0]["event_local_missing_hours"]
    assert base["event_rows"][0]["hard_break_provenance"] == perturbed["event_rows"][0]["hard_break_provenance"]


@pytest.mark.skipif(not REAL_ROOT.exists(), reason="PMXT curated real root not available")
def test_real_root_reconciles_441_panel_and_278_event_local_missing(tmp_path: Path) -> None:
    source_totals = _source_manifest_totals(REAL_ROOT)

    assert source_totals == {
        "event_count": 441,
        "rows_written_total": 747185591,
        "event_type_counts": {
            "book": 1675179,
            "last_trade_price": 495681,
            "price_change": 745011843,
            "tick_size_change": 2888,
        },
        "event_local_missing_events": 278,
    }

    result = inventory.build_inventory(root=REAL_ROOT, output_dir=tmp_path / "out")

    summary = result["summary"]
    assert summary["status"] == "pass"
    assert summary["event_count"] == 441
    assert summary["city_count"] == 49
    assert summary["date_count"] == 9
    assert summary["market_count"] == 4851
    assert summary["token_count"] == 9702
    assert summary["rows_written_total"] == source_totals["rows_written_total"]
    assert summary["event_type_counts_total"] == source_totals["event_type_counts"]
    assert summary["event_local_missing_events"] == source_totals["event_local_missing_events"]
    assert summary["contract_versions"] == ["highest-temperature-event-batch-v2"]
    assert summary["schema_fingerprints"] == [
        "877949c816469b388bad6781f7fc64aa8f27462ebf41ffdce64491525fa20ccc",
    ]
