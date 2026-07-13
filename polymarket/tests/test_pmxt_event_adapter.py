from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter
from polymarket.data_health import analyze_dataset_health


CONDITION_ID = "0xabc123"
OTHER_CONDITION_ID = "0xdef456"
YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"  # noqa: S105 - synthetic fixture token, not a secret
NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"  # noqa: S105 - synthetic fixture token, not a secret
OTHER_TOKEN = "3333333333333333333333333333333333333333333333333333333333333333"  # noqa: S105 - synthetic fixture token, not a secret


def _iso(seconds: int) -> str:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _base_rows() -> list[dict[str, Any]]:
    return [
        {
            "event_type": "book",
            "market": CONDITION_ID.encode(),
            "asset_id": YES_TOKEN,
            "timestamp_received": _iso(3),
            "timestamp": _iso(3),
            "bids": json.dumps([["0.40", "10"]]),
            "asks": json.dumps([["0.60", "9"]]),
            "side": None,
            "price": None,
            "size": None,
            "best_bid": "0.01",
            "best_ask": "0.99",
            "old_tick_size": None,
            "new_tick_size": None,
        },
        {
            "event_type": "price_change",
            "market": CONDITION_ID.encode(),
            "asset_id": YES_TOKEN,
            "timestamp_received": _iso(1),
            "timestamp": _iso(5),
            "bids": None,
            "asks": None,
            "side": "BUY",
            "price": "0.41",
            "size": "8",
            "best_bid": "0.02",
            "best_ask": "0.98",
            "old_tick_size": None,
            "new_tick_size": None,
        },
        {
            "event_type": "last_trade_price",
            "market": CONDITION_ID.encode(),
            "asset_id": YES_TOKEN,
            "timestamp_received": _iso(2),
            "timestamp": _iso(1),
            "bids": None,
            "asks": None,
            "side": "SELL",
            "price": "0.42",
            "size": "3",
            "best_bid": None,
            "best_ask": None,
            "old_tick_size": None,
            "new_tick_size": None,
        },
        {
            "event_type": "tick_size_change",
            "market": CONDITION_ID.encode(),
            "asset_id": YES_TOKEN,
            "timestamp_received": _iso(2),
            "timestamp": _iso(2),
            "bids": None,
            "asks": None,
            "side": None,
            "price": None,
            "size": None,
            "best_bid": None,
            "best_ask": None,
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
        },
        {
            "event_type": "book",
            "market": OTHER_CONDITION_ID.encode(),
            "asset_id": OTHER_TOKEN,
            "timestamp_received": _iso(0),
            "timestamp": _iso(0),
            "bids": json.dumps([["0.10", "1"]]),
            "asks": json.dumps([["0.90", "1"]]),
            "side": None,
            "price": None,
            "size": None,
            "best_bid": None,
            "best_ask": None,
            "old_tick_size": None,
            "new_tick_size": None,
        },
    ]


def _gamma_market(*, mismatch: bool = False) -> dict[str, Any]:
    outcomes = ["Yes", "No"]
    outcome_prices = ["1", "0", "0"] if mismatch else ["1", "0"]
    clob_token_ids = [YES_TOKEN, NO_TOKEN]
    return {
        "conditionId": CONDITION_ID,
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps(outcome_prices),
        "clobTokenIds": json.dumps(clob_token_ids),
        "feeSchedule": json.dumps({"rate": "0.02"}),
        "umaResolutionStatus": "resolved",
        "closedTime": "2026-01-02T03:04:05Z",
        "orderPriceMinTickSize": "0.001",
    }


def _write_event_dir(
    tmp_path: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    gamma_market: dict[str, Any] | None = None,
) -> Path:
    event_dir = tmp_path / "pmxt-event"
    event_dir.mkdir()
    pd.DataFrame(rows or _base_rows()).to_parquet(event_dir / "orderbook.parquet", index=False)
    market = gamma_market or _gamma_market()
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": [market]}), encoding="utf-8")
    (event_dir / "event_index.json").write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "conditionId": CONDITION_ID,
                        "yesToken": YES_TOKEN,
                        "noToken": NO_TOKEN,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-pmxt-test"}), encoding="utf-8")
    return event_dir


