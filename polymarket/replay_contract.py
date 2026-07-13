"""
Shared replay-order contract for Polymarket v1 research datasets.

This module is the single definition of replay ordering semantics shared by
PMXT factor research and the Nautilus-native research backtest runner.  It has
no NautilusTrader imports so contract logic stays testable without the
compiled runtime.

Two data-trust modes exist:

- ``strict_capture``: raw WebSocket or equivalent captures with a real local
  receive clock and preserved message boundaries.  Replay chronology is
  ``timestamp_received`` in capture order; receive-time inversions are hard
  failures.
- ``pmxt_research``: curated PMXT event data replayed in the deterministic
  reconstructed order produced by ``pmxt_event_v1`` (stable sort by source
  ``timestamp``, then ``timestamp_received``, then original row index).  The
  replay clock is the source ``timestamp`` with ``timestamp_received``
  fallback.  This order is reproducible but is not exchange message order and
  not L3 queue truth; receive-time inversions are diagnostics, not blockers.

The runner default is ``strict_capture``; PMXT data may only run a Nautilus
research backtest when the experiment config explicitly selects
``replay.mode: pmxt_research``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any


if TYPE_CHECKING:
    from polymarket._core.models import L2ReplayStepV1
    from polymarket._core.models import PolymarketL2DatasetV1
    from polymarket.data_health import DataHealthIssueV1
    from polymarket.data_health import DataHealthReportV1


STRICT_CAPTURE_MODE = "strict_capture"
PMXT_RESEARCH_MODE = "pmxt_research"
REPLAY_MODES = (STRICT_CAPTURE_MODE, PMXT_RESEARCH_MODE)
DEFAULT_REPLAY_MODE = STRICT_CAPTURE_MODE

PMXT_ADAPTER_NAME = "pmxt_event_v1"

# One stable ordering-key definition shared by the PMXT adapter, factor
# research trust metadata, and backtest provenance reporting.
PMXT_RESEARCH_ORDERING_KEY = "timestamp,timestamp_received,_original_row_index"
STRICT_CAPTURE_ORDERING_KEY = "capture_order(timestamp_received non-decreasing enforced)"

RECEIVE_TIME_REPLAY_CLOCK = "timestamp_received"
PMXT_REPLAY_CLOCK = "pmxt_replay_timestamp"
PMXT_SOURCE_TIME_POLICY = "timestamp_fallback_timestamp_received"

# Panel/frame column names for the two clocks.
PMXT_REPLAY_TIMESTAMP_COLUMN = "replay_timestamp"
LEGACY_RECEIVE_TIME_COLUMN = "timestamp_received"

# Health error codes that are diagnostics rather than blockers under PMXT
# research replay: the adapter sorts on source time, so receive time is not
# monotonic by construction.  Everything else (e.g. sequence_inversion) still
# blocks in both modes.
PMXT_RESEARCH_ALLOWED_HEALTH_ERROR_CODES = frozenset({"receive_time_inversion"})

PMXT_RESEARCH_DISCLAIMER = (
    "PMXT research backtest: replay order is the deterministic reconstructed "
    "PMXT ordering (timestamp, timestamp_received, original row index), not "
    "true exchange/message order and not L3 queue truth. Fills, positions, and "
    "PnL are research results computed by Nautilus BacktestEngine on a "
    "reconstructed L2 replay; they are not matching-level evidence and must "
    "not be read as production execution predictions."
)
STRICT_CAPTURE_STATEMENT = (
    "Strict capture replay: local receive-time ordered raw capture with "
    "preserved message boundaries; receive-time inversions are hard failures."
)


def replay_timestamp(step: L2ReplayStepV1) -> datetime:
    """
    Return the PMXT research replay clock for one step.

    Source ``timestamp`` is primary; ``timestamp_received`` is the fallback for
    rows without a source timestamp.  This is the same clock used to sort the
    adapter output, so it is non-decreasing in step order by construction.
    """
    return step.timestamp or step.timestamp_received


def replay_time_column(panel: Any, *, allow_legacy_receive_time: bool = False) -> str:
    """
    Return the panel column holding the PMXT research replay clock.

    Panels produced by the current factor pipeline always carry
    ``replay_timestamp``.  Old panels written before the shared contract only
    have ``timestamp_received``; consuming them silently would mix two ordering
    semantics, so the legacy fallback must be requested explicitly (e.g. via a
    config flag) and callers should surface that choice in their outputs.
    """
    columns = set(getattr(panel, "columns", panel))
    if PMXT_REPLAY_TIMESTAMP_COLUMN in columns:
        return PMXT_REPLAY_TIMESTAMP_COLUMN
    if allow_legacy_receive_time and LEGACY_RECEIVE_TIME_COLUMN in columns:
        return LEGACY_RECEIVE_TIME_COLUMN
    raise ValueError(
        f"panel has no {PMXT_REPLAY_TIMESTAMP_COLUMN!r} column; it predates the shared "
        "PMXT replay contract. Re-generate it, or explicitly opt in to legacy "
        "receive-time ordering (allow_legacy_receive_time=true) and mark the outputs.",
    )


def resolve_replay_mode(config: Mapping[str, Any]) -> str:
    """Read ``replay.mode`` from an experiment config, defaulting to strict."""
    replay_config = config.get("replay") or {}
    mode = str(replay_config.get("mode") or DEFAULT_REPLAY_MODE)
    if mode not in REPLAY_MODES:
        raise ValueError(
            f"unknown replay.mode {mode!r}; expected one of {sorted(REPLAY_MODES)}",
        )
    return mode


def enforce_replay_mode_gate(config: Mapping[str, Any]) -> str:
    """
    Validate the adapter/replay-mode pairing and return the resolved mode.

    - ``strict_capture`` (the default) refuses PMXT exploratory data.
    - ``pmxt_research`` refuses non-PMXT adapters so research-mode relaxations
      never silently apply to strict capture sources.
    """
    adapter_name = str((config.get("adapter") or {}).get("name"))
    mode = resolve_replay_mode(config)
    if mode == STRICT_CAPTURE_MODE and adapter_name == PMXT_ADAPTER_NAME:
        raise ValueError(
            "PMXT event data is exploratory and is refused by the default "
            "strict_capture replay mode of the Nautilus PnL/fill/fee/cash/position "
            "runner. Use the PMXT factor research path, or explicitly opt in to a "
            "research-grade backtest with replay.mode: pmxt_research (outputs are "
            "then marked as PMXT research, not matching-level truth).",
        )
    if mode == PMXT_RESEARCH_MODE and adapter_name != PMXT_ADAPTER_NAME:
        raise ValueError(
            f"replay.mode pmxt_research only applies to the {PMXT_ADAPTER_NAME} "
            f"adapter; adapter {adapter_name!r} must run in strict_capture mode so "
            "receive-time guarantees stay enforced.",
        )
    return mode


def blocking_health_issues(report: DataHealthReportV1, *, mode: str) -> list[DataHealthIssueV1]:
    """Return the health errors that block a backtest under the given mode."""
    if mode == PMXT_RESEARCH_MODE:
        return [
            issue
            for issue in report.issues
            if issue.severity == "error"
            and issue.code not in PMXT_RESEARCH_ALLOWED_HEALTH_ERROR_CODES
        ]
    return [issue for issue in report.issues if issue.severity == "error"]


def verify_pmxt_replay_clock_order(dataset: PolymarketL2DatasetV1) -> dict[str, Any]:
    """
    Prove the step stream satisfies the full PMXT contract ordering key.

    Verifies, at the backtest boundary rather than trusting the adapter:

    - the replay clock (``timestamp`` with receive fallback) is non-decreasing;
    - ``sequence`` is strictly increasing;
    - inside tied-clock groups, the declared tie-breakers hold:
      ``timestamp_received`` non-decreasing, and where receive times also tie,
      ``source_row_index`` strictly increasing (when the adapter supplies it).
    """
    previous: Any = None
    previous_clock: datetime | None = None
    tie_break_checked_pairs = 0
    row_index_available = all(step.source_row_index is not None for step in dataset.steps)
    for step in dataset.steps:
        clock = replay_timestamp(step)
        if previous is not None:
            if clock < previous_clock:
                raise ValueError(
                    "PMXT replay clock moved backwards; the dataset does not satisfy "
                    f"the pmxt_research replay contract at sequence={step.sequence}",
                )
            if step.sequence <= previous.sequence:
                raise ValueError(
                    "PMXT replay sequence must be strictly increasing; violated at "
                    f"sequence={step.sequence}",
                )
            if clock == previous_clock:
                tie_break_checked_pairs += 1
                if step.timestamp_received < previous.timestamp_received:
                    raise ValueError(
                        "PMXT tie-breaker violated: timestamp_received moved backwards "
                        f"inside a tied replay-clock group at sequence={step.sequence}",
                    )
                if step.timestamp_received == previous.timestamp_received:
                    # Fully tied pair: only the original row index can prove the
                    # declared triple ordering.  Missing it is fail-closed, not a
                    # skipped check, so tie_breaker_verified can never be a false
                    # positive.
                    if step.source_row_index is None or previous.source_row_index is None:
                        raise ValueError(
                            "PMXT tie-breaker unverifiable: a fully tied timestamp group "
                            f"at sequence={step.sequence} has no source_row_index, so the "
                            "declared ordering key cannot be proven. The adapter must "
                            "supply source_row_index for pmxt_research replay.",
                        )
                    if step.source_row_index <= previous.source_row_index:
                        raise ValueError(
                            "PMXT tie-breaker violated: source_row_index must be strictly "
                            f"increasing inside a fully tied group at sequence={step.sequence}",
                        )
        previous = step
        previous_clock = clock
    return {
        "replay_clock": PMXT_REPLAY_CLOCK,
        "replay_clock_monotonic": True,
        "sequence_strictly_increasing": True,
        "tie_breaker_verified": True,
        "tie_break_checked_pairs": tie_break_checked_pairs,
        "source_row_index_available": row_index_available,
        "step_count": len(dataset.steps),
    }


def enforce_ambiguous_ties_gate(
    *,
    mode: str,
    strategy_enabled: bool,
    ordering_ambiguous: bool,
    allow_ambiguous_ties: bool,
) -> None:
    """
    Fail closed when a strategy would trade through ambiguous PMXT tie order.

    Ambiguous ties are timestamp groups whose internal order is unknowable from
    PMXT data; fills can depend on the arbitrary (deterministic) arrangement.
    Data-only replays may proceed, but strategy runs must explicitly accept the
    risk via ``replay.allow_ambiguous_ties: true``.
    """
    if mode != PMXT_RESEARCH_MODE or not strategy_enabled or not ordering_ambiguous:
        return
    if not allow_ambiguous_ties:
        raise ValueError(
            "PMXT dataset contains ordering-ambiguous tied timestamp groups and a "
            "strategy is enabled; fills could depend on an arbitrary tie arrangement. "
            "Set replay.allow_ambiguous_ties: true to explicitly accept this risk "
            "(the run report will record the acceptance), or run data-only replay.",
        )


def build_replay_provenance(*, mode: str, dataset_metadata: Any) -> dict[str, Any]:
    """Return the replay-mode provenance block for resolved config/summary/report."""
    source_quality = dict(getattr(dataset_metadata, "source_quality", {}) or {})
    if mode == PMXT_RESEARCH_MODE:
        ordering_ambiguous = bool(
            source_quality.get("orderingAmbiguousRows", 0)
            or source_quality.get("orderingStatus") == "ambiguous",
        )
        credibility = "pmxt_research_reconstructed_order"
        if ordering_ambiguous:
            credibility = "pmxt_research_reconstructed_order_ambiguous_ties"
        # Tie-order sensitivity replay is not implemented yet.  Until it runs,
        # results on ambiguous datasets are engine-plumbing evidence only, not
        # performance claims: fills/PnL could depend on the arbitrary tie
        # arrangement inside ambiguous groups.
        claim_scope = "plumbing_only" if ordering_ambiguous else "research_replay"
        return {
            "claim_scope": claim_scope,
            "ambiguous_ties_sensitivity_status": "not_run" if ordering_ambiguous else "not_required",
            "performance_claims_allowed": False,
            "mode": PMXT_RESEARCH_MODE,
            "replay_clock": PMXT_REPLAY_CLOCK,
            "ordering_key": PMXT_RESEARCH_ORDERING_KEY,
            "tie_breaker": "timestamp_received,_original_row_index (stable mergesort)",
            "data_credibility": credibility,
            "ordering_ambiguous": ordering_ambiguous,
            "adapter": str(getattr(dataset_metadata, "adapter_name", "unknown")),
            "source_files": list(getattr(dataset_metadata, "source_files", ()) or ()),
            "source_quality": source_quality,
            "execution_claims_allowed": False,
            "matching_level_truth": False,
            "disclaimer": PMXT_RESEARCH_DISCLAIMER,
        }
    return {
        "claim_scope": "capture_replay",
        "ambiguous_ties_sensitivity_status": "not_applicable",
        "performance_claims_allowed": True,
        "mode": STRICT_CAPTURE_MODE,
        "replay_clock": RECEIVE_TIME_REPLAY_CLOCK,
        "ordering_key": STRICT_CAPTURE_ORDERING_KEY,
        "tie_breaker": "capture file order",
        "data_credibility": "strict_capture_receive_time",
        "ordering_ambiguous": False,
        "adapter": str(getattr(dataset_metadata, "adapter_name", "unknown")),
        "source_files": list(getattr(dataset_metadata, "source_files", ()) or ()),
        "source_quality": source_quality,
        "execution_claims_allowed": True,
        "matching_level_truth": False,
        "disclaimer": STRICT_CAPTURE_STATEMENT,
    }
