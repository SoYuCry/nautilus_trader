from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

import pandas as pd

from polymarket._core.models import L2ReplayStepV1
from polymarket._core.models import L2UpdateV1
from polymarket._core.models import PolymarketL2DatasetV1
from polymarket.replay_contract import replay_timestamp
from polymarket.replay_contract import verify_pmxt_replay_clock_order


PRIMARY_HORIZONS_SECONDS = (30, 120, 600)

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

@dataclass
class _TokenState:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    tick_size: float = 0.01
    last_mutation_ts: pd.Timestamp | None = None
    mutation_timestamps: list[pd.Timestamp] = field(default_factory=list)


def build_factor_panel(
    dataset: PolymarketL2DatasetV1,
    *,
    horizons_seconds: Iterable[int],
    include_labels: bool = True,
) -> pd.DataFrame:
    if not isinstance(dataset, PolymarketL2DatasetV1):
        raise TypeError("build_factor_panel requires PolymarketL2DatasetV1 input")
    verify_pmxt_replay_clock_order(dataset)

    states: dict[tuple[str, str], _TokenState] = {}
    records: list[dict[str, Any]] = []
    cohort_by_token = _cohort_by_token(dataset)

    for step in dataset.steps:
        step_ts = pd.Timestamp(replay_timestamp(step))
        touched: dict[tuple[str, str], dict[str, Any]] = {}
        for update in step.updates:
            key = _token_key(update)
            state = states.setdefault(key, _TokenState())
            info = touched.setdefault(
                key,
                {"actual_mutation": False, "event_types": [], "market": key[0], "asset_id": key[1]},
            )
            info["event_types"].append(update.event_type)
            if _apply_update(state, update):
                info["actual_mutation"] = True

        for key, info in touched.items():
            state = states[key]
            actual_mutation = bool(info["actual_mutation"])
            if actual_mutation:
                state.last_mutation_ts = step_ts
                state.mutation_timestamps.append(step_ts)
            records.append(
                _audit_record(
                    state=state,
                    key=key,
                    step=step,
                    step_ts=step_ts,
                    event_type="|".join(dict.fromkeys(info["event_types"])),
                    actual_mutation=actual_mutation,
                    source_quality_cohort=cohort_by_token.get(key[1], "clean"),
                ),
            )

    panel = pd.DataFrame(records)
    if panel.empty:
        return panel
    if include_labels:
        panel = _add_labels(panel, tuple(int(h) for h in horizons_seconds))
    return panel.reset_index(drop=True)


def run_factor_protocol(dataset: PolymarketL2DatasetV1, *, horizons_seconds: Iterable[int]) -> dict[str, Any]:
    if not isinstance(dataset, PolymarketL2DatasetV1):
        raise TypeError("run_factor_protocol requires PolymarketL2DatasetV1 input")
    horizons = tuple(horizons_seconds)
    if horizons != PRIMARY_HORIZONS_SECONDS:
        raise ValueError(f"horizons_seconds must exactly match {PRIMARY_HORIZONS_SECONDS}")
    panel = build_factor_panel(dataset, horizons_seconds=horizons, include_labels=True)
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


def _cohort_by_token(dataset: PolymarketL2DatasetV1) -> dict[str, str]:
    source_quality = getattr(dataset.metadata, "source_quality", {}) or {}
    raw = source_quality.get("cohort_by_token", {})
    return {str(token): str(cohort) for token, cohort in dict(raw).items()}


def _token_key(update: L2UpdateV1) -> tuple[str, str]:
    return (str(update.market), str(update.asset_id))


