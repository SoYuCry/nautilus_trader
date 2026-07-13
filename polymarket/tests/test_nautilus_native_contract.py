"""Non-negotiable contract: Polymarket runner stays Nautilus-native."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from polymarket.replay_contract import enforce_replay_mode_gate


def test_polymarket_runner_imports_nautilus_backtest_engine() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}

    assert "from nautilus_trader.backtest.engine import BacktestEngine" in source
    assert class_names <= {"NativeBacktestResultV1"}


def test_polymarket_strategy_namespace_is_not_defined() -> None:
    assert not Path("polymarket/strategies").exists()


def test_nautilus_bridge_defaults_to_received_time_replay_clock() -> None:
    source = Path("polymarket/_core/nautilus_native.py").read_text(encoding="utf-8")

    # Default replay clock stays receive time; the PMXT contract clock is an
    # explicit opt-in and its fallback expression is defined once in
    # polymarket/replay_contract.py, not re-derived in the bridge.
    assert "return datetime_to_nanos(step.timestamp_received)" in source
    assert "replay_clock: ReplayClockV1 = RECEIVE_TIME_REPLAY_CLOCK" in source
    assert "step.timestamp or step.timestamp_received" not in source

    contract_source = Path("polymarket/replay_contract.py").read_text(encoding="utf-8")
    assert contract_source.count("step.timestamp or step.timestamp_received") == 1


def test_runner_rejects_pmxt_before_adapter_load_and_nautilus_conversion() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")

    guard_pos = source.index("reject_pmxt_adapter_for_nautilus_backtest(config)")
    load_pos = source.index("dataset = load_adapter(config)")
    health_pos = source.index("data_health_report = analyze_dataset_health")
    conversion_pos = source.index("conversion = convert_dataset_to_nautilus")
    engine_pos = source.index("engine = build_engine(config")

    assert guard_pos < load_pos < health_pos < conversion_pos < engine_pos
    assert "reject_non_causal_source_time_config" not in source
    assert "reject_non_causal_dataset_metadata" not in source
    assert "PNL_ALLOWED_DATA_TIERS" not in source
    assert "PNL_ALLOWED_RUN_GRADES" not in source


def test_runner_guard_delegates_to_shared_replay_mode_gate() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reject_pmxt_adapter_for_nautilus_backtest"
    )
    guard_source = ast.get_source_segment(source, guard)

    assert guard_source is not None
    assert "enforce_replay_mode_gate(config)" in guard_source


def test_pmxt_adapter_guard_rejects_pmxt_event_v1_by_default() -> None:
    with pytest.raises(ValueError, match="PMXT event data is exploratory") as exc_info:
        enforce_replay_mode_gate({"adapter": {"name": "pmxt_event_v1"}})

    message = str(exc_info.value)
    assert "strict_capture" in message
    assert "PMXT factor research path" in message
    assert "replay.mode: pmxt_research" in message


def test_pmxt_adapter_guard_rejects_pmxt_event_v1_in_explicit_strict_mode() -> None:
    with pytest.raises(ValueError, match="PMXT event data is exploratory"):
        enforce_replay_mode_gate(
            {
                "adapter": {"name": "pmxt_event_v1"},
                "replay": {"mode": "strict_capture"},
            },
        )


def test_pmxt_adapter_guard_allows_pmxt_event_v1_in_explicit_research_mode() -> None:
    mode = enforce_replay_mode_gate(
        {
            "adapter": {"name": "pmxt_event_v1"},
            "replay": {"mode": "pmxt_research"},
        },
    )

    assert mode == "pmxt_research"


def test_pmxt_adapter_guard_allows_non_pmxt_adapter() -> None:
    assert enforce_replay_mode_gate({"adapter": {"name": "live_ws_v1"}}) == "strict_capture"


def test_pmxt_research_mode_refuses_non_pmxt_adapter() -> None:
    with pytest.raises(ValueError, match="only applies to the pmxt_event_v1 adapter"):
        enforce_replay_mode_gate(
            {
                "adapter": {"name": "live_ws_v1"},
                "replay": {"mode": "pmxt_research"},
            },
        )


def test_resolved_config_does_not_emit_trust_metadata() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")

    assert '"trust_metadata"' not in source
