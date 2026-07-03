"""Normalize pre-contract Polymarket capture wrappers into live_ws_v1 NDJSON.

The output format is intentionally strict:

    {"local_msg_index": 1, "recv_wall_time_utc": "...", "raw_json": {...}}

It preserves input WebSocket message order, splits JSON-array messages into
multiple replay-readable rows, skips non-L2 informational events, and refuses to
invent a receive timestamp from the Polymarket source timestamp.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from polymarket.adapters.utils import as_utc_datetime


RECEIVE_TIME_KEYS = ("recv_wall_time_utc", "timestamp_received", "received_at")
SUPPORTED_EVENT_TYPES = frozenset(
    {
        "book",
        "price_change",
        "last_trade_price",
        "tick_size_change",
        "ping",
        "pong",
    },
)


def normalize_file(input_path: Path, output_path: Path) -> dict[str, Any]:
    """Normalize capture NDJSON into the strict live_ws_v1-readable wrapper."""

    rows_written = 0
    control_messages = 0
    skipped_unsupported_messages = 0
    split_array_messages = 0
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
            messages = _extract_messages(payload, line_number=line_number)
            if len(messages) > 1:
                split_array_messages += 1
            received = _extract_receive_time(payload, line_number=line_number)
            for part_index, message in enumerate(messages, start=1):
                event_type = str(message.get("event_type") or message.get("type") or "").lower()
                if event_type not in SUPPORTED_EVENT_TYPES:
                    skipped_unsupported_messages += 1
                    continue
                rows_written += 1
                normalized = {
                    "local_msg_index": rows_written,
                    "source_local_msg_index": int(payload.get("local_msg_index", line_number)),
                    "source_message_part_index": part_index,
                    "recv_wall_time_utc": received,
                    "raw_json": message,
                }
                dst.write(json.dumps(normalized, separators=(",", ":"), ensure_ascii=False) + "\n")
                if event_type in {"ping", "pong"}:
                    control_messages += 1

    return {
        "input": input_path.as_posix(),
        "output": output_path.as_posix(),
        "rows_written": rows_written,
        "control_messages": control_messages,
        "skipped_unsupported_messages": skipped_unsupported_messages,
        "split_array_messages": split_array_messages,
        "format": "live_ws_v1",
        "receive_timestamp_policy": "required_explicit_receive_time",
    }


def _extract_messages(payload: Mapping[str, Any], *, line_number: int) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get("raw_json") or payload.get("message") or payload.get("data") or payload.get("raw_text")
    if raw is None:
        raw = payload
    if isinstance(raw, str):
        text = raw.strip()
        if text.upper() in {"PING", "PONG"}:
            raw = {"event_type": text.lower()}
        else:
            raw = json.loads(text)
    if isinstance(raw, list):
        messages = raw
    else:
        messages = [raw]
    if not all(isinstance(message, Mapping) for message in messages):
        raise ValueError(f"line {line_number}: unsupported raw WebSocket payload {type(raw)!r}")
    return tuple(dict(message) for message in messages)


def _extract_receive_time(
    payload: Mapping[str, Any],
    *,
    line_number: int,
) -> str:
    for key in RECEIVE_TIME_KEYS:
        if payload.get(key) is not None:
            return _as_iso_z(payload[key])
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
