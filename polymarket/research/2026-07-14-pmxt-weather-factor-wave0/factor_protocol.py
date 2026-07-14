from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

import pandas as pd


PRIMARY_FACTOR_NAMES = (
    "depth_imbalance_1",
    "depth_imbalance_3",
    "depth_imbalance_5",
    "microprice_minus_mid",
    "spread",
    "top_level_depth",
    "depth_slope",
    "depth_concentration",
    "bid_ask_liquidity_asymmetry",
    "distance_to_zero_one",
    "tick_size_regime",
)

DIAGNOSTIC_FACTOR_NAMES = (
    "book_staleness_seconds",
    "book_update_intensity",
    "valid_observation_counts",
)

_ALLOWED_INPUT_FIELDS = {
    "sequence",
    "timestamp",
    "timestamp_received",
    "event_id",
    "market",
    "token_id",
    "asset_id",
    "event_type",
    "bids",
    "asks",
    "side",
    "price",
    "size",
    "source_quality_cohort",
}


def build_factor_panel(
    rows: Iterable[dict[str, Any]],
    *,
    horizons_seconds: Iterable[int],
    include_labels: bool = True,
) -> pd.DataFrame:
    clean_rows = [_select_input_fields(row) for row in rows]
    ordered = sorted(
        clean_rows,
        key=lambda row: (
            _row_clock(row),
            _received_clock(row),
            int(row.get("sequence", 0)),
        ),
    )

    states: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for row in ordered:
        token_id = str(row.get("token_id") or row.get("asset_id"))
        event_id = str(row.get("event_id") or row.get("market"))
        state = states.setdefault(token_id, {"bids": {}, "asks": {}, "last_valid_ts": None, "updates": 0})
        _apply_l2_update(state, row)
        state["updates"] += 1

        ts = _row_clock(row)
        timestamp_received = _received_clock(row)
        bid_levels = _sorted_levels(state["bids"], reverse=True)
        ask_levels = _sorted_levels(state["asks"], reverse=False)
        bid1 = bid_levels[0][0] if bid_levels else math.nan
        ask1 = ask_levels[0][0] if ask_levels else math.nan
        validity = _book_validity(bid1, ask1)
        mid = (bid1 + ask1) / 2 if validity == "valid" else math.nan
        spread = ask1 - bid1 if validity == "valid" else math.nan

        if validity == "valid":
            state["last_valid_ts"] = ts
        staleness = (
            float((ts - state["last_valid_ts"]).total_seconds())
            if state["last_valid_ts"] is not None
            else math.nan
        )

        record = {
            "event_id": event_id,
            "token_id": token_id,
            "sequence": int(row.get("sequence", 0)),
            "timestamp": ts,
            "timestamp_received": timestamp_received,
            "event_type": row.get("event_type"),
            "source_quality_cohort": row.get("source_quality_cohort", "clean"),
            "book_validity": validity,
            "bid1": bid1,
            "ask1": ask1,
            "mid": mid,
            "spread": spread,
            "depth_imbalance_1": _depth_imbalance(bid_levels, ask_levels, 1),
            "depth_imbalance_3": _depth_imbalance(bid_levels, ask_levels, 3),
            "depth_imbalance_5": _depth_imbalance(bid_levels, ask_levels, 5),
            "microprice_minus_mid": _microprice_minus_mid(bid_levels, ask_levels, mid),
            "top_level_depth": _top_level_depth(bid_levels, ask_levels),
            "depth_slope": _depth_slope(bid_levels, ask_levels),
            "depth_concentration": _depth_concentration(bid_levels, ask_levels),
            "bid_ask_liquidity_asymmetry": _liquidity_asymmetry(bid_levels, ask_levels),
            "distance_to_zero_one": min(mid, 1.0 - mid) if validity == "valid" else math.nan,
            "tick_size_regime": _tick_size_regime(bid_levels, ask_levels),
            "book_staleness_seconds": staleness,
            "book_update_intensity": float(state["updates"]),
            "valid_observation_counts": 1 if validity == "valid" else 0,
        }
        records.append(record)

    panel = pd.DataFrame(records)
    if panel.empty:
        return panel

    panel = panel.sort_values(["timestamp", "timestamp_received", "sequence"], kind="mergesort").reset_index(drop=True)
    if include_labels:
        panel = _add_labels(panel, tuple(int(h) for h in horizons_seconds))
    return panel


