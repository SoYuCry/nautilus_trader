"""Bind the confirmed Shanghai upload to recorded condition/HTTP metadata; no remote fetches."""

from __future__ import annotations

import argparse
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import collector_native as c
from compile_temporary import verify_sources
from compile_temporary import write_json
from inspect_transfer import ROOT
from inspect_transfer import SOURCE


def array(value):
    return json.loads(value, parse_float=Decimal) if isinstance(value, str) else value


def timestamp(value):
    # Gamma closedTime records use a space and +00; normalize syntax, not meaning.
    return datetime.fromisoformat(value).isoformat()


def mapping(outcomes, tokens):
    outcomes, tokens = array(outcomes), array(tokens)
    if len(outcomes) != 2 or len(tokens) != 2 or {o.upper() for o in outcomes} != {"YES", "NO"}:
        raise ValueError("Incomplete recorded YES/NO mapping")
    return {o.upper(): str(t) for o, t in zip(outcomes, tokens, strict=True)}


def resolved_market(market, expected):
    """Only explicit resolved status AND closedTime AND terminal payouts authorize close."""
    if market.get("umaResolutionStatus") != "resolved" or market.get("closed") is not True:
        return None
    prices = array(market.get("outcomePrices") or [])
    outcomes = array(market["outcomes"])
    if not market.get("closedTime") or len(prices) != 2 or sorted(map(c.decimal, prices)) != [0, 1]:
        return None
    return {
        "winning_asset_id": expected[
            outcomes[next(i for i, p in enumerate(prices) if c.decimal(p) == 1)].upper()
        ],
        "actual_close_time": timestamp(market["closedTime"]),
    }


