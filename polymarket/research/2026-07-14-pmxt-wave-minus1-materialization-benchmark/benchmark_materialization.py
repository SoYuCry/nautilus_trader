"""Bounded, outcome-blind PMXT G004 O1 materialization benchmark."""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


BENCHMARK_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCHMARK_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.replay_contract import PMXT_RESEARCH_ORDERING_KEY  # noqa: E402


PROGRAM = "pmxt-wave-minus1-materialization-benchmark"
PROGRAM_VERSION = "2026-07-14.g004.v3"
PROTOCOL_VERSION = "wave-minus1-g003-outcome-blind-systems-calibration-v1"
PROTOCOL_HASH = "69f8be79713f7c6f37e06665950106cb6b2ced54156a385c28a963ae4a3076ae"
ORDERING_DOMAIN = "O1_selected_token_replay_order"
ORDERING_VERSION = "timestamp-source-fallback_received_physical-ordinal-v1"
KERNEL_SCHEMA = "selected_token_book_price_change_tick_trade_primitives"
KERNEL_VERSION = "g004-causal-state-kernel-v3"
LABEL_SCHEMA = "none_outcome_blind"
LABEL_SCHEMA_VERSION = "not-applicable-v1"
REPLAY_CONTRACT_VERSION = f"{ORDERING_DOMAIN}:{ORDERING_VERSION}"
DEFAULT_INVENTORY_DIR = REPO_ROOT / "polymarket" / "research" / "2026-07-13-pmxt-wave-minus1-inventory" / "outputs"
DEFAULT_REPRESENTATIVE_SAMPLE = DEFAULT_INVENTORY_DIR / "representative_sample.json"
G002_O3_ARTIFACT = REPO_ROOT / "polymarket" / "research" / "2026-07-13-pmxt-wave-minus1-ordering-parity" / "outputs" / "real_curated_smoke_parity.json"
DEFAULT_OUTPUT_DIR = BENCHMARK_DIR / "outputs"
STREAM_BATCH_SIZE = 65_536
RSS_SAMPLE_INTERVAL_SECONDS = 0.01

ORDERBOOK_COLUMNS = [
    "event_type", "market", "asset_id", "timestamp_received", "timestamp",
    "bids", "asks", "side", "price", "size", "old_tick_size", "new_tick_size",
]
PROHIBITED_FIELD_FRAGMENTS = (
    "outcome", "settlement", "future_return", "factor", "signal", "strategy",
    "order", "fill", "position", "cash", "fee", "pnl", "winner",
)
REQUIRED_METRIC_FIELDS = (
    "mode", "cold_elapsed_seconds", "warm_elapsed_seconds", "peak_rss_bytes",
    "warm_peak_rss_bytes", "peak_rss_method", "peak_rss_scope",
    "process_logical_read_bytes", "process_logical_write_bytes",
    "warm_process_logical_read_bytes", "warm_process_logical_write_bytes",
    "source_file_bytes_upper_bound", "source_pass_count", "warm_source_pass_count",
    "source_hash_pass_count", "source_materialization_pass_count",
    "warm_source_hash_pass_count", "warm_source_materialization_pass_count",
    "artifact_payload_bytes", "cache_manifest_bytes", "artifact_bytes", "row_count",
    "event_count", "token_count", "primitive_count", "retained_primitive_record_count", "parity_digest", "cache_key",
    "cache_status", "resume_behavior",
)


class Mode(StrEnum):
    M1 = "M1"
    M2 = "M2"
    M3 = "M3"


class IntentionalInterruption(RuntimeError):
    """Test-only interruption used to prove that partial cache state is not reused."""


@dataclass(frozen=True)
class CacheContract:
    protocol_hash: str = PROTOCOL_HASH
    ordering_domain: str = ORDERING_DOMAIN
    ordering_version: str = ORDERING_VERSION
    kernel_schema: str = KERNEL_SCHEMA
    kernel_version: str = KERNEL_VERSION
    label_schema: str = LABEL_SCHEMA
    label_schema_version: str = LABEL_SCHEMA_VERSION


DEFAULT_CACHE_CONTRACT = CacheContract()


@dataclass(frozen=True)
class EventSpec:
    role: str
    event_slug: str
    event_date: str
    city: str
    event_dir: Path
    orderbook_path: Path
    condition_id: str
    asset_id: str
    source_hash: str
    rows_written: int


@dataclass(frozen=True)
class SourceScan:
    selected_rows: list[dict[str, Any]]
    source_file_bytes_upper_bound: int
    source_rows_scanned: int
    source_row_groups_scanned: int
    source_batches_scanned: int
    source_pass_count: int = 1


@dataclass(frozen=True)
class CallMeasurement:
    result: Any
    elapsed_seconds: float
    peak_rss_bytes: int
    process_logical_read_bytes: int | None
    process_logical_write_bytes: int | None


@dataclass(frozen=True)
class ModeResult:
    mode: str
    event_slug: str
    cache_key: str
    cache_status: str
    parity_digest: str
    ordering_digest: str
    ordering_contract_version: str
    cold_elapsed_seconds: float
    warm_elapsed_seconds: float | None
    peak_rss_bytes: int
    peak_rss_method: str
    peak_rss_scope: str
    process_logical_read_bytes: int | None
    process_logical_write_bytes: int | None
    source_file_bytes_upper_bound: int
    source_pass_count: int
    source_hash_pass_count: int
    source_materialization_pass_count: int
    source_rows_scanned: int
    source_row_groups_scanned: int
    source_batches_scanned: int
    artifact_payload_bytes: int
    cache_manifest_bytes: int
    artifact_bytes: int
    row_count: int
    event_count: int
    token_count: int
    primitive_count: int
    ordering_checkpoints: list[dict[str, Any]]
    semantic_checkpoints: list[dict[str, Any]]
    event_type_counts: dict[str, int]
    retained_primitive_record_count: int
    artifact_path: Path | None
    resume_behavior: dict[str, Any]

    def metrics(self) -> dict[str, Any]:
        data = asdict(self)
        data["artifact_path"] = _portable_path(self.artifact_path) if self.artifact_path else None
        data.update({
            "warm_peak_rss_bytes": None,
            "warm_process_logical_read_bytes": None,
            "warm_process_logical_write_bytes": None,
            "warm_source_pass_count": None,
            "warm_source_hash_pass_count": None,
            "warm_source_materialization_pass_count": None,
        })
        return data


@dataclass
class BookState:
    bids: dict[Decimal, Decimal]
    asks: dict[Decimal, Decimal]
    tick_size: Decimal | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "bids": [[_decimal_text(p), _decimal_text(s)] for p, s in sorted(self.bids.items(), reverse=True) if s > 0],
            "asks": [[_decimal_text(p), _decimal_text(s)] for p, s in sorted(self.asks.items()) if s > 0],
            "tick_size": _decimal_text(self.tick_size),
        }


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_json_default)


