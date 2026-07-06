from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket.adapters.live_event_bundle_v1 import LiveEventBundleV1Adapter


def test_live_event_bundle_requires_explicit_raw_path_when_multiple_ndjson_files(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    raw_dir = bundle / "raw"
    raw_dir.mkdir(parents=True)
    (bundle / "a.ndjson").write_text("{}\n", encoding="utf-8")
    (raw_dir / "b.ndjson").write_text("{}\n", encoding="utf-8")

    adapter = LiveEventBundleV1Adapter(repo_root=Path.cwd())
    with pytest.raises(ValueError, match="multiple NDJSON candidates"):
        adapter.load({"input": {"bundle_dir": str(bundle)}})


def test_live_event_bundle_preserves_market_metadata_from_live_ws_adapter(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    raw_path = bundle / "raw.ndjson"
    raw_path.write_text(
        json.dumps(
            {
                "local_msg_index": 1,
                "recv_wall_time_utc": "2026-01-01T00:00:00Z",
                "raw_json": {
                    "event_type": "book",
                    "market": "condition",
                    "asset_id": "yes",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "bids": [["0.40", "10"]],
                    "asks": [["0.60", "10"]],
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )
    metadata_path = bundle / "market_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "condition_id": "condition",
                        "minimum_tick_size": "0.01",
                        "resolution_status": "resolved",
                        "resolution_time": "2026-01-01T00:01:00Z",
                        "tokens": [
                            {"token_id": "yes", "outcome": "Yes", "payout": "1"},
                            {"token_id": "no", "outcome": "No", "payout": "0"},
                        ],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    dataset = LiveEventBundleV1Adapter(repo_root=Path.cwd()).load(
        {
            "input": {
                "bundle_dir": str(bundle),
                "raw_ws_path": str(raw_path),
                "market_metadata_path": str(metadata_path),
            },
        },
    )

    assert dataset.metadata.adapter_name == "live_event_bundle_v1"
    assert len(dataset.metadata.market_metadata) == 2
    yes_metadata = next(item for item in dataset.metadata.market_metadata if item.token_id == "yes")
    assert yes_metadata.minimum_tick_size == Decimal("0.01")
    assert yes_metadata.token_payout == Decimal("1")
    assert yes_metadata.resolution_status == "resolved"
