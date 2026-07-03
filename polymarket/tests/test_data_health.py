from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket.data_health import DataHealthError
from polymarket.data_health import analyze_dataset_health
from polymarket.data_health import validate_dataset_for_backtest
from polymarket.models import DatasetMetadataV1
from polymarket.models import L2ReplayStepV1
from polymarket.models import L2UpdateV1
from polymarket.models import PolymarketL2DatasetV1


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def ts(milliseconds: int) -> datetime:
    return BASE + timedelta(milliseconds=milliseconds)


def step(sequence: int, received_ms: int, source_ms: int) -> L2ReplayStepV1:
    return L2ReplayStepV1(
        sequence=sequence,
        timestamp_received=ts(received_ms),
        timestamp=ts(source_ms),
        updates=(
            L2UpdateV1(
                event_type="price_change",
                market="condition",
                asset_id="yes",
                side="BUY",
                price=Decimal("0.40"),
                size=Decimal("10"),
            ),
        ),
    )


def dataset(steps: list[L2ReplayStepV1]) -> PolymarketL2DatasetV1:
    return PolymarketL2DatasetV1(
        metadata=DatasetMetadataV1(
            dataset_id="data-health-test",
            adapter_name="synthetic",
            adapter_version="v1",
            source_type="test",
        ),
        steps=tuple(steps),
    )


def test_data_health_passes_receive_order_and_reports_future_source_time() -> None:
    report = validate_dataset_for_backtest(
        dataset(
            [
                step(sequence=1, received_ms=100, source_ms=100),
                step(sequence=2, received_ms=200, source_ms=275),
            ],
        ),
        future_tolerance_ms=50,
    )

    assert report.ok is True
    assert report.summary.receive_time_inversion_count == 0
    assert report.summary.future_source_time_count == 1
    assert report.summary.max_future_source_time_ms == 75
    assert [issue.code for issue in report.issues] == ["future_source_time"]


def test_data_health_fails_receive_time_inversion_without_sorting() -> None:
    bad = dataset(
        [
            step(sequence=1, received_ms=200, source_ms=200),
            step(sequence=2, received_ms=100, source_ms=100),
        ],
    )

    with pytest.raises(DataHealthError, match="Inspect data_health.json instead of sorting"):
        validate_dataset_for_backtest(bad)

    report = analyze_dataset_health(bad)
    assert report.ok is False
    assert report.summary.receive_time_inversion_count == 1
    assert [issue.code for issue in report.issues if issue.severity == "error"] == [
        "receive_time_inversion",
    ]


def test_data_health_reports_source_inversion_as_diagnostic_only() -> None:
    report = validate_dataset_for_backtest(
        dataset(
            [
                step(sequence=1, received_ms=200, source_ms=200),
                step(sequence=2, received_ms=300, source_ms=150),
            ],
        ),
    )

    assert report.ok is True
    assert report.summary.source_time_inversion_count == 1
    assert [issue.code for issue in report.issues] == ["source_time_inversion"]


def test_data_health_reports_source_delay_over_threshold() -> None:
    report = validate_dataset_for_backtest(
        dataset([step(sequence=1, received_ms=2_000, source_ms=0)]),
        delay_warning_ms=1_000,
    )

    assert report.ok is True
    assert report.summary.source_delay_over_threshold_count == 1
    assert report.summary.max_source_delay_ms == 2_000
    assert [issue.code for issue in report.issues] == ["source_delay_over_threshold"]


def test_live_ws_requires_explicit_receive_timestamp(tmp_path: Path) -> None:
    ndjson = tmp_path / "missing_receive.ndjson"
    ndjson.write_text(
        '{"raw_json":{"event_type":"book","market":"m1","asset_id":"yes",'
        '"timestamp":"2026-01-01T00:00:00Z","bids":[],"asks":[]}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires an explicit receive timestamp"):
        LiveWsV1Adapter(repo_root=Path.cwd()).load({"input": {"ndjson_path": str(ndjson)}})
