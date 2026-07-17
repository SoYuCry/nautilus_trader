"""Run the preregistered six-event PMXT weather factor pilot.

This is intentionally one thin research script. It reuses the existing PMXT
adapter and Wave0 factor protocol, keeps only compact metrics, and produces the
four management charts plus Chinese reports required by EXPERIMENT_REQUIREMENTS.md.
It is factor research, not an execution/PnL backtest.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PROTOCOL_PATH = REPO_ROOT / "polymarket/research/2026-07-14-pmxt-weather-factor-wave0/factor_protocol.py"
PARITY_PATH = REPO_ROOT / "polymarket/research/2026-07-13-pmxt-wave-minus1-ordering-parity/ordering_parity.py"
INVENTORY_PATH = REPO_ROOT / "polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/event_inventory.parquet"
MAIN_FACTORS = ("depth_imbalance_1", "microprice_minus_mid")
APPENDIX_FACTORS = (
    "depth_imbalance_1", "depth_imbalance_3", "depth_imbalance_5",
    "microprice_minus_mid", "spread", "top_level_depth", "depth_slope",
    "depth_concentration", "bid_ask_liquidity_asymmetry",
    "distance_to_zero_one", "tick_size_regime",
)
MAIN_HORIZONS = (30, 120, 600)
ALL_HORIZONS = (30, 60, 120, 300, 600, 900)
VALID_OBS = (10, 50, 200)
PRICE_BUCKETS = ("[0,0.04)", "[0.04,0.20)", "[0.20,0.80]", "(0.80,0.96]", "(0.96,1]")


def load_protocol() -> Any:
    name = "pmxt_weather_next_stage_protocol"
    spec = importlib.util.spec_from_file_location(name, PROTOCOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load factor protocol: {PROTOCOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_parity_module() -> Any:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    name = "pmxt_weather_next_stage_parity"
    spec = importlib.util.spec_from_file_location(name, PARITY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load ordering parity: {PARITY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def safe_ic(factor: pd.Series, label: pd.Series) -> float:
    valid = pd.DataFrame({"factor": factor, "label": label}).dropna()
    if len(valid) < 2 or valid.factor.nunique() < 2 or valid.label.nunique() < 2:
        return math.nan
    value = valid.factor.rank(method="average").corr(valid.label.rank(method="average"))
    return float(value) if pd.notna(value) else math.nan


def select_pilot(inventory_path: Path) -> list[dict[str, Any]]:
    frame = pd.read_parquet(inventory_path)
    selected: list[pd.Series] = []
    # Pilot validates the path, not population statistics. Select the three
    # smallest distinct-city events from each quality cohort before looking at
    # factor results; activity is measured from each observation, not row count.
    for role in ("primary_development_replication", "degraded_robustness"):
        cohort = frame[frame.cohort_role.eq(role)].sort_values(["rows_written", "event_slug"], kind="mergesort")
        for _, row in cohort.iterrows():
            if str(row.city) in {str(item.city) for item in selected}:
                continue
            selected.append(row)
            if sum(item.cohort_role == role for item in selected) == 3:
                break
    return [row.to_dict() for row in selected]


def activity_bucket(values: pd.Series) -> pd.Series:
    # book_update_intensity is mutations/second over the trailing 30 seconds.
    return pd.cut(values, [-np.inf, 0.1, 1.0, np.inf], labels=["low", "medium", "high"], right=False)


def price_bucket(values: pd.Series) -> pd.Series:
    return pd.cut(
        values,
        [-np.inf, 0.04, 0.20, 0.80, 0.96, np.inf],
        labels=PRICE_BUCKETS,
        right=False,
        include_lowest=True,
    )


def lifecycle_bucket(timestamps: pd.Series, event_end: Any) -> pd.Series:
    end = pd.Timestamp(event_end)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    hours = (end - pd.to_datetime(timestamps, utc=True)).dt.total_seconds() / 3600
    return pd.cut(hours, [-np.inf, 1, 6, 24, np.inf], labels=["near", "late", "mid", "early"], right=True)


def top_bottom(frame: pd.DataFrame, factor: str, label: str) -> float:
    valid = frame[[factor, label]].dropna()
    if len(valid) < 10 or valid[factor].nunique() < 2:
        return math.nan
    q = pd.qcut(valid[factor], 5, labels=False, duplicates="drop")
    means = valid.assign(_q=q).dropna(subset=["_q"]).groupby("_q", observed=True)[label].mean()
    return float(means.iloc[-1] - means.iloc[0]) if len(means) >= 2 else math.nan


def metric_row(frame: pd.DataFrame, *, event: dict[str, Any], token_id: str, factor: str,
               horizon: int, slice_name: str, slice_value: str) -> dict[str, Any]:
    label = f"future_mid_return_{horizon}s"
    valid = frame[[factor, label]].dropna()
    directional = valid[(valid[factor] != 0) & (valid[label] != 0)]
    coverage = len(valid) / len(frame) if len(frame) else math.nan
    crossing = math.nan
    future_mid = f"future_mid_{horizon}s"
    crossing_frame = frame[[factor, "bid1", "ask1", future_mid]].dropna() if future_mid in frame else pd.DataFrame()
    if not crossing_frame.empty:
        marks = np.where(
            crossing_frame[factor] > 0,
            crossing_frame[future_mid] - crossing_frame.ask1,
            np.where(crossing_frame[factor] < 0, crossing_frame.bid1 - crossing_frame[future_mid], np.nan),
        )
        crossing = float(np.nanmean(marks)) if np.isfinite(marks).any() else math.nan
    return {
        "event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]),
        "quality": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded",
        "token_id": token_id, "factor": factor, "horizon_seconds": horizon,
        "slice_name": slice_name, "slice_value": slice_value, "rows": len(frame),
        "valid_rows": len(valid), "coverage": coverage,
        "zero_rate": float((valid[label] == 0).mean()) if len(valid) else math.nan,
        "ic": safe_ic(valid[factor], valid[label]),
        "nonzero_hit_rate": float((directional[factor] * directional[label] > 0).mean()) if len(directional) else math.nan,
        "top_bottom": top_bottom(frame, factor, label), "crossing_markout_diagnostic": crossing,
    }


def valid_obs_rows(ranking: pd.DataFrame, *, event: dict[str, Any], token_id: str, factor: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered = ranking.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    for count in VALID_OBS:
        future_mid = ordered.mid.shift(-count)
        future_ts = ordered.timestamp.shift(-count)
        label = future_mid - ordered.mid
        elapsed = (pd.to_datetime(future_ts, utc=True) - pd.to_datetime(ordered.timestamp, utc=True)).dt.total_seconds()
        valid = pd.DataFrame({"factor": ordered[factor], "label": label, "elapsed": elapsed}).dropna()
        rows.append({
            "event_slug": event["event_slug"], "city": event["city"], "quality": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded",
            "token_id": token_id, "factor": factor, "valid_obs": count, "rows": len(ordered), "valid_rows": len(valid),
            "coverage": len(valid) / len(ordered) if len(ordered) else math.nan,
            "zero_rate": float((valid.label == 0).mean()) if len(valid) else math.nan,
            "ic": safe_ic(valid.factor, valid.label),
            "elapsed_p50": float(valid.elapsed.quantile(0.5)) if len(valid) else math.nan,
            "elapsed_p90": float(valid.elapsed.quantile(0.9)) if len(valid) else math.nan,
        })
    return rows


def run_event(event: dict[str, Any]) -> dict[str, Any]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter

    started = time.perf_counter()
    protocol = load_protocol()
    event_dir = Path(event["paths"]["event_dir"])
    event_index = json.loads((event_dir / "event_index.json").read_text(encoding="utf-8"))
    adapter = PMXTEventV1Adapter(repo_root=REPO_ROOT)
    metrics: list[dict[str, Any]] = []
    valid_metrics: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    skipped_tokens: list[dict[str, str]] = []
    token_count = 0
    for market in event_index["markets"]:
        token_id = str(market["yesToken"])
        try:
            dataset = adapter.load({"input": {"event_dir": str(event_dir), "condition_id": str(market["conditionId"]), "asset_id": token_id, "dataset_id": str(event["event_slug"])}})
            panel = protocol.build_factor_panel(dataset, horizons_seconds=ALL_HORIZONS, include_labels=True)
            ranking = panel[panel.ranking_observation].copy()
            if ranking.empty:
                skipped_tokens.append({"market_label": str(market.get("label", market.get("index"))), "token_id": token_id, "reason": "no ranking observations"})
                continue
            ranking["activity_bucket"] = activity_bucket(ranking.book_update_intensity)
            ranking["price_bucket"] = price_bucket(ranking.mid)
            ranking["lifecycle_bucket"] = lifecycle_bucket(ranking.timestamp, event["event_end"])
            ranking["spread_bucket"] = pd.qcut(ranking.spread.rank(method="first"), 3, labels=["tight", "medium", "wide"], duplicates="drop")
            token_count += 1
            for factor in APPENDIX_FACTORS:
                for horizon in MAIN_HORIZONS:
                    metrics.append(metric_row(ranking, event=event, token_id=token_id, factor=factor, horizon=horizon, slice_name="overall", slice_value="all"))
            for factor in MAIN_FACTORS:
                valid_metrics.extend(valid_obs_rows(ranking, event=event, token_id=token_id, factor=factor))
                for horizon in ALL_HORIZONS:
                    for column in ("activity_bucket", "price_bucket", "lifecycle_bucket", "spread_bucket"):
                        for value, group in ranking.groupby(column, observed=True):
                            metrics.append(metric_row(group, event=event, token_id=token_id, factor=factor, horizon=horizon, slice_name=column, slice_value=str(value)))
        except Exception as exc:
            failures.append({"market_label": str(market.get("label", market.get("index"))), "token_id": token_id, "error": repr(exc)})
    return {"event_slug": event["event_slug"], "metrics": metrics, "valid_obs": valid_metrics, "failures": failures, "skipped_tokens": skipped_tokens,
            "token_count": token_count, "elapsed_seconds": time.perf_counter() - started}


def run_direct_native_parity(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        parity = load_parity_module()
    except Exception as exc:
        return [{"status": "not_run", "reason": "Nautilus compiled extension unavailable", "error": repr(exc)}]
    records: list[dict[str, Any]] = []
    for event in sorted(selected, key=lambda item: int(item["rows_written"]))[:4]:
        event_dir = Path(event["paths"]["event_dir"])
        index = json.loads((event_dir / "event_index.json").read_text(encoding="utf-8"))
        market = index["markets"][0]
        for leg, field in (("YES", "yesToken"), ("NO", "noToken")):
            asset_id = str(market[field])
            row = {
                "event_slug": event["event_slug"],
                "leg": leg,
                "condition_id": str(market["conditionId"]),
                "asset_id": asset_id,
                "parity_scope": "independent ordering and native convertibility smoke",
                "semantic_output_equality_checked": False,
            }
            try:
                dataset = parity.load_pmxt_event(event_dir, condition_id=row["condition_id"], asset_id=asset_id)
                direct = parity.o1_records(dataset)
                native = parity.convert_o3(dataset, asset_id=asset_id)
                direct_ok = all(direct[i]["replay_timestamp"] <= direct[i + 1]["replay_timestamp"] for i in range(len(direct) - 1))
                native_ok = native["records"] == sorted(native["records"], key=lambda item: item["ts_init"])
                row |= {"status": "pass" if direct_ok and native_ok else "fail", "direct_steps": len(direct), "native_counts": native["counts"],
                        "direct_replay_order_monotonic": direct_ok, "native_ts_init_monotonic": native_ok,
                        "skipped_native_updates": len(native["skipped_updates"])}
            except Exception as exc:
                row |= {"status": "fail", "error": repr(exc)}
            records.append(row)
    return records


def event_equal(token_metrics: pd.DataFrame) -> pd.DataFrame:
    keys = ["event_slug", "city", "quality", "factor", "horizon_seconds", "slice_name", "slice_value"]
    return token_metrics.groupby(keys, observed=True, dropna=False).agg(
        token_count=("token_id", "nunique"), coverage=("coverage", "mean"), zero_rate=("zero_rate", "mean"),
        ic=("ic", "mean"), nonzero_hit_rate=("nonzero_hit_rate", "mean"), top_bottom=("top_bottom", "mean"),
        crossing_markout_diagnostic=("crossing_markout_diagnostic", "mean"),
    ).reset_index()


def bootstrap_ci(values: pd.Series, seed: int = 20260715) -> tuple[float, float]:
    x = values.dropna().to_numpy(float)
    if len(x) < 2:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(x, size=len(x), replace=True).mean() for _ in range(2000)])
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def headline(event_metrics: pd.DataFrame) -> pd.DataFrame:
    primary = event_metrics[(event_metrics.slice_name == "overall") & event_metrics.factor.isin(MAIN_FACTORS) & event_metrics.horizon_seconds.isin(MAIN_HORIZONS)]
    rows = []
    for (factor, horizon), group in primary.groupby(["factor", "horizon_seconds"]):
        lo, hi = bootstrap_ci(group.ic)
        rows.append({"factor": factor, "horizon_seconds": horizon, "events": group.event_slug.nunique(),
                     "coverage": group.coverage.mean(), "zero_rate": group.zero_rate.mean(), "median_event_ic": group.ic.median(),
                     "positive_event_rate": (group.ic > 0).mean(), "median_top_bottom": group.top_bottom.median(),
                     "bootstrap_mean_ic_low": lo, "bootstrap_mean_ic_high": hi})
    return pd.DataFrame(rows)


def activity_summary(event_metrics: pd.DataFrame) -> pd.DataFrame:
    activity = event_metrics[(event_metrics.slice_name == "activity_bucket") & event_metrics.factor.isin(MAIN_FACTORS) & event_metrics.horizon_seconds.isin(MAIN_HORIZONS)]
    out = activity.groupby(["factor", "horizon_seconds", "slice_value"], observed=True).agg(
        events=("event_slug", "nunique"), ic=("ic", "mean"), coverage=("coverage", "mean"), zero_rate=("zero_rate", "mean")
    ).reset_index().rename(columns={"slice_value": "activity"})
    return out


def _write_heatmap(path: Path, title: str, pivot: pd.DataFrame) -> None:
    left, top, cell_w, cell_h = 190, 70, 150, 45
    width, height = max(760, left + len(pivot.columns) * cell_w + 30), max(340, top + len(pivot.index) * cell_h + 40)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">', '<rect width="100%" height="100%" fill="white"/>', f'<text x="20" y="30" font-size="20" font-family="sans-serif">{title}</text>']
    for j, column in enumerate(pivot.columns):
        parts.append(f'<text x="{left+j*cell_w+cell_w/2}" y="55" text-anchor="middle" font-size="13" font-family="sans-serif">{column}</text>')
    for i, index in enumerate(pivot.index):
        parts.append(f'<text x="{left-10}" y="{top+i*cell_h+28}" text-anchor="end" font-size="12" font-family="sans-serif">{index}</text>')
        for j in range(len(pivot.columns)):
            value = pivot.iloc[i, j]
            normalized = 0.5 if pd.isna(value) else min(1.0, max(0.0, (float(value) + .2) / .4))
            red = int(255 * (1 - normalized)); blue = int(255 * normalized); green = int(210 - abs(normalized - .5) * 220)
            x, y = left + j * cell_w, top + i * cell_h
            parts += [f'<rect x="{x}" y="{y}" width="{cell_w-2}" height="{cell_h-2}" fill="rgb({red},{green},{blue})"/>', f'<text x="{x+cell_w/2}" y="{y+28}" text-anchor="middle" font-size="13" font-family="monospace">{"NA" if pd.isna(value) else f"{value:.3f}"}</text>']
    parts.append('</svg>'); path.write_text("\n".join(parts), encoding="utf-8")


def _write_scatter(path: Path, title: str, frame: pd.DataFrame) -> None:
    width, height, left, top, plot_w, row_h = 900, max(340, 90 + len(frame) * 20), 350, 55, 500, 20
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">', '<rect width="100%" height="100%" fill="white"/>', f'<text x="20" y="28" font-size="20" font-family="sans-serif">{title}</text>', f'<line x1="{left+plot_w/2}" y1="{top}" x2="{left+plot_w/2}" y2="{height-25}" stroke="#777"/>']
    for i, row in enumerate(frame.itertuples()):
        y = top + i * row_h + 12; value = float(row.ic) if pd.notna(row.ic) else 0.0; x = left + plot_w * min(1, max(0, (value + .3) / .6))
        parts += [f'<text x="{left-10}" y="{y+4}" text-anchor="end" font-size="10" font-family="sans-serif">{row.event_slug[:43]} | {row.factor[:8]}</text>', f'<circle cx="{x}" cy="{y}" r="4" fill="#1665a8"/>']
    parts.append('</svg>'); path.write_text("\n".join(parts), encoding="utf-8")


def make_charts(event_metrics: pd.DataFrame, valid_metrics: pd.DataFrame, charts: Path) -> None:
    charts.mkdir(parents=True, exist_ok=True)
    activity = activity_summary(event_metrics)
    pivot = activity.pivot_table(index="activity", columns=["factor", "horizon_seconds"], values="ic", observed=True).reindex(["low", "medium", "high"])
    pivot.columns = [f"{factor[:8]} {horizon}s" for factor, horizon in pivot.columns]
    _write_heatmap(charts / "01_e1_activity_label_heatmap.svg", "E1 Activity × Label IC", pivot)

    forest = event_metrics[(event_metrics.slice_name == "overall") & event_metrics.factor.isin(MAIN_FACTORS) & event_metrics.horizon_seconds.eq(120)].sort_values(["factor", "ic"])
    _write_scatter(charts / "02_event_ic_forest.svg", "120s Event-level IC", forest)

    price = event_metrics[(event_metrics.slice_name == "price_bucket") & event_metrics.factor.isin(MAIN_FACTORS) & event_metrics.horizon_seconds.eq(120)]
    pivot = price.groupby(["slice_value", "factor"]).ic.mean().unstack().reindex(PRICE_BUCKETS)
    _write_heatmap(charts / "03_e2_price_bucket_heatmap.svg", "E2 120s Price Bucket IC", pivot)

    valid_pivot = valid_metrics.groupby(["valid_obs", "factor"]).zero_rate.mean().unstack().reindex(VALID_OBS)
    _write_heatmap(charts / "04_valid_obs_zero_rate.svg", "valid_obs Diagnostic Zero Rate", valid_pivot)


def write_reports(output: Path, selected: list[dict[str, Any]], results: list[dict[str, Any]], head: pd.DataFrame,
                  activity: pd.DataFrame, event_metrics: pd.DataFrame, valid_metrics: pd.DataFrame,
                  parity: list[dict[str, Any]], wall: float) -> None:
    report_dir = output / "report"; report_dir.mkdir(parents=True, exist_ok=True)
    failures = sum(len(item["failures"]) for item in results)
    event_failures = sum(item["token_count"] == 0 for item in results)
    activity_diff = activity.groupby(["factor", "horizon_seconds"]).ic.agg(lambda x: x.max() - x.min()).reset_index(name="activity_ic_diff")
    merged = head.merge(activity_diff, on=["factor", "horizon_seconds"], how="left")
    merged["gate_pass"] = (merged.coverage >= .70) & (merged.zero_rate <= .80) & (merged.positive_event_rate >= .60) & (merged.activity_ic_diff <= .05)
    if merged.gate_pass.any():
        decision = "U0" if merged.loc[merged.gate_pass, "activity_ic_diff"].max() <= .05 else "U1"
    elif (merged.positive_event_rate >= .60).any():
        decision = "U1 / R1（pilot 仅诊断，进入 36-event 前需确认 regime）"
    else:
        decision = "Stop"
    parity_pass = bool(parity) and all(item["status"] == "pass" for item in parity)
    first_parity_failure = next((item for item in parity if item.get("status") != "pass"), None)
    if parity_pass:
        parity_text = "ORDERING SMOKE PASS：4 Event × YES/NO 共 8 条完成转换且两侧各自时间单调；未检查完整语义输出相等。"
    elif first_parity_failure and first_parity_failure["status"] == "not_run":
        parity_text = f"NOT RUN：{first_parity_failure.get('reason', first_parity_failure.get('error', 'unknown'))}；按预注册规则暂不扩张。"
    else:
        passed = sum(item.get("status") == "pass" for item in parity)
        parity_text = f"ORDERING SMOKE FAIL：{passed}/{len(parity)} 完成转换且两侧各自时间单调；未检查完整语义输出相等；{first_parity_failure.get('error', 'unknown') if first_parity_failure else 'unknown'}；按预注册规则暂不扩张。"
    lines = ["# PMXT 天气因子 6-event pilot", "", "> 因子研究，不是成交/PnL 回测；crossing markout 仅为诊断。主结论 Event 等权，置信区间按 Event bootstrap。", "", "## 样本与运行", "", "| event | city | quality | source rows | elapsed | token failures |", "| --- | --- | --- | ---: | ---: | ---: |"]
    by_slug = {x["event_slug"]: x for x in results}
    for event in selected:
        result = by_slug[event["event_slug"]]; quality = "clean" if event["cohort_role"] == "primary_development_replication" else "degraded"
        lines.append(f"| `{event['event_slug']}` | {event['city']} | {quality} | {int(event['rows_written']):,} | {result['elapsed_seconds']:.1f}s | {len(result['failures'])} |")
    skipped = sum(len(item["skipped_tokens"]) for item in results)
    lines += ["", f"- 总 wall time：{wall:.1f}s；event failure：{event_failures}/6；真实异常 token：{failures}；无有效 ranking observation 而跳过的 token：{skipped}。", "", "## E1 主因子", "", "| factor | horizon | coverage | zero | median event IC | positive event | top-bottom | activity IC diff | gate |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in merged.itertuples():
        lines.append(f"| {row.factor} | {row.horizon_seconds}s | {row.coverage:.3f} | {row.zero_rate:.3f} | {row.median_event_ic:.3f} | {row.positive_event_rate:.3f} | {row.median_top_bottom:.5f} | {row.activity_ic_diff:.3f} | {'PASS' if row.gate_pass else 'FAIL'} |")
    lines += ["", "## 基础因子表现（附录诊断）", "", "| factor | 120s median event IC | positive event |", "| --- | ---: | ---: |"]
    basics = event_metrics[(event_metrics.slice_name == "overall") & event_metrics.horizon_seconds.eq(120)].groupby("factor").agg(ic=("ic", "median"), positive=("ic", lambda x: (x > 0).mean())).sort_values("ic", ascending=False)
    for factor, row in basics.iterrows():
        lines.append(f"| {factor} | {row.ic:.3f} | {row.positive:.3f} |")
    lines += ["", "## E2 价格区间", "", "pilot 只有 6 个 Event，所有 price-bucket cell 均小于正式门槛 8/12 Event，因此只作描述性诊断，不用于正式 gate。", "", "| factor | price bucket | events | 120s IC | zero | crossing markout（非 PnL） |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    price = event_metrics[(event_metrics.slice_name == "price_bucket") & event_metrics.factor.isin(MAIN_FACTORS) & event_metrics.horizon_seconds.eq(120)].groupby(["factor", "slice_value"]).agg(events=("event_slug", "nunique"), ic=("ic", "mean"), zero=("zero_rate", "mean"), crossing=("crossing_markout_diagnostic", "mean")).reset_index()
    for row in price.itertuples():
        lines.append(f"| {row.factor} | {row.slice_value} | {row.events} | {row.ic:.3f} | {row.zero:.3f} | {row.crossing:.5f} |")
    lines += ["", "## valid_obs", "", "valid_obs10/50/200 仅作诊断，不改写预注册主结论。", "", "| factor | valid_obs | coverage | zero | median IC | elapsed P90 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    valid_summary = valid_metrics.groupby(["factor", "valid_obs"]).agg(coverage=("coverage", "mean"), zero=("zero_rate", "mean"), ic=("ic", "median"), elapsed=("elapsed_p90", "median")).reset_index()
    for row in valid_summary.itertuples():
        lines.append(f"| {row.factor} | {row.valid_obs} | {row.coverage:.3f} | {row.zero:.3f} | {row.ic:.3f} | {row.elapsed:.1f}s |")
    obs10_zero = float(valid_summary.loc[valid_summary.valid_obs.eq(10), "zero"].mean())
    obs200_zero = float(valid_summary.loc[valid_summary.valid_obs.eq(200), "zero"].mean())
    obs200_elapsed = float(valid_summary.loc[valid_summary.valid_obs.eq(200), "elapsed"].max())
    valid_candidate = (obs10_zero - obs200_zero >= .15) and (obs200_elapsed <= 1800)
    valid_note = (
        f"obs200 相比 obs10 将 zero rate 降低 {obs10_zero-obs200_zero:.1%}，elapsed P90={obs200_elapsed:.1f}s，满足 amendment 候选门槛；本轮仍保持 diagnostic-only，尚未正式采用 amendment。"
        if valid_candidate else
        f"obs200 相比 obs10 将 zero rate 降低 {obs10_zero-obs200_zero:.1%}，elapsed P90={obs200_elapsed:.1f}s，未同时满足 amendment 候选门槛。"
    )
    lines += ["", valid_note, "", "## direct replay parity", "", parity_text, "具体记录见 `compact/pilot_6_event/direct_native_parity.json`。", "", "## E3", "", f"- 因子统计路径建议：**{decision}**。", f"- 扩张决策：**{'GO' if parity_pass and event_failures == 0 else 'NO-GO（先补 parity）'}**。", "- 6-event pilot 只验证流程和方向，不宣称发现可交易 alpha。", ""]
    (report_dir / "pilot_report.md").write_text("\n".join(lines), encoding="utf-8")

    score = ["# Boss scorecard：PMXT 天气因子 pilot", "", f"- 阶段：6-event pilot；城市 {len({str(e['city']) for e in selected})}；clean 3 / degraded 3。", f"- Runtime：{wall/60:.1f} 分钟；event failure：{event_failures}/6。", f"- 因子统计路径：**{decision}**；扩张：**{'GO' if parity_pass and event_failures == 0 else 'NO-GO'}**。", "- E2：价格分层样本不足，只能诊断；不得当作正式结论。", f"- valid_obs：diagnostic-only；{'已达到 amendment 候选门槛，但尚未采用' if valid_candidate else '未达到 amendment 候选门槛'}。", f"- direct→Nautilus parity：{parity_text}", "", "## 主指标", "", "| factor | horizon | coverage | zero | median IC | positive event | activity IC diff | gate |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in merged.itertuples():
        score.append(f"| {row.factor} | {row.horizon_seconds}s | {row.coverage:.3f} | {row.zero_rate:.3f} | {row.median_event_ic:.3f} | {row.positive_event_rate:.3f} | {row.activity_ic_diff:.3f} | {'PASS' if row.gate_pass else 'FAIL'} |")
    score += ["", "## 下一步", "", "pilot 基础设施通过且无 stop condition 时，按预注册样本设计扩至 36 Event；否则先修复失败项。", ""]
    (report_dir / "boss_scorecard.md").write_text("\n".join(score), encoding="utf-8")


def write_preregistration(output: Path, selected: list[dict[str, Any]]) -> None:
    protocol = output / "protocol"; protocol.mkdir(parents=True, exist_ok=True)
    rules = {
        "created_at": datetime.now(UTC).isoformat(), "main_factors": MAIN_FACTORS, "appendix_factors": APPENDIX_FACTORS,
        "main_horizons_seconds": MAIN_HORIZONS, "diagnostic_horizons_seconds": (60, 300, 900), "valid_obs_diagnostic_only": VALID_OBS,
        "activity_buckets_mutations_per_second_30s": {"low": "<0.1", "medium": "[0.1,1.0)", "high": ">=1.0"},
        "price_buckets": PRICE_BUCKETS, "event_equal": True, "event_bootstrap_draws": 2000,
        "crossing_markout_is_pnl": False, "sample_selection": "three smallest distinct-city events in each clean/degraded cohort before factor results; activity classified at observation time",
        "events": [e["event_slug"] for e in selected],
    }
    (protocol / "preregistration.json").write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")
    (protocol / "amendments.md").write_text("# Amendments\n\n本次 pilot 无 amendment。valid_obs 保持 diagnostic-only。\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--inventory", type=Path, default=INVENTORY_PATH)
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    started = time.perf_counter(); selected = select_pilot(args.inventory.resolve()); write_preregistration(args.output, selected)
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_event, event): event["event_slug"] for event in selected}
        for future in as_completed(futures):
            result = future.result(); results.append(result)
            print(f"completed {len(results)}/6 {result['event_slug']} elapsed={result['elapsed_seconds']:.1f}s failures={len(result['failures'])}", flush=True)
    order = {event["event_slug"]: i for i, event in enumerate(selected)}; results.sort(key=lambda x: order[x["event_slug"]])
    compact = args.output / "compact" / "pilot_6_event"; compact.mkdir(parents=True, exist_ok=True)
    token_metrics = pd.DataFrame(row for result in results for row in result["metrics"])
    valid_metrics = pd.DataFrame(row for result in results for row in result["valid_obs"])
    events = event_equal(token_metrics); head = headline(events); activity = activity_summary(events)
    token_metrics.to_parquet(compact / "token_slice_metrics.parquet", index=False)
    events.to_csv(compact / "event_equal_metrics.csv", index=False); head.to_csv(compact / "e1_headline.csv", index=False)
    activity.to_csv(compact / "e1_activity.csv", index=False); valid_metrics.to_csv(compact / "valid_obs_metrics.csv", index=False)
    events[events.slice_name.eq("price_bucket")].to_csv(compact / "e2_price_bucket.csv", index=False)
    appendix = events[(events.slice_name == "overall") & events.horizon_seconds.isin(MAIN_HORIZONS)].groupby(["factor", "horizon_seconds"]).agg(events=("event_slug", "nunique"), median_event_ic=("ic", "median"), positive_event_rate=("ic", lambda x: (x > 0).mean()), coverage=("coverage", "mean"), zero_rate=("zero_rate", "mean")).reset_index()
    appendix.to_csv(compact / "basic_factor_performance.csv", index=False)
    failures = {result["event_slug"]: result["failures"] for result in results}; (compact / "failures.json").write_text(json.dumps(failures, indent=2, ensure_ascii=False), encoding="utf-8")
    skipped = {result["event_slug"]: result["skipped_tokens"] for result in results}; (compact / "skipped_tokens.json").write_text(json.dumps(skipped, indent=2, ensure_ascii=False), encoding="utf-8")
    parity = run_direct_native_parity(selected); (compact / "direct_native_parity.json").write_text(json.dumps(parity, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    make_charts(events, valid_metrics, args.output / "charts")
    wall = time.perf_counter() - started; write_reports(args.output, selected, results, head, activity, events, valid_metrics, parity, wall)
    summary = {"generated_at": datetime.now(UTC).isoformat(), "stage": "pilot_6_event", "events": len(selected), "event_failures": sum(x["token_count"] == 0 for x in results), "token_failures": sum(len(x["failures"]) for x in results), "skipped_tokens": sum(len(x["skipped_tokens"]) for x in results), "direct_native_ordering_smoke_pass": all(x["status"] == "pass" for x in parity), "direct_native_semantic_output_equality_checked": False, "direct_native_parity_checks": len(parity), "direct_native_parity_passed": sum(x["status"] == "pass" for x in parity), "direct_native_parity_failures": [x for x in parity if x["status"] != "pass"], "wall_elapsed_seconds": wall, "report": str(args.output / "report/pilot_report.md")}
    (compact / "run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
