# ruff: noqa: RUF001
"""
PMXT L2 factor baseline research artifact.

Runnable from the repository root, for example:

    python polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py \
        --config polymarket/research/2026-07-08-pmxt-l2-factor-baseline/experiment.yml

This is intentionally a research data script, not a Nautilus strategy/backtest.
It uses only the PMXT v1 adapter and data-health checker to load canonical
replay steps, then reconstructs selected-token top-of-book/L2 state in PMXT
timestamp order.

Trust boundary: this script does not compute fills, fees, queue position, cash,
positions, PnL, or execution results. Those belong to Nautilus strategy backtests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import deque
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.data_health import analyze_dataset_health  # noqa: E402


DEFAULT_HORIZONS_SECONDS = (60, 300, 900)
VALID_BOOK = "valid"
LOCKED_BOOK = "locked"
CROSSED_BOOK = "crossed"
MISSING_BOOK = "missing"
BOOK_VALIDITY_ORDER = (VALID_BOOK, LOCKED_BOOK, CROSSED_BOOK, MISSING_BOOK)
TRUST_METADATA_BASE = {
    "data_tier": "TIER1_EXPLORATORY",
    "replay_clock": "timestamp",
    "ordering_key": "timestamp,timestamp_received,_original_row_index",
    "causality": "pmxt_source_time_ordered_not_exchange_sequence",
    "execution_claims_allowed": False,
    "source_time_policy": "primary_sort_key",
    "not_for_pnl": True,
    "diagnostic_non_causal": True,
}


@dataclass(frozen=True)
class RunSummary:
    config_path: str
    output_dir: str
    rows_loaded: int
    factor_rows: int
    analysis_rows: int
    first_timestamp_received: str | None
    last_timestamp_received: str | None
    first_replay_timestamp: str | None
    last_replay_timestamp: str | None
    replay_order_ok: bool
    health_warning_count: int
    health_error_count: int
    valid_book_rows: int
    locked_book_rows: int
    crossed_book_rows: int
    missing_book_rows: int
    panel_path: str
    panel_format: str
    factor_summary_path: str
    quantile_returns_path: str
    book_validity_summary_path: str
    spread_summary_path: str
    label_slippage_summary_path: str
    input_hashes_path: str
    run_metadata_path: str
    trust_metadata: dict[str, Any]
    report_path: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Build PMXT L2 factor baseline panel.")
    parser.add_argument("--config", required=True, type=Path, help="Path to experiment.yml")
    args = parser.parse_args()

    config_path = args.config.resolve()
    config = load_config(config_path)
    output_dir = resolve_output_dir(config, config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset = PMXTEventV1Adapter(repo_root=REPO_ROOT).load(config)
    health = analyze_dataset_health(dataset)
    health_path = output_dir / "data_health_summary.json"
    health_path.write_text(
        json.dumps(compact_health_report(health), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    blocking_issues = pmxt_research_blocking_health_issues(health)
    if blocking_issues:
        raise RuntimeError(
            "PMXT factor research refuses to continue because non-PMXT-ordering health checks failed; "
            f"blocking_codes={sorted({issue.code for issue in blocking_issues})}; inspect {health_path}",
        )

    panel = build_factor_panel(dataset, config)
    panel = add_labels(panel, config)
    analysis_panel = valid_book_panel(panel)

    panel_path, panel_format = write_factor_panel(panel, output_dir)
    factor_summary = build_factor_summary(analysis_panel, dataset, health, config, raw_panel=panel)
    factor_summary_path = output_dir / "factor_summary.csv"
    factor_summary.to_csv(factor_summary_path, index=False)

    quantile_returns = build_quantile_returns(analysis_panel, config)
    quantile_returns_path = output_dir / "quantile_returns.csv"
    quantile_returns.to_csv(quantile_returns_path, index=False)

    book_validity_summary = build_book_validity_summary(panel)
    book_validity_summary_path = output_dir / "book_validity_summary.csv"
    book_validity_summary.to_csv(book_validity_summary_path, index=False)

    spread_summary = build_spread_summary(panel)
    spread_summary_path = output_dir / "spread_summary.csv"
    spread_summary.to_csv(spread_summary_path, index=False)

    label_slippage_summary = build_label_slippage_summary(panel, config)
    label_slippage_summary_path = output_dir / "label_slippage_summary.csv"
    label_slippage_summary.to_csv(label_slippage_summary_path, index=False)

    input_hashes = build_input_hashes(dataset)
    input_hashes_path = output_dir / "input_hashes.json"
    input_hashes_path.write_text(
        json.dumps(input_hashes, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    trust_metadata = build_trust_metadata(health, panel, source_quality=dataset.metadata.source_quality)
    run_metadata_path = write_run_metadata(output_dir, trust_metadata, health)

    issues = health.issues
    validity_counts = book_validity_counts(panel)
    summary = RunSummary(
        config_path=str(config_path),
        output_dir=str(output_dir),
        rows_loaded=len(dataset.steps),
        factor_rows=len(panel),
        analysis_rows=len(analysis_panel),
        first_timestamp_received=iso_or_none(panel["timestamp_received"].min()) if not panel.empty else None,
        last_timestamp_received=iso_or_none(panel["timestamp_received"].max()) if not panel.empty else None,
        first_replay_timestamp=iso_or_none(panel["replay_timestamp"].min()) if not panel.empty else None,
        last_replay_timestamp=iso_or_none(panel["replay_timestamp"].max()) if not panel.empty else None,
        replay_order_ok=health.ok,
        health_warning_count=sum(1 for issue in issues if issue.severity == "warning"),
        health_error_count=sum(1 for issue in issues if issue.severity == "error"),
        valid_book_rows=validity_counts[VALID_BOOK],
        locked_book_rows=validity_counts[LOCKED_BOOK],
        crossed_book_rows=validity_counts[CROSSED_BOOK],
        missing_book_rows=validity_counts[MISSING_BOOK],
        panel_path=str(panel_path),
        panel_format=panel_format,
        factor_summary_path=str(factor_summary_path),
        quantile_returns_path=str(quantile_returns_path),
        book_validity_summary_path=str(book_validity_summary_path),
        spread_summary_path=str(spread_summary_path),
        label_slippage_summary_path=str(label_slippage_summary_path),
        input_hashes_path=str(input_hashes_path),
        run_metadata_path=str(run_metadata_path),
        trust_metadata=trust_metadata,
        report_path=str(output_dir / "report.md"),
    )
    write_report(
        output_dir / "report.md",
        summary,
        config,
        health,
        factor_summary,
        quantile_returns,
        book_validity_summary,
        spread_summary,
        label_slippage_summary,
        input_hashes,
        trust_metadata=trust_metadata,
    )
    (output_dir / "run_summary.json").write_text(
        json.dumps(asdict(summary), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
    return 0


def load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("PyYAML is required to read experiment.yml") from exc
    with path.open("r", encoding="utf-8-sig") as file:
        loaded = yaml.safe_load(file)
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping config in {path}")
    return loaded


def resolve_output_dir(config: dict[str, Any], config_path: Path) -> Path:
    output = config.get("output", {})
    configured = output.get("dir") if isinstance(output, dict) else None
    if configured:
        path = Path(str(configured))
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()
    return (config_path.parent / "outputs").resolve()


def reconstruct_l2_book(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct L2 book states from simple dict updates for unit tests/reuse."""
    bid_levels: dict[Decimal, Decimal] = {}
    ask_levels: dict[Decimal, Decimal] = {}
    tick_size: Decimal | None = None
    states: list[dict[str, Any]] = []
    for sequence, update in enumerate(updates, start=1):
        event_type = "trade" if update.get("event_type") == "last_trade_price" else str(update.get("event_type"))
        trade_pressure = Decimal(0)
        if event_type == "book":
            bid_levels = levels_from_dicts(update.get("bids", []))
            ask_levels = levels_from_dicts(update.get("asks", []))
        elif event_type == "price_change":
            levels = bid_levels if update.get("side") == "BUY" else ask_levels
            price = decimal_from(update.get("price"))
            size = decimal_from(update.get("size"))
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size
        elif event_type == "trade":
            trade_pressure = decimal_from(update.get("size"))
            if update.get("side") == "SELL":
                trade_pressure = -trade_pressure
        elif event_type == "tick_size_change":
            tick_size = decimal_from(update.get("new_tick_size"))
        states.append(
            {
                "timestamp_received": pd.Timestamp(update.get("timestamp")),
                "sequence": sequence,
                "event_type": event_type,
                "asset_id": update.get("asset_id"),
                "bid_levels": dict(bid_levels),
                "ask_levels": dict(ask_levels),
                "trade_pressure": trade_pressure,
                "tick_size": tick_size,
                "tail_regime": tick_size is not None,
            },
        )
    return states


