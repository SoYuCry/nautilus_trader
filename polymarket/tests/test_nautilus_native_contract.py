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
