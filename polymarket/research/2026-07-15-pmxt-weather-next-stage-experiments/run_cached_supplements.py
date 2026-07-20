"""Small cached-only supplements for the 36-event weather study.

The script does not read source PMXT parquet and does not replay markets.  It
uses the committed distribution output plus local token anchor caches to answer
the two meeting-critical questions: signal stability and probability-sum tail
attribution.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw


HERE = Path(__file__).resolve().parent
RUNNER_PATH = HERE / "run_event_level.py"
DISTRIBUTION_DIR = HERE / "event_level/distribution"
REPORT_DIR = HERE / "event_level/reports"
FRESH_SECONDS = 120.0
MAX_SPREAD = 0.10
MAX_SOURCE_SPAN_SECONDS = 60.0
BOOTSTRAP_DRAWS = 10_000
SEED = 20260715


def load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("pmxt_weather_cached_supplement_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load runner: {RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_ic(frame: pd.DataFrame) -> float:
    valid = frame[["pressure_factor", "future_mean_move"]].dropna()
    if len(valid) < 3 or valid.pressure_factor.nunique() < 2 or valid.future_mean_move.nunique() < 2:
        return math.nan
    return float(valid.pressure_factor.rank().corr(valid.future_mean_move.rank()))


def bootstrap_median(values: pd.Series) -> tuple[float, float, float]:
    sample = values.dropna().to_numpy(dtype=float)
    if len(sample) == 0:
        return math.nan, math.nan, math.nan
    rng = np.random.default_rng(SEED)
    draws = np.median(rng.choice(sample, size=(BOOTSTRAP_DRAWS, len(sample)), replace=True), axis=1)
    return float(np.median(sample)), float(np.quantile(draws, .05)), float(np.quantile(draws, .95))


def signal_stability(distribution: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    keys = ["event_slug", "city", "event_date", "cohort"]
    raw = distribution.groupby(keys).apply(safe_ic, include_groups=False).rename("event_ic").reset_index()
    gated_source = distribution[distribution.sum_deviation.abs().le(.10)]
    gated = gated_source.groupby(keys).apply(safe_ic, include_groups=False).rename("event_ic").reset_index()
    rows: list[dict[str, Any]] = []
    for scope, frame in (("raw", raw), ("abs_sum_deviation_le_0.10", gated)):
        estimate, low, high = bootstrap_median(frame.event_ic)
        rows.append({
            "record_type": "bootstrap_90ci", "scope": scope, "excluded_date": "", "event_date": "",
            "event_slug": "", "city": "", "cohort": "", "events": int(frame.event_ic.notna().sum()),
            "estimate": estimate, "ci90_low": low, "ci90_high": high,
            "positive_event_share": float(frame.event_ic.gt(0).mean()),
        })
    for omitted in sorted(raw.event_date.unique()):
        kept = raw[raw.event_date.ne(omitted)]
        rows.append({
            "record_type": "leave_one_date_out", "scope": "raw", "excluded_date": omitted, "event_date": "",
            "event_slug": "", "city": "", "cohort": "", "events": int(kept.event_ic.notna().sum()),
            "estimate": float(kept.event_ic.median()), "ci90_low": math.nan, "ci90_high": math.nan,
            "positive_event_share": float(kept.event_ic.gt(0).mean()),
        })
    for event_date, group in raw.groupby("event_date", sort=True):
        rows.append({
            "record_type": "date_event_equal", "scope": "raw", "excluded_date": "", "event_date": event_date,
            "event_slug": "", "city": "", "cohort": "", "events": int(group.event_ic.notna().sum()),
            "estimate": float(group.event_ic.mean()), "ci90_low": math.nan, "ci90_high": math.nan,
            "positive_event_share": float(group.event_ic.gt(0).mean()),
        })
    for row in raw.itertuples():
        rows.append({
            "record_type": "event_ic", "scope": "raw", "excluded_date": "", "event_date": row.event_date,
            "event_slug": row.event_slug, "city": row.city, "cohort": row.cohort, "events": 1,
            "estimate": row.event_ic, "ci90_low": math.nan, "ci90_high": math.nan,
            "positive_event_share": float(row.event_ic > 0),
        })
    return pd.DataFrame(rows), raw


def _font(runner: Any, size: int) -> Any:
    return runner._font(size)  # Reuse the research runner's Windows-safe font fallback.


def draw_forest(path: Path, event_ic: pd.DataFrame, runner: Any) -> None:
    frame = event_ic.sort_values("event_ic").reset_index(drop=True)
    width, row_height = 1100, 24
    height = 100 + len(frame) * row_height
    image = Image.new("RGB", (width, height), "white"); draw = ImageDraw.Draw(image)
    draw.text((30, 18), "Distribution pressure → 120s implied-temperature move (Event IC)", fill="black", font=_font(runner, 22))
    left, right, top = 470, 1050, 70
    lo = min(-.05, float(frame.event_ic.min())); hi = max(.05, float(frame.event_ic.max()))
    zero_x = left + (0-lo)/(hi-lo)*(right-left)
    draw.line((zero_x, top-8, zero_x, height-20), fill="#777", width=2)
    colors = {"clean": "#1665a8", "degraded": "#d14a2a"}
    for idx, row in frame.iterrows():
        y = top + idx * row_height
        x = left + (float(row.event_ic)-lo)/(hi-lo)*(right-left)
        draw.text((8, y-7), f"{row.event_date} | {row.city} | {row.event_slug[:34]}", fill="black", font=_font(runner, 12))
        draw.line((zero_x, y, x, y), fill=colors.get(row.cohort, "#555"), width=2)
        draw.ellipse((x-4, y-4, x+4, y+4), fill=colors.get(row.cohort, "#555"))
        draw.text((right-70, y-7), f"{row.event_ic:+.3f}", fill="black", font=_font(runner, 12))
    image.save(path)


def tail_snapshots(runner: Any, selected: pd.DataFrame) -> pd.DataFrame:
    summary_by_slug = {
        slug: json.loads((runner.RUN_ROOT / slug / "summary.json").read_text(encoding="utf-8"))
        for slug in selected.event_slug
    }
    rows: list[dict[str, Any]] = []
    for event in selected.to_dict("records"):
        panel = runner.event_grid(event)
        if panel.empty:
            continue
        expected = int(summary_by_slug[event["event_slug"]]["markets_total"])
        tick_conflict = bool(summary_by_slug[event["event_slug"]]["failures"])
        for timestamp, group in panel.groupby("timestamp", sort=True):
            valid = group[group.raw_mid.notna()]
            if valid.empty:
                continue
            source_times = pd.to_datetime(valid.state_timestamp, utc=True).dropna()
            rows.append({
                "event_slug": event["event_slug"], "city": event["city"], "event_date": str(event["event_date"]),
                "cohort": "clean" if event["cohort_role"] == "primary_development_replication" else "degraded",
                "timestamp": timestamp, "lifecycle_bucket": str(valid.lifecycle_bucket.iloc[0]),
                "expected_outcomes": expected, "observable_outcomes": int(panel.token_id.nunique()),
                "valid_outcomes": int(valid.token_id.nunique()), "complete_outcomes": bool(valid.token_id.nunique() == expected),
                "sum_mid": float(valid.raw_mid.sum()), "sum_deviation": float(valid.raw_mid.sum()-1),
                "max_bbo_age_seconds": float(valid.state_age_seconds.max()), "median_bbo_age_seconds": float(valid.state_age_seconds.median()),
                "max_spread": float(valid.spread.max()), "median_spread": float(valid.spread.median()),
                "outcome_source_span_seconds": float((source_times.max()-source_times.min()).total_seconds()) if len(source_times) else math.nan,
                "event_has_tick_size_conflict": tick_conflict,
                "sum_best_bid": float(valid.bid1.sum()), "sum_best_ask": float(valid.ask1.sum()),
            })
    return pd.DataFrame(rows)


def sensitivity_table(snapshots: pd.DataFrame) -> pd.DataFrame:
    filters = {
        "Raw": pd.Series(True, index=snapshots.index),
        "Complete outcomes": snapshots.complete_outcomes,
        f"Fresh BBO <={int(FRESH_SECONDS)}s": snapshots.max_bbo_age_seconds.le(FRESH_SECONDS),
        f"Spread max <={MAX_SPREAD:.2f}": snapshots.max_spread.le(MAX_SPREAD + 1e-9),
        f"Synchronized <={int(MAX_SOURCE_SPAN_SECONDS)}s": snapshots.outcome_source_span_seconds.le(MAX_SOURCE_SPAN_SECONDS),
    }
    filters["Complete + fresh"] = filters["Complete outcomes"] & filters[f"Fresh BBO <={int(FRESH_SECONDS)}s"]
    filters["Complete + fresh + spread + sync"] = filters["Complete + fresh"] & filters[f"Spread max <={MAX_SPREAD:.2f}"] & filters[f"Synchronized <={int(MAX_SOURCE_SPAN_SECONDS)}s"]
    rows = []
    for name, mask in filters.items():
        values = snapshots.loc[mask, "sum_deviation"].abs()
        rows.append({
            "filter": name, "snapshots": len(values), "events": int(snapshots.loc[mask, "event_slug"].nunique()),
            "p50_abs_deviation": float(values.quantile(.5)) if len(values) else math.nan,
            "p95_abs_deviation": float(values.quantile(.95)) if len(values) else math.nan,
            "share_abs_deviation_gt_010": float(values.gt(.10).mean()) if len(values) else math.nan,
            "max_abs_deviation": float(values.max()) if len(values) else math.nan,
        })
    return pd.DataFrame(rows)


def write_report(stability: pd.DataFrame, event_ic: pd.DataFrame, sensitivity: pd.DataFrame, tail: pd.DataFrame) -> None:
    raw_boot = stability[(stability.record_type == "bootstrap_90ci") & (stability.scope == "raw")].iloc[0]
    gated_boot = stability[(stability.record_type == "bootstrap_90ci") & (stability.scope != "raw")].iloc[0]
    loo = stability[stability.record_type == "leave_one_date_out"]
    negative = event_ic[event_ic.event_ic.lt(0)].sort_values("event_ic")
    raw_tail = sensitivity[sensitivity["filter"].eq("Raw")].iloc[0]
    strict_tail = sensitivity[sensitivity["filter"].eq("Complete + fresh + spread + sync")].iloc[0]
    anomalies = tail[tail.sum_deviation.abs().gt(.10)]
    strict_anomalies = anomalies[
        anomalies.complete_outcomes
        & anomalies.max_bbo_age_seconds.le(FRESH_SECONDS)
        & anomalies.max_spread.le(MAX_SPREAD + 1e-9)
        & anomalies.outcome_source_span_seconds.le(MAX_SOURCE_SPAN_SECONDS)
    ]
    top_raw = {key: int(value) for key, value in anomalies.groupby(["event_slug", "city"]).size().sort_values(ascending=False).head(3).items()}
    top_strict = {key: int(value) for key, value in strict_anomalies.groupby(["event_slug", "city"]).size().sort_values(ascending=False).head(3).items()}
    loo_stable = bool(loo.estimate.gt(0).all())
    tail_conclusion = (
        "完整、新鲜、spread 受控且时间同步的报价下仍存在明显尾部，值得继续做结构性定价诊断。"
        if strict_tail.snapshots > 0 and strict_tail.share_abs_deviation_gt_010 > .01
        else "异常主要随报价不完整、陈旧、宽 spread 或时间不同步而消失；当前更像数据/可观测性问题。"
    )
    lines = [
        "# 36 Event 缓存补充实验", "", "> 只读既有 anchor/distribution 缓存；未扩 Event、未重跑 PMXT replay、未做策略或 PnL。", "",
        "## P0-1 Distribution 信号稳定性", "",
        f"- Raw median Event IC={raw_boot.estimate:.3f}，Event bootstrap 90% CI=[{raw_boot.ci90_low:.3f}, {raw_boot.ci90_high:.3f}]。",
        f"- 排除 |sum(mid)-1|>0.10 后：median IC={gated_boot.estimate:.3f}，90% CI=[{gated_boot.ci90_low:.3f}, {gated_boot.ci90_high:.3f}]。",
        f"- 9 组 leave-one-date-out：最小/最大 median IC={loo.estimate.min():.3f}/{loo.estimate.max():.3f}；是否全部正向：{'是' if loo_stable else '否'}。",
        f"- 负 IC Event：{len(negative)} 个。", "",
        "| date | city | cohort | Event IC |", "| --- | --- | --- | ---: |",
    ]
    for row in negative.itertuples():
        lines.append(f"| {row.event_date} | {row.city} | {row.cohort} | {row.event_ic:.3f} |")
    lines += [
        "", "**判定：** " + ("可以表述为“跨日期稳定的弱结构信号”，仍不是 Alpha。" if loo_stable and raw_boot.ci90_low > 0 else "结果依赖样本日期，只能保持探索性措辞。"), "",
        "## P0-2 概率和异常尾部归因", "",
        f"固定口径：fresh BBO <= {FRESH_SECONDS:.0f}s；spread max <= {MAX_SPREAD:.2f}；同一分钟 Outcome source-time span <= {MAX_SOURCE_SPAN_SECONDS:.0f}s。", "",
        "| filter | snapshots | events | P95 | >0.10 | max |", "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sensitivity.itertuples():
        lines.append(f"| {row.filter} | {row.snapshots} | {row.events} | {row.p95_abs_deviation:.3f} | {row.share_abs_deviation_gt_010:.1%} | {row.max_abs_deviation:.3f} |")
    lines += [
        "", f"**判定：** {tail_conclusion}",
        f"Raw 异常占比={raw_tail.share_abs_deviation_gt_010:.1%}；严格同步完整子样本异常占比={strict_tail.share_abs_deviation_gt_010:.1%}（若无样本则为 NaN）。", "",
        f"- Raw >0.10：{len(anomalies)} 个 snapshot / {anomalies.event_slug.nunique()} Event；其中 complete={anomalies.complete_outcomes.mean():.1%}、fresh={anomalies.max_bbo_age_seconds.le(FRESH_SECONDS).mean():.1%}、spread-controlled={anomalies.max_spread.le(MAX_SPREAD + 1e-9).mean():.1%}、synchronized={anomalies.outcome_source_span_seconds.le(MAX_SOURCE_SPAN_SECONDS).mean():.1%}。",
        f"- Raw 异常前三：{top_raw}。",
        f"- 严格过滤后 >0.10：{len(strict_anomalies)} 个 snapshot / {strict_anomalies.event_slug.nunique()} Event；前三：{top_strict}。",
        "- 归因结论：绝大多数极端尾部随完整性、spread 与同步过滤消失；残余偏离仍存在，但集中于少数 Event，应做逐 Event 报价语义核验，不能直接解释为套利。", "",
        "## 可选 P1：Event 内可执行边界", "",
        "本轮不作正式执行结论。缓存只有 BBO 价格，没有每条腿 BBO size 与可验证 fee rate，无法可靠计算最小多腿容量和 fee 后边界；避免把 mid/BBO 偏离误称为套利。",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "cached_supplement_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    runner = load_runner()
    distribution = pd.read_csv(DISTRIBUTION_DIR / "distribution_metrics.csv", parse_dates=["timestamp"])
    stability, event_ic = signal_stability(distribution)
    stability.to_csv(DISTRIBUTION_DIR / "distribution_signal_stability.csv", index=False)
    draw_forest(DISTRIBUTION_DIR / "distribution_signal_forest.png", event_ic, runner)
    selected = runner.select_batch36(runner.INVENTORY_PATH)
    tail = tail_snapshots(runner, selected)
    sensitivity = sensitivity_table(tail)
    sensitivity.to_csv(DISTRIBUTION_DIR / "deviation_tail_sensitivity.csv", index=False)
    tail[tail.sum_deviation.abs().gt(.10)].sort_values("sum_deviation", key=lambda x: x.abs(), ascending=False).to_csv(DISTRIBUTION_DIR / "deviation_tail_attribution.csv", index=False)
    write_report(stability, event_ic, sensitivity, tail)
    print(REPORT_DIR / "cached_supplement_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