def _audit_record(
    *,
    state: _TokenState,
    key: tuple[str, str],
    step: L2ReplayStepV1,
    step_ts: pd.Timestamp,
    event_type: str,
    actual_mutation: bool,
    source_quality_cohort: str,
) -> dict[str, Any]:
    bid_levels = _sorted_levels(state.bids, reverse=True)
    ask_levels = _sorted_levels(state.asks, reverse=False)
    bid1 = bid_levels[0][0] if bid_levels else math.nan
    ask1 = ask_levels[0][0] if ask_levels else math.nan
    validity = _book_validity(bid1, ask1)
    valid = validity == "valid"
    mid = (bid1 + ask1) / 2 if valid else math.nan
    spread = ask1 - bid1 if valid else math.nan
    ranking_observation = bool(actual_mutation and valid)
    staleness = (
        0.0
        if actual_mutation
        else float((step_ts - state.last_mutation_ts).total_seconds())
        if state.last_mutation_ts is not None
        else math.nan
    )
    intensity = _mutation_intensity(state.mutation_timestamps, step_ts)

    record = {
        "event_id": key[0],
        "token_id": key[1],
        "market": key[0],
        "asset_id": key[1],
        "sequence": int(step.sequence),
        "timestamp": step_ts,
        "timestamp_received": pd.Timestamp(step.timestamp_received),
        "replay_timestamp": step_ts,
        "event_type": event_type,
        "source_quality_cohort": source_quality_cohort,
        "actual_mutation": actual_mutation,
        "ranking_observation": ranking_observation,
        "book_validity": validity,
        "bid1": bid1,
        "ask1": ask1,
        "mid": mid,
        "spread": spread,
        "tick_size_regime": state.tick_size,
        "book_staleness_seconds": staleness,
        "book_update_intensity": intensity,
        "valid_observation_counts": 1 if valid else 0,
    }
    ranking_values = {
        "depth_imbalance_1": _depth_imbalance(bid_levels, ask_levels, 1),
        "depth_imbalance_3": _depth_imbalance(bid_levels, ask_levels, 3),
        "depth_imbalance_5": _depth_imbalance(bid_levels, ask_levels, 5),
        "microprice_minus_mid": _microprice_minus_mid(bid_levels, ask_levels, mid),
        "top_level_depth": _top_level_depth(bid_levels, ask_levels),
        "depth_slope": _depth_slope(bid_levels, ask_levels),
        "depth_concentration": _depth_concentration(bid_levels, ask_levels),
        "bid_ask_liquidity_asymmetry": _liquidity_asymmetry(bid_levels, ask_levels),
        "distance_to_zero_one": min(mid, 1.0 - mid) if valid else math.nan,
    }
    for factor, value in ranking_values.items():
        record[factor] = value if valid else math.nan
    return record


def _mutation_intensity(mutation_timestamps: list[pd.Timestamp], ts: pd.Timestamp) -> float:
    left = ts - pd.Timedelta(seconds=30)
    return sum(1 for mutation_ts in mutation_timestamps if left < mutation_ts <= ts) / 30.0


def _apply_update(state: _TokenState, update: L2UpdateV1) -> bool:
    if update.event_type == "book":
        new_bids = _levels_from(update.bids)
        new_asks = _levels_from(update.asks)
        changed = new_bids != state.bids or new_asks != state.asks
        state.bids = new_bids
        state.asks = new_asks
        return changed
    if update.event_type == "price_change":
        return _apply_price_change(state, update)
    if update.event_type == "tick_size_change":
        _apply_tick_size_change(state, update)
        return False
    return False


def _apply_price_change(state: _TokenState, update: L2UpdateV1) -> bool:
    side = str(update.side or "").upper()
    book = state.bids if side == "BUY" else state.asks if side == "SELL" else None
    if book is None:
        return False
    price = _to_float(update.price)
    size = _to_float(update.size)
    if not math.isfinite(price) or not math.isfinite(size):
        return False
    old_size = book.get(price)
    if size <= 0:
        if old_size is None:
            return False
        del book[price]
        return True
    if old_size == size:
        return False
    book[price] = size
    return True


