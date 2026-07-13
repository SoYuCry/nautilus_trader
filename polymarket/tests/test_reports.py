"""Tests for curated Polymarket report outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from polymarket._core.reports import build_fills_view
from polymarket._core.reports import write_backtest_reports


@dataclass(frozen=True, slots=True)
class FakeResult:
    data_count: int = 2
    order_book_deltas_count: int = 1
    trade_ticks_count: int = 1
    instrument_close_count: int = 0
    skipped_updates: tuple[str, ...] = ()
    tick_size_changes: tuple[tuple[str, str], ...] = ()
    settlement: dict = None  # type: ignore[assignment]
    data_health: dict = None  # type: ignore[assignment]
    fees: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "settlement", self.settlement or {"mode": "open", "enabled": False})
        object.__setattr__(
            self,
            "data_health",
            self.data_health
            or {
                "ok": True,
                "summary": {
                    "receive_time_inversion_count": 0,
                    "sequence_inversion_count": 0,
                    "source_time_inversion_count": 0,
                    "future_source_time_count": 0,
                },
            },
        )
        object.__setattr__(
            self,
            "fees",
            self.fees
            or {
                "enabled": True,
                "model": "PolymarketFeeModel",
                "maker_rebates_enabled": False,
                "require_explicit": False,
                "instrument_maker_fee": "0",
                "instrument_taker_fee": "0.05",
                "instrument_fee_source": "instrument_config",
                "warning": None,
            },
        )


def test_build_fills_view_keeps_research_columns_and_cashflows() -> None:
    fills = pd.DataFrame(
        [
            {
                "strategy_id": "TakeBestAskExitAfter-000",
                "instrument_id": "very-long-id.POLYMARKET",
                "type": "MARKET",
                "side": "BUY",
                "filled_qty": "1.000000",
                "avg_px": "0.49",
                "commissions": "['0.012490 pUSD']",
                "liquidity_side": "TAKER",
                "status": "FILLED",
                "ts_init": "2026-06-26 02:25:28.635472+00:00",
            },
            {
                "strategy_id": "TakeBestAskExitAfter-000",
                "instrument_id": "very-long-id.POLYMARKET",
                "type": "MARKET",
                "side": "SELL",
                "filled_qty": "1.000000",
                "avg_px": "0.47",
                "commissions": "['0.012460 pUSD']",
                "liquidity_side": "TAKER",
                "status": "FILLED",
                "ts_init": "2026-06-26 02:26:29.010797+00:00",
            },
        ],
    )

    view = build_fills_view(fills, instrument_context={"alias": "Yes"})

    assert list(view.columns) == [
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
    assert "instrument_id" not in view.columns
    assert view.loc[0, "strategy"] == "TakeBestAskExitAfter"
    assert view.loc[0, "gross_cashflow_pusd"] == "-0.49"
    assert view.loc[0, "net_cashflow_pusd"] == "-0.50249"
    assert view.loc[1, "gross_cashflow_pusd"] == "0.47"
    assert view.loc[1, "net_cashflow_pusd"] == "0.45754"


def test_write_backtest_reports_writes_curated_csv_and_raw_nautilus_audit(tmp_path: Path) -> None:
    account = pd.DataFrame(
        [
            {"total": "10000.000000", "locked": "0.000000", "free": "10000.000000", "currency": "pUSD", "reported": True},
            {"total": "9999.955050", "locked": "0.000000", "free": "9999.955050", "currency": "pUSD", "reported": False},
        ],
        index=pd.to_datetime(["2026-06-26T02:25:28Z", "2026-06-26T02:26:29Z"], utc=True),
    )
    fills = pd.DataFrame(
        [
            {
                "strategy_id": "TakeBestAskExitAfter-000",
                "instrument_id": "long-condition-long-token.POLYMARKET",
                "type": "MARKET",
                "side": "BUY",
                "filled_qty": "1.000000",
                "avg_px": "0.49",
                "commissions": "['0.012490 pUSD']",
                "liquidity_side": "TAKER",
                "status": "FILLED",
                "ts_init": "2026-06-26 02:25:28.635472+00:00",
            },
        ],
    )
    positions = pd.DataFrame(
        [
            {
                "strategy_id": "TakeBestAskExitAfter-000",
                "instrument_id": "long-condition-long-token.POLYMARKET",
                "entry": "BUY",
                "side": "FLAT",
                "quantity": "0.000000",
                "peak_qty": "1.000000",
                "avg_px_open": "0.49",
                "avg_px_close": "0.47",
                "commissions": "['0.024950 pUSD']",
                "realized_pnl": "-0.044950 pUSD",
                "realized_return": "-0.04082",
                "ts_opened": "2026-06-26 02:25:28.635472+00:00",
                "ts_closed": "2026-06-26 02:26:29.010797+00:00",
                "is_snapshot": False,
            },
        ],
    )
    instrument = SimpleNamespace(
        id="condition-yes.POLYMARKET",
        raw_symbol="yes",
        outcome="Yes",
        info={"condition_id": "condition", "token_id": "yes", "fee_source": "instrument_config"},
    )

    artifacts = write_backtest_reports(
        run_dir=tmp_path,
        result=FakeResult(),
        account=account,
        fills=fills,
        positions=positions,
        instrument=instrument,
    )

    assert (tmp_path / "fills.csv").exists()
    assert (tmp_path / "positions.csv").exists()
    assert (tmp_path / "account.csv").exists()
    assert (tmp_path / "raw_nautilus" / "fills.csv").exists()
    assert not (tmp_path / "fills_report.txt").exists()
    assert not (tmp_path / "fills_report.csv").exists()
    assert "raw_nautilus_fills" in artifacts.reports

    fills_text = (tmp_path / "fills.csv").read_text(encoding="utf-8")
    assert "instrument_id" not in fills_text
    assert "net_cashflow_pusd" in fills_text
    assert "-0.50249" in fills_text

    report = (tmp_path / "run_report.md").read_text(encoding="utf-8")
    assert "## Result summary" in report
    assert "## Fills summary" in report
    assert "| ts_event | strategy | instrument | side |" in report
    assert "TakeBestAskExitAfter" in report
    assert "raw_nautilus/fills.csv" in report

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["reports"]["fills"] == {"path": "fills.csv", "status": "present"}
    assert summary["reports"]["raw_nautilus_fills"] == {"path": "raw_nautilus/fills.csv", "status": "present"}
    # Without an omission manifest the health artifact reads as present.
    assert summary["reports"]["data_health"]["status"] == "present"
    assert "omitted_artifacts" not in summary["reports"]
    assert summary["instrument"]["alias"] == "Yes"
