from __future__ import annotations

import json
import textwrap
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from polymarket.replay_contract import PMXT_RESEARCH_ORDERING_KEY


CONDITION_ID = "0xorderingparity"
YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"  # noqa: S105
NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"  # noqa: S105


def _iso(second: int) -> str:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _base_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "event_type": "book",
        "market": CONDITION_ID.encode(),
        "asset_id": YES_TOKEN,
        "timestamp_received": _iso(1),
        "timestamp": _iso(1),
        "bids": None,
        "asks": None,
        "side": None,
        "price": None,
        "size": None,
        "best_bid": None,
        "best_ask": None,
        "old_tick_size": None,
        "new_tick_size": None,
    }
    row.update(overrides)
    return row


def _callback_rows() -> list[dict[str, Any]]:
    return [
        # Physical row 0 sorts last by source timestamp, proving callback oracle
        # is not physical parquet order.
        _base_row(
            event_type="price_change",
            timestamp_received=_iso(1),
            timestamp=_iso(5),
            side="SELL",
            price="0.620",
            size="5",
        ),
        # Snapshot book: expected callback must expose CLEAR + ADD + ADD with
        # snapshot flags and F_LAST only on the final delta.
        _base_row(
            event_type="book",
            timestamp_received=_iso(4),
            timestamp=_iso(1),
            bids=json.dumps([["0.400", "10"]]),
            asks=json.dumps([["0.600", "9"]]),
            best_bid="0.400",
            best_ask="0.600",
        ),
        # Full tie group begins. Tick-size change must produce no callback but
        # must make the following 0.501 update legal.
        _base_row(
            event_type="tick_size_change",
            timestamp_received=_iso(2),
            timestamp=_iso(2),
            old_tick_size="0.01",
            new_tick_size="0.001",
        ),
        _base_row(
            event_type="last_trade_price",
            timestamp_received=_iso(2),
            timestamp=_iso(2),
            side="BUY",
            price="0.420",
            size="3",
        ),
        _base_row(
            event_type="price_change",
            timestamp_received=_iso(2),
            timestamp=_iso(2),
            side="BUY",
            price="0.501",
            size="7",
        ),
        _base_row(
            event_type="last_trade_price",
            timestamp_received=_iso(3),
            timestamp=_iso(3),
            side="SELL",
            price="0.502",
            size="4",
        ),
        _base_row(
            event_type="price_change",
            timestamp_received=_iso(4),
            timestamp=_iso(4),
            side="BUY",
            price="0.501",
            size="0",
        ),
    ]


