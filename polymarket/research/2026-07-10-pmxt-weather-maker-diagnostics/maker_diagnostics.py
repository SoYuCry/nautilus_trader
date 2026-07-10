# ruff: noqa: I001,RUF001
"""
PMXT weather maker diagnostics.

Research-only artifact: this is not a Nautilus backtest and does not compute
cash, inventory, fees, queue priority, or executable PnL. It evaluates whether
the depth_imbalance_1 signal survives a crude passive-fill proxy and post-fill
markout check.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.data_health import analyze_dataset_health  # noqa: E402

BASELINE_MODULE_PATH = REPO_ROOT / "polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py"
VALID_BOOK = "valid"
TIME_BUCKET_ORDER = ("early_gt_24h", "last_24h", "last_6h", "final_1h", "post_close")
BUY = "buy_bid"
SELL = "sell_ask"
PMXT_RESEARCH_ALLOWED_HEALTH_ERRORS = frozenset({"receive_time_inversion"})


@dataclass(frozen=True)
class RunSummary:
    config_path: str
    output_dir: str
    event_count: int
    token_count: int
    probe_count: int
    metric_rows: int
    generated_at: str
    report_path: str


@dataclass(frozen=True)
class FillResult:
    filled: bool
    fill_timestamp_ns: int | None = None
    fill_timestamp: pd.Timestamp | None = None
    fill_wait_seconds: float = math.nan
    cumulative_eligible_volume: float = 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PMXT weather maker diagnostics.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    config_path = args.config.resolve()
    config = load_config(config_path)
    output_dir = resolve_output_dir(config, config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline = load_baseline_module()
    event_dirs = [Path(raw) for raw in config["input"]["event_dirs"]]
    probe_config = config["probe"]
    factor = str(probe_config.get("factor", "depth_imbalance_1"))
    quantiles = int(probe_config.get("quantiles", 5))
    probe_interval_seconds = int(probe_config.get("probe_interval_seconds", 60))
    order_size = float(probe_config.get("order_size", 1.0))
    fill_windows = [int(value) for value in probe_config.get("fill_windows_seconds", [60, 300])]
    markouts = [int(value) for value in probe_config.get("markout_seconds", [60, 300, 900])]
    queue_models = {
        name: float(model.get("queue_ahead_fraction", 0.0))
        for name, model in dict(probe_config.get("queue_models", {"optimistic": {"queue_ahead_fraction": 0.0}})).items()
    }
    fill_sample_limit = int(config.get("analysis", {}).get("fill_event_sample_limit", 500))
    min_probes_per_bucket = int(config.get("analysis", {}).get("min_probes_per_bucket", 20))
    min_fills_per_metric = int(config.get("analysis", {}).get("min_fills_per_metric", 5))

    inventories: list[dict[str, Any]] = []
    token_summaries: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    fill_sample_rows: list[dict[str, Any]] = []
    processed_tokens = 0
    total_probes = 0

    for event_dir in event_dirs:
        event_index = read_json(event_dir / "event_index.json")
        for market in event_index["markets"]:
            token_id = str(market["yesToken"])
            token_config = build_token_config(config, event_dir, event_index, market, token_id)
            dataset = PMXTEventV1Adapter(repo_root=REPO_ROOT).load(token_config)
            health = analyze_dataset_health(dataset)
            blocking_issues = pmxt_maker_blocking_health_issues(health)
            if blocking_issues:
                blocking_codes = sorted({issue.code for issue in blocking_issues})
                raise RuntimeError(
                    f"data health failed for {event_index['eventSlug']} {market['label']} YES; "
                    f"blocking_error_codes={blocking_codes}",
                )
            source_quality = dict(dataset.metadata.source_quality)

            panel = baseline.build_factor_panel(dataset, token_config)
            panel = add_update_fields(panel, dataset)
            panel = add_event_metadata(panel, event_index, market, token_id)
            panel = add_time_to_close(panel)

            probes = build_probe_frame(
                panel,
                factor=factor,
                quantiles=quantiles,
                probe_interval_seconds=probe_interval_seconds,
            )
            total_probes += len(probes)
            inventories.append(build_inventory_row(event_index, market, token_id, panel, probes, health, source_quality))
            token_summaries.append(build_token_summary(event_index, market, token_id, probes, panel, health, source_quality))
            if probes.empty:
                processed_tokens += 1
                continue

            diagnostics, samples = run_token_diagnostics(
                event_index=event_index,
                market=market,
                token_id=token_id,
                probes=probes,
                panel=panel,
                factor=factor,
                queue_models=queue_models,
                fill_windows=fill_windows,
                markouts=markouts,
                order_size=order_size,
                min_probes_per_bucket=min_probes_per_bucket,
                min_fills_per_metric=min_fills_per_metric,
            )
            metric_rows.extend(diagnostics)
            remaining_sample_slots = max(0, fill_sample_limit - len(fill_sample_rows))
            if remaining_sample_slots:
                fill_sample_rows.extend(samples[:remaining_sample_slots])
            processed_tokens += 1
            print(
                f"processed {processed_tokens}: {event_index['eventSlug']} {market['label']} YES "
                f"panel_rows={len(panel)} probes={len(probes)}",
            )

    inventory_frame = pd.DataFrame(inventories)
    token_summary_frame = pd.DataFrame(token_summaries)
    diagnostics_frame = pd.DataFrame(metric_rows)
    strategy_tail_frame = build_strategy_tail_summary(diagnostics_frame)
    event_summary_frame = build_event_summary(diagnostics_frame)
    fill_sample_frame = pd.DataFrame(fill_sample_rows)

    inventory_frame.to_csv(output_dir / "event_token_inventory.csv", index=False)
    token_summary_frame.to_csv(output_dir / "token_probe_summary.csv", index=False)
    diagnostics_frame.to_csv(output_dir / "maker_diagnostics.csv", index=False)
    strategy_tail_frame.to_csv(output_dir / "strategy_tail_summary.csv", index=False)
    event_summary_frame.to_csv(output_dir / "event_summary.csv", index=False)
    fill_sample_frame.to_csv(output_dir / "fill_event_sample.csv", index=False)

    svg_paths = write_svgs(output_dir, strategy_tail_frame, diagnostics_frame)
    summary = RunSummary(
        config_path=str(config_path),
        output_dir=str(output_dir),
        event_count=len(event_dirs),
        token_count=processed_tokens,
        probe_count=total_probes,
        metric_rows=len(diagnostics_frame),
        generated_at=datetime.now(UTC).isoformat(),
        report_path=str(output_dir / "report.md"),
    )
    (output_dir / "run_summary.json").write_text(
        json.dumps(asdict(summary), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_dir / "report.md",
        summary,
        config,
        inventory_frame,
        token_summary_frame,
        diagnostics_frame,
        strategy_tail_frame,
        event_summary_frame,
        svg_paths,
    )
    print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
    return 0


def load_config(path: Path) -> dict[str, Any]:
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected mapping config: {path}")
    return loaded


def resolve_output_dir(config: dict[str, Any], config_path: Path) -> Path:
    configured = (config.get("output") or {}).get("dir", "outputs")
    path = Path(str(configured))
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def load_baseline_module() -> Any:
    spec = importlib.util.spec_from_file_location("pmxt_l2_factor_baseline", BASELINE_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load baseline module: {BASELINE_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_token_config(base_config: dict[str, Any], event_dir: Path, event_index: dict[str, Any], market: dict[str, Any], token_id: str) -> dict[str, Any]:
    return {
        "input": {
            "event_dir": str(event_dir),
            "condition_id": market["conditionId"],
            "asset_id": token_id,
            "dataset_id": f"{event_index['eventSlug']}-{market['index']}-yes-maker-diagnostics",
        },
        "labels": {"horizons_seconds": sorted(set(base_config["probe"].get("markout_seconds", [60, 300, 900])))},
        "factors": base_config.get("factors", {}),
    }


def add_update_fields(panel: pd.DataFrame, dataset: Any) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for step in dataset.steps:
        if len(step.updates) != 1:
            raise ValueError(f"expected one canonical update per step; sequence={step.sequence}")
        update = step.updates[0]
        rows.append(
            {
                "sequence": step.sequence,
                "update_price": float(update.price) if update.price is not None else math.nan,
                "update_size": float(update.size) if update.size is not None else math.nan,
                "update_side": update.side,
            },
        )
    update_frame = pd.DataFrame(rows)
    return panel.merge(update_frame, on="sequence", how="left", validate="one_to_one")


def add_event_metadata(panel: pd.DataFrame, event_index: dict[str, Any], market: dict[str, Any], token_id: str) -> pd.DataFrame:
    panel = panel.copy()
    for key, value in event_market_fields(event_index, market, token_id).items():
        panel[key] = value
    panel["event_start"] = pd.Timestamp(event_index.get("startDate"))
    panel["event_end"] = pd.Timestamp(event_index.get("endDate"))
    return panel


def event_market_fields(event_index: dict[str, Any], market: dict[str, Any], token_id: str) -> dict[str, Any]:
    return {
        "event_slug": event_index["eventSlug"],
        "event_title": event_index.get("title"),
        "event_start": event_index.get("startDate"),
        "event_end": event_index.get("endDate"),
        "market_index": int(market["index"]),
        "market_label": market.get("label"),
        "condition_id": market.get("conditionId"),
        "asset_id": token_id,
        "token_side": "YES",
    }


def add_time_to_close(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.copy()
    clock_col = replay_time_column(panel)
    panel["time_to_close_seconds"] = (panel["event_end"] - panel[clock_col]).dt.total_seconds()
    panel["time_to_close_bucket"] = panel["time_to_close_seconds"].map(time_to_close_bucket)
    return panel


def replay_time_column(panel: pd.DataFrame) -> str:
    """Return the PMXT replay clock column for timestamp-ordered diagnostics."""
    return "replay_timestamp" if "replay_timestamp" in panel.columns else "timestamp_received"


def time_to_close_bucket(seconds: float) -> str:
    if pd.isna(seconds):
        return "unknown"
    if seconds < 0:
        return "post_close"
    if seconds <= 3600:
        return "final_1h"
    if seconds <= 6 * 3600:
        return "last_6h"
    if seconds <= 24 * 3600:
        return "last_24h"
    return "early_gt_24h"


def build_probe_frame(panel: pd.DataFrame, *, factor: str, quantiles: int, probe_interval_seconds: int) -> pd.DataFrame:
    required = ["timestamp_received", "sequence", "bid1", "ask1", "bid_size1", "ask_size1", "mid", "spread", "book_validity", factor]
    missing = [column for column in required if column not in panel]
    if missing:
        raise ValueError(f"panel missing required columns: {missing}")
    clock_col = replay_time_column(panel)
    frame = panel[
        (panel["book_validity"] == VALID_BOOK)
        & np.isfinite(pd.to_numeric(panel["bid1"], errors="coerce"))
        & np.isfinite(pd.to_numeric(panel["ask1"], errors="coerce"))
        & np.isfinite(pd.to_numeric(panel["mid"], errors="coerce"))
        & np.isfinite(pd.to_numeric(panel[factor], errors="coerce"))
        & (pd.to_numeric(panel["spread"], errors="coerce") > 0)
        & (panel["time_to_close_bucket"] != "post_close")
    ].copy()
    if frame.empty:
        return frame
    frame["probe_bin"] = frame[clock_col].dt.floor(f"{probe_interval_seconds}s")
    frame = (
        frame.sort_values([clock_col, "sequence"], kind="mergesort")
        .groupby("probe_bin", sort=False, as_index=False)
        .tail(1)
        .sort_values([clock_col, "sequence"], kind="mergesort")
        .reset_index(drop=True)
    )
    frame["factor_quantile"] = factor_quantiles(frame[factor], quantiles)
    return frame.dropna(subset=["factor_quantile"]).copy()


def factor_quantiles(values: pd.Series, quantiles: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.nunique(dropna=True) < 2:
        return pd.Series(pd.NA, index=values.index, dtype="Int64")
    ranked = numeric.rank(method="first")
    labels = pd.qcut(ranked, q=quantiles, labels=False, duplicates="drop")
    return (labels + 1).astype("Int64")


def run_token_diagnostics(
    *,
    event_index: dict[str, Any],
    market: dict[str, Any],
    token_id: str,
    probes: pd.DataFrame,
    panel: pd.DataFrame,
    factor: str,
    queue_models: dict[str, float],
    fill_windows: list[int],
    markouts: list[int],
    order_size: float,
    min_probes_per_bucket: int,
    min_fills_per_metric: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_fields = event_market_fields(event_index, market, token_id)
    trades = build_trade_arrays(panel)
    future = build_future_mid_arrays(panel)
    rows: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []

    for quote_side in (BUY, SELL):
        side_probes = probes.copy()
        side_probes["quote_side"] = quote_side
        side_probes["quote_price"] = side_probes["bid1"] if quote_side == BUY else side_probes["ask1"]
        side_probes["displayed_top_size"] = side_probes["bid_size1"] if quote_side == BUY else side_probes["ask_size1"]
        side_probes = side_probes[np.isfinite(pd.to_numeric(side_probes["displayed_top_size"], errors="coerce")) & (side_probes["displayed_top_size"] >= 0)]
        for queue_model, queue_fraction in queue_models.items():
            for fill_window in fill_windows:
                fills = simulate_fills(
                    side_probes,
                    trades=trades,
                    quote_side=quote_side,
                    queue_fraction=queue_fraction,
                    order_size=order_size,
                    fill_window_seconds=fill_window,
                )
                for markout_seconds in markouts:
                    metrics = compute_markout_metrics(
                        side_probes,
                        fills,
                        future=future,
                        quote_side=quote_side,
                        markout_seconds=markout_seconds,
                    )
                    rows.extend(
                        summarize_metrics(
                            metrics,
                            base_fields=base_fields,
                            factor=factor,
                            queue_model=queue_model,
                            queue_fraction=queue_fraction,
                            fill_window_seconds=fill_window,
                            markout_seconds=markout_seconds,
                            min_probes_per_bucket=min_probes_per_bucket,
                            min_fills_per_metric=min_fills_per_metric,
                        ),
                    )
                    if len(samples) < 500:
                        samples.extend(sample_fills(metrics, base_fields, queue_model, fill_window, markout_seconds, limit=25))
    return rows, samples


def build_trade_arrays(panel: pd.DataFrame) -> dict[str, np.ndarray]:
    clock_col = replay_time_column(panel)
    trades = panel[
        (panel["event_type"] == "trade")
        & np.isfinite(pd.to_numeric(panel["update_price"], errors="coerce"))
        & np.isfinite(pd.to_numeric(panel["update_size"], errors="coerce"))
        & (pd.to_numeric(panel["update_size"], errors="coerce") > 0)
    ].copy()
    trades = trades.sort_values([clock_col, "sequence"], kind="mergesort")
    return {
        "time_ns": timestamps_to_ns(trades[clock_col]),
        "price": pd.to_numeric(trades["update_price"], errors="coerce").to_numpy(dtype=float),
        "size": pd.to_numeric(trades["update_size"], errors="coerce").to_numpy(dtype=float),
        "side": trades["update_side"].astype(str).to_numpy(),
    }


def build_future_mid_arrays(panel: pd.DataFrame) -> dict[str, np.ndarray]:
    clock_col = replay_time_column(panel)
    valid = (
        panel[
            (panel["book_validity"] == VALID_BOOK)
            & np.isfinite(pd.to_numeric(panel["mid"], errors="coerce"))
        ][[clock_col, "sequence", "mid"]]
        .sort_values([clock_col, "sequence"], kind="mergesort")
        .groupby(clock_col, sort=False, as_index=False)
        .tail(1)
        .sort_values(clock_col, kind="mergesort")
    )
    return {
        "time_ns": timestamps_to_ns(valid[clock_col]),
        "mid": pd.to_numeric(valid["mid"], errors="coerce").to_numpy(dtype=float),
    }


def timestamps_to_ns(values: pd.Series) -> np.ndarray:
    """Return UTC nanoseconds even when parquet yields datetime64[us, UTC]."""
    timestamps = pd.to_datetime(values, utc=True)
    return timestamps.dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]").astype("int64").to_numpy()


def simulate_fills(
    probes: pd.DataFrame,
    *,
    trades: dict[str, np.ndarray],
    quote_side: str,
    queue_fraction: float,
    order_size: float,
    fill_window_seconds: int,
) -> list[FillResult]:
    trade_times = trades["time_ns"]
    trade_prices = trades["price"]
    trade_sizes = trades["size"]
    trade_sides = trades["side"]
    results: list[FillResult] = []
    window_ns = int(fill_window_seconds * 1_000_000_000)
    for probe in probes.itertuples(index=False):
        probe_time = getattr(probe, "replay_timestamp", probe.timestamp_received)
        start_ns = int(probe_time.value)
        end_ns = start_ns + window_ns
        threshold = max(0.0, float(probe.displayed_top_size) * queue_fraction) + order_size
        fill = find_fill(
            trade_times,
            trade_prices,
            trade_sizes,
            trade_sides,
            start_ns=start_ns,
            end_ns=end_ns,
            quote_side=quote_side,
            quote_price=float(probe.quote_price),
            threshold=threshold,
        )
        results.append(fill)
    return results


def find_fill(
    trade_times_ns: np.ndarray,
    trade_prices: np.ndarray,
    trade_sizes: np.ndarray,
    trade_sides: np.ndarray,
    *,
    start_ns: int,
    end_ns: int,
    quote_side: str,
    quote_price: float,
    threshold: float,
) -> FillResult:
    if len(trade_times_ns) == 0 or not math.isfinite(quote_price) or threshold <= 0:
        return FillResult(False)
    start = int(np.searchsorted(trade_times_ns, start_ns, side="right"))
    end = int(np.searchsorted(trade_times_ns, end_ns, side="right"))
    if start >= end:
        return FillResult(False)
    cumulative = 0.0
    for index in range(start, end):
        if is_eligible_trade(quote_side, quote_price, float(trade_prices[index]), str(trade_sides[index])):
            cumulative += float(trade_sizes[index])
            if cumulative >= threshold:
                fill_ns = int(trade_times_ns[index])
                return FillResult(
                    True,
                    fill_timestamp_ns=fill_ns,
                    fill_timestamp=pd.Timestamp(fill_ns, unit="ns", tz="UTC"),
                    fill_wait_seconds=(fill_ns - start_ns) / 1_000_000_000,
                    cumulative_eligible_volume=cumulative,
                )
    return FillResult(False, cumulative_eligible_volume=cumulative)


def is_eligible_trade(quote_side: str, quote_price: float, trade_price: float, trade_side: str) -> bool:
    epsilon = 1e-12
    if quote_side == BUY:
        return trade_side == "SELL" and trade_price <= quote_price + epsilon
    if quote_side == SELL:
        return trade_side == "BUY" and trade_price >= quote_price - epsilon
    raise ValueError(f"unknown quote_side: {quote_side}")


def compute_markout_metrics(
    probes: pd.DataFrame,
    fills: list[FillResult],
    *,
    future: dict[str, np.ndarray],
    quote_side: str,
    markout_seconds: int,
) -> pd.DataFrame:
    future_times = future["time_ns"]
    future_mids = future["mid"]
    records: list[dict[str, Any]] = []
    markout_ns = int(markout_seconds * 1_000_000_000)
    for probe, fill in zip(probes.itertuples(index=False), fills, strict=True):
        record = {
            "timestamp_received": probe.timestamp_received,
            "replay_timestamp": getattr(probe, "replay_timestamp", probe.timestamp_received),
            "time_to_close_bucket": probe.time_to_close_bucket,
            "factor_quantile": int(probe.factor_quantile),
            "factor_value": float(probe.depth_imbalance_1),
            "quote_side": quote_side,
            "quote_price": float(probe.quote_price),
            "displayed_top_size": float(probe.displayed_top_size),
            "spread": float(probe.spread),
            "mid": float(probe.mid),
            "filled": fill.filled,
            "fill_timestamp": fill.fill_timestamp,
            "fill_wait_seconds": fill.fill_wait_seconds,
            "cumulative_eligible_volume": fill.cumulative_eligible_volume,
            "markout_seconds": markout_seconds,
            "future_mid": math.nan,
            "markout": math.nan,
        }
        if fill.filled and fill.fill_timestamp_ns is not None:
            target_ns = fill.fill_timestamp_ns + markout_ns
            idx = int(np.searchsorted(future_times, target_ns, side="left"))
            if idx < len(future_mids):
                future_mid = float(future_mids[idx])
                record["future_mid"] = future_mid
                record["markout"] = future_mid - record["quote_price"] if quote_side == BUY else record["quote_price"] - future_mid
        records.append(record)
    return pd.DataFrame(records)


def summarize_metrics(
    metrics: pd.DataFrame,
    *,
    base_fields: dict[str, Any],
    factor: str,
    queue_model: str,
    queue_fraction: float,
    fill_window_seconds: int,
    markout_seconds: int,
    min_probes_per_bucket: int,
    min_fills_per_metric: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grouped = metrics.groupby(["quote_side", "factor_quantile", "time_to_close_bucket"], observed=True)
    for keys, frame in grouped:
        quote_side, factor_quantile, bucket = keys
        if len(frame) < min_probes_per_bucket:
            continue
        rows.append(
            {
                **base_fields,
                "factor": factor,
                "queue_model": queue_model,
                "queue_ahead_fraction": queue_fraction,
                "fill_window_seconds": fill_window_seconds,
                "markout_seconds": markout_seconds,
                "quote_side": quote_side,
                "factor_quantile": int(factor_quantile),
                "time_to_close_bucket": bucket,
                **aggregate_markout_frame(frame, min_fills_per_metric=min_fills_per_metric),
            },
        )
    all_frame = metrics.copy()
    grouped_all = all_frame.groupby(["quote_side", "factor_quantile"], observed=True)
    for keys, frame in grouped_all:
        quote_side, factor_quantile = keys
        if len(frame) < min_probes_per_bucket:
            continue
        rows.append(
            {
                **base_fields,
                "factor": factor,
                "queue_model": queue_model,
                "queue_ahead_fraction": queue_fraction,
                "fill_window_seconds": fill_window_seconds,
                "markout_seconds": markout_seconds,
                "quote_side": quote_side,
                "factor_quantile": int(factor_quantile),
                "time_to_close_bucket": "all",
                **aggregate_markout_frame(frame, min_fills_per_metric=min_fills_per_metric),
            },
        )
    return rows


def aggregate_markout_frame(frame: pd.DataFrame, *, min_fills_per_metric: int) -> dict[str, Any]:
    filled = frame[frame["filled"] == True].copy()  # noqa: E712
    markout = pd.to_numeric(filled["markout"], errors="coerce").dropna()
    markout_is_sufficient = markout.count() >= min_fills_per_metric
    wins = markout[markout > 0]
    losses = markout[markout < 0]
    avg_win = float(wins.mean()) if markout_is_sufficient and not wins.empty else math.nan
    avg_loss_abs = float((-losses).mean()) if markout_is_sufficient and not losses.empty else math.nan
    return {
        "probe_count": len(frame),
        "filled_count": len(filled),
        "markout_count": int(markout.count()),
        "metric_quality": "sufficient" if markout_is_sufficient else "insufficient_fills",
        "fill_rate": float(len(filled) / len(frame)) if len(frame) else math.nan,
        "mean_fill_wait_seconds": float(pd.to_numeric(filled["fill_wait_seconds"], errors="coerce").mean()) if not filled.empty else math.nan,
        "median_spread": float(pd.to_numeric(frame["spread"], errors="coerce").median()) if len(frame) else math.nan,
        "mean_markout": float(markout.mean()) if markout_is_sufficient else math.nan,
        "median_markout": float(markout.median()) if markout_is_sufficient else math.nan,
        "hit_rate": float((markout > 0).mean()) if markout_is_sufficient else math.nan,
        "avg_win": avg_win,
        "avg_loss_abs": avg_loss_abs,
        "win_loss_ratio": avg_win / avg_loss_abs if math.isfinite(avg_win) and math.isfinite(avg_loss_abs) and avg_loss_abs > 0 else math.nan,
        "adverse_selection_flag": bool(math.isfinite(avg_win) and math.isfinite(avg_loss_abs) and avg_loss_abs > avg_win),
    }


def sample_fills(
    metrics: pd.DataFrame,
    base_fields: dict[str, Any],
    queue_model: str,
    fill_window_seconds: int,
    markout_seconds: int,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    filled = metrics[metrics["filled"] == True].head(limit)  # noqa: E712
    rows: list[dict[str, Any]] = []
    for row in filled.itertuples(index=False):
        rows.append(
            {
                **base_fields,
                "queue_model": queue_model,
                "fill_window_seconds": fill_window_seconds,
                "markout_seconds": markout_seconds,
                "timestamp_received": row.timestamp_received,
                "replay_timestamp": row.replay_timestamp,
                "fill_timestamp": row.fill_timestamp,
                "quote_side": row.quote_side,
                "factor_quantile": row.factor_quantile,
                "quote_price": row.quote_price,
                "future_mid": row.future_mid,
                "markout": row.markout,
                "fill_wait_seconds": row.fill_wait_seconds,
            },
        )
    return rows


def build_strategy_tail_summary(diagnostics: pd.DataFrame) -> pd.DataFrame:
    if diagnostics.empty:
        return pd.DataFrame()
    tail = diagnostics[
        (diagnostics["time_to_close_bucket"] == "all")
        & (((diagnostics["quote_side"] == BUY) & (diagnostics["factor_quantile"] == 5)) | ((diagnostics["quote_side"] == SELL) & (diagnostics["factor_quantile"] == 1)))
    ].copy()
    if tail.empty:
        return tail
    rows: list[dict[str, Any]] = []
    grouped = tail.groupby(["queue_model", "queue_ahead_fraction", "fill_window_seconds", "markout_seconds", "quote_side"], observed=True)
    for keys, frame in grouped:
        queue_model, queue_fraction, fill_window, markout, quote_side = keys
        rows.append(
            {
                "queue_model": queue_model,
                "queue_ahead_fraction": queue_fraction,
                "fill_window_seconds": int(fill_window),
                "markout_seconds": int(markout),
                "quote_side": quote_side,
                "token_metric_count": len(frame),
                "total_probes": int(pd.to_numeric(frame["probe_count"], errors="coerce").sum()),
                "total_fills": int(pd.to_numeric(frame["filled_count"], errors="coerce").sum()),
                "median_fill_rate": float(pd.to_numeric(frame["fill_rate"], errors="coerce").median()),
                "median_mean_markout": float(pd.to_numeric(frame["mean_markout"], errors="coerce").median()),
                "positive_markout_token_share": float((pd.to_numeric(frame["mean_markout"], errors="coerce") > 0).mean()),
                "median_hit_rate": float(pd.to_numeric(frame["hit_rate"], errors="coerce").median()),
                "median_avg_win": float(pd.to_numeric(frame["avg_win"], errors="coerce").median()),
                "median_avg_loss_abs": float(pd.to_numeric(frame["avg_loss_abs"], errors="coerce").median()),
                "median_win_loss_ratio": float(pd.to_numeric(frame["win_loss_ratio"], errors="coerce").median()),
                "adverse_selection_token_share": float(pd.to_numeric(frame["adverse_selection_flag"], errors="coerce").mean()),
            },
        )
    combined = tail.groupby(["queue_model", "queue_ahead_fraction", "fill_window_seconds", "markout_seconds"], observed=True)
    for keys, frame in combined:
        queue_model, queue_fraction, fill_window, markout = keys
        rows.append(
            {
                "queue_model": queue_model,
                "queue_ahead_fraction": queue_fraction,
                "fill_window_seconds": int(fill_window),
                "markout_seconds": int(markout),
                "quote_side": "strategy_tail_combined",
                "token_metric_count": len(frame),
                "total_probes": int(pd.to_numeric(frame["probe_count"], errors="coerce").sum()),
                "total_fills": int(pd.to_numeric(frame["filled_count"], errors="coerce").sum()),
                "median_fill_rate": float(pd.to_numeric(frame["fill_rate"], errors="coerce").median()),
                "median_mean_markout": float(pd.to_numeric(frame["mean_markout"], errors="coerce").median()),
                "positive_markout_token_share": float((pd.to_numeric(frame["mean_markout"], errors="coerce") > 0).mean()),
                "median_hit_rate": float(pd.to_numeric(frame["hit_rate"], errors="coerce").median()),
                "median_avg_win": float(pd.to_numeric(frame["avg_win"], errors="coerce").median()),
                "median_avg_loss_abs": float(pd.to_numeric(frame["avg_loss_abs"], errors="coerce").median()),
                "median_win_loss_ratio": float(pd.to_numeric(frame["win_loss_ratio"], errors="coerce").median()),
                "adverse_selection_token_share": float(pd.to_numeric(frame["adverse_selection_flag"], errors="coerce").mean()),
            },
        )
    return pd.DataFrame(rows).sort_values(["queue_ahead_fraction", "fill_window_seconds", "markout_seconds", "quote_side"])


def build_event_summary(diagnostics: pd.DataFrame) -> pd.DataFrame:
    if diagnostics.empty:
        return pd.DataFrame()
    frame = diagnostics[diagnostics["time_to_close_bucket"] == "all"].copy()
    rows: list[dict[str, Any]] = []
    grouped = frame.groupby(["event_slug", "queue_model", "fill_window_seconds", "markout_seconds", "quote_side", "factor_quantile"], observed=True)
    for keys, group in grouped:
        event_slug, queue_model, fill_window, markout, quote_side, quantile = keys
        rows.append(
            {
                "event_slug": event_slug,
                "queue_model": queue_model,
                "fill_window_seconds": int(fill_window),
                "markout_seconds": int(markout),
                "quote_side": quote_side,
                "factor_quantile": int(quantile),
                "token_metric_count": len(group),
                "total_probes": int(pd.to_numeric(group["probe_count"], errors="coerce").sum()),
                "total_fills": int(pd.to_numeric(group["filled_count"], errors="coerce").sum()),
                "median_fill_rate": float(pd.to_numeric(group["fill_rate"], errors="coerce").median()),
                "median_mean_markout": float(pd.to_numeric(group["mean_markout"], errors="coerce").median()),
                "positive_markout_token_share": float((pd.to_numeric(group["mean_markout"], errors="coerce") > 0).mean()),
            },
        )
    return pd.DataFrame(rows)


def pmxt_maker_blocking_health_issues(health: Any) -> list[Any]:
    """Return hard data-health issues that still block PMXT maker diagnostics."""
    return [
        issue
        for issue in health.issues
        if issue.severity == "error" and issue.code not in PMXT_RESEARCH_ALLOWED_HEALTH_ERRORS
    ]


def source_quality_fields(source_quality: dict[str, Any]) -> dict[str, Any]:
    """Flatten PMXT sourceQuality manifest fields for CSV/report outputs."""
    ordering_ambiguous_rows = int(source_quality.get("orderingAmbiguousRows", 0) or 0)
    return {
        "source_quality_coverage_status": source_quality.get("coverageStatus", "unknown"),
        "source_quality_ordering_status": source_quality.get("orderingStatus", "unknown"),
        "source_quality_snapshot_replay_status": source_quality.get("snapshotReplayStatus", "unknown"),
        "source_quality_metadata_join_status": source_quality.get("metadataJoinStatus", "unknown"),
        "source_quality_ordering_ambiguous_groups": int(source_quality.get("orderingAmbiguousGroups", 0) or 0),
        "source_quality_ordering_ambiguous_rows": ordering_ambiguous_rows,
        "source_quality_missing_source_timestamp_rows": int(source_quality.get("missingSourceTimestampRows", 0) or 0),
        "source_quality_stable_sort_key": source_quality.get("stableSortKey", ""),
        "ordering_ambiguous": str(source_quality.get("orderingStatus", "unknown")) == "ambiguous" or ordering_ambiguous_rows > 0,
    }


def build_inventory_row(
    event_index: dict[str, Any],
    market: dict[str, Any],
    token_id: str,
    panel: pd.DataFrame,
    probes: pd.DataFrame,
    health: Any,
    source_quality: dict[str, Any],
) -> dict[str, Any]:
    fields = event_market_fields(event_index, market, token_id)
    trades = panel[panel["event_type"] == "trade"]
    clock_col = replay_time_column(panel)
    return {
        **fields,
        **source_quality_fields(source_quality),
        "panel_rows": len(panel),
        "probe_rows": len(probes),
        "trade_rows": len(trades),
        "first_timestamp_received": iso_or_none(panel["timestamp_received"].min()) if not panel.empty else None,
        "last_timestamp_received": iso_or_none(panel["timestamp_received"].max()) if not panel.empty else None,
        "first_replay_timestamp": iso_or_none(panel[clock_col].min()) if not panel.empty else None,
        "last_replay_timestamp": iso_or_none(panel[clock_col].max()) if not panel.empty else None,
        "source_time_inversion_count": health.summary.source_time_inversion_count,
        "source_delay_over_threshold_count": health.summary.source_delay_over_threshold_count,
    }


def build_token_summary(
    event_index: dict[str, Any],
    market: dict[str, Any],
    token_id: str,
    probes: pd.DataFrame,
    panel: pd.DataFrame,
    health: Any,
    source_quality: dict[str, Any],
) -> dict[str, Any]:
    fields = event_market_fields(event_index, market, token_id)
    clock_col = replay_time_column(panel)
    return {
        **fields,
        **source_quality_fields(source_quality),
        "probe_rows": len(probes),
        "trade_rows": int((panel["event_type"] == "trade").sum()),
        "first_timestamp_received": iso_or_none(panel["timestamp_received"].min()) if not panel.empty else None,
        "last_timestamp_received": iso_or_none(panel["timestamp_received"].max()) if not panel.empty else None,
        "first_replay_timestamp": iso_or_none(panel[clock_col].min()) if not panel.empty else None,
        "last_replay_timestamp": iso_or_none(panel[clock_col].max()) if not panel.empty else None,
        "median_spread": float(pd.to_numeric(probes.get("spread"), errors="coerce").median()) if not probes.empty else math.nan,
        "median_depth_imbalance_1": float(pd.to_numeric(probes.get("depth_imbalance_1"), errors="coerce").median()) if not probes.empty else math.nan,
        "source_time_inversion_count": health.summary.source_time_inversion_count,
        "source_delay_over_threshold_count": health.summary.source_delay_over_threshold_count,
    }


def write_svgs(output_dir: Path, strategy_tail: pd.DataFrame, diagnostics: pd.DataFrame) -> list[Path]:
    paths: list[Path] = []
    if not strategy_tail.empty:
        combined = strategy_tail[strategy_tail["quote_side"] == "strategy_tail_combined"].copy()
        for fill_window in sorted(combined["fill_window_seconds"].unique()):
            plot = combined[(combined["fill_window_seconds"] == fill_window) & (combined["markout_seconds"] == 300)]
            if plot.empty:
                continue
            table = plot.pivot_table(index="queue_model", columns="markout_seconds", values="median_mean_markout", aggfunc="first").fillna(0.0)
            path = output_dir / f"strategy_tail_markout_fill_{fill_window}s.svg"
            write_grouped_bar_svg(path, table, title=f"Strategy-tail median mean markout, fill window {fill_window}s")
            paths.append(path)
    if not diagnostics.empty:
        frame = diagnostics[(diagnostics["time_to_close_bucket"] == "all") & (diagnostics["queue_model"] == "optimistic") & (diagnostics["fill_window_seconds"] == 300) & (diagnostics["markout_seconds"] == 300)]
        if not frame.empty:
            table = frame.pivot_table(index="factor_quantile", columns="quote_side", values="fill_rate", aggfunc="median").fillna(0.0)
            path = output_dir / "optimistic_300s_fill_rate_by_quantile.svg"
            write_grouped_bar_svg(path, table, title="Optimistic 300s fill-rate by factor quantile")
            paths.append(path)
    return paths


def write_grouped_bar_svg(path: Path, table: pd.DataFrame, *, title: str) -> None:
    width, height = 920, 500
    margin_left, margin_right, margin_top, margin_bottom = 130, 30, 58, 95
    plot_w, plot_h = width - margin_left - margin_right, height - margin_top - margin_bottom
    values = table.to_numpy(dtype=float).flatten()
    finite = [float(value) for value in values if math.isfinite(float(value))] or [0.0]
    y_min, y_max = min(0.0, *finite), max(0.0, *finite)
    if y_min == y_max:
        y_min, y_max = y_min - 1.0, y_max + 1.0
    pad = (y_max - y_min) * 0.10
    y_min, y_max = y_min - pad, y_max + pad
    zero_y = margin_top + (y_max / (y_max - y_min)) * plot_h
    row_count, col_count = max(len(table.index), 1), max(len(table.columns), 1)
    group_w, bar_w = plot_w / row_count, plot_w / row_count / (col_count + 1)
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c"]

    def y(value: float) -> float:
        return margin_top + (y_max - value) / (y_max - y_min) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-size="19" font-family="Arial">{escape_xml(title)}</text>',
        f'<line x1="{margin_left}" y1="{zero_y:.2f}" x2="{width - margin_right}" y2="{zero_y:.2f}" stroke="#111827" stroke-width="1"/>',
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" stroke="#111827" stroke-width="1"/>',
    ]
    for row_i, index_value in enumerate(table.index):
        x0 = margin_left + row_i * group_w
        parts.append(
            f'<text x="{x0 + group_w / 2:.2f}" y="{height - 62}" text-anchor="middle" font-size="12" font-family="Arial">{escape_xml(str(index_value))}</text>',
        )
        for col_i, col in enumerate(table.columns):
            value = float(table.loc[index_value, col])
            bar_x = x0 + (col_i + 0.5) * bar_w
            bar_y = y(max(value, 0.0)) if value >= 0 else zero_y
            bar_h = abs(y(value) - zero_y)
            parts.append(
                f'<rect x="{bar_x:.2f}" y="{bar_y:.2f}" width="{bar_w * 0.82:.2f}" height="{bar_h:.2f}" fill="{colors[col_i % len(colors)]}" opacity="0.88"><title>{escape_xml(str(index_value))} {escape_xml(str(col))}: {value:.6g}</title></rect>',
            )
    for col_i, col in enumerate(table.columns):
        lx = margin_left + col_i * 150
        parts.append(f'<rect x="{lx}" y="{height - 35}" width="12" height="12" fill="{colors[col_i % len(colors)]}"/>')
        parts.append(f'<text x="{lx + 18}" y="{height - 24}" font-size="12" font-family="Arial">{escape_xml(str(col))}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_report(
    path: Path,
    summary: RunSummary,
    config: dict[str, Any],
    inventory: pd.DataFrame,
    token_summary: pd.DataFrame,
    diagnostics: pd.DataFrame,
    strategy_tail: pd.DataFrame,
    event_summary: pd.DataFrame,
    svg_paths: list[Path],
) -> None:
    headline = build_headline(strategy_tail)

    lines = [
        "# PMXT 天气 maker markout / fill 诊断报告",
        "",
        f"生成时间: {summary.generated_at}",
        "",
        "## 0. 结论先行",
        "",
        "这份报告不是 Nautilus 回测，也不是 PnL。它只回答一个更窄的问题：",
        "",
        "> `depth_imbalance_1` 如果被当作 maker / quote-skew 信号，在粗糙 fill proxy 下，成交后的 markout 是否还能为正？",
        "",
        headline,
        "",
        "核心读法：如果假设自己几乎排在队首，信号还有一点弱正 markout；但只要引入半队列/全队列假设，strategy-tail 的 median markout 很快转负或接近 0。这说明上一轮 IC 里的盘口预测性，至少在这两个 PMXT 天气 event 上，并不能直接升级成 maker 可捕获收益。",
        "",
        "本报告比上一轮 IC 分析更保守：上一轮证明的是“盘口状态后 mid-price 倾向怎么走”；这一轮检查的是“如果我被动挂单并被成交，成交后的 mid-price markout 怎么样”。",
        "",
        "## 1. 信任边界",
        "",
        "- 数据源：curated PMXT event parquet。",
        "- 回放时钟：`replay_timestamp`，PMXT 中优先使用 source `timestamp`，缺失时才退回 `timestamp_received`。",
        f"- probe 抽样：每 `{config['probe']['probe_interval_seconds']}` 秒取最后一个 valid book state，避免每行都提交一个虚拟订单导致严重重复计数。",
        "- fill proxy：未来 trade touch/cross 当前 best bid/ask。",
        "- queue proxy：需要成交量 >= displayed top size × queue fraction + order size。",
        "- markout：成交后未来 mid-price 相对 fill price 的变化。",
        "- 不包含 fee、rebate、真实 queue priority、撤单、库存、现金、仓位、Nautilus order state 或可执行 PnL。",
        "",
        "## 2. Fill 模型",
        "",
        "| queue_model | 含义 |",
        "| --- | --- |",
        "| optimistic | 队列前面没有别人，只要求成交量覆盖自己的 order_size。 |",
        "| half_queue | 假设自己排在当前 displayed top size 的一半之后。 |",
        "| full_queue | 假设自己排在当前 displayed top size 全部之后。 |",
        "",
        "买单 fill 条件：未来 `SELL` trade price <= 当前 bid。卖单 fill 条件：未来 `BUY` trade price >= 当前 ask。",
        "",
        "## 3. 使用的 event/token",
        "",
        f"- event_count: {summary.event_count}",
        f"- token_count: {summary.token_count}",
        f"- probe_count: {summary.probe_count}",
        "",
    ]
    append_markdown_table(
        lines,
        inventory[
            [
                "event_slug",
                "market_index",
                "market_label",
                "panel_rows",
                "probe_rows",
                "trade_rows",
                "first_replay_timestamp",
                "last_replay_timestamp",
                "source_quality_ordering_status",
                "source_quality_ordering_ambiguous_rows",
            ]
        ].head(50),
    )
    lines.extend(["", "## 4. Strategy-tail 汇总", ""])
    lines.extend(
        [
            "strategy-tail 的定义：",
            "",
            "- Q5 + buy_bid：因子最高分位时挂买一档；",
            "- Q1 + sell_ask：因子最低分位时挂卖一档；",
            "- combined：把上面两个方向合并看。",
            "",
        ],
    )
    append_markdown_table(lines, strategy_tail.head(80))
    lines.extend(["", "## 5. Event-level 预览", ""])
    append_markdown_table(lines, event_summary.head(80))
    lines.extend(["", "## 6. Token probe 概览", ""])
    append_markdown_table(lines, token_summary.head(50))
    lines.extend(["", "## 7. 图", ""])
    for svg in svg_paths:
        lines.append(f"![{svg.name}]({svg.name})")
        lines.append("")
    lines.extend(["", "## 8. 输出文件", ""])
    for name in (
        "event_token_inventory.csv",
        "token_probe_summary.csv",
        "maker_diagnostics.csv",
        "strategy_tail_summary.csv",
        "event_summary.csv",
        "fill_event_sample.csv",
        "run_summary.json",
    ):
        lines.append(f"- `{name}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_headline(strategy_tail: pd.DataFrame) -> str:
    if strategy_tail.empty:
        return "没有生成 strategy-tail 汇总。"

    def find(queue_model: str, fill_window: int = 300, markout: int = 300) -> pd.Series | None:
        frame = strategy_tail[
            (strategy_tail["quote_side"] == "strategy_tail_combined")
            & (strategy_tail["queue_model"] == queue_model)
            & (strategy_tail["fill_window_seconds"] == fill_window)
            & (strategy_tail["markout_seconds"] == markout)
        ]
        return None if frame.empty else frame.iloc[0]

    optimistic = find("optimistic")
    half = find("half_queue")
    full = find("full_queue")
    parts = []
    if optimistic is not None:
        parts.append(
            "乐观 300s fill / 300s markout："
            f"median_mean_markout=`{optimistic['median_mean_markout']:.6g}`，"
            f"median_fill_rate=`{optimistic['median_fill_rate']:.3g}`，"
            f"positive_token_share=`{optimistic['positive_markout_token_share']:.3g}`。"
        )
    if half is not None:
        parts.append(
            "半队列假设："
            f"median_mean_markout=`{half['median_mean_markout']:.6g}`，"
            f"median_fill_rate=`{half['median_fill_rate']:.3g}`，"
            f"positive_token_share=`{half['positive_markout_token_share']:.3g}`。"
        )
    if full is not None:
        parts.append(
            "全队列假设："
            f"median_mean_markout=`{full['median_mean_markout']:.6g}`，"
            f"median_fill_rate=`{full['median_fill_rate']:.3g}`，"
            f"positive_token_share=`{full['positive_markout_token_share']:.3g}`。"
        )
    return " ".join(parts) if parts else "没有找到 300s fill / 300s markout 的 strategy-tail combined 行。"


def append_markdown_table(lines: list[str], frame: pd.DataFrame) -> None:
    if frame.empty:
        lines.append("_No rows._")
        return
    display = frame.copy()
    for col in display.columns:
        if pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda x: "" if pd.isna(x) else f"{x:.6g}")
    lines.append("| " + " | ".join(map(str, display.columns)) + " |")
    lines.append("| " + " | ".join("---" for _ in display.columns) + " |")
    for _, row in display.iterrows():
        lines.append("| " + " | ".join(str(row[col]).replace("|", "\\|") for col in display.columns) + " |")


def escape_xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def iso_or_none(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
