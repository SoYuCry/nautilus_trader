from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from bisect import bisect_right
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from heapq import heapify
from heapq import heappop
from heapq import heappush
from typing import Any

import pandas as pd

from polymarket._core.models import L2ReplayStepV1
from polymarket._core.models import L2UpdateV1
from polymarket._core.models import LevelV1
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

_SUPPORTED_EVENT_TYPES = frozenset({"book", "price_change", "trade", "tick_size_change"})
_IDENTITY_COLUMNS = ["event_id", "market", "token_id"]
_DEFAULT_TICK_SIZE = Decimal("0.01")
_NARROW_TICK_SIZE = Decimal("0.001")
_DECIMAL_ZERO = Decimal(0)
_DECIMAL_ONE = Decimal(1)
_MISSING_LEVEL = object()
_BOOK_SIDES = ("BUY", "SELL")


@dataclass(frozen=True)
class _LabelBarrier:
    timestamp: pd.Timestamp
    reason: str
    kind: str
    provenance: str | None = None


@dataclass
class _BarrierIndex:
    token: dict[tuple[str, str], list[_LabelBarrier]] = field(default_factory=dict)
    market: dict[str, list[_LabelBarrier]] = field(default_factory=dict)

    def for_token(self, key: tuple[str, str]) -> list[_LabelBarrier]:
        barriers = [*self.token.get(key, ()), *self.market.get(key[0], ())]
        barriers.sort(key=lambda barrier: barrier.timestamp)
        return barriers


@dataclass
class _StepBookTracker:
    original_levels: dict[tuple[str, float], float | object] = field(default_factory=dict)
    pre_snapshot_sides: dict[str, dict[float, float]] = field(default_factory=dict)

    def record_level(self, state: _TokenState, side: str, price: float) -> None:
        if side in self.pre_snapshot_sides:
            return
        book = _book_side(state, side)
        self.original_levels.setdefault((side, price), book.get(price, _MISSING_LEVEL))

    def capture_snapshot_side(self, state: _TokenState, side: str) -> None:
        if side in self.pre_snapshot_sides:
            return
        pre_step = _book_side(state, side).copy()
        for (tracked_side, price), original in self.original_levels.items():
            if tracked_side != side:
                continue
            if original is _MISSING_LEVEL:
                pre_step.pop(price, None)
            else:
                pre_step[price] = float(original)
        self.pre_snapshot_sides[side] = pre_step

    def final_state_changed(self, state: _TokenState) -> bool:
        for side, pre_step in self.pre_snapshot_sides.items():
            if _book_side(state, side) != pre_step:
                return True
        for (side, price), original in self.original_levels.items():
            if side in self.pre_snapshot_sides:
                continue
            if _book_side(state, side).get(price, _MISSING_LEVEL) != original:
                return True
        return False


@dataclass
class _TokenState:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    bid_heap: list[tuple[float, int, float]] = field(default_factory=list)
    ask_heap: list[tuple[float, int, float]] = field(default_factory=list)
    bid_versions: dict[float, int] = field(default_factory=dict)
    ask_versions: dict[float, int] = field(default_factory=dict)
    level_version: int = 0
    book_cache: dict[str, Any] | None = None
    tick_size: Decimal = _DEFAULT_TICK_SIZE
    last_mutation_ts: pd.Timestamp | None = None
    mutation_timestamps: deque[pd.Timestamp] = field(default_factory=deque)


