"""Non-negotiable contract: Polymarket runner stays Nautilus-native."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest


def test_polymarket_runner_imports_nautilus_backtest_engine() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}

    assert "from nautilus_trader.backtest.engine import BacktestEngine" in source
    assert class_names <= {"NativeBacktestResultV1"}


def test_polymarket_strategy_namespace_is_not_defined() -> None:
    assert not Path("polymarket/strategies").exists()


def test_nautilus_bridge_uses_received_time_for_replay_clock() -> None:
    source = Path("polymarket/_core/nautilus_native.py").read_text(encoding="utf-8")

    assert "return datetime_to_nanos(step.timestamp_received)" in source
    assert "step.timestamp or step.timestamp_received" not in source


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


def _load_pmxt_guard() -> Any:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module = ast.Module(
        body=[
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "reject_pmxt_adapter_for_nautilus_backtest"
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)

    class _PMXTEventV1Adapter:
        adapter_name = "pmxt_event_v1"

    namespace: dict[str, Any] = {"Any": Any, "PMXTEventV1Adapter": _PMXTEventV1Adapter}
    exec(compile(module, "polymarket/backtest_v1.py", "exec"), namespace)  # noqa: S102 - test executes local source
    return namespace["reject_pmxt_adapter_for_nautilus_backtest"]


def test_pmxt_adapter_guard_rejects_pmxt_event_v1() -> None:
    guard = _load_pmxt_guard()

    with pytest.raises(ValueError, match="PMXT event data is exploratory") as exc_info:
        guard({"adapter": {"name": "pmxt_event_v1"}})

    message = str(exc_info.value)
    assert "PMXT factor research path" in message
    assert "not the Nautilus PnL/fill/fee/cash/position runner" in message


def test_pmxt_adapter_guard_allows_non_pmxt_adapter() -> None:
    guard = _load_pmxt_guard()

    guard({"adapter": {"name": "live_ws_v1"}})


def test_resolved_config_does_not_emit_trust_metadata() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")

    assert '"trust_metadata"' not in source
