"""Non-negotiable contract: Polymarket runner stays Nautilus-native."""

from __future__ import annotations

import ast
from pathlib import Path


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


def test_runner_writes_data_health_before_nautilus_conversion() -> None:
    source = Path("polymarket/backtest_v1.py").read_text(encoding="utf-8")

    health_pos = source.index("data_health_report = analyze_dataset_health")
    health_file_pos = source.index('run_dir / "data_health.json"')
    conversion_pos = source.index("conversion = convert_dataset_to_nautilus")
    engine_pos = source.index("engine = build_engine(config)")

    assert health_pos < health_file_pos < conversion_pos < engine_pos
