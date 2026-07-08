"""
PMXT L2 factor baseline research artifact.

Runnable from the repository root, for example:

    python polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py \
        --config polymarket/research/2026-07-08-pmxt-l2-factor-baseline/experiment.yml

This is intentionally a research data script, not a Nautilus strategy.  It uses
only the PMXT v1 adapter and data-health checker to load canonical replay steps,
then reconstructs selected-token top-of-book/L2 state in receive-time order.
"""

from __future__ import annotations

import argparse
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
DEFAULT_SENSITIVITY_FEES = (0.0, 0.001, 0.002, 0.005)


@dataclass(frozen=True)
class FeeResolution:
    taker_fee: float
    source: str


@dataclass(frozen=True)
class RunSummary:
    config_path: str
    output_dir: str
    rows_loaded: int
    factor_rows: int
    first_timestamp_received: str | None
    last_timestamp_received: str | None
    health_ok: bool
    health_warning_count: int
    health_error_count: int
    taker_fee: float
    taker_fee_source: str
    panel_path: str
    panel_format: str
    factor_summary_path: str
    quantile_returns_path: str
    fee_sensitivity_path: str
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
    fee_resolution = resolve_taker_fee(config, dataset)
    health = analyze_dataset_health(dataset)
    health_path = output_dir / "data_health_summary.json"
    health_path.write_text(json.dumps(compact_health_report(health), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    panel = build_factor_panel(dataset, config)
    panel = add_labels(panel, config, fee_resolution=fee_resolution)

    panel_path, panel_format = write_factor_panel(panel, output_dir)
    factor_summary = build_factor_summary(panel, dataset, health, config, fee_resolution=fee_resolution)
    factor_summary_path = output_dir / "factor_summary.csv"
    factor_summary.to_csv(factor_summary_path, index=False)

    quantile_returns = build_quantile_returns(panel, config)
    quantile_returns_path = output_dir / "quantile_returns.csv"
    quantile_returns.to_csv(quantile_returns_path, index=False)

    fee_sensitivity = build_fee_sensitivity(panel, config, fee_resolution=fee_resolution)
    fee_sensitivity_path = output_dir / "fee_sensitivity.csv"
    fee_sensitivity.to_csv(fee_sensitivity_path, index=False)

    issues = health.issues
    summary = RunSummary(
        config_path=str(config_path),
        output_dir=str(output_dir),
        rows_loaded=len(dataset.steps),
        factor_rows=len(panel),
        first_timestamp_received=iso_or_none(panel["timestamp_received"].iloc[0]) if not panel.empty else None,
        last_timestamp_received=iso_or_none(panel["timestamp_received"].iloc[-1]) if not panel.empty else None,
        health_ok=health.ok,
        health_warning_count=sum(1 for issue in issues if issue.severity == "warning"),
        health_error_count=sum(1 for issue in issues if issue.severity == "error"),
        taker_fee=fee_resolution.taker_fee,
        taker_fee_source=fee_resolution.source,
        panel_path=str(panel_path),
        panel_format=panel_format,
        factor_summary_path=str(factor_summary_path),
        quantile_returns_path=str(quantile_returns_path),
        fee_sensitivity_path=str(fee_sensitivity_path),
        report_path=str(output_dir / "report.md"),
    )
    write_report(
        output_dir / "report.md",
        summary,
        config,
        health,
        factor_summary,
        quantile_returns,
        fee_sensitivity,
        fee_resolution,
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
        bid_depth_3 = decimal_depth(bid_levels, 3)
        ask_depth_3 = decimal_depth(ask_levels, 3)
        bid_depth_5 = decimal_depth(bid_levels, 5)
        ask_depth_5 = decimal_depth(ask_levels, 5)
        microprice = compute_microprice(bid1, ask1, bid_size1, ask_size1)
        rows.append(
            {
                "timestamp_received": state["timestamp_received"],
                "sequence": state["sequence"],
                "bid1": bid1,
                "ask1": ask1,
                "bid_size1": bid_size1,
                "ask_size1": ask_size1,
                "mid": mid,
                "spread": ask1 - bid1 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan,
                "microprice": microprice,
                "microprice_edge": microprice - mid if math.isfinite(microprice) and math.isfinite(mid) else math.nan,
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


def build_l2_factor_dataset(
    updates: list[dict[str, Any]],
    *,
    horizon_rows: int,
    taker_fee: Decimal = Decimal(0),
) -> pd.DataFrame:
    """Build factors plus row-offset labels for small deterministic tests."""
    frame = calculate_l2_factors(reconstruct_l2_book(updates))
    future_bid = frame["bid1"].shift(-horizon_rows)
    future_ask = frame["ask1"].shift(-horizon_rows)
    future_updates = [updates[index + horizon_rows] if index + horizon_rows < len(updates) else {} for index in range(len(updates))]
    future_bid = future_bid.copy()
    future_ask = future_ask.copy()
    for index, update in enumerate(future_updates):
        if update.get("event_type") == "price_change" and update.get("price") is not None:
            if update.get("side") == "BUY":
                future_bid.iloc[index] = float(decimal_from(update["price"]))
            elif update.get("side") == "SELL":
                future_ask.iloc[index] = float(decimal_from(update["price"]))
    fee = float(taker_fee)
    frame["future_bid1"] = future_bid
    frame["future_ask1"] = future_ask
    frame["future_mid_return"] = ((future_bid + future_ask) / 2) - frame["mid"]
    frame["tradeable_long_edge"] = future_bid - frame["ask1"] - fee
    frame["tradeable_short_edge"] = frame["bid1"] - future_ask - fee
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

def build_factor_panel(dataset: Any, config: dict[str, Any]) -> pd.DataFrame:
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
        update = step.updates[0]
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
            trade_window.append((step.timestamp_received, signed))
        elif update.event_type == "tick_size_change":
            tick_size = to_float(update.new_tick_size) if update.new_tick_size is not None else tick_size

        top_bids = sorted(((p, s) for p, s in bids.items() if s > 0), reverse=True)[:max_depth]
        top_asks = sorted((p, s) for p, s in asks.items() if s > 0)[:max_depth]
        bid1, bid_size1 = top_bids[0] if top_bids else (math.nan, math.nan)
        ask1, ask_size1 = top_asks[0] if top_asks else (math.nan, math.nan)
        mid = (bid1 + ask1) / 2 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan
        spread = ask1 - bid1 if math.isfinite(bid1) and math.isfinite(ask1) else math.nan
        microprice = compute_microprice(bid1, ask1, bid_size1, ask_size1)
        microprice_edge = microprice - mid if math.isfinite(microprice) and math.isfinite(mid) else math.nan

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
        ofi_window.append((step.timestamp_received, ofi_increment))
        expire_before = step.timestamp_received - timedelta(seconds=30)
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
                "sequence": step.sequence,
                "event_type": update.event_type,
                "bid1": bid1,
                "ask1": ask1,
                "bid_size1": bid_size1,
                "ask_size1": ask_size1,
                "mid": mid,
                "spread": spread,
                "microprice": microprice,
                "microprice_edge": microprice_edge,
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


def add_labels(
    panel: pd.DataFrame,
    config: dict[str, Any],
    *,
    fee_resolution: FeeResolution | None = None,
) -> pd.DataFrame:
    if panel.empty:
        return panel
    panel = panel.sort_values(["timestamp_received", "sequence"], kind="mergesort").reset_index(drop=True)
    horizons = horizons_from_config(config)
    taker_fee = (fee_resolution or resolve_taker_fee(config)).taker_fee
    base = (
        panel[["timestamp_received", "sequence", "mid", "bid1", "ask1"]]
        .sort_values(["timestamp_received", "sequence"], kind="mergesort")
        .groupby("timestamp_received", sort=False, as_index=False)
        .tail(1)
        .drop(columns=["sequence"])
    )
    for horizon in horizons:
        future = base.rename(
            columns={"mid": f"future_mid_{horizon}s", "bid1": f"future_bid_{horizon}s", "ask1": f"future_ask_{horizon}s"},
        )
        left = pd.DataFrame(
            {
                "target_timestamp": panel["timestamp_received"] + pd.to_timedelta(horizon, unit="s"),
                "_row": panel.index,
            },
        ).sort_values("target_timestamp", kind="mergesort")
        right = future.sort_values("timestamp_received", kind="mergesort")
        merged = pd.merge_asof(
            left,
            right,
            left_on="target_timestamp",
            right_on="timestamp_received",
            direction="forward",
            allow_exact_matches=True,
        ).sort_values("_row")
        future_mid = merged[f"future_mid_{horizon}s"].reset_index(drop=True)
        future_bid = merged[f"future_bid_{horizon}s"].reset_index(drop=True)
        future_ask = merged[f"future_ask_{horizon}s"].reset_index(drop=True)
        panel[f"future_mid_{horizon}s"] = future_mid
        panel[f"future_bid_{horizon}s"] = future_bid
        panel[f"future_ask_{horizon}s"] = future_ask
        panel[f"future_mid_return_{horizon}s"] = future_mid - panel["mid"]
        panel[f"long_edge_{horizon}s"] = future_bid - panel["ask1"] - taker_fee
        panel[f"short_edge_{horizon}s"] = panel["bid1"] - future_ask - taker_fee
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
    fee_resolution: FeeResolution | None = None,
) -> pd.DataFrame:
    resolved_fee = fee_resolution or resolve_taker_fee(config, dataset)
    rows: list[dict[str, Any]] = [
        {"metric": "dataset_id", "value": dataset.metadata.dataset_id},
        {"metric": "adapter", "value": f"{dataset.metadata.adapter_name}:{dataset.metadata.adapter_version}"},
        {"metric": "steps", "value": len(dataset.steps)},
        {"metric": "factor_rows", "value": len(panel)},
        {"metric": "health_ok", "value": health.ok},
        {"metric": "health_issue_count", "value": len(health.issues)},
        {"metric": "first_timestamp_received", "value": iso_or_none(panel["timestamp_received"].iloc[0]) if not panel.empty else ""},
        {"metric": "last_timestamp_received", "value": iso_or_none(panel["timestamp_received"].iloc[-1]) if not panel.empty else ""},
        {"metric": "taker_fee", "value": resolved_fee.taker_fee},
        {"metric": "taker_fee_source", "value": resolved_fee.source},
    ]
    numeric_columns = [
        "bid1",
        "ask1",
        "bid_size1",
        "ask_size1",
        "mid",
        "spread",
        "microprice_edge",
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
        if col in panel:
            series = pd.to_numeric(panel[col], errors="coerce")
            rows.extend(
                [
                    {"metric": f"{col}.count", "value": int(series.count())},
                    {"metric": f"{col}.mean", "value": series.mean()},
                    {"metric": f"{col}.p50", "value": series.quantile(0.50)},
                ],
            )
    return pd.DataFrame(rows)


def build_quantile_returns(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    factors = ["microprice_edge", "depth_imbalance_1", "depth_imbalance_3", "depth_imbalance_5", "ofi_30s", "trade_pressure_30s"]
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
            if ret_col not in panel:
                continue
            frame = pd.DataFrame({"quantile": quantiles, "ret": pd.to_numeric(panel[ret_col], errors="coerce")}).dropna()
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


def build_fee_sensitivity(
    panel: pd.DataFrame,
    config: dict[str, Any],
    *,
    fee_resolution: FeeResolution | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    configured_fees = list(config.get("fees", {}).get("sensitivity_taker_fees", DEFAULT_SENSITIVITY_FEES))
    resolved_fee = fee_resolution or resolve_taker_fee(config)
    if resolved_fee.taker_fee not in {float(value) for value in configured_fees}:
        configured_fees.append(resolved_fee.taker_fee)
    fees = sorted({float(value) for value in configured_fees})
    for horizon in horizons_from_config(config):
        future_bid_col = f"future_bid_{horizon}s"
        future_ask_col = f"future_ask_{horizon}s"
        if future_bid_col not in panel or future_ask_col not in panel:
            continue
        for fee in fees:
            fee_float = float(fee)
            long_edge = pd.to_numeric(panel[future_bid_col], errors="coerce") - pd.to_numeric(panel["ask1"], errors="coerce") - fee_float
            short_edge = pd.to_numeric(panel["bid1"], errors="coerce") - pd.to_numeric(panel[future_ask_col], errors="coerce") - fee_float
            valid_long = long_edge.dropna()
            valid_short = short_edge.dropna()
            rows.append(
                {
                    "horizon_seconds": horizon,
                    "taker_fee": fee_float,
                    "long_count": int(valid_long.count()),
                    "long_positive_rate": float((valid_long > 0).mean()) if not valid_long.empty else math.nan,
                    "long_mean_edge": valid_long.mean(),
                    "short_count": int(valid_short.count()),
                    "short_positive_rate": float((valid_short > 0).mean()) if not valid_short.empty else math.nan,
                    "short_mean_edge": valid_short.mean(),
                },
            )
    return pd.DataFrame(rows)


def write_report(
    path: Path,
    summary: RunSummary,
    config: dict[str, Any],
    health: Any,
    factor_summary: pd.DataFrame,
    quantile_returns: pd.DataFrame,
    fee_sensitivity: pd.DataFrame,
    fee_resolution: FeeResolution,
) -> None:
    input_config = config.get("input", {})
    horizons = horizons_from_config(config)
    lines: list[str] = [
        "# PMXT L2 factor baseline report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "## Scope",
        "",
        "Research-only PMXT L2 factor panel; this is not a Nautilus strategy or backtest.",
        "The script loads canonical replay steps through `PMXTEventV1Adapter`, validates them with `analyze_dataset_health`, and reconstructs selected-token book state in receive-time order.",
        "",
        "## Input",
        "",
        f"- event_dir: `{input_config.get('event_dir')}`",
        f"- condition_id: `{input_config.get('condition_id')}`",
        f"- asset_id/token_id: `{input_config.get('asset_id')}`",
        f"- horizons_seconds: `{horizons}`",
        f"- taker_fee used for configured edge labels: `{fee_resolution.taker_fee}`",
        f"- taker_fee source: `{fee_resolution.source}`",
    ]
    if fee_resolution.source.startswith("fallback_zero"):
        lines.append("- fee warning: `taker_fee=0` is a fallback because neither config nor market metadata supplied a fee.")
    lines.extend([
        "",
        "## Run summary",
        "",
        f"- loaded replay steps: {summary.rows_loaded}",
        f"- factor rows: {summary.factor_rows}",
        f"- receive-time span: {summary.first_timestamp_received} to {summary.last_timestamp_received}",
        f"- data health ok: {summary.health_ok}",
        f"- data health warnings/errors: {summary.health_warning_count}/{summary.health_error_count}",
        f"- factor panel: `{Path(summary.panel_path).name}` ({summary.panel_format})",
        "",
        "## Output files",
        "",
        f"- `{Path(summary.panel_path).name}`",
        "- `factor_summary.csv`",
        "- `quantile_returns.csv`",
        "- `fee_sensitivity.csv`",
        "- `data_health_summary.json`",
        "- `run_summary.json`",
        "- `report.md`",
        "",
        "## Factor construction notes",
        "",
        "- `book` snapshots reset the selected token book.",
        "- `price_change` updates or removes levels by side/price/size.",
        "- `trade` contributes signed 30-second trade pressure but does not mutate the book.",
        "- `tick_size_change` records the latest tick-size regime.",
        "- Labels use forward as-of rows at each target receive timestamp; when multiple rows share the same receive timestamp, the label uses the last replay state at that timestamp.",
        "- Factors use only current/past replay state.",
        "",
        "## Caveats",
        "",
        "- The PMXT sample is receive-time ordered by the adapter; source-time inversions remain data-health diagnostics.",
        "- Trade aggressor-side semantics are inherited from the adapter-normalized PMXT side field.",
        "- Fee sensitivity is descriptive; it does not simulate queue position, fill probability, or market impact.",
        "- Missing future rows near the end of the sample produce null labels for affected horizons.",
        "",
        "## Selected summary metrics",
        "",
    ])
    dataframe_to_markdown(factor_summary.head(24), lines)
    lines.extend(["", "## Quantile return preview", ""])
    dataframe_to_markdown(quantile_returns.head(30), lines)
    lines.extend(["", "## Fee sensitivity", ""])
    dataframe_to_markdown(fee_sensitivity, lines)
    lines.extend(["", "## Data-health issue preview", ""])
    health_report = compact_health_report(health, issue_limit=10)
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
    if pd.isna(value):
        text = ""
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def horizons_from_config(config: dict[str, Any]) -> list[int]:
    labels = config.get("labels", {})
    horizons = labels.get("horizons_seconds", DEFAULT_HORIZONS_SECONDS) if isinstance(labels, dict) else DEFAULT_HORIZONS_SECONDS
    return [int(value) for value in horizons]


def resolve_taker_fee(config: dict[str, Any], dataset: Any | None = None) -> FeeResolution:
    configured = config.get("fees", {}).get("taker_fee") if isinstance(config.get("fees", {}), dict) else None
    if configured is not None:
        return FeeResolution(float(configured), "experiment.yml:fees.taker_fee")

    market_metadata = tuple(getattr(getattr(dataset, "metadata", None), "market_metadata", ()) or ()) if dataset is not None else ()
    asset_id = str(config.get("input", {}).get("asset_id", "")) if isinstance(config.get("input", {}), dict) else ""
    selected_market = next(
        (
            market
            for market in market_metadata
            if str(getattr(market, "token_id", "")) == asset_id and getattr(market, "taker_fee", None) is not None
        ),
        None,
    )
    if selected_market is not None:
        source = getattr(selected_market, "fee_source", None) or "dataset.metadata.market_metadata.taker_fee"
        return FeeResolution(float(selected_market.taker_fee), source)

    return FeeResolution(0.0, "fallback_zero_no_config_or_market_metadata")


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