def build_factor_panel(
    dataset: PolymarketL2DatasetV1,
    *,
    horizons_seconds: Iterable[int],
    include_labels: bool = True,
) -> pd.DataFrame:
    if not isinstance(dataset, PolymarketL2DatasetV1):
        raise TypeError("build_factor_panel requires PolymarketL2DatasetV1 input")
    verify_pmxt_replay_clock_order(dataset)
    _validate_dataset(dataset)
    barrier_index = _build_barrier_index(dataset)

    states: dict[tuple[str, str], _TokenState] = {}
    records: list[dict[str, Any]] = []
    cohort_by_token = _cohort_by_token(dataset)
    event_id = dataset.metadata.dataset_id
    horizons = tuple(int(horizon) for horizon in horizons_seconds)

    for step in dataset.steps:
        step_ts = pd.Timestamp(replay_timestamp(step))
        touched: dict[tuple[str, str], list[str]] = {}
        for update in step.updates:
            key = _token_key(update)
            touched.setdefault(key, []).append(update.event_type)
            states.setdefault(key, _TokenState())

        trackers = {key: _StepBookTracker() for key in touched}
        for update in step.updates:
            key = _token_key(update)
            _apply_update(states[key], update, trackers[key])

        for key, event_types in touched.items():
            state = states[key]
            actual_mutation = trackers[key].final_state_changed(state)
            if actual_mutation:
                state.book_cache = None
            _advance_mutation_window(state, step_ts, actual_mutation=actual_mutation)
            barriers = barrier_index.for_token(key)
            records.append(
                _audit_record(
                    state=state,
                    key=key,
                    step=step,
                    step_ts=step_ts,
                    event_id=event_id,
                    event_type="|".join(dict.fromkeys(event_types)),
                    actual_mutation=actual_mutation,
                    source_quality_cohort=cohort_by_token.get(key[1], "unknown"),
                    hard_break_provenance=_next_hard_break_provenance(barriers, step_ts),
                ),
            )

    panel = pd.DataFrame(records)
    if panel.empty:
        return panel
    if include_labels:
        panel = _add_labels(panel, horizons, barrier_index)
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
    if not isinstance(source_quality, dict):
        return {}
    raw = source_quality.get("cohort_by_token", {})
    if not isinstance(raw, dict):
        return {}
    return {
        token: cohort
        for token, cohort in raw.items()
        if isinstance(token, str) and isinstance(cohort, str) and cohort in {"clean", "degraded"}
    }


def _build_barrier_index(dataset: PolymarketL2DatasetV1) -> _BarrierIndex:
    barriers = _BarrierIndex()
    _add_known_archive_gaps(dataset, barriers)
    _add_resolution_closes(dataset, barriers)
    for values in (*barriers.token.values(), *barriers.market.values()):
        values.sort(key=lambda barrier: barrier.timestamp)
    return barriers


def _add_known_archive_gaps(dataset: PolymarketL2DatasetV1, barriers: _BarrierIndex) -> None:
    source_quality = getattr(dataset.metadata, "source_quality", {}) or {}
    if not isinstance(source_quality, dict):
        return
    raw_gaps = source_quality.get("knownArchiveGaps", [])
    if not isinstance(raw_gaps, list):
        raise ValueError("source_quality.knownArchiveGaps must be a list")
    for position, raw_gap in enumerate(raw_gaps):
        context = f"source_quality.knownArchiveGaps[{position}]"
        market, asset_id, provenance, start, end = _parse_known_archive_gap(raw_gap, context)
        if end <= start:
            raise ValueError(f"{context}.end must be later than start")
        barriers.token.setdefault((market, asset_id), []).append(
            _LabelBarrier(
                timestamp=start,
                reason=f"hard_break:{provenance}",
                kind="hard_break",
                provenance=provenance,
            ),
        )


def _parse_known_archive_gap(
    raw_gap: Any,
    context: str,
) -> tuple[str, str, str, pd.Timestamp, pd.Timestamp]:
    expected_fields = {"market", "asset_id", "start", "end", "provenance"}
    if not isinstance(raw_gap, dict):
        raise ValueError(f"{context} must be an object")
    missing = expected_fields - raw_gap.keys()
    extra = raw_gap.keys() - expected_fields
    if missing:
        raise ValueError(f"{context}.{sorted(missing)[0]} is required")
    if extra:
        raise ValueError(f"{context} has unsupported field {sorted(extra)[0]!r}")
    market = raw_gap["market"]
    asset_id = raw_gap["asset_id"]
    provenance = raw_gap["provenance"]
    if not isinstance(market, str) or not market.strip():
        raise ValueError(f"{context}.market must be a nonempty string")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError(f"{context}.asset_id must be a nonempty string")
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValueError(f"{context}.provenance must be a nonempty string")
    start = _parse_utc_metadata_timestamp(raw_gap["start"], f"{context}.start")
    end = _parse_utc_metadata_timestamp(raw_gap["end"], f"{context}.end")
    return market, asset_id, provenance, start, end


