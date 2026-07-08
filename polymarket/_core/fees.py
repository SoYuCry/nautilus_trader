"""Pure fee reporting helpers for Polymarket research backtests."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


_MONEY_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s+([A-Za-z][A-Za-z0-9_]*)")
_FEE_COLUMNS = ("commission", "commissions", "fee", "fees")


def build_fee_report(
    fees_config: Mapping[str, Any],
    *,
    maker_fee: Any,
    taker_fee: Any,
    fee_source: str,
) -> dict[str, Any]:
    """Build a serializable fee configuration report for one run."""
    enabled = bool(fees_config.get("enabled", True))
    source = str(fee_source or "unknown")
    is_default_zero = source == "default_zero"
    warning = None
    if enabled and is_default_zero:
        warning = (
            "fee_source=default_zero: maker/taker fee was not provided by config or market metadata; "
            "Nautilus will run this backtest as zero-fee unless fees.require_explicit is enabled."
        )
    return {
        "enabled": enabled,
        "model": "PolymarketFeeModel" if enabled else "disabled",
        "maker_rebates_enabled": bool(fees_config.get("maker_rebates_enabled", False)),
        "require_explicit": bool(fees_config.get("require_explicit", False)),
        "instrument_maker_fee": str(maker_fee),
        "instrument_taker_fee": str(taker_fee),
        "instrument_fee_source": source,
        "uses_default_zero": is_default_zero,
        "warning": warning,
    }


def enforce_fee_report(fee_report: Mapping[str, Any]) -> None:
    """Fail fast when the run requires explicit fee metadata but only has zero fallback."""
    if (
        bool(fee_report.get("enabled", True))
        and bool(fee_report.get("require_explicit", False))
        and bool(fee_report.get("uses_default_zero", False))
    ):
        raise ValueError(
            "fees.require_explicit=true but fee_source=default_zero. "
            "Provide instrument.maker_fee/taker_fee or market metadata feeSchedule/taker_fee before backtesting.",
        )


def summarize_fill_fee_totals(fills_report: Any) -> dict[str, Any]:
    """Summarize realized fee/commission totals from a Nautilus fills report frame."""
    fill_rows = len(fills_report) if fills_report is not None else 0
    if fills_report is None or getattr(fills_report, "empty", True):
        return {
            "available": True,
            "source_column": None,
            "fill_rows": fill_rows,
            "by_currency": {},
            "total_display": "0 (no filled orders)",
            "warning": None,
        }

    columns = [str(column) for column in getattr(fills_report, "columns", [])]
    source_column = next((column for column in _FEE_COLUMNS if column in columns), None)
    if source_column is None:
        return {
            "available": False,
            "source_column": None,
            "fill_rows": fill_rows,
            "by_currency": {},
            "total_display": "unavailable",
            "warning": (
                "Could not find a commission/fee column in fills_report; "
                "inspect fills_report.csv for the raw Nautilus columns."
            ),
            "columns": columns,
        }

    totals: defaultdict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for value in fills_report[source_column].tolist():
        for amount, currency in _parse_money_values(value):
            totals[currency] += amount

    if not totals:
        return {
            "available": False,
            "source_column": source_column,
            "fill_rows": fill_rows,
            "by_currency": {},
            "total_display": "unavailable",
            "warning": f"Column {source_column!r} was present but no Money-like values were parsed.",
            "columns": columns,
        }

    by_currency = {currency: _decimal_to_str(amount) for currency, amount in sorted(totals.items())}
    return {
        "available": True,
        "source_column": source_column,
        "fill_rows": fill_rows,
        "by_currency": by_currency,
        "total_display": format_fee_totals(by_currency),
        "warning": None,
    }


def format_fee_totals(by_currency: Mapping[str, str]) -> str:
    if not by_currency:
        return "0"
    return ", ".join(f"{amount} {currency}" for currency, amount in sorted(by_currency.items()))


def _parse_money_values(value: Any) -> tuple[tuple[Decimal, str], ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        parsed: list[tuple[Decimal, str]] = []
        for item in value:
            parsed.extend(_parse_money_values(item))
        return tuple(parsed)
    text = str(value)
    if text.lower() in {"", "nan", "none", "null"}:
        return ()
    return tuple((Decimal(amount), currency) for amount, currency in _MONEY_RE.findall(text))


def _decimal_to_str(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"
