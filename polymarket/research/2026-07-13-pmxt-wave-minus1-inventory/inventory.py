"""
Wave -1 PMXT weather inventory and quality model.

Research-only program for the 441 highest-temperature PMXT bundles.  It
intentionally stays out of factor, strategy, PnL, and generic Nautilus engine
code.  Inputs are source manifests, event indexes, file metadata, and
timestamp/integrity fields only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any


try:
    import pandas as pd
except Exception:  # pragma: no cover - CSV fallback remains available
    pd = None  # type: ignore[assignment]


DEFAULT_ROOT = Path("C:/Projects/PolyReaper/data/curated/polymarket/events")
PROGRAM_NAME = "pmxt-wave-minus1-inventory"
PROGRAM_VERSION = "2026-07-13.v1"
EXPECTED_EVENTS = 441
EXPECTED_CITIES = 49
EXPECTED_DATES = 9
EXPECTED_MARKETS = 4851
EXPECTED_TOKENS = 9702
EXPECTED_EVENT_LOCAL_MISSING_EVENTS = 278
PRIMARY_DATES = frozenset(
    {
        "2026-06-04",
        "2026-06-05",
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
    },
)
DEGRADED_DATES = frozenset({"2026-06-06", "2026-06-07", "2026-06-11", "2026-06-12"})
KNOWN_CORRUPT_HOURS = ("2026-06-04T14",)
PROHIBITED_WAVE_MINUS1_FIELDS = (
    "outcome",
    "settlement",
    "factor_result",
    "strategy_result",
    "fill",
    "fee",
    "position",
    "cash",
    "pnl",
)


@dataclass(frozen=True)
class Interval:
    start: str
    end: str
    hour: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build PMXT Wave -1 inventory artifacts.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Curated PMXT event bundle root.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "outputs",
        help="Artifact output directory.",
    )
    parser.add_argument("--allow-partial", action="store_true", help="Emit artifacts without exact universe assertions.")
    parser.add_argument(
        "--hash-parquet-content",
        action="store_true",
        help="Also compute full orderbook.parquet content sha256 hashes (slower; metadata hashes are always emitted).",
    )
    args = parser.parse_args(argv)

    result = build_inventory(
        root=args.root,
        output_dir=args.output_dir,
        allow_partial=args.allow_partial,
        hash_parquet_content=args.hash_parquet_content,
    )
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    return 0 if result["summary"]["status"] == "pass" else 2


def build_inventory(
    *,
    root: Path,
    output_dir: Path,
    allow_partial: bool = False,
    hash_parquet_content: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    generated_at = now_utc()
    event_rows: list[dict[str, Any]] = []
    token_rows: list[dict[str, Any]] = []
    task_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for event_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        task_id = stable_hash({"program": PROGRAM_VERSION, "event_dir": str(event_dir)})
        task: dict[str, Any] = {
            "task_id": task_id,
            "event_slug": event_dir.name,
            "stage": "inventory",
            "status": "started",
            "started_at": generated_at,
        }
        try:
            event_row, event_token_rows = inventory_one_event(
                event_dir,
                root=root,
                hash_parquet_content=hash_parquet_content,
            )
            event_rows.append(event_row)
            token_rows.extend(event_token_rows)
            task.update(
                {
                    "status": "succeeded",
                    "completed_at": now_utc(),
                    "artifact_hash": stable_hash(event_row),
                },
            )
        except Exception as exc:  # failure isolation: keep unrelated events inspectable
            failure = {
                "event_slug": event_dir.name,
                "event_dir": str(event_dir),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            failures.append(failure)
            task.update({"status": "failed", "completed_at": now_utc(), "error": failure})
        task_rows.append(task)

    event_rows.sort(key=lambda row: (row.get("event_date") or "", row.get("city") or "", row.get("event_slug") or ""))
    token_rows.sort(
        key=lambda row: (
            row.get("event_date") or "",
            row.get("city") or "",
            row.get("event_slug") or "",
            int(row.get("market_index") or 0),
            row.get("token_side") or "",
        ),
    )
    task_rows.sort(key=lambda row: row["event_slug"])

    summary = summarize(event_rows, token_rows, failures, root=root, allow_partial=allow_partial)
    protocol = build_protocol(summary, generated_at)
    quality = build_quality_calibration(summary)

    write_artifacts(
        output_dir=output_dir,
        event_rows=event_rows,
        token_rows=token_rows,
        task_rows=task_rows,
        summary=summary,
        quality=quality,
        protocol=protocol,
    )
    return {
        "summary": summary,
        "event_rows": event_rows,
        "token_rows": token_rows,
        "task_rows": task_rows,
        "quality": quality,
        "protocol": protocol,
    }


def inventory_one_event(
    event_dir: Path,
    *,
    root: Path,
    hash_parquet_content: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    event_index_path = event_dir / "event_index.json"
    gamma_path = event_dir / "gamma_event.raw.json"
    manifest_path = event_dir / "manifest.json"
    orderbook_path = event_dir / "orderbook.parquet"
    missing_files = [str(path.name) for path in (event_index_path, gamma_path, manifest_path, orderbook_path) if not path.exists()]
    if missing_files:
        raise FileNotFoundError(f"missing required files: {', '.join(missing_files)}")

    event_index = read_json(event_index_path)
    manifest = read_json(manifest_path)
    event_start = parse_utc(event_index["startDate"])
    event_end = parse_utc(event_index["endDate"])
    if event_start >= event_end:
        raise ValueError(f"invalid event window: {event_start.isoformat()} >= {event_end.isoformat()}")

    markets = list(event_index.get("markets") or [])
    city = str(event_index["city"])
    event_date = event_start_to_date(event_index)
    raw_missing = normalize_hours(manifest.get("missingHours", []))
    raw_corrupt = normalize_corrupt_hours(manifest)
    local_missing = intersect_hours(raw_missing, event_start, event_end)
    local_corrupt = intersect_hours(raw_corrupt, event_start, event_end)
    nonintersecting_missing = [hour for hour in raw_missing if hour not in {item.hour for item in local_missing}]
    nonintersecting_corrupt = [hour for hour in raw_corrupt if hour not in {item.hour for item in local_corrupt}]
    file_meta = file_identity(orderbook_path, content_hash=hash_parquet_content)
    paths = {
        "event_dir": str(event_dir),
        "event_dir_relative_to_root": event_dir.relative_to(root).as_posix() if event_dir.is_relative_to(root) else str(event_dir),
        "event_index": str(event_index_path),
        "gamma_raw": str(gamma_path),
        "manifest": str(manifest_path),
        "orderbook": str(orderbook_path),
    }
    hashes = {
        "event_index_sha256": sha256_file(event_index_path),
        "gamma_raw_sha256": sha256_file(gamma_path),
        "manifest_sha256": sha256_file(manifest_path),
        "orderbook_identity_sha256": stable_hash(file_meta),
    }
    if hash_parquet_content:
        hashes["orderbook_content_sha256"] = sha256_file(orderbook_path)

    event_type_counts = {str(key): int(value) for key, value in sorted((manifest.get("eventTypeCounts") or {}).items())}
    row_count = int(manifest.get("rowsWritten") or 0)
    token_rows: list[dict[str, Any]] = []
    seen_markets: set[str] = set()
    seen_tokens: set[str] = set()
    for market in markets:
        condition_id = str(market.get("conditionId") or "")
        if not condition_id:
            raise ValueError(f"market missing conditionId in {event_dir}")
        if condition_id in seen_markets:
            raise ValueError(f"duplicate conditionId in {event_dir}: {condition_id}")
        seen_markets.add(condition_id)
        for side, token_key in (("yes", "yesToken"), ("no", "noToken")):
            token_id = str(market.get(token_key) or "")
            if not token_id:
                raise ValueError(f"market missing {token_key} in {event_dir}: {condition_id}")
            if token_id in seen_tokens:
                raise ValueError(f"duplicate token in {event_dir}: {token_id}")
            seen_tokens.add(token_id)
            token_rows.append(
                {
                    "event_slug": str(event_index["eventSlug"]),
                    "event_date": event_date,
                    "cohort_role": cohort_role(event_date),
                    "city": city,
                    "condition_id": condition_id,
                    "market_index": int(market.get("index") or len(seen_markets) - 1),
                    "market_label": str(market.get("label") or market.get("question") or ""),
                    "token_side": side,
                    "asset_id": token_id,
                },
            )

    event_row = {
        "program": PROGRAM_NAME,
        "program_version": PROGRAM_VERSION,
        "ordering_domain": "inventory_source_metadata",
        "performance_claims_allowed": False,
        "current_confirmatory_label": None,
        "event_slug": str(event_index["eventSlug"]),
        "event_id": str(event_index.get("eventId") or ""),
        "event_title": str(event_index.get("title") or ""),
        "city": city,
        "date_text": str(event_index.get("dateText") or ""),
        "event_date": event_date,
        "cohort_role": cohort_role(event_date),
        "event_start": event_start.isoformat().replace("+00:00", "Z"),
        "event_end": event_end.isoformat().replace("+00:00", "Z"),
        "event_window_semantics": "half_open_[event_start,event_end)",
        "contract_version": str(manifest.get("contractVersion") or ""),
        "schema_fingerprint": str(manifest.get("orderbookSchemaFingerprint") or ""),
        "schema_text_sha256": stable_hash(str(manifest.get("orderbookSchema") or "")),
        "paths": paths,
        "hashes": hashes,
        "orderbook_file": file_meta,
        "markets": len(markets),
        "tokens": len(token_rows),
        "rows_written": row_count,
        "event_type_counts": event_type_counts,
        "raw_missing_hours": raw_missing,
        "event_local_missing_hours": [asdict(item) for item in local_missing],
        "event_local_missing_hour_count": len(local_missing),
        "nonintersecting_missing_hours": nonintersecting_missing,
        "raw_corrupt_hours": raw_corrupt,
        "event_local_corrupt_hours": [asdict(item) for item in local_corrupt],
        "event_local_corrupt_hour_count": len(local_corrupt),
        "nonintersecting_corrupt_hours": nonintersecting_corrupt,
        "hard_break_provenance": hard_break_provenance(local_missing, local_corrupt),
        "natural_staleness_policy": "observed_update_gap_without_external_missing_or_corrupt_evidence_is_not_a_hard_break",
        "source_files_used_count": len(manifest.get("sourceFilesUsed") or []),
        "source_files_scanned_count": len(manifest.get("sourceFilesScanned") or []),
        "bad_files_count": len(manifest.get("badFiles") or []),
    }
    event_row["event_identity_sha256"] = stable_hash(
        {
            "event_slug": event_row["event_slug"],
            "contract_version": event_row["contract_version"],
            "schema_fingerprint": event_row["schema_fingerprint"],
            "paths": paths["event_dir_relative_to_root"],
            "hashes": hashes,
            "rows_written": row_count,
            "event_type_counts": event_type_counts,
        },
    )
    return event_row, token_rows


def summarize(
    event_rows: list[dict[str, Any]],
    token_rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    root: Path,
    allow_partial: bool,
) -> dict[str, Any]:
    event_count = len(event_rows)
    city_count = len({row["city"] for row in event_rows})
    dates = sorted({row["event_date"] for row in event_rows})
    date_count = len(dates)
    market_count = sum(int(row["markets"]) for row in event_rows)
    token_count = len(token_rows)
    contract_versions = sorted({row["contract_version"] for row in event_rows})
    schema_fingerprints = sorted({row["schema_fingerprint"] for row in event_rows})
    local_missing_events = sum(1 for row in event_rows if int(row["event_local_missing_hour_count"]) > 0)
    local_corrupt_events = sum(1 for row in event_rows if int(row["event_local_corrupt_hour_count"]) > 0)
    event_type_counts = Counter()
    for row in event_rows:
        event_type_counts.update({key: int(value) for key, value in row["event_type_counts"].items()})
    rows_written = sum(int(row["rows_written"]) for row in event_rows)
    duplicate_keys = duplicate_event_keys(event_rows)
    cohort_counts = Counter(row["cohort_role"] for row in event_rows)

    checks = {
        "events_exact_441": event_count == EXPECTED_EVENTS,
        "cities_exact_49": city_count == EXPECTED_CITIES,
        "dates_exact_9": date_count == EXPECTED_DATES,
        "markets_exact_4851": market_count == EXPECTED_MARKETS,
        "tokens_exact_9702": token_count == EXPECTED_TOKENS,
        "one_contract_version": len(contract_versions) == 1,
        "one_schema_fingerprint": len(schema_fingerprints) == 1,
        "event_local_missing_events_exact_278": local_missing_events == EXPECTED_EVENT_LOCAL_MISSING_EVENTS,
        "no_duplicate_date_city_events": not duplicate_keys,
        "no_event_failures": not failures,
        "no_current_confirmatory_dates": all(
            row["cohort_role"] in {"primary_development_replication", "degraded_robustness"} for row in event_rows
        ),
    }
    pass_status = all(checks.values()) or allow_partial
    return {
        "program": PROGRAM_NAME,
        "program_version": PROGRAM_VERSION,
        "root": str(root),
        "status": "pass" if pass_status else "fail",
        "allow_partial": allow_partial,
        "generated_at": now_utc(),
        "performance_claims_allowed": False,
        "current_panel_confirmation_status": "not_confirmatory",
        "confirmation_gate": "future_unseen_cohort_required",
        "event_count": event_count,
        "city_count": city_count,
        "date_count": date_count,
        "dates": dates,
        "market_count": market_count,
        "token_count": token_count,
        "rows_written_total": rows_written,
        "event_type_counts_total": dict(sorted(event_type_counts.items())),
        "contract_versions": contract_versions,
        "schema_fingerprints": schema_fingerprints,
        "event_local_missing_events": local_missing_events,
        "event_local_corrupt_events": local_corrupt_events,
        "cohort_counts": dict(sorted(cohort_counts.items())),
        "duplicate_date_city_events": duplicate_keys,
        "checks": checks,
        "failures": failures,
        "prohibited_wave_minus1_inputs": PROHIBITED_WAVE_MINUS1_FIELDS,
    }


def build_quality_calibration(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "program": PROGRAM_NAME,
        "program_version": PROGRAM_VERSION,
        "status": "inventory_only_ready" if summary["status"] == "pass" else "inventory_failed",
        "performance_claims_allowed": False,
        "outcome_blind_inputs": [
            "manifest contract/schema",
            "file presence/identity/hashes",
            "row counts",
            "event_type counts",
            "event start/end timestamps",
            "manifest missingHours",
            "known corrupt/bad source hours",
            "date/city/market/token identifiers",
        ],
        "prohibited_inputs": list(PROHIBITED_WAVE_MINUS1_FIELDS),
        "hard_break_rule": (
            "Only externally evidenced corrupt/missing source-hour intervals intersecting "
            "the half-open event window create hard-break provenance."
        ),
        "natural_staleness_rule": (
            "Observed inactivity without intersecting external missing/corrupt evidence is natural staleness, "
            "not an automatic continuity break."
        ),
        "event_local_missing_events": summary["event_local_missing_events"],
        "expected_event_local_missing_events": EXPECTED_EVENT_LOCAL_MISSING_EVENTS,
        "current_panel_confirmation_status": "not_confirmatory",
    }


def build_protocol(summary: dict[str, Any], generated_at: str) -> dict[str, Any]:
    return {
        "protocol_version": "wave-minus1-inventory-v1",
        "generated_at": generated_at,
        "program": PROGRAM_NAME,
        "program_version": PROGRAM_VERSION,
        "performance_claims_allowed": False,
        "cohorts": {
            "primary_development_replication": sorted(PRIMARY_DATES),
            "degraded_robustness": sorted(DEGRADED_DATES),
            "current_confirmatory_dates": [],
            "future_confirmation_requirement": "future_unseen_cohort_required",
        },
        "hard_breaks": {
            "window_semantics": "half_open_[event_start,event_end)",
            "manifest_missing_hours": "hard_break_only_when_intersecting_event_window",
            "known_corrupt_hours": list(KNOWN_CORRUPT_HOURS),
            "nonintersecting_hours": "provenance_only_no_reset_no_exclusion_no_censor",
        },
        "natural_staleness": {
            "definition": "no update gap without external missing/corrupt evidence",
            "continuity": "not_an_automatic_hard_break",
        },
        "inventory_checks": summary["checks"],
        "factor_promotion_rules": [],
        "future_cohort_sizing": None,
    }


def write_artifacts(
    *,
    output_dir: Path,
    event_rows: list[dict[str, Any]],
    token_rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    quality: dict[str, Any],
    protocol: dict[str, Any],
) -> None:
    atomic_write_json(output_dir / "inventory_summary.json", summary)
    atomic_write_json(output_dir / "quality_calibration.json", quality)
    atomic_write_json(output_dir / "protocol-v1.json", protocol)
    atomic_write_json(output_dir / "task_state.json", task_rows)
    remove_if_exists(output_dir / "event_inventory.json")
    remove_if_exists(output_dir / "token_inventory.json")
    write_parquet_fallback(output_dir / "event_inventory.parquet", event_rows)
    write_parquet_fallback(output_dir / "token_inventory.parquet", token_rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        atomic_write_text(path, "")
        return
    fieldnames = sorted(rows[0])
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})
        tmp_name = handle.name
    os.replace(tmp_name, path)


def write_parquet_fallback(path: Path, rows: list[dict[str, Any]]) -> None:
    csv_path = path.with_suffix(".csv")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    unavailable_path = path.with_suffix(".parquet.unavailable.txt")
    if pd is None:
        remove_if_exists(path)
        write_csv(csv_path, rows)
        atomic_write_text(unavailable_path, "parquet unavailable; CSV fallback emitted\n")
        return
    try:
        frame = pd.DataFrame(rows)
        frame.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
        remove_if_exists(csv_path)
        remove_if_exists(unavailable_path)
    except Exception as exc:
        remove_if_exists(tmp_path)
        remove_if_exists(path)
        write_csv(csv_path, rows)
        atomic_write_text(unavailable_path, f"parquet unavailable; CSV fallback emitted: {exc}\n")


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def read_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected JSON object: {path}")
    return loaded


def parse_utc(value: str) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def event_start_to_date(event_index: dict[str, Any]) -> str:
    end = parse_utc(event_index["endDate"])
    return end.date().isoformat()


def cohort_role(event_date: str) -> str:
    if event_date in PRIMARY_DATES:
        return "primary_development_replication"
    if event_date in DEGRADED_DATES:
        return "degraded_robustness"
    return "outside_current_panel"


def normalize_hours(values: Any) -> list[str]:
    if not values:
        return []
    return sorted({normalize_hour(str(value)) for value in values})


def normalize_corrupt_hours(manifest: dict[str, Any]) -> list[str]:
    hours: set[str] = set(KNOWN_CORRUPT_HOURS)
    known = manifest.get("knownCorruptHour")
    if isinstance(known, dict) and known.get("hour"):
        hours.add(normalize_hour(str(known["hour"])))
    for item in manifest.get("badFiles") or []:
        if isinstance(item, dict) and item.get("hour"):
            hours.add(normalize_hour(str(item["hour"])))
    return sorted(hours)


def normalize_hour(value: str) -> str:
    return value[:13]


def hour_interval(hour: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(normalize_hour(hour), "%Y-%m-%dT%H").replace(tzinfo=UTC)
    return start, start + timedelta(hours=1)


def half_open_intersects(left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


def intersect_hours(hours: list[str], event_start: datetime, event_end: datetime) -> list[Interval]:
    intervals: list[Interval] = []
    for hour in hours:
        start, end = hour_interval(hour)
        if half_open_intersects(start, end, event_start, event_end):
            intervals.append(
                Interval(
                    start=max(start, event_start).isoformat().replace("+00:00", "Z"),
                    end=min(end, event_end).isoformat().replace("+00:00", "Z"),
                    hour=hour,
                ),
            )
    return intervals


def hard_break_provenance(local_missing: list[Interval], local_corrupt: list[Interval]) -> dict[str, Any]:
    return {
        "has_hard_break": bool(local_missing or local_corrupt),
        "missing_hour_breaks": [asdict(item) for item in local_missing],
        "corrupt_hour_breaks": [asdict(item) for item in local_corrupt],
        "action": "exclude_or_censor_only_at_intersecting_external_source_gaps",
    }


def file_identity(path: Path, *, content_hash: bool = False) -> dict[str, Any]:
    stat = path.stat()
    identity: dict[str, Any] = {
        "path": str(path),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    if content_hash:
        identity["content_sha256"] = sha256_file(path)
    return identity


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def duplicate_event_keys(event_rows: list[dict[str, Any]]) -> list[str]:
    counts = Counter(f"{row['event_date']}|{row['city']}" for row in event_rows)
    return sorted(key for key, count in counts.items() if count > 1)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False, dir=path.parent) as handle:
        handle.write(text)
        tmp_name = handle.name
    os.replace(tmp_name, path)


def remove_if_exists(path: Path) -> None:
    path.unlink(missing_ok=True)


def now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    sys.exit(main())
