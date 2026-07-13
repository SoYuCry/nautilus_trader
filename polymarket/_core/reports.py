"""Human-readable and machine-readable report writers for Polymarket backtests."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from polymarket._core.fees import summarize_fill_fee_totals


_MONEY_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s+([A-Za-z][A-Za-z0-9_]*)")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_FEE_COLUMNS = ("commissions", "commission", "fees", "fee")


@dataclass(frozen=True, slots=True)
class BacktestReportArtifacts:
    """Paths and metadata written for one research backtest run."""

    fees: dict[str, Any]
    instrument: dict[str, Any]
    reports: dict[str, str]


def write_backtest_reports(
    *,
    run_dir: Path,
    result: Any,
    account: pd.DataFrame,
    fills: pd.DataFrame,
    positions: pd.DataFrame,
    instrument: Any,
) -> BacktestReportArtifacts:
    """Write curated reports plus raw Nautilus CSV audit files for one run."""
    instrument_context = build_instrument_context(instrument)
    raw_dir = run_dir / "raw_nautilus"
    raw_dir.mkdir(parents=True, exist_ok=True)

    account_view = build_account_view(account)
    fills_view = build_fills_view(fills, instrument_context=instrument_context)
    positions_view = build_positions_view(positions, instrument_context=instrument_context)

    # Curated CSVs are self-describing once they leave the run directory: every
    # row carries the replay mode and data-credibility grade of the run.
    replay = getattr(result, "replay", {}) or {}
    for view in (account_view, fills_view, positions_view):
        view["replay_mode"] = str(replay.get("mode", "unknown"))
        view["data_credibility"] = str(replay.get("data_credibility", "unknown"))

    account.to_csv(raw_dir / "account.csv")
    fills.to_csv(raw_dir / "fills.csv")
    positions.to_csv(raw_dir / "positions.csv")

    account_view.to_csv(run_dir / "account.csv", index=False)
    fills_view.to_csv(run_dir / "fills.csv", index=False)
    positions_view.to_csv(run_dir / "positions.csv", index=False)

    fee_totals = summarize_fill_fee_totals(fills)
    fee_report = {**result.fees, "totals_from_fills_report": fee_totals}
    data_health_artifact = dict(getattr(result, "data_health_artifact", {}) or {})
    if not data_health_artifact:
        data_health_artifact = {"path": "data_health.json", "status": "present"}
    reports: dict[str, Any] = {
        "account": {"path": "account.csv", "status": "present"},
        "fills": {"path": "fills.csv", "status": "present"},
        "positions": {"path": "positions.csv", "status": "present"},
        "raw_nautilus_account": {"path": "raw_nautilus/account.csv", "status": "present"},
        "raw_nautilus_fills": {"path": "raw_nautilus/fills.csv", "status": "present"},
        "raw_nautilus_positions": {"path": "raw_nautilus/positions.csv", "status": "present"},
        "markdown": {"path": "run_report.md", "status": "present"},
        "data_health": data_health_artifact,
    }
    if data_health_artifact.get("status") == "omitted_from_git":
        reports["omitted_artifacts"] = {"path": "OMITTED_ARTIFACTS.json", "status": "present"}

    _write_run_report_markdown(
        run_dir=run_dir,
        result=result,
        account_view=account_view,
        fills_view=fills_view,
        positions_view=positions_view,
        fee_totals=fee_totals,
        instrument_context=instrument_context,
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "engine": "nautilus_trader.backtest.engine.BacktestEngine",
                "replay": getattr(result, "replay", {}),
                "data_health_gate": getattr(result, "health_gate", {}),
                "engine_config": getattr(result, "engine_config", {}),
                "input_hashes": getattr(result, "input_hashes", []),
                "data_count": result.data_count,
                "order_book_deltas_count": result.order_book_deltas_count,
                "trade_ticks_count": result.trade_ticks_count,
                "instrument_close_count": result.instrument_close_count,
                "skipped_updates": list(result.skipped_updates),
                "tick_size_changes": list(result.tick_size_changes),
                "settlement": result.settlement,
                "data_health": result.data_health,
                "instrument": instrument_context,
                "fees": fee_report,
                "reports": reports,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return BacktestReportArtifacts(fees=fee_report, instrument=instrument_context, reports=reports)


def build_instrument_context(instrument: Any) -> dict[str, Any]:
    """Extract instrument metadata once instead of repeating long IDs per fill row."""
    info = getattr(instrument, "info", None) or {}
    if not isinstance(info, Mapping):
        info = {}
    instrument_id = str(getattr(instrument, "id", "") or "")
    raw_symbol = str(getattr(instrument, "raw_symbol", "") or "")
    token_id = str(info.get("token_id") or raw_symbol or "")
    outcome = str(getattr(instrument, "outcome", "") or "")
    alias = outcome or _abbreviate_identifier(token_id) or _abbreviate_identifier(instrument_id) or "selected"
    return {
        "alias": alias,
        "instrument_id": instrument_id,
        "condition_id": str(info.get("condition_id") or ""),
        "token_id": token_id,
        "outcome": outcome,
        "raw_symbol": raw_symbol,
        "fee_source": str(info.get("fee_source") or ""),
    }


def build_fills_view(fills: pd.DataFrame, *, instrument_context: Mapping[str, Any]) -> pd.DataFrame:
    """Return the strategy-research fill table, not the raw Nautilus audit table."""
    columns = [
        "ts_event",
        "strategy",
        "instrument",
        "side",
        "order_type",
        "quantity",
        "avg_px",
        "fee",
        "fee_pusd",
        "gross_cashflow_pusd",
        "net_cashflow_pusd",
        "liquidity_side",
        "status",
    ]
    if fills.empty:
        return pd.DataFrame(columns=columns)

    # Research view is time-ordered; raw_nautilus/fills.csv keeps the engine's
    # original row order as the audit baseline.
    sort_key = "ts_init" if "ts_init" in fills.columns else None
    if sort_key is not None:
        fills = fills.sort_values(sort_key, kind="mergesort")

    rows: list[dict[str, Any]] = []
    fee_column = _first_existing_column(fills, _FEE_COLUMNS)
    for _, row in fills.iterrows():
        side = str(row.get("side", "") or "")
        qty = _parse_decimal(row.get("filled_qty", row.get("quantity")))
        price = _parse_decimal(row.get("avg_px"))
        fee_by_currency = _parse_money_values(row.get(fee_column)) if fee_column else {}
        fee_pusd = fee_by_currency.get("pUSD", Decimal(0))
        gross_cashflow = _gross_cashflow(side=side, quantity=qty, price=price)
        net_cashflow = gross_cashflow - fee_pusd if gross_cashflow is not None else None
        rows.append(
            {
                "ts_event": _string_or_empty(row.get("ts_init") or row.get("ts_last")),
                "strategy": _short_strategy_id(row.get("strategy_id")),
                "instrument": str(instrument_context.get("alias") or "selected"),
                "side": side,
                "order_type": _string_or_empty(row.get("type")),
                "quantity": _decimal_to_str(qty),
                "avg_px": _decimal_to_str(price),
                "fee": _format_money_values(fee_by_currency),
                "fee_pusd": _decimal_to_str(fee_pusd),
                "gross_cashflow_pusd": _decimal_to_str(gross_cashflow),
                "net_cashflow_pusd": _decimal_to_str(net_cashflow),
                "liquidity_side": _string_or_empty(row.get("liquidity_side")),
                "status": _string_or_empty(row.get("status")),
            },
        )
    return pd.DataFrame(rows, columns=columns)


def build_positions_view(positions: pd.DataFrame, *, instrument_context: Mapping[str, Any]) -> pd.DataFrame:
    """Return the strategy-research position table with long IDs removed."""
    columns = [
        "strategy",
        "instrument",
        "entry",
        "side",
        "quantity",
        "peak_qty",
        "avg_px_open",
        "avg_px_close",
        "commissions",
        "realized_pnl",
        "realized_return",
        "ts_opened",
        "ts_closed",
        "is_snapshot",
    ]
    if positions.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for _, row in positions.iterrows():
        rows.append(
            {
                "strategy": _short_strategy_id(row.get("strategy_id")),
                "instrument": str(instrument_context.get("alias") or "selected"),
                "entry": _string_or_empty(row.get("entry")),
                "side": _string_or_empty(row.get("side")),
                "quantity": _string_or_empty(row.get("quantity")),
                "peak_qty": _string_or_empty(row.get("peak_qty")),
                "avg_px_open": _string_or_empty(row.get("avg_px_open")),
                "avg_px_close": _string_or_empty(row.get("avg_px_close")),
                "commissions": _string_or_empty(row.get("commissions")),
                "realized_pnl": _string_or_empty(row.get("realized_pnl")),
                "realized_return": _string_or_empty(row.get("realized_return")),
                "ts_opened": _string_or_empty(row.get("ts_opened")),
                "ts_closed": _string_or_empty(row.get("ts_closed")),
                "is_snapshot": _string_or_empty(row.get("is_snapshot")),
            },
        )
    return pd.DataFrame(rows, columns=columns)


def build_account_view(account: pd.DataFrame) -> pd.DataFrame:
    """Return a compact account balance timeline."""
    columns = ["ts_event", "total", "locked", "free", "currency", "reported"]
    if account.empty:
        return pd.DataFrame(columns=columns)

    frame = account.reset_index()
    first_col = str(frame.columns[0])
    if first_col in {"index", ""} or "ts_event" not in frame.columns:
        frame = frame.rename(columns={frame.columns[0]: "ts_event"})

    selected = [column for column in columns if column in frame.columns]
    view = frame[selected].copy()
    for column in columns:
        if column not in view.columns:
            view[column] = ""
    return view[columns]


def _write_run_report_markdown(
    *,
    run_dir: Path,
    result: Any,
    account_view: pd.DataFrame,
    fills_view: pd.DataFrame,
    positions_view: pd.DataFrame,
    fee_totals: dict[str, Any],
    instrument_context: Mapping[str, Any],
) -> None:
    health_summary = result.data_health.get("summary", {})
    replay = getattr(result, "replay", {}) or {}
    health_gate = getattr(result, "health_gate", {}) or {}
    engine_config = getattr(result, "engine_config", {}) or {}
    ts_init_audit = replay.get("ts_init_audit", {}) or {}
    data_health_artifact = dict(getattr(result, "data_health_artifact", {}) or {})
    lines = [
        "# Polymarket backtest run report",
        "",
        "## Replay trust boundary",
        "",
        f"- Replay mode: `{replay.get('mode', 'unknown')}`",
        f"- Claim scope: `{replay.get('claim_scope', 'unknown')}`",
        f"- Performance claims allowed: `{str(replay.get('performance_claims_allowed', False)).lower()}`",
        f"- Replay clock: `{replay.get('replay_clock', 'unknown')}`",
        f"- Ordering key: `{replay.get('ordering_key', 'unknown')}`",
        f"- Tie-breaker: `{replay.get('tie_breaker', 'unknown')}`",
        f"- Data credibility: `{replay.get('data_credibility', 'unknown')}`",
        f"- Adapter: `{replay.get('adapter', 'unknown')}`",
        f"- Ordering ambiguous ties: `{str(replay.get('ordering_ambiguous', False)).lower()}`"
        + (
            f" (explicitly accepted: `{str(replay.get('ambiguous_ties_accepted', False)).lower()}`, "
            f"sensitivity: `{replay.get('ambiguous_ties_sensitivity_status', 'unknown')}`)"
            if "ambiguous_ties_accepted" in replay
            else ""
        ),
        f"- Execution claims allowed: `{str(replay.get('execution_claims_allowed', False)).lower()}`",
        f"- Matching-level truth: `{str(replay.get('matching_level_truth', False)).lower()}`",
        f"- Boundary: {replay.get('disclaimer', 'unknown')}",
        "",
        "## Data-health gate (mode-aware)",
        "",
        f"- Raw health ok (mode-agnostic receive-time check): `{str(health_gate.get('raw_health_ok', 'unknown')).lower()}`",
        f"- Mode health gate passed: `{str(health_gate.get('mode_health_gate_passed', 'unknown')).lower()}`",
        f"- Blocking codes: `{health_gate.get('blocking_codes', [])}`",
        f"- Replay clock verified: `{str(health_gate.get('replay_clock_verified', 'unknown')).lower()}`",
        "- In pmxt_research mode, raw health can be `false` (receive-time inversions are diagnostics) while the mode gate passed; the mode gate is the authoritative go/no-go.",
        "",
        "## Synthetic ts_init audit",
        "",
        f"- Policy: `{ts_init_audit.get('ts_init_policy', 'unknown')}`",
        f"- Events serialized by +1ns adjustment: `{ts_init_audit.get('adjusted_event_count', 'unknown')}`",
        f"- Max synthetic offset from replay clock (ns): `{ts_init_audit.get('max_synthetic_offset_ns', 'unknown')}`",
        "- Factor research and this backtest share the adapter step order and source replay clock, but factor labels aggregate tied timestamps while the strategy observes those events one-by-one on synthetic ts_init; the two views are order-consistent, not identical.",
        "",
        "## Engine fill configuration",
        "",
        f"- trade_execution: `{str(engine_config.get('trade_execution', 'unknown')).lower()}`",
        f"- liquidity_consumption: `{str(engine_config.get('liquidity_consumption', 'unknown')).lower()}`",
        f"- queue_position: `{str(engine_config.get('queue_position', 'unknown')).lower()}`",
        f"- book_type: `{engine_config.get('book_type', 'unknown')}` / oms: `{engine_config.get('oms_type', 'unknown')}` / account: `{engine_config.get('account_type', 'unknown')}`",
        f"- starting_balance: `{engine_config.get('starting_balance', 'unknown')}`",
        f"- fee_model_enabled: `{str(engine_config.get('fee_model_enabled', 'unknown')).lower()}` (maker rebates: `{str(engine_config.get('maker_rebates_enabled', 'unknown')).lower()}`)",
        "",
        "## Result summary",
        "",
        _markdown_kv_table(_result_summary_rows(account_view, fills_view, positions_view, fee_totals)),
        "",
        "## Replay summary",
        "",
        "- Engine: `nautilus_trader.backtest.engine.BacktestEngine`",
        f"- Nautilus data count: `{result.data_count}`",
        f"- OrderBookDeltas count: `{result.order_book_deltas_count}`",
        f"- TradeTick count: `{result.trade_ticks_count}`",
        f"- InstrumentClose count: `{result.instrument_close_count}`",
        f"- Settlement mode: `{result.settlement.get('mode', 'open')}`",
        f"- Settlement enabled: `{str(result.settlement.get('enabled', False)).lower()}`",
        f"- Settlement reason: {result.settlement.get('reason', 'unknown')}",
        "- Settlement modes: `official` uses resolution metadata; `inferred` is a clearly marked terminal-price guess; `open` leaves final positions unclosed.",
        f"- Data health ok: `{str(result.data_health.get('ok')).lower()}`",
        f"- Receive-time inversions: `{health_summary.get('receive_time_inversion_count')}`",
        f"- Sequence inversions: `{health_summary.get('sequence_inversion_count')}`",
        f"- Source-time inversions: `{health_summary.get('source_time_inversion_count')}`",
        f"- Future source-time count: `{health_summary.get('future_source_time_count')}`",
        "",
        "## Instrument",
        "",
        f"- Alias: `{instrument_context.get('alias', 'selected')}`",
        f"- Outcome: `{instrument_context.get('outcome') or 'unknown'}`",
        f"- condition_id: `{instrument_context.get('condition_id') or 'unknown'}`",
        f"- token_id: `{instrument_context.get('token_id') or 'unknown'}`",
        f"- instrument_id: `{instrument_context.get('instrument_id') or 'unknown'}`",
        "",
        "## Fees",
        "",
        f"- Fee model enabled: `{str(result.fees.get('enabled', False)).lower()}`",
        f"- Fee model: `{result.fees.get('model', 'unknown')}`",
        f"- Maker rebates enabled: `{str(result.fees.get('maker_rebates_enabled', False)).lower()}`",
        f"- Require explicit fee metadata: `{str(result.fees.get('require_explicit', False)).lower()}`",
        f"- Instrument maker fee: `{result.fees.get('instrument_maker_fee', 'unknown')}`",
        f"- Instrument taker fee: `{result.fees.get('instrument_taker_fee', 'unknown')}`",
        f"- Fee source: `{result.fees.get('instrument_fee_source', 'unknown')}`",
        f"- Total fees from `raw_nautilus/fills.csv`: `{fee_totals.get('total_display', 'unavailable')}`",
        f"- Fee total source column: `{fee_totals.get('source_column')}`",
        f"- Fee warning: {result.fees.get('warning') or 'none'}",
        f"- Fee total warning: {fee_totals.get('warning') or 'none'}",
        "",
        "## Fills summary",
        "",
        _markdown_frame(fills_view, max_rows=20),
        "",
        "## Positions summary",
        "",
        _markdown_frame(positions_view, max_rows=20),
        "",
        "## Account summary",
        "",
        _markdown_frame(account_view, max_rows=20),
        "",
        "## Report files",
        "",
        "- `summary.json`",
        (
            "- `data_health.json` — omitted from Git (large regenerable diagnostics); "
            "integrity metadata in `OMITTED_ARTIFACTS.json`"
            if data_health_artifact.get("status") == "omitted_from_git"
            else "- `data_health.json`"
        ),
        "- `fills.csv`",
        "- `positions.csv`",
        "- `account.csv`",
        "- `raw_nautilus/fills.csv`",
        "- `raw_nautilus/positions.csv`",
        "- `raw_nautilus/account.csv`",
        "",
        "## Notes",
        "",
        "- `TradeTick count` is selected-token `last_trade_price` converted into Nautilus `TradeTick`; it is not strategy fill count.",
        "- Settlement uses Nautilus `InstrumentClose` + venue `settlement_prices`; it is not converted into a market `TradeTick`.",
        "- Strategy fills are recorded in `fills.csv`; raw Nautilus order fields are retained under `raw_nautilus/` for audit.",
        "- Fees use Nautilus' Polymarket fee model when enabled and read the instrument `maker_fee` / `taker_fee` fields.",
    ]
    if result.settlement.get("mode") == "open":
        lines.extend(
            [
                "- Open settlement mode means no `InstrumentClose` was generated; inspect the final positions below.",
                "",
                "## Final open positions",
                "",
                _markdown_frame(positions_view, max_rows=20),
            ],
        )
    if result.settlement.get("mode") == "inferred":
        lines.append("- Inferred settlement is a research convenience, not official Polymarket resolution evidence.")
    (run_dir / "run_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _result_summary_rows(
    account_view: pd.DataFrame,
    fills_view: pd.DataFrame,
    positions_view: pd.DataFrame,
    fee_totals: Mapping[str, Any],
) -> list[tuple[str, str]]:
    starting_balance = _account_balance(account_view, first=True)
    ending_balance = _account_balance(account_view, first=False)
    net_change = _account_net_change(account_view)
    final_position = _final_position(positions_view)
    return [
        ("fills", str(len(fills_view))),
        ("starting_balance", starting_balance),
        ("ending_balance", ending_balance),
        ("net_cash_change", net_change),
        ("total_fees", str(fee_totals.get("total_display", "unavailable"))),
        ("final_position", final_position),
    ]


def _markdown_kv_table(rows: list[tuple[str, str]]) -> str:
    table = ["| metric | value |", "| --- | --- |"]
    table.extend(f"| {_escape_md(key)} | {_escape_md(value)} |" for key, value in rows)
    return "\n".join(table)


def _markdown_frame(frame: pd.DataFrame, *, max_rows: int) -> str:
    if frame.empty:
        return "_empty_"
    display = frame.head(max_rows)
    lines = [_dataframe_to_markdown(display)]
    if len(frame) > max_rows:
        lines.append(f"\n_showing first {max_rows} of {len(frame)} rows; full data in CSV._")
    return "\n".join(lines)


def _dataframe_to_markdown(frame: pd.DataFrame) -> str:
    columns = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(_escape_md(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(_escape_md(_string_or_empty(row[column])) for column in columns) + " |")
    return "\n".join(lines)


def _account_balance(account_view: pd.DataFrame, *, first: bool) -> str:
    if account_view.empty:
        return "unavailable"
    row = account_view.iloc[0 if first else -1]
    total = _string_or_empty(row.get("total"))
    currency = _string_or_empty(row.get("currency"))
    return f"{total} {currency}".strip() or "unavailable"


def _account_net_change(account_view: pd.DataFrame) -> str:
    if account_view.empty:
        return "unavailable"
    start = _parse_decimal(account_view.iloc[0].get("total"))
    end = _parse_decimal(account_view.iloc[-1].get("total"))
    currency = _string_or_empty(account_view.iloc[-1].get("currency"))
    if start is None or end is None:
        return "unavailable"
    return f"{_decimal_to_str(end - start)} {currency}".strip()


def _final_position(positions_view: pd.DataFrame) -> str:
    if positions_view.empty:
        return "flat / none"
    row = positions_view.iloc[-1]
    side = _string_or_empty(row.get("side")) or "unknown"
    quantity = _string_or_empty(row.get("quantity")) or "unknown"
    if side.upper() == "FLAT" or quantity in {"0", "0.0", "0.000000"}:
        return "flat"
    return f"{side} {quantity}"


def _gross_cashflow(*, side: str, quantity: Decimal | None, price: Decimal | None) -> Decimal | None:
    if quantity is None or price is None:
        return None
    notional = quantity * price
    side_upper = side.upper()
    if side_upper == "BUY":
        return -notional
    if side_upper == "SELL":
        return notional
    return None


def _parse_money_values(value: Any) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    if value is None:
        return totals
    for amount, currency in _MONEY_RE.findall(str(value)):
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(amount)
    return totals


def _format_money_values(values: Mapping[str, Decimal]) -> str:
    if not values:
        return "0"
    return ", ".join(f"{_decimal_to_str(amount)} {currency}" for currency, amount in sorted(values.items()))


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value)
    if text.lower() in {"", "nan", "none", "null"}:
        return None
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    return Decimal(match.group(0))


def _decimal_to_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f") if value != 0 else "0"


def _first_existing_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    columns = {str(column) for column in frame.columns}
    return next((candidate for candidate in candidates if candidate in columns), None)


def _short_strategy_id(value: Any) -> str:
    text = _string_or_empty(value)
    if "-" not in text:
        return text
    base, suffix = text.rsplit("-", maxsplit=1)
    return base if suffix.isdigit() else text


def _abbreviate_identifier(value: str, *, prefix: int = 10, suffix: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix + 1:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _escape_md(value: Any) -> str:
    return _string_or_empty(value).replace("|", "\\|").replace("\n", " ")
