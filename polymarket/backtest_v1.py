"""
Polymarket v1 Nautilus-native research backtest entry point.

This entry point deliberately uses NautilusTrader's native BacktestEngine. It
must not implement independent order matching, fill accounting, cash, position,
or PnL logic.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket.fee_model import PolymarketFeeModel
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.trading.strategy import Strategy
from polymarket._core.fees import build_fee_report
from polymarket._core.fees import enforce_fee_report
from polymarket._core.models import PolymarketL2DatasetV1
from polymarket._core.nautilus_native import convert_dataset_to_nautilus
from polymarket._core.nautilus_native import install_effective_tick_size_order_guard
from polymarket._core.nautilus_native import load_binary_option_from_config
from polymarket._core.reports import write_backtest_reports
from polymarket.adapters.live_event_bundle_v1 import LiveEventBundleV1Adapter
from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter
from polymarket.adapters.utils import repo_relative_or_absolute
from polymarket.data_health import DataHealthError
from polymarket.data_health import analyze_dataset_health
from polymarket.replay_contract import PMXT_REPLAY_CLOCK
from polymarket.replay_contract import PMXT_RESEARCH_MODE
from polymarket.replay_contract import RECEIVE_TIME_REPLAY_CLOCK
from polymarket.replay_contract import blocking_health_issues
from polymarket.replay_contract import build_replay_provenance
from polymarket.replay_contract import enforce_ambiguous_ties_gate
from polymarket.replay_contract import enforce_replay_mode_gate
from polymarket.replay_contract import verify_pmxt_replay_clock_order
from polymarket.strategy import PolymarketStrategyBase


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    LiveWsV1Adapter.adapter_name: LiveWsV1Adapter,
    LiveEventBundleV1Adapter.adapter_name: LiveEventBundleV1Adapter,
    PMXTEventV1Adapter.adapter_name: PMXTEventV1Adapter,
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
    replay: dict[str, Any]
    health_gate: dict[str, Any]
    engine_config: dict[str, Any]
    input_hashes: list[dict[str, Any]]
    data_health_artifact: dict[str, Any]


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


def expand_config_paths(config: dict[str, Any]) -> dict[str, Any]:
    """Expand environment variables in adapter input paths (e.g. ${POLYREAPER_EVENT_DIR})."""
    input_config = (config.get("adapter") or {}).get("input")
    if isinstance(input_config, dict):
        for key in ("event_dir", "ndjson_path", "bundle_dir"):
            if isinstance(input_config.get(key), str):
                input_config[key] = os.path.expandvars(input_config[key])
    return config


def load_adapter(config: Mapping[str, Any]) -> PolymarketL2DatasetV1:
    adapter_config = config.get("adapter") or {}
    name = str(adapter_config.get("name"))
    if name not in ADAPTERS:
        raise ValueError(f"unknown adapter {name!r}; expected one of {sorted(ADAPTERS)}")
    return ADAPTERS[name](repo_root=REPO_ROOT).load(adapter_config)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_input_hashes(dataset: PolymarketL2DatasetV1) -> list[dict[str, Any]]:
    """Return size/sha256 for each adapter source file so runs pin their inputs."""
    rows: list[dict[str, Any]] = []
    for source_file in dataset.metadata.source_files:
        path = Path(str(source_file))
        resolved = path if path.is_absolute() else (REPO_ROOT / path).resolve()
        row: dict[str, Any] = {"source_file": str(source_file), "exists": resolved.is_file()}
        if resolved.is_file():
            row["size_bytes"] = resolved.stat().st_size
            row["sha256"] = sha256_path(resolved)
        rows.append(row)
    return rows


# Artifacts above this size are declared omitted-from-git in the canonical
# report index; OMITTED_ARTIFACTS.json pins their integrity metadata instead.
GIT_OMIT_SIZE_BYTES = 5_000_000


def describe_data_health_artifact(run_dir: Path) -> dict[str, Any]:
    """Describe data_health.json for the canonical report index.

    Large runs produce per-issue diagnostics too big to commit; the runner
    writes OMITTED_ARTIFACTS.json alongside so a reader of summary.json can
    tell deliberate omission apart from a corrupt or forgotten file.
    """
    path = run_dir / "data_health.json"
    size_bytes = path.stat().st_size
    sha256 = sha256_path(path)
    artifact: dict[str, Any] = {
        "path": "data_health.json",
        "size_bytes": size_bytes,
        "sha256": sha256,
    }
    if size_bytes <= GIT_OMIT_SIZE_BYTES:
        artifact["status"] = "present"
        return artifact
    artifact["status"] = "omitted_from_git"
    artifact["manifest"] = "OMITTED_ARTIFACTS.json"
    (run_dir / "OMITTED_ARTIFACTS.json").write_text(
        json.dumps(
            {
                "omitted": [
                    {
                        "path": "data_health.json",
                        "committed": False,
                        "reason": (
                            "full per-issue diagnostics exceed the git-commit size "
                            f"threshold ({GIT_OMIT_SIZE_BYTES} bytes); regenerate by "
                            "re-running the experiment config"
                        ),
                        "size_bytes": size_bytes,
                        "sha256": sha256,
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact


def reject_pmxt_adapter_for_nautilus_backtest(config: Mapping[str, Any]) -> str:
    """
    Replay-mode gate: PMXT data only enters the runner in explicit pmxt_research mode.

    The default strict_capture mode still rejects PMXT exploratory data; the
    pairing rules live in :mod:`polymarket.replay_contract`.
    """
    return enforce_replay_mode_gate(config)


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
    # mandatory pre-run gate proves ts_init is monotonic on the mode's replay
    # clock (receive time in strict_capture, the shared PMXT contract clock in
    # pmxt_research); this Nautilus sort only syncs separately added native
    # data types into a single ts_init-ordered stream required by BacktestEngine.
    engine.sort_data()
    return len(order_book_deltas), len(trade_ticks), len(instrument_closes)


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


def run_from_config(config_path: Path, *, event_dir_override: Path | None = None) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = expand_config_paths(load_yaml(config_path))
    if event_dir_override is not None:
        adapter_input = config.setdefault("adapter", {}).setdefault("input", {})
        adapter_input["event_dir"] = str(event_dir_override)
    replay_mode = reject_pmxt_adapter_for_nautilus_backtest(config)
    run_id = str((config.get("runtime") or {}).get("run_id") or now_run_id())
    output_dir = resolve_output_dir(config_path, config.get("report") or {})
    run_dir = ensure_child(output_dir, output_dir / run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, run_dir / "original_config.yml")

    dataset = load_adapter(config)
    input_hashes = build_input_hashes(dataset)
    health_config = config.get("data_health") or {}
    data_health_report = analyze_dataset_health(
        dataset,
        future_tolerance_ms=float(health_config.get("future_tolerance_ms", 50.0)),
        delay_warning_ms=float(health_config.get("delay_warning_ms", 1_000.0)),
    )
    (run_dir / "data_health.json").write_text(data_health_report.to_json() + "\n", encoding="utf-8")
    data_health_artifact = describe_data_health_artifact(run_dir)
    blocking_issues = blocking_health_issues(data_health_report, mode=replay_mode)
    if blocking_issues:
        blocking_codes = sorted({issue.code for issue in blocking_issues})
        raise DataHealthError(
            "Polymarket data-health check failed before Nautilus backtest; "
            f"replay_mode={replay_mode}, blocking_codes={blocking_codes}. "
            f"Inspect {repo_relative_or_absolute(run_dir / 'data_health.json', repo_root=REPO_ROOT)}; "
            "do not sort the source data to make this pass.",
        )
    replay_clock_check: dict[str, Any] = {}
    if replay_mode == PMXT_RESEARCH_MODE:
        # Receive-time inversions are diagnostics in this mode; instead prove
        # the shared PMXT research replay clock is safe to consume as-is.
        replay_clock_check = verify_pmxt_replay_clock_order(dataset)
    replay_provenance = build_replay_provenance(mode=replay_mode, dataset_metadata=dataset.metadata)
    replay_provenance["replay_clock_check"] = replay_clock_check

    strategy_config = config.get("strategy") or {}
    strategy_enabled = bool(strategy_config) and strategy_config.get("enabled") is not False
    allow_ambiguous_ties = bool((config.get("replay") or {}).get("allow_ambiguous_ties", False))
    enforce_ambiguous_ties_gate(
        mode=replay_mode,
        strategy_enabled=strategy_enabled,
        ordering_ambiguous=bool(replay_provenance.get("ordering_ambiguous", False)),
        allow_ambiguous_ties=allow_ambiguous_ties,
    )
    if replay_mode == PMXT_RESEARCH_MODE:
        replay_provenance["ambiguous_ties_accepted"] = allow_ambiguous_ties
    health_gate = {
        "raw_health_ok": data_health_report.ok,
        "mode_health_gate_passed": True,
        "blocking_codes": [],
        "replay_clock_verified": bool(replay_clock_check.get("replay_clock_monotonic", replay_mode != PMXT_RESEARCH_MODE)),
    }
    # Full issue detail lives in data_health.json only; the copies embedded in
    # resolved_config.json / summary.json stay compact (PMXT-scale runs can
    # produce tens of thousands of diagnostic issues).
    issue_counts_by_code: dict[str, int] = {}
    for issue in data_health_report.issues:
        issue_counts_by_code[issue.code] = issue_counts_by_code.get(issue.code, 0) + 1
    full_health = data_health_report.to_dict()
    raw_checker_assumptions = list(full_health["assumptions"])
    if replay_mode == PMXT_RESEARCH_MODE:
        # The generic checker frames replay as receive-time ordered with
        # receive-time inversions as hard failures.  Neither statement applies
        # under the pmxt_research gate, so the inapplicable lines are rewritten
        # instead of appended-to; the raw checker's own framing stays only in
        # data_health.json (the raw checker artifact).
        mode_corrections = {
            "Replay chronology is timestamp_received order, not source timestamp order.": (
                "Replay chronology is the PMXT contract clock "
                "(timestamp, fallback timestamp_received), not receive-time order."
            ),
            "Hard failures are receive-time or local-sequence inversions.": (
                "Hard failures are local-sequence inversions and other non-PMXT error "
                "codes; receive-time inversions are diagnostics in pmxt_research mode."
            ),
        }
        health_assumptions = [
            mode_corrections.get(assumption, assumption) for assumption in raw_checker_assumptions
        ]
        mode_gate_assumptions = [
            "pmxt_research mode: replay chronology is the PMXT contract clock "
            "(timestamp, fallback timestamp_received); receive-time inversions are "
            "diagnostics here, not blockers, and 'ok: false' from the generic "
            "receive-time check does not mean the mode gate failed.",
        ]
    else:
        health_assumptions = raw_checker_assumptions
        mode_gate_assumptions = [
            "strict_capture mode: replay chronology is timestamp_received in capture "
            "order; receive-time and local-sequence inversions are hard failures.",
        ]
    compact_health = {
        "ok": full_health["ok"],
        "replay_mode": replay_mode,
        "summary": full_health["summary"],
        "assumptions": health_assumptions,
        "mode_gate_assumptions": mode_gate_assumptions,
        "raw_checker_artifact": "data_health.json",
        "issue_count": len(full_health["issues"]),
        "issue_counts_by_code": issue_counts_by_code,
        "issue_sample_limit": 20,
        "issue_sample": full_health["issues"][:20],
        "full_detail": "data_health.json",
        "artifact": data_health_artifact,
    }

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
        replay_clock=PMXT_REPLAY_CLOCK if replay_mode == PMXT_RESEARCH_MODE else RECEIVE_TIME_REPLAY_CLOCK,
    )
    replay_provenance["ts_init_audit"] = dict(conversion.ts_init_audit or {})
    engine_config_resolved = {
        "trader_id": str((config.get("engine") or {}).get("trader_id", "POLY-BACKTEST-001")),
        "book_type": "L2_MBP",
        "oms_type": "NETTING",
        "account_type": "CASH",
        "starting_balance": str((config.get("portfolio") or {}).get("starting_balance", "10000 pUSD")),
        "trade_execution": bool((config.get("engine") or {}).get("trade_execution", True)),
        "liquidity_consumption": bool((config.get("engine") or {}).get("liquidity_consumption", False)),
        "queue_position": bool((config.get("engine") or {}).get("queue_position", False)),
        "fee_model_enabled": bool((config.get("fees") or {}).get("enabled", True)),
        "maker_rebates_enabled": bool((config.get("fees") or {}).get("maker_rebates_enabled", False)),
    }

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
            data_health=compact_health,
            fees=fee_report,
            replay=replay_provenance,
            health_gate=health_gate,
            engine_config=engine_config_resolved,
            input_hashes=input_hashes,
            data_health_artifact=data_health_artifact,
        )
        resolved = {
            "engine": "nautilus_trader.backtest.engine.BacktestEngine",
            "engine_config": engine_config_resolved,
            "replay": replay_provenance,
            "data_health_gate": health_gate,
            "adapter": {
                "name": dataset.metadata.adapter_name,
                "adapter_version": dataset.metadata.adapter_version,
                "source_type": dataset.metadata.source_type,
                "source_files_resolved": list(dataset.metadata.source_files),
                "input_hashes": input_hashes,
                "assumptions": list(dataset.metadata.assumptions),
                "warnings": list(dataset.metadata.warnings),
                "source_quality": dict(dataset.metadata.source_quality),
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
            "data_health": compact_health,
            "runtime": {
                "run_id": run_id,
                "created_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "config_path": repo_relative_or_absolute(config_path, repo_root=REPO_ROOT),
                "run_dir": repo_relative_or_absolute(run_dir, repo_root=REPO_ROOT),
            },
        }
        (run_dir / "resolved_config.json").write_text(json.dumps(resolved, indent=2), encoding="utf-8")
        report_metadata = write_backtest_reports(
            run_dir=run_dir,
            result=result,
            account=engine.trader.generate_account_report(POLYMARKET_VENUE),
            fills=engine.trader.generate_order_fills_report(),
            positions=engine.trader.generate_positions_report(),
            instrument=instrument,
        )
        summary = {
            "run_dir": repo_relative_or_absolute(run_dir, repo_root=REPO_ROOT),
            "engine": "nautilus_trader.backtest.engine.BacktestEngine",
            "replay_mode": replay_provenance["mode"],
            "replay_clock": replay_provenance["replay_clock"],
            "data_credibility": replay_provenance["data_credibility"],
            "outputs": {
                "original_config": repo_relative_or_absolute(run_dir / "original_config.yml", repo_root=REPO_ROOT),
                "resolved_config": repo_relative_or_absolute(run_dir / "resolved_config.json", repo_root=REPO_ROOT),
                "data_health": repo_relative_or_absolute(run_dir / "data_health.json", repo_root=REPO_ROOT),
                "summary": repo_relative_or_absolute(run_dir / "summary.json", repo_root=REPO_ROOT),
                "account": repo_relative_or_absolute(run_dir / "account.csv", repo_root=REPO_ROOT),
                "fills": repo_relative_or_absolute(run_dir / "fills.csv", repo_root=REPO_ROOT),
                "positions": repo_relative_or_absolute(run_dir / "positions.csv", repo_root=REPO_ROOT),
                "raw_nautilus_account": repo_relative_or_absolute(
                    run_dir / "raw_nautilus" / "account.csv",
                    repo_root=REPO_ROOT,
                ),
                "raw_nautilus_fills": repo_relative_or_absolute(
                    run_dir / "raw_nautilus" / "fills.csv",
                    repo_root=REPO_ROOT,
                ),
                "raw_nautilus_positions": repo_relative_or_absolute(
                    run_dir / "raw_nautilus" / "positions.csv",
                    repo_root=REPO_ROOT,
                ),
                "run_report": repo_relative_or_absolute(run_dir / "run_report.md", repo_root=REPO_ROOT),
            },
            "data_count": result.data_count,
            "order_book_deltas_count": result.order_book_deltas_count,
            "trade_ticks_count": result.trade_ticks_count,
            "instrument_close_count": result.instrument_close_count,
            "settlement_mode": result.settlement.get("mode", "open"),
            "settlement_enabled": bool(result.settlement.get("enabled", False)),
            # raw_health_ok is the mode-agnostic health verdict; in pmxt_research
            # it may be false (receive-time inversions) while the mode gate passed.
            "data_health_ok": data_health_report.ok,
            "raw_health_ok": health_gate["raw_health_ok"],
            "mode_health_gate_passed": health_gate["mode_health_gate_passed"],
            "blocking_codes": health_gate["blocking_codes"],
            "replay_clock_verified": health_gate["replay_clock_verified"],
            "claim_scope": replay_provenance.get("claim_scope", "unknown"),
            "input_hashes": input_hashes,
            "fees": report_metadata.fees,
        }
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--event-dir",
        type=Path,
        default=None,
        help="Override adapter.input.event_dir (external data dependency, e.g. PolyReaper)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_from_config(args.config, event_dir_override=args.event_dir)


if __name__ == "__main__":
    main()

