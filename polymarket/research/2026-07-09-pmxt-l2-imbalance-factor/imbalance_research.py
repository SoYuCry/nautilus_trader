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


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FACTORS = (
    "depth_imbalance_1",
    "depth_imbalance_3",
    "depth_imbalance_5",
    "microprice_minus_mid",
    "ofi_30s",
    "trade_pressure_30s",
)
DEFAULT_HORIZONS = (60, 300, 900)
VALID_BOOK = "valid"


@dataclass(frozen=True)
class StudySummary:
    config_path: str
    output_dir: str
    panel_path: str
    rows_loaded: int
    current_valid_rows: int
    generated_at: str
    factor_count: int
    horizon_count: int
    report_path: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze PMXT L2 imbalance factor relationships.")
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
    quantiles = int(config.get("analysis", {}).get("quantiles", 5))
    spread_buckets = int(config.get("analysis", {}).get("spread_buckets", 3))

    run_metadata = load_optional_json(config_path, config.get("input", {}).get("baseline_run_metadata"))
    current_valid = panel[panel.get("book_validity") == VALID_BOOK].copy()

    filter_summary = build_filter_summary(panel, current_valid, horizons)
    factor_summary, quantile_returns = analyze_factors(current_valid, factors, horizons, quantiles)
    spread_conditioned = analyze_spread_conditioned(current_valid, factors, horizons, spread_buckets, quantiles)

    filter_summary_path = output_dir / "data_filter_summary.csv"
    factor_summary_path = output_dir / "factor_ic_summary.csv"
    quantile_returns_path = output_dir / "quantile_returns.csv"
    spread_conditioned_path = output_dir / "spread_conditioned_top_bottom.csv"
    filter_summary.to_csv(filter_summary_path, index=False)
    factor_summary.to_csv(factor_summary_path, index=False)
    quantile_returns.to_csv(quantile_returns_path, index=False)
    spread_conditioned.to_csv(spread_conditioned_path, index=False)

    svg_paths = write_svgs(output_dir, factor_summary, quantile_returns)
    summary = StudySummary(
        config_path=str(config_path),
        output_dir=str(output_dir),
        panel_path=str(panel_path),
        rows_loaded=len(panel),
        current_valid_rows=len(current_valid),
        generated_at=datetime.now(UTC).isoformat(),
        factor_count=len(factors),
        horizon_count=len(horizons),
        report_path=str(output_dir / "report.md"),
    )
    write_report(
        output_dir / "report.md",
        summary,
        filter_summary,
        factor_summary,
        quantile_returns,
        spread_conditioned,
        svg_paths,
        run_metadata,
    )
    (output_dir / "run_summary.json").write_text(
        json.dumps({**summary.__dict__, "svg_paths": [str(path) for path in svg_paths]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary.__dict__, indent=2, ensure_ascii=False))
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


def load_optional_json(config_path: Path, raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    path = resolve_input_path(config_path, raw)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_filter_summary(panel: pd.DataFrame, current_valid: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = [
        {"metric": "rows_total", "value": len(panel)},
        {"metric": "current_valid_rows", "value": len(current_valid)},
        {"metric": "current_valid_share", "value": len(current_valid) / len(panel) if len(panel) else math.nan},
    ]
    if "book_validity" in panel:
        for validity, count in panel["book_validity"].value_counts(dropna=False).items():
            rows.append({"metric": f"book_validity.{validity}", "value": int(count)})
    for horizon in horizons:
        ret_col = f"future_mid_return_{horizon}s"
        future_valid_col = f"future_book_validity_{horizon}s"
        frame = current_valid
        if future_valid_col in frame:
            frame = frame[frame[future_valid_col] == VALID_BOOK]
        valid_label = frame[ret_col].notna() if ret_col in frame else pd.Series(dtype=bool)
        rows.extend(
            [
                {"metric": f"h{horizon}.current_and_future_valid_rows", "value": int(valid_label.sum())},
                {"metric": f"h{horizon}.label_coverage_on_current_valid", "value": int(valid_label.sum()) / len(current_valid) if len(current_valid) else math.nan},
            ],
        )
    return pd.DataFrame(rows)


def analyze_factors(
    panel: pd.DataFrame,
    factors: list[str],
    horizons: list[int],
    quantiles: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    quantile_rows: list[dict[str, Any]] = []
    for factor in factors:
        factor_series = pd.to_numeric(panel[factor], errors="coerce")
        for horizon in horizons:
            ret_col = f"future_mid_return_{horizon}s"
            future_valid_col = f"future_book_validity_{horizon}s"
            if ret_col not in panel:
                continue
            frame = pd.DataFrame(
                {
                    "factor": factor_series,
                    "ret": pd.to_numeric(panel[ret_col], errors="coerce"),
                    "future_book_validity": panel.get(future_valid_col, VALID_BOOK),
                },
            )
            frame = frame[frame["future_book_validity"] == VALID_BOOK].dropna(subset=["factor", "ret"])
            if len(frame) < 10 or frame["factor"].nunique(dropna=True) < 2:
                continue
            quantile = safe_qcut(frame["factor"], quantiles)
            frame = frame.assign(quantile=quantile).dropna(subset=["quantile"])
            grouped = frame.groupby("quantile", observed=True)["ret"]
            for q, series in grouped:
                quantile_rows.append(
                    {
                        "factor": factor,
                        "horizon_seconds": horizon,
                        "quantile": int(q),
                        "count": int(series.count()),
                        "mean_future_mid_return": float(series.mean()),
                        "median_future_mid_return": float(series.median()),
                        "positive_return_share": float((series > 0).mean()),
                        "zero_return_share": float((series == 0).mean()),
                    },
                )
            quantile_means = grouped.mean().sort_index()
            bottom = float(quantile_means.iloc[0]) if len(quantile_means) else math.nan
            top = float(quantile_means.iloc[-1]) if len(quantile_means) else math.nan
            pearson = frame["factor"].corr(frame["ret"], method="pearson")
            spearman = spearman_corr(frame["factor"], frame["ret"])
            slope = ols_slope(frame["factor"], frame["ret"])
            summary_rows.append(
                {
                    "factor": factor,
                    "horizon_seconds": horizon,
                    "count": len(frame),
                    "factor_mean": float(frame["factor"].mean()),
                    "factor_std": float(frame["factor"].std()),
                    "future_mid_return_mean": float(frame["ret"].mean()),
                    "future_mid_return_std": float(frame["ret"].std()),
                    "pearson_corr": float(pearson) if pd.notna(pearson) else math.nan,
                    "spearman_corr": float(spearman) if pd.notna(spearman) else math.nan,
                    "ols_slope_ret_per_factor_unit": slope,
                    "bottom_quantile_mean_return": bottom,
                    "top_quantile_mean_return": top,
                    "top_minus_bottom_mean_return": top - bottom if math.isfinite(top) and math.isfinite(bottom) else math.nan,
                    "positive_return_share": float((frame["ret"] > 0).mean()),
                    "zero_return_share": float((frame["ret"] == 0).mean()),
                },
            )
    return pd.DataFrame(summary_rows), pd.DataFrame(quantile_rows)


def analyze_spread_conditioned(
    panel: pd.DataFrame,
    factors: list[str],
    horizons: list[int],
    buckets: int,
    quantiles: int,
) -> pd.DataFrame:
    if "spread" not in panel:
        return pd.DataFrame()
    spread = pd.to_numeric(panel["spread"], errors="coerce")
    spread_bucket = safe_qcut(spread, buckets)
    rows: list[dict[str, Any]] = []
    for factor in factors:
        if factor not in panel:
            continue
        factor_series = pd.to_numeric(panel[factor], errors="coerce")
        for horizon in horizons:
            ret_col = f"future_mid_return_{horizon}s"
            future_valid_col = f"future_book_validity_{horizon}s"
            if ret_col not in panel:
                continue
            frame = pd.DataFrame(
                {
                    "spread_bucket": spread_bucket,
                    "factor": factor_series,
                    "ret": pd.to_numeric(panel[ret_col], errors="coerce"),
                    "future_book_validity": panel.get(future_valid_col, VALID_BOOK),
                },
            )
            frame = frame[frame["future_book_validity"] == VALID_BOOK].dropna()
            for bucket, bucket_frame in frame.groupby("spread_bucket", observed=True):
                if len(bucket_frame) < 10 or bucket_frame["factor"].nunique(dropna=True) < 2:
                    continue
                q = safe_qcut(bucket_frame["factor"], quantiles)
                bucket_frame = bucket_frame.assign(quantile=q).dropna(subset=["quantile"])
                grouped = bucket_frame.groupby("quantile", observed=True)["ret"].mean().sort_index()
                if len(grouped) < 2:
                    continue
                rows.append(
                    {
                        "factor": factor,
                        "horizon_seconds": horizon,
                        "spread_bucket": int(bucket),
                        "count": len(bucket_frame),
                        "bottom_quantile_mean_return": float(grouped.iloc[0]),
                        "top_quantile_mean_return": float(grouped.iloc[-1]),
                        "top_minus_bottom_mean_return": float(grouped.iloc[-1] - grouped.iloc[0]),
                    },
                )
    return pd.DataFrame(rows)


def safe_qcut(series: pd.Series, q: int) -> pd.Series:
    try:
        result = pd.qcut(series, q=q, labels=False, duplicates="drop")
    except ValueError:
        return pd.Series([math.nan] * len(series), index=series.index)
    return result.astype("float") + 1


def ols_slope(x: pd.Series, y: pd.Series) -> float:
    x = pd.to_numeric(x, errors="coerce")
    y = pd.to_numeric(y, errors="coerce")
    variance = x.var()
    if pd.isna(variance) or variance == 0:
        return math.nan
    return float(x.cov(y) / variance)


def spearman_corr(x: pd.Series, y: pd.Series) -> float:
    """
    Compute Spearman rank correlation without scipy.

    pandas delegates ``Series.corr(method="spearman")`` to scipy in this
    environment.  The research script intentionally stays dependency-light, so
    rank both series with pandas and then calculate ordinary Pearson
    correlation on the ranks.
    """
    frame = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(frame) < 2:
        return math.nan
    if frame["x"].nunique(dropna=True) < 2 or frame["y"].nunique(dropna=True) < 2:
        return math.nan
    return float(frame["x"].rank(method="average").corr(frame["y"].rank(method="average"), method="pearson"))


def write_svgs(output_dir: Path, factor_summary: pd.DataFrame, quantile_returns: pd.DataFrame) -> list[Path]:
    paths: list[Path] = []
    if not factor_summary.empty:
        pivot = factor_summary.pivot_table(
            index="factor",
            columns="horizon_seconds",
            values="spearman_corr",
            aggfunc="first",
        ).fillna(0.0)
        path = output_dir / "spearman_corr_by_factor.svg"
        write_grouped_bar_svg(path, pivot, title="Spearman IC by factor and horizon")
        paths.append(path)
    for factor in ("depth_imbalance_1", "depth_imbalance_3", "depth_imbalance_5", "microprice_minus_mid"):
        frame = quantile_returns[quantile_returns["factor"] == factor] if not quantile_returns.empty else pd.DataFrame()
        if frame.empty:
            continue
        pivot = frame.pivot_table(
            index="quantile",
            columns="horizon_seconds",
            values="mean_future_mid_return",
            aggfunc="first",
        ).fillna(0.0)
        path = output_dir / f"{factor}_quantile_returns.svg"
        write_grouped_bar_svg(path, pivot, title=f"{factor}: quantile mean future mid-return")
        paths.append(path)
    return paths


def write_grouped_bar_svg(path: Path, table: pd.DataFrame, *, title: str) -> None:
    width = 980
    height = 520
    margin_left = 170
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
    summary: StudySummary,
    filter_summary: pd.DataFrame,
    factor_summary: pd.DataFrame,
    quantile_returns: pd.DataFrame,
    spread_conditioned: pd.DataFrame,
    svg_paths: list[Path],
    run_metadata: dict[str, Any],
) -> None:
    top = factor_summary.sort_values("top_minus_bottom_mean_return", ascending=False).head(12) if not factor_summary.empty else pd.DataFrame()
    ic = factor_summary.reindex(factor_summary["spearman_corr"].abs().sort_values(ascending=False).index).head(12) if not factor_summary.empty else pd.DataFrame()
    lines: list[str] = [
        "# PMXT L2 Imbalance 因子研究报告",
        "",
        f"生成时间: {summary.generated_at}",
        "",
        "## 0. 结论先行",
        "",
        "这是一份探索性盘口因子研究，不是 Nautilus 成交回测，也不是 PnL 报告。",
        "PMXT 数据按共享 replay contract 的 `replay_timestamp`（source `timestamp`，缺失回退 `timestamp_received`）replay；receive-time inversion 只作为数据质量诊断暴露。",
        "",
        "本轮研究关注：depth imbalance / microprice deviation / OFI 与未来 mid-return 的统计关系。",
        "",
    ]
    if not top.empty:
        best = top.iloc[0]
        lines.extend(
            [
                f"- 最强 top-bottom 分层差：`{best['factor']}` @ {int(best['horizon_seconds'])}s，top-bottom mean future mid-return = `{best['top_minus_bottom_mean_return']:.6f}`。",
            ],
        )
    if not ic.empty:
        best_ic = ic.iloc[0]
        lines.append(
            f"- 最大 |Spearman IC|：`{best_ic['factor']}` @ {int(best_ic['horizon_seconds'])}s，IC = `{best_ic['spearman_corr']:.6f}`。",
        )
    lines.extend(
        [
            "- 注意：样本里 future return 的零值比例很高，均值差不能直接解释为可交易收益。",
            "- 下一步应该做跨 event 复验、手续费/滑点前的纯信号稳定性筛选，然后再决定是否进入 Nautilus 策略回测。",
            "",
            "## 1. 信任边界",
            "",
            "- data_tier: `TIER1_EXPLORATORY`",
            "- replay_clock: `replay_timestamp (timestamp, fallback timestamp_received)`",
            "- execution_claims_allowed: `false`",
            "- not_for_pnl: `true`",
            "- 不计算 fee/fill/queue/cash/position/PnL。",
            "- 不把 future mid-return 当成可成交收益。",
            "",
            "## 2. 输入和过滤",
            "",
            f"- panel: `{summary.panel_path}`",
            f"- rows_loaded: {summary.rows_loaded}",
            f"- current_valid_rows: {summary.current_valid_rows}",
            "",
        ],
    )
    append_markdown_table(lines, filter_summary)
    lines.extend(["", "## 3. 图", ""])
    for svg in svg_paths:
        rel = svg.name
        lines.append(f"![{rel}]({rel})")
        lines.append("")
    lines.extend(["", "## 4. Top-bottom 分层最强组合", ""])
    append_markdown_table(lines, top)
    lines.extend(["", "## 5. IC 排名", ""])
    append_markdown_table(lines, ic)
    lines.extend(["", "## 6. Quantile return 明细预览", ""])
    append_markdown_table(lines, quantile_returns.head(60))
    lines.extend(["", "## 7. Spread 条件下的 top-bottom", ""])
    append_markdown_table(lines, spread_conditioned.head(60))
    if run_metadata:
        trust = run_metadata.get("trust_metadata", {})
        lines.extend(
            [
                "",
                "## 8. 上游 baseline run metadata",
                "",
                f"- run_grade: `{trust.get('run_grade')}`",
                f"- source_time_inversion_count: `{(trust.get('source_time_diagnostics') or {}).get('source_time_inversion_count')}`",
                f"- source_delay_over_threshold_count: `{(trust.get('source_time_diagnostics') or {}).get('source_delay_over_threshold_count')}`",
                f"- book_validity_warning: `{trust.get('book_validity_warning')}`",
            ],
        )
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