def stable_hash(data: Any) -> str:
    return hashlib.sha256(stable_json(data).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return _portable_path(value)
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return _ts_text(value)
    return str(value)


def _portable_path(path: Path | None) -> str | None:
    if path is None:
        return None
    return os.path.relpath(Path(path).resolve(), BENCHMARK_DIR).replace("\\", "/")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return Decimal(str(value))


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    parsed = pd.Timestamp(value).to_pydatetime()
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _ts_text(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _levels(value: Any) -> dict[Decimal, Decimal]:
    if value is None:
        return {}
    decoded = json.loads(value) if isinstance(value, str) else value
    return {} if decoded is None else {Decimal(str(price)): Decimal(str(size)) for price, size in decoded}


def _top(book: dict[Decimal, Decimal], *, bid: bool) -> Decimal | None:
    prices = [price for price, size in book.items() if size > 0]
    return (max(prices) if bid else min(prices)) if prices else None


def _arrow_text_scalar(array: pa.Array, value: str) -> pa.Scalar:
    if pa.types.is_binary(array.type) or pa.types.is_fixed_size_binary(array.type) or pa.types.is_large_binary(array.type):
        return pa.scalar(value.encode(), type=array.type)
    return pa.scalar(value, type=array.type)


def _stream_selected_rows(event: EventSpec, *, batch_size: int = STREAM_BATCH_SIZE) -> SourceScan:
    """Bounded source streaming plus an in-memory selected-token O1 sort buffer."""
    parquet = pq.ParquetFile(event.orderbook_path)
    missing = sorted(set(ORDERBOOK_COLUMNS) - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"orderbook parquet missing required columns: {missing}")
    selected: list[dict[str, Any]] = []
    physical_offset = 0
    batch_count = 0
    for row_group in range(parquet.metadata.num_row_groups):
        for batch in parquet.iter_batches(row_groups=[row_group], batch_size=batch_size, columns=ORDERBOOK_COLUMNS, use_threads=True):
            batch_count += 1
            market = batch.column(batch.schema.get_field_index("market"))
            asset = batch.column(batch.schema.get_field_index("asset_id"))
            mask = pc.and_(pc.equal(market, _arrow_text_scalar(market, event.condition_id)), pc.equal(asset, _arrow_text_scalar(asset, event.asset_id)))
            mask = pc.fill_null(mask, False)
            indices = pc.indices_nonzero(mask).to_pylist()
            if indices:
                records = batch.filter(mask).to_pylist()
                for local_index, record in zip(indices, records, strict=True):
                    record["_original_row_index"] = physical_offset + int(local_index)
                    received = _parse_ts(record.get("timestamp_received"))
                    source = _parse_ts(record.get("timestamp"))
                    if received is None or (source or received) is None:
                        raise ValueError("selected PMXT row lacks a usable replay timestamp")
                    record["_received_dt"] = received
                    record["_sort_dt"] = source or received
                    selected.append(record)
            physical_offset += batch.num_rows
    if not selected:
        raise ValueError(f"no selected rows for {event.event_slug} {event.condition_id}/{event.asset_id}")
    selected.sort(key=lambda row: (row["_sort_dt"], row["_received_dt"], row["_original_row_index"]))
    return SourceScan(selected, event.orderbook_path.stat().st_size, physical_offset, parquet.metadata.num_row_groups, batch_count)


def _primitive_from_row(row: dict[str, Any], state: BookState, sequence: int) -> dict[str, Any]:
    event_type = "trade" if str(row.get("event_type")) == "last_trade_price" else str(row.get("event_type"))
    source_ts = _parse_ts(row.get("timestamp"))
    received_ts = row["_received_dt"]
    if event_type == "book":
        state.bids, state.asks = _levels(row.get("bids")), _levels(row.get("asks"))
    elif event_type == "price_change":
        side, price, size = str(row.get("side") or "").upper(), _parse_decimal(row.get("price")), _parse_decimal(row.get("size"))
        if price is not None and size is not None and side in {"BUY", "SELL"}:
            book = state.bids if side == "BUY" else state.asks
            book.pop(price, None) if size <= 0 else book.__setitem__(price, size)
    elif event_type == "tick_size_change":
        state.tick_size = _parse_decimal(row.get("new_tick_size"))
    elif event_type != "trade":
        raise ValueError(f"unsupported PMXT event_type: {event_type!r}")
    best_bid, best_ask = _top(state.bids, bid=True), _top(state.asks, bid=False)
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return {
        "contract_version": REPLAY_CONTRACT_VERSION, "ordering_domain": ORDERING_DOMAIN,
        "sequence": sequence, "timestamp": _ts_text(source_ts or received_ts),
        "timestamp_source": "timestamp" if source_ts else "timestamp_received_fallback",
        "timestamp_received": _ts_text(received_ts), "source_row_index": int(row["_original_row_index"]),
        "event_type": event_type,
        "side": str(row.get("side")).upper() if row.get("side") is not None else None,
        "price": _decimal_text(_parse_decimal(row.get("price"))), "size": _decimal_text(_parse_decimal(row.get("size"))),
        "best_bid": _decimal_text(best_bid), "best_ask": _decimal_text(best_ask),
        "mid": _decimal_text(mid), "spread": _decimal_text(spread), "tick_size": _decimal_text(state.tick_size),
    }


def _digest_records(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(stable_json(record).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _ordering_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("sequence", "timestamp", "timestamp_received", "source_row_index", "event_type")}


def _semantic_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("sequence", "event_type", "side", "price", "size", "best_bid", "best_ask", "tick_size")}


def _sample_checkpoints(records: list[dict[str, Any]], mapper: Callable[[dict[str, Any]], dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    return [mapper(records[index]) for index in sorted({0, len(records) // 2, len(records) - 1})]


def _materialize_mode(event: EventSpec, mode: Mode, config: dict[str, Any]) -> dict[str, Any]:
    scan = _stream_selected_rows(event, batch_size=int(config.get("batch_size", STREAM_BATCH_SIZE)))
    state = BookState({}, {})
    trace: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    ordering_checkpoints: list[dict[str, Any]] = []
    semantic_checkpoints: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    every = max(1, int(config.get("anchor_every_rows", 1000)))
    running = hashlib.sha256()
    ordering_running = hashlib.sha256()
    row_count = len(scan.selected_rows)
    checkpoint_sequences = {1, row_count // 2 + 1, row_count}
    last_primitive: dict[str, Any] | None = None
    for sequence, row in enumerate(scan.selected_rows, start=1):
        if config.get("fail_after_rows") is not None and sequence > int(config["fail_after_rows"]):
            raise IntentionalInterruption(f"intentional interruption after {config['fail_after_rows']} selected rows")
        primitive = _primitive_from_row(row, state, sequence)
        running.update(stable_json(primitive).encode())
        running.update(b"\n")
        ordering = _ordering_record(primitive)
        ordering_running.update(stable_json(ordering).encode())
        ordering_running.update(b"\n")
        counts[primitive["event_type"]] = counts.get(primitive["event_type"], 0) + 1
        if sequence in checkpoint_sequences:
            ordering_checkpoints.append(ordering)
            semantic_checkpoints.append(_semantic_record(primitive))
        if mode == Mode.M3:
            trace.append(primitive)
        if mode == Mode.M2 and (sequence == 1 or sequence % every == 0):
            anchors.append({"sequence": sequence, "source_row_index": primitive["source_row_index"], "stream_digest_through_anchor": running.hexdigest(), "state": state.snapshot(), "primitive": primitive})
        last_primitive = primitive
    if mode == Mode.M2 and last_primitive is not None and anchors[-1]["sequence"] != row_count:
        anchors.append({"sequence": row_count, "source_row_index": last_primitive["source_row_index"], "stream_digest_through_anchor": running.hexdigest(), "state": state.snapshot(), "primitive": last_primitive})
    return {
        "scan": scan, "trace": trace, "anchors": anchors, "parity_digest": running.hexdigest(),
        "ordering_digest": ordering_running.hexdigest(),
        "ordering_checkpoints": ordering_checkpoints,
        "semantic_checkpoints": semantic_checkpoints,
        "event_type_counts": counts, "row_count": row_count, "final_state": state.snapshot(),
        "retained_primitive_record_count": len(trace),
    }


def _sanitized_config(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in sorted(config.items()) if key != "fail_after_rows"}


def _source_content_sha256(event: EventSpec) -> str:
    """
    Return the actual orderbook bytes digest used for cache identity.

    Inventory hashes may be path/size/mtime identities on older artifacts; the
    benchmark cache must key only on the selected event source bytes so a
    same-size rewrite with restored mtime cannot reuse stale materialization.
    """
    return sha256_file(event.orderbook_path)


def _cache_identity(event: EventSpec, mode: Mode, config: dict[str, Any], *, contract: CacheContract = DEFAULT_CACHE_CONTRACT) -> dict[str, Any]:
    source_content_sha256 = _source_content_sha256(event)
    return {
        **asdict(contract),
        "source_content_sha256": source_content_sha256,
        "source_hash": source_content_sha256,
        "event_slug": event.event_slug,
        "condition_id": event.condition_id, "asset_id": event.asset_id, "mode": mode.value,
        "mode_config": _sanitized_config(config),
    }


def _cache_key(event: EventSpec, mode: Mode, config: dict[str, Any], *, contract: CacheContract = DEFAULT_CACHE_CONTRACT) -> str:
    return stable_hash(_cache_identity(event, mode, config, contract=contract))


def _artifact_metadata(
    event: EventSpec,
    mode: Mode,
    cache_key: str,
    config: dict[str, Any],
    scan: SourceScan,
    identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "program": PROGRAM, "program_version": PROGRAM_VERSION, "cache_key": cache_key,
        "cache_identity": identity, "mode": mode.value, "mode_config": _sanitized_config(config),
        "event_slug": event.event_slug, "event_date": event.event_date, "city": event.city,
        "condition_id": event.condition_id, "asset_id": event.asset_id,
        "source_content_sha256": identity["source_content_sha256"],
        "source_hash": identity["source_hash"],
        "ordering_domain": ORDERING_DOMAIN, "ordering_scope": "O1_only", "ordering_key": PMXT_RESEARCH_ORDERING_KEY,
        "replay_contract_version": REPLAY_CONTRACT_VERSION, "source_access_method": "sha256_source_byte_pass_plus_pyarrow_row_group_batch_streaming_selected_token_sort_buffer",
        "source_pass_count": 1 + scan.source_pass_count,
        "source_hash_pass_count": 1,
        "source_materialization_pass_count": scan.source_pass_count,
        "source_rows_scanned": scan.source_rows_scanned,
        "source_rows_scanned_semantics": "parsed rows from materialization pass only; source hash pass reads bytes and parses zero rows",
        "source_row_groups_scanned": scan.source_row_groups_scanned, "source_batches_scanned": scan.source_batches_scanned,
        "performance_claims_allowed": False, "not_for_pnl": True, "outcome_blind": True,
        "o2_event_wide_diagnostics": "locate_only_never_execution_truth",
        "best_bid_best_ask_policy": "source_fields_not_read; derived_from_book_and_price_change_state",
    }


def _write_atomic_json(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as temporary:
        json.dump(payload, temporary, indent=2, sort_keys=True, default=_json_default)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
    return path.stat().st_size


def _cache_manifest_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def _write_cache_payload(path: Path, payload: dict[str, Any], expected_identity: dict[str, Any]) -> tuple[int, int]:
    payload_bytes = _write_atomic_json(path, payload)
    manifest = {
        "commit_status": "committed", "artifact_filename": path.name,
        "artifact_sha256": sha256_file(path), "artifact_payload_bytes": payload_bytes,
        "cache_key": payload["metadata"]["cache_key"], "cache_identity": expected_identity,
        "program_version": PROGRAM_VERSION,
    }
    manifest_bytes = _write_atomic_json(_cache_manifest_path(path), manifest)
    return payload_bytes, manifest_bytes


def _read_cache_payload(path: Path, expected_identity: dict[str, Any]) -> dict[str, Any]:
    manifest_path = _cache_manifest_path(path)
    if not path.exists() or not manifest_path.exists():
        raise ValueError("cache payload or manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("commit_status") != "committed" or manifest.get("cache_identity") != expected_identity:
        raise ValueError("cache manifest is not a committed matching identity")
    if manifest.get("artifact_filename") != path.name or path.stat().st_size != int(manifest.get("artifact_payload_bytes", -1)):
        raise ValueError("cache payload name or size mismatch")
    if sha256_file(path) != manifest.get("artifact_sha256"):
        raise ValueError("cache payload sha256 mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("metadata", {}).get("cache_identity") != expected_identity or payload.get("metadata", {}).get("cache_key") != manifest.get("cache_key"):
        raise ValueError("cache payload metadata mismatch")
    return payload


def _windows_process_io() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    counters = _IO_COUNTERS()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IO_COUNTERS)]
    kernel32.GetProcessIoCounters.restype = wintypes.BOOL
    if not kernel32.GetProcessIoCounters(kernel32.GetCurrentProcess(), ctypes.byref(counters)):
        return None
    return int(counters.ReadTransferCount), int(counters.WriteTransferCount)


def _current_process_rss() -> int:
    if os.name != "nt":
        return 0
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return 0
    return int(counters.WorkingSetSize)


class _RssSampler:
    def __init__(self) -> None:
        self.peak = _current_process_rss()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(RSS_SAMPLE_INTERVAL_SECONDS):
            self.peak = max(self.peak, _current_process_rss())

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> int:
        self.peak = max(self.peak, _current_process_rss())
        self._stop.set()
        self._thread.join()
        return self.peak


def _measure_call(func: Callable[[], Any]) -> CallMeasurement:
    io_before = _windows_process_io()
    sampler = _RssSampler()
    start = time.perf_counter()
    sampler.start()
    try:
        result = func()
    finally:
        elapsed = time.perf_counter() - start
        peak = sampler.finish()
    io_after = _windows_process_io()
    read_delta = write_delta = None
    if io_before is not None and io_after is not None:
        read_delta = max(0, io_after[0] - io_before[0])
        write_delta = max(0, io_after[1] - io_before[1])
    return CallMeasurement(result, elapsed, peak, read_delta, write_delta)


def run_mode(event: EventSpec, mode: Mode, *, output_dir: Path, config: dict[str, Any] | None = None) -> ModeResult:  # noqa: C901 - warm/cold measured boundary stays joined for accounting truth
    config = dict(config or {})
    cache_dir = Path(output_dir) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def measured_work() -> dict[str, Any]:
        # This is intentionally inside the measured boundary. Every run performs
        # exactly one source-content byte hash pass before cache lookup or
        # materialization, and that identity is reused for artifact metadata.
        identity = _cache_identity(event, mode, config)
        cache_key = stable_hash(identity)
        artifact_path = None if mode == Mode.M1 else cache_dir / f"{cache_key}-{mode.value.lower()}.json"
        partial_path = cache_dir / f"{cache_key}-{mode.value.lower()}.partial"
        partials = list(cache_dir.glob(f"{cache_key}-{mode.value.lower()}*.partial"))
        invalid_cache_reason: str | None = None

        if artifact_path is not None and (artifact_path.exists() or _cache_manifest_path(artifact_path).exists()):
            try:
                payload = _read_cache_payload(artifact_path, identity)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                invalid_cache_reason = type(exc).__name__
                artifact_path.unlink(missing_ok=True)
                _cache_manifest_path(artifact_path).unlink(missing_ok=True)
            else:
                return {
                    "path": "warm",
                    "identity": identity,
                    "cache_key": cache_key,
                    "artifact_path": artifact_path,
                    "partials": partials,
                    "payload": payload,
                }

        try:
            materialized = _materialize_mode(event, mode, config)
        except IntentionalInterruption:
            _write_atomic_json(partial_path, {"cache_key": cache_key, "status": "interrupted_uncommitted_partial"})
            raise
        payload_bytes = manifest_bytes = 0
        if artifact_path is not None:
            scan = materialized["scan"]
            metadata = {
                **_artifact_metadata(event, mode, cache_key, config, scan, identity),
                "row_count": materialized["row_count"], "primitive_count": materialized["row_count"],
                "retained_primitive_record_count": materialized["retained_primitive_record_count"],
            }
            if mode == Mode.M2:
                metadata["anchor_rule"] = f"first_every_{max(1, int(config.get('anchor_every_rows', 1000)))}_rows_and_last"
                payload = {"metadata": metadata, "parity_digest": materialized["parity_digest"], "ordering_digest": materialized["ordering_digest"], "ordering_checkpoints": materialized["ordering_checkpoints"], "semantic_checkpoints": materialized["semantic_checkpoints"], "event_type_counts": materialized["event_type_counts"], "anchors": materialized["anchors"]}
            else:
                payload = {"metadata": metadata, "parity_digest": materialized["parity_digest"], "ordering_digest": materialized["ordering_digest"], "ordering_checkpoints": materialized["ordering_checkpoints"], "semantic_checkpoints": materialized["semantic_checkpoints"], "event_type_counts": materialized["event_type_counts"], "trace": materialized["trace"]}
            payload_bytes, manifest_bytes = _write_cache_payload(artifact_path, payload, identity)
        return {
            "path": "cold",
            "identity": identity,
            "cache_key": cache_key,
            "artifact_path": artifact_path,
            "partial_path": partial_path,
            "partials": partials,
            "invalid_cache_reason": invalid_cache_reason,
            "materialized": materialized,
            "payload_bytes": payload_bytes,
            "manifest_bytes": manifest_bytes,
        }

    try:
        measured = _measure_call(measured_work)
    except IntentionalInterruption:
        raise

    work = measured.result
    cache_key = work["cache_key"]
    artifact_path = work["artifact_path"]
    partials = work.get("partials", [])

    if work["path"] == "warm":
        payload = work["payload"]
        metadata = payload["metadata"]
        return ModeResult(
            mode.value, event.event_slug, cache_key, "warm_reused_committed_matching_payload",
            payload["parity_digest"], payload["ordering_digest"], REPLAY_CONTRACT_VERSION,
            0.0, measured.elapsed_seconds, measured.peak_rss_bytes,
            "windows_psapi_GetProcessMemoryInfo_working_set_sampled_10ms", "sampled_process_working_set_during_call",
            measured.process_logical_read_bytes, measured.process_logical_write_bytes,
            event.orderbook_path.stat().st_size, 1, 1, 0, 0, 0, 0,
            artifact_path.stat().st_size, _cache_manifest_path(artifact_path).stat().st_size,
            artifact_path.stat().st_size + _cache_manifest_path(artifact_path).stat().st_size,
            int(metadata["row_count"]), 1, 1, int(metadata["primitive_count"]),
            payload["ordering_checkpoints"], payload["semantic_checkpoints"], payload["event_type_counts"],
            int(metadata["retained_primitive_record_count"]), artifact_path,
            {"ignored_partial_artifacts": len(partials), "reused_completed_artifact": True, "committed_matching_payload_only": True},
        )

    materialized = work["materialized"]
    removed_partials = 0
    for partial in partials:
        partial.unlink(missing_ok=True)
        removed_partials += 1
    scan = materialized["scan"]
    return ModeResult(
        mode.value, event.event_slug, cache_key,
        "direct_no_persistent_derivative" if mode == Mode.M1 else ("cold_recomputed_after_invalid_cache" if work.get("invalid_cache_reason") else "cold_committed"),
        materialized["parity_digest"], materialized["ordering_digest"], REPLAY_CONTRACT_VERSION,
        measured.elapsed_seconds, None, measured.peak_rss_bytes,
        "windows_psapi_GetProcessMemoryInfo_working_set_sampled_10ms", "sampled_process_working_set_during_call",
        measured.process_logical_read_bytes, measured.process_logical_write_bytes,
        scan.source_file_bytes_upper_bound, 1 + scan.source_pass_count, 1, scan.source_pass_count,
        scan.source_rows_scanned, scan.source_row_groups_scanned, scan.source_batches_scanned,
        work["payload_bytes"], work["manifest_bytes"], work["payload_bytes"] + work["manifest_bytes"], materialized["row_count"], 1, 1,
        materialized["row_count"], materialized["ordering_checkpoints"], materialized["semantic_checkpoints"],
        materialized["event_type_counts"], materialized["retained_primitive_record_count"], artifact_path,
        {"ignored_partial_artifacts": len(partials), "removed_partial_artifacts": removed_partials, "reused_completed_artifact": False, "committed_matching_payload_only": True, "invalid_cache_reason": work.get("invalid_cache_reason"), "failure_isolation": "per_event_token_cache_identity"},
    )

def _oracle_stream_and_reduce(event: EventSpec, *, anchor_sequences: set[int] | None = None) -> dict[str, Any]:  # noqa: C901 - deliberately independent audit reducer
    """Independent Arrow source scan and plain-dict state reducer for real O1 audit."""
    anchor_sequences = anchor_sequences or set()
    parquet = pq.ParquetFile(event.orderbook_path)
    selected: list[dict[str, Any]] = []
    physical_offset = 0
    batches = 0
    for row_group in range(parquet.metadata.num_row_groups):
        for batch in parquet.iter_batches(row_groups=[row_group], batch_size=STREAM_BATCH_SIZE, columns=ORDERBOOK_COLUMNS, use_threads=True):
            batches += 1
            market_values = batch.column(batch.schema.get_field_index("market")).to_pylist()
            asset_values = batch.column(batch.schema.get_field_index("asset_id")).to_pylist()
            matching_indices = [
                index for index, (market, asset) in enumerate(zip(market_values, asset_values, strict=True))
                if PMXTEventV1Adapter._canonical_market(market) == event.condition_id and str(asset) == event.asset_id
            ]
            matching_records = batch.take(pa.array(matching_indices, type=pa.int64())).to_pylist() if matching_indices else []
            for local_index, row in zip(matching_indices, matching_records, strict=True):
                received = _parse_ts(row.get("timestamp_received"))
                source = _parse_ts(row.get("timestamp"))
                if received is None:
                    raise ValueError("oracle selected row missing timestamp_received")
                row["ordinal"] = physical_offset + local_index
                row["received"] = received
                row["replay"] = source or received
                selected.append(row)
            physical_offset += batch.num_rows
    selected.sort(key=lambda row: (row["replay"], row["received"], row["ordinal"]))

    bids: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    tick: Decimal | None = None
    records: list[dict[str, Any]] = []
    anchor_oracles: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    running = hashlib.sha256()
    for sequence, row in enumerate(selected, start=1):
        event_type = "trade" if row["event_type"] == "last_trade_price" else row["event_type"]
        if event_type == "book":
            bids = _levels(row.get("bids"))
            asks = _levels(row.get("asks"))
        elif event_type == "price_change":
            side = str(row.get("side") or "").upper()
            price, size = _parse_decimal(row.get("price")), _parse_decimal(row.get("size"))
            if side in {"BUY", "SELL"} and price is not None and size is not None:
                target = bids if side == "BUY" else asks
                if size <= 0:
                    target.pop(price, None)
                else:
                    target[price] = size
        elif event_type == "tick_size_change":
            tick = _parse_decimal(row.get("new_tick_size"))
        elif event_type != "trade":
            raise ValueError(f"oracle unsupported event type {event_type}")
        best_bid = max((price for price, size in bids.items() if size > 0), default=None)
        best_ask = min((price for price, size in asks.items() if size > 0), default=None)
        source_ts = _parse_ts(row.get("timestamp"))
        mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        record = {
            "contract_version": REPLAY_CONTRACT_VERSION, "ordering_domain": ORDERING_DOMAIN,
            "sequence": sequence, "timestamp": _ts_text(source_ts or row["received"]),
            "timestamp_source": "timestamp" if source_ts else "timestamp_received_fallback",
            "timestamp_received": _ts_text(row["received"]), "source_row_index": int(row["ordinal"]),
            "event_type": event_type, "side": str(row.get("side")).upper() if row.get("side") is not None else None,
            "price": _decimal_text(_parse_decimal(row.get("price"))), "size": _decimal_text(_parse_decimal(row.get("size"))),
            "best_bid": _decimal_text(best_bid), "best_ask": _decimal_text(best_ask),
            "mid": _decimal_text(mid), "spread": _decimal_text(spread), "tick_size": _decimal_text(tick),
        }
        records.append(record)
        running.update(stable_json(record).encode())
        running.update(b"\n")
        if sequence in anchor_sequences:
            anchor_oracles.append({
                "sequence": sequence, "source_row_index": record["source_row_index"],
                "stream_digest_through_anchor": running.hexdigest(),
                "state": {
                    "bids": [[_decimal_text(price), _decimal_text(size)] for price, size in sorted(bids.items(), reverse=True) if size > 0],
                    "asks": [[_decimal_text(price), _decimal_text(size)] for price, size in sorted(asks.items()) if size > 0],
                    "tick_size": _decimal_text(tick),
                },
                "primitive": record,
            })
        counts[event_type] = counts.get(event_type, 0) + 1
    return {
        "event_slug": event.event_slug,
        "method": "independent_pyarrow_row_group_scan_and_plain_dict_source_state_reducer",
        "source_pass_count": 1, "source_rows_scanned": physical_offset,
        "source_row_groups_scanned": parquet.metadata.num_row_groups, "source_batches_scanned": batches,
        "selected_rows": len(records), "parity_digest": _digest_records(records),
        "ordering_digest": _digest_records([_ordering_record(record) for record in records]),
        "ordering_checkpoints": _sample_checkpoints(records, _ordering_record),
        "semantic_checkpoints": _sample_checkpoints(records, _semantic_record),
        "event_type_counts": counts,
        "_records": records,
        "_anchor_oracles": anchor_oracles,
    }


def build_parity_oracles(events: list[EventSpec], mode_results: dict[str, dict[Mode, ModeResult]]) -> dict[str, Any]:
    scans: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    mode_audits: list[dict[str, Any]] = []
    for event in events:
        results = mode_results[event.event_slug]
        m1, m2, m3 = results[Mode.M1], results[Mode.M2], results[Mode.M3]
        assert m2.artifact_path is not None
        assert m3.artifact_path is not None
        m2_payload = _read_cache_payload(m2.artifact_path, _cache_identity(event, Mode.M2, {"anchor_every_rows": 1000}))
        m3_payload = _read_cache_payload(m3.artifact_path, _cache_identity(event, Mode.M3, {}))
        anchor_sequences = {int(anchor["sequence"]) for anchor in m2_payload["anchors"]}
        oracle = _oracle_stream_and_reduce(event, anchor_sequences=anchor_sequences)
        oracle_records = oracle.pop("_records")
        oracle_anchors = oracle.pop("_anchor_oracles")
        scans.append(oracle)
        m1_audit = {
            "event_slug": event.event_slug,
            "row_count_match": oracle["selected_rows"] == m1.row_count,
            "ordering_digest_match": oracle["ordering_digest"] == m1.ordering_digest,
            "ordering_checkpoints_match": oracle["ordering_checkpoints"] == m1.ordering_checkpoints,
            "derived_semantics_match": oracle["parity_digest"] == m1.parity_digest and oracle["semantic_checkpoints"] == m1.semantic_checkpoints,
            "event_type_semantics_match": oracle["event_type_counts"] == m1.event_type_counts,
        }
        m1_audit["status"] = "pass" if all(value for key, value in m1_audit.items() if key != "event_slug") else "fail"
        audits.append(m1_audit)

        m2_checks = {
            "full_parity_digest_match": m2_payload["parity_digest"] == oracle["parity_digest"] == m2.parity_digest,
            "full_ordering_digest_match": m2_payload["ordering_digest"] == oracle["ordering_digest"] == m2.ordering_digest,
            "ordering_checkpoints_match": m2_payload["ordering_checkpoints"] == oracle["ordering_checkpoints"],
            "semantic_checkpoints_match": m2_payload["semantic_checkpoints"] == oracle["semantic_checkpoints"],
            "anchor_sequence_count": len(anchor_sequences),
            "all_anchors_match": m2_payload["anchors"] == oracle_anchors,
        }
        m2_checks["status"] = "pass" if all(value for key, value in m2_checks.items() if key not in {"anchor_sequence_count", "status"}) else "fail"

        trace = m3_payload["trace"]
        m3_checks = {
            "full_parity_digest_match": m3_payload["parity_digest"] == oracle["parity_digest"] == _digest_records(trace) == m3.parity_digest,
            "full_ordering_digest_match": m3_payload["ordering_digest"] == oracle["ordering_digest"] == _digest_records([_ordering_record(record) for record in trace]) == m3.ordering_digest,
            "ordering_checkpoints_match": m3_payload["ordering_checkpoints"] == oracle["ordering_checkpoints"],
            "semantic_checkpoints_match": m3_payload["semantic_checkpoints"] == oracle["semantic_checkpoints"],
            "exact_trace_match": trace == oracle_records,
        }
        m3_checks["status"] = "pass" if all(value for key, value in m3_checks.items() if key != "status") else "fail"
        mode_audit = {
            "event_slug": event.event_slug,
            "modes": {
                "M1": {**{key: value for key, value in m1_audit.items() if key != "event_slug"}},
                "M2": m2_checks,
                "M3": m3_checks,
            },
        }
        mode_audits.append(mode_audit)
        if any(mode_audit["modes"][mode]["status"] != "pass" for mode in ("M1", "M2", "M3")):
            raise ValueError(f"decoded M1/M2/M3 oracle audit failed for {event.event_slug}")
    return {
        "g004_ordering_scope": "O1_only",
        "independent_source_scan": scans,
        "m1_audit": audits,
        "mode_audits": mode_audits,
        "g002_o3_external_evidence": {
            "artifact": _portable_path(G002_O3_ARTIFACT),
            "artifact_sha256": sha256_file(G002_O3_ARTIFACT),
            "scope": "external_reference_only_not_rerun_not_claimed_by_g004",
            "claim_boundary": "G002 O3 evidence is not a G004 O3 benchmark or parity claim",
        },
    }


def run_cache_resume_tests(event: EventSpec, output_dir: Path) -> dict[str, Any]:
    probe_dir = Path(output_dir) / "cache_resume_probe"
    base = run_mode(event, Mode.M3, output_dir=probe_dir)
    assert base.artifact_path is not None
    path = base.artifact_path
    manifest_path = _cache_manifest_path(path)
    identity = _cache_identity(event, Mode.M3, {})
    original_payload = path.read_bytes()
    original_manifest = manifest_path.read_bytes()

    def detected(mutator: Callable[[], None]) -> bool:
        path.write_bytes(original_payload)
        manifest_path.write_bytes(original_manifest)
        mutator()
        try:
            _read_cache_payload(path, identity)
        except Exception:
            return True
        return False

    def bitflip() -> None:
        changed = bytearray(original_payload)
        changed[len(changed) // 2] ^= 1
        path.write_bytes(changed)

    bitflip_detected = detected(bitflip)
    truncate_detected = detected(lambda: path.write_bytes(original_payload[: len(original_payload) // 2]))
    manifest_missing_detected = detected(lambda: manifest_path.unlink())
    manifest_corrupt_detected = detected(lambda: manifest_path.write_text("{corrupt", encoding="utf-8"))
    path.write_bytes(original_payload)
    manifest_path.write_bytes(original_manifest)

    interrupt_dir = Path(output_dir) / "cache_resume_interrupt"
    with contextlib.suppress(IntentionalInterruption):
        run_mode(event, Mode.M3, output_dir=interrupt_dir, config={"fail_after_rows": 1})
    resumed = run_mode(event, Mode.M3, output_dir=interrupt_dir)
    partial_prevented = resumed.resume_behavior["ignored_partial_artifacts"] >= 1 and resumed.resume_behavior["reused_completed_artifact"] is False
    return {
        "bitflip_detected": bitflip_detected, "truncate_detected": truncate_detected,
        "manifest_missing_detected": manifest_missing_detected, "manifest_corrupt_detected": manifest_corrupt_detected,
        "partial_reuse_prevented": partial_prevented, "interrupted_resume_digest": resumed.parity_digest,
        "committed_matching_payload_only": True, "failure_isolation": "per_event_token_cache_identity_no_partial_reuse",
    }


def _strict_materialization_decision(evidence: dict[str, Any]) -> dict[str, Any]:
    allowed_events = []
    for event in evidence.get("events", []):
        allowed_modes = {}
        for mode_name, metrics in event.get("modes", {}).items():
            allowed_modes[mode_name] = {
                key: metrics.get(key) for key in (
                    "cold_elapsed_seconds", "warm_elapsed_seconds", "peak_rss_bytes", "warm_peak_rss_bytes",
                    "process_logical_read_bytes", "process_logical_write_bytes", "source_file_bytes_upper_bound",
                    "source_pass_count", "artifact_bytes", "row_count", "primitive_count", "parity_digest",
                )
            }
        allowed_events.append({"role": event.get("role"), "event_slug": event.get("event_slug"), "parity_pass": event.get("parity_pass"), "modes": allowed_modes})
    sanitized = {
        "events": allowed_events,
        "ambiguity_role_status": evidence.get("ambiguity_role_status"),
        "complete_date_status": evidence.get("complete_date_status"),
        "complete_date_expected_task_count": evidence.get("complete_date_expected_task_count"),
    }
    resource_output = {
        "event_count": len(allowed_events),
        "mode_count": sum(len(event["modes"]) for event in allowed_events),
        "cold_elapsed_seconds_total": sum(float(mode["cold_elapsed_seconds"] or 0) for event in allowed_events for mode in event["modes"].values()),
        "artifact_bytes_total": sum(int(mode["artifact_bytes"] or 0) for event in allowed_events for mode in event["modes"].values()),
        "source_file_bytes_upper_bound_total_by_mode": sum(int(mode["source_file_bytes_upper_bound"] or 0) for event in allowed_events for mode in event["modes"].values()),
    }
    decision = {
        "status": "deferred" if sanitized["ambiguity_role_status"] != "complete" or sanitized["complete_date_status"] != "complete" else "criteria_complete_no_mode_selected_here",
        "performance_claims_allowed": False, "not_for_pnl": True,
    }
    return {
        "sanitized_input_hash": stable_hash(sanitized), "decision_output": decision,
        "decision_output_hash": stable_hash(decision), "resource_output": resource_output,
        "resource_output_hash": stable_hash(resource_output),
    }


def outcome_blind_invariance(event_reports: list[dict[str, Any]], complete_date: dict[str, Any]) -> dict[str, Any]:
    baseline = {
        "events": event_reports, "ambiguity_role_status": "deferred",
        "complete_date_status": complete_date["status"],
        "complete_date_expected_task_count": complete_date["expected_task_count"],
        "outcome_alias": "BENIGN_SENTINEL_NOT_CONSUMED",
        "settlement": {"winner": "BENIGN_SENTINEL_NOT_CONSUMED"},
        "future_return": "BENIGN_SENTINEL_NOT_CONSUMED",
        "factor": "BENIGN_SENTINEL_NOT_CONSUMED",
        "signal": "BENIGN_SENTINEL_NOT_CONSUMED",
        "strategy": "BENIGN_SENTINEL_NOT_CONSUMED",
        "fills": ["BENIGN_SENTINEL_NOT_CONSUMED"],
        "PnL": "BENIGN_SENTINEL_NOT_CONSUMED",
    }
    poisoned = copy.deepcopy(baseline)
    poisoned.update({
        "outcome_alias": "POISON_WINNER", "settlement": {"winner": "POISON"},
        "future_return": 999, "factor": "POISON", "signal": "POISON", "strategy": "POISON",
        "fills": [{"price": 999}], "PnL": 999,
    })
    for event in poisoned["events"]:
        event["resolved_outcome"] = "POISON"
        for metrics in event["modes"].values():
            metrics["strategy_decision"] = "POISON"
            metrics["fills_and_pnl"] = "POISON"
    removed = copy.deepcopy(poisoned)
    for key in ("outcome_alias", "settlement", "future_return", "factor", "signal", "strategy", "fills", "PnL"):
        removed.pop(key)
    for event in removed["events"]:
        event.pop("resolved_outcome", None)
        for metrics in event["modes"].values():
            metrics.pop("strategy_decision", None)
            metrics.pop("fills_and_pnl", None)

    raw_inputs = [("baseline", baseline), ("poisoned", poisoned), ("removed", removed)]
    runs = []
    outputs = []
    for name, raw in raw_inputs:
        output = _strict_materialization_decision(raw)
        outputs.append(output)
        runs.append({
            "variant": name, "raw_input_hash": stable_hash(raw),
            "sanitized_input_hash": output["sanitized_input_hash"],
            "decision_output_hash": output["decision_output_hash"],
            "resource_output_hash": output["resource_output_hash"],
            "decision_output": output["decision_output"], "resource_output": output["resource_output"],
        })
    equivalent = len({item["sanitized_input_hash"] for item in outputs}) == 1 and len({item["decision_output_hash"] for item in outputs}) == 1 and len({item["resource_output_hash"] for item in outputs}) == 1
    raw_hashes = {run["raw_input_hash"] for run in runs}
    if len(raw_hashes) != 3:
        raise ValueError("outcome-blind invariance requires three genuinely distinct raw inputs")
    return {
        "equivalent": equivalent,
        "raw_inputs_distinct": True,
        "raw_hash_unique_count": len(raw_hashes),
        "method": "baseline_benign_prohibited_sentinels_then_mutated_poisoned_then_removed_through_one_strict_allowlisted_decision_resource_function",
        "poisoned_field_classes": ["aliases", "outcomes", "settlement", "future_returns", "factors", "signals", "strategies", "fills", "PnL"],
        "strict_allowed_surface": ["event identity/role", "M1/M2/M3 resource metrics and parity", "ambiguity status", "complete-date feasibility status/count"],
        "runs": runs,
    }


def complete_date_probe(
    events: list[EventSpec], *, expected_task_ids: list[str] | None = None,
    representative_m3_to_source_ratio: float | None = None,
) -> dict[str, Any]:
    if expected_task_ids is None:
        expected_task_ids = list((load_representative_sample().get("complete_date") or {}).get("event_slugs", []))
    if len(expected_task_ids) != 49 or len(set(expected_task_ids)) != 49:
        raise ValueError("complete-date manifest feasibility requires exactly 49 unique frozen Jun4 event slugs")
    supplied = {event.event_slug: event for event in events}
    statuses = []
    for slug in expected_task_ids:
        event = supplied.get(slug)
        statuses.append({
            "task_id": slug, "event_slug": slug, "status": "manifest_feasibility_only",
            "manifest_available": event is not None,
            "manifest_projected_rows": int(event.rows_written) if event else 0,
            "manifest_projected_source_file_bytes_upper_bound": event.orderbook_path.stat().st_size if event and event.orderbook_path.exists() else 0,
        })
    rows = sum(item["manifest_projected_rows"] for item in statuses)
    source_bytes = sum(item["manifest_projected_source_file_bytes_upper_bound"] for item in statuses)
    return {
        "status": "deferred",
        "reason": "all 49 frozen Jun4 task IDs are manifest-feasibility-only; no full-date M1/M2/M3 execution or completion is claimed",
        "expected_task_count": 49, "manifest_feasibility_only_count": 49,
        "manifest_available_event_count": sum(bool(item["manifest_available"]) for item in statuses),
        "task_statuses": statuses,
        "manifest_projected_rows_total": rows,
        "manifest_projected_source_file_bytes_upper_bound_total": source_bytes,
        "representative_m3_artifact_to_source_file_upper_bound_ratio": representative_m3_to_source_ratio,
        "manifest_projected_m3_artifact_bytes": round(source_bytes * representative_m3_to_source_ratio) if representative_m3_to_source_ratio is not None else None,
        "projection_scope": "capacity_projection_only_not_a_complete_date_benchmark",
        "full_49_event_m3_attempted": False,
    }


def run_benchmark(
    events: list[EventSpec], *, output_dir: Path, include_complete_date: bool = False,
    complete_date_events: list[EventSpec] | None = None,
    complete_date_task_ids: list[str] | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    event_reports: list[dict[str, Any]] = []
    mode_results: dict[str, dict[Mode, ModeResult]] = {}
    for event in events:
        modes: dict[str, Any] = {}
        event_mode_results: dict[Mode, ModeResult] = {}
        for mode in (Mode.M1, Mode.M2, Mode.M3):
            config = {"anchor_every_rows": 1000} if mode == Mode.M2 else {}
            cold = run_mode(event, mode, output_dir=output_dir, config=config)
            application_warm = run_mode(event, mode, output_dir=output_dir, config=config)
            event_mode_results[mode] = cold
            metrics = cold.metrics()
            metrics.update({
                "warm_elapsed_seconds": application_warm.warm_elapsed_seconds if mode != Mode.M1 else application_warm.cold_elapsed_seconds,
                "warm_peak_rss_bytes": application_warm.peak_rss_bytes,
                "warm_process_logical_read_bytes": application_warm.process_logical_read_bytes,
                "warm_process_logical_write_bytes": application_warm.process_logical_write_bytes,
                "warm_source_pass_count": application_warm.source_pass_count,
                "warm_source_hash_pass_count": application_warm.source_hash_pass_count,
                "warm_source_materialization_pass_count": application_warm.source_materialization_pass_count,
                "warm_cache_status": application_warm.cache_status,
                "application_cold_os_cache": "unknown",
            })
            modes[mode.value] = metrics
        mode_results[event.event_slug] = event_mode_results
        parity = {metrics["parity_digest"] for metrics in modes.values()}
        event_reports.append({
            "event_slug": event.event_slug, "role": event.role, "modes": modes,
            "parity_pass": len(parity) == 1,
            "hard_break_status": "inventory_role_degraded_case_requires_manifest_intersection_evidence" if event.role == "degraded_event_local_missing_case" else "not_flagged_by_representative_role",
            "natural_staleness_status": "distinct_from_hard_break; update gaps alone are not hard breaks",
        })

    representative_source = sum(event["modes"]["M3"]["source_file_bytes_upper_bound"] for event in event_reports)
    representative_m3 = sum(event["modes"]["M3"]["artifact_bytes"] for event in event_reports)
    ratio = representative_m3 / representative_source if representative_source else None
    if include_complete_date:
        complete = complete_date_probe(complete_date_events or [], expected_task_ids=complete_date_task_ids, representative_m3_to_source_ratio=ratio)
    else:
        complete = complete_date_probe([], expected_task_ids=complete_date_task_ids, representative_m3_to_source_ratio=ratio)
    parity_oracles = build_parity_oracles(events, mode_results) if events else {
        "g004_ordering_scope": "O1_only", "independent_source_scan": [], "m1_audit": [],
        "mode_audits": [],
        "g002_o3_external_evidence": {"artifact": _portable_path(G002_O3_ARTIFACT), "artifact_sha256": sha256_file(G002_O3_ARTIFACT), "scope": "external_reference_only_not_rerun_not_claimed_by_g004"},
    }
    cache_tests = run_cache_resume_tests(events[-1], output_dir) if events else {}
    invariance = outcome_blind_invariance(event_reports, complete)
    decision = {
        "status": "deferred",
        "reason": "ambiguity role remains external/resource-deferred and all 49 Jun4 tasks remain manifest-feasibility-only",
        "performance_claims_allowed": False, "not_for_pnl": True,
    }
    report = {
        "program": PROGRAM, "program_version": PROGRAM_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "benchmark_manifest": {
            "program": PROGRAM, "program_version": PROGRAM_VERSION,
            "protocol_version": PROTOCOL_VERSION, "protocol_hash": PROTOCOL_HASH,
            "ordering_scope": "O1_only", "ordering_domain": ORDERING_DOMAIN, "ordering_version": ORDERING_VERSION,
            "kernel_schema": KERNEL_SCHEMA, "kernel_version": KERNEL_VERSION,
            "label_schema": LABEL_SCHEMA, "label_schema_version": LABEL_SCHEMA_VERSION,
            "source_access_method": "sha256_source_byte_pass_plus_bounded_pyarrow_row_group_batch_streaming_selected_token_O1_sort_buffer",
            "source_rows_scanned_semantics": "parsed rows from materialization pass only; source hash pass reads bytes and parses zero rows",
            "application_cold_os_cache": "unknown", "worker_count": 1,
            "process_io_method": "Windows_GetProcessIoCounters_ReadTransferCount_WriteTransferCount_delta",
            "process_io_scope": "per_call_process_logical_transfer_bytes_not_physical_disk",
            "peak_rss_method": "Windows_PSAPI_GetProcessMemoryInfo_WorkingSetSize_sampled_every_10ms",
            "peak_rss_scope": "sampled_process_working_set_during_call_not_python_allocator_memory",
            "hard_break_policy": "manifest_missing_or_corrupt_hours_are_hard_breaks_only_when_they_intersect_the_event_window",
            "natural_staleness_policy": "observed_update_gap_without_external_missing_or_corrupt_evidence_is_not_a_hard_break",
            "performance_claims_allowed": False, "not_for_pnl": True,
        },
        "representative_sample_g004": {
            "events": [{"role": event.role, "event_slug": event.event_slug, "event_date": event.event_date, "city": event.city} for event in events],
            "ambiguity_role_status": "deferred",
            "ambiguity_boundary": "G003 real scan resource-deferred; bounded real G002 ordering-parity artifact is external representative evidence and not global heaviest",
            "g004_ordering_scope": "O1_only", "g002_o3_status": "external_reference_only_not_rerun_not_claimed",
        },
        "performance_claims_allowed": False, "not_for_pnl": True, "outcome_blind": True,
        "current_panel_confirmation_status": "not_confirmatory",
        "ordering_scope": "O1_only", "ordering_domain": ORDERING_DOMAIN,
        "o2_event_wide_diagnostics": "locate_only_never_execution_truth",
        "m1_contract": "one source-content byte hash pass plus one authoritative source materialization pass with selected-token O1 sort buffer; digest only; no persistent derivative",
        "m2_contract": "cold path uses one source-content byte hash pass plus one materialization pass; warm path uses one source-content byte hash pass plus cache lookup/read/validation; sparse causal anchors; disposable committed cache",
        "m3_contract": "cold path uses one source-content byte hash pass plus one materialization pass; warm path uses one source-content byte hash pass plus cache lookup/read/validation; full selected-token primitives only; no orders, fills, strategy, or PnL",
        "events": event_reports, "m1_m2_m3_results": event_reports,
        "parity_oracles": parity_oracles, "cache_resume_tests": cache_tests,
        "outcome_blind_invariance_g004": invariance,
        "complete_date_feasibility": complete,
        "decision": decision, "materialization_decision": decision,
    }
    validate_report_schema(report)
    _write_atomic_json(output_dir / "benchmark_report.json", report)
    for artifact_name in (
        "benchmark_manifest", "representative_sample_g004", "m1_m2_m3_results", "parity_oracles",
        "cache_resume_tests", "outcome_blind_invariance_g004", "complete_date_feasibility", "materialization_decision",
    ):
        _write_atomic_json(output_dir / f"{artifact_name}.json", report[artifact_name])
    return report


def validate_report_schema(report: dict[str, Any]) -> None:  # noqa: C901 - fail-closed joined artifact validation
    if report.get("performance_claims_allowed") is not False or report.get("not_for_pnl") is not True:
        raise ValueError("G004 report must remain research-only and not-for-PnL")
    if report.get("ordering_scope") != "O1_only" or report.get("materialization_decision", {}).get("status") != "deferred":
        raise ValueError("G004 ordering scope must be O1-only and decision must remain deferred")
    for event in report.get("events", []):
        for mode, metrics in event.get("modes", {}).items():
            missing = [field for field in REQUIRED_METRIC_FIELDS if field not in metrics]
            if missing:
                raise ValueError(f"metrics for {event.get('event_slug')} {mode} missing {missing}")
            if metrics["source_file_bytes_upper_bound"] < 0:
                raise ValueError("modes require a clearly named source-file upper bound")
            if metrics["source_hash_pass_count"] != 1:
                raise ValueError("every measured mode requires exactly one source-content hash byte pass")
            if metrics["source_pass_count"] != metrics["source_hash_pass_count"] + metrics["source_materialization_pass_count"]:
                raise ValueError("source pass accounting must equal hash pass plus materialization pass")
            if metrics["source_pass_count"] != 2 or metrics["source_materialization_pass_count"] != 1:
                raise ValueError("cold modes require one hash pass plus one materialization source pass")
            warm_pass_count = metrics.get("warm_source_pass_count")
            warm_hash_pass_count = metrics.get("warm_source_hash_pass_count")
            warm_materialization_pass_count = metrics.get("warm_source_materialization_pass_count")
            if warm_hash_pass_count != 1:
                raise ValueError("warm modes require exactly one measured source-content hash byte pass")
            expected_warm_materialization = 1 if mode == "M1" else 0
            expected_warm_pass_count = 1 + expected_warm_materialization
            if warm_materialization_pass_count != expected_warm_materialization or warm_pass_count != expected_warm_pass_count:
                raise ValueError("warm source pass accounting must distinguish application-warm M1 from cache-warm M2/M3")
    complete = report.get("complete_date_feasibility", {})
    if complete.get("expected_task_count") != 49 or len(complete.get("task_statuses", [])) != 49:
        raise ValueError("complete-date feasibility denominator must remain exactly 49")
    if {item.get("status") for item in complete["task_statuses"]} != {"manifest_feasibility_only"}:
        raise ValueError("complete-date tasks must remain manifest-feasibility-only")
    mode_audits = report.get("parity_oracles", {}).get("mode_audits", [])
    if report.get("events") and len(mode_audits) != len(report["events"]):
        raise ValueError("decoded M1/M2/M3 oracle audit missing event coverage")
    for audit in mode_audits:
        modes = audit.get("modes", {})
        if set(modes) != {"M1", "M2", "M3"} or any(modes[mode].get("status") != "pass" for mode in modes):
            raise ValueError(f"decoded M1/M2/M3 oracle audit failed for {audit.get('event_slug')}")
        if modes["M2"].get("all_anchors_match") is not True or modes["M3"].get("exact_trace_match") is not True:
            raise ValueError(f"decoded M1/M2/M3 oracle audit failed for {audit.get('event_slug')}")
    serialized = json.dumps(report, default=_json_default)
    if "C:\\" in serialized or "C:/" in serialized:
        raise ValueError("generated report contains an absolute Windows path")


def load_representative_sample(path: Path = DEFAULT_REPRESENTATIVE_SAMPLE) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    by_role = {item["role"]: item for item in data.get("selections", [])}
    roles = ["largest_event", "median_volume_event", "degraded_event_local_missing_case"]
    ambiguity = by_role.get("ambiguity_heavy_within_bounded_o1_scan", {})
    token = ambiguity.get("selected_token") or {}
    return {
        "required_roles": [by_role[role] for role in roles],
        "ambiguity_representative": {
            "source": "G002_bounded_real_ordering_parity_external_representative",
            "not_global_heaviest": token.get("candidate_boundary") == "bounded_deterministic_budget_not_global_heaviest",
            "artifact": token.get("external_real_ordering_evidence"), "status": ambiguity.get("status"),
        },
        "complete_date": by_role.get("one_complete_date"),
    }


def _dict_from_pandas(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value) if isinstance(value, str) else dict(value)


def load_event_specs_from_inventory(inventory_dir: Path = DEFAULT_INVENTORY_DIR) -> tuple[list[EventSpec], list[EventSpec], list[str]]:
    sample = load_representative_sample(inventory_dir / "representative_sample.json")
    events_frame = pd.read_parquet(inventory_dir / "event_inventory.parquet")
    tokens_frame = pd.read_parquet(inventory_dir / "token_inventory.parquet")

    def spec_for(role: str, slug: str) -> EventSpec:
        row = events_frame[events_frame["event_slug"] == slug].iloc[0].to_dict()
        token = tokens_frame[tokens_frame["event_slug"] == slug].sort_values(["market_index", "token_side"]).iloc[0].to_dict()
        paths, hashes = _dict_from_pandas(row["paths"]), _dict_from_pandas(row["hashes"])
        return EventSpec(
            role, str(row["event_slug"]), str(row["event_date"]), str(row["city"]), Path(paths["event_dir"]),
            Path(paths["orderbook"]), str(token["condition_id"]), str(token["asset_id"]),
            str(hashes.get("orderbook_content_sha256") or hashes.get("orderbook_identity_sha256") or stable_hash(hashes)),
            int(row["rows_written"]),
        )

    selected = [spec_for(item["role"], item["event_slug"]) for item in sample["required_roles"]]
    complete_slugs = list((sample.get("complete_date") or {}).get("event_slugs", []))
    complete = [spec_for("complete_date_manifest_feasibility", slug) for slug in complete_slugs]
    return selected, complete, complete_slugs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded PMXT G004 O1 materialization benchmark")
    parser.add_argument("--inventory-dir", type=Path, default=DEFAULT_INVENTORY_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--include-complete-date-probe", action="store_true")
    args = parser.parse_args(argv)
    selected, complete, complete_slugs = load_event_specs_from_inventory(args.inventory_dir)
    report = run_benchmark(
        selected, output_dir=args.output_dir, include_complete_date=args.include_complete_date_probe,
        complete_date_events=complete, complete_date_task_ids=complete_slugs,
    )
    print(json.dumps({
        "status": "pass", "output": _portable_path(args.output_dir / "benchmark_report.json"),
        "events": len(report["events"]), "decision": report["materialization_decision"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