def _write_event_dir(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    event_dir = tmp_path / "pmxt-event"
    event_dir.mkdir()
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False)
    (event_dir / "gamma_event.raw.json").write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "conditionId": CONDITION_ID,
                        "outcomes": json.dumps(["Yes", "No"]),
                        "outcomePrices": json.dumps(["1", "0"]),
                        "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
                        "feeSchedule": json.dumps({"rate": "0"}),
                        "umaResolutionStatus": "resolved",
                        "closedTime": "2026-01-02T00:00:00Z",
                        "orderPriceMinTickSize": "0.001",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (event_dir / "event_index.json").write_text(
        json.dumps({"markets": [{"conditionId": CONDITION_ID, "yesToken": YES_TOKEN, "noToken": NO_TOKEN}]}),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-ordering-parity-test"}), encoding="utf-8")
    return event_dir


def _write_callback_strategy(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            r"""
            from __future__ import annotations

            import json
            from pathlib import Path

            from nautilus_trader.config import StrategyConfig
            from nautilus_trader.model.data import OrderBookDeltas
            from nautilus_trader.model.data import TradeTick
            from nautilus_trader.model.enums import BookType
            from nautilus_trader.model.identifiers import InstrumentId
            from nautilus_trader.trading.strategy import Strategy


            class CallbackRecorderConfig(StrategyConfig, frozen=True):
                instrument_id: InstrumentId
                output_path: str


            class CallbackRecorder(Strategy):
                def __init__(self, instrument_id: str, output_path: str) -> None:
                    super().__init__(
                        CallbackRecorderConfig(
                            instrument_id=InstrumentId.from_str(instrument_id),
                            output_path=output_path,
                        ),
                    )
                    self.output = Path(output_path)

                def on_start(self) -> None:
                    self.output.parent.mkdir(parents=True, exist_ok=True)
                    self.output.write_text("", encoding="utf-8")
                    self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)
                    self.subscribe_trade_ticks(self.config.instrument_id)

                def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
                    self._append(
                        {
                            "callback_type": "order_book_deltas",
                            "ts_event": deltas.ts_event,
                            "ts_init": deltas.ts_init,
                            "deltas": [self._delta(delta) for delta in deltas.deltas],
                        },
                    )

                def on_trade_tick(self, tick: TradeTick) -> None:
                    self._append(
                        {
                            "callback_type": "trade_tick",
                            "ts_event": tick.ts_event,
                            "ts_init": tick.ts_init,
                            "price": str(tick.price),
                            "size": str(tick.size),
                            "aggressor_side": getattr(tick.aggressor_side, "name", str(tick.aggressor_side)),
                        },
                    )

                def _append(self, row: dict) -> None:
                    with self.output.open("a", encoding="utf-8") as file:
                        file.write(json.dumps(row, sort_keys=True) + "\n")

                @staticmethod
                def _delta(delta) -> dict:
                    order = delta.order
                    action = getattr(delta.action, "name", str(delta.action))
                    if action == "CLEAR":
                        return {
                            "sequence": delta.sequence,
                            "action": action,
                            "flags": int(delta.flags),
                            "side": None,
                            "price": None,
                            "size": None,
                        }
                    return {
                        "sequence": delta.sequence,
                        "action": action,
                        "flags": int(delta.flags),
                        "side": None if order is None else getattr(order.side, "name", str(order.side)),
                        "price": None if order is None else str(order.price),
                        "size": None if order is None else str(order.size),
                    }
        """,
        ).lstrip(),
        encoding="utf-8",
    )