def _apply_tick_size_change(state: _TokenState, update: L2UpdateV1) -> None:
    old_tick = _to_float(update.old_tick_size)
    new_tick = _to_float(update.new_tick_size)
    if not math.isfinite(old_tick) or not math.isfinite(new_tick):
        raise ValueError("tick_size_change requires old_tick_size and new_tick_size")
    if not math.isclose(old_tick, state.tick_size, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("tick_size_change old_tick_size does not match current token tick state")
    if not math.isclose(new_tick, 0.001, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("tick_size_change only supports transition to 0.001")
    state.tick_size = new_tick


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
                "row_count": int(row_count_sum),
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
        primary_horizon_ics = (
            clean[clean["horizon_seconds"].isin(PRIMARY_HORIZONS_SECONDS)]
            .groupby("horizon_seconds", sort=True)["event_ic"]
            .mean()
            .dropna()
        )
        same_sign_two_horizons = bool(
            (primary_horizon_ics > 0).sum() >= 2 or (primary_horizon_ics < 0).sum() >= 2,
        )
        bootstrap_scope = clean[clean["horizon_seconds"].isin(PRIMARY_HORIZONS_SECONDS)]
        block_bootstrap_interval_excludes_zero = _any_explicit_true(
            bootstrap_scope,
            "block_bootstrap_interval_excludes_zero",
        )
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


def _levels_from(values: Iterable[Any]) -> dict[float, float]:
    levels: dict[float, float] = {}
    for value in values:
        price = _to_float(getattr(value, "price", None))
        size = _to_float(getattr(value, "size", None))
        if math.isfinite(price) and math.isfinite(size) and size > 0:
            levels[price] = size
    return levels


def _sorted_levels(levels: dict[float, float], *, reverse: bool) -> list[tuple[float, float]]:
    return sorted(levels.items(), key=lambda item: item[0], reverse=reverse)


def _book_validity(bid1: float, ask1: float) -> str:
    if not math.isfinite(bid1) or not math.isfinite(ask1):
        return "missing"
    if bid1 == ask1:
        return "locked"
    if bid1 > ask1:
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
    def side_g(levels: list[tuple[float, float]]) -> float:
        sizes = [size for _, size in levels[:5]]
        sizes.extend([0.0] * (5 - len(sizes)))
        total = sum(sizes)
        if total <= 0:
            return math.nan
        xs = (0.0, 0.25, 0.5, 0.75, 1.0)
        return 2.0 * sum(x * (size / total) for x, size in zip(xs, sizes, strict=True)) - 1.0

    bid_g = side_g(bids)
    ask_g = side_g(asks)
    if not math.isfinite(bid_g) or not math.isfinite(ask_g):
        return math.nan
    return (bid_g + ask_g) / 2.0


def _depth_concentration(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    if not bids or not asks:
        return math.nan
    bid_total = sum(size for _, size in bids[:5])
    ask_total = sum(size for _, size in asks[:5])
    if bid_total <= 0 or ask_total <= 0:
        return math.nan
    return ((bids[0][1] / bid_total) + (asks[0][1] / ask_total)) / 2.0


def _liquidity_asymmetry(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
    if not bids or not asks:
        return math.nan
    bid_total = sum(size for _, size in bids[:5])
    ask_total = sum(size for _, size in asks[:5])
    if bid_total <= 0 or ask_total <= 0:
        return math.nan
    c_bid = bids[0][1] / bid_total
    c_ask = asks[0][1] / ask_total
    denom = c_bid + c_ask
    return (c_bid - c_ask) / denom if denom else math.nan


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
        token = panel.loc[indexes]
        anchor_token = token[token["ranking_observation"]]
        target_token = token[token["actual_mutation"]]
        anchor_indexes = list(anchor_token.index)
        target_indexes = list(target_token.index)
        target_timestamps = list(target_token["timestamp"])
        for position, index in enumerate(anchor_indexes):
            current_ts = panel.loc[index, "timestamp"]
            current_mid = panel.loc[index, "mid"]
            for horizon in horizons:
                target = current_ts + pd.Timedelta(seconds=horizon)
                match_index = _first_at_or_after_group_last(target_indexes, target_timestamps, target)
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
            future_anchor_indexes = anchor_indexes[position + 1 :]
            for future_index in future_anchor_indexes:
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
                ranking = group[group["ranking_observation"]]
                valid = ranking[[factor, label]].dropna()
                token_ic = _safe_corr(valid[factor], valid[label])
                token_rows.append(
                    {
                        "event_id": event_id,
                        "token_id": token_id,
                        "factor": factor,
                        "horizon_seconds": horizon,
                        "token_ic": token_ic,
                        "row_count": len(valid),
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
    if frame.empty or column not in frame.columns:
        return False
    return any(type(value) is bool and value is True for value in frame[column])


def _all_explicit_true(frame: pd.DataFrame, column: str) -> bool:
    if frame.empty or column not in frame.columns:
        return False
    return all(type(value) is bool and value is True for value in frame[column])


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