def run_factor_protocol(rows: Iterable[dict[str, Any]], *, horizons_seconds: Iterable[int]) -> dict[str, Any]:
    horizons = tuple(int(horizon) for horizon in horizons_seconds)
    panel = build_factor_panel(rows, horizons_seconds=horizons, include_labels=True)
    candidate_table = _build_candidate_table(panel, horizons)
    primary_shortlist = candidate_table[
        candidate_table.get("passes_shortlist", pd.Series(dtype=bool)).fillna(False)
        & candidate_table["factor"].isin(PRIMARY_FACTOR_NAMES)
    ].reset_index(drop=True)
    digest = _frame_digest(panel) + ":" + _frame_digest(candidate_table)
    return {
        "panel": panel,
        "candidate_table": candidate_table,
        "primary_shortlist": primary_shortlist,
        "deterministic_digest": hashlib.sha256(digest.encode()).hexdigest(),
    }


def aggregate_factor_metrics(token_rows: pd.DataFrame) -> pd.DataFrame:
    if token_rows.empty:
        return pd.DataFrame()

    out: list[dict[str, Any]] = []
    for (factor, horizon), group in token_rows.groupby(["factor", "horizon_seconds"], sort=True):
        event_ic = group.groupby("event_id", sort=True)["token_ic"].mean()
        row_weight_sum = float((group["token_ic"] * group["row_count"]).sum())
        row_count_sum = float(group["row_count"].sum())
        out.append(
            {
                "factor": factor,
                "horizon_seconds": horizon,
                "event_count": int(group["event_id"].nunique()),
                "token_count": int(group["token_id"].nunique()),
                "global_event_equal_ic": float(event_ic.mean()),
                "row_weighted_ic_audit": row_weight_sum / row_count_sum if row_count_sum else math.nan,
            },
        )
    return pd.DataFrame(out)


def apply_candidate_gates(cohort_metrics: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if cohort_metrics.empty:
        return pd.DataFrame()

    for factor, group in cohort_metrics.groupby("factor", sort=True):
        clean = group[group["cohort"] == "clean"]
        degraded = group[group["cohort"] == "degraded"]
        clean_ics = [float(v) for v in clean["event_ic"].dropna()]
        pos_share = float(clean["positive_event_share"].mean()) if not clean.empty else math.nan
        signs = {1 if v > 0 else -1 for v in clean_ics if v != 0}
        same_sign_two_horizons = any(sum(1 for v in clean_ics if (v > 0) == (sign > 0)) >= 2 for sign in signs)
        block_bootstrap_interval_excludes_zero = _any_explicit_true(group, "block_bootstrap_interval_excludes_zero")
        widest_spread_independent = _all_explicit_true(group, "widest_spread_independent")
        coverage_complete = _all_explicit_true(group, "coverage_complete")
        failure_manifest_complete = _all_explicit_true(group, "failure_manifest_complete")
        canonical_hashes_complete = _all_explicit_true(group, "canonical_hashes_complete")
        reversal = False
        if clean_ics and not degraded.empty:
            clean_sign = 1 if sum(clean_ics) > 0 else -1 if sum(clean_ics) < 0 else 0
            degraded_mean = float(degraded["event_ic"].dropna().mean())
            degraded_sign = 1 if degraded_mean > 0 else -1 if degraded_mean < 0 else 0
            reversal = bool(clean_sign and degraded_sign and clean_sign != degraded_sign)

        rows.append(
            {
                "factor": factor,
                "clean_positive_event_share": pos_share,
                "clean_degraded_sign_reversal": reversal,
                "same_sign_two_primary_horizons": same_sign_two_horizons,
                "block_bootstrap_interval_excludes_zero": block_bootstrap_interval_excludes_zero,
                "widest_spread_independent": widest_spread_independent,
                "coverage_complete": coverage_complete,
                "failure_manifest_complete": failure_manifest_complete,
                "canonical_hashes_complete": canonical_hashes_complete,
                "passes_shortlist": bool(
                    factor in PRIMARY_FACTOR_NAMES
                    and same_sign_two_horizons
                    and pos_share >= 0.60
                    and not reversal
                    and block_bootstrap_interval_excludes_zero
                    and widest_spread_independent
                    and coverage_complete
                    and failure_manifest_complete
                    and canonical_hashes_complete
                ),
            },
        )
    return pd.DataFrame(rows)


def _select_input_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in _ALLOWED_INPUT_FIELDS if key in row}


def _row_clock(row: dict[str, Any]) -> pd.Timestamp:
    source_timestamp = _parse_timestamp(row.get("timestamp"))
    if pd.notna(source_timestamp):
        return source_timestamp
    return _received_clock(row)


def _received_clock(row: dict[str, Any]) -> pd.Timestamp:
    received_timestamp = _parse_timestamp(row.get("timestamp_received"))
    if pd.notna(received_timestamp):
        return received_timestamp
    return _parse_timestamp(row.get("timestamp"))


def _parse_timestamp(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value)


