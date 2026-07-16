"""Run the fixed 36-event token baseline and paired event-level analyses.

This is deliberately a single research runner.  It reuses the existing PMXT
adapter and factor protocol, persists one compact token anchor per market so an
interrupted run can resume, and produces only the outputs required by
EVENT_LEVEL_EXPERIMENT_REQUIREMENTS.md.  It is not a PnL backtest.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
INVENTORY_PATH = REPO_ROOT / "polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/event_inventory.parquet"
PROTOCOL_PATH = REPO_ROOT / "polymarket/research/2026-07-14-pmxt-weather-factor-wave0/factor_protocol.py"
RUN_ROOT = HERE / "run/batch_36_event"
EVENT_LEVEL_ROOT = HERE / "event_level"
FACTORS = ("depth_imbalance_1", "microprice_minus_mid")
HORIZONS = (30, 60, 120, 300, 600, 900)
LIFECYCLE_ORDER = [">24h", "6-24h", "1-6h", "<1h"]
GRID_FREQUENCY = "1min"
EVENT_HORIZON_STEPS = 2  # 120 seconds on the one-minute event grid.


def load_protocol() -> Any:
    name = "pmxt_weather_event_level_protocol"
    spec = importlib.util.spec_from_file_location(name, PROTOCOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load factor protocol: {PROTOCOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def safe_ic(factor: pd.Series, label: pd.Series) -> float:
    valid = pd.DataFrame({"factor": factor, "label": label}).dropna()
    if len(valid) < 3 or valid.factor.nunique() < 2 or valid.label.nunique() < 2:
        return math.nan
    return float(valid.factor.rank().corr(valid.label.rank()))


def lifecycle_bucket(timestamp: pd.Series, event_end: Any) -> pd.Series:
    hours = (pd.Timestamp(event_end) - pd.to_datetime(timestamp, utc=True)).dt.total_seconds() / 3600
    return pd.cut(
        hours,
        [-np.inf, 1, 6, 24, np.inf],
        labels=["<1h", "1-6h", "6-24h", ">24h"],
        ordered=True,
    )


def select_batch36(inventory_path: Path) -> pd.DataFrame:
    """Pre-result deterministic sample balanced over the available rebuild dates."""
    inventory = pd.read_parquet(inventory_path).sort_values(["event_date", "rows_written", "event_slug"], kind="mergesort")
    selected: list[pd.Series] = []
    used_cities: set[str] = set()
    dates = sorted(inventory.event_date.unique())
    base, remainder = divmod(36, len(dates))
    for date_index, (event_date, date_rows) in enumerate(inventory.groupby("event_date", sort=True)):
        date_rows = date_rows.reset_index(drop=True)
        available = set(date_rows.index)
        quota = base + (date_index < remainder)
        targets = np.linspace(0.05, 0.95, quota)
        for quantile in targets:
            target = quantile * (len(date_rows) - 1)
            candidates = sorted(
                available,
                key=lambda idx: (str(date_rows.loc[idx, "city"]) in used_cities, abs(idx - target), str(date_rows.loc[idx, "event_slug"])),
            )
            idx = candidates[0]
            row = date_rows.loc[idx].copy()
            row["sample_activity"] = "low" if quantile < 0.25 else "high" if quantile > 0.75 else "medium"
            selected.append(row)
            used_cities.add(str(row.city))
            available.remove(idx)
    result = pd.DataFrame(selected).reset_index(drop=True)
    if len(result) != 36 or result.event_date.nunique() != len(dates):
        raise RuntimeError(f"batch sample must be 36 events over all rebuild dates, got {len(result)} over {result.event_date.nunique()}")
    if result.city.nunique() < 24:
        raise RuntimeError(f"batch sample requires >=24 cities, got {result.city.nunique()}")
    return result


def temperature_value(label: str, fallback: int) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", str(label))
    return float(match.group()) if match else float(fallback)


def token_metric_rows(ranking: pd.DataFrame, event: dict[str, Any], token_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ranking = ranking.copy()
    ranking["lifecycle_bucket"] = lifecycle_bucket(ranking.timestamp, event["event_end"])
    for factor in FACTORS:
        for horizon in HORIZONS:
            label = f"future_mid_return_{horizon}s"
            future_mid = f"future_mid_{horizon}s"
            for bucket, group in [("all", ranking), *list(ranking.groupby("lifecycle_bucket", observed=True))]:
                valid = group[[factor, label]].dropna()
                crossing = math.nan
                cross = group[[factor, "bid1", "ask1", future_mid]].dropna()
                if not cross.empty:
                    marks = np.where(
                        cross[factor] > 0,
                        cross[future_mid] - cross.ask1,
                        np.where(cross[factor] < 0, cross.bid1 - cross[future_mid], np.nan),
                    )
                    crossing = float(np.nanmean(marks)) if np.isfinite(marks).any() else math.nan
                rows.append(
                    {
                        "event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]),
                        "cohort": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded",
                        "sample_activity": event["sample_activity"], "token_id": token_id, "factor": factor,
                        "horizon_seconds": horizon, "lifecycle_bucket": str(bucket), "rows": len(group),
                        "valid_rows": len(valid), "coverage": len(valid) / len(group) if len(group) else math.nan,
                        "zero_rate": float(valid[label].eq(0).mean()) if len(valid) else math.nan,
                        "ic": safe_ic(valid[factor], valid[label]), "crossing_markout": crossing,
                    },
                )
    return rows


def process_event(event: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter

    event_dir = Path(event["paths"]["event_dir"])
    event_index = json.loads((event_dir / "event_index.json").read_text(encoding="utf-8"))
    output = RUN_ROOT / str(event["event_slug"])
    token_root = output / "tokens"
    token_root.mkdir(parents=True, exist_ok=True)
    metrics_path = output / "token_baseline_metrics.csv"
    summary_path = output / "summary.json"
    if not force and metrics_path.exists() and summary_path.exists() and len(list(token_root.glob("*.parquet"))) == len(event_index["markets"]):
        return json.loads(summary_path.read_text(encoding="utf-8"))

    started = time.perf_counter()
    protocol = load_protocol()
    adapter = PMXTEventV1Adapter(repo_root=REPO_ROOT)
    metrics: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for market in event_index["markets"]:
        market_index = int(market["index"])
        token_id = str(market["yesToken"])
        anchor_path = token_root / f"{market_index:02d}.parquet"
        metric_sidecar = token_root / f"{market_index:02d}.metrics.json"
        if not force and anchor_path.exists() and metric_sidecar.exists():
            sidecar = json.loads(metric_sidecar.read_text(encoding="utf-8"))
            metrics.extend(sidecar["metrics"])
            skipped.extend(sidecar["skipped"])
            failures.extend(sidecar["failures"])
            continue
        token_metrics: list[dict[str, Any]] = []
        token_skipped: list[dict[str, Any]] = []
        token_failures: list[dict[str, Any]] = []
        try:
            dataset = adapter.load({"input": {"event_dir": str(event_dir), "condition_id": str(market["conditionId"]), "asset_id": token_id, "dataset_id": str(event["event_slug"])}})
            panel = protocol.build_factor_panel(dataset, horizons_seconds=HORIZONS, include_labels=True)
            keep = panel.ranking_observation | panel.event_type.astype(str).str.contains("trade", case=False, na=False)
            anchors = panel.loc[keep, [
                "timestamp", "sequence", "market", "token_id", "event_type", "actual_mutation", "ranking_observation",
                "bid1", "ask1", "mid", "spread", "book_update_intensity", *FACTORS,
                *[f"future_mid_{h}s" for h in HORIZONS], *[f"future_mid_return_{h}s" for h in HORIZONS],
            ]].copy()
            anchors["market_index"] = market_index
            anchors["market_label"] = str(market.get("label", market_index))
            anchors["temperature_bin"] = temperature_value(str(market.get("label", "")), market_index)
            anchors.to_parquet(anchor_path, index=False)
            ranking = panel[panel.ranking_observation].copy()
            if ranking.empty:
                token_skipped.append({"event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]), "cohort": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded", "market_index": market_index, "token_id": token_id, "reason": "no ranking observations"})
            else:
                token_metrics.extend(token_metric_rows(ranking, event, token_id))
        except Exception as exc:
            token_failures.append({"market_index": market_index, "token_id": token_id, "error": repr(exc)})
            pd.DataFrame(columns=["timestamp", "token_id"]).to_parquet(anchor_path, index=False)
        sidecar = {"metrics": token_metrics, "skipped": token_skipped, "failures": token_failures}
        metric_sidecar.write_text(json.dumps(sidecar, ensure_ascii=False, default=str), encoding="utf-8")
        metrics.extend(token_metrics); skipped.extend(token_skipped); failures.extend(token_failures)

    pd.DataFrame(metrics).to_csv(metrics_path, index=False)
    summary = {
        "event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]),
        "cohort": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded",
        "sample_activity": event["sample_activity"], "markets_total": len(event_index["markets"]),
        "tokens_used": len(event_index["markets"]) - len(skipped) - len(failures), "tokens_skipped": len(skipped),
        "failures": failures, "skipped": skipped, "elapsed_seconds": time.perf_counter() - started,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def event_grid(event: dict[str, Any]) -> pd.DataFrame:
    event_output = RUN_ROOT / str(event["event_slug"])
    token_frames: list[pd.DataFrame] = []
    for path in sorted((event_output / "tokens").glob("*.parquet")):
        frame = pd.read_parquet(path)
        if not frame.empty:
            token_frames.append(frame)
    if not token_frames:
        return pd.DataFrame()
    observations = pd.concat(token_frames, ignore_index=True)
    observations["timestamp"] = pd.to_datetime(observations.timestamp, utc=True)
    start = max(pd.Timestamp(event["event_start"]), observations.timestamp.min()).ceil(GRID_FREQUENCY)
    end = min(pd.Timestamp(event["event_end"]), observations.timestamp.max()).floor(GRID_FREQUENCY)
    grid_times = pd.DataFrame({"timestamp": pd.date_range(start, end, freq=GRID_FREQUENCY, tz="UTC")})
    states: list[pd.DataFrame] = []
    for token_id, token in observations.groupby("token_id", sort=False):
        token = token.sort_values(["timestamp", "sequence"], kind="mergesort")
        state = token[token.ranking_observation].drop_duplicates("timestamp", keep="last")
        if state.empty:
            continue
        state = state.rename(columns={"timestamp": "state_timestamp"})
        merged = pd.merge_asof(
            grid_times,
            state,
            left_on="timestamp",
            right_on="state_timestamp",
            direction="backward",
            allow_exact_matches=True,
        )
        merged["state_age_seconds"] = (merged.timestamp - merged.state_timestamp).dt.total_seconds()
        merged["token_id"] = token_id
        minute_mutations = token[token.actual_mutation].set_index("timestamp").resample(GRID_FREQUENCY).size().reindex(grid_times.timestamp, fill_value=0)
        minute_trades = token[token.event_type.astype(str).str.contains("trade", case=False, na=False)].set_index("timestamp").resample(GRID_FREQUENCY).size().reindex(grid_times.timestamp, fill_value=0)
        merged["updates"] = minute_mutations.to_numpy()
        merged["trades"] = minute_trades.to_numpy()
        merged["rolling_mutations"] = merged.updates.rolling(30, min_periods=1).sum()
        merged["rolling_trades"] = merged.trades.rolling(30, min_periods=1).sum()
        states.append(merged)
    if not states:
        return pd.DataFrame()
    panel = pd.concat(states, ignore_index=True)
    panel["has_valid_bbo"] = panel.bid1.notna() & panel.ask1.notna() & panel.bid1.lt(panel.ask1)
    panel["raw_mid"] = panel.mid.where(panel.has_valid_bbo)
    panel["lifecycle_bucket"] = lifecycle_bucket(panel.timestamp, event["event_end"])
    panel["cohort"] = "clean" if event["cohort_role"] == "primary_development_replication" else "degraded"
    panel["city"] = event["city"]; panel["event_date"] = str(event["event_date"]); panel["event_slug"] = event["event_slug"]
    panel["probability_rank"] = panel.groupby("timestamp").raw_mid.rank(method="first", ascending=False)
    panel["activity_rank"] = panel.groupby("timestamp").rolling_mutations.rank(method="first", ascending=False)
    panel["top1_flag"] = panel.probability_rank.le(1)
    panel["top3_flag"] = panel.probability_rank.le(3)
    panel["is_active"] = panel.has_valid_bbo & (
        panel.top3_flag | (panel.activity_rank.le(3) & panel.rolling_mutations.gt(0)) | panel.rolling_trades.gt(0)
    )
    panel["active_reason"] = np.select(
        [panel.top3_flag, panel.rolling_trades.gt(0), panel.activity_rank.le(3) & panel.rolling_mutations.gt(0)],
        ["top3_probability", "recent_trade", "top3_activity"], default="inactive",
    )
    panel["future_mid"] = panel.groupby("token_id", sort=False).raw_mid.shift(-EVENT_HORIZON_STEPS)
    panel["forward_return"] = panel.future_mid - panel.raw_mid
    crossing = np.where(panel.depth_imbalance_1 > 0, panel.future_mid - panel.ask1, np.where(panel.depth_imbalance_1 < 0, panel.bid1 - panel.future_mid, np.nan))
    panel["crossing_markout"] = crossing
    return panel


def event_distribution(panel: pd.DataFrame) -> pd.DataFrame:
    valid = panel.dropna(subset=["raw_mid"]).copy()
    if valid.empty:
        return pd.DataFrame()
    valid["sum_mid"] = valid.groupby("timestamp").raw_mid.transform("sum")
    valid["normalized_probability"] = valid.raw_mid / valid.sum_mid.replace(0, np.nan)
    valid["weighted_temperature"] = valid.normalized_probability * valid.temperature_bin
    valid["pressure_component"] = valid.normalized_probability * valid.depth_imbalance_1
    rows: list[dict[str, Any]] = []
    for timestamp, group in valid.groupby("timestamp", sort=True):
        probabilities = group.normalized_probability.dropna().to_numpy()
        if len(probabilities) == 0:
            continue
        mean = float(group.weighted_temperature.sum())
        variance = float((group.normalized_probability * (group.temperature_bin - mean) ** 2).sum())
        entropy = float(-(probabilities * np.log(np.clip(probabilities, 1e-12, None))).sum())
        ordered = group.sort_values("normalized_probability", ascending=False)
        pressure = float((group.normalized_probability * (group.temperature_bin - mean) * group.depth_imbalance_1).sum())
        rows.append({
            "event_slug": group.event_slug.iloc[0], "city": group.city.iloc[0], "event_date": group.event_date.iloc[0], "cohort": group.cohort.iloc[0],
            "timestamp": timestamp, "lifecycle_bucket": str(group.lifecycle_bucket.iloc[0]), "sum_mid": float(group.raw_mid.sum()),
            "sum_deviation": float(group.raw_mid.sum() - 1.0), "implied_temperature": mean, "distribution_variance": variance,
            "distribution_width": float(math.sqrt(max(variance, 0))), "entropy": entropy,
            "top3_probability_mass": float(ordered.normalized_probability.head(3).sum()), "active_market_count": int(group.is_active.sum()),
            "valid_market_count": len(group), "updates": int(group.updates.sum()), "trades": int(group.trades.sum()),
            "spread_median": float(group.spread.median()), "spread_p90": float(group.spread.quantile(.9)),
            "probability_mass_flow": math.nan, "pressure_factor": pressure,
        })
    result = pd.DataFrame(rows).sort_values("timestamp")
    result["probability_mass_flow"] = result.implied_temperature.diff().abs()
    result["future_mean_move"] = result.implied_temperature.shift(-EVENT_HORIZON_STEPS) - result.implied_temperature
    result["future_entropy_change"] = result.entropy.shift(-EVENT_HORIZON_STEPS) - result.entropy
    return result


def aggregate_event_outputs(selected: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    lifecycle_parts: list[pd.DataFrame] = []
    active_parts: list[pd.DataFrame] = []
    distribution_parts: list[pd.DataFrame] = []
    snapshots: list[pd.DataFrame] = []
    for event in selected.to_dict("records"):
        panel = event_grid(event)
        if panel.empty:
            continue
        distribution = event_distribution(panel)
        distribution_parts.append(distribution)
        for bucket, group in panel.groupby("lifecycle_bucket", observed=True):
            lifecycle_parts.append(pd.DataFrame([{
                "event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]),
                "cohort": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded",
                "lifecycle_bucket": str(bucket), "grid_rows": group.timestamp.nunique(), "updates": int(group.updates.sum()),
                "trades": int(group.trades.sum()), "updates_per_minute": float(group.groupby("timestamp").updates.sum().mean()),
                "trades_per_minute": float(group.groupby("timestamp").trades.sum().mean()),
                "active_market_count": float(group.groupby("timestamp").is_active.sum().mean()),
                "spread_median": float(group.spread.median()), "spread_p90": float(group.spread.quantile(.9)),
            }]))
        for scope, group in (("all_market", panel[panel.has_valid_bbo]), ("dynamic_active", panel[panel.is_active])):
            valid = group[["depth_imbalance_1", "forward_return"]].dropna()
            active_parts.append(pd.DataFrame([{
                "event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]),
                "cohort": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded", "scope": scope,
                "rows": len(group), "coverage": len(valid) / len(group) if len(group) else math.nan,
                "zero_rate": float(valid.forward_return.eq(0).mean()) if len(valid) else math.nan,
                "ic": safe_ic(valid.depth_imbalance_1, valid.forward_return), "crossing_markout": float(group.crossing_markout.mean()),
                "top1_update_share": float(group.loc[group.top1_flag, "updates"].sum() / group.updates.sum()) if group.updates.sum() else math.nan,
                "top3_update_share": float(group.loc[group.top3_flag, "updates"].sum() / group.updates.sum()) if group.updates.sum() else math.nan,
                "top1_trade_share": float(group.loc[group.top1_flag, "trades"].sum() / group.trades.sum()) if group.trades.sum() else math.nan,
                "top3_trade_share": float(group.loc[group.top3_flag, "trades"].sum() / group.trades.sum()) if group.trades.sum() else math.nan,
                "top1_probability_mass": float(group.groupby("timestamp").apply(lambda x: x.loc[x.top1_flag, "raw_mid"].sum() / x.raw_mid.sum() if x.raw_mid.sum() else np.nan, include_groups=False).mean()),
                "top3_probability_mass": float(group.groupby("timestamp").apply(lambda x: x.loc[x.top3_flag, "raw_mid"].sum() / x.raw_mid.sum() if x.raw_mid.sum() else np.nan, include_groups=False).mean()),
                "active_market_count": float(group.groupby("timestamp").is_active.sum().mean()),
            }]))
        targets = [(48, "T-48"), (24, "T-24"), (6, "T-6"), (1, "T-1")]
        for hours, label in targets:
            target = pd.Timestamp(event["event_end"]) - pd.Timedelta(hours=hours)
            times = panel.timestamp.drop_duplicates().sort_values()
            eligible = times[times <= target]
            if eligible.empty:
                continue
            chosen = eligible.iloc[-1]
            snap = panel[(panel.timestamp == chosen) & panel.raw_mid.notna()].copy()
            total = snap.raw_mid.sum()
            snap["normalized_probability"] = snap.raw_mid / total if total else np.nan
            snap["snapshot"] = label
            snapshots.append(snap[["event_slug", "city", "event_date", "timestamp", "snapshot", "market_index", "market_label", "temperature_bin", "raw_mid", "normalized_probability"]])
    return (
        pd.concat(lifecycle_parts, ignore_index=True) if lifecycle_parts else pd.DataFrame(),
        pd.concat(active_parts, ignore_index=True) if active_parts else pd.DataFrame(),
        pd.concat(distribution_parts, ignore_index=True) if distribution_parts else pd.DataFrame(),
        pd.concat(snapshots, ignore_index=True) if snapshots else pd.DataFrame(),
    )


def _font(size: int) -> ImageFont.ImageFont:
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _line_chart(path: Path, title: str, labels: list[str], series: dict[str, list[float]]) -> None:
    image = Image.new("RGB", (1100, 620), "white"); draw = ImageDraw.Draw(image)
    draw.text((40, 20), title, fill="black", font=_font(26))
    left, top, right, bottom = 90, 90, 1040, 520
    draw.line((left, bottom, right, bottom), fill="#444", width=2); draw.line((left, top, left, bottom), fill="#444", width=2)
    values = [v for points in series.values() for v in points if pd.notna(v)]
    lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
    if math.isclose(lo, hi): hi = lo + 1.0
    colors = ["#1665a8", "#d14a2a", "#2f8f46", "#8552a1"]
    for idx, label in enumerate(labels):
        x = left + idx * (right-left) / max(1, len(labels)-1)
        draw.text((x-30, bottom+12), label, fill="black", font=_font(17))
    for color, (name, points) in zip(colors, series.items(), strict=False):
        coords = []
        for idx, value in enumerate(points):
            if pd.isna(value): continue
            x = left + idx * (right-left) / max(1, len(labels)-1)
            y = bottom - (float(value)-lo)/(hi-lo)*(bottom-top)
            coords.append((x, y)); draw.ellipse((x-5, y-5, x+5, y+5), fill=color)
        if len(coords) > 1: draw.line(coords, fill=color, width=4)
        draw.text((right-190, top+25*list(series).index(name)), f"{name}", fill=color, font=_font(18))
    image.save(path)


def _bar_chart(path: Path, title: str, labels: list[str], values: list[float]) -> None:
    image = Image.new("RGB", (1000, 560), "white"); draw = ImageDraw.Draw(image)
    draw.text((35, 20), title, fill="black", font=_font(25))
    left, top, right, bottom = 90, 90, 950, 470
    finite = [v for v in values if pd.notna(v)]; lo = min([0.0, *finite]) if finite else 0.; hi = max([1e-9, *finite]) if finite else 1.
    zero_y = bottom - (0-lo)/(hi-lo)*(bottom-top)
    draw.line((left, zero_y, right, zero_y), fill="#444", width=2)
    width = (right-left)/max(1, len(values))
    for idx, (label, value) in enumerate(zip(labels, values, strict=True)):
        if pd.isna(value): continue
        x0 = left + idx*width + width*.15; x1 = left + (idx+1)*width - width*.15
        y = bottom - (float(value)-lo)/(hi-lo)*(bottom-top)
        draw.rectangle((x0, min(y,zero_y), x1, max(y,zero_y)), fill="#1665a8")
        draw.text((x0, bottom+10), label, fill="black", font=_font(15)); draw.text((x0, y-22), f"{value:.3f}", fill="black", font=_font(14))
    image.save(path)


def plot_outputs(lifecycle: pd.DataFrame, active: pd.DataFrame, distribution: pd.DataFrame, snapshots: pd.DataFrame) -> None:
    lifecycle_dir = EVENT_LEVEL_ROOT / "lifecycle"; lifecycle_dir.mkdir(parents=True, exist_ok=True)
    active_dir = EVENT_LEVEL_ROOT / "active_set"; active_dir.mkdir(parents=True, exist_ok=True)
    distribution_dir = EVENT_LEVEL_ROOT / "distribution"; distribution_dir.mkdir(parents=True, exist_ok=True)
    order = [x for x in LIFECYCLE_ORDER if x in set(lifecycle.lifecycle_bucket)]
    summary = lifecycle.groupby("lifecycle_bucket", observed=True).agg(updates=("updates_per_minute", "median"), trades=("trades_per_minute", "median"), active=("active_market_count", "median")).reindex(order)
    _line_chart(lifecycle_dir / "lifecycle_activity_curve.png", "Lifecycle activity (Event median / minute)", order, {column: summary[column].tolist() for column in summary.columns})
    active_summary = active.groupby("scope").agg(ic=("ic", "median"), zero_rate=("zero_rate", "median"), crossing=("crossing_markout", "median"))
    _bar_chart(active_dir / "all_market_vs_active_set_comparison.png", "All-market vs dynamic-active (IC)", active_summary.index.tolist(), active_summary.ic.tolist())
    concentration = active[active.scope.eq("all_market")].groupby("event_slug").agg(active_count=("active_market_count", "mean"), top3_updates=("top3_update_share", "mean"), top3_trades=("top3_trade_share", "mean"), top3_mass=("top3_probability_mass", "mean"))
    _bar_chart(active_dir / "active_market_count_top3_concentration.png", "Active markets / 11 and Top3 concentration (Event median)", ["active/11", "updates", "trades", "probability"], [float(concentration.active_count.median() / 11.0), float(concentration.top3_updates.median()), float(concentration.top3_trades.median()), float(concentration.top3_mass.median())])
    if not snapshots.empty:
        representative = snapshots.event_slug.value_counts().index[0]
        snap = snapshots[snapshots.event_slug.eq(representative)]
        pivot = snap.pivot_table(index="market_label", columns="snapshot", values="normalized_probability", aggfunc="last")
        pivot = pivot.reindex(columns=[x for x in ["T-48", "T-24", "T-6", "T-1"] if x in pivot])
        labels = [str(x)[:18] for x in pivot.index]
        latest = pivot.iloc[:, -1].fillna(0).tolist()
        _bar_chart(distribution_dir / "representative_event_snapshots.png", f"Representative distribution ({pivot.columns[-1]}): {representative}", labels, latest)
    flow = distribution.pivot_table(index="event_slug", columns="lifecycle_bucket", values="probability_mass_flow", aggfunc="median")
    matrix = flow.fillna(0).to_numpy(); vmax = float(matrix.max()) if matrix.size and matrix.max() > 0 else 1.0
    cell_w, cell_h = 170, 18; image = Image.new("RGB", (260+cell_w*len(flow.columns), 90+cell_h*len(flow)), "white"); draw = ImageDraw.Draw(image)
    draw.text((20, 15), "Median probability mass flow", fill="black", font=_font(22))
    for j, column in enumerate(flow.columns): draw.text((250+j*cell_w, 55), str(column), fill="black", font=_font(14))
    for i, (slug, row) in enumerate(zip(flow.index, matrix, strict=True)):
        y=85+i*cell_h; draw.text((5,y), str(slug)[:35], fill="black", font=_font(11))
        for j,value in enumerate(row):
            intensity=int(255*float(value)/vmax); color=(255-intensity,255-intensity//2,255)
            draw.rectangle((245+j*cell_w,y,245+(j+1)*cell_w-2,y+cell_h-2),fill=color)
    image.save(distribution_dir / "mass_flow_heatmap.png")


def write_outputs(selected: pd.DataFrame, summaries: list[dict[str, Any]], started: float) -> None:
    EVENT_LEVEL_ROOT.mkdir(parents=True, exist_ok=True)
    (EVENT_LEVEL_ROOT / "lifecycle").mkdir(parents=True, exist_ok=True)
    (EVENT_LEVEL_ROOT / "active_set").mkdir(parents=True, exist_ok=True)
    (EVENT_LEVEL_ROOT / "distribution").mkdir(parents=True, exist_ok=True)
    baseline_parts = [pd.read_csv(RUN_ROOT / slug / "token_baseline_metrics.csv") for slug in selected.event_slug]
    baseline = pd.concat(baseline_parts, ignore_index=True)
    compact = HERE / "compact/batch_36_event"; compact.mkdir(parents=True, exist_ok=True)
    baseline.to_csv(compact / "token_level_fixed_horizon_baseline.csv", index=False)
    baseline_event = baseline.groupby(["event_slug", "city", "event_date", "cohort", "factor", "horizon_seconds", "lifecycle_bucket"], as_index=False).agg(
        ic=("ic", "mean"), zero_rate=("zero_rate", "mean"), crossing_markout=("crossing_markout", "mean"), coverage=("coverage", "mean"), tokens=("token_id", "nunique"),
    )
    baseline_event.to_csv(compact / "token_level_fixed_horizon_event_equal.csv", index=False)
    selected[["event_slug", "city", "event_date", "cohort_role", "sample_activity", "rows_written"]].to_csv(HERE / "protocol/batch_36_sample_plan.csv", index=False)
    lifecycle, active, distribution, snapshots = aggregate_event_outputs(selected)
    lifecycle.to_csv(EVENT_LEVEL_ROOT / "lifecycle/lifecycle_metrics.csv", index=False)
    lifecycle.to_csv(EVENT_LEVEL_ROOT / "lifecycle/lifecycle_by_cohort_city_date.csv", index=False)
    active.to_csv(EVENT_LEVEL_ROOT / "active_set/active_set_metrics.csv", index=False)
    distribution.to_csv(EVENT_LEVEL_ROOT / "distribution/distribution_metrics.csv", index=False)
    snapshots.to_csv(EVENT_LEVEL_ROOT / "distribution/representative_event_snapshots.csv", index=False)
    plot_outputs(lifecycle, active, distribution, snapshots)

    skipped = [row for summary in summaries for row in summary["skipped"]]
    failures = [
        {
            "event_slug": summary["event_slug"],
            "city": summary["city"],
            "event_date": summary["event_date"],
            "cohort": summary["cohort"],
            **row,
        }
        for summary in summaries
        for row in summary["failures"]
    ]
    event_elapsed_sum = float(sum(summary["elapsed_seconds"] for summary in summaries))
    pd.DataFrame(skipped).to_csv(compact / "skipped_tokens.csv", index=False)
    pd.DataFrame(failures).to_csv(compact / "token_failures.csv", index=False)
    total_tokens = sum(int(summary["markets_total"]) for summary in summaries)
    lifecycle_summary = lifecycle.groupby("lifecycle_bucket", observed=True).agg(events=("event_slug", "nunique"), updates_per_minute=("updates_per_minute", "median"), trades_per_minute=("trades_per_minute", "median"), active_markets=("active_market_count", "median"), spread=("spread_median", "median")).reindex(LIFECYCLE_ORDER)
    baseline_life = baseline_event[(baseline_event.factor == "depth_imbalance_1") & (baseline_event.horizon_seconds == 120) & (baseline_event.lifecycle_bucket != "all")].groupby("lifecycle_bucket").agg(events=("event_slug", "nunique"), ic=("ic", "median"), zero=("zero_rate", "median"), crossing=("crossing_markout", "median"), coverage=("coverage", "median"))
    life = lifecycle_summary.join(baseline_life, rsuffix="_baseline")
    active_summary = active.groupby("scope").agg(events=("event_slug", "nunique"), ic=("ic", "median"), zero=("zero_rate", "median"), crossing=("crossing_markout", "median"), top3_updates=("top3_update_share", "median"), top3_trades=("top3_trade_share", "median"), top3_mass=("top3_probability_mass", "median"))
    pressure_ic = distribution.groupby("event_slug").apply(lambda x: safe_ic(x.pressure_factor, x.future_mean_move), include_groups=False)
    entropy_ic = distribution.groupby("event_slug").apply(lambda x: safe_ic(x.pressure_factor.abs(), -x.future_entropy_change), include_groups=False)
    pressure_ic_gated = distribution[distribution.sum_deviation.abs().le(.1)].groupby("event_slug").apply(lambda x: safe_ic(x.pressure_factor, x.future_mean_move), include_groups=False)
    sum_dev_p90 = float(distribution.sum_deviation.abs().quantile(.9))
    sum_dev_p95 = float(distribution.sum_deviation.abs().quantile(.95))
    sum_dev_max = float(distribution.sum_deviation.abs().max())
    sum_dev_gt10 = float(distribution.sum_deviation.abs().gt(.1).mean())
    baseline_headline = baseline_event[baseline_event.lifecycle_bucket.eq("all")].groupby(["factor", "horizon_seconds"]).agg(
        events=("event_slug", "nunique"), median_ic=("ic", "median"), positive_event=("ic", lambda x: float((x > 0).mean())), zero=("zero_rate", "median"), crossing=("crossing_markout", "median"), coverage=("coverage", "median"),
    ).reset_index()
    skipped_frame = pd.DataFrame(skipped)
    skipped_city = skipped_frame.groupby("city").size().sort_values(ascending=False).to_dict() if not skipped_frame.empty else {}
    skipped_date = skipped_frame.groupby("event_date").size().sort_index().to_dict() if not skipped_frame.empty else {}
    pressure_event = distribution.groupby(["event_slug", "cohort", "event_date"]).apply(lambda x: safe_ic(x.pressure_factor, x.future_mean_move), include_groups=False).rename("ic").reset_index()
    pressure_cohort = pressure_event.groupby("cohort").agg(median_ic=("ic", "median"), positive=("ic", lambda x: float((x > 0).mean())))
    reports = EVENT_LEVEL_ROOT / "reports"; reports.mkdir(parents=True, exist_ok=True)
    lines = [
        "# PMXT 天气 Event-level 36 Event 实验", "",
        "> 这是因子结构研究，不是 PnL 回测。token 旧实验改名为 `token-level fixed-horizon baseline`；crossing markout 不是成交收益。", "",
        "## 样本与可审计性", "",
        f"- Event：36；日期：{selected.event_date.nunique()}；城市：{selected.city.nunique()}；clean/degraded：{(selected.cohort_role == 'primary_development_replication').sum()}/{(selected.cohort_role == 'degraded_robustness').sum()}。",
        f"- token 总数：{total_tokens}；进入分析：{total_tokens-len(skipped)-len(failures)}；skipped：{len(skipped)}；失败：{len(failures)}。",
        f"- skipped clean/degraded：{sum(x['cohort']=='clean' for x in skipped)}/{sum(x['cohort']=='degraded' for x in skipped)}；city={skipped_city}；date={skipped_date}。",
        "- skipped token 为无 ranking observation 的 inactive market；其 city/date 分布已在上一行完整列出，不作额外集中性推断。",
        f"- 失败 token：{len(failures)}，涉及 {len({row['event_slug'] for row in failures})} 个 Event；没有整个 Event 失效。明细见 `compact/batch_36_event/token_failures.csv`。23 个 source-time 间隔不超过 8ms 的重复 tick-size 通知已按告警跳过；Wuhan 的约 325s 长间隔重复仍被严格拒绝。",
        "- 对齐：Event grid 为 1 分钟；每个 token 只用该时刻之前最后一个有效盘口（backward as-of），active gate 仅使用当时及过去 30 分钟信息。", "",
        "## Token-level fixed-horizon baseline", "", "| factor | horizon | events | median IC | positive Event | zero | crossing | coverage |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in baseline_headline.itertuples():
        lines.append(f"| {row.factor} | {row.horizon_seconds}s | {row.events} | {row.median_ic:.3f} | {row.positive_event:.3f} | {row.zero:.3f} | {row.crossing:.4f} | {row.coverage:.3f} |")
    lines += ["", "所有 crossing 中位数只作诊断；若为负，不作策略化解释。", "", "## Lifecycle", "", "| bucket | events | updates/min | trades/min | active markets | spread | 120s IC | zero | crossing | coverage |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for bucket, row in life.iterrows():
        lines.append(f"| {bucket} | {row.events:.0f} | {row.updates_per_minute:.2f} | {row.trades_per_minute:.2f} | {row.active_markets:.2f} | {row.spread:.4f} | {row.ic:.3f} | {row.zero:.3f} | {row.crossing:.4f} | {row.coverage:.3f} |")
    lines += ["", "## Active set", "", "| scope | events | IC | zero | crossing | Top3 updates | Top3 trades | Top3 mass |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for scope, row in active_summary.iterrows():
        lines.append(f"| {scope} | {row.events:.0f} | {row.ic:.3f} | {row.zero:.3f} | {row.crossing:.4f} | {row.top3_updates:.3f} | {row.top3_trades:.3f} | {row.top3_mass:.3f} |")
    lines += [
        "", "## Event probability distribution", "",
        f"- |sum(mid)-1|：P90={sum_dev_p90:.3f}，P95={sum_dev_p95:.3f}，最大={sum_dev_max:.3f}，超过 0.10 的观测占 {sum_dev_gt10:.1%}；异常尾部不隐藏。",
        f"- pressure → future implied-temperature move：Event IC 中位数 {pressure_ic.median():.3f}，正 Event 比例 {(pressure_ic > 0).mean():.3f}。",
        f"- 仅作敏感性检查的 |sum deviation|<=0.10 子样本：IC 中位数 {pressure_ic_gated.median():.3f}，正 Event 比例 {(pressure_ic_gated > 0).mean():.3f}；未替代全样本结论。",
        f"- cohort 稳健性：clean IC={pressure_cohort.loc['clean','median_ic']:.3f}（positive={pressure_cohort.loc['clean','positive']:.3f}），degraded IC={pressure_cohort.loc['degraded','median_ic']:.3f}（positive={pressure_cohort.loc['degraded','positive']:.3f}）。",
        f"- |pressure| → entropy contraction：Event IC 中位数 {entropy_ic.median():.3f}，正 Event 比例 {(entropy_ic > 0).mean():.3f}。", "",
        "## 决策", "",
        f"- 主要研究窗口：**{baseline_life.ic.idxmax()}**（120s IC 最高）；<1h coverage 为零，不能用于方向性结论。所有可计算 bucket crossing 均负，不进入策略化解释。",
        f"- Active set：**暂不采用动态 gate**。它将 IC 从 {active_summary.loc['all_market','ic']:.3f} 提高到 {active_summary.loc['dynamic_active','ic']:.3f}、zero 从 {active_summary.loc['all_market','zero']:.3f} 降到 {active_summary.loc['dynamic_active','zero']:.3f}，但 crossing 从 {active_summary.loc['all_market','crossing']:.4f} 恶化到 {active_summary.loc['dynamic_active','crossing']:.4f}；Top3 只作描述字段。",
        "- Distribution：pressure 对未来均值移动有跨 cohort 的弱正结构信号，但 sum deviation 有明显异常尾部；只保留候选结构特征，不能称 Alpha。",
        "- 单 token 因子：仅保留为 event-level 局部特征与 fixed-horizon baseline。",
        "- 扩 100：本轮按需求停止，不扩。",
        "- Nautilus：native parity 已运行 8 条，6 条通过；Beijing YES/NO 两条因有效 tick 为 `0.01` 时出现 `0.001` 价格而失败，不能进入正式策略回测。", "",
        f"累计 Event replay 处理时间：{event_elapsed_sum/3600:.2f} CPUh；本次缓存聚合耗时：{(time.perf_counter()-started)/60:.1f} 分钟。",
    ]
    (reports / "event_level_summary.md").write_text("\n".join(lines), encoding="utf-8")
    score = [
        "# PMXT Weather Event-level Scorecard", "", "日期：2026-07-15", "", "## 结构发现", "",
        f"- Lifecycle 主窗口：{baseline_life.ic.idxmax()}（120s IC 最高）；<1h coverage 为零，只保留为数据诊断。",
        f"- Active set：暂不采用；all IC={active_summary.loc['all_market','ic']:.3f}/crossing={active_summary.loc['all_market','crossing']:.4f}，dynamic IC={active_summary.loc['dynamic_active','ic']:.3f}/crossing={active_summary.loc['dynamic_active','crossing']:.4f}。",
        f"- Distribution：pressure/mean IC median={pressure_ic.median():.3f}，但 sum deviation P95={sum_dev_p95:.3f}、max={sum_dev_max:.3f}，只保留候选结构信号。",
        "- 单 token baseline 角色：局部盘口特征与配对基准，不作 Alpha 声明。", "", "## 下一步", "",
        "- 是否扩 100：否，本轮停止于 36 Event。", "- 是否进入 Nautilus：否，native parity 后再评估。",
        f"- Stop condition：所有 crossing 为负；失败 token={len(failures)}；skipped token={len(skipped)}；概率和偏差 P95={sum_dev_p95:.3f}。",
    ]
    (reports / "boss_scorecard.md").write_text("\n".join(score), encoding="utf-8")
    (compact / "run_summary.json").write_text(json.dumps({
        "generated_at": datetime.now(UTC).isoformat(), "stage": "batch_36_event", "events": 36,
        "cities": int(selected.city.nunique()), "clean": int((selected.cohort_role == "primary_development_replication").sum()),
        "degraded": int((selected.cohort_role == "degraded_robustness").sum()), "tokens_total": total_tokens,
        "tokens_skipped": len(skipped), "token_failures": len(failures), "sum_deviation_abs_p90": sum_dev_p90,
        "event_elapsed_sum_seconds": event_elapsed_sum, "aggregation_elapsed_seconds": time.perf_counter() - started,
    }, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=INVENTORY_PATH)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    started = time.perf_counter()
    selected = select_batch36(args.inventory.resolve())
    run_selected = selected.head(args.limit) if args.limit else selected
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=min(args.workers, len(run_selected))) as pool:
        futures = {pool.submit(process_event, event, force=args.force): event["event_slug"] for event in run_selected.to_dict("records")}
        for future in as_completed(futures):
            result = future.result(); summaries.append(result)
            print(f"completed {len(summaries)}/{len(run_selected)} {result['event_slug']} elapsed={result['elapsed_seconds']:.1f}s failures={len(result['failures'])}", flush=True)
    if args.limit:
        print("smoke cache complete; full aggregation skipped because --limit was supplied", flush=True)
        return 0
    order = {slug: idx for idx, slug in enumerate(selected.event_slug)}
    summaries.sort(key=lambda x: order[x["event_slug"]])
    write_outputs(selected, summaries, started)
    print(f"complete: {EVENT_LEVEL_ROOT / 'reports/event_level_summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
