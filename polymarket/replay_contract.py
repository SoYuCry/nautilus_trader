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
    Prove the PMXT replay clock is non-decreasing and sequence strictly increases.

    The adapter's stable sort guarantees this by construction; this check makes
    the guarantee explicit at the backtest boundary instead of trusting the
    adapter silently.
    """
    previous_clock: datetime | None = None
    previous_sequence: int | None = None
    for step in dataset.steps:
        clock = replay_timestamp(step)
        if previous_clock is not None and clock < previous_clock:
            raise ValueError(
                "PMXT replay clock moved backwards; the dataset does not satisfy "
                f"the pmxt_research replay contract at sequence={step.sequence}",
            )
        if previous_sequence is not None and step.sequence <= previous_sequence:
            raise ValueError(
                "PMXT replay sequence must be strictly increasing; violated at "
                f"sequence={step.sequence}",
            )
        previous_clock = clock
        previous_sequence = step.sequence
    return {
        "replay_clock": PMXT_REPLAY_CLOCK,
        "replay_clock_monotonic": True,
        "sequence_strictly_increasing": True,
        "step_count": len(dataset.steps),
    }


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
        return {
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