def _load(event_dir: Path, *, condition_id: str = CONDITION_ID, asset_id: str = YES_TOKEN):
    return PMXTEventV1Adapter(repo_root=Path.cwd()).load(
        {
            "input": {
                "event_dir": str(event_dir),
                "dataset_id": "synthetic-pmxt-event",
                "condition_id": condition_id,
                "asset_id": asset_id,
            },
        },
    )


def _updates(dataset) -> list[Any]:
    return [update for step in dataset.steps for update in step.updates]


def _metadata_text(dataset) -> str:
    return "\n".join((*dataset.metadata.assumptions, *dataset.metadata.warnings)).lower()


def _ordering_diagnostic(dataset) -> dict[str, str]:
    warning = next(
        item for item in dataset.metadata.warnings if item.startswith("PMXT ordering diagnostic: selected_rows=")
    )
    payload = warning.removeprefix("PMXT ordering diagnostic: ").removesuffix(".")
    return dict(item.split("=", maxsplit=1) for item in payload.split(", "))


def test_runner_adapter_registry_loads_pmxt_event_v1_from_yaml_adapter_config(tmp_path: Path) -> None:
    pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")
    from polymarket.backtest_v1 import ADAPTERS
    from polymarket.backtest_v1 import load_adapter

    event_dir = _write_event_dir(tmp_path)

    assert ADAPTERS["pmxt_event_v1"] is PMXTEventV1Adapter

    dataset = load_adapter(
        {
            "adapter": {
                "name": "pmxt_event_v1",
                "input": {
                    "event_dir": str(event_dir),
                    "dataset_id": "yaml-pmxt-event",
                    "condition_id": CONDITION_ID,
                    "asset_id": YES_TOKEN,
                },
            },
        },
    )

    assert dataset.metadata.adapter_name == "pmxt_event_v1"
    assert dataset.metadata.dataset_id == "yaml-pmxt-event"
    assert {update.asset_id for update in _updates(dataset)} == {YES_TOKEN}


def test_decodes_bytes_market_to_canonical_condition_id(tmp_path: Path) -> None:
    dataset = _load(_write_event_dir(tmp_path))

    assert {update.market for update in _updates(dataset)} == {CONDITION_ID}
    assert all(not update.market.startswith("b'") for update in _updates(dataset))


def test_converts_pmxt_event_types_to_canonical_update_types(tmp_path: Path) -> None:
    dataset = _load(_write_event_dir(tmp_path))

    assert [update.event_type for update in _updates(dataset)] == [
        "trade",
        "tick_size_change",
        "book",
        "price_change",
    ]


def test_sorts_replay_steps_by_source_time_receive_time_then_original_row_index_and_reassigns_sequence(
    tmp_path: Path,
) -> None:
    dataset = _load(_write_event_dir(tmp_path))

    assert [step.sequence for step in dataset.steps] == [1, 2, 3, 4]
    assert [step.timestamp for step in dataset.steps if step.timestamp is not None] == sorted(
        step.timestamp for step in dataset.steps if step.timestamp is not None
    )
    assert [step.updates[0].event_type for step in dataset.steps] == [
        "trade",
        "tick_size_change",
        "book",
        "price_change",
    ]
    text = _metadata_text(dataset)
    assert "stable" in text
    assert "timestamp," in text
    assert "timestamp_received" in text
    assert "original" in text
    assert "row" in text
    assert "pmxt" in text
    assert "source-time" in text
    assert "ordering" in text
    assert "diagnostic" in text
    assert "post-sort local replay sequence" in text
    assert "not vendor/raw sequence" in text
    assert dataset.metadata.source_quality["orderingStatus"] == "timestamp_ordered"
    assert dataset.metadata.source_quality["orderingAmbiguousGroups"] == 0
    diagnostic = _ordering_diagnostic(dataset)
    assert diagnostic == {
        "selected_rows": "4",
        "global_physical_receive_time_inversions": "not_computed_filtered_adapter",
        "selected_receive_time_inversions_before_sort": "1",
        "selected_receive_time_inversions_after_sort": "1",
        "selected_source_time_inversions_before_sort": "1",
        "selected_source_time_inversions_after_sort": "0",
        "missing_source_timestamp_count": "0",
        "duplicate_timestamp_received_group_count": "1",
        "max_rows_per_timestamp_received": "2",
        "tied_timestamp_group_count": "0",
        "tied_timestamp_row_count": "0",
        "ordering_ambiguous_groups": "0",
        "ordering_ambiguous_rows": "0",
        "stable_sort_key": "timestamp,timestamp_received,_original_row_index",
        "selected_receive_time_monotonic_after_sort": "false",
        "selected_source_time_monotonic_after_sort": "true",
    }


