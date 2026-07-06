from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket.adapters.pmxt_parquet_v1 import PMXTParquetV1Adapter
from polymarket.adapters.utils import as_utc_datetime


def test_as_utc_datetime_parses_polymarket_epoch_millisecond_strings() -> None:
    parsed = as_utc_datetime("1782440717084")

    assert parsed.isoformat().replace("+00:00", "Z") == "2026-06-26T02:25:17.084000Z"


def test_pmxt_unknown_event_type_fails_loudly(tmp_path: Path) -> None:
    parquet_path = tmp_path / "unknown_event.parquet"
    pd.DataFrame(
        [
            {
                "timestamp_received": pd.Timestamp("2026-01-01T00:00:00Z"),
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "market": "m1",
                "event_type": "unexpected_event",
                "asset_id": "yes",
            },
        ],
    ).to_parquet(parquet_path, index=False)

    with pytest.raises(ValueError, match="unsupported PMXT event_type"):
        PMXTParquetV1Adapter(repo_root=Path.cwd()).load({"input": {"parquet_path": str(parquet_path)}})


def test_pmxt_unknown_prebook_event_type_is_not_hidden_by_drop_until_first_book(tmp_path: Path) -> None:
    parquet_path = tmp_path / "unknown_prebook_then_book.parquet"
    pd.DataFrame(
        [
            {
                "timestamp_received": pd.Timestamp("2026-01-01T00:00:00Z"),
                "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "market": "m1",
                "event_type": "unexpected_event",
                "asset_id": "yes",
            },
            {
                "timestamp_received": pd.Timestamp("2026-01-01T00:00:01Z"),
                "timestamp": pd.Timestamp("2026-01-01T00:00:01Z"),
                "market": "m1",
                "event_type": "book",
                "asset_id": "yes",
                "bids": '[["0.40", "10"]]',
                "asks": '[["0.60", "10"]]',
            },
        ],
    ).to_parquet(parquet_path, index=False)

    with pytest.raises(ValueError, match="unsupported PMXT event_type"):
        PMXTParquetV1Adapter(repo_root=Path.cwd()).load(
            {"input": {"parquet_path": str(parquet_path), "drop_until_first_book": True}},
        )


def test_pmxt_replay_order_is_explicit(tmp_path: Path) -> None:
    parquet_path = tmp_path / "ordering.parquet"
    pd.DataFrame(
        [
            {
                "timestamp_received": pd.Timestamp("2026-01-01T00:00:00Z"),
                "timestamp": pd.Timestamp("2026-01-01T00:00:02Z"),
                "market": "m1",
                "event_type": "book",
                "asset_id": "yes",
                "bids": '[["0.50", "10"]]',
                "asks": '[["0.60", "10"]]',
            },
            {
                "timestamp_received": pd.Timestamp("2026-01-01T00:00:01Z"),
                "timestamp": pd.Timestamp("2026-01-01T00:00:01Z"),
                "market": "m1",
                "event_type": "book",
                "asset_id": "yes",
                "bids": '[["0.55", "10"]]',
                "asks": '[["0.65", "10"]]',
            },
        ],
    ).to_parquet(parquet_path, index=False)
    adapter = PMXTParquetV1Adapter(repo_root=Path.cwd())

    received = adapter.load({"input": {"parquet_path": str(parquet_path), "replay_order": "received_time"}})
    source = adapter.load({"input": {"parquet_path": str(parquet_path), "replay_order": "source_time"}})

    assert received.steps[0].updates[0].best_bid is None
    assert received.steps[0].updates[0].bids[0].price == Decimal("0.50")
    assert source.steps[0].updates[0].bids[0].price == Decimal("0.55")


def test_live_ws_unknown_event_type_fails_loudly(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "unknown_live.ndjson"
    ndjson_path.write_text(
        json.dumps(
            {
                "local_msg_index": 1,
                "recv_wall_time_utc": "2026-01-01T00:00:00Z",
                "raw_json": {"event_type": "unexpected_event", "timestamp": "2026-01-01T00:00:00Z"},
            },
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported live WS event_type"):
        LiveWsV1Adapter(repo_root=Path.cwd()).load({"input": {"ndjson_path": str(ndjson_path)}})


def test_live_ws_control_messages_are_explicitly_skipped_with_warning(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "control_then_book.ndjson"
    lines = [
        {
            "local_msg_index": 1,
            "recv_wall_time_utc": "2026-01-01T00:00:00Z",
            "raw_json": {"type": "PING", "timestamp": "2026-01-01T00:00:00Z"},
        },
        {
            "local_msg_index": 2,
            "recv_wall_time_utc": "2026-01-01T00:00:01Z",
            "raw_json": {
                "event_type": "book",
                "market": "m1",
                "asset_id": "yes",
                "timestamp": "2026-01-01T00:00:01Z",
                "bids": [["0.40", "10"]],
                "asks": [["0.60", "10"]],
            },
        },
    ]
    ndjson_path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    dataset = LiveWsV1Adapter(repo_root=Path.cwd()).load({"input": {"ndjson_path": str(ndjson_path)}})

    assert len(dataset.steps) == 1
    assert dataset.steps[0].sequence == 2
    assert any("Skipped 1 explicit live WS control" in warning for warning in dataset.metadata.warnings)


def test_live_ws_loads_market_fee_metadata_sidecar(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    ndjson_path.write_text(
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
    metadata_path = tmp_path / "market_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "condition_id": "condition",
                        "minimum_tick_size": "0.01",
                        "maker_fee": "0",
                        "feeSchedule": {"rate": "0.05"},
                        "fee_source": "clob_market_info.feeSchedule.rate",
                        "category": "weather",
                        "resolution_status": "resolved",
                        "resolution_time": "2026-01-01T00:01:00Z",
                        "tokens": [
                            {"token_id": "yes", "outcome": "Yes", "payout": "1", "winner": True},
                            {"token_id": "no", "outcome": "No", "payout": "0", "winner": False},
                        ],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    dataset = LiveWsV1Adapter(repo_root=Path.cwd()).load(
        {
            "input": {
                "ndjson_path": str(ndjson_path),
                "market_metadata_path": str(metadata_path),
            },
        },
    )

    assert len(dataset.metadata.market_metadata) == 2
    market_metadata = next(item for item in dataset.metadata.market_metadata if item.token_id == "yes")
    assert market_metadata.condition_id == "condition"
    assert market_metadata.token_id == "yes"
    assert market_metadata.outcome == "Yes"
    assert market_metadata.maker_fee == Decimal("0")
    assert market_metadata.taker_fee == Decimal("0.05")
    assert market_metadata.fee_source == "clob_market_info.feeSchedule.rate"
    assert market_metadata.minimum_tick_size == Decimal("0.01")
    assert market_metadata.token_payout == Decimal("1")
    assert market_metadata.winner is True
    assert market_metadata.resolution_status == "resolved"
