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
from nautilus_trader.adapters.polymarket.fee_model import PolymarketFeeModel
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.trading.strategy import Strategy

from polymarket.adapters.live_event_bundle_v1 import LiveEventBundleV1Adapter
from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket.adapters.utils import repo_relative_or_absolute
from polymarket.data_health import DataHealthError
from polymarket.data_health import analyze_dataset_health
from polymarket._core.fees import build_fee_report
from polymarket._core.fees import enforce_fee_report
from polymarket._core.fees import summarize_fill_fee_totals
from polymarket._core.models import PolymarketL2DatasetV1
from polymarket._core.nautilus_native import convert_dataset_to_nautilus
from polymarket._core.nautilus_native import install_effective_tick_size_order_guard
from polymarket._core.nautilus_native import load_binary_option_from_config
from polymarket.strategy import PolymarketStrategyBase


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    LiveWsV1Adapter.adapter_name: LiveWsV1Adapter,
    LiveEventBundleV1Adapter.adapter_name: LiveEventBundleV1Adapter,
}


@dataclass(frozen=True, slots=True)
class NativeBacktestResultV1:
    run_dir: Path
    data_count: int
    order_book_deltas_count: int
    trade_ticks_count: int
    instrument_close_count: int
    skipped_updates: tuple[str, ...]
    tick_size_changes: tuple[tuple[str, str], ...]
    settlement: dict[str, Any]
    data_health: dict[str, Any]
    fees: dict[str, Any]


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


def configure_polymarket_strategy_timeline(strategy: Strategy, conversion: Any) -> dict[str, Any]:
    """Inject Polymarket effective tick timeline into compatible strategies."""

    if not isinstance(strategy, PolymarketStrategyBase):
        return {
            "enabled": False,
            "reason": "strategy does not subclass PolymarketStrategyBase",
        }
    strategy.set_polymarket_tick_timeline(
        initial_tick_size=conversion.initial_tick_size,
        changes=conversion.effective_tick_size_changes,
    )
    return {
        "enabled": True,
        "initial_tick_size": str(conversion.initial_tick_size),
        "changes": [
            {
                "sequence": change.sequence,
                "effective_from_ts_init": change.effective_from_ts_init,
                "old_tick_size": str(change.old_tick_size),
                "new_tick_size": str(change.new_tick_size),
            }
            for change in conversion.effective_tick_size_changes
        ],
    }


def collect_polymarket_strategy_rounding(strategy: Strategy | None) -> dict[str, Any]:
    """Return strategy-level Polymarket price rounding audit data, if present."""

    if strategy is None or not isinstance(strategy, PolymarketStrategyBase):
        return {"enabled": False, "count": 0, "events": []}
    events = strategy.polymarket_price_rounding_events
    event_rows = [
        {
            "ts_ns": event.ts_ns,
            "original_price": str(event.original_price),
            "rounded_price": str(event.rounded_price),
            "tick_size": str(event.tick_size),
            "side": event.side,
            "intent": event.intent,
            "direction": event.direction,
        }
        for event in events
    ]
    return {"enabled": True, "count": len(event_rows), "events": event_rows}


def collect_fee_report(config: Mapping[str, Any], instrument: Any) -> dict[str, Any]:
    """Return the run's fee model/source contract in a serializable form."""
    return build_fee_report(
        config.get("fees") or {},
        maker_fee=instrument.maker_fee,
        taker_fee=instrument.taker_fee,
        fee_source=str((getattr(instrument, "info", None) or {}).get("fee_source", "unknown")),
    )


def build_engine(
    config: Mapping[str, Any],
    *,
    settlement_prices: Mapping[Any, float] | None = None,
) -> BacktestEngine:
    fees_config = config.get("fees") or {}
    fee_model = None
    if fees_config.get("enabled", True):
        fee_model = PolymarketFeeModel(
            maker_rebates_enabled=bool(fees_config.get("maker_rebates_enabled", False)),
        )
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
        fee_model=fee_model,
        book_type=BookType.L2_MBP,
        trade_execution=bool((config.get("engine") or {}).get("trade_execution", True)),
        liquidity_consumption=bool((config.get("engine") or {}).get("liquidity_consumption", False)),
        queue_position=bool((config.get("engine") or {}).get("queue_position", False)),
        settlement_prices=dict(settlement_prices or {}),
    )
    return engine


