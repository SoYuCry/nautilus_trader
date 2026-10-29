"""Compile explicitly frozen collector input using the existing native Catalog APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from collections import Counter
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from pathlib import Path

import collector_native as c
from inspect_transfer import NOT_BEFORE
from inspect_transfer import ROOT
from inspect_transfer import file_identity

import nautilus_trader
from nautilus_trader.model import PRECISION_BYTES
from nautilus_trader.model import BookAction
from nautilus_trader.model import InstrumentClose
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OrderBookDeltas
from nautilus_trader.model import OrderSide
from nautilus_trader.model import RecordFlag
from nautilus_trader.model import TradeTick
from nautilus_trader.persistence import ParquetDataCatalog
from polymarket.event_catalog.native import flatten_order_book_batches
from polymarket.event_catalog.native import restore_order_book_batches


REPO = ROOT


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def verify_sources(contract: dict) -> None:
    if not contract.get("source_files"):
        raise ValueError("Frozen source_files are required")
    for record in contract["source_files"]:
        path = Path(record["path"])
        if not path.is_absolute() or not stat.S_ISREG(record["identity"]["mode"]):
            raise ValueError("Frozen source must be an absolute regular-file path")
        if file_identity(path) != record["identity"]:
            raise ValueError(f"Frozen source changed: {path}")


def validate_contract(contract: dict) -> None:
    if contract.get("schema") != "temporary_weather_frozen_input/v1":
        raise ValueError("Expected temporary_weather_frozen_input/v1")
    if contract.get("transfer_status") != "completed_and_frozen" or not contract.get(
        "completion_confirmation"
    ):
        raise ValueError("Explicit completed/frozen transfer confirmation is required")
    if not contract.get("analysis_window") or not contract.get("event_slug"):
        raise ValueError("Event identity and separately recorded analysis_window are required")
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", contract["event_slug"]) is None:
        raise ValueError("Event slug must be a safe single path component")
    files = {row["path"] for row in contract.get("source_files", [])}
    if contract["raw_path"] not in files:
        raise ValueError("Raw path is not part of the frozen handoff")
    if contract.get("synthetic_test"):
        if any(not Path(path).resolve().is_relative_to((ROOT / "tmp").resolve()) for path in files):
            raise ValueError("Synthetic tests may only consume task-local scratch")
    elif datetime.now(UTC) < NOT_BEFORE:
        raise ValueError("Real source compilation is forbidden before the inspection gate")
    for condition in contract["metadata"]["conditions"]:
        evidence = condition.get("metadata_evidence") or {}
        if evidence.get("path") not in files:
            raise ValueError("Metadata evidence is not in the frozen handoff")
        observations = evidence.get("saved_http", []) + (condition.get("fee_schedule") or {}).get(
            "observations", []
        )
        for observation in observations:
            if observation["path"] not in files or observation["request_log_path"] not in files:
                raise ValueError("Saved HTTP evidence is not in the frozen handoff")
    for resolution in contract.get("http_resolutions", []):
        evidence = resolution["evidence"]
        if evidence["path"] not in files or evidence["request_log_path"] not in files:
            raise ValueError("Actual HTTP resolution evidence is not frozen")
    verify_sources(contract)


def code_provenance() -> dict:
    paths = [
        Path(__file__).resolve(),
        ROOT / "collector_native.py",
        ROOT / "inspect_transfer.py",
        ROOT / "freeze_inputs.py",
        REPO / "polymarket/event_catalog/native.py",
        REPO / "polymarket/_core/identifiers.py",
        c.PRUNER_PATH,
    ]
    return {str(path): c.sha256(path) for path in paths}


def signature_add(digest, record) -> None:
    values = record.deltas if isinstance(record, OrderBookDeltas) else [record]
    for value in values:
        # Native to_dict uses lossless decimal strings and integer nanoseconds.
        digest.update(json.dumps(value.to_dict(), sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")


def key_for(record) -> tuple[str, str]:
    return type(record).__name__, str(record.instrument_id)


def readback(root: Path, instruments: list, expected: dict) -> dict:
    catalog = ParquetDataCatalog(str(root))
    actual_instruments = catalog.instruments()
    if {str(item.id) for item in actual_instruments} != {str(item.id) for item in instruments}:
        raise ValueError("Native instrument mapping differs")
    # The native dict includes every interpretation-bearing field: outcome/raw symbol,
    # price/size precision and increments, scheduled expiry, initial tick and provenance info.
    expected_instruments = {str(item.id): item.to_dict() for item in instruments}
    for instrument in actual_instruments:
        if instrument.to_dict() != expected_instruments[str(instrument.id)]:
            raise ValueError(f"Native instrument semantic fields differ: {instrument.id}")
    actual = {}
    samples = []
    for instrument in actual_instruments:
        iid = str(instrument.id)
        streams = {
            "OrderBookDeltas": restore_order_book_batches(catalog.query_order_book_deltas([iid])),
            "TradeTick": catalog.query_trade_ticks([iid]),
            "InstrumentClose": catalog.query(NautilusDataType.InstrumentClose, identifiers=[iid]),
        }
        for kind, stream in streams.items():
            if not stream:
                continue
            digest, previous = hashlib.sha256(), -1
            levels = {}
            for record in stream:
                if record.ts_init <= previous:
                    raise ValueError("Native readback ordering inversion")
                previous = record.ts_init
                signature_add(digest, record)
                if kind == "OrderBookDeltas":
                    if sum(bool(d.flags & int(RecordFlag.F_LAST)) for d in record.deltas) != 1:
                        raise ValueError("Native batch does not have exactly one F_LAST")
                    if len({d.ts_init for d in record.deltas}) != 1:
                        raise ValueError("Native batch has mixed availability clocks")
                    for delta in record.deltas:
                        if delta.action == BookAction.CLEAR:
                            levels.clear()
                        else:
                            key = delta.order.side, str(delta.order.price)
                            if (
                                delta.action == BookAction.DELETE
                                or delta.order.size.as_decimal() == 0
                            ):
                                levels.pop(key, None)
                            else:
                                levels[key] = delta.order.size
                    bids = sum(side == OrderSide.BUY for side, _ in levels)
                    asks = sum(side == OrderSide.SELL for side, _ in levels)
                    if (not bids or not asks) and len(samples) < 12:
                        samples.append(
                            {
                                "instrument_id": iid,
                                "ts_init": record.ts_init,
                                "bid_levels": bids,
                                "ask_levels": asks,
                                "kind": "empty" if not levels else "single_sided",
                            }
                        )
                    elif max(bids, asks) > 1 and len(samples) < 12:
                        samples.append(
                            {
                                "instrument_id": iid,
                                "ts_init": record.ts_init,
                                "bid_levels": bids,
                                "ask_levels": asks,
                                "kind": "retained_depth",
                            }
                        )
            actual[(kind, iid)] = {
                "count": len(stream),
                "sha256": digest.hexdigest(),
                "first_ts_init": stream[0].ts_init,
                "last_ts_init": stream[-1].ts_init,
                "min_ts_event": min(
                    d.ts_event
                    for r in stream
                    for d in (r.deltas if kind == "OrderBookDeltas" else [r])
                ),
                "max_ts_event": max(
                    d.ts_event
                    for r in stream
                    for d in (r.deltas if kind == "OrderBookDeltas" else [r])
                ),
            }
    if actual != expected:
        raise ValueError(
            f"Native readback differs from emitted counts/content/time bounds: {actual} != {expected}"
        )
    return {
        "status": "passed",
        "instrument_count": len(actual_instruments),
        "streams": {"|".join(key): value for key, value in sorted(actual.items())},
        "book_samples": samples,
        "checks": [
            "all condition/token IDs and complete native instrument semantic fields",
            "lossless native record digest/count/time bounds",
            "native F_LAST batch restoration",
            "per-instrument increasing ts_init",
            "retained depth and single-sided/empty native state samples",
        ],
    }


def compile_event(contract_path: Path, output: Path) -> Path:
    if not contract_path.resolve().is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Frozen input contract must be task-local scratch")
    contract = read_json(contract_path)
    validate_contract(contract)  # Includes the real-input gate before raw content access.
    if not output.resolve().is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Output must stay below this task's tmp/")
    if output.exists():
        raise FileExistsError(output)
    code = code_provenance()
    contract_hash = c.sha256(contract_path)
    raw = Path(contract["raw_path"])
    scanned = c.scan(raw, contract["metadata"])
    if contract["analysis_window"].get("end_policy") == "actual_frozen_raw_receive_cutoff":
        contract["analysis_window"]["end_ns"] = scanned["received_max_ns"]
        seconds, nanos = divmod(scanned["received_max_ns"], 1_000_000_000)
        contract["analysis_window"]["end"] = (
            datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
            + f".{nanos:09d}+00:00"
        )
    instruments = c.instruments_from_scan(scanned)
    verify_sources(contract)
    building = ROOT / "tmp/building" / f"{contract['event_slug']}.{os.getpid()}.{time.time_ns()}"
    building.mkdir(parents=True, exist_ok=False)
    catalog = ParquetDataCatalog(str(building))
    catalog.write_instruments(instruments)
    buffers = defaultdict(list)
    digests = defaultdict(hashlib.sha256)
    expected = {}
    counts = Counter()
    audit = {}
    buffered_rows = 0

    def flush():
        for (kind, _), records in buffers.items():
            if kind == "OrderBookDeltas":
                catalog.write_order_book_deltas(flatten_order_book_batches(records))
            elif kind == "TradeTick":
                catalog.write_trade_ticks(records)
            elif kind == "InstrumentClose":
                catalog.write_instrument_closes(records)
        buffers.clear()

    try:
        previous = -1
        with (building / "source_mapping.jsonl").open("x", encoding="utf-8") as trace:
            for record in c.native_stream(
                raw, instruments, audit, trace, contract.get("http_resolutions", [])
            ):
                if record.ts_init <= previous:
                    raise ValueError("Native Event availability order inversion")
                previous = record.ts_init
                key = key_for(record)
                signature_add(digests[key], record)
                times = (
                    [d.ts_event for d in record.deltas]
                    if isinstance(record, OrderBookDeltas)
                    else [record.ts_event]
                )
                state = expected.setdefault(
                    key,
                    {
                        "count": 0,
                        "first_ts_init": record.ts_init,
                        "last_ts_init": record.ts_init,
                        "min_ts_event": min(times),
                        "max_ts_event": max(times),
                    },
                )
                state["count"] += 1
                state["last_ts_init"] = record.ts_init
                state["min_ts_event"] = min(state["min_ts_event"], min(times))
                state["max_ts_event"] = max(state["max_ts_event"], max(times))
                counts[type(record).__name__] += 1
                buffers[key].append(record)
                buffered_rows += len(record.deltas) if isinstance(record, OrderBookDeltas) else 1
                if buffered_rows >= 50_000:
                    flush()
                    buffered_rows = 0
        flush()
        for key, value in expected.items():
            value["sha256"] = digests[key].hexdigest()
        validation = readback(building, instruments, expected)
        verify_sources(contract)
        if code != code_provenance() or contract_hash != c.sha256(contract_path):
            raise ValueError("Compiler implementation or frozen input contract changed")
        partial = {
            "tokens_without_snapshot": audit["tokens_without_snapshot"],
            "tokens_without_actual_close": audit["tokens_without_actual_close"],
            "tokens_with_preinitialization_updates": [
                t for t, s in scanned["tokens"].items() if s["updates_before_first_book"]
            ],
            "tokens_with_unknown_initial_tick": [
                t for t, s in scanned["tokens"].items() if s["initial_tick_size"] is None
            ],
            "tokens_with_unknown_fees": [
                t for t, s in scanned["tokens"].items() if s["fee_status"] == "unknown"
            ],
        }
        manifest = {
            "schema": "temporary_weather_capacity_catalog/v1",
            "storage": "nautilus_parquet_catalog",
            "event_slug": contract["event_slug"],
            "purpose": "capacity_and_conditional_profit_input_not_strategy_run",
            "capacity_stream_status": "partial"
            if any(partial.values())
            else "observed_capture_complete",
            "complete_strategy_replay_admission": "not_assessed_not_granted",
            "built_with": {
                "nautilus_version": nautilus_trader.__version__,
                "precision_bytes": int(PRECISION_BYTES),
                "python_module": nautilus_trader.__file__,
            },
            "source_files": contract["source_files"],
            "completion_confirmation": contract["completion_confirmation"],
            "input_contract": str(contract_path),
            "input_contract_sha256": contract_hash,
            "metadata": contract["metadata"],
            "http_resolutions": contract.get("http_resolutions", []),
            "http_close_availability_note": "Saved explicit resolutions may be received after the raw market-data cutoff; never backdated to closedTime.",
            "analysis_window": contract["analysis_window"],
            "captured_window": {key: value for key, value in scanned.items() if key != "tokens"},
            "replay": {
                "clock": "receive_availability",
                "ordering": "ts_init",
                "window_policy": "full_captured_event_window_no_analysis_crop",
                "batch_policy": "original_message_plus_token",
                "source_timestamp": "per_delta_ts_event",
                "ties": "global_deterministic_plus_1ns_serialization_audited_in_source_mapping",
            },
            "depth_policy": {
                "rule": c.RULE,
                "implementation": str(c.PRUNER_PATH),
                "sha256": c.sha256(c.PRUNER_PATH),
                "depth": "all_observed_levels",
                "missing_bbo": "unchanged_original_pruner_behavior",
            },
            "code_provenance": code,
            "counts": dict(counts),
            "audit": audit,
            "tokens": scanned["tokens"],
            "partial_metadata": partial,
            "limitations": [
                "No inferred settlement or winning labels for open conditions.",
                "Scheduled native expiration is not actual closure or a data crop.",
                "Receipt serialization does not establish exchange sequencing.",
                "Observed depth is not proof of executable liquidity or capture completeness.",
                "Unknown fees are not verified zero fees; no permanent inventory registration.",
            ],
        }
        write_json(building / "manifest.json", manifest)
        write_json(building / "validation.json", validation)
        write_json(
            building / "COMPLETE.json",
            {
                "status": "passed",
                "event_slug": contract["event_slug"],
                "manifest_sha256": c.sha256(building / "manifest.json"),
                "validation_sha256": c.sha256(building / "validation.json"),
                "counts": dict(counts),
                "source_immutability": "metadata_identities_verified_before_scan_after_scan_after_readback",
            },
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError(output)
        building.rename(output)
    except BaseException as error:
        write_json(building / "ERROR.json", {"error": repr(error), "published": False})
        raise
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(compile_event(args.input_contract, args.output))


if __name__ == "__main__":
    main()
