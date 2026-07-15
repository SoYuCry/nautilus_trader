"""
Run one Wave0 factor across a small, event-equal PMXT weather sample.

This is deliberately a thin research runner: no cache, sharding protocol,
resume ledger, strategy, fills, fees, or PnL.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import as_completed
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCH_DIR = Path(__file__).resolve().parent
FACTOR_PROTOCOL_PATH = RESEARCH_DIR / "factor_protocol.py"
DEFAULT_INVENTORY = (
    REPO_ROOT
    / "polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/event_inventory.parquet"
)
DEFAULT_OUTPUT = RESEARCH_DIR / "outputs/depth_imbalance_1_clean_10"
FACTOR = "depth_imbalance_1"
HORIZONS = (30, 120, 600)


def _load_factor_protocol() -> Any:
    name = "pmxt_weather_factor_wave0_runtime"
    spec = importlib.util.spec_from_file_location(name, FACTOR_PROTOCOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load factor protocol: {FACTOR_PROTOCOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def select_events(inventory_path: Path, limit: int) -> list[dict[str, Any]]:
    columns = [
        "event_slug",
        "event_date",
        "cohort_role",
        "rows_written",
        "event_local_missing_hour_count",
        "event_local_corrupt_hour_count",
        "paths",
    ]
    frame = pd.read_parquet(inventory_path, columns=columns)
    clean = frame[
        frame["cohort_role"].eq("primary_development_replication")
        & frame["event_local_missing_hour_count"].eq(0)
        & frame["event_local_corrupt_hour_count"].eq(0)
    ].sort_values(["rows_written", "event_slug"], kind="mergesort")
    if len(clean) < limit:
        raise ValueError(f"requested {limit} strict-clean events, only {len(clean)} available")
    return clean.head(limit).to_dict(orient="records")


def _safe_spearman(left: pd.Series, right: pd.Series) -> float:
    if len(left) < 2 or left.nunique() < 2 or right.nunique() < 2:
        return math.nan
    value = left.rank(method="average").corr(right.rank(method="average"))
    return float(value) if pd.notna(value) else math.nan


def summarize_panel(panel: pd.DataFrame, *, event_slug: str, market_label: str, token_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ranking = panel[panel["ranking_observation"]].copy()
    for horizon in HORIZONS:
        label = f"future_mid_return_{horizon}s"
        valid = ranking[[FACTOR, label]].dropna()
        directional = valid[(valid[FACTOR] != 0) & (valid[label] != 0)]
        wins = directional[directional[FACTOR] * directional[label] > 0]
        losses = directional[directional[FACTOR] * directional[label] < 0]
        top_minus_bottom = math.nan
        if len(valid) >= 10 and valid[FACTOR].nunique() >= 2:
            quantiles = pd.qcut(valid[FACTOR], q=5, labels=False, duplicates="drop")
            grouped = valid.assign(_q=quantiles).dropna(subset=["_q"]).groupby("_q")[label].mean()
            if len(grouped) >= 2:
                top_minus_bottom = float(grouped.iloc[-1] - grouped.iloc[0])
        rows.append(
            {
                "event_slug": event_slug,
                "market_label": market_label,
                "token_id": token_id,
                "factor": FACTOR,
                "horizon_seconds": horizon,
                "valid_rows": len(valid),
                "nonzero_rows": len(directional),
                "spearman_ic": _safe_spearman(valid[FACTOR], valid[label]),
                "nonzero_hit_rate": float(len(wins) / len(directional)) if len(directional) else math.nan,
                "average_win_move": float(wins[label].abs().mean()) if len(wins) else math.nan,
                "average_loss_move": float(losses[label].abs().mean()) if len(losses) else math.nan,
                "zero_return_share": float((valid[label] == 0).mean()) if len(valid) else math.nan,
                "top_minus_bottom": top_minus_bottom,
            },
        )
    return rows


def run_event(event: dict[str, Any]) -> dict[str, Any]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter

    started = time.perf_counter()
    protocol = _load_factor_protocol()
    event_dir = Path(event["paths"]["event_dir"])
    event_index = json.loads((event_dir / "event_index.json").read_text(encoding="utf-8"))
    adapter = PMXTEventV1Adapter(repo_root=REPO_ROOT)
    metrics: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for market in event_index["markets"]:
        token_id = str(market["yesToken"])
        try:
            dataset = adapter.load(
                {
                    "input": {
                        "event_dir": str(event_dir),
                        "condition_id": str(market["conditionId"]),
                        "asset_id": token_id,
                        "dataset_id": str(event["event_slug"]),
                    },
                },
            )
            panel = protocol.build_factor_panel(dataset, horizons_seconds=HORIZONS, include_labels=True)
            metrics.extend(
                summarize_panel(
                    panel,
                    event_slug=str(event["event_slug"]),
                    market_label=str(market.get("label") or market["index"]),
                    token_id=token_id,
                ),
            )
        except Exception as exc:  # event-level report must retain token failures
            failures.append({"market_label": str(market.get("label")), "token_id": token_id, "error": repr(exc)})
    return {
        "event_slug": event["event_slug"],
        "event_date": str(event["event_date"]),
        "source_rows": int(event["rows_written"]),
        "metrics": metrics,
        "failures": failures,
        "elapsed_seconds": time.perf_counter() - started,
    }


def aggregate_events(token_metrics: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (event_slug, horizon), group in token_metrics.groupby(["event_slug", "horizon_seconds"], sort=True):
        finite_ic = group["spearman_ic"].dropna()
        finite_hit = group["nonzero_hit_rate"].dropna()
        rows.append(
            {
                "event_slug": event_slug,
                "horizon_seconds": int(horizon),
                "token_count": int(group["token_id"].nunique()),
                "tokens_with_ic": len(finite_ic),
                "event_equal_token_ic": float(finite_ic.mean()) if len(finite_ic) else math.nan,
                "positive_token_share": float((finite_ic > 0).mean()) if len(finite_ic) else math.nan,
                "event_equal_nonzero_hit_rate": float(finite_hit.mean()) if len(finite_hit) else math.nan,
                "event_equal_top_minus_bottom": float(group["top_minus_bottom"].mean()),
            },
        )
    return pd.DataFrame(rows)


def write_report(path: Path, *, selected: list[dict[str, Any]], results: list[dict[str, Any]], event_metrics: pd.DataFrame) -> None:
    lines = [
        "# depth_imbalance_1：10 个 clean 天气 event 小批量测试",
        "",
        "> 研究性因子检验，不是策略回测；不包含成交、queue、fee 或 PnL。每个二元 market 只取 YES token，避免 YES/NO 机械互补造成伪重复样本。",
        "",
        "## 样本",
        "",
        "| event | source rows | elapsed | failures |",
        "| --- | ---: | ---: | ---: |",
    ]
    by_slug = {result["event_slug"]: result for result in results}
    for event in selected:
        result = by_slug[event["event_slug"]]
        lines.append(
            f"| `{event['event_slug']}` | {int(event['rows_written']):,} | "
            f"{result['elapsed_seconds']:.1f}s | {len(result['failures'])} |",
        )
    lines.extend([
        "",
        "## Event-equal 结果",
        "",
        "| horizon | events with IC | median event IC | positive event share | median hit rate | median top-bottom |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for horizon, group in event_metrics.groupby("horizon_seconds", sort=True):
        ic = group["event_equal_token_ic"].dropna()
        hit = group["event_equal_nonzero_hit_rate"].dropna()
        spread = group["event_equal_top_minus_bottom"].dropna()
        lines.append(
            f"| {int(horizon)}s | {len(ic)} | {ic.median():.4f} | "
            f"{(ic > 0).mean():.3f} | {hit.median():.3f} | {spread.median():.5f} |",
        )
    total_elapsed = sum(result["elapsed_seconds"] for result in results)
    lines.extend([
        "",
        "## 运行说明",
        "",
        f"- 因子：`{FACTOR}`。",
        f"- horizons：{', '.join(str(value) + 's' for value in HORIZONS)}。",
        "- event 内 token 等权；headline 再按 event 等权。",
        "- 样本按严格 clean 条件筛选后取 source rows 最少的 10 个 event，因此这是快速 smoke batch，不是随机样本或确认集。",
        f"- 各 event CPU wall-time 合计：{total_elapsed:.1f}s；并行后的实际总耗时见 `run_summary.json`。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    started = time.perf_counter()
    selected = select_events(args.inventory.resolve(), args.limit)
    args.output.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_event, event): event["event_slug"] for event in selected}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"completed {len(results)}/{len(selected)} {result['event_slug']} "
                f"elapsed={result['elapsed_seconds']:.1f}s failures={len(result['failures'])}",
                flush=True,
            )
    order = {event["event_slug"]: index for index, event in enumerate(selected)}
    results.sort(key=lambda result: order[result["event_slug"]])
    token_metrics = pd.DataFrame(row for result in results for row in result["metrics"])
    event_metrics = aggregate_events(token_metrics)
    token_metrics.to_csv(args.output / "token_metrics.csv", index=False)
    event_metrics.to_csv(args.output / "event_metrics.csv", index=False)
    (args.output / "failures.json").write_text(
        json.dumps({result["event_slug"]: result["failures"] for result in results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_report(args.output / "report.md", selected=selected, results=results, event_metrics=event_metrics)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "factor": FACTOR,
        "horizons_seconds": HORIZONS,
        "event_count": len(selected),
        "yes_token_count": int(token_metrics["token_id"].nunique()) if not token_metrics.empty else 0,
        "failure_count": sum(len(result["failures"]) for result in results),
        "wall_elapsed_seconds": time.perf_counter() - started,
        "report": str(args.output / "report.md"),
    }
    (args.output / "run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
