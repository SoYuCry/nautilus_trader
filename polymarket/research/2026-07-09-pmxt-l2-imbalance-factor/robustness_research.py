
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


VALID_BOOK = "valid"
DEFAULT_FACTORS = (
    "depth_imbalance_1",
    "depth_imbalance_3",
    "depth_imbalance_5",
    "microprice_minus_mid",
    "ofi_30s",
    "trade_pressure_30s",
)
DEFAULT_HORIZONS = (60, 300, 900)
DEFAULT_TIME_BUCKETS = 12
DEFAULT_NULL_REPEATS = 50
DEFAULT_BOOTSTRAP_REPEATS = 1000
RANDOM_SEED = 20260709
FLOW_FACTORS = frozenset({"ofi_30s", "trade_pressure_30s"})


@dataclass(frozen=True)
class RobustnessRunSummary:
    config_path: str
    output_dir: str
    panel_path: str
    rows_loaded: int
    current_valid_rows: int
    time_buckets: int
    null_repeats: int
    bootstrap_repeats: int
    generated_at: str
    report_path: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Run robustness checks for PMXT L2 factor IC results.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    config_path = args.config.resolve()
    config = load_config(config_path)
    output_dir = resolve_output_dir(config, config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    panel_path = resolve_input_path(config_path, config["input"]["baseline_panel"])
    panel = pd.read_parquet(panel_path)
    factors = [factor for factor in config.get("analysis", {}).get("factors", DEFAULT_FACTORS) if factor in panel]
    horizons = [int(value) for value in config.get("analysis", {}).get("horizons_seconds", DEFAULT_HORIZONS)]
    time_buckets = int(config.get("analysis", {}).get("time_buckets", DEFAULT_TIME_BUCKETS))
    null_repeats = int(config.get("analysis", {}).get("null_repeats", DEFAULT_NULL_REPEATS))
    bootstrap_repeats = int(config.get("analysis", {}).get("bootstrap_repeats", DEFAULT_BOOTSTRAP_REPEATS))

    current_valid = panel[panel.get("book_validity") == VALID_BOOK].copy()
    current_valid = current_valid.sort_values(["timestamp_received", "sequence"], kind="mergesort")
    current_valid["time_bucket"] = assign_time_buckets(current_valid["timestamp_received"], time_buckets)

    temporal_ic = compute_temporal_ic(current_valid, factors, horizons)
    summary = compute_robustness_summary(current_valid, factors, horizons, temporal_ic, null_repeats, bootstrap_repeats)
    report_path = output_dir / "robustness_report.md"
    temporal_ic_path = output_dir / "temporal_ic_by_bucket.csv"
    summary_path = output_dir / "robustness_summary.csv"
    temporal_ic.to_csv(temporal_ic_path, index=False)
    summary.to_csv(summary_path, index=False)

    svg_paths = write_robustness_svgs(output_dir, summary, temporal_ic)
    run_summary = RobustnessRunSummary(
        config_path=str(config_path),
        output_dir=str(output_dir),
        panel_path=str(panel_path),
        rows_loaded=len(panel),
        current_valid_rows=len(current_valid),
        time_buckets=time_buckets,
        null_repeats=null_repeats,
        bootstrap_repeats=bootstrap_repeats,
        generated_at=datetime.now(UTC).isoformat(),
        report_path=str(report_path),
    )
    write_report(report_path, run_summary, summary, temporal_ic, svg_paths)
    (output_dir / "robustness_run_summary.json").write_text(
        json.dumps({**run_summary.__dict__, "svg_paths": [str(path) for path in svg_paths]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(run_summary.__dict__, indent=2, ensure_ascii=False))
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
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def resolve_input_path(config_path: Path, raw: str) -> Path:
    path = Path(str(raw))
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def assign_time_buckets(timestamp: pd.Series, bucket_count: int) -> pd.Series:
    order = timestamp.rank(method="first")
    try:
        buckets = pd.qcut(order, q=bucket_count, labels=False, duplicates="drop")
    except ValueError:
        return pd.Series([0] * len(timestamp), index=timestamp.index, dtype="int64")
    return buckets.astype("int64") + 1


def compute_temporal_ic(panel: pd.DataFrame, factors: list[str], horizons: list[int]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for factor in factors:
        factor_values = pd.to_numeric(panel[factor], errors="coerce")
        for horizon in horizons:
            ret_col = f"future_mid_return_{horizon}s"
            future_valid_col = f"future_book_validity_{horizon}s"
            if ret_col not in panel:
                continue
            frame = pd.DataFrame(
                {
                    "time_bucket": panel["time_bucket"],
                    "factor": factor_values,
                    "ret": pd.to_numeric(panel[ret_col], errors="coerce"),
                    "future_book_validity": panel.get(future_valid_col, VALID_BOOK),
                },
            )
            frame = frame[frame["future_book_validity"] == VALID_BOOK].dropna(subset=["factor", "ret", "time_bucket"])
            for bucket, bucket_frame in frame.groupby("time_bucket", observed=True):
                rows.append(
                    {
                        "factor": factor,
                        "horizon_seconds": horizon,
                        "time_bucket": int(bucket),
                        "count": len(bucket_frame),
                        "spearman_corr": spearman_corr(bucket_frame["factor"], bucket_frame["ret"]),
                        "factor_mean": float(bucket_frame["factor"].mean()),
                        "future_mid_return_mean": float(bucket_frame["ret"].mean()),
                        "zero_return_share": float((bucket_frame["ret"] == 0).mean()),
                    },
                )
    return pd.DataFrame(rows)


def compute_robustness_summary(
    panel: pd.DataFrame,
    factors: list[str],
    horizons: list[int],
    temporal_ic: pd.DataFrame,
    null_repeats: int,
    bootstrap_repeats: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for factor in factors:
        factor_values = pd.to_numeric(panel[factor], errors="coerce")
        for horizon in horizons:
            ret_col = f"future_mid_return_{horizon}s"
            future_valid_col = f"future_book_validity_{horizon}s"
            if ret_col not in panel:
                continue
            frame = pd.DataFrame(
                {
                    "timestamp_received": panel["timestamp_received"],
                    "asset_id": panel.get("asset_id", "asset"),
                    "time_bucket": panel["time_bucket"],
                    "factor": factor_values,
                    "ret": pd.to_numeric(panel[ret_col], errors="coerce"),
                    "future_book_validity": panel.get(future_valid_col, VALID_BOOK),
                },
            )
            frame = frame[frame["future_book_validity"] == VALID_BOOK].dropna(subset=["factor", "ret", "time_bucket"])
            if len(frame) < 50:
                continue
            raw = spearman_corr(frame["factor"], frame["ret"])
            time_neutral = time_neutral_spearman(frame)
            time_demeaned = time_demeaned_spearman(frame)
            non_overlap = select_non_overlapping(frame, horizon)
            non_overlap_spearman = spearman_corr(non_overlap["factor"], non_overlap["ret"]) if len(non_overlap) >= 10 else math.nan
            nonzero_direction_hit = directional_hit_nonzero(frame)
            monotonicity = quantile_monotonicity(frame, quantiles=5)
            bucket_rows = temporal_ic[(temporal_ic["factor"] == factor) & (temporal_ic["horizon_seconds"] == horizon)].copy()
            bucket_ic = pd.to_numeric(bucket_rows["spearman_corr"], errors="coerce").dropna()
            bucket_ci_low, bucket_ci_high = bootstrap_mean_ci(bucket_ic, repeats=bootstrap_repeats)
            null = permutation_null_abs_spearman(frame["factor"], frame["ret"], repeats=null_repeats)
            null_p95_abs = percentile(null, 0.95)
            raw_abs = abs(raw) if math.isfinite(raw) else math.nan
            time_neutral_abs = abs(time_neutral) if math.isfinite(time_neutral) else math.nan
            positive_share = float((bucket_ic > 0).mean()) if len(bucket_ic) else math.nan
            same_sign_share = same_sign_bucket_share(bucket_ic, raw)
            factor_family = "flow_clock_sensitive" if factor in FLOW_FACTORS else "book"
            candidate_screen = bool(
                factor_family == "book"
                and horizon <= 300
                and math.isfinite(time_neutral)
                and math.isfinite(null_p95_abs)
                and abs(time_neutral) > null_p95_abs
                and same_sign_share >= 0.67
                and math.isfinite(non_overlap_spearman)
                and (non_overlap_spearman == 0 or (non_overlap_spearman > 0) == (raw > 0))
                and math.isfinite(nonzero_direction_hit)
                and nonzero_direction_hit >= 0.52
                and math.isfinite(monotonicity)
                and monotonicity >= 0.5
            )
            rows.append(
                {
                    "factor": factor,
                    "factor_family": factor_family,
                    "horizon_seconds": horizon,
                    "count": len(frame),
                    "raw_spearman": raw,
                    "time_neutral_spearman": time_neutral,
                    "time_demeaned_spearman": time_demeaned,
                    "non_overlap_count": len(non_overlap),
                    "non_overlap_spearman": non_overlap_spearman,
                    "nonzero_direction_hit": nonzero_direction_hit,
                    "quantile_monotonicity_spearman": monotonicity,
                    "shape_warning": bool(not math.isfinite(monotonicity) or monotonicity < 0.5),
                    "bucket_ic_mean": float(bucket_ic.mean()) if len(bucket_ic) else math.nan,
                    "bucket_ic_median": float(bucket_ic.median()) if len(bucket_ic) else math.nan,
                    "bucket_ic_std": float(bucket_ic.std()) if len(bucket_ic) else math.nan,
                    "bucket_ic_min": float(bucket_ic.min()) if len(bucket_ic) else math.nan,
                    "bucket_ic_max": float(bucket_ic.max()) if len(bucket_ic) else math.nan,
                    "bucket_ic_bootstrap_mean_p05": bucket_ci_low,
                    "bucket_ic_bootstrap_mean_p95": bucket_ci_high,
                    "positive_bucket_share": positive_share,
                    "same_sign_bucket_share": same_sign_share,
                    "null_abs_spearman_p95": null_p95_abs,
                    "raw_abs_over_null_p95": raw_abs / null_p95_abs if null_p95_abs and math.isfinite(raw_abs) else math.nan,
                    "time_neutral_abs_over_null_p95": time_neutral_abs / null_p95_abs if null_p95_abs and math.isfinite(time_neutral_abs) else math.nan,
                    "passes_basic_screen": bool(
                        math.isfinite(time_neutral)
                        and math.isfinite(null_p95_abs)
                        and abs(time_neutral) > null_p95_abs
                        and same_sign_share >= 0.67
                    ),
                    "candidate_for_cross_event": candidate_screen,
                },
            )
    return pd.DataFrame(rows).sort_values(["candidate_for_cross_event", "passes_basic_screen", "time_neutral_abs_over_null_p95"], ascending=[False, False, False])


def spearman_corr(x: pd.Series, y: pd.Series) -> float:
    frame = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(frame) < 2:
        return math.nan
    if frame["x"].nunique(dropna=True) < 2 or frame["y"].nunique(dropna=True) < 2:
        return math.nan
    return float(frame["x"].rank(method="average").corr(frame["y"].rank(method="average"), method="pearson"))


def time_neutral_spearman(frame: pd.DataFrame) -> float:
    ranked = frame.copy()
    ranked["factor_rank"] = ranked.groupby("time_bucket", observed=True)["factor"].rank(method="average", pct=True)
    ranked["ret_rank"] = ranked.groupby("time_bucket", observed=True)["ret"].rank(method="average", pct=True)
    return spearman_corr(ranked["factor_rank"], ranked["ret_rank"])


def time_demeaned_spearman(frame: pd.DataFrame) -> float:
    demeaned = frame.copy()
    demeaned["factor_demeaned"] = demeaned["factor"] - demeaned.groupby("time_bucket", observed=True)["factor"].transform("mean")
    demeaned["ret_demeaned"] = demeaned["ret"] - demeaned.groupby("time_bucket", observed=True)["ret"].transform("mean")
    return spearman_corr(demeaned["factor_demeaned"], demeaned["ret_demeaned"])


def select_non_overlapping(frame: pd.DataFrame, horizon_seconds: int) -> pd.DataFrame:
    selected_parts: list[pd.DataFrame] = []
    delta = pd.Timedelta(seconds=horizon_seconds)
    for _, group in frame.sort_values("timestamp_received", kind="mergesort").groupby("asset_id", observed=True):
        selected_index: list[Any] = []
        next_allowed = pd.Timestamp("1900-01-01", tz="UTC")
        for idx, timestamp in group["timestamp_received"].items():
            if pd.isna(timestamp):
                continue
            if timestamp >= next_allowed:
                selected_index.append(idx)
                next_allowed = timestamp + delta
        if selected_index:
            selected_parts.append(group.loc[selected_index])
    if not selected_parts:
        return frame.iloc[0:0]
    return pd.concat(selected_parts, axis=0).sort_values("timestamp_received", kind="mergesort")


def directional_hit_nonzero(frame: pd.DataFrame) -> float:
    ranked = frame.copy()
    ranked["factor_rank_centered"] = ranked.groupby("time_bucket", observed=True)["factor"].rank(method="average", pct=True) - 0.5
    nonzero = ranked[(ranked["ret"] != 0) & (ranked["factor_rank_centered"] != 0)].dropna(subset=["factor_rank_centered", "ret"])
    if nonzero.empty:
        return math.nan
    return float(((nonzero["factor_rank_centered"] > 0) == (nonzero["ret"] > 0)).mean())


def quantile_monotonicity(frame: pd.DataFrame, *, quantiles: int) -> float:
    work = frame.dropna(subset=["factor", "ret"]).copy()
    if len(work) < quantiles * 5 or work["factor"].nunique(dropna=True) < quantiles:
        return math.nan
    try:
        work["quantile"] = pd.qcut(work["factor"], q=quantiles, labels=False, duplicates="drop")
    except ValueError:
        return math.nan
    grouped = work.groupby("quantile", observed=True)["ret"].mean().dropna()
    if len(grouped) < 3:
        return math.nan
    return spearman_corr(pd.Series(grouped.index.to_numpy(dtype=float), index=grouped.index), grouped)


def same_sign_bucket_share(bucket_ic: pd.Series, raw: float) -> float:
    bucket_ic = bucket_ic.dropna()
    if bucket_ic.empty or not math.isfinite(raw) or raw == 0:
        return math.nan
    return float(((bucket_ic > 0) == (raw > 0)).mean())


def permutation_null_abs_spearman(factor: pd.Series, ret: pd.Series, *, repeats: int) -> list[float]:
    frame = pd.DataFrame({"factor": factor, "ret": ret}).dropna()
    if len(frame) < 50:
        return []
    factor_rank = frame["factor"].rank(method="average").to_numpy()
    ret_rank = frame["ret"].rank(method="average").to_numpy()
    factor_rank = (factor_rank - factor_rank.mean()) / factor_rank.std()
    ret_rank = (ret_rank - ret_rank.mean()) / ret_rank.std()
    if not math.isfinite(float(factor_rank.std())) or not math.isfinite(float(ret_rank.std())):
        return []
    values: list[float] = []
    for repeat in range(repeats):
        shuffled = pd.Series(ret_rank).sample(frac=1.0, random_state=RANDOM_SEED + repeat).to_numpy()
        values.append(abs(float((factor_rank * shuffled).mean())))
    return values


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    series = pd.Series(values).dropna().sort_values()
    if series.empty:
        return math.nan
    return float(series.quantile(q))


def bootstrap_mean_ci(values: pd.Series, *, repeats: int) -> tuple[float, float]:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty:
        return math.nan, math.nan
    means: list[float] = []
    for repeat in range(repeats):
        sample = values.sample(n=len(values), replace=True, random_state=RANDOM_SEED + 10_000 + repeat)
        means.append(float(sample.mean()))
    return percentile(means, 0.05), percentile(means, 0.95)


def write_robustness_svgs(output_dir: Path, summary: pd.DataFrame, temporal_ic: pd.DataFrame) -> list[Path]:
    paths: list[Path] = []
    if not summary.empty:
        pivot = summary.pivot_table(index="factor", columns="horizon_seconds", values="time_neutral_spearman", aggfunc="first").fillna(0.0)
        path = output_dir / "time_neutral_spearman_by_factor.svg"
        write_grouped_bar_svg(path, pivot, title="Time-neutral Spearman IC by factor and horizon")
        paths.append(path)
        pivot2 = summary.pivot_table(index="factor", columns="horizon_seconds", values="same_sign_bucket_share", aggfunc="first").fillna(0.0)
        path2 = output_dir / "same_sign_bucket_share_by_factor.svg"
        write_grouped_bar_svg(path2, pivot2, title="Same-sign bucket share by factor and horizon")
        paths.append(path2)
    if not temporal_ic.empty:
        for factor in ("depth_imbalance_3", "depth_imbalance_5", "microprice_minus_mid", "ofi_30s"):
            frame = temporal_ic[temporal_ic["factor"] == factor]
            if frame.empty:
                continue
            pivot = frame.pivot_table(index="time_bucket", columns="horizon_seconds", values="spearman_corr", aggfunc="first").fillna(0.0)
            path = output_dir / f"{factor}_temporal_ic.svg"
            write_grouped_bar_svg(path, pivot, title=f"{factor}: bucket Spearman IC")
            paths.append(path)
    return paths


def write_grouped_bar_svg(path: Path, table: pd.DataFrame, *, title: str) -> None:
    width = 980
    height = 520
    margin_left = 150
    margin_right = 30
    margin_top = 58
    margin_bottom = 90
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    values = table.to_numpy(dtype=float).flatten()
    finite_values = [float(v) for v in values if math.isfinite(float(v))]
    if not finite_values:
        finite_values = [0.0]
    y_min = min(0.0, *finite_values)
    y_max = max(0.0, *finite_values)
    if y_min == y_max:
        y_min -= 1.0
        y_max += 1.0
    pad = (y_max - y_min) * 0.10
    y_min -= pad
    y_max += pad
    zero_y = margin_top + (y_max / (y_max - y_min)) * plot_h
    row_count = max(len(table.index), 1)
    col_count = max(len(table.columns), 1)
    group_w = plot_w / row_count
    bar_w = group_w / (col_count + 1)
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c"]

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
        label = str(index_value)
        parts.append(
            f'<text x="{x0 + group_w/2:.2f}" y="{height-56}" text-anchor="middle" font-size="12" font-family="Arial" transform="rotate(-25 {x0 + group_w/2:.2f},{height-56})">{escape_xml(label)}</text>',
        )
        for col_i, col in enumerate(table.columns):
            value = float(table.loc[index_value, col])
            bar_x = x0 + (col_i + 0.5) * bar_w
            bar_y = y(max(value, 0.0)) if value >= 0 else zero_y
            bar_h = abs(y(value) - zero_y)
            parts.append(
                f'<rect x="{bar_x:.2f}" y="{bar_y:.2f}" width="{bar_w*0.82:.2f}" height="{bar_h:.2f}" fill="{colors[col_i % len(colors)]}" opacity="0.88"><title>{escape_xml(str(index_value))} h{escape_xml(str(col))}: {value:.6g}</title></rect>',
            )
    legend_x = margin_left
    legend_y = height - 25
    for col_i, col in enumerate(table.columns):
        lx = legend_x + col_i * 120
        parts.append(f'<rect x="{lx}" y="{legend_y-12}" width="12" height="12" fill="{colors[col_i % len(colors)]}"/>')
        parts.append(f'<text x="{lx+18}" y="{legend_y}" font-size="12" font-family="Arial">h={escape_xml(str(col))}s</text>')
    parts.append(f'<text x="20" y="{margin_top+12}" font-size="12" font-family="Arial">max {y_max:.4g}</text>')
    parts.append(f'<text x="20" y="{height-margin_bottom}" font-size="12" font-family="Arial">min {y_min:.4g}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def escape_xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_report(
    path: Path,
    run_summary: RobustnessRunSummary,
    summary: pd.DataFrame,
    temporal_ic: pd.DataFrame,
    svg_paths: list[Path],
) -> None:
    ranked = summary.sort_values("time_neutral_abs_over_null_p95", ascending=False) if not summary.empty else pd.DataFrame()
    passed = ranked[ranked["passes_basic_screen"] == True] if not ranked.empty else pd.DataFrame()  # noqa: E712
    candidates = ranked[ranked["candidate_for_cross_event"] == True] if not ranked.empty else pd.DataFrame()  # noqa: E712
    flow = ranked[ranked["factor_family"] == "flow_clock_sensitive"] if not ranked.empty else pd.DataFrame()
    lines: list[str] = [
        "# PMXT L2 因子稳健性检查",
        "",
        f"生成时间: {run_summary.generated_at}",
        "",
        "## 0. 结论先行",
        "",
        "这一步不是新增策略，而是专门检查上一份报告里偏高的 IC 是否可能只是单 event 趋势、重叠 label 或时钟结构造成。",
        "",
    ]
    if not candidates.empty:
        best = candidates.iloc[0]
        lines.append(
            f"- 可进入跨 event 复验的最强 clean book 候选: `{best['factor']}` @ {int(best['horizon_seconds'])}s，time-neutral IC = `{best['time_neutral_spearman']:.6f}`，non-overlap IC = `{best['non_overlap_spearman']:.6f}`，非零方向命中率 = `{best['nonzero_direction_hit']:.3f}`，分层单调性 = `{best['quantile_monotonicity_spearman']:.3f}`。",
        )
    else:
        lines.append("- 暂无组合通过“book + <=300s + time-neutral + 非重叠 + 非零方向命中 + 分层单调性”的候选筛选；上一轮 raw IC 需要谨慎看待。")
    if not passed.empty:
        best_passed = passed.iloc[0]
        lines.append(
            f"- 若只看基础统计筛选，最强组合是 `{best_passed['factor']}` @ {int(best_passed['horizon_seconds'])}s，但需要结合 factor_family / horizon / spread / 时钟风险 / 分层形态降级。",
        )
    if not flow.empty:
        lines.append("- `ofi_30s` / `trade_pressure_30s` 标为 flow_clock_sensitive：在上游时钟失序修复前，只作诊断，不升级为策略候选。")
    lines.extend(
        [
            "- 这仍然只是单 event 研究；下一步必须跨 event 复验，且最后仍要进 Nautilus 成交回测。",
            "",
            "## 1. 方法",
            "",
            f"- time buckets: `{run_summary.time_buckets}`，按 `timestamp_received` 等量切片。",
            f"- null repeats: `{run_summary.null_repeats}`，随机打散 future return 后计算 |Spearman| 的 95% 分位。",
            f"- bootstrap repeats: `{run_summary.bootstrap_repeats}`，在时间切片 IC 上做 block/bootstrap 风格均值置信区间。",
            "- raw Spearman: 全样本因子 rank vs future mid-return rank。",
            "- time-neutral Spearman: 每个时间切片内部 rank 后再算整体相关，用来削弱趋势项。",
            "- time-demeaned Spearman: 每个时间切片内先去均值，再算 rank correlation。",
            "- non-overlap Spearman: 每个 asset 内按 horizon 做贪心非重叠抽样，降低重叠 label 虚增样本量。",
            "- nonzero direction hit: 只看 future return 非零样本，用时间切片内因子 rank 的方向预测涨跌。",
            "- quantile monotonicity: 看分层均值是否单调，避免把驼峰形活跃度信号误判为方向 alpha。",
            "",
            "## 2. 候选筛选规则",
            "",
            "进入 `candidate_for_cross_event` 需要同时满足：",
            "",
            "1. `factor_family == book`，即暂时排除 flow_clock_sensitive 因子；",
            "2. horizon <= 300s，暂时不升级 900s 长窗口信号；",
            "3. time-neutral IC 超过随机置换 null 的 95% 分位；",
            "4. 时间切片 IC 同方向比例 >= 2/3；",
            "5. 非重叠 IC 与 raw IC 同方向；",
            "6. 非零 move 方向命中率 >= 52%；",
            "7. quantile monotonicity >= 0.5，避免把驼峰形活跃度信号误判为方向 alpha。",
            "",
            "## 3. 图",
            "",
        ],
    )
    for svg in svg_paths:
        lines.append(f"![{svg.name}]({svg.name})")
        lines.append("")
    lines.extend(["", "## 4. 稳健性排名", ""])
    append_markdown_table(lines, ranked.head(30))
    lines.extend(["", "## 5. Flow 因子隔离区", ""])
    append_markdown_table(lines, flow.head(20))
    lines.extend(["", "## 6. 时间切片 IC 预览", ""])
    append_markdown_table(lines, temporal_ic.head(80))
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


if __name__ == "__main__":
    raise SystemExit(main())
