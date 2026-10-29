"""Freeze recorded Shanghai capacity inputs; no statistics, network or Catalog writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


TASK = Path(__file__).resolve().parent
MANAGER = Path("/data/liuchang/poly-weather-runs/multi-event-native/manager")
PRIMARY = Path("/data/liuchang/nautilus_trader")
METAR = PRIMARY / "polymarket/strategies/weather_metar.py"
ZONE = ZoneInfo("Asia/Shanghai")
CONFIG_FIELDS = (
    "event_slug",
    "date",
    "station",
    "timezone",
    "rule_id",
    "metar_rule",
    "settlement_source_verified",
    "conditions",
    "limits",
    "instruments",
)
LIMIT_FIELDS = (
    "max_event_cost",
    "max_condition_quantity",
    "max_order_quantity",
    "minimum_edge",
    "safety_margin",
)


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    )


def snapshot(path):
    """Read one saved JSON snapshot, never follow config references to credentials."""
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(before.st_size)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Configuration changed during read: {path}")
    return json.loads(data, parse_float=Decimal), {
        "path": str(path),
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "device": before.st_dev,
        "inode": before.st_ino,
    }


def decimal_string(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite business parameter")
    return str(result)


def bucket_upper(label):
    """Same label rule as 2026-10-20-global-weather-capacity/scan.py; no ordinal fallback."""
    label = label.replace("\u2212", "-").replace("\u2013", "-")
    match = re.fullmatch(r"(-?\d+)(?:-(-?\d+))?°([CF])( or below| or higher)?", label)
    if not match or match[3] != "C":
        raise ValueError(f"Unknown Shanghai bucket label: {label!r}")
    return None if match[4] == " or higher" else str(Decimal(match[2] or match[1]))


def pointer(value, location):
    for key in location.strip("/").split("/"):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def config_snapshots(manager, slugs):
    observations, identities = {slug: [] for slug in slugs}, []
    paths = sorted(manager.glob("run-*/manifest-*.json")) + [manager / "active.json"]
    for path in paths:
        if not path.exists():
            continue
        raw, identity = snapshot(path)
        identities.append(identity)
        historical = path.name.startswith("manifest-")
        config = raw.get("config", {}) if historical else raw
        recorded = int(path.stem.rsplit("-", 1)[1]) if historical else identity["mtime_ns"]
        for index, event in enumerate(config.get("events", [])):
            slug = event.get("event_slug")
            if slug not in observations:
                continue
            if event.get("station") != "ZSPD" or event.get("timezone") != "Asia/Shanghai":
                raise ValueError(f"Unexpected Shanghai station/timezone: {path}")
            observations[slug].append(
                {
                    "config": {key: event[key] for key in CONFIG_FIELDS if key in event},
                    "source": {
                        "path": str(path),
                        "json_pointer": f"/config/events/{index}"
                        if historical
                        else f"/events/{index}",
                        "recorded_ns": recorded,
                        "kind": "historical_run_manifest" if historical else "active_snapshot",
                    },
                }
            )
    return observations, identities


def freeze_logs(paths):
    """Freeze every size before reading any log, including .part-* segments."""
    result = []
    for path in sorted(paths):
        stat = path.stat()
        result.append(
            {
                "path": str(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "device": stat.st_dev,
                "inode": stat.st_ino,
            }
        )
    return result


def frozen_lines(bound):
    """Only newline-terminated lines wholly inside the initial size; allow append, not shrink."""
    with Path(bound["path"]).open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if (stat.st_dev, stat.st_ino) != (bound["device"], bound["inode"]) or stat.st_size < bound[
            "size"
        ]:
            raise ValueError(f"Frozen weather log replaced/truncated: {bound['path']}")
        remaining, line_number, consumed = bound["size"], 0, 0
        while remaining:
            data = stream.readline(remaining)
            if not data:
                raise ValueError(f"Weather log shrank while reading: {bound['path']}")
            remaining -= len(data)
            if not data.endswith(b"\n"):
                bound["excluded_partial_bytes"] = len(data)
                break
            consumed += len(data)
            line_number += 1
            if data.strip():
                yield line_number, json.loads(data)
        bound["complete_line_bytes"] = consumed
        bound.setdefault("excluded_partial_bytes", 0)
        after = os.fstat(stream.fileno())
        if after.st_size < bound["size"]:
            raise ValueError(f"Weather log truncated during read: {bound['path']}")
        bound["size_after_read"] = after.st_size


def collect_weather(bounds, dates):
    """Keep entire polls for matching Event/local receipt day; do not filter upstream rows."""
    events, excluded = {slug: {} for slug in dates}, Counter()
    for bound in bounds:
        for line, raw in frozen_lines(bound):
            kind, slug = raw.get("kind"), raw.get("event_id", raw.get("event_slug"))
            if kind not in ("metar_poll", "metar_error") or slug not in events:
                continue
            if raw.get("station", "ZSPD") != "ZSPD":
                raise ValueError("Shanghai weather event has another station")
            received = raw.get("received_ns")
            if isinstance(received, bool) or not isinstance(received, int) or received <= 0:
                raise ValueError(f"Missing original local received_ns: {bound['path']}:{line}")
            if (
                datetime.fromtimestamp(received // 1_000_000_000, ZONE).date().isoformat()
                != dates[slug]
            ):
                excluded[slug] += 1
                continue
            rows = raw.get("rows", [])
            if not isinstance(rows, list) or (kind == "metar_poll" and "rows" not in raw):
                raise ValueError("Invalid full METAR poll rows")
            # Arrival/audit IDs are transport annotations, not weather content.
            semantic = {
                k: v
                for k, v in raw.items()
                if k not in ("arrival_id", "audit_sequence", "recorded_at_ns")
            }
            source = {"path": bound["path"], "line": line}
            # One fetch can emit a poll AND a normalization error with the same receipt.
            # Preserve both; only conflicting copies of the same kind are contradictory.
            key = (received, 0 if kind == "metar_poll" else 1)
            old = events[slug].get(key)
            if old:
                if old["_semantic"] != semantic:
                    raise ValueError(f"Conflicting weather content: {slug} received_ns={received}")
                old["sources"].append(source)
                continue
            events[slug][key] = {
                "received_ns": received,
                "kind": kind,
                "rows": rows,
                "source": source,
                "sources": [source],
                "record_metadata": {k: v for k, v in raw.items() if k != "rows"},
                "_semantic": semantic,
            }
    result = {}
    for slug, records in events.items():
        result[slug] = []
        for key in sorted(records):
            record = records[key]
            del record["_semantic"]
            result[slug].append(record)
    return result, dict(excluded)


def choose_parameter(observations, condition_id, key, fallback):
    found = []
    for observation in observations:
        value = observation["config"].get("conditions", {}).get(condition_id, {}).get(key)
        if value is not None:
            found.append(
                {
                    "value": decimal_string(value),
                    "source": {
                        **observation["source"],
                        "field": f"conditions/{condition_id}/{key}",
                    },
                }
            )
    if found:
        if len({Decimal(v["value"]) for v in found}) != 1:
            raise ValueError(f"Conflicting saved Event {key}: {condition_id}")
        return (
            found[0]["value"],
            "saved_event_business_configuration_not_independent_fee_verification",
            found,
        )
    rates = []
    for observation in fallback:
        fields = observation["fields"]
        schedule = fields.get("feeSchedule") or fields.get("fd") or {}
        value = schedule.get("rate", schedule.get("r"))
        if value is not None:
            rates.append({"value": decimal_string(value), "source": observation})
    if len({Decimal(v["value"]) for v in rates}) > 1:
        raise ValueError(f"Conflicting saved HTTP fee parameters: {condition_id}")
    return (
        (rates[0]["value"], "saved_http_parameter_scenario_not_verified_time_effective", rates)
        if rates
        else (None, "unknown_no_default_zero", [])
    )


def prepare(catalog_index, manager, normalizer, output):
    if not output.resolve().is_relative_to((TASK / "tmp").resolve()):
        raise ValueError("Output must remain inside task/tmp")
    if output.exists():
        raise FileExistsError(output)
    catalogs, _ = snapshot(catalog_index)
    entries = catalogs["catalogs"]
    dates = {e["event_slug"]: e["event_date"] for e in entries}
    bounds = freeze_logs(manager.glob("run-*/weather-*.jsonl"))
    configs, config_sources = config_snapshots(manager, dates)
    frozen_module = normalizer.read_bytes()
    weather, excluded = collect_weather(bounds, dates)
    building = output.with_name(output.name + f".building-{time.time_ns()}")
    building.mkdir(parents=True)
    (building / "weather_metar_frozen.py").write_bytes(frozen_module)
    normalizer_info = {
        "source": str(normalizer),
        "path": str(output / "weather_metar_frozen.py"),
        "sha256": hashlib.sha256(frozen_module).hexdigest(),
    }
    index = {
        "schema": "shanghai_capacity_inputs_index/v1",
        "events": [],
        "catalog_index": str(catalog_index),
        "weather_metar_frozen": normalizer_info,
        "weather_log_bounds": bounds,
        "config_sources": config_sources,
        "weather_policy": "full rows, Event identity and local received day; no Catalog-cutoff crop",
        "statistics_run": False,
        "catalogs_modified": False,
    }
    cache = {}
    for entry in entries:
        slug, day = entry["event_slug"], entry["event_date"]
        contract_path = catalog_index.parent / entry["frozen_contract_relative_path"]
        contract, _ = snapshot(contract_path)
        config = configs[slug]
        conditions = []
        rules = {v["config"].get("metar_rule", "decoded-temperature") for v in config}
        if len(rules) > 1:
            raise ValueError(f"Conflicting saved Event METAR rules: {slug}")
        metar_rule = next(iter(rules)) if rules else None
        for condition in contract["metadata"]["conditions"]:
            cid = condition["condition_id"]
            tokens = {t["outcome"].upper(): t["token_id"] for t in condition["tokens"]}
            evidence = next(
                (
                    e
                    for e in condition["metadata_evidence"]["saved_http"]
                    if e.get("category") == "gamma-event"
                ),
                None,
            )
            if not evidence:
                raise ValueError(f"No recorded bucket label evidence: {cid}")
            path = evidence["path"]
            if path not in cache:
                cache[path] = snapshot(Path(path))[0]
            market = pointer(cache[path], evidence["json_pointer"])
            if market["conditionId"] != cid:
                raise ValueError("Saved Gamma condition identity mismatch")
            upper = bucket_upper(market["groupItemTitle"])
            for v in config:
                saved = v["config"].get("conditions", {}).get(cid, {})
                if "upper_bound" in saved and decimal_string(saved["upper_bound"]) != upper:
                    raise ValueError(f"Bucket label disagrees with saved business config: {cid}")
            rate, scenario, rate_sources = choose_parameter(
                config, cid, "fee_rate", condition["fee_schedule"]["observations"]
            )
            minimum = decimal_string(market.get("orderMinSize"))
            if minimum is not None and Decimal(minimum) <= 0:
                raise ValueError("Nonpositive recorded minimum quantity")
            conditions.append(
                {
                    "condition_id": cid,
                    "no_token_id": tokens["NO"],
                    "upper_bound": upper,
                    "upper_bound_source": {
                        **evidence,
                        "field": "groupItemTitle",
                        "value": market["groupItemTitle"],
                    },
                    "fee_rate": rate,
                    "fee_scenario": scenario,
                    "fee_rate_sources": rate_sources,
                    "fee_effective_verified": False,
                    "raw_fee_parameters": condition["fee_schedule"]["observations"],
                    "minimum_quantity": minimum,
                    "minimum_quantity_source": {**evidence, "field": "orderMinSize"},
                    "metar_rule": metar_rule,
                }
            )
        business, business_sources = {}, {}
        for key in LIMIT_FIELDS:
            values = [
                {
                    "value": decimal_string(v["config"]["limits"][key]),
                    "source": {**v["source"], "field": f"limits/{key}"},
                }
                for v in config
                if key in v["config"].get("limits", {})
            ]
            if len({v["value"] for v in values}) > 1:
                raise ValueError(f"Conflicting saved Event limit: {slug}/{key}")
            business[key] = values[0]["value"] if values else None
            business_sources[key] = values
        # These are the explicit current-rule input requirements, not invented historical config.
        timing = {
            "max_book_age_ns": (
                "5000000000",
                "polymarket/strategies/weather_observed_high.py",
                "WeatherLimits.max_book_age_ns",
            ),
            "weather_poll_ttl_ns": (
                "120000000000",
                "polymarket/strategies/weather_native.py",
                "NativeWeatherStrategy._evaluate weather_received_ns guard",
            ),
            "max_observation_age_seconds": (
                "4500",
                "polymarket/strategies/weather_metar.py",
                "normalize_metar.max_age_seconds",
            ),
        }
        for key, (value, path, symbol) in timing.items():
            business[key] = value
            business_sources[key] = [
                {
                    "value": value,
                    "source": {
                        "kind": "explicit_task_current_rule_requirement_not_historical_manifest",
                        "path": str(PRIMARY / path),
                        "symbol": symbol,
                    },
                }
            ]
        records = weather[slug]
        counts = dict(Counter(r["kind"] for r in records))
        value = {
            "schema": "shanghai_capacity_event_inputs/v1",
            "event_slug": slug,
            "date": day,
            "station": "ZSPD",
            "timezone": "Asia/Shanghai",
            "catalog_path": entry["catalog_path"],
            "analysis_window": entry["analysis_window"],
            "conditions": conditions,
            "metar_rule": metar_rule,
            "metar_rule_sources": [v["source"] for v in config],
            "metar_rule_omitted_means": "decoded-temperature (normalizer default)",
            "limits": business,
            "business_limits": business,
            "business_limit_sources": business_sources,
            "saved_event_configs": config,
            "weather": records,
            "weather_counts": counts,
            "weather_missing": not any(r["kind"] == "metar_poll" for r in records),
            "excluded_outside_local_receipt_day": excluded.get(slug, 0),
            "weather_metar_frozen": normalizer_info,
            "gaps": ([] if records else ["no_recorded_weather_for_event_day"]),
        }
        write_json(building / f"{slug}.json", value)
        index["events"].append(
            {
                "event_slug": slug,
                "date": day,
                "path": str(output / f"{slug}.json"),
                "weather_counts": counts,
                "weather_missing": value["weather_missing"],
                "weather_first_received_ns": records[0]["received_ns"] if records else None,
                "weather_last_received_ns": records[-1]["received_ns"] if records else None,
                "fee_rates": dict(Counter(c["fee_rate"] or "unknown" for c in conditions)),
                "fee_scenarios": sorted({c["fee_scenario"] for c in conditions}),
                "metar_rule": metar_rule,
                "condition_count": len(conditions),
            }
        )
    write_json(building / "index.json", index)
    building.rename(output)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-index", type=Path, default=TASK / "tmp/catalog-delivery/index.json"
    )
    parser.add_argument("--manager", type=Path, default=MANAGER)
    parser.add_argument("--normalizer", type=Path, default=METAR)
    parser.add_argument("--output", type=Path, default=TASK / "tmp/capacity-inputs")
    args = parser.parse_args()
    index = prepare(args.catalog_index, args.manager, args.normalizer, args.output)
    print(json.dumps({"output": str(args.output), "events": index["events"]}, indent=2))


if __name__ == "__main__":
    main()