def _apply_l2_update(state: dict[str, Any], row: dict[str, Any]) -> None:
    event_type = row.get("event_type")
    if event_type == "book":
        state["bids"] = _levels_from(row.get("bids", []))
        state["asks"] = _levels_from(row.get("asks", []))
    elif event_type == "price_change":
        side = str(row.get("side", "")).upper()
        book_side = "bids" if side == "BUY" else "asks" if side == "SELL" else ""
        if book_side:
            price = _to_float(row.get("price"))
            size = _to_float(row.get("size"))
            if size <= 0:
                state[book_side].pop(price, None)
            else:
                state[book_side][price] = size


def _levels_from(values: Iterable[dict[str, Any]]) -> dict[float, float]:
    levels: dict[float, float] = {}
    for value in values:
        price = _to_float(value.get("price"))
        size = _to_float(value.get("size"))
        if math.isfinite(price) and math.isfinite(size) and size > 0:
            levels[price] = size
    return levels


def _sorted_levels(levels: dict[float, float], *, reverse: bool) -> list[tuple[float, float]]:
    return sorted(levels.items(), key=lambda item: item[0], reverse=reverse)


def _book_validity(bid1: float, ask1: float) -> str:
    if not math.isfinite(bid1) or not math.isfinite(ask1):
        return "missing"
    if bid1 >= ask1:
        return "crossed"
    return "valid"


def _depth_imbalance(bids: list[tuple[float, float]], asks: list[tuple[float, float]], depth: int) -> float:
    bid_size = sum(size for _, size in bids[:depth])
    ask_size = sum(size for _, size in asks[:depth])
    total = bid_size + ask_size
    return (bid_size - ask_size) / total if total else math.nan


def _microprice_minus_mid(bids: list[tuple[float, float]], asks: list[tuple[float, float]], mid: float) -> float:
    if not bids or not asks or not math.isfinite(mid):
        return math.nan
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    total = bid_size + ask_size
    return ((ask * bid_size + bid * ask_size) / total) - mid if total else math.nan