def _add_resolution_closes(dataset: PolymarketL2DatasetV1, barriers: _BarrierIndex) -> None:
    for position, metadata in enumerate(dataset.metadata.market_metadata):
        if metadata.resolution_time is None:
            continue
        context = f"market_metadata[{position}]"
        market = metadata.condition_id
        if not isinstance(market, str) or not market.strip():
            raise ValueError(f"{context}.condition_id must be a nonempty string")
        close_time = _parse_utc_metadata_timestamp(metadata.resolution_time, f"{context}.resolution_time")
        source = metadata.resolution_source if isinstance(metadata.resolution_source, str) else "unknown"
        if metadata.token_id is None:
            barriers.market.setdefault(market, []).append(
                _LabelBarrier(
                    timestamp=close_time,
                    reason=f"close:{source}",
                    kind="close",
                ),
            )
            continue
        if not isinstance(metadata.token_id, str) or not metadata.token_id.strip():
            raise ValueError(f"{context}.token_id must be a nonempty string or None")
        barriers.token.setdefault((market, metadata.token_id), []).append(
            _LabelBarrier(
                timestamp=close_time,
                reason=f"token_close:{source}",
                kind="token_close",
            ),
        )


def _parse_utc_metadata_timestamp(value: Any, context: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context} must be a UTC-parseable timestamp") from exc
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError(f"{context} must be a UTC-parseable timezone-aware timestamp")
    return timestamp.tz_convert("UTC")


def _token_key(update: L2UpdateV1) -> tuple[str, str]:
    return (update.market, update.asset_id)


def _validate_dataset(dataset: PolymarketL2DatasetV1) -> None:
    tick_sizes: dict[tuple[str, str], Decimal] = {}
    for step in dataset.steps:
        if not step.updates:
            raise _validation_error(step, None, "updates", "must contain at least one update")
        for update in step.updates:
            _validate_update(step, update, tick_sizes)


def _validate_update(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    tick_sizes: dict[tuple[str, str], Decimal],
) -> None:
    if not isinstance(update.event_type, str) or update.event_type not in _SUPPORTED_EVENT_TYPES:
        raise _validation_error(step, update, "event_type", "is not supported")
    if not isinstance(update.market, str) or not update.market.strip():
        raise _validation_error(step, update, "market", "must be nonempty")
    if not isinstance(update.asset_id, str) or not update.asset_id.strip():
        raise _validation_error(step, update, "asset_id", "must be nonempty")

    if update.event_type == "book":
        _validate_levels(step, update, "bids", update.bids)
        _validate_levels(step, update, "asks", update.asks)
        return
    if update.event_type == "price_change":
        _validate_side(step, update)
        _validate_number(step, update, "price", update.price, minimum=0.0, maximum=1.0)
        _validate_number(step, update, "size", update.size, minimum=0.0)
        return
    if update.event_type == "trade":
        _validate_side(step, update)
        _validate_number(step, update, "price", update.price, minimum=0.0, maximum=1.0)
        _validate_number(step, update, "size", update.size, minimum=0.0, minimum_inclusive=False)
        return

    key = _token_key(update)
    current_tick = tick_sizes.get(key, _DEFAULT_TICK_SIZE)
    old_tick = update.old_tick_size
    new_tick = update.new_tick_size
    if not isinstance(old_tick, Decimal) or not old_tick.is_finite() or old_tick != current_tick:
        raise _validation_error(
            step,
            update,
            "old_tick_size",
            f"must equal current token tick size {current_tick}",
        )
    if old_tick != _DEFAULT_TICK_SIZE:
        raise _validation_error(step, update, "old_tick_size", "only 0.01 -> 0.001 is supported")
    if not isinstance(new_tick, Decimal) or not new_tick.is_finite() or new_tick != _NARROW_TICK_SIZE:
        raise _validation_error(step, update, "new_tick_size", "only 0.01 -> 0.001 is supported")
    tick_sizes[key] = _NARROW_TICK_SIZE


