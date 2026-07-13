"""Tests for the shared PMXT research replay contract and runner mode gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from polymarket._core.models import L2ReplayStepV1
from polymarket._core.models import L2UpdateV1
from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter
from polymarket.replay_contract import DEFAULT_REPLAY_MODE
from polymarket.replay_contract import PMXT_REPLAY_CLOCK
from polymarket.replay_contract import PMXT_RESEARCH_MODE
from polymarket.replay_contract import PMXT_RESEARCH_ORDERING_KEY
from polymarket.replay_contract import STRICT_CAPTURE_MODE
from polymarket.replay_contract import blocking_health_issues
from polymarket.replay_contract import build_replay_provenance
from polymarket.replay_contract import enforce_replay_mode_gate
from polymarket.replay_contract import replay_timestamp
from polymarket.replay_contract import resolve_replay_mode
from polymarket.replay_contract import verify_pmxt_replay_clock_order


CONDITION_ID = "0xabc123"
YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"  # noqa: S105 - synthetic fixture token, not a secret
NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"  # noqa: S105 - synthetic fixture token, not a secret
FACTOR_BASELINE_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-08-pmxt-l2-factor-baseline"
    / "factor_research.py"
)


def _dt(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=UTC)


def _iso(seconds: int) -> str:
    return _dt(seconds).isoformat().replace("+00:00", "Z")


def _step(sequence: int, *, received: int, source: int | None) -> L2ReplayStepV1:
    return L2ReplayStepV1(
        sequence=sequence,
        timestamp_received=_dt(received),
        timestamp=_dt(source) if source is not None else None,
        updates=(
            L2UpdateV1(event_type="price_change", market=CONDITION_ID, asset_id=YES_TOKEN, side="BUY"),
        ),
    )


def _row(
    *,
    event_type: str = "price_change",
    received: int,
    source: int | None,
    price: str | None = "0.41",
    size: str | None = "5",
    side: str | None = "BUY",
    bids: str | None = None,
    asks: str | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "market": CONDITION_ID.encode(),
        "asset_id": YES_TOKEN,
        "timestamp_received": _iso(received),
        "timestamp": _iso(source) if source is not None else None,
        "bids": bids,
        "asks": asks,
        "side": side,
        "price": price,
        "size": size,
        "best_bid": None,
        "best_ask": None,
        "old_tick_size": None,
        "new_tick_size": None,
    }


def _write_event_dir(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    event_dir = tmp_path / "pmxt-event"
    event_dir.mkdir()
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False)
    (event_dir / "gamma_event.raw.json").write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "conditionId": CONDITION_ID,
                        "outcomes": json.dumps(["Yes", "No"]),
                        "outcomePrices": json.dumps(["1", "0"]),
                        "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
                        "feeSchedule": json.dumps({"rate": "0.02"}),
                        "umaResolutionStatus": "resolved",
                        "closedTime": "2026-01-02T03:04:05Z",
                        "orderPriceMinTickSize": "0.001",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (event_dir / "event_index.json").write_text(
        json.dumps({"markets": [{"conditionId": CONDITION_ID, "yesToken": YES_TOKEN, "noToken": NO_TOKEN}]}),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-pmxt-test"}), encoding="utf-8")
    return event_dir


def _load_dataset(event_dir: Path) -> Any:
    return PMXTEventV1Adapter(repo_root=Path.cwd()).load(
        {
            "input": {
                "event_dir": str(event_dir),
                "dataset_id": "synthetic-replay-contract",
                "condition_id": CONDITION_ID,
                "asset_id": YES_TOKEN,
            },
        },
    )


def _load_factor_baseline_module() -> Any:
    spec = importlib.util.spec_from_file_location("pmxt_l2_factor_baseline_contract", FACTOR_BASELINE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_replay_mode_is_strict_capture() -> None:
    assert resolve_replay_mode({}) == STRICT_CAPTURE_MODE
    assert DEFAULT_REPLAY_MODE == STRICT_CAPTURE_MODE


def test_unknown_replay_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"unknown replay\.mode"):
        resolve_replay_mode({"replay": {"mode": "casual"}})


def test_mode_gate_default_rejects_pmxt_and_explicit_research_allows_it() -> None:
    with pytest.raises(ValueError, match="strict_capture"):
        enforce_replay_mode_gate({"adapter": {"name": "pmxt_event_v1"}})

    mode = enforce_replay_mode_gate(
        {"adapter": {"name": "pmxt_event_v1"}, "replay": {"mode": "pmxt_research"}},
    )
    assert mode == PMXT_RESEARCH_MODE


def test_mode_gate_research_mode_rejects_strict_adapters() -> None:
    for adapter_name in ("live_ws_v1", "live_event_bundle_v1"):
        with pytest.raises(ValueError, match="must run in strict_capture mode"):
            enforce_replay_mode_gate(
                {"adapter": {"name": adapter_name}, "replay": {"mode": "pmxt_research"}},
            )


def test_replay_timestamp_prefers_source_time_with_receive_fallback() -> None:
    with_source = _step(1, received=10, source=5)
    without_source = _step(2, received=10, source=None)

    assert replay_timestamp(with_source) == _dt(5)
    assert replay_timestamp(without_source) == _dt(10)


def test_blocking_health_issues_ignore_receive_time_inversion_only_in_pmxt_mode() -> None:
    report = SimpleNamespace(
        issues=(
            SimpleNamespace(severity="error", code="receive_time_inversion"),
            SimpleNamespace(severity="error", code="sequence_inversion"),
            SimpleNamespace(severity="warning", code="source_time_inversion"),
        ),
    )

    strict_codes = [issue.code for issue in blocking_health_issues(report, mode=STRICT_CAPTURE_MODE)]
    pmxt_codes = [issue.code for issue in blocking_health_issues(report, mode=PMXT_RESEARCH_MODE)]

    assert strict_codes == ["receive_time_inversion", "sequence_inversion"]
    assert pmxt_codes == ["sequence_inversion"]


def test_verify_pmxt_replay_clock_order_accepts_contract_sorted_steps() -> None:
    dataset = SimpleNamespace(
        steps=(
            _step(1, received=9, source=1),
            _step(2, received=3, source=2),  # receive-time inversion is fine
            _step(3, received=2, source=None),  # falls back to receive time 2... still >= 2
        ),
    )

    check = verify_pmxt_replay_clock_order(dataset)

    assert check["replay_clock"] == PMXT_REPLAY_CLOCK
    assert check["replay_clock_monotonic"] is True
    assert check["step_count"] == 3


def test_verify_pmxt_replay_clock_order_rejects_backwards_clock_and_bad_sequence() -> None:
    backwards = SimpleNamespace(steps=(_step(1, received=1, source=5), _step(2, received=9, source=4)))
    with pytest.raises(ValueError, match="replay clock moved backwards"):
        verify_pmxt_replay_clock_order(backwards)

    bad_sequence = SimpleNamespace(steps=(_step(2, received=1, source=1), _step(2, received=2, source=2)))
    with pytest.raises(ValueError, match="strictly increasing"):
        verify_pmxt_replay_clock_order(bad_sequence)


def test_replay_provenance_marks_pmxt_research_limits() -> None:
    metadata = SimpleNamespace(
        adapter_name="pmxt_event_v1",
        source_files=("a.parquet",),
        source_quality={"orderingStatus": "timestamp_ordered", "orderingAmbiguousRows": 0},
    )

    provenance = build_replay_provenance(mode=PMXT_RESEARCH_MODE, dataset_metadata=metadata)

    assert provenance["mode"] == PMXT_RESEARCH_MODE
    assert provenance["replay_clock"] == PMXT_REPLAY_CLOCK
    assert provenance["ordering_key"] == PMXT_RESEARCH_ORDERING_KEY
    assert provenance["data_credibility"] == "pmxt_research_reconstructed_order"
    assert provenance["execution_claims_allowed"] is False
    assert provenance["matching_level_truth"] is False
    assert "not" in provenance["disclaimer"]
    assert "exchange/message order" in provenance["disclaimer"]


def test_replay_provenance_downgrades_on_ambiguous_ties() -> None:
    metadata = SimpleNamespace(
        adapter_name="pmxt_event_v1",
        source_files=(),
        source_quality={"orderingStatus": "ambiguous", "orderingAmbiguousRows": 4},
    )

    provenance = build_replay_provenance(mode=PMXT_RESEARCH_MODE, dataset_metadata=metadata)

    assert provenance["ordering_ambiguous"] is True
    assert provenance["data_credibility"] == "pmxt_research_reconstructed_order_ambiguous_ties"


def test_replay_provenance_strict_mode_keeps_receive_time_semantics() -> None:
    metadata = SimpleNamespace(adapter_name="live_ws_v1", source_files=(), source_quality={})

    provenance = build_replay_provenance(mode=STRICT_CAPTURE_MODE, dataset_metadata=metadata)

    assert provenance["mode"] == STRICT_CAPTURE_MODE
    assert provenance["replay_clock"] == "timestamp_received"
    assert provenance["execution_claims_allowed"] is True
    assert provenance["matching_level_truth"] is False


def test_adapter_ordering_is_stable_and_matches_contract_key(tmp_path: Path) -> None:
    # Deliberately shuffled physical rows with source-time inversions against
    # receive time and one missing source timestamp.
    rows = [
        _row(received=9, source=4, price="0.44"),
        _row(received=1, source=6, price="0.46"),
        _row(received=5, source=None, price="0.45"),  # sorts on receive fallback 5
        _row(received=2, source=4, price="0.43"),  # tied source ts 4: receive 2 < 9
        _row(
            event_type="book",
            received=3,
            source=1,
            price=None,
            size=None,
            side=None,
            bids=json.dumps([["0.40", "10"]]),
            asks=json.dumps([["0.60", "9"]]),
        ),
    ]
    dataset = _load_dataset(_write_event_dir(tmp_path, rows))

    clocks = [replay_timestamp(step) for step in dataset.steps]
    assert clocks == sorted(clocks)
    assert [step.sequence for step in dataset.steps] == list(range(1, len(rows) + 1))
    # Tie on source ts 4 is broken by receive time: received=2 before received=9.
    tied = [step for step in dataset.steps if step.timestamp == _dt(4)]
    assert [step.timestamp_received for step in tied] == [_dt(2), _dt(9)]
    assert dataset.metadata.source_quality["stableSortKey"] == PMXT_RESEARCH_ORDERING_KEY
    # Loading the same files again yields the identical replay order.
    dataset_again = _load_dataset(tmp_path / "pmxt-event")
    assert [
        (step.sequence, step.timestamp, step.timestamp_received) for step in dataset.steps
    ] == [(step.sequence, step.timestamp, step.timestamp_received) for step in dataset_again.steps]

    check = verify_pmxt_replay_clock_order(dataset)
    assert check["replay_clock_monotonic"] is True


def test_factor_panel_and_backtest_replay_share_step_order_and_clock(tmp_path: Path) -> None:
    rows = [
        _row(received=9, source=4, price="0.44"),
        _row(received=1, source=6, price="0.46"),
        _row(received=5, source=None, price="0.45"),
        _row(
            event_type="book",
            received=3,
            source=1,
            price=None,
            size=None,
            side=None,
            bids=json.dumps([["0.40", "10"]]),
            asks=json.dumps([["0.60", "9"]]),
        ),
    ]
    dataset = _load_dataset(_write_event_dir(tmp_path, rows))
    factor_research = _load_factor_baseline_module()

    panel = factor_research.build_factor_panel(dataset, {"factors": {"max_depth_levels": 5}})

    # Factor research consumes adapter steps as-is: same order, same clock.
    assert list(panel["sequence"]) == [step.sequence for step in dataset.steps]
    assert [ts.to_pydatetime() for ts in panel["replay_timestamp"]] == [
        replay_timestamp(step) for step in dataset.steps
    ]
    assert factor_research.TRUST_METADATA_BASE["ordering_key"] == PMXT_RESEARCH_ORDERING_KEY
    # The backtest runner replays the identical step stream: the pmxt_research
    # gate accepts it and the shared clock check passes on the same dataset.
    assert verify_pmxt_replay_clock_order(dataset)["replay_clock_monotonic"] is True