def calculate_l2_factors(states: list[dict[str, Any]]) -> pd.DataFrame:
    """Calculate a compact L2 factor frame from reconstructed dict states."""
    rows: list[dict[str, Any]] = []
    for state in states:
        bid_levels = sorted(state["bid_levels"].items(), reverse=True)
        ask_levels = sorted(state["ask_levels"].items())
        bid1_dec, bid_size1_dec = bid_levels[0] if bid_levels else (None, None)
        ask1_dec, ask_size1_dec = ask_levels[0] if ask_levels else (None, None)
        bid1 = float(bid1_dec) if bid1_dec is not None else math.nan
        ask1 = float(ask1_dec) if ask1_dec is not None else math.nan
        bid_size1 = float(bid_size1_dec) if bid_size1_dec is not None else math.nan
        ask_size1 = float(ask_size1_dec) if ask_size1_dec is not None else math.nan
        mid = (bid1 + ask1) / 2 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan
        spread = ask1 - bid1 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan
        bid_depth_3 = decimal_depth(bid_levels, 3)
        ask_depth_3 = decimal_depth(ask_levels, 3)
        bid_depth_5 = decimal_depth(bid_levels, 5)
        ask_depth_5 = decimal_depth(ask_levels, 5)
        microprice = compute_microprice(bid1, ask1, bid_size1, ask_size1)
        validity = classify_book_validity(bid1, ask1)
        rows.append(
            {
                "timestamp_received": state["timestamp_received"],
                "sequence": state["sequence"],
                "bid1": bid1,
                "ask1": ask1,
                "bid_size1": bid_size1,
                "ask_size1": ask_size1,
                "mid": mid,
                "spread": spread,
                "book_validity": validity,
                "is_valid_book": validity == VALID_BOOK,
                "microprice": microprice,
                "microprice_minus_mid": microprice - mid if math.isfinite(microprice) and math.isfinite(mid) else math.nan,
                "depth_imbalance_1": imbalance(float(bid_size1_dec or 0), float(ask_size1_dec or 0)),
                "depth_imbalance_3": imbalance(float(bid_depth_3), float(ask_depth_3)),
                "depth_imbalance_5": imbalance(float(bid_depth_5), float(ask_depth_5)),
                "bid_depth_3": float(bid_depth_3),
                "bid_depth_5": float(bid_depth_5),
                "ask_depth_3": float(ask_depth_3),
                "ask_depth_5": float(ask_depth_5),
                "trade_pressure_30s": float(state["trade_pressure"]),
                "distance_to_boundary": min(mid, 1.0 - mid) if math.isfinite(mid) else math.nan,
                "tail_regime": state["tail_regime"],
            },
        )
    return pd.DataFrame(rows)