def test_missing_or_unparseable_timestamp_received_fails_before_sort(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    bad_root = tmp_path / "bad"
    missing_root.mkdir()
    bad_root.mkdir()
    rows = _base_rows()
    rows[1]["timestamp_received"] = None
    with pytest.raises(ValueError, match=r"timestamp_received.*missing|missing.*timestamp_received"):
        _load(_write_event_dir(missing_root, rows=rows))

    rows = _base_rows()
    rows[1]["timestamp_received"] = "not-a-timestamp"
    with pytest.raises(ValueError, match=r"timestamp_received.*unparseable|unparseable.*timestamp_received"):
        _load(_write_event_dir(bad_root, rows=rows))


def test_ordering_diagnostic_counts_source_ordering_and_duplicate_receive_times(tmp_path: Path) -> None:
    rows = _base_rows()
    rows[0]["timestamp_received"] = _iso(4)
    rows[1]["timestamp_received"] = _iso(1)
    rows[2]["timestamp_received"] = _iso(3)
    rows[3]["timestamp_received"] = _iso(1)
    rows[0]["timestamp"] = _iso(1)
    rows[1]["timestamp"] = None
    rows[2]["timestamp"] = _iso(5)
    rows[3]["timestamp"] = _iso(2)

    dataset = _load(_write_event_dir(tmp_path, rows=rows))

    assert [step.timestamp for step in dataset.steps if step.timestamp is not None] == sorted(
        step.timestamp for step in dataset.steps if step.timestamp is not None
    )
    diagnostic = _ordering_diagnostic(dataset)
    assert diagnostic["selected_rows"] == "4"
    assert diagnostic["selected_receive_time_inversions_before_sort"] == "2"
    assert diagnostic["selected_receive_time_inversions_after_sort"] == "1"
    assert diagnostic["selected_source_time_inversions_before_sort"] == "1"
    assert diagnostic["selected_source_time_inversions_after_sort"] == "0"
    assert diagnostic["duplicate_timestamp_received_group_count"] == "1"
    assert diagnostic["max_rows_per_timestamp_received"] == "2"
    assert diagnostic["stable_sort_key"] == "timestamp,timestamp_received,_original_row_index"
    assert diagnostic["selected_receive_time_monotonic_after_sort"] == "false"
    assert diagnostic["selected_source_time_monotonic_after_sort"] == "true"


def test_pmxt_source_time_ordering_can_make_receive_time_health_fail_without_blocking_metadata(
    tmp_path: Path,
) -> None:
    rows = _base_rows()
    rows[1]["event_type"] = "price_change"
    rows[1]["timestamp_received"] = _iso(1)
    rows[1]["timestamp"] = _iso(5)
    rows[2]["event_type"] = "price_change"
    rows[2]["timestamp_received"] = _iso(2)
    rows[2]["timestamp"] = _iso(1)
    dataset = _load(_write_event_dir(tmp_path, rows=rows))

    report = analyze_dataset_health(dataset)

    assert report.ok is False
    assert report.summary.receive_time_inversion_count >= 1
    assert any(issue.code == "receive_time_inversion" for issue in report.issues)
    assert dataset.metadata.source_quality["orderingStatus"] == "timestamp_ordered"


def test_flags_ambiguous_tied_timestamp_groups(tmp_path: Path) -> None:
    rows = _base_rows()
    rows[1]["timestamp_received"] = _iso(2)
    rows[1]["timestamp"] = _iso(2)
    rows[1]["price"] = "0.41"
    rows[3]["event_type"] = "price_change"
    rows[3]["side"] = "BUY"
    rows[3]["price"] = "0.42"
    rows[3]["size"] = "7"

    dataset = _load(_write_event_dir(tmp_path, rows=rows))

    diagnostic = _ordering_diagnostic(dataset)
    assert diagnostic["tied_timestamp_group_count"] == "1"
    assert diagnostic["tied_timestamp_row_count"] == "2"
    assert diagnostic["ordering_ambiguous_groups"] == "1"
    assert diagnostic["ordering_ambiguous_rows"] == "2"
    assert dataset.metadata.source_quality["orderingStatus"] == "ambiguous"
    assert dataset.metadata.source_quality["orderingAmbiguousGroups"] == 1
    assert dataset.metadata.source_quality["orderingAmbiguousRows"] == 2


def test_missing_source_timestamp_sorts_by_timestamp_received_fallback(tmp_path: Path) -> None:
    rows = _base_rows()
    rows[1]["timestamp"] = None
    rows[1]["timestamp_received"] = _iso(1)
    rows[2]["timestamp"] = _iso(2)
    rows[2]["timestamp_received"] = _iso(4)
    rows[3]["timestamp"] = _iso(3)
    rows[3]["timestamp_received"] = _iso(3)
    rows[0]["timestamp"] = _iso(4)
    rows[0]["timestamp_received"] = _iso(2)

    dataset = _load(_write_event_dir(tmp_path, rows=rows))

    assert dataset.steps[0].updates[0].event_type == "price_change"
    assert dataset.steps[0].timestamp is None
    assert dataset.steps[0].timestamp_received == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert dataset.metadata.source_quality["missingSourceTimestampRows"] == 1
    assert dataset.metadata.source_quality["orderingStatus"] == "ambiguous"


def test_decodes_gamma_json_string_metadata_into_fee_and_settlement_for_yes_no(tmp_path: Path) -> None:
    dataset = _load(_write_event_dir(tmp_path))

    by_token = {item.token_id: item for item in dataset.metadata.market_metadata}
    yes = by_token[YES_TOKEN]
    no = by_token[NO_TOKEN]

    assert yes.condition_id == CONDITION_ID
    assert yes.outcome == "Yes"
    assert yes.taker_fee == Decimal("0.02")
    assert yes.fee_source == "gamma_event.raw.market.feeSchedule.rate"
    assert yes.resolution_status == "resolved"
    assert yes.resolution_time == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert yes.token_payout == Decimal(1)
    assert yes.winner is True
    assert no.outcome == "No"
    assert no.token_payout == Decimal(0)
    assert no.winner is False


def test_gamma_metadata_length_mismatch_fails_clearly(tmp_path: Path) -> None:
    event_dir = _write_event_dir(tmp_path, gamma_market=_gamma_market(mismatch=True))

    with pytest.raises(ValueError, match=r"outcomes.*outcomePrices.*clobTokenIds|length"):
        _load(event_dir)


def test_preserves_stale_bbo_but_warns_bbo_is_not_trusted_for_filtering_or_execution(tmp_path: Path) -> None:
    dataset = _load(_write_event_dir(tmp_path))
    updates = _updates(dataset)

    assert any(update.best_bid == Decimal("0.01") and update.best_ask == Decimal("0.99") for update in updates)
    assert any(update.best_bid == Decimal("0.02") and update.best_ask == Decimal("0.98") for update in updates)
    text = _metadata_text(dataset)
    assert "best_bid" in text
    assert "best_ask" in text
    assert "proxy" in text or "audit" in text
    assert "not" in text
    assert "execution" in text or "filter" in text or "trusted" in text


def test_wrong_condition_id_or_asset_id_filter_fails_clearly(tmp_path: Path) -> None:
    event_dir = _write_event_dir(tmp_path)

    with pytest.raises(ValueError, match=r"condition_id|market|available"):
        _load(event_dir, condition_id="0xmissing", asset_id=YES_TOKEN)
    with pytest.raises(ValueError, match=r"asset_id|token|available"):
        _load(event_dir, condition_id=CONDITION_ID, asset_id="999999")


def test_requires_adapter_input_selection_and_emits_only_selected_rows(tmp_path: Path) -> None:
    event_dir = _write_event_dir(tmp_path)

    with pytest.raises(ValueError, match="condition_id"):
        PMXTEventV1Adapter(repo_root=Path.cwd()).load(
            {"input": {"event_dir": str(event_dir), "asset_id": YES_TOKEN}},
        )
    with pytest.raises(ValueError, match="asset_id"):
        PMXTEventV1Adapter(repo_root=Path.cwd()).load(
            {"input": {"event_dir": str(event_dir), "condition_id": CONDITION_ID}},
        )

    dataset = _load(event_dir, condition_id=CONDITION_ID, asset_id=YES_TOKEN)

    assert dataset.steps
    assert {update.market for update in _updates(dataset)} == {CONDITION_ID}
    assert {update.asset_id for update in _updates(dataset)} == {YES_TOKEN}


def test_reads_selected_parquet_rows_with_columns_and_filter_pushdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event_dir = _write_event_dir(tmp_path)
    calls: list[dict[str, Any]] = []
    original_read_parquet = pd.read_parquet

    def recording_read_parquet(*args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(dict(kwargs))
        return original_read_parquet(*args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", recording_read_parquet)

    dataset = _load(event_dir, condition_id=CONDITION_ID, asset_id=YES_TOKEN)

    assert dataset.steps
    first_call = calls[0]
    assert set(first_call["columns"]) == set(PMXTEventV1Adapter._ORDERBOOK_COLUMNS)
    assert first_call["filters"] == [
        ("market", "==", CONDITION_ID.encode()),
        ("asset_id", "==", YES_TOKEN),
    ]
    assert all(set(call["columns"]) <= set(PMXTEventV1Adapter._ORDERBOOK_COLUMNS) for call in calls)


def test_parquet_filter_rejection_falls_back_to_in_memory_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_dir = _write_event_dir(tmp_path)
    calls: list[dict[str, Any]] = []
    original_read_parquet = pd.read_parquet

    def rejecting_filtered_read_parquet(*args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(dict(kwargs))
        if "filters" in kwargs:
            raise TypeError("parquet engine does not support filters")
        return original_read_parquet(*args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", rejecting_filtered_read_parquet)

    dataset = _load(event_dir, condition_id=CONDITION_ID, asset_id=YES_TOKEN)

    assert dataset.steps
    assert {update.market for update in _updates(dataset)} == {CONDITION_ID}
    assert {update.asset_id for update in _updates(dataset)} == {YES_TOKEN}
    assert any("filters" in call for call in calls)
    assert any(
        "filters" not in call
        and set(call["columns"]) == set(PMXTEventV1Adapter._ORDERBOOK_COLUMNS)
        for call in calls
    )


def test_empty_filter_result_falls_back_when_canonical_diagnostics_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_dir = _write_event_dir(tmp_path)
    calls: list[dict[str, Any]] = []
    original_read_parquet = pd.read_parquet

    def empty_filtered_read_parquet(*args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(dict(kwargs))
        if "filters" in kwargs:
            return pd.DataFrame(columns=kwargs["columns"])
        return original_read_parquet(*args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", empty_filtered_read_parquet)

    dataset = _load(event_dir, condition_id=CONDITION_ID, asset_id=YES_TOKEN)

    assert dataset.steps
    assert {update.market for update in _updates(dataset)} == {CONDITION_ID}
    assert {update.asset_id for update in _updates(dataset)} == {YES_TOKEN}
    assert sum(1 for call in calls if "filters" in call) == 2
    assert any(call.get("columns") == list(PMXTEventV1Adapter._DIAGNOSTIC_COLUMNS) for call in calls)


def test_filter_pushdown_recovers_physical_ordinals_with_persisted_non_range_index(tmp_path: Path) -> None:
    rows = [
        {**_base_rows()[4], "timestamp": _iso(0), "timestamp_received": _iso(0)},
        {**_base_rows()[0], "timestamp": _iso(1), "timestamp_received": _iso(1)},
        {**_base_rows()[4], "timestamp": _iso(2), "timestamp_received": _iso(2)},
        {**_base_rows()[1], "timestamp": _iso(1), "timestamp_received": _iso(1)},
        {**_base_rows()[4], "timestamp": _iso(4), "timestamp_received": _iso(4)},
    ]
    event_dir = tmp_path / "pmxt-event"
    event_dir.mkdir()
    frame = pd.DataFrame(rows)
    frame.index = pd.Index([900, 100, 800, 50, 700], name="persisted_non_range_index")
    frame.to_parquet(event_dir / "orderbook.parquet")
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": [_gamma_market()]}), encoding="utf-8")
    (event_dir / "event_index.json").write_text(
        json.dumps({"markets": [{"conditionId": CONDITION_ID, "yesToken": YES_TOKEN, "noToken": NO_TOKEN}]}),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-pmxt-test"}), encoding="utf-8")

    dataset = _load(event_dir)

    assert [step.source_row_index for step in dataset.steps] == [1, 3]
    assert [step.updates[0].event_type for step in dataset.steps] == ["book", "price_change"]


def test_filter_pushdown_preserves_physical_original_row_indices_across_row_groups(tmp_path: Path) -> None:
    rows = [
        {**_base_rows()[4], "timestamp": _iso(0), "timestamp_received": _iso(0)},
        {**_base_rows()[0], "timestamp": _iso(1), "timestamp_received": _iso(1)},
        {**_base_rows()[4], "timestamp": _iso(2), "timestamp_received": _iso(2)},
        {**_base_rows()[1], "timestamp": _iso(3), "timestamp_received": _iso(3)},
        {**_base_rows()[4], "timestamp": _iso(4), "timestamp_received": _iso(4)},
        {**_base_rows()[2], "timestamp": _iso(5), "timestamp_received": _iso(5)},
    ]
    event_dir = tmp_path / "pmxt-event"
    event_dir.mkdir()
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False, row_group_size=2)
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": [_gamma_market()]}), encoding="utf-8")
    (event_dir / "event_index.json").write_text(
        json.dumps({"markets": [{"conditionId": CONDITION_ID, "yesToken": YES_TOKEN, "noToken": NO_TOKEN}]}),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-pmxt-test"}), encoding="utf-8")

    dataset = _load(event_dir)

    assert [step.source_row_index for step in dataset.steps] == [1, 3, 5]
    assert [step.updates[0].event_type for step in dataset.steps] == ["book", "price_change", "trade"]


def test_original_row_index_mapping_fails_closed_when_diagnostics_cannot_prove_physical_ordinals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {"market": CONDITION_ID.encode(), "asset_id": YES_TOKEN},
            {"market": CONDITION_ID.encode(), "asset_id": YES_TOKEN},
        ],
    )

    def empty_diagnostics(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame(columns=list(PMXTEventV1Adapter._DIAGNOSTIC_COLUMNS))

    monkeypatch.setattr(PMXTEventV1Adapter, "_read_parquet_columns", staticmethod(empty_diagnostics))
    with pytest.raises(ValueError, match=r"cannot prove physical original row ordinal.*filtered-local ordinal"):
        PMXTEventV1Adapter._with_original_row_indices(
            tmp_path / "orderbook.parquet",
            frame,
            condition_id=CONDITION_ID,
            asset_id=YES_TOKEN,
        )

    def mismatched_diagnostics(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame([{"market": CONDITION_ID.encode(), "asset_id": YES_TOKEN}])

    monkeypatch.setattr(PMXTEventV1Adapter, "_read_parquet_columns", staticmethod(mismatched_diagnostics))
    with pytest.raises(ValueError, match=r"diagnostics selected 1 row\(s\) but filtered frame has 2 row\(s\)"):
        PMXTEventV1Adapter._with_original_row_indices(
            tmp_path / "orderbook.parquet",
            frame,
            condition_id=CONDITION_ID,
            asset_id=YES_TOKEN,
        )
