from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "2026-07-10-pmxt-weather-maker-diagnostics"
    / "maker_diagnostics.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("pmxt_weather_maker_diagnostics", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_find_fill_respects_side_price_and_queue_threshold():
    module = load_module()
    base = pd.Timestamp("2026-06-01T00:00:00Z").value
    trade_times = np.array(
        [
            pd.Timestamp("2026-06-01T00:00:10Z").value,
            pd.Timestamp("2026-06-01T00:00:20Z").value,
            pd.Timestamp("2026-06-01T00:00:30Z").value,
        ],
        dtype=np.int64,
    )
    trade_prices = np.array([0.51, 0.50, 0.49], dtype=float)
    trade_sizes = np.array([1.0, 2.0, 3.0], dtype=float)
    trade_sides = np.array(["BUY", "SELL", "SELL"], dtype=str)

    no_fill = module.find_fill(
        trade_times,
        trade_prices,
        trade_sizes,
        trade_sides,
        start_ns=base,
        end_ns=base + 25_000_000_000,
        quote_side=module.BUY,
        quote_price=0.50,
        threshold=3.0,
    )
    assert not no_fill.filled
    assert no_fill.cumulative_eligible_volume == 2.0

    fill = module.find_fill(
        trade_times,
        trade_prices,
        trade_sizes,
        trade_sides,
        start_ns=base,
        end_ns=base + 40_000_000_000,
        quote_side=module.BUY,
        quote_price=0.50,
        threshold=3.0,
    )
    assert fill.filled
    assert fill.fill_timestamp == pd.Timestamp("2026-06-01T00:00:30Z")
    assert fill.fill_wait_seconds == 30.0
    assert fill.cumulative_eligible_volume == 5.0


def test_find_fill_supports_sell_ask_side():
    module = load_module()
    base = pd.Timestamp("2026-06-01T00:00:00Z").value
    trade_times = np.array(
        [
            pd.Timestamp("2026-06-01T00:00:10Z").value,
            pd.Timestamp("2026-06-01T00:00:20Z").value,
            pd.Timestamp("2026-06-01T00:00:30Z").value,
        ],
        dtype=np.int64,
    )
    trade_prices = np.array([0.51, 0.52, 0.49], dtype=float)
    trade_sizes = np.array([1.0, 2.0, 100.0], dtype=float)
    trade_sides = np.array(["BUY", "BUY", "SELL"], dtype=str)

    fill = module.find_fill(
        trade_times,
        trade_prices,
        trade_sizes,
        trade_sides,
        start_ns=base,
        end_ns=base + 40_000_000_000,
        quote_side=module.SELL,
        quote_price=0.52,
        threshold=2.0,
    )

    assert fill.filled
    assert fill.fill_timestamp == pd.Timestamp("2026-06-01T00:00:20Z")
    assert fill.cumulative_eligible_volume == 2.0


def test_timestamps_to_ns_normalizes_parquet_microsecond_timestamps():
    module = load_module()
    timestamp = pd.Timestamp("2026-06-07T04:56:43.591Z")
    series = pd.Series([timestamp]).astype("datetime64[us, UTC]")

    values = module.timestamps_to_ns(series)

    assert values[0] == timestamp.value


def test_simulate_fills_handles_microsecond_timestamp_arrays_end_to_end():
    module = load_module()
    panel = pd.DataFrame(
        {
            "event_type": ["trade"],
            "timestamp_received": [pd.Timestamp("2026-06-01T00:00:10.123456Z")],
            "sequence": [1],
            "update_price": [0.50],
            "update_size": [2.0],
            "update_side": ["SELL"],
        },
    )
    panel["timestamp_received"] = panel["timestamp_received"].astype("datetime64[us, UTC]")
    trades = module.build_trade_arrays(panel)
    probes = pd.DataFrame(
        {
            "timestamp_received": pd.Series([pd.Timestamp("2026-06-01T00:00:00Z")]).astype("datetime64[us, UTC]"),
            "quote_price": [0.50],
            "displayed_top_size": [0.0],
        },
    )

    fills = module.simulate_fills(
        probes,
        trades=trades,
        quote_side=module.BUY,
        queue_fraction=0.0,
        order_size=1.0,
        fill_window_seconds=60,
    )

    assert fills[0].filled
    assert fills[0].fill_timestamp == pd.Timestamp("2026-06-01T00:00:10.123456Z")