def _validate_levels(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    side_name: str,
    levels: Iterable[Any] | None,
) -> None:
    if levels is None:
        raise _validation_error(step, update, side_name, "must be an iterable of price levels")
    try:
        iterator = iter(levels)
    except TypeError as exc:
        raise _validation_error(step, update, side_name, "must be an iterable of price levels") from exc
    seen_prices: set[float] = set()
    for position, level in enumerate(iterator):
        context = f"{side_name}[{position}]"
        if not isinstance(level, LevelV1):
            raise _validation_error(step, update, context, "must be LevelV1")
        price = _validate_number(
            step,
            update,
            f"{context}.price",
            getattr(level, "price", None),
            minimum=0.0,
            maximum=1.0,
        )
        if price in seen_prices:
            raise _validation_error(
                step,
                update,
                f"{context}.price",
                f"duplicate {side_name} price level {getattr(level, 'price', None)!r}",
            )
        seen_prices.add(price)
        _validate_number(
            step,
            update,
            f"{context}.size",
            getattr(level, "size", None),
            minimum=0.0,
            minimum_inclusive=False,
        )


def _validate_side(step: L2ReplayStepV1, update: L2UpdateV1) -> None:
    if update.side not in ("BUY", "SELL"):
        raise _validation_error(step, update, "side", "must be exactly BUY or SELL")


def _validate_number(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    field_name: str,
    raw_value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
) -> float:
    if not isinstance(raw_value, Decimal):
        raise _validation_error(step, update, field_name, "must be Decimal")
    value = float(raw_value)
    if not math.isfinite(value):
        raise _validation_error(step, update, field_name, "must be finite")
    if minimum is not None and (value < minimum if minimum_inclusive else value <= minimum):
        operator = ">=" if minimum_inclusive else ">"
        raise _validation_error(step, update, field_name, f"must be {operator} {minimum}")
    if maximum is not None and value > maximum:
        raise _validation_error(step, update, field_name, f"must be <= {maximum}")
    return value


def _validation_error(
    step: L2ReplayStepV1,
    update: L2UpdateV1 | None,
    field_name: str,
    detail: str,
) -> ValueError:
    event_type = update.event_type if update is not None else "<missing>"
    market = update.market if update is not None else "<missing>"
    asset_id = update.asset_id if update is not None else "<missing>"
    return ValueError(
        f"sequence={step.sequence}, event_type={event_type!r}, market={market!r}, "
        f"asset_id={asset_id!r}, field={field_name}: {detail}",
    )


def _audit_record(
    *,
    state: _TokenState,
    key: tuple[str, str],
    step: L2ReplayStepV1,
    step_ts: pd.Timestamp,
    event_id: str,
    event_type: str,
    actual_mutation: bool,
    source_quality_cohort: str,
    hard_break_provenance: str | None,
) -> dict[str, Any]:
    cached = _book_snapshot(state)
    bid1 = cached["bid1"]
    ask1 = cached["ask1"]
    validity = cached["validity"]
    valid = validity == "valid"
    mid = cached["mid"]
    spread = cached["spread"]
    ranking_observation = bool(actual_mutation and valid)
    staleness = (
        0.0
        if actual_mutation
        else float((step_ts - state.last_mutation_ts).total_seconds())
        if state.last_mutation_ts is not None
        else math.nan
    )
    intensity = len(state.mutation_timestamps) / 30.0

    record = {
        "event_id": event_id,
        "token_id": key[1],
        "market": key[0],
        "asset_id": key[1],
        "sequence": int(step.sequence),
        "timestamp": step_ts,
        "timestamp_received": pd.Timestamp(step.timestamp_received),
        "replay_timestamp": step_ts,
        "event_type": event_type,
        "source_quality_cohort": source_quality_cohort,
        "hard_break_provenance": hard_break_provenance,
        "actual_mutation": actual_mutation,
        "ranking_observation": ranking_observation,
        "book_validity": validity,
        "bid1": bid1,
        "ask1": ask1,
        "mid": mid,
        "spread": spread,
        "tick_size_regime": float(state.tick_size),
        "book_staleness_seconds": staleness,
        "book_update_intensity": intensity,
        "valid_observation_counts": 1 if valid else 0,
    }
    ranking_values = cached["ranking_values"]
    for factor, value in ranking_values.items():
        record[factor] = value if valid else math.nan
    return record