def build_l2_factor_dataset(updates: list[dict[str, Any]], *, horizon_rows: int) -> pd.DataFrame:
    """Build factors plus row-offset mid-return labels for deterministic tests."""
    frame = calculate_l2_factors(reconstruct_l2_book(updates))
    future_bid = frame["bid1"].shift(-horizon_rows)
    future_ask = frame["ask1"].shift(-horizon_rows)
    future_mid = (future_bid + future_ask) / 2
    frame["future_bid1"] = future_bid
    frame["future_ask1"] = future_ask
    frame["future_mid_return"] = future_mid - frame["mid"]
    return frame


def levels_from_dicts(levels: Any) -> dict[Decimal, Decimal]:
    return {
        decimal_from(level["price"]): decimal_from(level["size"])
        for level in levels
        if decimal_from(level["size"]) > 0
    }


def decimal_from(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def decimal_depth(levels: list[tuple[Decimal, Decimal]], count: int) -> Decimal:
    return sum((size for _, size in levels[:count]), Decimal(0))


def compact_health_report(health: Any, issue_limit: int = 100) -> dict[str, Any]:
    data = health.to_dict()
    issues = data.get("issues", [])
    issue_counts_by_code: dict[str, int] = {}
    for issue in issues:
        code = str(issue.get("code", "unknown")) if isinstance(issue, dict) else "unknown"
        issue_counts_by_code[code] = issue_counts_by_code.get(code, 0) + 1
    return {
        "ok": data.get("ok"),
        "summary": data.get("summary"),
        "issue_count": len(issues),
        "issue_counts_by_code": issue_counts_by_code,
        "issues_truncated": len(issues) > issue_limit,
        "issue_sample_limit": issue_limit,
        "issue_sample": issues[:issue_limit],
        "assumptions": data.get("assumptions", []),
    }


def pmxt_research_blocking_health_issues(health: Any) -> list[Any]:
    """
    Return health issues that still block PMXT timestamp-ordered research.

    The generic data-health checker is receive-time oriented for live/Nautilus
    inputs. PMXT v2 research is intentionally timestamp ordered, so receive-time
    inversions are retained as diagnostics rather than used as a hard blocker.
    """
    ignored_error_codes = {"receive_time_inversion"}
    return [
        issue
        for issue in getattr(health, "issues", ())
        if getattr(issue, "severity", None) == "error"
        and getattr(issue, "code", None) not in ignored_error_codes
    ]


def build_trust_metadata(
    health: Any,
    panel: pd.DataFrame | None = None,
    *,
    source_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return machine-readable trust/clock/execution-boundary metadata."""
    health_summary = getattr(health, "summary", None)
    source_quality = dict(source_quality or {})
    source_time_diagnostics = {
        "source_time_inversion_count": int(getattr(health_summary, "source_time_inversion_count", 0) or 0),
        "source_delay_over_threshold_count": int(
            getattr(health_summary, "source_delay_over_threshold_count", 0) or 0,
        ),
        "future_source_time_count": int(getattr(health_summary, "future_source_time_count", 0) or 0),
        "source_timestamp_missing_step_count": int(
            getattr(health_summary, "source_timestamp_missing_step_count", 0) or 0,
        ),
    }
    book_counts = book_validity_counts(panel) if panel is not None else dict.fromkeys(BOOK_VALIDITY_ORDER, 0)
    has_clock_warning = any(source_time_diagnostics.values()) or any(
        getattr(issue, "severity", None) == "warning"
        and str(getattr(issue, "code", "")).startswith(("source_", "future_source_time", "missing_source"))
        for issue in getattr(health, "issues", ())
    )
    has_book_warning = any(book_counts.get(validity, 0) for validity in (LOCKED_BOOK, CROSSED_BOOK, MISSING_BOOK))
    ordering_status = str(source_quality.get("orderingStatus", "unknown"))
    ordering_ambiguous = ordering_status == "ambiguous" or bool(source_quality.get("orderingAmbiguousRows", 0))
    run_grade = "TIER1_OK_TIMESTAMP_ORDERED"
    if ordering_ambiguous:
        run_grade = "TIER1_CAUTION_ORDERING_AMBIGUOUS"
    elif has_clock_warning or has_book_warning:
        run_grade = "TIER1_CAUTION_PMXT_QUALITY"
    return {
        **TRUST_METADATA_BASE,
        "run_grade": run_grade,
        "source_quality": source_quality,
        "ordering_ambiguous": ordering_ambiguous,
        "source_time_diagnostics": source_time_diagnostics,
        "book_validity_counts": {key: int(value) for key, value in book_counts.items()},
        "book_validity_warning": has_book_warning,
        "claim_boundary": (
            "Exploratory PMXT timestamp-ordered L2 factors and future mid-return labels only; "
            "stable fallback is reproducible but not proof of true exchange/message order; no fees, fills, "
            "queue position, cash, positions, PnL, executable edge, or tradeable claims."
        ),
    }


def write_run_metadata(output_dir: Path, trust_metadata: dict[str, Any], health: Any) -> Path:
    path = output_dir / "run_metadata.json"
    health_report = compact_health_report(health, issue_limit=100)
    path.write_text(
        json.dumps(
            {
                "trust_metadata": trust_metadata,
                "source_quality": trust_metadata.get("source_quality", {}),
                "health_summary": health_report.get("summary", {}),
                "health_issue_counts_by_code": health_report.get("issue_counts_by_code", {}),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def build_factor_panel(dataset: Any, config: dict[str, Any]) -> pd.DataFrame:  # noqa: C901
    max_depth = int(config.get("factors", {}).get("max_depth_levels", 5))
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    tick_size: float | None = None
    rows: list[dict[str, Any]] = []
    ofi_window: deque[tuple[datetime, float]] = deque()
    trade_window: deque[tuple[datetime, float]] = deque()
    previous_bid1: float | None = None
    previous_ask1: float | None = None
    previous_bid_size1: float | None = None
    previous_ask_size1: float | None = None

    for step in dataset.steps:
        if len(step.updates) != 1:
            raise ValueError(
                "PMXT L2 factor baseline expects one canonical update per replay step; "
                f"sequence={step.sequence} update_count={len(step.updates)}",
            )
        update = step.updates[0]
        replay_timestamp = step.timestamp or step.timestamp_received
        trade_pressure_increment = 0.0
        if update.event_type == "book":
            bids = {to_float(level.price): to_float(level.size) for level in update.bids if to_float(level.size) > 0}
            asks = {to_float(level.price): to_float(level.size) for level in update.asks if to_float(level.size) > 0}
        elif update.event_type == "price_change":
            book_side = bids if update.side == "BUY" else asks if update.side == "SELL" else None
            if book_side is not None and update.price is not None and update.size is not None:
                price = to_float(update.price)
                size = to_float(update.size)
                if size <= 0:
                    book_side.pop(price, None)
                else:
                    book_side[price] = size
        elif update.event_type == "trade":
            signed = signed_size(update.side, update.size)
            trade_pressure_increment = signed
            trade_window.append((replay_timestamp, signed))
        elif update.event_type == "tick_size_change":
            tick_size = to_float(update.new_tick_size) if update.new_tick_size is not None else tick_size

        top_bids = sorted(((p, s) for p, s in bids.items() if s > 0), reverse=True)[:max_depth]
        top_asks = sorted((p, s) for p, s in asks.items() if s > 0)[:max_depth]
        bid1, bid_size1 = top_bids[0] if top_bids else (math.nan, math.nan)
        ask1, ask_size1 = top_asks[0] if top_asks else (math.nan, math.nan)
        mid = (bid1 + ask1) / 2 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan
        spread = ask1 - bid1 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan
        validity = classify_book_validity(bid1, ask1)
        microprice = compute_microprice(bid1, ask1, bid_size1, ask_size1)
        microprice_minus_mid = microprice - mid if math.isfinite(microprice) and math.isfinite(mid) else math.nan

        ofi_increment = compute_ofi_increment(
            bid1,
            ask1,
            bid_size1,
            ask_size1,
            previous_bid1,
            previous_ask1,
            previous_bid_size1,
            previous_ask_size1,
        )
        ofi_window.append((replay_timestamp, ofi_increment))
        expire_before = replay_timestamp - timedelta(seconds=30)
        while ofi_window and ofi_window[0][0] < expire_before:
            ofi_window.popleft()
        while trade_window and trade_window[0][0] < expire_before:
            trade_window.popleft()

        bid_depth_3 = depth(top_bids, 3)
        ask_depth_3 = depth(top_asks, 3)
        bid_depth_5 = depth(top_bids, 5)
        ask_depth_5 = depth(top_asks, 5)
        rows.append(
            {
                "timestamp_received": step.timestamp_received,
                "timestamp": step.timestamp,
                "replay_timestamp": replay_timestamp,
                "sequence": step.sequence,
                "event_type": update.event_type,
                "market": update.market,
                "asset_id": update.asset_id,
                "bid1": bid1,
                "ask1": ask1,
                "bid_size1": bid_size1,
                "ask_size1": ask_size1,
                "mid": mid,
                "spread": spread,
                "book_validity": validity,
                "is_valid_book": validity == VALID_BOOK,
                "microprice": microprice,
                "microprice_minus_mid": microprice_minus_mid,
                "depth_imbalance_1": imbalance(depth(top_bids, 1), depth(top_asks, 1)),
                "depth_imbalance_3": imbalance(bid_depth_3, ask_depth_3),
                "depth_imbalance_5": imbalance(bid_depth_5, ask_depth_5),
                "bid_depth_3": bid_depth_3,
                "bid_depth_5": bid_depth_5,
                "ask_depth_3": ask_depth_3,
                "ask_depth_5": ask_depth_5,
                "ofi_30s": sum(value for _, value in ofi_window),
                "trade_pressure_30s": sum(value for _, value in trade_window),
                "distance_to_boundary": min(mid, 1.0 - mid) if math.isfinite(mid) else math.nan,
                "tail_regime": tail_regime(mid),
                "tick_size": tick_size,
                "trade_pressure_step": trade_pressure_increment,
            },
        )
        previous_bid1 = bid1 if math.isfinite(bid1) else previous_bid1
        previous_ask1 = ask1 if math.isfinite(ask1) else previous_ask1
        previous_bid_size1 = bid_size1 if math.isfinite(bid_size1) else previous_bid_size1
        previous_ask_size1 = ask_size1 if math.isfinite(ask_size1) else previous_ask_size1

    return pd.DataFrame(rows)


def add_labels(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    if panel.empty:
        return panel
    clock_col = "replay_timestamp" if "replay_timestamp" in panel.columns else "timestamp_received"
    panel = panel.sort_values([clock_col, "sequence"], kind="mergesort").reset_index(drop=True)
    if "book_validity" not in panel:
        panel["book_validity"] = [
            classify_book_validity(float(bid), float(ask))
            for bid, ask in zip(panel["bid1"], panel["ask1"], strict=True)
        ]
        panel["is_valid_book"] = panel["book_validity"] == VALID_BOOK
    base = (
        panel[[clock_col, "sequence", "mid", "bid1", "ask1", "book_validity"]]
        .sort_values([clock_col, "sequence"], kind="mergesort")
        .groupby(clock_col, sort=False, as_index=False)
        .tail(1)
        .drop(columns=["sequence"])
    )
    for horizon in horizons_from_config(config):
        matched_col = f"label_matched_timestamp_{horizon}s"
        target_col = f"label_target_timestamp_{horizon}s"
        future = base.rename(
            columns={
                clock_col: matched_col,
                "mid": f"future_mid_{horizon}s",
                "bid1": f"future_bid_{horizon}s",
                "ask1": f"future_ask_{horizon}s",
                "book_validity": f"future_book_validity_{horizon}s",
            },
        )
        left = pd.DataFrame(
            {
                target_col: panel[clock_col] + pd.to_timedelta(horizon, unit="s"),
                "_row": panel.index,
            },
        ).sort_values(target_col, kind="mergesort")
        right = future.sort_values(matched_col, kind="mergesort")
        merged = pd.merge_asof(
            left,
            right,
            left_on=target_col,
            right_on=matched_col,
            direction="forward",
            allow_exact_matches=True,
        ).sort_values("_row")
        future_mid = merged[f"future_mid_{horizon}s"].reset_index(drop=True)
        panel[target_col] = merged[target_col].reset_index(drop=True)
        panel[matched_col] = merged[matched_col].reset_index(drop=True)
        panel[f"future_mid_{horizon}s"] = future_mid
        panel[f"future_bid_{horizon}s"] = merged[f"future_bid_{horizon}s"].reset_index(drop=True)
        panel[f"future_ask_{horizon}s"] = merged[f"future_ask_{horizon}s"].reset_index(drop=True)
        panel[f"future_book_validity_{horizon}s"] = merged[f"future_book_validity_{horizon}s"].reset_index(drop=True)
        panel[f"future_mid_return_{horizon}s"] = future_mid - panel["mid"]
        slippage = merged[matched_col].reset_index(drop=True) - panel[target_col]
        panel[f"label_slippage_seconds_{horizon}s"] = slippage.dt.total_seconds()
    return panel


def write_factor_panel(panel: pd.DataFrame, output_dir: Path) -> tuple[Path, str]:
    parquet_path = output_dir / "factor_panel.parquet"
    try:
        panel.to_parquet(parquet_path, index=False)
        return parquet_path, "parquet"
    except (ImportError, ValueError, OSError):
        csv_path = output_dir / "factor_panel.csv"
        panel.to_csv(csv_path, index=False)
        return csv_path, "csv"


def build_factor_summary(
    panel: pd.DataFrame,
    dataset: Any,
    health: Any,
    config: dict[str, Any],
    *,
    raw_panel: pd.DataFrame | None = None,
) -> pd.DataFrame:
    raw = raw_panel if raw_panel is not None else panel
    counts = book_validity_counts(raw)
    trust_metadata = build_trust_metadata(
        health,
        raw,
        source_quality=getattr(dataset.metadata, "source_quality", {}),
    )
    rows: list[dict[str, Any]] = [
        {"metric": "data_tier", "value": trust_metadata["data_tier"]},
        {"metric": "run_grade", "value": trust_metadata["run_grade"]},
        {"metric": "replay_clock", "value": trust_metadata["replay_clock"]},
        {"metric": "ordering_key", "value": trust_metadata["ordering_key"]},
        {"metric": "causality", "value": trust_metadata["causality"]},
        {"metric": "execution_claims_allowed", "value": trust_metadata["execution_claims_allowed"]},
        {"metric": "source_time_policy", "value": trust_metadata["source_time_policy"]},
        {"metric": "not_for_pnl", "value": trust_metadata["not_for_pnl"]},
        {"metric": "ordering_ambiguous", "value": trust_metadata["ordering_ambiguous"]},
        {
            "metric": "source_quality.orderingStatus",
            "value": trust_metadata.get("source_quality", {}).get("orderingStatus", "unknown"),
        },
        {
            "metric": "source_quality.orderingAmbiguousRows",
            "value": trust_metadata.get("source_quality", {}).get("orderingAmbiguousRows", 0),
        },
        {"metric": "dataset_id", "value": dataset.metadata.dataset_id},
        {"metric": "adapter", "value": f"{dataset.metadata.adapter_name}:{dataset.metadata.adapter_version}"},
        {"metric": "steps", "value": len(dataset.steps)},
        {"metric": "raw_factor_rows", "value": len(raw)},
        {"metric": "analysis_rows_valid_current_book", "value": len(panel)},
        {"metric": "replay_order_ok", "value": health.ok},
        {"metric": "health_issue_count", "value": len(health.issues)},
        {"metric": "book_valid_rows", "value": counts[VALID_BOOK]},
        {"metric": "book_locked_rows", "value": counts[LOCKED_BOOK]},
        {"metric": "book_crossed_rows", "value": counts[CROSSED_BOOK]},
        {"metric": "book_missing_rows", "value": counts[MISSING_BOOK]},
        {"metric": "first_timestamp_received", "value": iso_or_none(raw["timestamp_received"].min()) if not raw.empty else ""},
        {"metric": "last_timestamp_received", "value": iso_or_none(raw["timestamp_received"].max()) if not raw.empty else ""},
        {
            "metric": "first_replay_timestamp",
            "value": iso_or_none(raw["replay_timestamp"].min()) if not raw.empty and "replay_timestamp" in raw else "",
        },
        {
            "metric": "last_replay_timestamp",
            "value": iso_or_none(raw["replay_timestamp"].max()) if not raw.empty and "replay_timestamp" in raw else "",
        },
    ]
    numeric_columns = [
        "bid1",
        "ask1",
        "bid_size1",
        "ask_size1",
        "mid",
        "spread",
        "microprice_minus_mid",
        "depth_imbalance_1",
        "depth_imbalance_3",
        "depth_imbalance_5",
        "ofi_30s",
        "trade_pressure_30s",
        "distance_to_boundary",
    ]
    for column in numeric_columns:
        if column in panel:
            series = pd.to_numeric(panel[column], errors="coerce")
            rows.extend(
                [
                    {"metric": f"{column}.count", "value": int(series.count())},
                    {"metric": f"{column}.mean", "value": series.mean()},
                    {"metric": f"{column}.p05", "value": series.quantile(0.05)},
                    {"metric": f"{column}.p50", "value": series.quantile(0.50)},
                    {"metric": f"{column}.p95", "value": series.quantile(0.95)},
                ],
            )
    for horizon in horizons_from_config(config):
        col = f"future_mid_return_{horizon}s"
        future_valid_col = f"future_book_validity_{horizon}s"
        if col in panel:
            frame = panel
            if future_valid_col in panel:
                frame = frame[frame[future_valid_col] == VALID_BOOK]
            series = pd.to_numeric(frame[col], errors="coerce")
            rows.extend(
                [
                    {"metric": f"{col}.valid_current_and_future_count", "value": int(series.count())},
                    {"metric": f"{col}.mean", "value": series.mean()},
                    {"metric": f"{col}.p50", "value": series.quantile(0.50)},
                ],
            )
    return pd.DataFrame(rows)


def build_quantile_returns(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    factors = ["microprice_minus_mid", "depth_imbalance_1", "depth_imbalance_3", "depth_imbalance_5", "ofi_30s", "trade_pressure_30s"]
    rows: list[dict[str, Any]] = []
    for factor in factors:
        values = pd.to_numeric(panel.get(factor), errors="coerce")
        if values is None or values.notna().sum() < 10 or values.nunique(dropna=True) < 2:
            continue
        try:
            quantiles = pd.qcut(values, q=5, labels=False, duplicates="drop") + 1
        except ValueError:
            continue
        for horizon in horizons_from_config(config):
            ret_col = f"future_mid_return_{horizon}s"
            future_valid_col = f"future_book_validity_{horizon}s"
            if ret_col not in panel:
                continue
            frame = pd.DataFrame(
                {
                    "quantile": quantiles,
                    "ret": pd.to_numeric(panel[ret_col], errors="coerce"),
                    "future_book_validity": panel.get(future_valid_col, VALID_BOOK),
                },
            )
            frame = frame[frame["future_book_validity"] == VALID_BOOK].dropna(subset=["quantile", "ret"])
            grouped = frame.groupby("quantile", observed=True)["ret"]
            for quantile, series in grouped:
                rows.append(
                    {
                        "factor": factor,
                        "horizon_seconds": horizon,
                        "quantile": int(quantile),
                        "count": int(series.count()),
                        "mean_future_mid_return": series.mean(),
                        "median_future_mid_return": series.median(),
                    },
                )
    return pd.DataFrame(rows)


def build_book_validity_summary(panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total = len(panel)
    counts = book_validity_counts(panel)
    for validity in BOOK_VALIDITY_ORDER:
        count = counts[validity]
        rows.append(
            {
                "book_validity": validity,
                "count": count,
                "share": count / total if total else math.nan,
            },
        )
    return pd.DataFrame(rows)


def build_spread_summary(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty or "spread" not in panel:
        return pd.DataFrame(columns=["metric", "value"])
    spread = pd.to_numeric(panel["spread"], errors="coerce")
    return pd.DataFrame(
        [
            {"metric": "spread_count", "value": int(spread.count())},
            {"metric": "spread_min", "value": spread.min()},
            {"metric": "spread_p05", "value": spread.quantile(0.05)},
            {"metric": "spread_p50", "value": spread.quantile(0.50)},
            {"metric": "spread_p95", "value": spread.quantile(0.95)},
            {"metric": "spread_max", "value": spread.max()},
        ],
    )


def build_label_slippage_summary(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for horizon in horizons_from_config(config):
        slip_col = f"label_slippage_seconds_{horizon}s"
        matched_col = f"label_matched_timestamp_{horizon}s"
        future_valid_col = f"future_book_validity_{horizon}s"
        if slip_col not in panel:
            continue
        slippage = pd.to_numeric(panel[slip_col], errors="coerce")
        matched = panel[matched_col].notna() if matched_col in panel else slippage.notna()
        rows.append(
            {
                "horizon_seconds": horizon,
                "rows": len(panel),
                "matched_rows": int(matched.sum()),
                "missing_future_rows": int((~matched).sum()),
                "mean_slippage_seconds": slippage.mean(),
                "p95_slippage_seconds": slippage.quantile(0.95),
                "max_slippage_seconds": slippage.max(),
                "future_valid_rows": int((panel.get(future_valid_col) == VALID_BOOK).sum()) if future_valid_col in panel else int(matched.sum()),
                "future_invalid_rows": int((panel.get(future_valid_col) != VALID_BOOK).sum()) if future_valid_col in panel else 0,
            },
        )
    return pd.DataFrame(rows)


def build_input_hashes(dataset: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_file in getattr(dataset.metadata, "source_files", ()) or ():
        path = Path(str(source_file))
        resolved = path if path.is_absolute() else (REPO_ROOT / path).resolve()
        item: dict[str, Any] = {"source_file": str(source_file), "resolved_path": str(resolved), "exists": resolved.exists()}
        if resolved.exists() and resolved.is_file():
            item["size_bytes"] = resolved.stat().st_size
            item["sha256"] = sha256_file(resolved)
        rows.append(item)
    return rows


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_book_validity(bid1: float, ask1: float) -> str:
    if not math.isfinite(bid1) or not math.isfinite(ask1):
        return MISSING_BOOK
    spread = ask1 - bid1
    if spread < 0:
        return CROSSED_BOOK
    if spread == 0:
        return LOCKED_BOOK
    return VALID_BOOK


def valid_book_panel(panel: pd.DataFrame) -> pd.DataFrame:
    if "book_validity" not in panel:
        return panel.copy()
    return panel[panel["book_validity"] == VALID_BOOK].copy()


def book_validity_counts(panel: pd.DataFrame) -> dict[str, int]:
    if "book_validity" not in panel:
        return dict.fromkeys(BOOK_VALIDITY_ORDER, 0)
    raw_counts = panel["book_validity"].value_counts(dropna=False).to_dict()
    return {validity: int(raw_counts.get(validity, 0)) for validity in BOOK_VALIDITY_ORDER}


def write_report(
    path: Path,
    summary: RunSummary,
    config: dict[str, Any],
    health: Any,
    factor_summary: pd.DataFrame,
    quantile_returns: pd.DataFrame,
    book_validity_summary: pd.DataFrame,
    spread_summary: pd.DataFrame,
    label_slippage_summary: pd.DataFrame,
    input_hashes: list[dict[str, Any]],
    *,
    trust_metadata: dict[str, Any] | None = None,
) -> None:
    input_config = config.get("input", {})
    horizons = horizons_from_config(config)
    health_report = compact_health_report(health, issue_limit=10)
    health_summary = health_report.get("summary", {}) or {}
    trust_metadata = trust_metadata or build_trust_metadata(health, None)
    lines: list[str] = [
        "# PMXT L2 因子研究报告",
        "",
        f"生成时间: {datetime.now(UTC).isoformat()}",
        "",
        "## 0. 信任边界",
        "",
        "这份报告是 **PMXT L2 因子研究**，不是 Nautilus 回测、成交、PnL 或可交易收益报告。",
        "",
        "- 不计算手续费、返佣、订单状态、排队、部分成交、现金、仓位或 PnL。",
        "- 不把 `future_bid - current_ask` 之类的量解释成可成交利润。",
        "- fee/fill/PnL 必须放到 Nautilus 原生策略回测入口里处理。",
        "- 本报告只回答：按 PMXT `replay_timestamp` 重建 L2 后，盘口因子和未来 mid-return 标签是否可用于研究。",
        "",
        "Machine-readable boundary:",
        "",
        f"- data_tier: `{trust_metadata['data_tier']}`",
        f"- run_grade: `{trust_metadata['run_grade']}`",
        f"- replay_clock: `{trust_metadata['replay_clock']}`",
        f"- ordering_key: `{trust_metadata['ordering_key']}`",
        f"- causality: `{trust_metadata['causality']}`",
        f"- execution_claims_allowed: `{str(trust_metadata['execution_claims_allowed']).lower()}`",
        f"- source_time_policy: `{trust_metadata['source_time_policy']}`",
        f"- not_for_pnl: `{str(trust_metadata['not_for_pnl']).lower()}`",
        f"- diagnostic_non_causal: `{str(trust_metadata['diagnostic_non_causal']).lower()}`",
        f"- ordering_ambiguous: `{str(trust_metadata['ordering_ambiguous']).lower()}`",
        f"- source_quality.orderingStatus: `{trust_metadata.get('source_quality', {}).get('orderingStatus', 'unknown')}`",
        f"- source_quality.orderingAmbiguousRows: `{trust_metadata.get('source_quality', {}).get('orderingAmbiguousRows', 0)}`",
        f"- claim_boundary: {trust_metadata['claim_boundary']}",
        "",
        "## 1. 输入",
        "",
        f"- event_dir: `{input_config.get('event_dir')}`",
        f"- condition_id: `{input_config.get('condition_id')}`",
        f"- asset_id/token_id: `{input_config.get('asset_id')}`",
        f"- horizons_seconds: `{horizons}`",
        "",
        "## 2. 运行摘要",
        "",
        f"- loaded replay steps: {summary.rows_loaded}",
        f"- raw factor rows: {summary.factor_rows}",
        f"- valid-book analysis rows: {summary.analysis_rows}",
        f"- replay_timestamp span: {summary.first_replay_timestamp} to {summary.last_replay_timestamp}",
        f"- timestamp_received audit span: {summary.first_timestamp_received} to {summary.last_timestamp_received}",
        f"- replay-order hard check ok: {summary.replay_order_ok}",
        f"- data-health warnings/errors: {summary.health_warning_count}/{summary.health_error_count}",
        f"- valid/locked/crossed/missing book rows: {summary.valid_book_rows}/{summary.locked_book_rows}/{summary.crossed_book_rows}/{summary.missing_book_rows}",
        f"- factor panel: `{Path(summary.panel_path).name}` ({summary.panel_format})",
        "",
        "## 3. 输出文件",
        "",
        f"- `{Path(summary.panel_path).name}`: raw panel，保留全部 row，并带 `book_validity` 标记。",
        "- `factor_summary.csv`: 只基于 valid current book 的摘要。",
        "- `quantile_returns.csv`: 因子分位数 vs future mid-return；current/future book 都要求 valid。",
        "- `book_validity_summary.csv`: crossed/locked/missing book 统计。",
        "- `spread_summary.csv`: raw panel 的 spread 分布诊断。",
        "- `label_slippage_summary.csv`: forward as-of 标签匹配的时间滑移统计。",
        "- `input_hashes.json`: 输入文件大小和 sha256，用于复现实验。",
        "- `data_health_summary.json`: receive/source time 诊断摘要。",
        "- `run_metadata.json`: machine-readable trust tier/run grade/clock policy/claim boundary.",
        "- `run_summary.json` / `report.md`",
        "",
        "## 4. 数据健康解释",
        "",
        "`replay-order hard check ok` 来自通用 receive-time data_health；PMXT timestamp-ordered 研究会把 receive-time inversion 视为诊断，不把它当作 PMXT 研究硬失败。",
        "PMXT v2 的当前口径按 `timestamp, timestamp_received, stable fallback` 排序；fallback 只保证可复现，不代表真实 WebSocket/message 顺序。",
        "",
        f"- receive_time_inversion_count: {health_summary.get('receive_time_inversion_count')}",
        f"- sequence_inversion_count: {health_summary.get('sequence_inversion_count')}",
        f"- source_time_inversion_count: {health_summary.get('source_time_inversion_count')}",
        f"- source_delay_over_threshold_count: {health_summary.get('source_delay_over_threshold_count')}",
        f"- max_source_delay_ms: {health_summary.get('max_source_delay_ms')}",
        f"- missing_source_timestamp_step_count: {health_summary.get('source_timestamp_missing_step_count')}",
        "",
        "## 5. 盘口有效性",
        "",
    ]
    dataframe_to_markdown(book_validity_summary, lines)
    lines.extend(["", "Spread 分布诊断:", ""])
    dataframe_to_markdown(spread_summary, lines)
    lines.extend(
        [
            "",
            "说明：正式因子统计默认只使用 `book_validity == valid` 的 current row。",
            "crossed/locked/missing row 不删除；它们留在 panel 中用于诊断数据和 replay 质量。",
            "",
            "## 6. 标签滑移",
            "",
        ],
    )
    dataframe_to_markdown(label_slippage_summary, lines)
    lines.extend(
        [
            "",
            "标签构造：对每个 row 的 `replay_timestamp + horizon` 做 forward as-of；PMXT 中 `replay_timestamp` 优先使用 source `timestamp`，缺失时才退回 `timestamp_received`。",
            "如果同一 replay timestamp 有多条 replay row，标签用该 timestamp 的最后一个重建状态；若 tied group 内容不同，需要结合 ordering_ambiguous 和 sensitivity test 降级解读。",
            "",
            "## 7. 因子构造说明",
            "",
            "- `book` snapshot 重置 selected token book。",
            "- `price_change` 按 side/price/size 更新或删除价位。",
            "- `trade` 只贡献 signed 30-second trade pressure，不直接改 book。",
            "- `tick_size_change` 只记录当前 tick-size regime，不参与订单撮合。",
            "- 因子只用当前/过去 replay state；future label 单独生成。",
            "",
            "## 8. Selected summary metrics",
            "",
        ],
    )
    dataframe_to_markdown(factor_summary.head(30), lines)
    lines.extend(["", "## 9. Quantile return preview", ""])
    dataframe_to_markdown(quantile_returns.head(40), lines)
    lines.extend(["", "## 10. Data-health issue preview", ""])
    issue_counts = health_report.get("issue_counts_by_code", {})
    if issue_counts:
        lines.extend(["Issue counts by code:", ""])
        dataframe_to_markdown(
            pd.DataFrame(
                [
                    {"code": code, "count": count}
                    for code, count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0]))
                ],
            ),
            lines,
        )
        lines.append("")
    if health.issues:
        for issue in health.issues[:10]:
            lines.append(f"- {issue.severity}: {issue.code} at sequence {issue.sequence}: {issue.message}")
        if len(health.issues) > 10:
            lines.append(f"- ... {len(health.issues) - 10} additional issues summarized in data_health_summary.json")
    else:
        lines.append("- No issues reported.")
    lines.extend(["", "## 11. Input hashes", ""])
    dataframe_to_markdown(pd.DataFrame(input_hashes), lines)
    path.write_text("\n".join(str(line) for line in lines) + "\n", encoding="utf-8")

def dataframe_to_markdown(frame: pd.DataFrame, lines: list[Any]) -> str:
    if frame.empty:
        lines.append("_No rows._")
        return ""
    columns = [str(column) for column in frame.columns]
    lines.append("| " + " | ".join(escape_markdown_cell(column) for column in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(escape_markdown_cell(row[column]) for column in frame.columns) + " |")
    return ""


def escape_markdown_cell(value: Any) -> str:
    try:
        missing_value = pd.isna(value)
        missing = bool(missing_value) if not hasattr(missing_value, "__len__") else False
    except (TypeError, ValueError):
        missing = False
    text = "" if missing else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def horizons_from_config(config: dict[str, Any]) -> list[int]:
    labels = config.get("labels", {})
    horizons = labels.get("horizons_seconds", DEFAULT_HORIZONS_SECONDS) if isinstance(labels, dict) else DEFAULT_HORIZONS_SECONDS
    return [int(value) for value in horizons]


def to_float(value: Decimal | float | None) -> float:
    if value is None:
        return math.nan
    return float(value)


def signed_size(side: str | None, size: Decimal | None) -> float:
    if size is None:
        return 0.0
    value = to_float(size)
    if side == "BUY":
        return value
    if side == "SELL":
        return -value
    return 0.0


def compute_microprice(bid: float, ask: float, bid_size: float, ask_size: float) -> float:
    if not all(math.isfinite(value) for value in (bid, ask, bid_size, ask_size)):
        return math.nan
    denominator = bid_size + ask_size
    if denominator <= 0:
        return math.nan
    return (ask * bid_size + bid * ask_size) / denominator


def compute_ofi_increment(
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    previous_bid: float | None,
    previous_ask: float | None,
    previous_bid_size: float | None,
    previous_ask_size: float | None,
) -> float:
    if previous_bid is None or previous_ask is None or previous_bid_size is None or previous_ask_size is None:
        return 0.0
    bid_component = 0.0
    ask_component = 0.0
    if math.isfinite(bid) and math.isfinite(bid_size):
        if bid > previous_bid:
            bid_component = bid_size
        elif bid == previous_bid:
            bid_component = bid_size - previous_bid_size
        else:
            bid_component = -previous_bid_size
    if math.isfinite(ask) and math.isfinite(ask_size):
        if ask < previous_ask:
            ask_component = ask_size
        elif ask == previous_ask:
            ask_component = -(ask_size - previous_ask_size)
        else:
            ask_component = -previous_ask_size
    return bid_component + ask_component


def depth(levels: list[tuple[float, float]], count: int) -> float:
    return float(sum(size for _, size in levels[:count]))


def imbalance(bid_depth: float, ask_depth: float) -> float:
    denominator = bid_depth + ask_depth
    if denominator <= 0:
        return math.nan
    return (bid_depth - ask_depth) / denominator


def tail_regime(mid: float) -> str:
    if not math.isfinite(mid):
        return "unknown"
    if mid <= 0.10:
        return "low_tail"
    if mid >= 0.90:
        return "high_tail"
    return "interior"


def iso_or_none(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