def test_maker_diagnostics_prefers_replay_timestamp_for_trade_and_probe_clock():
    module = load_module()
    panel = pd.DataFrame(
        {
            "event_type": ["trade"],
            "timestamp_received": [pd.Timestamp("2026-06-01T00:10:00Z")],
            "replay_timestamp": [pd.Timestamp("2026-06-01T00:00:10Z")],
            "sequence": [1],
            "update_price": [0.50],
            "update_size": [2.0],
            "update_side": ["SELL"],
        },
    )
    trades = module.build_trade_arrays(panel)
    probes = pd.DataFrame(
        {
            "timestamp_received": [pd.Timestamp("2026-06-01T00:09:00Z")],
            "replay_timestamp": [pd.Timestamp("2026-06-01T00:00:00Z")],
            "quote_price": [0.50],
            "displayed_top_size": [0.0],
        },
    )

    fills = module.simulate_fills(
        probes,
        trades=trades,
        quote_side=module.BUY,
        queue_fraction=0.0,
        order_size=1.0,
        fill_window_seconds=60,
    )

    assert fills[0].filled
    assert fills[0].fill_timestamp == pd.Timestamp("2026-06-01T00:00:10Z")


def test_pmxt_maker_health_gate_allows_receive_time_inversion_only():
    module = load_module()
    receive_inversion = SimpleNamespace(severity="error", code="receive_time_inversion")
    sequence_inversion = SimpleNamespace(severity="error", code="sequence_inversion")

    assert module.pmxt_maker_blocking_health_issues(SimpleNamespace(issues=[receive_inversion])) == []
    assert module.pmxt_maker_blocking_health_issues(
        SimpleNamespace(issues=[receive_inversion, sequence_inversion]),
    ) == [sequence_inversion]


def test_maker_inventory_and_token_summary_propagate_source_quality():
    module = load_module()
    event_index = {"eventSlug": "weather-demo", "title": "Weather demo", "startDate": None, "endDate": None}
    market = {"index": 1, "label": "30C", "conditionId": "0xcondition"}
    panel = pd.DataFrame(
        {
            "event_type": ["price_change", "trade"],
            "timestamp_received": [
                pd.Timestamp("2026-06-01T00:10:00Z"),
                pd.Timestamp("2026-06-01T00:01:00Z"),
            ],
            "replay_timestamp": [
                pd.Timestamp("2026-06-01T00:00:00Z"),
                pd.Timestamp("2026-06-01T00:00:01Z"),
            ],
        },
    )
    probes = pd.DataFrame({"spread": [0.02], "depth_imbalance_1": [0.5]})
    health = SimpleNamespace(
        summary=SimpleNamespace(
            source_time_inversion_count=3,
            source_delay_over_threshold_count=4,
        ),
    )
    source_quality = {
        "coverageStatus": "not_run",
        "orderingStatus": "ambiguous",
        "snapshotReplayStatus": "not_run",
        "metadataJoinStatus": "pass",
        "orderingAmbiguousGroups": 2,
        "orderingAmbiguousRows": 7,
        "missingSourceTimestampRows": 1,
        "stableSortKey": "timestamp,timestamp_received,_original_row_index",
    }

    inventory = module.build_inventory_row(event_index, market, "token-yes", panel, probes, health, source_quality)
    token_summary = module.build_token_summary(event_index, market, "token-yes", probes, panel, health, source_quality)

    for row in (inventory, token_summary):
        assert row["source_quality_ordering_status"] == "ambiguous"
        assert row["source_quality_ordering_ambiguous_groups"] == 2
        assert row["source_quality_ordering_ambiguous_rows"] == 7
        assert row["source_quality_metadata_join_status"] == "pass"
        assert row["source_quality_stable_sort_key"] == "timestamp,timestamp_received,_original_row_index"
        assert row["ordering_ambiguous"] is True


def test_compute_markout_uses_buy_and_sell_sign_conventions():
    module = load_module()
    probes = pd.DataFrame(
        {
            "timestamp_received": [pd.Timestamp("2026-06-01T00:00:00Z")],
            "time_to_close_bucket": ["last_6h"],
            "factor_quantile": [5],
            "depth_imbalance_1": [0.8],
            "quote_price": [0.50],
            "displayed_top_size": [10.0],
            "spread": [0.02],
            "mid": [0.51],
        },
    )
    fills = [
        module.FillResult(
            True,
            fill_timestamp_ns=pd.Timestamp("2026-06-01T00:00:10Z").value,
            fill_timestamp=pd.Timestamp("2026-06-01T00:00:10Z"),
            fill_wait_seconds=10.0,
            cumulative_eligible_volume=11.0,
        ),
    ]
    future = {
        "time_ns": np.array([pd.Timestamp("2026-06-01T00:01:10Z").value], dtype=np.int64),
        "mid": np.array([0.54], dtype=float),
    }

    buy = module.compute_markout_metrics(probes, fills, future=future, quote_side=module.BUY, markout_seconds=60)
    sell = module.compute_markout_metrics(probes, fills, future=future, quote_side=module.SELL, markout_seconds=60)

    assert math.isclose(float(buy.loc[0, "markout"]), 0.04)
    assert math.isclose(float(sell.loc[0, "markout"]), -0.04)