def _top_level_depth(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    if not bids or not asks:
        return math.nan
    return bids[0][1] + asks[0][1]


def _depth_slope(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    levels = bids[:5] + asks[:5]
    if len(levels) < 2:
        return 0.0 if levels else math.nan
    return float(levels[-1][1] - levels[0][1]) / max(len(levels) - 1, 1)


def _depth_concentration(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    total_depth = sum(size for _, size in bids[:5] + asks[:5])
    top_depth = _top_level_depth(bids, asks)
    return top_depth / total_depth if total_depth and math.isfinite(top_depth) else math.nan


def _liquidity_asymmetry(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    """Book-state bid/ask liquidity asymmetry across visible top-five depth."""
    bid_liquidity = sum(size for _, size in bids[:5])
    ask_liquidity = sum(size for _, size in asks[:5])
    total_liquidity = bid_liquidity + ask_liquidity
    return (bid_liquidity - ask_liquidity) / total_liquidity if total_liquidity else math.nan


def _tick_size_regime(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    prices = sorted({price for price, _ in bids[:5] + asks[:5]})
    diffs = [b - a for a, b in pairwise(prices) if b > a]
    return min(diffs) if diffs else math.nan


def _add_labels(panel: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:  # noqa: C901
    panel = panel.copy()
    for horizon in horizons:
        for column in (
            f"future_mid_{horizon}s",
            f"future_mid_return_{horizon}s",
            f"future_book_validity_{horizon}s",
            f"label_matched_timestamp_{horizon}s",
        ):
            panel[column] = pd.NA

    panel["next_nonzero_mid_move"] = pd.NA
    panel["next_nonzero_mid_move_direction"] = pd.NA
    panel["next_nonzero_mid_move_matched_sequence"] = pd.NA

    for token_index in panel.groupby("token_id", sort=False).groups.values():
        indexes = list(token_index)
        token = panel.loc[indexes].sort_values(["timestamp", "timestamp_received", "sequence"], kind="mergesort")
        token_indexes = list(token.index)
        timestamps = list(token["timestamp"])
        for position, index in enumerate(token_indexes):
            current_ts = panel.loc[index, "timestamp"]
            current_mid = panel.loc[index, "mid"]
            for horizon in horizons:
                target = current_ts + pd.Timedelta(seconds=horizon)
                match_index = _first_at_or_after_group_last(token_indexes, timestamps, target)
                if match_index is None:
                    continue
                future_mid = panel.loc[match_index, "mid"]
                future_validity = panel.loc[match_index, "book_validity"]
                panel.loc[index, f"label_matched_timestamp_{horizon}s"] = panel.loc[match_index, "timestamp"]
                panel.loc[index, f"future_book_validity_{horizon}s"] = future_validity
                if future_validity == "valid" and math.isfinite(current_mid) and math.isfinite(future_mid):
                    panel.loc[index, f"future_mid_{horizon}s"] = future_mid
                    panel.loc[index, f"future_mid_return_{horizon}s"] = future_mid - current_mid

            if not math.isfinite(current_mid):
                continue
            for future_index in token_indexes[position + 1 :]:
                future_mid = panel.loc[future_index, "mid"]
                if panel.loc[future_index, "book_validity"] != "valid" or not math.isfinite(future_mid):
                    continue
                move = future_mid - current_mid
                if move != 0:
                    panel.loc[index, "next_nonzero_mid_move"] = move
                    panel.loc[index, "next_nonzero_mid_move_direction"] = 1 if move > 0 else -1
                    panel.loc[index, "next_nonzero_mid_move_matched_sequence"] = panel.loc[future_index, "sequence"]
                    break
    return panel


def _first_at_or_after_group_last(indexes: list[int], timestamps: list[pd.Timestamp], target: pd.Timestamp) -> int | None:
    for position, (index, timestamp) in enumerate(zip(indexes, timestamps, strict=True)):
        if timestamp >= target:
            group_last = index
            for next_index, next_timestamp in zip(indexes[position + 1 :], timestamps[position + 1 :], strict=True):
                if next_timestamp != timestamp:
                    break
                group_last = next_index
            return group_last
    return None


def _build_candidate_table(panel: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    token_rows: list[dict[str, Any]] = []
    for factor in PRIMARY_FACTOR_NAMES + DIAGNOSTIC_FACTOR_NAMES:
        if factor not in panel.columns:
            continue
        for horizon in horizons:
            label = f"future_mid_return_{horizon}s"
            if label not in panel.columns:
                continue
            for (event_id, token_id), group in panel.groupby(["event_id", "token_id"], sort=True):
                valid = group[[factor, label]].dropna()
                token_ic = _safe_corr(valid[factor], valid[label])
                token_rows.append(
                    {
                        "event_id": event_id,
                        "token_id": token_id,
                        "factor": factor,
                        "horizon_seconds": horizon,
                        "token_ic": token_ic,
                        "row_count": len(group),
                    },
                )

    token_metrics = pd.DataFrame(token_rows)
    global_metrics = aggregate_factor_metrics(token_metrics) if not token_metrics.empty else pd.DataFrame()

    cohort_rows: list[dict[str, Any]] = []
    if not token_metrics.empty:
        token_cohorts = panel.groupby(["event_id", "token_id"], sort=True)["source_quality_cohort"].first()
        token_metrics = token_metrics.join(token_cohorts.rename("cohort"), on=["event_id", "token_id"])
        for (factor, horizon, cohort), group in token_metrics.groupby(["factor", "horizon_seconds", "cohort"], sort=True):
            event_ic = group.groupby("event_id", sort=True)["token_ic"].mean()
            cohort_rows.append(
                {
                    "factor": factor,
                    "horizon_seconds": horizon,
                    "cohort": cohort,
                    "event_ic": float(event_ic.mean()) if len(event_ic) else math.nan,
                    "positive_event_share": float((event_ic > 0).mean()) if len(event_ic) else math.nan,
                },
            )
    gates = apply_candidate_gates(pd.DataFrame(cohort_rows))
    if global_metrics.empty:
        return gates
    return global_metrics.merge(gates, on="factor", how="left").sort_values(
        ["factor", "horizon_seconds"],
        kind="mergesort",
    ).reset_index(drop=True)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _safe_corr(left: pd.Series, right: pd.Series) -> float:
    if len(left) < 2 or len(right) < 2:
        return math.nan
    if left.nunique(dropna=True) < 2 or right.nunique(dropna=True) < 2:
        return math.nan
    value = left.corr(right)
    return float(value) if pd.notna(value) else math.nan


def _any_explicit_true(frame: pd.DataFrame, column: str) -> bool:
    if column not in frame.columns:
        return False
    return any(value is True for value in frame[column].dropna())


def _all_explicit_true(frame: pd.DataFrame, column: str) -> bool:
    if column not in frame.columns or frame[column].dropna().empty:
        return False
    return all(value is True for value in frame[column].dropna())


def _frame_digest(frame: pd.DataFrame) -> str:
    stable = frame.copy()
    sort_columns = [
        column
        for column in ("event_id", "token_id", "sequence", "factor", "horizon_seconds", "cohort")
        if column in stable.columns
    ]
    if sort_columns:
        stable = stable.sort_values(sort_columns, kind="mergesort")
    payload = stable.reset_index(drop=True).to_json(orient="split", date_format="iso", default_handler=str)
    return hashlib.sha256(json.dumps(json.loads(payload), sort_keys=True).encode()).hexdigest()

