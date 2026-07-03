"""Normalize pre-contract Polymarket capture wrappers into live_ws_v1 NDJSON.

The output format is intentionally strict:

    {"local_msg_index": 1, "recv_wall_time_utc": "...", "raw_json": {...}}

It preserves one input row as one local WebSocket message and refuses to invent a
receive timestamp from the Polymarket source timestamp.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket.adapters.utils import as_utc_datetime


RECEIVE_TIME_KEYS = ("recv_wall_time_utc", "timestamp_received", "received_at")


def normalize_file(input_path: Path, output_path: Path) -> dict[str, Any]:
    """Normalize capture NDJSON into the strict live_ws_v1-readable wrapper."""

    rows_written = 0
    control_messages = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        input_path.open(encoding="utf-8-sig") as src,
        output_path.open("w", encoding="utf-8") as dst,
    ):
        for line_number, line in enumerate(src, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, Mapping):
                raise ValueError(f"line {line_number}: expected JSON object")
            message = _extract_message(payload, line_number=line_number)
            received = _extract_receive_time(payload, message, line_number=line_number)
            normalized = {
                "local_msg_index": int(payload.get("local_msg_index", line_number)),
                "recv_wall_time_utc": received,
                "raw_json": message,
            }
            dst.write(json.dumps(normalized, separators=(",", ":"), ensure_ascii=False) + "\n")
            rows_written += 1
            event_type = str(message.get("event_type") or message.get("type") or "").lower()
            if event_type in {"ping", "pong"}:
                control_messages += 1

    return {
        "input": input_path.as_posix(),
        "output": output_path.as_posix(),
        "rows_written": rows_written,
        "control_messages": control_messages,
        "format": "live_ws_v1",
        "receive_timestamp_policy": "required_explicit_receive_time",
    }


def _extract_message(payload: Mapping[str, Any], *, line_number: int) -> Mapping[str, Any]:
    raw = payload.get("raw_json") or payload.get("message") or payload.get("data")
    if raw is None:
        raw = payload
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError(f"line {line_number}: expected one WebSocket message per row")
        raw = raw[0]
    if not isinstance(raw, Mapping):
        raise ValueError(f"line {line_number}: unsupported raw WebSocket payload {type(raw)!r}")
    return dict(raw)


def _extract_receive_time(
    payload: Mapping[str, Any],
    message: Mapping[str, Any],
    *,
    line_number: int,
) -> str:
    for key in RECEIVE_TIME_KEYS:
        if payload.get(key) is not None:
            return _as_iso_z(payload[key])
    if message.get("timestamp_received") is not None:
        return _as_iso_z(message["timestamp_received"])
    raise ValueError(
        f"line {line_number}: missing explicit receive timestamp; "
        "refusing to use source timestamp as recv_wall_time_utc",
    )


def _as_iso_z(value: Any) -> str:
    return as_utc_datetime(value).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = normalize_file(args.input, args.output)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