def _write_config(config_path: Path, event_dir: Path, strategy_path: Path, callback_path: Path) -> None:
    instrument_id = f"{CONDITION_ID}-{YES_TOKEN}.POLYMARKET"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: pmxt_ordering_parity_callback_test
            adapter:
              name: pmxt_event_v1
              input:
                event_dir: {event_dir.as_posix()}
                condition_id: "{CONDITION_ID}"
                asset_id: "{YES_TOKEN}"
            selection:
              asset_id: "{YES_TOKEN}"
            replay:
              mode: pmxt_research
              allow_ambiguous_ties: true
            engine:
              trade_execution: false
              liquidity_consumption: false
              queue_position: false
            strategy:
              path: {strategy_path.as_posix()}
              class: CallbackRecorder
              params:
                instrument_id: "{instrument_id}"
                output_path: {callback_path.as_posix()}
            runtime:
              run_id: pmxt-ordering-parity-callback
            report:
              output_dir: ./runs
            fees:
              enabled: false
            """,
        ).lstrip(),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _event_ns(second: int) -> int:
    return int(datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC).timestamp() * 1_000_000_000)


def test_pmxt_research_run_from_config_emits_engine_callbacks_in_oracle_order(tmp_path: Path) -> None:
    pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")
    from nautilus_trader.model.enums import RecordFlag
    from polymarket.backtest_v1 import run_from_config

    rows = _callback_rows()
    event_dir = _write_event_dir(tmp_path, rows)
    strategy_path = tmp_path / "callback_strategy.py"
    callback_path = tmp_path / "callbacks.jsonl"
    config_path = tmp_path / "experiment.yml"
    _write_callback_strategy(strategy_path)
    _write_config(config_path, event_dir, strategy_path, callback_path)

    summary = run_from_config(config_path)

    callbacks = _read_jsonl(callback_path)
    snapshot_flag = int(RecordFlag.F_SNAPSHOT)
    last_flag = int(RecordFlag.F_LAST)
    expected = [
        {
            "callback_type": "order_book_deltas",
            "ts_event": _event_ns(1),
            "ts_init": _event_ns(1),
            "deltas": [
                {"sequence": 1, "action": "CLEAR", "flags": snapshot_flag, "side": None, "price": None, "size": None},
                {"sequence": 1, "action": "ADD", "flags": snapshot_flag, "side": "BUY", "price": "0.400", "size": "10.000000"},
                {"sequence": 1, "action": "ADD", "flags": snapshot_flag | last_flag, "side": "SELL", "price": "0.600", "size": "9.000000"},
            ],
        },
        {"callback_type": "trade_tick", "ts_event": _event_ns(2), "ts_init": _event_ns(2), "price": "0.420", "size": "3.000000", "aggressor_side": "BUYER"},
        {
            "callback_type": "order_book_deltas",
            "ts_event": _event_ns(2),
            "ts_init": _event_ns(2) + 1,
            "deltas": [
                {"sequence": 4, "action": "UPDATE", "flags": last_flag, "side": "BUY", "price": "0.501", "size": "7.000000"},
            ],
        },
        {"callback_type": "trade_tick", "ts_event": _event_ns(3), "ts_init": _event_ns(3), "price": "0.502", "size": "4.000000", "aggressor_side": "SELLER"},
        {
            "callback_type": "order_book_deltas",
            "ts_event": _event_ns(4),
            "ts_init": _event_ns(4),
            "deltas": [
                {"sequence": 6, "action": "DELETE", "flags": last_flag, "side": "BUY", "price": "0.501", "size": "0.000000"},
            ],
        },
        {
            "callback_type": "order_book_deltas",
            "ts_event": _event_ns(5),
            "ts_init": _event_ns(5),
            "deltas": [
                {"sequence": 7, "action": "UPDATE", "flags": last_flag, "side": "SELL", "price": "0.620", "size": "5.000000"},
            ],
        },
    ]

    assert callbacks == expected
    assert summary["replay_mode"] == "pmxt_research"
    assert summary["claim_scope"] == "plumbing_only"
    assert summary["order_book_deltas_count"] == 4
    assert summary["trade_ticks_count"] == 2

    run_dir = Path(summary["run_dir"])
    resolved = json.loads((run_dir / "resolved_config.json").read_text(encoding="utf-8"))
    assert resolved["replay"]["ordering_key"] == PMXT_RESEARCH_ORDERING_KEY
    assert resolved["replay"]["ordering_ambiguous"] is True
    assert resolved["replay"]["ambiguous_ties_accepted"] is True
    assert resolved["replay"]["performance_claims_allowed"] is False
    assert resolved["tick_size"]["effective_tick_size_changes"][0]["sequence"] == 2
    assert resolved["replay"]["ts_init_audit"]["adjusted_event_count"] >= 1


def test_pmxt_research_invalid_price_fails_fast_before_engine_run(tmp_path: Path) -> None:
    pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")
    from polymarket.backtest_v1 import run_from_config

    rows = [
        _base_row(
            event_type="book",
            timestamp_received=_iso(1),
            timestamp=_iso(1),
            bids=json.dumps([["1.010", "1"]]),
            asks=json.dumps([["0.990", "1"]]),
        ),
    ]
    event_dir = _write_event_dir(tmp_path, rows)
    strategy_path = tmp_path / "callback_strategy.py"
    callback_path = tmp_path / "callbacks.jsonl"
    config_path = tmp_path / "experiment.yml"
    _write_callback_strategy(strategy_path)
    _write_config(config_path, event_dir, strategy_path, callback_path)

    with pytest.raises(ValueError, match=r"Polymarket price must be in \[0, 1\].*price=1\.010"):
        run_from_config(config_path)
    assert not callback_path.exists(), "invalid native conversion must fail before strategy callbacks are written"

