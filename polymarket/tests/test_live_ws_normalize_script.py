from __future__ import annotations

import json
from pathlib import Path

import pytest

from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket._tools.normalize_live_ws_v1 import normalize_file


def test_normalize_live_ws_capture_writes_adapter_readable_ndjson(tmp_path: Path) -> None:
    source = tmp_path / "capture.ndjson"
    output = tmp_path / "normalized.ndjson"
    source.write_text(
        json.dumps(
            {
                "local_msg_index": 7,
                "received_at": "2026-06-26T02:25:28.635Z",
                "message": json.dumps(
                    {
                        "event_type": "price_change",
                        "market": "condition",
                        "timestamp": "2026-06-26T02:25:28.600Z",
                        "price_changes": [
                            {
                                "asset_id": "yes",
                                "side": "BUY",
                                "price": "0.40",
                                "size": "10",
                            },
                        ],
                    },
                ),
            },
        )
        + "\n",
        encoding="utf-8",
    )

    summary = normalize_file(source, output)
    dataset = LiveWsV1Adapter(repo_root=Path.cwd()).load({"input": {"ndjson_path": str(output)}})

    assert summary["rows_written"] == 1
    assert dataset.steps[0].sequence == 1
    assert dataset.steps[0].updates[0].event_type == "price_change"
    assert dataset.steps[0].updates[0].asset_id == "yes"


def test_normalize_live_ws_capture_refuses_to_invent_receive_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "capture.ndjson"
    output = tmp_path / "normalized.ndjson"
    source.write_text(
        json.dumps(
            {
                "raw_json": {
                    "event_type": "book",
                    "market": "condition",
                    "asset_id": "yes",
                    "timestamp": "2026-06-26T02:25:28.600Z",
                    "bids": [],
                    "asks": [],
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing explicit receive timestamp"):
        normalize_file(source, output)


def test_normalize_live_ws_capture_splits_raw_text_arrays_and_skips_non_l2(
    tmp_path: Path,
) -> None:
    source = tmp_path / "capture.ndjson"
    output = tmp_path / "normalized.ndjson"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "local_msg_index": 1,
                        "recv_wall_time_utc": "2026-06-26T02:25:28.635Z",
                        "raw_text": json.dumps(
                            [
                                {
                                    "event_type": "book",
                                    "market": "condition",
                                    "asset_id": "yes",
                                    "timestamp": "2026-06-26T02:25:28.600Z",
                                    "bids": [],
                                    "asks": [],
                                },
                                {
                                    "event_type": "book",
                                    "market": "condition",
                                    "asset_id": "no",
                                    "timestamp": "2026-06-26T02:25:28.600Z",
                                    "bids": [],
                                    "asks": [],
                                },
                            ],
                        ),
                    },
                ),
                json.dumps(
                    {
                        "local_msg_index": 2,
                        "recv_wall_time_utc": "2026-06-26T02:25:29.000Z",
                        "raw_text": "PONG",
                    },
                ),
                json.dumps(
                    {
                        "local_msg_index": 3,
                        "recv_wall_time_utc": "2026-06-26T02:25:30.000Z",
                        "raw_text": json.dumps({"event_type": "best_bid_ask", "market": "condition"}),
                    },
                ),
            ],
        )
        + "\n",
        encoding="utf-8",
    )

    summary = normalize_file(source, output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    dataset = LiveWsV1Adapter(repo_root=Path.cwd()).load({"input": {"ndjson_path": str(output)}})

    assert summary["rows_written"] == 3
    assert summary["control_messages"] == 1
    assert summary["skipped_unsupported_messages"] == 1
    assert summary["split_array_messages"] == 1
    assert [row["local_msg_index"] for row in rows] == [1, 2, 3]
    assert [row["source_local_msg_index"] for row in rows] == [1, 1, 2]
    assert [step.sequence for step in dataset.steps] == [1, 2]
    assert [step.updates[0].asset_id for step in dataset.steps] == ["yes", "no"]