def prepare(frozen_path: Path, output: Path):
    if not frozen_path.resolve().is_relative_to(
        (ROOT / "tmp").resolve()
    ) or not output.resolve().is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Freeze and contracts must remain task-local")
    if output.exists():
        raise FileExistsError(output)
    frozen = json.loads(frozen_path.read_text())
    base = Path(frozen["source_root"])
    if base != SOURCE and not base.resolve().is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Only confirmed Shanghai input or task-local fixtures")
    if not frozen.get("completion_confirmation"):
        raise ValueError("Explicit user completion confirmation required; no marker required")
    verify_sources(frozen)
    identities = {r["path"]: r for r in frozen["source_files"]}

    def load(path):
        if str(path) not in identities:
            raise ValueError(f"Unfrozen metadata file: {path}")
        return json.loads(path.read_text(), parse_float=Decimal)

    requests_path = base / "runtime/http-requests.jsonl"
    if str(requests_path) not in identities:
        raise ValueError("Saved HTTP request log is not frozen")
    with requests_path.open(encoding="utf-8") as stream:
        requests = [(i, json.loads(line)) for i, line in enumerate(stream, 1)]
    contracts = []
    for day in range(16, 24):
        slug = f"highest-temperature-in-shanghai-on-october-{day}-2026"
        event_path = base / "events" / slug / "manifest.json"
        event = load(event_path)
        raw = event_path.parent / "raw/market.jsonl"
        conditions, required = {}, {str(raw), str(event_path), str(requests_path)}
        for condition_id in event["conditions"]:
            path = event_path.parent / "conditions" / condition_id / "manifest.json"
            value = load(path)
            if value["condition_id"] != condition_id or value["event_slug"] != slug:
                raise ValueError("Condition manifest identity mismatch")
            expected = mapping(value["outcomes"], value["token_ids"])
            if not value.get("end_date"):
                raise ValueError(f"Recorded expiration missing: {condition_id}")
            required.add(str(path))
            conditions[condition_id] = {
                "condition_id": condition_id,
                "scheduled_expiration": timestamp(value["end_date"]),
                "tokens": [{"outcome": o.title(), "token_id": t} for o, t in expected.items()],
                "fee_status": "unknown",
                "fee_schedule": {
                    "status": "raw_parameters_recorded_not_verified_effective_schedule",
                    "observations": [],
                },
                "metadata_evidence": {
                    "path": str(path),
                    "keys": ["condition_id", "outcomes", "token_ids", "end_date"],
                    "saved_http": [],
                },
            }
        resolutions = {}
        http_counts = {}
        for line, request in requests:
            category = request.get("category")
            params = request.get("params") or {}
            matched = category == "gamma-event" and params.get("slug") == slug
            cid = (request.get("url") or "").rsplit("/", 1)[-1]
            token = str(params.get("token_id") or "")
            token_conditions = {
                t["token_id"]: k for k, v in conditions.items() for t in v["tokens"]
            }
            matched = (
                matched
                or (category == "clob-market-info" and cid in conditions)
                or (category == "fee-rate" and token in token_conditions)
            )
            if not matched or request.get("http_status") != 200 or not request.get("body_path"):
                continue
            path = base / request["body_path"]
            body = load(path)
            required.add(str(path))
            evidence = {
                "path": str(path),
                "request_log_path": str(requests_path),
                "request_log_line": line,
                "received_at": request["received_at"],
                "requested_at": request.get("requested_at"),
                "http_status": request["http_status"],
                "request_id": request.get("request_id"),
                "category": category,
            }
            http_counts[category] = http_counts.get(category, 0) + 1
            if category == "gamma-event":
                events = body if isinstance(body, list) else [body]
                matches = [v for v in events if v.get("slug") == slug]
                if len(matches) != 1:
                    raise ValueError("Gamma Event response identity mismatch")
                markets = matches[0]["markets"]
                if {m["conditionId"] for m in markets} != set(conditions):
                    raise ValueError(
                        "Gamma/Event condition coverage mismatch; never silently exclude"
                    )
                for index, market in enumerate(markets):
                    condition_id = market["conditionId"]
                    condition = conditions[condition_id]
                    expected = {t["outcome"].upper(): t["token_id"] for t in condition["tokens"]}
                    if mapping(market["outcomes"], market["clobTokenIds"]) != expected or c.iso_ns(
                        timestamp(market["endDate"])
                    ) != c.iso_ns(condition["scheduled_expiration"]):
                        raise ValueError(
                            "Recorded Gamma mapping/expiration conflicts with condition manifest"
                        )
                    located = {
                        **evidence,
                        "json_pointer": f"/{events.index(matches[0])}/markets/{index}",
                    }
                    if not condition["metadata_evidence"]["saved_http"]:
                        condition["metadata_evidence"]["saved_http"].append(located)
                        condition["fee_schedule"]["observations"].append(
                            {
                                **located,
                                "fields": {
                                    k: market.get(k)
                                    for k in (
                                        "feesEnabled",
                                        "feeType",
                                        "feeSchedule",
                                        "makerBaseFee",
                                        "takerBaseFee",
                                    )
                                },
                            }
                        )
                    actual = resolved_market(market, expected)
                    if actual:
                        if (
                            condition_id in resolutions
                            and resolutions[condition_id]["winning_asset_id"]
                            != actual["winning_asset_id"]
                        ):
                            raise ValueError(f"Conflicting actual HTTP winners: {condition_id}")
                        resolutions.setdefault(
                            condition_id,
                            {
                                **actual,
                                "condition_id": condition_id,
                                "evidence": {
                                    **located,
                                    "fields": {
                                        k: market.get(k)
                                        for k in (
                                            "closed",
                                            "closedTime",
                                            "umaResolutionStatus",
                                            "outcomePrices",
                                            "outcomes",
                                            "clobTokenIds",
                                        )
                                    },
                                },
                            },
                        )
            elif category == "clob-market-info":
                if body.get("c") != cid or mapping(
                    [v["o"] for v in body["t"]], [v["t"] for v in body["t"]]
                ) != {t["outcome"].upper(): t["token_id"] for t in conditions[cid]["tokens"]}:
                    raise ValueError("CLOB condition/token mapping conflict")
                conditions[cid]["metadata_evidence"]["saved_http"].append(evidence)
                conditions[cid]["fee_schedule"]["observations"].append(
                    {**evidence, "fields": {k: body.get(k) for k in ("mbf", "tbf", "fd")}}
                )
            else:
                conditions[token_conditions[token]]["fee_schedule"]["observations"].append(
                    {**evidence, "token_id": token, "fields": body}
                )
        start = datetime(2026, 10, day, tzinfo=UTC) - timedelta(hours=8)
        contract = {
            "schema": "temporary_weather_frozen_input/v1",
            "transfer_status": "completed_and_frozen",
            "completion_confirmation": frozen["completion_confirmation"],
            "frozen_handoff": str(frozen_path),
            "event_slug": slug,
            "raw_path": str(raw),
            "source_files": [identities[p] for p in sorted(required)],
            "analysis_window": {
                "timezone": "Asia/Shanghai",
                "start": start.isoformat(),
                "end": (start + timedelta(days=1)).isoformat() if day != 23 else None,
                "end_policy": "next_local_midnight"
                if day != 23
                else "actual_frozen_raw_receive_cutoff",
            },
            "metadata": {
                "conditions": list(conditions.values()),
                "http_observation_counts": http_counts,
                "http_resolution_policy": "closed_true_and_uma_resolved_and_closedTime_and_terminal_01_payouts; first_recorded_receipt_only",
            },
            "http_resolutions": sorted(
                resolutions.values(), key=lambda v: c.iso_ns(v["evidence"]["received_at"])
            ),
        }
        contracts.append(contract)
    verify_sources(frozen)
    output.mkdir(parents=True)
    for contract in contracts:
        write_json(output / f"{contract['event_slug']}.json", contract)
    return contracts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-handoff", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contracts = prepare(args.frozen_handoff, args.output)
    print(
        json.dumps(
            [
                {
                    "event_slug": v["event_slug"],
                    "conditions": len(v["metadata"]["conditions"]),
                    "http_resolutions": len(v["http_resolutions"]),
                    "frozen_files": len(v["source_files"]),
                }
                for v in contracts
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
