"""Contract tests for the Polymarket v1 Nautilus-native runner."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest


pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")

from polymarket.backtest_v1 import run_from_config


def write_ndjson(path: Path) -> None:
    rows = [
        {
            "local_msg_index": 1,
            "recv_wall_time_utc": "2026-06-26T02:25:28.635Z",
            "raw_json": {
                "event_type": "book",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.600Z",
                "bids": [["0.40", "100"]],
                "asks": [["0.60", "100"]],
            },
        },
        {
            "local_msg_index": 2,
            "recv_wall_time_utc": "2026-06-26T02:25:29.000Z",
            "raw_json": {
                "event_type": "price_change",
                "market": "condition",
                "timestamp": "2026-06-26T02:25:28.900Z",
                "price_changes": [
                    {
                        "asset_id": "yes",
                        "side": "BUY",
                        "price": "0.41",
                        "size": "50",
                    },
                    {
                        "asset_id": "yes",
                        "side": "SELL",
                        "price": "0.60",
                        "size": "90",
                    },
                ],
            },
        },
        {
            "local_msg_index": 3,
            "recv_wall_time_utc": "2026-06-26T02:25:30.000Z",
            "raw_json": {
                "event_type": "last_trade_price",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:29.900Z",
                "side": "BUY",
                "price": "0.60",
                "size": "10",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_ndjson_with_tick_change(path: Path) -> None:
    rows = [
        {
            "local_msg_index": 1,
            "recv_wall_time_utc": "2026-06-26T02:25:28.635Z",
            "raw_json": {
                "event_type": "book",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.600Z",
                "bids": [["0.40", "100"]],
                "asks": [["0.60", "100"]],
            },
        },
        {
            "local_msg_index": 2,
            "recv_wall_time_utc": "2026-06-26T02:25:29.000Z",
            "raw_json": {
                "event_type": "tick_size_change",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.900Z",
                "old_tick_size": "0.01",
                "new_tick_size": "0.001",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_ndjson_with_post_tick_change_liquidity(path: Path) -> None:
    rows = [
        {
            "local_msg_index": 1,
            "recv_wall_time_utc": "2026-06-26T02:25:28.635Z",
            "raw_json": {
                "event_type": "book",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.600Z",
                "bids": [["0.40", "100"]],
                "asks": [["0.60", "100"]],
            },
        },
        {
            "local_msg_index": 2,
            "recv_wall_time_utc": "2026-06-26T02:25:29.000Z",
            "raw_json": {
                "event_type": "tick_size_change",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.900Z",
                "old_tick_size": "0.01",
                "new_tick_size": "0.001",
            },
        },
        {
            "local_msg_index": 3,
            "recv_wall_time_utc": "2026-06-26T02:25:29.500Z",
            "raw_json": {
                "event_type": "price_change",
                "market": "condition",
                "timestamp": "2026-06-26T02:25:29.400Z",
                "price_changes": [
                    {
                        "asset_id": "yes",
                        "side": "SELL",
                        "price": "0.601",
                        "size": "50",
                    },
                ],
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_ndjson_terminal_yes(path: Path) -> None:
    rows = [
        {
            "local_msg_index": 1,
            "recv_wall_time_utc": "2026-06-26T02:25:28.635Z",
            "raw_json": {
                "event_type": "book",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.600Z",
                "bids": [["0.99", "100"]],
                "asks": [["1.00", "100"]],
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_take_best_ask_once_strategy(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from decimal import Decimal

            from nautilus_trader.config import StrategyConfig
            from nautilus_trader.model.book import OrderBook
            from nautilus_trader.model.data import OrderBookDeltas
            from nautilus_trader.model.enums import BookType
            from nautilus_trader.model.enums import OrderSide
            from nautilus_trader.model.enums import TimeInForce
            from nautilus_trader.model.identifiers import InstrumentId
            from nautilus_trader.model.instruments import Instrument
            from nautilus_trader.trading.strategy import Strategy


            class TakeBestAskOnceConfig(StrategyConfig, frozen=True):
                instrument_id: InstrumentId
                quantity: Decimal


            class TakeBestAskOnce(Strategy):
                def __init__(self, instrument_id: str, quantity: str = "1") -> None:
                    config = TakeBestAskOnceConfig(
                        instrument_id=InstrumentId.from_str(instrument_id),
                        quantity=Decimal(str(quantity)),
                    )
                    super().__init__(config)
                    self.instrument: Instrument | None = None
                    self.submitted = False

                def on_start(self) -> None:
                    self.instrument = self.cache.instrument(self.config.instrument_id)
                    if self.instrument is None:
                        self.stop()
                        return
                    self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

                def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
                    if self.submitted or self.instrument is None:
                        return
                    book: OrderBook | None = self.cache.order_book(self.config.instrument_id)
                    if book is None or book.best_ask_price() is None:
                        return
                    order = self.order_factory.market(
                        instrument_id=self.instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=self.instrument.make_qty(self.config.quantity),
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submitted = True
                    self.submit_order(order)
            """,
        ).lstrip(),
        encoding="utf-8",
    )


