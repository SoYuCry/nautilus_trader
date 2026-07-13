# ruff: noqa: I001,RUF001
"""
Cross-event PMXT weather L2 factor research.

Research-only artifact: no Nautilus fills, fees, queue, cash, positions, or PnL.
It processes one selected weather YES token at a time and writes compact
summaries rather than multi-million-row factor panels.
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

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.data_health import analyze_dataset_health  # noqa: E402

BASELINE_MODULE_PATH = REPO_ROOT / "polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py"
VALID_BOOK = "valid"
FLOW_FACTORS = frozenset({"ofi_30s", "trade_pressure_30s"})
BOOK_FACTORS = ("depth_imbalance_1", "depth_imbalance_3", "depth_imbalance_5", "microprice_minus_mid")
DEFAULT_FACTORS = (*BOOK_FACTORS, "ofi_30s", "trade_pressure_30s")
TIME_BUCKET_ORDER = ("early_gt_24h", "last_24h", "last_6h", "final_1h", "post_close")


@dataclass(frozen=True)
class RunSummary:
    config_path: str
    output_dir: str
    event_count: int
    token_count: int
    metric_rows: int
    generated_at: str
    report_path: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Run cross-event PMXT weather factor research.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    config_path = args.config.resolve()
    config = load_config(config_path)
    output_dir = resolve_output_dir(config, config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline = load_baseline_module()
    event_dirs = [Path(raw) for raw in config["input"]["event_dirs"]]
    wall_seconds = [int(v) for v in config.get("labels", {}).get("wall_clock_seconds", [60, 300, 900])]
    valid_steps = [int(v) for v in config.get("labels", {}).get("valid_observation_steps", [10, 50, 200])]
    factors = list(config.get("factors", {}).get("names", DEFAULT_FACTORS))
    quantiles = int(config.get("analysis", {}).get("quantiles", 5))
    min_rows = int(config.get("analysis", {}).get("min_rows_per_metric", 200))

    inventories: list[dict[str, Any]] = []
    token_metrics: list[dict[str, Any]] = []
    regime_metrics: list[dict[str, Any]] = []
    elapsed_rows: list[dict[str, Any]] = []
    processed_tokens = 0

    for event_dir in event_dirs:
        event_index = read_json(event_dir / "event_index.json")
        inventory = build_event_inventory(event_dir, event_index)
        inventories.extend(inventory)
        for market in event_index["markets"]:
            token_id = str(market["yesToken"])
            token_inventory = next((row for row in inventory if row["asset_id"] == token_id), None)
            if token_inventory is None or int(token_inventory["rows"]) <= 0:
                continue
            token_config = build_token_config(config, event_dir, event_index, market, token_id, wall_seconds)
            dataset = PMXTEventV1Adapter(repo_root=REPO_ROOT).load(token_config)
            health = analyze_dataset_health(dataset)
            if not health.ok:
                raise RuntimeError(f"data health failed for {event_index['eventSlug']} {market['label']} YES")

            panel = baseline.build_factor_panel(dataset, token_config)
            panel = baseline.add_labels(panel, token_config)
            panel = add_event_metadata(panel, event_index, market, token_id)
            panel = add_time_to_close(panel)
            panel = add_valid_observation_labels(panel, valid_steps)

            specs = label_specs(wall_seconds, valid_steps)
            token_metrics.extend(
                compute_token_metrics(
                    panel,
                    event_index=event_index,
                    market=market,
                    token_id=token_id,
                    factors=factors,
                    specs=specs,
                    quantiles=quantiles,
                    min_rows=min_rows,
                    health=health,
                ),
            )
            regime_metrics.extend(
                compute_regime_metrics(
                    panel,
                    event_index=event_index,
                    market=market,
                    token_id=token_id,
                    factors=factors,
                    specs=specs,
                    min_rows=min_rows,
                ),
            )
            elapsed_rows.extend(build_elapsed_summary(panel, event_index, market, token_id, valid_steps))
            processed_tokens += 1
            print(f"processed {processed_tokens}: {event_index['eventSlug']} {market['label']} YES rows={len(panel)}")

    inventory_frame = pd.DataFrame(inventories)
    token_metrics_frame = pd.DataFrame(token_metrics)
    regime_metrics_frame = pd.DataFrame(regime_metrics)
    elapsed_frame = pd.DataFrame(elapsed_rows)
    candidate_frame = build_candidate_summary(token_metrics_frame, config)

    inventory_frame.to_csv(output_dir / "event_token_inventory.csv", index=False)
    token_metrics_frame.to_csv(output_dir / "token_factor_metrics.csv", index=False)
    regime_metrics_frame.to_csv(output_dir / "regime_factor_metrics.csv", index=False)
    elapsed_frame.to_csv(output_dir / "valid_observation_elapsed_summary.csv", index=False)
    candidate_frame.to_csv(output_dir / "candidate_summary.csv", index=False)

    svg_paths = write_svgs(output_dir, candidate_frame, token_metrics_frame, elapsed_frame)
    summary = RunSummary(
        config_path=str(config_path),
        output_dir=str(output_dir),
        event_count=len(event_dirs),
        token_count=processed_tokens,
        metric_rows=len(token_metrics_frame),
        generated_at=datetime.now(UTC).isoformat(),
        report_path=str(output_dir / "report.md"),
    )
    write_report(
        output_dir / "report.md",
        summary,
        inventory_frame,
        candidate_frame,
        token_metrics_frame,
        regime_metrics_frame,
        elapsed_frame,
        svg_paths,
    )
    (output_dir / "run_summary.json").write_text(
        json.dumps(asdict(summary), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
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


def build_event_inventory(event_dir: Path, event_index: dict[str, Any]) -> list[dict[str, Any]]:
    orderbook = pd.read_parquet(event_dir / "orderbook.parquet", columns=["asset_id", "timestamp_received", "event_type"])
    orderbook["asset_id"] = orderbook["asset_id"].astype(str)
    rows: list[dict[str, Any]] = []
    for market in event_index["markets"]:
        token_id = str(market["yesToken"])
        frame = orderbook[orderbook["asset_id"] == token_id]
        rows.append(
            {
                **event_market_fields(event_index, market, token_id),
                "rows": len(frame),
                "price_change_rows": int((frame["event_type"] == "price_change").sum()) if not frame.empty else 0,
                "trade_rows": int((frame["event_type"] == "last_trade_price").sum()) if not frame.empty else 0,
                "first_timestamp_received": iso_or_none(frame["timestamp_received"].min()) if not frame.empty else None,
                "last_timestamp_received": iso_or_none(frame["timestamp_received"].max()) if not frame.empty else None,
            },
        )
    return rows


def build_token_config(
    base_config: dict[str, Any],
    event_dir: Path,
    event_index: dict[str, Any],
    market: dict[str, Any],
    token_id: str,
    wall_seconds: list[int],
) -> dict[str, Any]:
    return {
        "input": {
            "event_dir": str(event_dir),
            "condition_id": market["conditionId"],
            "asset_id": token_id,
            "dataset_id": f"{event_index['eventSlug']}-{market['index']}-yes-pmxt-l2",
        },
        "labels": {"horizons_seconds": wall_seconds},
        "factors": base_config.get("factors", {}),
    }


def add_event_metadata(panel: pd.DataFrame, event_index: dict[str, Any], market: dict[str, Any], token_id: str) -> pd.DataFrame:
    panel = panel.copy()
    fields = event_market_fields(event_index, market, token_id)
    for key, value in fields.items():
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


def replay_time_column(panel: pd.DataFrame) -> str:
    """Prefer the shared PMXT research replay clock; fall back for old panels."""
    return "replay_timestamp" if "replay_timestamp" in panel.columns else "timestamp_received"


def add_time_to_close(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.copy()
    clock_col = replay_time_column(panel)
    panel["time_to_close_seconds"] = (panel["event_end"] - panel[clock_col]).dt.total_seconds()
    panel["time_to_close_bucket"] = panel["time_to_close_seconds"].map(time_to_close_bucket)
    return panel


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


def add_valid_observation_labels(panel: pd.DataFrame, valid_steps: list[int]) -> pd.DataFrame:
    if panel.empty:
        return panel
    clock_col = replay_time_column(panel)
    panel = panel.sort_values([clock_col, "sequence"], kind="mergesort").reset_index(drop=True)
    valid = panel[panel["book_validity"] == VALID_BOOK].copy()
    base = (
        valid[[clock_col, "sequence", "mid", "bid1", "ask1"]]
        .sort_values([clock_col, "sequence"], kind="mergesort")
        .groupby(clock_col, sort=False, as_index=False)
        .tail(1)
        .sort_values(clock_col, kind="mergesort")
        .reset_index(drop=True)
    )
    if base.empty:
        return panel
    mapping = pd.DataFrame({clock_col: panel[clock_col], "_row": panel.index})
    for steps in valid_steps:
        future = pd.DataFrame(
            {
                clock_col: base[clock_col],
                f"future_mid_valid_obs_{steps}": base["mid"].shift(-steps),
                f"future_bid_valid_obs_{steps}": base["bid1"].shift(-steps),
                f"future_ask_valid_obs_{steps}": base["ask1"].shift(-steps),
                f"label_matched_timestamp_valid_obs_{steps}": base[clock_col].shift(-steps),
            },
        )
        merged = mapping.merge(future, on=clock_col, how="left").sort_values("_row")
        future_mid = merged[f"future_mid_valid_obs_{steps}"].reset_index(drop=True)
        matched_ts = merged[f"label_matched_timestamp_valid_obs_{steps}"].reset_index(drop=True)
        panel[f"future_mid_valid_obs_{steps}"] = future_mid
        panel[f"future_bid_valid_obs_{steps}"] = merged[f"future_bid_valid_obs_{steps}"].reset_index(drop=True)
        panel[f"future_ask_valid_obs_{steps}"] = merged[f"future_ask_valid_obs_{steps}"].reset_index(drop=True)
        panel[f"label_matched_timestamp_valid_obs_{steps}"] = matched_ts
        panel[f"future_mid_return_valid_obs_{steps}"] = future_mid - panel["mid"]
        panel[f"label_elapsed_seconds_valid_obs_{steps}"] = (matched_ts - panel[clock_col]).dt.total_seconds()
        valid_col = f"future_book_validity_valid_obs_{steps}"
        panel[valid_col] = VALID_BOOK
        panel.loc[future_mid.isna(), valid_col] = pd.NA
    return panel


def label_specs(wall_seconds: list[int], valid_steps: list[int]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for seconds in wall_seconds:
        specs.append(
            {
                "label_name": f"wall_{seconds}s",
                "label_mode": "wall_clock_seconds",
                "horizon_value": seconds,
                "ret_col": f"future_mid_return_{seconds}s",
                "future_valid_col": f"future_book_validity_{seconds}s",
                "elapsed_col": f"label_slippage_seconds_{seconds}s",
                "elapsed_base_seconds": seconds,
            },
        )
    for steps in valid_steps:
        specs.append(
            {
                "label_name": f"valid_obs_{steps}",
                "label_mode": "valid_observation_steps",
                "horizon_value": steps,
                "ret_col": f"future_mid_return_valid_obs_{steps}",
                "future_valid_col": f"future_book_validity_valid_obs_{steps}",
                "elapsed_col": f"label_elapsed_seconds_valid_obs_{steps}",
                "elapsed_base_seconds": 0,
            },
        )
    return specs


def compute_token_metrics(
    panel: pd.DataFrame,
    *,
    event_index: dict[str, Any],
    market: dict[str, Any],
    token_id: str,
    factors: list[str],
    specs: list[dict[str, Any]],
    quantiles: int,
    min_rows: int,
    health: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if spec["ret_col"] not in panel:
            continue
        for factor in factors:
            if factor not in panel:
                continue
            frame = metric_frame(panel, factor, spec)
            if len(frame) < min_rows:
                continue
            raw = spearman_corr(frame["factor"], frame["ret"])
            neutral = progress_neutral_spearman(frame)
            hit = directional_hit_nonzero(frame)
            mono = quantile_monotonicity(frame, quantiles=quantiles)
            bottom, top = bottom_top_quantile_return(frame, quantiles=quantiles)
            rows.append(
                {
                    **event_market_fields(event_index, market, token_id),
                    "factor": factor,
                    "factor_family": "flow_clock_sensitive" if factor in FLOW_FACTORS else "book",
                    "label_name": spec["label_name"],
                    "label_mode": spec["label_mode"],
                    "horizon_value": spec["horizon_value"],
                    "count": len(frame),
                    "raw_spearman": raw,
                    "progress_neutral_spearman": neutral,
                    "nonzero_direction_hit": hit,
                    "quantile_monotonicity_spearman": mono,
                    "bottom_quantile_mean_return": bottom,
                    "top_quantile_mean_return": top,
                    "top_minus_bottom_mean_return": top - bottom if math.isfinite(top) and math.isfinite(bottom) else math.nan,
                    "zero_return_share": float((frame["ret"] == 0).mean()),
                    "positive_return_share": float((frame["ret"] > 0).mean()),
                    "median_label_elapsed_seconds": float(frame["elapsed_seconds"].median()) if frame["elapsed_seconds"].notna().any() else math.nan,
                    "p90_label_elapsed_seconds": float(frame["elapsed_seconds"].quantile(0.90)) if frame["elapsed_seconds"].notna().any() else math.nan,
                    "source_time_inversion_count": health.summary.source_time_inversion_count,
                    "source_delay_over_threshold_count": health.summary.source_delay_over_threshold_count,
                },
            )
    return rows


def compute_regime_metrics(
    panel: pd.DataFrame,
    *,
    event_index: dict[str, Any],
    market: dict[str, Any],
    token_id: str,
    factors: list[str],
    specs: list[dict[str, Any]],
    min_rows: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if spec["ret_col"] not in panel:
            continue
        for factor in factors:
            if factor not in panel:
                continue
            frame = metric_frame(panel, factor, spec)
            if frame.empty:
                continue
            for bucket in TIME_BUCKET_ORDER:
                bucket_frame = frame[frame["time_to_close_bucket"] == bucket]
                if len(bucket_frame) < min_rows:
                    continue
                rows.append(
                    {
                        **event_market_fields(event_index, market, token_id),
                        "factor": factor,
                        "factor_family": "flow_clock_sensitive" if factor in FLOW_FACTORS else "book",
                        "label_name": spec["label_name"],
                        "label_mode": spec["label_mode"],
                        "horizon_value": spec["horizon_value"],
                        "time_to_close_bucket": bucket,
                        "count": len(bucket_frame),
                        "raw_spearman": spearman_corr(bucket_frame["factor"], bucket_frame["ret"]),
                        "nonzero_direction_hit": directional_hit_nonzero(bucket_frame),
                        "zero_return_share": float((bucket_frame["ret"] == 0).mean()),
                        "median_label_elapsed_seconds": float(bucket_frame["elapsed_seconds"].median()) if bucket_frame["elapsed_seconds"].notna().any() else math.nan,
                    },
                )
    return rows


def metric_frame(panel: pd.DataFrame, factor: str, spec: dict[str, Any]) -> pd.DataFrame:
    elapsed = pd.to_numeric(panel.get(spec["elapsed_col"], math.nan), errors="coerce") + float(spec.get("elapsed_base_seconds", 0))
    frame = pd.DataFrame(
        {
            "factor": pd.to_numeric(panel[factor], errors="coerce"),
            "ret": pd.to_numeric(panel[spec["ret_col"]], errors="coerce"),
            "future_book_validity": panel.get(spec["future_valid_col"], VALID_BOOK),
            "book_validity": panel.get("book_validity", VALID_BOOK),
            "time_to_close_bucket": panel.get("time_to_close_bucket", "unknown"),
            "elapsed_seconds": elapsed,
        },
    )
    return frame[(frame["book_validity"] == VALID_BOOK) & (frame["future_book_validity"] == VALID_BOOK)].dropna(subset=["factor", "ret"])


def build_elapsed_summary(panel: pd.DataFrame, event_index: dict[str, Any], market: dict[str, Any], token_id: str, valid_steps: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for steps in valid_steps:
        col = f"label_elapsed_seconds_valid_obs_{steps}"
        if col not in panel:
            continue
        frame = panel[(panel["book_validity"] == VALID_BOOK) & panel[col].notna()].copy()
        for bucket in TIME_BUCKET_ORDER:
            values = pd.to_numeric(frame.loc[frame["time_to_close_bucket"] == bucket, col], errors="coerce").dropna()
            if values.empty:
                continue
            rows.append(
                {
                    **event_market_fields(event_index, market, token_id),
                    "label_name": f"valid_obs_{steps}",
                    "valid_observation_steps": steps,
                    "time_to_close_bucket": bucket,
                    "count": len(values),
                    "elapsed_seconds_p10": float(values.quantile(0.10)),
                    "elapsed_seconds_p50": float(values.quantile(0.50)),
                    "elapsed_seconds_p90": float(values.quantile(0.90)),
                    "elapsed_seconds_max": float(values.max()),
                },
            )
    return rows


def build_candidate_summary(token_metrics: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    if token_metrics.empty:
        return pd.DataFrame()
    positive_threshold = float(config.get("analysis", {}).get("candidate_positive_token_share", 0.6))
    min_events = int(config.get("analysis", {}).get("candidate_min_event_count", 2))
    min_hit = float(config.get("analysis", {}).get("candidate_min_nonzero_hit", 0.52))
    rows: list[dict[str, Any]] = []
    grouped = token_metrics.groupby(["factor", "factor_family", "label_name", "label_mode", "horizon_value"], observed=True)
    for keys, frame in grouped:
        factor, family, label_name, label_mode, horizon_value = keys
        event_count = frame["event_slug"].nunique()
        raw = pd.to_numeric(frame["raw_spearman"], errors="coerce")
        neutral = pd.to_numeric(frame["progress_neutral_spearman"], errors="coerce")
        hit = pd.to_numeric(frame["nonzero_direction_hit"], errors="coerce")
        mono = pd.to_numeric(frame["quantile_monotonicity_spearman"], errors="coerce")
        positive_share = float((neutral > 0).mean()) if len(neutral.dropna()) else math.nan
        is_candidate = bool(
            family == "book"
            and label_mode == "valid_observation_steps"
            and event_count >= min_events
            and positive_share >= positive_threshold
            and float(hit.median()) >= min_hit
            and float(mono.median()) >= 0.3
        )
        rows.append(
            {
                "factor": factor,
                "factor_family": family,
                "label_name": label_name,
                "label_mode": label_mode,
                "horizon_value": horizon_value,
                "event_count": event_count,
                "token_metric_count": len(frame),
                "median_raw_spearman": float(raw.median()),
                "median_progress_neutral_spearman": float(neutral.median()),
                "positive_token_share": positive_share,
                "median_nonzero_direction_hit": float(hit.median()),
                "median_quantile_monotonicity_spearman": float(mono.median()),
                "median_zero_return_share": float(pd.to_numeric(frame["zero_return_share"], errors="coerce").median()),
                "median_label_elapsed_seconds": float(pd.to_numeric(frame["median_label_elapsed_seconds"], errors="coerce").median()),
                "is_cross_event_candidate": is_candidate,
            },
        )
    return pd.DataFrame(rows).sort_values(
        ["is_cross_event_candidate", "median_progress_neutral_spearman", "positive_token_share"],
        ascending=[False, False, False],
    )


def spearman_corr(x: pd.Series, y: pd.Series) -> float:
    frame = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(frame) < 2 or frame["x"].nunique(dropna=True) < 2 or frame["y"].nunique(dropna=True) < 2:
        return math.nan
    return float(frame["x"].rank(method="average").corr(frame["y"].rank(method="average"), method="pearson"))


def progress_neutral_spearman(frame: pd.DataFrame) -> float:
    ranked = frame.copy()
    ranked["factor_rank"] = ranked.groupby("time_to_close_bucket", observed=True)["factor"].rank(method="average", pct=True)
    ranked["ret_rank"] = ranked.groupby("time_to_close_bucket", observed=True)["ret"].rank(method="average", pct=True)
    return spearman_corr(ranked["factor_rank"], ranked["ret_rank"])


def directional_hit_nonzero(frame: pd.DataFrame) -> float:
    work = frame.copy()
    work["factor_rank_centered"] = work.groupby("time_to_close_bucket", observed=True)["factor"].rank(method="average", pct=True) - 0.5
    nonzero = work[(work["ret"] != 0) & (work["factor_rank_centered"] != 0)].dropna(subset=["factor_rank_centered", "ret"])
    if nonzero.empty:
        return math.nan
    return float(((nonzero["factor_rank_centered"] > 0) == (nonzero["ret"] > 0)).mean())


def quantile_monotonicity(frame: pd.DataFrame, *, quantiles: int) -> float:
    work = frame.dropna(subset=["factor", "ret"]).copy()
    if len(work) < quantiles * 5 or work["factor"].nunique(dropna=True) < 3:
        return math.nan
    try:
        work["quantile"] = pd.qcut(work["factor"], q=quantiles, labels=False, duplicates="drop")
    except ValueError:
        return math.nan
    grouped = work.groupby("quantile", observed=True)["ret"].mean().dropna()
    if len(grouped) < 3:
        return math.nan
    return spearman_corr(pd.Series(grouped.index.to_numpy(dtype=float), index=grouped.index), grouped)


def bottom_top_quantile_return(frame: pd.DataFrame, *, quantiles: int) -> tuple[float, float]:
    work = frame.dropna(subset=["factor", "ret"]).copy()
    if len(work) < quantiles * 5 or work["factor"].nunique(dropna=True) < 3:
        return math.nan, math.nan
    try:
        work["quantile"] = pd.qcut(work["factor"], q=quantiles, labels=False, duplicates="drop")
    except ValueError:
        return math.nan, math.nan
    grouped = work.groupby("quantile", observed=True)["ret"].mean().sort_index()
    if len(grouped) < 2:
        return math.nan, math.nan
    return float(grouped.iloc[0]), float(grouped.iloc[-1])


def write_svgs(output_dir: Path, candidate: pd.DataFrame, token_metrics: pd.DataFrame, elapsed: pd.DataFrame) -> list[Path]:
    paths: list[Path] = []
    if not candidate.empty:
        adaptive = candidate[candidate["label_mode"] == "valid_observation_steps"]
        if not adaptive.empty:
            pivot = adaptive.pivot_table(index="factor", columns="label_name", values="median_progress_neutral_spearman", aggfunc="first").fillna(0.0)
            path = output_dir / "adaptive_horizon_median_ic.svg"
            write_grouped_bar_svg(path, pivot, title="Adaptive valid-observation horizon: median progress-neutral IC")
            paths.append(path)
    if not token_metrics.empty:
        compare = token_metrics[(token_metrics["factor_family"] == "book") & (token_metrics["label_name"].isin(["wall_300s", "valid_obs_50"]))]
        if not compare.empty:
            pivot = compare.pivot_table(index="factor", columns="label_name", values="progress_neutral_spearman", aggfunc="median").fillna(0.0)
            path = output_dir / "wall_300s_vs_valid_obs_50.svg"
            write_grouped_bar_svg(path, pivot, title="Fixed 300s vs adaptive 50 valid observations")
            paths.append(path)
    if not elapsed.empty:
        pivot = (
            elapsed[elapsed["label_name"] == "valid_obs_50"]
            .pivot_table(index="time_to_close_bucket", columns="event_slug", values="elapsed_seconds_p50", aggfunc="median")
            .reindex(TIME_BUCKET_ORDER)
            .dropna(how="all")
            .fillna(0.0)
        )
        if not pivot.empty:
            path = output_dir / "valid_obs_50_elapsed_by_regime.svg"
            write_grouped_bar_svg(path, pivot, title="valid_obs_50 median elapsed seconds by event phase")
            paths.append(path)
    return paths


def write_grouped_bar_svg(path: Path, table: pd.DataFrame, *, title: str) -> None:
    width, height = 1040, 540
    margin_left, margin_right, margin_top, margin_bottom = 180, 30, 58, 105
    plot_w, plot_h = width - margin_left - margin_right, height - margin_top - margin_bottom
    values = table.to_numpy(dtype=float).flatten()
    finite = [float(v) for v in values if math.isfinite(float(v))] or [0.0]
    y_min, y_max = min(0.0, *finite), max(0.0, *finite)
    if y_min == y_max:
        y_min, y_max = y_min - 1.0, y_max + 1.0
    pad = (y_max - y_min) * 0.10
    y_min, y_max = y_min - pad, y_max + pad
    zero_y = margin_top + (y_max / (y_max - y_min)) * plot_h
    row_count, col_count = max(len(table.index), 1), max(len(table.columns), 1)
    group_w, bar_w = plot_w / row_count, plot_w / row_count / (col_count + 1)
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2"]

    def y(value: float) -> float:
        return margin_top + (y_max - value) / (y_max - y_min) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" font-size="20" font-family="Arial">{escape_xml(title)}</text>',
        f'<line x1="{margin_left}" y1="{zero_y:.2f}" x2="{width-margin_right}" y2="{zero_y:.2f}" stroke="#111827" stroke-width="1"/>',
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height-margin_bottom}" stroke="#111827" stroke-width="1"/>',
    ]
    for row_i, index_value in enumerate(table.index):
        x0 = margin_left + row_i * group_w
        parts.append(
            f'<text x="{x0 + group_w/2:.2f}" y="{height-67}" text-anchor="middle" font-size="12" font-family="Arial" transform="rotate(-25 {x0 + group_w/2:.2f},{height-67})">{escape_xml(str(index_value))}</text>',
        )
        for col_i, col in enumerate(table.columns):
            value = float(table.loc[index_value, col])
            bar_x = x0 + (col_i + 0.5) * bar_w
            bar_y = y(max(value, 0.0)) if value >= 0 else zero_y
            bar_h = abs(y(value) - zero_y)
            parts.append(
                f'<rect x="{bar_x:.2f}" y="{bar_y:.2f}" width="{bar_w*0.82:.2f}" height="{bar_h:.2f}" fill="{colors[col_i % len(colors)]}" opacity="0.88"><title>{escape_xml(str(index_value))} {escape_xml(str(col))}: {value:.6g}</title></rect>',
            )
    for col_i, col in enumerate(table.columns):
        lx = margin_left + col_i * 170
        parts.append(f'<rect x="{lx}" y="{height-37}" width="12" height="12" fill="{colors[col_i % len(colors)]}"/>')
        parts.append(f'<text x="{lx+18}" y="{height-25}" font-size="12" font-family="Arial">{escape_xml(str(col))}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_report(
    path: Path,
    summary: RunSummary,
    inventory: pd.DataFrame,
    candidate: pd.DataFrame,
    token_metrics: pd.DataFrame,
    regime_metrics: pd.DataFrame,
    elapsed: pd.DataFrame,
    svg_paths: list[Path],
) -> None:
    clean = candidate[candidate["is_cross_event_candidate"] == True] if not candidate.empty else pd.DataFrame()  # noqa: E712
    adaptive = candidate[candidate["label_mode"] == "valid_observation_steps"] if not candidate.empty else pd.DataFrame()
    wall = candidate[candidate["label_mode"] == "wall_clock_seconds"] if not candidate.empty else pd.DataFrame()
    lines = [
        "# PMXT Weather Cross-Event L2 因子研究报告",
        "",
        f"生成时间: {summary.generated_at}",
        "",
        "## 0. 结论先行",
        "",
        "上一轮单 event 使用的是 `highest-temperature-in-shanghai-on-june-9-2026 / 25°C YES`。这轮改为两个上海最高温天气 event 的全部 YES token。",
        "",
    ]
    if not clean.empty:
        best = clean.iloc[0]
        lines.append(
            f"- 当前跨 event/adaptive 候选第一名: `{best['factor']}` + `{best['label_name']}`，median progress-neutral IC = `{best['median_progress_neutral_spearman']:.6f}`，positive token share = `{best['positive_token_share']:.3f}`，median nonzero hit = `{best['median_nonzero_direction_hit']:.3f}`。",
        )
    else:
        lines.append("- 当前没有因子通过跨 event/adaptive 候选筛选；上一轮单 event 的高 IC 不能升级为稳定信号。")
    lines.extend(
        [
            "- 固定 `300s` 对天气市场不公平：早期/末期交易频率差异很大，所以本报告只把 wall-clock horizon 当诊断。",
            "- 真正用于候选的是 `valid_obs_N`，即向后第 N 个 valid book observation，更接近事件时间 / 活跃度归一化 horizon。",
            "- `ofi_30s` / `trade_pressure_30s` 仍标记为 flow_clock_sensitive；PMXT source timestamp 问题解决前不升级为策略候选。",
            "- 这仍不是 Nautilus 回测，不包含成交、fee、queue、cash、position、PnL。",
            "",
            "## 1. 用了哪些 event/token",
            "",
            f"- event_count: {summary.event_count}",
            f"- processed YES tokens: {summary.token_count}",
            "",
        ],
    )
    append_markdown_table(lines, inventory[["event_slug", "market_index", "market_label", "rows", "trade_rows", "first_timestamp_received", "last_timestamp_received"]].head(40))
    lines.extend(["", "## 2. 为什么不能只看固定 300s", ""])
    lines.extend(
        [
            "天气 event 生命周期约 2-3 天，但交易活跃度集中在临近结算阶段。固定 300s 在早期可能只是没有信息流的 5 分钟，在末期却可能跨过大量盘口更新。",
            "",
            "- `wall_60s / wall_300s / wall_900s`: 固定秒数，只做诊断；",
            "- `valid_obs_10 / 50 / 200`: 向后第 N 个 valid book observation，更接近事件时间。",
            "",
            "`valid_obs_50` 在不同阶段对应的真实秒数如下：",
            "",
        ],
    )
    elapsed_preview = aggregate_elapsed_preview(elapsed, "valid_obs_50")
    append_markdown_table(lines, elapsed_preview.head(40))
    lines.extend(["", "## 3. 图", ""])
    for svg in svg_paths:
        lines.append(f"![{svg.name}]({svg.name})")
        lines.append("")
    lines.extend(["", "## 4. Adaptive horizon 候选排名", ""])
    append_markdown_table(lines, adaptive.head(30))
    lines.extend(["", "## 5. 固定 wall-clock horizon 诊断排名", ""])
    append_markdown_table(lines, wall.head(30))
    lines.extend(["", "## 6. Token-level 指标预览", ""])
    append_markdown_table(lines, token_metrics.head(60))
    lines.extend(["", "## 7. Event phase / regime 指标预览", ""])
    append_markdown_table(lines, regime_metrics.head(60))
    lines.extend(["", "## 8. 产物", ""])
    for name in (
        "event_token_inventory.csv",
        "token_factor_metrics.csv",
        "regime_factor_metrics.csv",
        "valid_observation_elapsed_summary.csv",
        "candidate_summary.csv",
        "run_summary.json",
    ):
        lines.append(f"- `{name}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def aggregate_elapsed_preview(elapsed: pd.DataFrame, label_name: str) -> pd.DataFrame:
    if elapsed.empty:
        return pd.DataFrame()
    frame = elapsed[elapsed["label_name"] == label_name].copy()
    if frame.empty:
        return pd.DataFrame()
    grouped = (
        frame.groupby(["event_slug", "time_to_close_bucket"], observed=True)
        .agg(
            token_count=("asset_id", "nunique"),
            row_count=("count", "sum"),
            median_elapsed_p50=("elapsed_seconds_p50", "median"),
            median_elapsed_p90=("elapsed_seconds_p90", "median"),
            max_elapsed=("elapsed_seconds_max", "max"),
        )
        .reset_index()
    )
    grouped["time_to_close_bucket"] = pd.Categorical(grouped["time_to_close_bucket"], categories=TIME_BUCKET_ORDER, ordered=True)
    return grouped.sort_values(["event_slug", "time_to_close_bucket"])


def escape_xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def iso_or_none(value: Any) -> str | None:
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