def _advance_mutation_window(
    state: _TokenState,
    ts: pd.Timestamp,
    *,
    actual_mutation: bool,
) -> None:
    left = ts - pd.Timedelta(seconds=30)
    while state.mutation_timestamps and state.mutation_timestamps[0] <= left:
        state.mutation_timestamps.popleft()
    if actual_mutation:
        state.last_mutation_ts = ts
        state.mutation_timestamps.append(ts)


def _apply_update(state: _TokenState, update: L2UpdateV1, tracker: _StepBookTracker) -> None:
    if update.event_type == "book":
        tracker.capture_snapshot_side(state, "BUY")
        tracker.capture_snapshot_side(state, "SELL")
        state.bids = _levels_from(update.bids)
        state.asks = _levels_from(update.asks)
        _rebuild_heaps(state)
        state.book_cache = None
    elif update.event_type == "price_change":
        _apply_price_change(state, update, tracker)
    elif update.event_type == "tick_size_change":
        state.tick_size = _NARROW_TICK_SIZE


def _apply_price_change(state: _TokenState, update: L2UpdateV1, tracker: _StepBookTracker) -> None:
    book = state.bids if update.side == "BUY" else state.asks
    versions = state.bid_versions if update.side == "BUY" else state.ask_versions
    heap = state.bid_heap if update.side == "BUY" else state.ask_heap
    price = float(update.price)  # type: ignore[arg-type]
    size = float(update.size)  # type: ignore[arg-type]
    tracker.record_level(state, update.side, price)
    current = book.get(price, _MISSING_LEVEL)
    if (size == 0.0 and current is _MISSING_LEVEL) or current == size:
        return
    state.level_version += 1
    versions[price] = state.level_version
    if size == 0.0:
        book.pop(price, None)
    else:
        book[price] = size
        priority = -price if update.side == "BUY" else price
        heappush(heap, (priority, state.level_version, price))
    state.book_cache = None