def add_native_data(engine: BacktestEngine, data: tuple[Any, ...]) -> tuple[int, int, int]:
    order_book_deltas = [item for item in data if isinstance(item, OrderBookDeltas)]
    trade_ticks = [item for item in data if isinstance(item, TradeTick)]
    instrument_closes = [item for item in data if isinstance(item, InstrumentClose)]
    if order_book_deltas:
        engine.add_data(order_book_deltas, sort=False)
    if trade_ticks:
        engine.add_data(trade_ticks, sort=False)
    if instrument_closes:
        engine.add_data(instrument_closes, sort=False)
    # The source dataset is not repaired or re-sorted to hide problems.  The
    # mandatory pre-run data-health gate proves ts_init is receive-time
    # monotonic; this Nautilus sort only syncs separately added native data
    # types into a single ts_init-ordered stream required by BacktestEngine.
    engine.sort_data()
    return len(order_book_deltas), len(trade_ticks), len(instrument_closes)


def write_reports(run_dir: Path, engine: BacktestEngine, result: NativeBacktestResultV1) -> dict[str, Any]:
    account = engine.trader.generate_account_report(POLYMARKET_VENUE)
    fills = engine.trader.generate_order_fills_report()
    positions = engine.trader.generate_positions_report()
    fee_totals = summarize_fill_fee_totals(fills)
    fee_report = {**result.fees, "totals_from_fills_report": fee_totals}
    with pd.option_context("display.max_rows", 200, "display.max_columns", None, "display.width", 300):
        (run_dir / "account_report.txt").write_text(str(account), encoding="utf-8")
        (run_dir / "fills_report.txt").write_text(str(fills), encoding="utf-8")
        (run_dir / "positions_report.txt").write_text(str(positions), encoding="utf-8")
    account.to_csv(run_dir / "account_report.csv")
    fills.to_csv(run_dir / "fills_report.csv")
    positions.to_csv(run_dir / "positions_report.csv")
    _write_run_report_markdown(
        run_dir=run_dir,
        result=result,
        account=account,
        fills=fills,
        positions=positions,
        fee_totals=fee_totals,
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "engine": "nautilus_trader.backtest.engine.BacktestEngine",
                "data_count": result.data_count,
                "order_book_deltas_count": result.order_book_deltas_count,
                "trade_ticks_count": result.trade_ticks_count,
                "instrument_close_count": result.instrument_close_count,
                "skipped_updates": list(result.skipped_updates),
                "tick_size_changes": list(result.tick_size_changes),
                "settlement": result.settlement,
                "data_health": result.data_health,
                "fees": fee_report,
                "reports": {
                    "account_txt": "account_report.txt",
                    "account_csv": "account_report.csv",
                    "fills_txt": "fills_report.txt",
                    "fills_csv": "fills_report.csv",
                    "positions_txt": "positions_report.txt",
                    "positions_csv": "positions_report.csv",
                    "markdown": "run_report.md",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"fees": fee_report}


def _write_run_report_markdown(
    *,
    run_dir: Path,
    result: NativeBacktestResultV1,
    account: pd.DataFrame,
    fills: pd.DataFrame,
    positions: pd.DataFrame,
    fee_totals: dict[str, Any],
) -> None:
    health_summary = result.data_health.get("summary", {})
    lines = [
        "# Polymarket backtest run report",
        "",
        "## Summary",
        "",
        "- Engine: `nautilus_trader.backtest.engine.BacktestEngine`",
        f"- Nautilus data count: `{result.data_count}`",
        f"- OrderBookDeltas count: `{result.order_book_deltas_count}`",
        f"- TradeTick count: `{result.trade_ticks_count}`",
        f"- InstrumentClose count: `{result.instrument_close_count}`",
        f"- Settlement mode: `{result.settlement.get('mode', 'open')}`",
        f"- Settlement enabled: `{str(result.settlement.get('enabled', False)).lower()}`",
        f"- Settlement reason: {result.settlement.get('reason', 'unknown')}",
        "- Settlement modes: `official` uses resolution metadata; `inferred` is a clearly marked terminal-price guess; `open` leaves final positions unclosed.",
        f"- Data health ok: `{str(result.data_health.get('ok')).lower()}`",
        f"- Receive-time inversions: `{health_summary.get('receive_time_inversion_count')}`",
        f"- Sequence inversions: `{health_summary.get('sequence_inversion_count')}`",
        f"- Source-time inversions: `{health_summary.get('source_time_inversion_count')}`",
        f"- Future source-time count: `{health_summary.get('future_source_time_count')}`",
        "",
        "## Fees",
        "",
        f"- Fee model enabled: `{str(result.fees.get('enabled', False)).lower()}`",
        f"- Fee model: `{result.fees.get('model', 'unknown')}`",
        f"- Maker rebates enabled: `{str(result.fees.get('maker_rebates_enabled', False)).lower()}`",
        f"- Require explicit fee metadata: `{str(result.fees.get('require_explicit', False)).lower()}`",
        f"- Instrument maker fee: `{result.fees.get('instrument_maker_fee', 'unknown')}`",
        f"- Instrument taker fee: `{result.fees.get('instrument_taker_fee', 'unknown')}`",
        f"- Fee source: `{result.fees.get('instrument_fee_source', 'unknown')}`",
        f"- Total fees from `fills_report`: `{fee_totals.get('total_display', 'unavailable')}`",
        f"- Fee total source column: `{fee_totals.get('source_column')}`",
        f"- Fee warning: {result.fees.get('warning') or 'none'}",
        f"- Fee total warning: {fee_totals.get('warning') or 'none'}",
        "",
        "## Report files",
        "",
        "- `summary.json`",
        "- `data_health.json`",
        "- `fills_report.csv` / `fills_report.txt`",
        "- `positions_report.csv` / `positions_report.txt`",
        "- `account_report.csv` / `account_report.txt`",
        "",
        "## Fills preview",
        "",
        _frame_preview(fills),
        "",
        "## Positions preview",
        "",
        _frame_preview(positions),
        "",
        "## Account preview",
        "",
        _frame_preview(account),
        "",
        "## Notes",
        "",
        "- `TradeTick count` is selected-token `last_trade_price` converted into Nautilus `TradeTick`; it is not strategy fill count.",
        "- Settlement uses Nautilus `InstrumentClose` + venue `settlement_prices`; it is not converted into a market `TradeTick`.",
        "- Strategy fills are recorded in `fills_report.csv`.",
        "- Fees use Nautilus' Polymarket fee model when enabled and read the instrument `maker_fee` / `taker_fee` fields.",
    ]
    if result.settlement.get("mode") == "open":
        lines.extend(
            [
                "- Open settlement mode means no `InstrumentClose` was generated; inspect the final positions below.",
                "",
                "## Final open positions",
                "",
                _frame_preview(positions),
            ],
        )
    if result.settlement.get("mode") == "inferred":
        lines.append("- Inferred settlement is a research convenience, not official Polymarket resolution evidence.")
    (run_dir / "run_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _frame_preview(frame: pd.DataFrame, *, max_rows: int = 10) -> str:
    if frame.empty:
        return "_empty_"
    with pd.option_context("display.max_rows", max_rows, "display.max_columns", None, "display.width", 300):
        return "```text\n" + str(frame.head(max_rows)) + "\n```"


def _settlement_to_dict(conversion: Any) -> dict[str, Any]:
    settlement = conversion.settlement
    if settlement is None:
        return {
            "mode": "open",
            "enabled": False,
            "reason": conversion.settlement_reason,
            "evidence": dict(conversion.settlement_evidence),
            "mechanism": "no InstrumentClose generated; final positions remain open",
        }
    return {
        "mode": settlement.mode,
        "enabled": True,
        "condition_id": settlement.condition_id,
        "token_id": settlement.token_id,
        "resolution_time_ns": settlement.resolution_time_ns,
        "payout": str(settlement.payout),
        "source": settlement.source,
        "status": settlement.status,
        "reason": settlement.reason,
        "evidence": dict(settlement.evidence),
        "mechanism": "Nautilus InstrumentClose + venue settlement_prices",
    }


def run_from_config(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = load_yaml(config_path)
    run_id = str((config.get("runtime") or {}).get("run_id") or now_run_id())
    output_dir = resolve_output_dir(config_path, config.get("report") or {})
    run_dir = ensure_child(output_dir, output_dir / run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, run_dir / "original_config.yml")

    dataset = load_adapter(config)
    health_config = config.get("data_health") or {}
    data_health_report = analyze_dataset_health(
        dataset,
        future_tolerance_ms=float(health_config.get("future_tolerance_ms", 50.0)),
        delay_warning_ms=float(health_config.get("delay_warning_ms", 1_000.0)),
    )
    (run_dir / "data_health.json").write_text(data_health_report.to_json() + "\n", encoding="utf-8")
    if not data_health_report.ok:
        raise DataHealthError(
            "Polymarket data-health check failed before Nautilus backtest. "
            f"Inspect {repo_relative_or_absolute(run_dir / 'data_health.json', repo_root=REPO_ROOT)}; "
            "do not sort the source data to make this pass.",
        )

    selected_asset_id = (config.get("selection") or {}).get("asset_id")
    instrument = load_binary_option_from_config(
        config.get("instrument") or {},
        dataset=dataset,
        selected_asset_id=selected_asset_id,
        config_base_dir=config_path.parent,
    )
    fee_report = collect_fee_report(config, instrument)
    enforce_fee_report(fee_report)
    conversion = convert_dataset_to_nautilus(
        dataset,
        instrument=instrument,
        selected_asset_id=selected_asset_id,
        fail_on_tick_size_change=bool((config.get("replay") or {}).get("fail_on_tick_size_change", False)),
    )

    settlement_prices = (
        {instrument.id: float(conversion.settlement.payout)}
        if conversion.settlement is not None
        else None
    )
    engine = build_engine(config, settlement_prices=settlement_prices)
    try:
        engine.add_instrument(instrument)
        order_book_count, trade_count, instrument_close_count = add_native_data(engine, conversion.data)
        strategy, strategy_provenance = load_native_strategy(config_path, config.get("strategy") or {})
        if strategy is not None:
            strategy_provenance["polymarket_strategy_base_tick_timeline"] = configure_polymarket_strategy_timeline(
                strategy,
                conversion,
            )
            strategy_provenance["effective_tick_size_order_guard"] = install_effective_tick_size_order_guard(
                strategy,
                initial_tick_size=conversion.initial_tick_size,
                changes=conversion.effective_tick_size_changes,
            )
            engine.add_strategy(strategy)
        engine.run()
        strategy_provenance["polymarket_price_rounding"] = collect_polymarket_strategy_rounding(strategy)

        settlement_dict = _settlement_to_dict(conversion)
        result = NativeBacktestResultV1(
            run_dir=run_dir,
            data_count=len(conversion.data),
            order_book_deltas_count=order_book_count,
            trade_ticks_count=trade_count,
            instrument_close_count=instrument_close_count,
            skipped_updates=conversion.skipped_updates,
            tick_size_changes=conversion.tick_size_changes,
            settlement=settlement_dict,
            data_health=data_health_report.to_dict(),
            fees=fee_report,
        )
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
            "tick_size": {
                "instrument_price_increment": str(instrument.price_increment),
                "initial_effective_tick_size": str(conversion.initial_tick_size),
                "effective_tick_size_changes": [
                    {
                        "sequence": change.sequence,
                        "effective_from_ts_init": change.effective_from_ts_init,
                        "old_tick_size": str(change.old_tick_size),
                        "new_tick_size": str(change.new_tick_size),
                    }
                    for change in conversion.effective_tick_size_changes
                ],
            },
            "settlement": settlement_dict,
            "fees": fee_report,
            "data_health": data_health_report.to_dict(),
            "runtime": {
                "run_id": run_id,
                "created_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "config_path": repo_relative_or_absolute(config_path, repo_root=REPO_ROOT),
                "run_dir": repo_relative_or_absolute(run_dir, repo_root=REPO_ROOT),
            },
        }
        (run_dir / "resolved_config.json").write_text(json.dumps(resolved, indent=2), encoding="utf-8")
        report_metadata = write_reports(run_dir, engine, result)
        summary = {
            "run_dir": repo_relative_or_absolute(run_dir, repo_root=REPO_ROOT),
            "engine": "nautilus_trader.backtest.engine.BacktestEngine",
            "outputs": {
                "original_config": repo_relative_or_absolute(run_dir / "original_config.yml", repo_root=REPO_ROOT),
                "resolved_config": repo_relative_or_absolute(run_dir / "resolved_config.json", repo_root=REPO_ROOT),
                "data_health": repo_relative_or_absolute(run_dir / "data_health.json", repo_root=REPO_ROOT),
                "summary": repo_relative_or_absolute(run_dir / "summary.json", repo_root=REPO_ROOT),
                "account_report": repo_relative_or_absolute(run_dir / "account_report.txt", repo_root=REPO_ROOT),
                "account_report_csv": repo_relative_or_absolute(run_dir / "account_report.csv", repo_root=REPO_ROOT),
                "fills_report": repo_relative_or_absolute(run_dir / "fills_report.txt", repo_root=REPO_ROOT),
                "fills_report_csv": repo_relative_or_absolute(run_dir / "fills_report.csv", repo_root=REPO_ROOT),
                "positions_report": repo_relative_or_absolute(run_dir / "positions_report.txt", repo_root=REPO_ROOT),
                "positions_report_csv": repo_relative_or_absolute(run_dir / "positions_report.csv", repo_root=REPO_ROOT),
                "run_report": repo_relative_or_absolute(run_dir / "run_report.md", repo_root=REPO_ROOT),
            },
            "data_count": result.data_count,
            "order_book_deltas_count": result.order_book_deltas_count,
            "trade_ticks_count": result.trade_ticks_count,
            "instrument_close_count": result.instrument_close_count,
            "settlement_mode": result.settlement.get("mode", "open"),
            "settlement_enabled": bool(result.settlement.get("enabled", False)),
            "data_health_ok": data_health_report.ok,
            "fees": report_metadata["fees"],
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

