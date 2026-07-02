"""Polymarket v1 Nautilus-native research backtest entry point.

This entry point deliberately uses NautilusTrader's native BacktestEngine. It
must not implement independent order matching, fill accounting, cash, position,
or PnL logic.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.trading.strategy import Strategy

from polymarket.adapters.live_event_bundle_v1 import LiveEventBundleV1Adapter
from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter
from polymarket.adapters.pmxt_parquet_v1 import PMXTParquetV1Adapter
from polymarket.adapters.utils import repo_relative_or_absolute
from polymarket.models import PolymarketL2DatasetV1
from polymarket.nautilus_native import convert_dataset_to_nautilus
from polymarket.nautilus_native import load_binary_option_from_config


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    PMXTParquetV1Adapter.adapter_name: PMXTParquetV1Adapter,
    PMXTEventV1Adapter.adapter_name: PMXTEventV1Adapter,
    LiveWsV1Adapter.adapter_name: LiveWsV1Adapter,
    LiveEventBundleV1Adapter.adapter_name: LiveEventBundleV1Adapter,
}


@dataclass(frozen=True, slots=True)
class NativeBacktestResultV1:
    run_dir: Path
    data_count: int
    order_book_deltas_count: int
    trade_ticks_count: int
    skipped_updates: tuple[str, ...]
    tick_size_changes: tuple[tuple[str, str], ...]


def now_run_id() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_child(parent: Path, child: Path) -> bool:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    return parent_resolved == child_resolved or parent_resolved in child_resolved.parents


def ensure_child(parent: Path, child: Path) -> Path:
    child_resolved = child.resolve()
    if not is_child(parent, child_resolved):
        raise ValueError(f"refusing to write outside {parent}: {child}")
    return child_resolved


def load_adapter(config: Mapping[str, Any]) -> PolymarketL2DatasetV1:
    adapter_config = config.get("adapter") or {}
    name = str(adapter_config.get("name"))
    if name not in ADAPTERS:
        raise ValueError(f"unknown adapter {name!r}; expected one of {sorted(ADAPTERS)}")
    return ADAPTERS[name](repo_root=REPO_ROOT).load(adapter_config)


def resolve_output_dir(config_path: Path, report_config: Mapping[str, Any]) -> Path:
    runs_root = (config_path.parent / "runs").resolve()
    configured = report_config.get("output_dir", "./runs")
    output_dir = Path(str(configured))
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    output_dir = output_dir.resolve()
    if not is_child(runs_root, output_dir):
        raise ValueError(
            "report.output_dir must stay inside the experiment-local runs directory "
            f"{runs_root}; got {output_dir}",
        )
    return output_dir


def load_native_strategy(config_path: Path, strategy_config: Mapping[str, Any]) -> tuple[Strategy | None, dict[str, Any]]:
    if not strategy_config or strategy_config.get("enabled") is False:
        return None, {"enabled": False, "loader_mode": "none"}

    source = strategy_config.get("path")
    class_name = strategy_config.get("class")
    if not source or not class_name:
        raise ValueError("strategy.path and strategy.class are required for a Nautilus-native strategy")
    source_path = Path(str(source))
    if not source_path.is_absolute():
        source_path = config_path.parent / source_path
    source_path = source_path.resolve()
    module_name = f"_polymarket_native_strategy_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy file: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    strategy_cls = getattr(module, str(class_name))
    params = dict(strategy_config.get("params") or {})
    strategy = strategy_cls(**params)
    if not isinstance(strategy, Strategy):
        raise TypeError(
            f"{class_name} must subclass nautilus_trader.trading.strategy.Strategy.",
        )
    return strategy, {
        "enabled": True,
        "loader_mode": "path",
        "source_path_resolved": repo_relative_or_absolute(source_path, repo_root=REPO_ROOT),
        "class": str(class_name),
        "params_resolved": params,
    }


def build_engine(config: Mapping[str, Any]) -> BacktestEngine:
    engine_config = BacktestEngineConfig(
        trader_id=TraderId(str((config.get("engine") or {}).get("trader_id", "POLY-BACKTEST-001"))),
        logging=LoggingConfig(bypass_logging=bool((config.get("engine") or {}).get("bypass_logging", True))),
        run_analysis=bool((config.get("engine") or {}).get("run_analysis", False)),
    )
    engine = BacktestEngine(config=engine_config)
    engine.add_venue(
        venue=POLYMARKET_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=pUSD,
        starting_balances=[Money.from_str(str((config.get("portfolio") or {}).get("starting_balance", "10000 pUSD")))],
        book_type=BookType.L2_MBP,
        trade_execution=bool((config.get("engine") or {}).get("trade_execution", True)),
        liquidity_consumption=bool((config.get("engine") or {}).get("liquidity_consumption", False)),
        queue_position=bool((config.get("engine") or {}).get("queue_position", False)),
    )
    return engine


def add_native_data(engine: BacktestEngine, data: tuple[Any, ...]) -> tuple[int, int]:
    order_book_deltas = [item for item in data if isinstance(item, OrderBookDeltas)]
    trade_ticks = [item for item in data if isinstance(item, TradeTick)]
    if order_book_deltas:
        engine.add_data(order_book_deltas, sort=False)
    if trade_ticks:
        engine.add_data(trade_ticks, sort=False)
    engine.sort_data()
    return len(order_book_deltas), len(trade_ticks)


def write_reports(run_dir: Path, engine: BacktestEngine, result: NativeBacktestResultV1) -> None:
    account = engine.trader.generate_account_report(POLYMARKET_VENUE)
    fills = engine.trader.generate_order_fills_report()
    positions = engine.trader.generate_positions_report()
    with pd.option_context("display.max_rows", 200, "display.max_columns", None, "display.width", 300):
        (run_dir / "account_report.txt").write_text(str(account), encoding="utf-8")
        (run_dir / "fills_report.txt").write_text(str(fills), encoding="utf-8")
        (run_dir / "positions_report.txt").write_text(str(positions), encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "engine": "nautilus_trader.backtest.engine.BacktestEngine",
                "data_count": result.data_count,
                "order_book_deltas_count": result.order_book_deltas_count,
                "trade_ticks_count": result.trade_ticks_count,
                "skipped_updates": list(result.skipped_updates),
                "tick_size_changes": list(result.tick_size_changes),
                "reports": {
                    "account": "account_report.txt",
                    "fills": "fills_report.txt",
                    "positions": "positions_report.txt",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def run_from_config(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = load_yaml(config_path)
    run_id = str((config.get("runtime") or {}).get("run_id") or now_run_id())
    output_dir = resolve_output_dir(config_path, config.get("report") or {})
    run_dir = ensure_child(output_dir, output_dir / run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    dataset = load_adapter(config)
    selected_asset_id = (config.get("selection") or {}).get("asset_id")
    instrument = load_binary_option_from_config(
        config.get("instrument") or {},
        dataset=dataset,
        selected_asset_id=selected_asset_id,
    )
    conversion = convert_dataset_to_nautilus(
        dataset,
        instrument=instrument,
        selected_asset_id=selected_asset_id,
        fail_on_tick_size_change=bool((config.get("replay") or {}).get("fail_on_tick_size_change", True)),
    )

    engine = build_engine(config)
    try:
        engine.add_instrument(instrument)
        order_book_count, trade_count = add_native_data(engine, conversion.data)
        strategy, strategy_provenance = load_native_strategy(config_path, config.get("strategy") or {})
        if strategy is not None:
            engine.add_strategy(strategy)
        engine.run()

        result = NativeBacktestResultV1(
            run_dir=run_dir,
            data_count=len(conversion.data),
            order_book_deltas_count=order_book_count,
            trade_ticks_count=trade_count,
            skipped_updates=conversion.skipped_updates,
            tick_size_changes=conversion.tick_size_changes,
        )
        shutil.copyfile(config_path, run_dir / "original_config.yml")
        resolved = {
            "engine": "nautilus_trader.backtest.engine.BacktestEngine",
            "adapter": {
                "name": dataset.metadata.adapter_name,
                "adapter_version": dataset.metadata.adapter_version,
                "source_type": dataset.metadata.source_type,
                "source_files_resolved": list(dataset.metadata.source_files),
                "assumptions": list(dataset.metadata.assumptions),
                "warnings": list(dataset.metadata.warnings),
            },
            "instrument_id": str(instrument.id),
            "strategy": strategy_provenance,
            "runtime": {
                "run_id": run_id,
                "created_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "config_path": repo_relative_or_absolute(config_path, repo_root=REPO_ROOT),
                "run_dir": repo_relative_or_absolute(run_dir, repo_root=REPO_ROOT),
            },
        }
        (run_dir / "resolved_config.json").write_text(json.dumps(resolved, indent=2), encoding="utf-8")
        write_reports(run_dir, engine, result)
        summary = {
            "run_dir": repo_relative_or_absolute(run_dir, repo_root=REPO_ROOT),
            "engine": "nautilus_trader.backtest.engine.BacktestEngine",
            "outputs": {
                "original_config": repo_relative_or_absolute(run_dir / "original_config.yml", repo_root=REPO_ROOT),
                "resolved_config": repo_relative_or_absolute(run_dir / "resolved_config.json", repo_root=REPO_ROOT),
                "summary": repo_relative_or_absolute(run_dir / "summary.json", repo_root=REPO_ROOT),
                "account_report": repo_relative_or_absolute(run_dir / "account_report.txt", repo_root=REPO_ROOT),
                "fills_report": repo_relative_or_absolute(run_dir / "fills_report.txt", repo_root=REPO_ROOT),
                "positions_report": repo_relative_or_absolute(run_dir / "positions_report.txt", repo_root=REPO_ROOT),
            },
            "data_count": result.data_count,
            "order_book_deltas_count": result.order_book_deltas_count,
            "trade_ticks_count": result.trade_ticks_count,
        }
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_from_config(args.config)


if __name__ == "__main__":
    main()