def aggregate_factor_metrics(token_rows: pd.DataFrame) -> pd.DataFrame:
    if token_rows.empty:
        return pd.DataFrame()

    out: list[dict[str, Any]] = []
    for (factor, horizon), group in token_rows.groupby(["factor", "horizon_seconds"], sort=True):
        event_ic = group.groupby("event_id", sort=True)["token_ic"].mean()
        row_weight_sum = float((group["token_ic"] * group["row_count"]).sum())
        row_count_sum = float(group["row_count"].sum())
        identity_columns = [column for column in _IDENTITY_COLUMNS if column in group.columns]
        out.append(
            {
                "factor": factor,
                "horizon_seconds": horizon,
                "event_count": int(group["event_id"].nunique()),
                "token_count": len(group[identity_columns].drop_duplicates()),
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
    return {float(value.price): float(value.size) for value in values}


def _book_side(state: _TokenState, side: str) -> dict[float, float]:
    return state.bids if side == "BUY" else state.asks


def _rebuild_heaps(state: _TokenState) -> None:
    state.level_version += 1
    base_version = state.level_version
    state.bid_versions = dict.fromkeys(state.bids, base_version)
    state.ask_versions = dict.fromkeys(state.asks, base_version)
    state.bid_heap = [(-price, base_version, price) for price in state.bids]
    state.ask_heap = [(price, base_version, price) for price in state.asks]
    heapify(state.bid_heap)
    heapify(state.ask_heap)


def _top_levels(state: _TokenState, side: str, depth: int = 5) -> list[tuple[float, float]]:
    book = state.bids if side == "BUY" else state.asks
    heap = state.bid_heap if side == "BUY" else state.ask_heap
    versions = state.bid_versions if side == "BUY" else state.ask_versions
    out: list[tuple[float, float]] = []
    skipped: list[tuple[float, int, float]] = []
    while heap and len(out) < depth:
        priority, version, price = heappop(heap)
        if versions.get(price) != version or price not in book:
            continue
        out.append((price, book[price]))
        skipped.append((priority, version, price))
    for item in skipped:
        heappush(heap, item)
    return out


def _book_snapshot(state: _TokenState) -> dict[str, Any]:
    if state.book_cache is not None:
        return state.book_cache
    bid_levels = _top_levels(state, "BUY")
    ask_levels = _top_levels(state, "SELL")
    bid1 = bid_levels[0][0] if bid_levels else math.nan
    ask1 = ask_levels[0][0] if ask_levels else math.nan
    validity = _book_validity(bid1, ask1)
    valid = validity == "valid"
    mid = (bid1 + ask1) / 2 if valid else math.nan
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
    state.book_cache = {
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "bid1": bid1,
        "ask1": ask1,
        "validity": validity,
        "mid": mid,
        "spread": ask1 - bid1 if valid else math.nan,
        "ranking_values": ranking_values,
    }
    return state.book_cache


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


def _add_labels(panel: pd.DataFrame, horizons: tuple[int, ...], barrier_index: _BarrierIndex) -> pd.DataFrame:
    panel = panel.copy()
    for horizon in horizons:
        panel[f"future_mid_{horizon}s"] = pd.Series(math.nan, index=panel.index, dtype="float64")
        panel[f"future_mid_return_{horizon}s"] = pd.Series(math.nan, index=panel.index, dtype="float64")
        panel[f"future_book_validity_{horizon}s"] = pd.Series(None, index=panel.index, dtype="object")
        panel[f"label_censor_reason_{horizon}s"] = pd.Series(None, index=panel.index, dtype="object")
        panel[f"label_matched_timestamp_{horizon}s"] = pd.Series(
            pd.NaT,
            index=panel.index,
            dtype="datetime64[ns, UTC]",
        )

    panel["next_nonzero_mid_move"] = pd.Series(math.nan, index=panel.index, dtype="float64")
    panel["next_nonzero_mid_move_direction"] = pd.Series(math.nan, index=panel.index, dtype="float64")
    panel["next_nonzero_mid_move_matched_sequence"] = pd.Series(pd.NA, index=panel.index, dtype="Int64")
    panel["next_nonzero_mid_move_censor_reason"] = pd.Series(None, index=panel.index, dtype="object")

    for (_, market, token_id), token in panel.groupby(_IDENTITY_COLUMNS, sort=False):
        barriers = barrier_index.for_token((market, token_id))
        anchors = token[token["ranking_observation"]]
        mutations = token[token["actual_mutation"]]
        target_indexes = list(mutations.index)
        target_timestamps = list(mutations["timestamp"])

        for index in anchors.index:
            current_ts = panel.loc[index, "timestamp"]
            current_mid = float(panel.loc[index, "mid"])
            for horizon in horizons:
                target = current_ts + pd.Timedelta(seconds=horizon)
                match_index = _fixed_horizon_match(target_indexes, target_timestamps, target)
                if match_index is None:
                    continue
                barrier = _first_barrier_between(barriers, current_ts, panel.loc[match_index, "timestamp"])
                if barrier is not None:
                    panel.loc[index, f"label_censor_reason_{horizon}s"] = _barrier_reason(barrier)
                    continue
                future_mid = float(panel.loc[match_index, "mid"])
                future_validity = panel.loc[match_index, "book_validity"]
                panel.loc[index, f"label_matched_timestamp_{horizon}s"] = panel.loc[match_index, "timestamp"]
                panel.loc[index, f"future_book_validity_{horizon}s"] = future_validity
                if future_validity == "valid" and math.isfinite(current_mid) and math.isfinite(future_mid):
                    panel.loc[index, f"future_mid_{horizon}s"] = future_mid
                    panel.loc[index, f"future_mid_return_{horizon}s"] = future_mid - current_mid

        _add_next_nonzero_group_labels(panel, mutations, barriers)
    return panel


def _fixed_horizon_match(
    indexes: list[int],
    timestamps: list[pd.Timestamp],
    target: pd.Timestamp,
) -> int | None:
    position = bisect_left(timestamps, target)
    if position == len(timestamps):
        return None
    final_position = bisect_right(timestamps, timestamps[position]) - 1
    return indexes[final_position]


def _add_next_nonzero_group_labels(panel: pd.DataFrame, mutations: pd.DataFrame, barriers: list[_LabelBarrier]) -> None:
    valid_segment: list[int] = []
    for row in mutations.itertuples():
        if row.book_validity == "valid":
            valid_segment.append(row.Index)
        else:
            _add_next_nonzero_segment_labels(panel, valid_segment, barriers)
            valid_segment = []
    _add_next_nonzero_segment_labels(panel, valid_segment, barriers)


def _add_next_nonzero_segment_labels(panel: pd.DataFrame, indexes: list[int], barriers: list[_LabelBarrier]) -> None:
    if len(indexes) < 2:
        return
    mids = [float(panel.loc[index, "mid"]) for index in indexes]
    next_different: list[int | None] = [None] * len(indexes)
    for position in range(len(indexes) - 2, -1, -1):
        if mids[position + 1] != mids[position]:
            next_different[position] = position + 1
        else:
            next_different[position] = next_different[position + 1]

    for position, target_position in enumerate(next_different):
        if target_position is None:
            continue
        index = indexes[position]
        target_index = indexes[target_position]
        barrier = _first_barrier_between(
            barriers,
            panel.loc[index, "timestamp"],
            panel.loc[target_index, "timestamp"],
        )
        if barrier is not None:
            panel.loc[index, "next_nonzero_mid_move_censor_reason"] = _barrier_reason(barrier)
            continue
        move = mids[target_position] - mids[position]
        panel.loc[index, "next_nonzero_mid_move"] = move
        panel.loc[index, "next_nonzero_mid_move_direction"] = 1.0 if move > 0.0 else -1.0
        panel.loc[index, "next_nonzero_mid_move_matched_sequence"] = int(panel.loc[target_index, "sequence"])


def _first_barrier_between(
    barriers: list[_LabelBarrier],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> _LabelBarrier | None:
    for barrier in barriers:
        if start < barrier.timestamp <= end:
            return barrier
        if barrier.timestamp > end:
            return None
    return None


def _barrier_reason(barrier: _LabelBarrier) -> str:
    return f"{barrier.reason}:{barrier.provenance}" if barrier.provenance else barrier.reason


def _next_hard_break_provenance(barriers: list[_LabelBarrier], ts: pd.Timestamp) -> str | None:
    for barrier in barriers:
        if barrier.kind == "hard_break" and barrier.timestamp >= ts:
            return barrier.provenance
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
            for (event_id, market, token_id), group in panel.groupby(_IDENTITY_COLUMNS, sort=True):
                ranking = group[group["ranking_observation"]]
                valid = ranking[[factor, label]].dropna()
                token_ic = _safe_corr(valid[factor], valid[label])
                token_rows.append(
                    {
                        "event_id": event_id,
                        "market": market,
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
        token_cohorts = panel.groupby(_IDENTITY_COLUMNS, sort=True)["source_quality_cohort"].first()
        token_metrics = token_metrics.join(token_cohorts.rename("cohort"), on=_IDENTITY_COLUMNS)
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
    pairs = pd.concat({"factor": left, "label": right}, axis=1, join="inner").dropna()
    if len(pairs) < 2:
        return math.nan
    if pairs["factor"].nunique() < 2 or pairs["label"].nunique() < 2:
        return math.nan
    factor_ranks = pairs["factor"].rank(method="average")
    label_ranks = pairs["label"].rank(method="average")
    value = factor_ranks.corr(label_ranks)
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

