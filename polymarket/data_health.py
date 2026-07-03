"""Data-health checks for Polymarket v1 replay datasets.

The backtest path is intentionally ordered by local receive time.  These checks
therefore validate that the adapter-produced replay steps are already safe to
consume in their current order; they do not sort or repair the dataset.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from polymarket.models import L2ReplayStepV1
from polymarket.models import PolymarketL2DatasetV1


SeverityV1 = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class DataHealthIssueV1:
    """One concrete data-health issue or warning."""

    severity: SeverityV1
    code: str
    message: str
    sequence: int | None = None
    previous_sequence: int | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DataHealthSummaryV1:
    """Compact metrics for replay-order and clock pathologies."""

    step_count: int
    first_timestamp_received: str | None
    last_timestamp_received: str | None
    receive_time_inversion_count: int
    sequence_inversion_count: int
    source_time_inversion_count: int
    future_source_time_count: int
    max_future_source_time_ms: float
    source_delay_over_threshold_count: int
    max_source_delay_ms: float


@dataclass(frozen=True, slots=True)
class DataHealthReportV1:
    """Serializable data-health report."""

    ok: bool
    summary: DataHealthSummaryV1
    issues: tuple[DataHealthIssueV1, ...]
    assumptions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": asdict(self.summary),
            "issues": [asdict(issue) for issue in self.issues],
            "assumptions": list(self.assumptions),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


class DataHealthError(ValueError):
    """Raised when data-health checks find hard replay blockers."""


def analyze_dataset_health(
    dataset: PolymarketL2DatasetV1,
    *,
    future_tolerance_ms: float = 50.0,
    delay_warning_ms: float = 1_000.0,
) -> DataHealthReportV1:
    """Analyze ordering and clock pathologies without mutating or sorting data."""

    issues: list[DataHealthIssueV1] = []
    steps = dataset.steps
    receive_time_inversion_count = 0
    sequence_inversion_count = 0
    source_time_inversion_count = 0
    future_source_time_count = 0
    source_delay_over_threshold_count = 0
    max_future_source_time_ms = 0.0
    max_source_delay_ms = 0.0
    previous_source_by_group: dict[tuple[str, str], tuple[int, datetime]] = {}

    previous_step: L2ReplayStepV1 | None = None
    for step in steps:
        if previous_step is not None:
            if step.timestamp_received < previous_step.timestamp_received:
                receive_time_inversion_count += 1
                issues.append(
                    DataHealthIssueV1(
                        severity="error",
                        code="receive_time_inversion",
                        message=(
                            "timestamp_received moved backwards; replay must not sort this away"
                        ),
                        sequence=step.sequence,
                        previous_sequence=previous_step.sequence,
                        details={
                            "previous_timestamp_received": _iso(
                                previous_step.timestamp_received,
                            ),
                            "timestamp_received": _iso(step.timestamp_received),
                        },
                    ),
                )
            if step.sequence <= previous_step.sequence:
                sequence_inversion_count += 1
                issues.append(
                    DataHealthIssueV1(
                        severity="error",
                        code="sequence_inversion",
                        message="local replay sequence must be strictly increasing",
                        sequence=step.sequence,
                        previous_sequence=previous_step.sequence,
                    ),
                )
        previous_step = step

        if step.timestamp is not None:
            future_ms = _delta_ms(step.timestamp, step.timestamp_received)
            delay_ms = _delta_ms(step.timestamp_received, step.timestamp)
            max_future_source_time_ms = max(max_future_source_time_ms, future_ms)
            max_source_delay_ms = max(max_source_delay_ms, delay_ms)
            if future_ms > future_tolerance_ms:
                future_source_time_count += 1
                issues.append(
                    DataHealthIssueV1(
                        severity="warning",
                        code="future_source_time",
                        message=(
                            "source timestamp is later than receive time; reported for "
                            "severity, replay remains receive-time ordered"
                        ),
                        sequence=step.sequence,
                        details={
                            "timestamp": _iso(step.timestamp),
                            "timestamp_received": _iso(step.timestamp_received),
                            "future_ms": round(future_ms, 6),
                            "future_tolerance_ms": future_tolerance_ms,
                        },
                    ),
                )
            if delay_ms > delay_warning_ms:
                source_delay_over_threshold_count += 1
                issues.append(
                    DataHealthIssueV1(
                        severity="warning",
                        code="source_delay_over_threshold",
                        message=(
                            "source timestamp is much earlier than receive time; "
                            "reported for late-delivery severity"
                        ),
                        sequence=step.sequence,
                        details={
                            "timestamp": _iso(step.timestamp),
                            "timestamp_received": _iso(step.timestamp_received),
                            "delay_ms": round(delay_ms, 6),
                            "delay_warning_ms": delay_warning_ms,
                        },
                    ),
                )

        for update in step.updates:
            if step.timestamp is None:
                continue
            group = (update.event_type, update.asset_id)
            previous_source = previous_source_by_group.get(group)
            if previous_source is not None and step.timestamp < previous_source[1]:
                source_time_inversion_count += 1
                backtrack_ms = _delta_ms(previous_source[1], step.timestamp)
                issues.append(
                    DataHealthIssueV1(
                        severity="warning",
                        code="source_time_inversion",
                        message=(
                            "source timestamp moved backwards within event_type+asset_id; "
                            "this is diagnostic only because replay uses receive time"
                        ),
                        sequence=step.sequence,
                        previous_sequence=previous_source[0],
                        details={
                            "event_type": update.event_type,
                            "asset_id": update.asset_id,
                            "previous_timestamp": _iso(previous_source[1]),
                            "timestamp": _iso(step.timestamp),
                            "backtrack_ms": round(backtrack_ms, 6),
                        },
                    ),
                )
            previous_source_by_group[group] = (step.sequence, step.timestamp)

    hard_errors = [issue for issue in issues if issue.severity == "error"]
    summary = DataHealthSummaryV1(
        step_count=len(steps),
        first_timestamp_received=_iso(steps[0].timestamp_received) if steps else None,
        last_timestamp_received=_iso(steps[-1].timestamp_received) if steps else None,
        receive_time_inversion_count=receive_time_inversion_count,
        sequence_inversion_count=sequence_inversion_count,
        source_time_inversion_count=source_time_inversion_count,
        future_source_time_count=future_source_time_count,
        max_future_source_time_ms=round(max_future_source_time_ms, 6),
        source_delay_over_threshold_count=source_delay_over_threshold_count,
        max_source_delay_ms=round(max_source_delay_ms, 6),
    )
    return DataHealthReportV1(
        ok=not hard_errors,
        summary=summary,
        issues=tuple(issues),
        assumptions=(
            "Replay chronology is timestamp_received order, not source timestamp order.",
            "Hard failures are receive-time or local-sequence inversions.",
            "Future/source-time issues are surfaced as diagnostics for severity review.",
            "This check never sorts, drops, or repairs data.",
        ),
    )


def validate_dataset_for_backtest(
    dataset: PolymarketL2DatasetV1,
    *,
    future_tolerance_ms: float = 50.0,
    delay_warning_ms: float = 1_000.0,
) -> DataHealthReportV1:
    """Return the health report or raise if the dataset is unsafe to replay."""

    report = analyze_dataset_health(
        dataset,
        future_tolerance_ms=future_tolerance_ms,
        delay_warning_ms=delay_warning_ms,
    )
    if not report.ok:
        error_codes = sorted({issue.code for issue in report.issues if issue.severity == "error"})
        raise DataHealthError(
            "Polymarket data-health check failed before backtest; "
            f"hard_error_codes={error_codes}. Inspect data_health.json instead of sorting.",
        )
    return report


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _delta_ms(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() * 1_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check live_ws_v1 NDJSON data health.")
    parser.add_argument("--ndjson", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--future-tolerance-ms", type=float, default=50.0)
    parser.add_argument("--delay-warning-ms", type=float, default=1_000.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter

    dataset = LiveWsV1Adapter(repo_root=Path.cwd()).load(
        {"input": {"ndjson_path": args.ndjson}},
    )
    report = analyze_dataset_health(
        dataset,
        future_tolerance_ms=args.future_tolerance_ms,
        delay_warning_ms=args.delay_warning_ms,
    )
    text = report.to_json()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
