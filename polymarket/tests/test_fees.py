"""Tests for Polymarket fee reporting helpers."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from polymarket._core.fees import build_fee_report
from polymarket._core.fees import enforce_fee_report
from polymarket._core.fees import summarize_fill_fee_totals


def test_fee_report_marks_default_zero_as_warning_not_silent_success() -> None:
    report = build_fee_report(
        {},
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        fee_source="default_zero",
    )

    assert report["enabled"] is True
    assert report["uses_default_zero"] is True
    assert report["instrument_fee_source"] == "default_zero"
    assert "fee_source=default_zero" in report["warning"]


def test_require_explicit_fee_rejects_default_zero_fallback() -> None:
    report = build_fee_report(
        {"require_explicit": True},
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        fee_source="default_zero",
    )

    with pytest.raises(ValueError, match=r"fees.require_explicit=true.*fee_source=default_zero"):
        enforce_fee_report(report)


def test_fee_totals_parse_commission_money_strings_from_fills_report() -> None:
    fills = pd.DataFrame(
        {
            "commission": ["0.012000 pUSD", "0.003 pUSD", "1 USDC"],
        },
    )

    totals = summarize_fill_fee_totals(fills)

    assert totals["available"] is True
    assert totals["source_column"] == "commission"
    assert totals["by_currency"] == {"USDC": "1", "pUSD": "0.015"}
    assert totals["total_display"] == "1 USDC, 0.015 pUSD"
    assert totals["warning"] is None


def test_fee_totals_warn_when_fills_report_has_no_fee_column() -> None:
    fills = pd.DataFrame({"price": ["0.60"], "quantity": ["1"]})

    totals = summarize_fill_fee_totals(fills)

    assert totals["available"] is False
    assert totals["total_display"] == "unavailable"
    assert "commission/fee column" in totals["warning"]