def test_runner_uses_nautilus_backtest_engine_and_native_reports(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: native_runner_smoke
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: native-runner-smoke
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_dir = Path(summary["run_dir"])
    resolved = json.loads(Path(summary["outputs"]["resolved_config"]).read_text(encoding="utf-8"))
    run_summary = json.loads(Path(summary["outputs"]["summary"]).read_text(encoding="utf-8"))

    assert summary["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert run_summary["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert resolved["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert summary["order_book_deltas_count"] == 2
    assert summary["trade_ticks_count"] == 1
    assert (run_dir / "account.csv").exists()
    assert (run_dir / "fills.csv").exists()
    assert (run_dir / "positions.csv").exists()
    assert (run_dir / "raw_nautilus" / "account.csv").exists()
    assert (run_dir / "raw_nautilus" / "fills.csv").exists()
    assert (run_dir / "raw_nautilus" / "positions.csv").exists()
    assert not (run_dir / "account_report.txt").exists()
    assert not (run_dir / "fills_report.txt").exists()
    assert not (run_dir / "positions_report.txt").exists()
    assert (run_dir / "run_report.md").exists()


def test_runner_executes_native_strategy_and_reports_fill(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    strategy_path = tmp_path / "strategy_take_best_ask.py"
    strategy_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from decimal import Decimal

            from nautilus_trader.config import StrategyConfig
            from nautilus_trader.model.book import OrderBook
            from nautilus_trader.model.data import OrderBookDeltas
            from nautilus_trader.model.enums import BookType
            from nautilus_trader.model.enums import OrderSide
            from nautilus_trader.model.enums import TimeInForce
            from nautilus_trader.model.identifiers import InstrumentId
            from nautilus_trader.model.instruments import Instrument
            from nautilus_trader.trading.strategy import Strategy


            class TakeBestAskOnceConfig(StrategyConfig, frozen=True):
                instrument_id: InstrumentId
                quantity: Decimal


            class TakeBestAskOnce(Strategy):
                def __init__(self, instrument_id: str, quantity: str = "1") -> None:
                    config = TakeBestAskOnceConfig(
                        instrument_id=InstrumentId.from_str(instrument_id),
                        quantity=Decimal(str(quantity)),
                    )
                    super().__init__(config)
                    self.instrument: Instrument | None = None
                    self.submitted = False

                def on_start(self) -> None:
                    self.instrument = self.cache.instrument(self.config.instrument_id)
                    if self.instrument is None:
                        self.stop()
                        return
                    self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

                def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
                    if self.submitted or self.instrument is None:
                        return
                    book: OrderBook | None = self.cache.order_book(self.config.instrument_id)
                    if book is None or book.best_ask_price() is None:
                        return
                    order = self.order_factory.market(
                        instrument_id=self.instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=self.instrument.make_qty(self.config.quantity),
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submitted = True
                    self.submit_order(order)
            """,
        ).lstrip(),
        encoding="utf-8",
    )
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: native_runner_strategy
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            instrument:
              condition_id: "condition"
              token_id: "yes"
              price_increment: "0.001"
              size_increment: "0.000001"
              taker_fee: "0.05"
            fees:
              enabled: true
              maker_rebates_enabled: false
            strategy:
              enabled: true
              path: {strategy_path.as_posix()}
              class: TakeBestAskOnce
              params:
                instrument_id: "condition-yes.POLYMARKET"
                quantity: "1"
            runtime:
              run_id: native-runner-strategy
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_summary = json.loads(Path(summary["outputs"]["summary"]).read_text(encoding="utf-8"))
    resolved = json.loads(Path(summary["outputs"]["resolved_config"]).read_text(encoding="utf-8"))
    raw_fills = Path(summary["outputs"]["raw_nautilus_fills"]).read_text(encoding="utf-8")
    fills_csv = Path(summary["outputs"]["fills"]).read_text(encoding="utf-8")
    positions = Path(summary["outputs"]["positions"]).read_text(encoding="utf-8")
    run_report = Path(summary["outputs"]["run_report"]).read_text(encoding="utf-8")

    assert summary["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert run_summary["order_book_deltas_count"] == 2
    assert run_summary["trade_ticks_count"] == 1
    assert resolved["strategy"]["class"] == "TakeBestAskOnce"
    assert resolved["fees"]["model"] == "PolymarketFeeModel"
    assert resolved["fees"]["maker_rebates_enabled"] is False
    assert resolved["fees"]["require_explicit"] is False
    assert resolved["fees"]["instrument_taker_fee"] == "0.05"
    assert resolved["fees"]["instrument_fee_source"] == "instrument_config"
    assert run_summary["fees"]["instrument_fee_source"] == "instrument_config"
    assert run_summary["fees"]["totals_from_fills_report"]["total_display"] == "0.012 pUSD"
    assert "MARKET" in raw_fills
    assert "BUY" in raw_fills
    assert "FILLED" in raw_fills
    assert "1.000000" in raw_fills
    assert "net_cashflow_pusd" in fills_csv
    assert "0.012 pUSD" in fills_csv
    assert "LONG" in positions
    assert "avg_px_open" in positions
    assert "0.6" in positions
    assert "TradeTick count" in run_report
    assert "## Fees" in run_report
    assert "Fee source: `instrument_config`" in run_report
    assert "Total fees from `raw_nautilus/fills.csv`: `0.012 pUSD`" in run_report


def test_runner_rejects_strategy_limit_price_outside_effective_tick(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live_tick_change.ndjson"
    write_ndjson_with_tick_change(ndjson_path)
    strategy_path = tmp_path / "strategy_bad_tick.py"
    strategy_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from decimal import Decimal

            from nautilus_trader.config import StrategyConfig
            from nautilus_trader.model.data import OrderBookDeltas
            from nautilus_trader.model.enums import BookType
            from nautilus_trader.model.enums import OrderSide
            from nautilus_trader.model.enums import TimeInForce
            from nautilus_trader.model.identifiers import InstrumentId
            from nautilus_trader.model.instruments import Instrument
            from nautilus_trader.trading.strategy import Strategy


            class BadTickLimitConfig(StrategyConfig, frozen=True):
                instrument_id: InstrumentId


            class BadTickLimit(Strategy):
                def __init__(self, instrument_id: str) -> None:
                    super().__init__(BadTickLimitConfig(instrument_id=InstrumentId.from_str(instrument_id)))
                    self.instrument: Instrument | None = None
                    self.submitted = False

                def on_start(self) -> None:
                    self.instrument = self.cache.instrument(self.config.instrument_id)
                    self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

                def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
                    if self.submitted or self.instrument is None:
                        return
                    order = self.order_factory.limit(
                        instrument_id=self.instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=self.instrument.make_qty(Decimal("1")),
                        price=self.instrument.make_price(Decimal("0.501")),
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submitted = True
                    self.submit_order(order)
            """,
        ).lstrip(),
        encoding="utf-8",
    )
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: effective_tick_guard
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: true
              path: {strategy_path.as_posix()}
              class: BadTickLimit
              params:
                instrument_id: "condition-yes.POLYMARKET"
            runtime:
              run_id: effective-tick-guard
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"price violates effective tick size.*0\.501.*0\.01"):
        run_from_config(config_path)


def test_runner_allows_strategy_base_rounded_limit_price(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    strategy_path = tmp_path / "strategy_rounded_tick.py"
    strategy_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from decimal import Decimal

            from nautilus_trader.config import StrategyConfig
            from nautilus_trader.model.data import OrderBookDeltas
            from nautilus_trader.model.enums import BookType
            from nautilus_trader.model.enums import OrderSide
            from nautilus_trader.model.enums import TimeInForce
            from nautilus_trader.model.identifiers import InstrumentId
            from nautilus_trader.model.instruments import Instrument

            from polymarket.strategy import PolymarketStrategyBase


            class RoundedTickLimitConfig(StrategyConfig, frozen=True):
                instrument_id: InstrumentId


            class RoundedTickLimit(PolymarketStrategyBase):
                def __init__(self, instrument_id: str) -> None:
                    super().__init__(RoundedTickLimitConfig(instrument_id=InstrumentId.from_str(instrument_id)))
                    self.instrument: Instrument | None = None
                    self.submitted = False

                def on_start(self) -> None:
                    self.instrument = self.cache.instrument(self.config.instrument_id)
                    self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

                def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
                    if self.submitted or self.instrument is None:
                        return
                    rounded_price = self.make_polymarket_price(
                        Decimal("0.601"),
                        side=OrderSide.BUY,
                        intent="aggressive",
                    )
                    order = self.order_factory.limit(
                        instrument_id=self.instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=self.instrument.make_qty(Decimal("1")),
                        price=rounded_price,
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submitted = True
                    self.submit_order(order)
            """,
        ).lstrip(),
        encoding="utf-8",
    )
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: rounded_effective_tick
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: true
              path: {strategy_path.as_posix()}
              class: RoundedTickLimit
              params:
                instrument_id: "condition-yes.POLYMARKET"
            engine:
              trade_execution: true
              liquidity_consumption: false
              queue_position: false
              bypass_logging: true
              run_analysis: false
            runtime:
              run_id: rounded-effective-tick
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    resolved = json.loads(Path(summary["outputs"]["resolved_config"]).read_text(encoding="utf-8"))

    assert resolved["strategy"]["polymarket_strategy_base_tick_timeline"]["enabled"] is True
    assert resolved["strategy"]["effective_tick_size_order_guard"]["enabled"] is True
    rounding = resolved["strategy"]["polymarket_price_rounding"]
    assert rounding["enabled"] is True
    assert rounding["count"] == 1
    assert rounding["events"][0]["original_price"] == "0.601"
    assert rounding["events"][0]["rounded_price"] == "0.61"
    fills_csv = Path(summary["outputs"]["fills"]).read_text(encoding="utf-8")
    assert "BUY" in fills_csv
    assert "0.6" in fills_csv


def test_runner_strategy_base_uses_post_tick_change_precision(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live_post_tick_change.ndjson"
    write_ndjson_with_post_tick_change_liquidity(ndjson_path)
    strategy_path = tmp_path / "strategy_post_tick.py"
    strategy_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from decimal import Decimal

            from nautilus_trader.config import StrategyConfig
            from nautilus_trader.model.data import OrderBookDeltas
            from nautilus_trader.model.enums import BookType
            from nautilus_trader.model.enums import OrderSide
            from nautilus_trader.model.enums import TimeInForce
            from nautilus_trader.model.identifiers import InstrumentId
            from nautilus_trader.model.instruments import Instrument

            from polymarket.strategy import PolymarketStrategyBase


            class PostTickLimitConfig(StrategyConfig, frozen=True):
                instrument_id: InstrumentId


            class PostTickLimit(PolymarketStrategyBase):
                def __init__(self, instrument_id: str) -> None:
                    super().__init__(PostTickLimitConfig(instrument_id=InstrumentId.from_str(instrument_id)))
                    self.instrument: Instrument | None = None
                    self.delta_count = 0
                    self.submitted = False

                def on_start(self) -> None:
                    self.instrument = self.cache.instrument(self.config.instrument_id)
                    self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

                def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
                    self.delta_count += 1
                    if self.submitted or self.instrument is None or self.delta_count < 2:
                        return
                    rounded_price = self.make_polymarket_price(
                        Decimal("0.6014"),
                        side=OrderSide.BUY,
                        intent="aggressive",
                    )
                    order = self.order_factory.limit(
                        instrument_id=self.instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=self.instrument.make_qty(Decimal("1")),
                        price=rounded_price,
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submitted = True
                    self.submit_order(order)
            """,
        ).lstrip(),
        encoding="utf-8",
    )
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: post_tick_change_strategy_base
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: true
              path: {strategy_path.as_posix()}
              class: PostTickLimit
              params:
                instrument_id: "condition-yes.POLYMARKET"
            engine:
              trade_execution: true
              liquidity_consumption: false
              queue_position: false
              bypass_logging: true
              run_analysis: false
            runtime:
              run_id: post-tick-change-strategy-base
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    resolved = json.loads(Path(summary["outputs"]["resolved_config"]).read_text(encoding="utf-8"))

    rounding = resolved["strategy"]["polymarket_price_rounding"]
    assert rounding["enabled"] is True
    assert rounding["count"] == 1
    assert rounding["events"][0]["tick_size"] == "0.001"
    assert rounding["events"][0]["original_price"] == "0.6014"
    assert rounding["events"][0]["rounded_price"] == "0.602"
    fills_csv = Path(summary["outputs"]["fills"]).read_text(encoding="utf-8")
    assert "BUY" in fills_csv
    assert "0.601" in fills_csv


def test_runner_settlement_metadata_closes_open_position_without_trade_tick(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    strategy_path = tmp_path / "strategy_take_best_ask.py"
    write_take_best_ask_once_strategy(strategy_path)
    metadata_path = tmp_path / "market_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "condition_id": "condition",
                        "minimum_tick_size": "0.01",
                        "feeSchedule": {"rate": "0.05"},
                        "resolution_status": "resolved",
                        "resolution_time": "2026-06-26T02:25:31Z",
                        "resolution_source": "test_resolution_metadata",
                        "tokens": [
                            {"token_id": "yes", "outcome": "Yes", "payout": "1", "winner": True},
                            {"token_id": "no", "outcome": "No", "payout": "0", "winner": False},
                        ],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: settlement_yes
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
                market_metadata_path: {metadata_path.as_posix()}
            selection:
              asset_id: "yes"
            instrument:
              condition_id: "condition"
              token_id: "yes"
              price_increment: "0.001"
              size_increment: "0.000001"
            fees:
              enabled: true
              maker_rebates_enabled: false
            strategy:
              enabled: true
              path: {strategy_path.as_posix()}
              class: TakeBestAskOnce
              params:
                instrument_id: "condition-yes.POLYMARKET"
                quantity: "1"
            runtime:
              run_id: settlement-yes
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_summary = json.loads(Path(summary["outputs"]["summary"]).read_text(encoding="utf-8"))
    resolved = json.loads(Path(summary["outputs"]["resolved_config"]).read_text(encoding="utf-8"))
    fills_csv = Path(summary["outputs"]["fills"]).read_text(encoding="utf-8")
    positions_csv = Path(summary["outputs"]["positions"]).read_text(encoding="utf-8")
    run_report = Path(summary["outputs"]["run_report"]).read_text(encoding="utf-8")

    assert run_summary["trade_ticks_count"] == 1
    assert run_summary["instrument_close_count"] == 1
    assert run_summary["settlement"]["mode"] == "official"
    assert run_summary["settlement"]["payout"] == "1"
    assert resolved["settlement"]["mode"] == "official"
    assert resolved["settlement"]["mechanism"] == "Nautilus InstrumentClose + venue settlement_prices"
    assert "EXPIRATION" in fills_csv
    assert ",SELL," in fills_csv
    assert ",1.0," in fills_csv
    assert ",1.0," in positions_csv
    assert "0.388000 pUSD" in positions_csv
    assert "Settlement enabled: `true`" in run_report
    assert "Settlement mode: `official`" in run_report
    assert "not converted into a market `TradeTick`" in run_report


def test_runner_infers_settlement_and_marks_report_as_inferred(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "terminal_yes.ndjson"
    write_ndjson_terminal_yes(ndjson_path)
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: settlement_inferred
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: settlement-inferred
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_summary = json.loads(Path(summary["outputs"]["summary"]).read_text(encoding="utf-8"))
    run_report = Path(summary["outputs"]["run_report"]).read_text(encoding="utf-8")

    assert run_summary["instrument_close_count"] == 1
    assert run_summary["settlement"]["mode"] == "inferred"
    assert run_summary["settlement"]["payout"] == "1"
    assert run_summary["settlement"]["evidence"]["terminal_mark_source"] == "bbo_mid"
    assert summary["settlement_mode"] == "inferred"
    assert "Settlement mode: `inferred`" in run_report
    assert "research convenience, not official" in run_report


def test_runner_leaves_settlement_open_and_reports_positions(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: settlement_open
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: settlement-open
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_summary = json.loads(Path(summary["outputs"]["summary"]).read_text(encoding="utf-8"))
    run_report = Path(summary["outputs"]["run_report"]).read_text(encoding="utf-8")

    assert run_summary["instrument_close_count"] == 0
    assert run_summary["settlement"]["mode"] == "open"
    assert run_summary["settlement"]["enabled"] is False
    assert summary["settlement_mode"] == "open"
    assert "Settlement mode: `open`" in run_report
    assert "## Final open positions" in run_report


def test_runner_require_explicit_fee_rejects_default_zero_fallback(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: require_explicit_fee
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            fees:
              require_explicit: true
            strategy:
              enabled: false
            runtime:
              run_id: require-explicit-fee
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"fees.require_explicit=true.*fee_source=default_zero"):
        run_from_config(config_path)


def test_report_output_dir_must_stay_inside_experiment_runs(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "bad-output.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: bad_output
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: bad-output
            report:
              output_dir: ../outside-runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="experiment-local runs"):
        run_from_config(config_path)


PMXT_CONDITION_ID = "0xabc123"
PMXT_YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"  # noqa: S105 - synthetic fixture token, not a secret
PMXT_NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"  # noqa: S105 - synthetic fixture token, not a secret


def write_pmxt_event_dir(tmp_path: Path) -> Path:
    """Synthetic PMXT event dir whose receive times invert after the source-time sort."""
    pd = pytest.importorskip("pandas")

    def iso(seconds: int) -> str:
        return f"2026-01-01T00:00:{seconds:02d}Z"

    rows = [
        {
            "event_type": "price_change",
            "market": PMXT_CONDITION_ID.encode(),
            "asset_id": PMXT_YES_TOKEN,
            "timestamp_received": iso(9),
            "timestamp": iso(2),
            "bids": None,
            "asks": None,
            "side": "BUY",
            "price": "0.41",
            "size": "5",
            "best_bid": None,
            "best_ask": None,
            "old_tick_size": None,
            "new_tick_size": None,
        },
        {
            "event_type": "book",
            "market": PMXT_CONDITION_ID.encode(),
            "asset_id": PMXT_YES_TOKEN,
            "timestamp_received": iso(3),
            "timestamp": iso(1),
            "bids": json.dumps([["0.40", "10"]]),
            "asks": json.dumps([["0.60", "9"]]),
            "side": None,
            "price": None,
            "size": None,
            "best_bid": None,
            "best_ask": None,
            "old_tick_size": None,
            "new_tick_size": None,
        },
        {
            "event_type": "last_trade_price",
            "market": PMXT_CONDITION_ID.encode(),
            "asset_id": PMXT_YES_TOKEN,
            "timestamp_received": iso(4),
            "timestamp": iso(3),
            "bids": None,
            "asks": None,
            "side": "SELL",
            "price": "0.40",
            "size": "2",
            "best_bid": None,
            "best_ask": None,
            "old_tick_size": None,
            "new_tick_size": None,
        },
    ]
    event_dir = tmp_path / "pmxt-event"
    event_dir.mkdir()
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False)
    (event_dir / "gamma_event.raw.json").write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "conditionId": PMXT_CONDITION_ID,
                        "outcomes": json.dumps(["Yes", "No"]),
                        "outcomePrices": json.dumps(["1", "0"]),
                        "clobTokenIds": json.dumps([PMXT_YES_TOKEN, PMXT_NO_TOKEN]),
                        "feeSchedule": json.dumps({"rate": "0.02"}),
                        "umaResolutionStatus": "resolved",
                        "closedTime": "2026-01-02T03:04:05Z",
                        "orderPriceMinTickSize": "0.001",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (event_dir / "event_index.json").write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "conditionId": PMXT_CONDITION_ID,
                        "yesToken": PMXT_YES_TOKEN,
                        "noToken": PMXT_NO_TOKEN,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-pmxt-test"}), encoding="utf-8")
    return event_dir


def write_pmxt_config(config_path: Path, event_dir: Path, *, replay_mode: str | None) -> None:
    replay_block = f"replay:\n  mode: {replay_mode}\n" if replay_mode is not None else ""
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: pmxt_research_backtest
            adapter:
              name: pmxt_event_v1
              input:
                event_dir: {event_dir.as_posix()}
                condition_id: "{PMXT_CONDITION_ID}"
                asset_id: "{PMXT_YES_TOKEN}"
            selection:
              asset_id: "{PMXT_YES_TOKEN}"
            strategy:
              enabled: false
            runtime:
              run_id: pmxt-research-mode
            report:
              output_dir: ./runs
            """,
        ).lstrip()
        + replay_block,
        encoding="utf-8",
    )


def test_runner_rejects_pmxt_adapter_without_explicit_research_mode(tmp_path: Path) -> None:
    event_dir = write_pmxt_event_dir(tmp_path)
    config_path = tmp_path / "experiment.yml"
    write_pmxt_config(config_path, event_dir, replay_mode=None)

    with pytest.raises(ValueError, match="PMXT event data is exploratory"):
        run_from_config(config_path)


def test_runner_runs_pmxt_research_mode_with_marked_outputs(tmp_path: Path) -> None:
    event_dir = write_pmxt_event_dir(tmp_path)
    config_path = tmp_path / "experiment.yml"
    write_pmxt_config(config_path, event_dir, replay_mode="pmxt_research")

    summary = run_from_config(config_path)
    run_dir = Path(summary["run_dir"])
    resolved = json.loads((run_dir / "resolved_config.json").read_text(encoding="utf-8"))
    run_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    data_health = json.loads((run_dir / "data_health.json").read_text(encoding="utf-8"))
    run_report = (run_dir / "run_report.md").read_text(encoding="utf-8")

    # PMXT source-time sort makes receive time non-monotonic: still diagnostic,
    # not a blocker, in pmxt_research mode.
    assert data_health["summary"]["receive_time_inversion_count"] > 0
    assert summary["replay_mode"] == "pmxt_research"
    assert summary["replay_clock"] == "pmxt_replay_timestamp"
    assert summary["data_credibility"].startswith("pmxt_research_reconstructed_order")
    assert resolved["replay"]["mode"] == "pmxt_research"
    assert resolved["replay"]["ordering_key"] == "timestamp,timestamp_received,_original_row_index"
    assert resolved["replay"]["execution_claims_allowed"] is False
    assert resolved["replay"]["replay_clock_check"]["replay_clock_monotonic"] is True
    assert run_summary["replay"]["mode"] == "pmxt_research"
    assert "## Replay trust boundary" in run_report
    assert "Replay mode: `pmxt_research`" in run_report
    assert "not" in run_summary["replay"]["disclaimer"]
    # The engine still replayed the data and settled the resolved token.
    assert summary["order_book_deltas_count"] == 2
    assert summary["trade_ticks_count"] == 1
    assert summary["instrument_close_count"] == 1
    assert summary["settlement_mode"] == "official"


def test_runner_strict_mode_keeps_marking_and_receive_time_gate(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: strict_capture_marking
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: strict-marking
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_dir = Path(summary["run_dir"])
    resolved = json.loads((run_dir / "resolved_config.json").read_text(encoding="utf-8"))
    run_report = (run_dir / "run_report.md").read_text(encoding="utf-8")

    assert summary["replay_mode"] == "strict_capture"
    assert summary["replay_clock"] == "timestamp_received"
    assert summary["data_credibility"] == "strict_capture_receive_time"
    assert resolved["replay"]["mode"] == "strict_capture"
    assert "Replay mode: `strict_capture`" in run_report
