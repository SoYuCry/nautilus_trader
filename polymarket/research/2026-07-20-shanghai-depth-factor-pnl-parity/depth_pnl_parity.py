"""Compare direct executable depth-factor labels with Nautilus-native fills and PnL."""

# ruff: noqa: I001

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
import nautilus_trader.core.data  # noqa: E402,F401

sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE  # noqa: E402
from nautilus_trader.backtest.config import BacktestEngineConfig  # noqa: E402
from nautilus_trader.backtest.engine import BacktestEngine  # noqa: E402
from nautilus_trader.config import LoggingConfig, StrategyConfig  # noqa: E402
from nautilus_trader.model.currencies import pUSD  # noqa: E402
from nautilus_trader.model.data import OrderBookDeltas, TradeTick  # noqa: E402
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide, TimeInForce  # noqa: E402
from nautilus_trader.model.events import OrderFilled  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId, TraderId  # noqa: E402
from nautilus_trader.model.instruments import Instrument  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402
from polymarket._core.models import PolymarketL2DatasetV1  # noqa: E402
from polymarket._core.nautilus_native import convert_dataset_to_nautilus  # noqa: E402
from polymarket._core.nautilus_native import load_binary_option_from_config  # noqa: E402
from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.replay_contract import PMXT_REPLAY_CLOCK  # noqa: E402


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "outputs"
SHANGHAI_DIR = Path(
    r"C:\Projects\PolyReaper\data\curated\polymarket\events-rebuild\highest-temperature-in-shanghai-on-june-9-2026",
)
FACTOR_PROTOCOL_PATH = (
    REPO_ROOT / "polymarket" / "research" / "2026-07-14-pmxt-weather-factor-wave0" / "factor_protocol.py"
)
THRESHOLD = 0.10
HOLD_SECONDS = 120
QUANTITY = 1.0
TOLERANCE = 1e-12


def _load_factor_protocol() -> Any:
    spec = importlib.util.spec_from_file_location("shanghai_depth_pnl_factor_protocol", FACTOR_PROTOCOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load factor protocol: {FACTOR_PROTOCOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FACTOR_PROTOCOL = _load_factor_protocol()


def _float(value: Any) -> float:
    return float(str(value)) if value is not None else math.nan


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=TOLERANCE, abs_tol=TOLERANCE)


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class DepthPnlConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId


class DepthPnlStrategy(Strategy):
    """Long-only executable strategy used solely for parity evidence."""

    def __init__(self, instrument_id: str) -> None:
        super().__init__(DepthPnlConfig(instrument_id=InstrumentId.from_str(instrument_id)))
        self.instrument: Instrument | None = None
        self.callbacks: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.rejections: list[str] = []
        self._book_signature: tuple[Any, ...] | None = None
        self._entry_submitted = False
        self._entry_filled = False
        self._exit_submitted = False
        self._entry_fill_ts = 0
        self._current_sequence: int | None = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            raise RuntimeError(f"Missing instrument {self.config.instrument_id}")
        self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:  # noqa: C901 - parity flow is intentionally linear
        if self.instrument is None:
            return
        sequences = {int(delta.sequence) for delta in deltas.deltas}
        if len(sequences) != 1:
            raise RuntimeError(f"Expected one source sequence per callback, got {sorted(sequences)}")
        sequence = next(iter(sequences))
        self._current_sequence = sequence
        book = self.cache.order_book(self.config.instrument_id)
        if book is None:
            raise RuntimeError("Nautilus cache returned no order book")
        bid = _float(book.best_bid_price())
        ask = _float(book.best_ask_price())
        bid_size = _float(book.best_bid_size())
        ask_size = _float(book.best_ask_size())
        # Nautilus reports an absent side's size as zero, while the research
        # replay represents the whole missing top level as NaN. Normalize the
        # native callback to the research meaning before parity comparison.
        if not math.isfinite(bid):
            bid_size = math.nan
        if not math.isfinite(ask):
            ask_size = math.nan
        valid = math.isfinite(bid) and math.isfinite(ask) and bid < ask
        total = bid_size + ask_size
        factor = (bid_size - ask_size) / total if valid and total > 0 else math.nan
        signature = (
            tuple((str(level.price), float(level.size())) for level in book.bids()),
            tuple((str(level.price), float(level.size())) for level in book.asks()),
        )
        actual_mutation = signature != self._book_signature
        self._book_signature = signature
        callback = {
            "sequence": sequence,
            "ts_init": int(deltas.ts_init),
            "actual_mutation": actual_mutation,
            "bid1": bid,
            "ask1": ask,
            "bid1_size": bid_size,
            "ask1_size": ask_size,
            "depth_imbalance_1": factor,
            "action": "none",
        }

        if self._entry_filled:
            due = int(deltas.ts_init) >= self._entry_fill_ts + HOLD_SECONDS * 1_000_000_000
            if due and not self._exit_submitted:
                if math.isfinite(bid) and math.isfinite(bid_size) and bid_size >= QUANTITY:
                    callback["action"] = "submit_exit"
                    self._exit_submitted = True
                    self.submit_order(
                        self.order_factory.market(
                            instrument_id=self.instrument.id,
                            order_side=OrderSide.SELL,
                            quantity=self.instrument.make_qty(QUANTITY),
                            time_in_force=TimeInForce.GTC,
                        ),
                    )
                else:
                    callback["action"] = "exit_due_missing_bid"
        elif not self._entry_submitted and actual_mutation and valid and factor >= THRESHOLD:
            if math.isfinite(ask_size) and ask_size >= QUANTITY:
                callback["action"] = "submit_entry"
                self._entry_submitted = True
                self.submit_order(
                    self.order_factory.market(
                        instrument_id=self.instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=self.instrument.make_qty(QUANTITY),
                        time_in_force=TimeInForce.GTC,
                    ),
                )
            else:
                callback["action"] = "signal_missing_ask_liquidity"
        self.callbacks.append(callback)

    def on_order_filled(self, event: OrderFilled) -> None:
        self.fills.append(
            {
                "sequence": self._current_sequence,
                "side": "BUY" if event.order_side == OrderSide.BUY else "SELL",
                "ts_init": int(event.ts_init),
                "price": _float(event.last_px),
                "quantity": _float(event.last_qty),
                "commission": _float(event.commission.as_decimal()),
            },
        )
        if event.order_side == OrderSide.BUY:
            self._entry_filled = True
            self._entry_fill_ts = int(event.ts_init)
        else:
            self._entry_submitted = False
            self._entry_filled = False
            self._exit_submitted = False
            self._entry_fill_ts = 0

    def on_order_rejected(self, event: Any) -> None:
        self.rejections.append(str(event))


@dataclass(frozen=True)
class NativeContext:
    instrument: Any
    data: tuple[Any, ...]
    callback_ts_by_sequence: dict[int, int]


def load_dataset(event_dir: Path, condition_id: str, asset_id: str) -> PolymarketL2DatasetV1:
    return PMXTEventV1Adapter(repo_root=REPO_ROOT).load(
        {
            "input": {
                "event_dir": str(event_dir),
                "dataset_id": event_dir.name,
                "condition_id": condition_id,
                "asset_id": asset_id,
            },
        },
    )


def native_context(dataset: PolymarketL2DatasetV1, asset_id: str) -> NativeContext:
    instrument = load_binary_option_from_config(
        {"maker_fee": "0", "taker_fee": "0", "fee_source": "parity_forced_zero"},
        dataset=dataset,
        selected_asset_id=asset_id,
    )
    conversion = convert_dataset_to_nautilus(
        dataset,
        instrument=instrument,
        selected_asset_id=asset_id,
        replay_clock=PMXT_REPLAY_CLOCK,
    )
    callback_ts: dict[int, int] = {}
    for item in conversion.data:
        if not isinstance(item, OrderBookDeltas):
            continue
        sequences = {int(delta.sequence) for delta in item.deltas}
        if len(sequences) != 1:
            raise RuntimeError(f"Native deltas contain mixed sequences: {sorted(sequences)}")
        callback_ts[next(iter(sequences))] = int(item.ts_init)
    return NativeContext(instrument=instrument, data=conversion.data, callback_ts_by_sequence=callback_ts)


def direct_rows(dataset: PolymarketL2DatasetV1, context: NativeContext) -> list[dict[str, Any]]:
    panel = FACTOR_PROTOCOL.build_factor_panel(dataset, horizons_seconds=(), include_labels=False)
    rows = []
    for row in panel.itertuples(index=False):
        sequence = int(row.sequence)
        if sequence not in context.callback_ts_by_sequence:
            continue
        factor = float(row.depth_imbalance_1)
        depth = float(row.top_level_depth)
        bid_size = depth * (1 + factor) / 2 if math.isfinite(depth) and math.isfinite(factor) else math.nan
        ask_size = depth * (1 - factor) / 2 if math.isfinite(depth) and math.isfinite(factor) else math.nan
        rows.append(
            {
                "sequence": sequence,
                "ts_init": context.callback_ts_by_sequence[sequence],
                "actual_mutation": bool(row.actual_mutation),
                "bid1": float(row.bid1),
                "ask1": float(row.ask1),
                "bid1_size": bid_size,
                "ask1_size": ask_size,
                "depth_imbalance_1": factor,
            },
        )
    return rows


def direct_oracle(rows: list[dict[str, Any]]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    entry: dict[str, Any] | None = None
    missing_entry_ask_callbacks = 0
    exit_due_missing_bid_callbacks = 0
    for row in rows:
        valid = math.isfinite(row["bid1"]) and math.isfinite(row["ask1"]) and row["bid1"] < row["ask1"]
        if entry is not None:
            due = row["ts_init"] >= entry["ts_init"] + HOLD_SECONDS * 1_000_000_000
            if not due:
                continue
            if not math.isfinite(row["bid1"]) or not math.isfinite(row["bid1_size"]) or row["bid1_size"] < QUANTITY:
                exit_due_missing_bid_callbacks += 1
                continue
            exit_action = {"sequence": row["sequence"], "side": "SELL", "ts_init": row["ts_init"], "price": row["bid1"]}
            actions.append(exit_action)
            trades.append(
                {
                    "entry_sequence": entry["sequence"],
                    "exit_sequence": row["sequence"],
                    "entry_ts_init": entry["ts_init"],
                    "exit_ts_init": row["ts_init"],
                    "entry_price": entry["price"],
                    "exit_price": row["bid1"],
                    "quantity": QUANTITY,
                    "gross_pnl": (row["bid1"] - entry["price"]) * QUANTITY,
                },
            )
            entry = None
            continue
        signal = row["actual_mutation"] and valid and row["depth_imbalance_1"] >= THRESHOLD
        if not signal:
            if row["actual_mutation"] and row["bid1"] >= 0.999 and not math.isfinite(row["ask1"]):
                missing_entry_ask_callbacks += 1
            continue
        if not math.isfinite(row["ask1_size"]) or row["ask1_size"] < QUANTITY:
            missing_entry_ask_callbacks += 1
            continue
        entry = {"sequence": row["sequence"], "ts_init": row["ts_init"], "price": row["ask1"]}
        actions.append({"sequence": row["sequence"], "side": "BUY", "ts_init": row["ts_init"], "price": row["ask1"]})
    return {
        "actions": actions,
        "trades": trades,
        "open_entry": entry,
        "missing_entry_ask_callbacks": missing_entry_ask_callbacks,
        "exit_due_missing_bid_callbacks": exit_due_missing_bid_callbacks,
    }


def run_native(context: NativeContext) -> tuple[DepthPnlStrategy, pd.DataFrame, pd.DataFrame]:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("DEPTH-PNL-PARITY-001"),
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        ),
    )
    strategy = DepthPnlStrategy(str(context.instrument.id))
    try:
        engine.add_venue(
            venue=POLYMARKET_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=pUSD,
            starting_balances=[Money.from_str("10000 pUSD")],
            fee_model=None,
            book_type=BookType.L2_MBP,
            trade_execution=True,
            liquidity_consumption=False,
            queue_position=False,
        )
        engine.add_instrument(context.instrument)
        deltas = [item for item in context.data if isinstance(item, OrderBookDeltas)]
        trades = [item for item in context.data if isinstance(item, TradeTick)]
        if deltas:
            engine.add_data(deltas, sort=False)
        if trades:
            engine.add_data(trades, sort=False)
        engine.sort_data()
        engine.add_strategy(strategy)
        engine.run()
        fills_report = engine.trader.generate_order_fills_report().copy()
        positions_report = engine.trader.generate_positions_report().copy()
    finally:
        engine.dispose()
    return strategy, fills_report, positions_report


def native_round_trips(fills: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    trades: list[dict[str, Any]] = []
    entry: dict[str, Any] | None = None
    for fill in fills:
        if fill["side"] == "BUY":
            if entry is not None:
                raise RuntimeError("Received BUY while direct parity position is already open")
            entry = fill
        else:
            if entry is None:
                raise RuntimeError("Received SELL without preceding BUY")
            trades.append(
                {
                    "entry_sequence": entry["sequence"],
                    "exit_sequence": fill["sequence"],
                    "entry_ts_init": entry["ts_init"],
                    "exit_ts_init": fill["ts_init"],
                    "entry_price": entry["price"],
                    "exit_price": fill["price"],
                    "quantity": entry["quantity"],
                    "gross_pnl": (fill["price"] - entry["price"]) * entry["quantity"],
                },
            )
            entry = None
    return trades, entry


def compare_case(dataset: PolymarketL2DatasetV1, asset_id: str) -> dict[str, Any]:  # noqa: C901 - explicit audit checks
    context = native_context(dataset, asset_id)
    expected_rows = direct_rows(dataset, context)
    oracle = direct_oracle(expected_rows)
    strategy, fills_report, positions_report = run_native(context)
    native_trades, native_open = native_round_trips(strategy.fills)
    callback_by_sequence = {row["sequence"]: row for row in strategy.callbacks}
    factor_mismatches = []
    for row in expected_rows:
        actual = callback_by_sequence.get(row["sequence"])
        if actual is None:
            factor_mismatches.append({"sequence": row["sequence"], "reason": "missing_callback"})
            continue
        fields = []
        comparison_fields = ["bid1", "ask1", "depth_imbalance_1"]
        # The factor panel intentionally emits NaN top-level depth for a
        # one-sided book, so individual side sizes cannot be reconstructed
        # from it. Compare sizes only where both paths observe a valid BBO;
        # one-sided execution behavior is checked separately via actions.
        if math.isfinite(row["bid1"]) and math.isfinite(row["ask1"]):
            comparison_fields.extend(("bid1_size", "ask1_size"))
        for field in comparison_fields:
            left, right = row[field], actual[field]
            if math.isnan(left) and math.isnan(right):
                continue
            if not _close(left, right):
                fields.append(field)
        if row["actual_mutation"] != actual["actual_mutation"]:
            fields.append("actual_mutation")
        if fields:
            factor_mismatches.append({"sequence": row["sequence"], "fields": fields})

    expected_actions = oracle["actions"]
    native_actions = [
        {"sequence": fill["sequence"], "side": fill["side"], "ts_init": fill["ts_init"], "price": fill["price"]}
        for fill in strategy.fills
    ]
    action_mismatches = []
    for index in range(max(len(expected_actions), len(native_actions))):
        expected = expected_actions[index] if index < len(expected_actions) else None
        actual = native_actions[index] if index < len(native_actions) else None
        if expected is None or actual is None:
            action_mismatches.append({"index": index, "direct": expected, "native": actual})
            continue
        fields = [field for field in ("sequence", "side", "ts_init") if expected[field] != actual[field]]
        if not _close(expected["price"], actual["price"]):
            fields.append("price")
        if fields:
            action_mismatches.append({"index": index, "fields": fields, "direct": expected, "native": actual})

    pnl_mismatches = []
    for index in range(max(len(oracle["trades"]), len(native_trades))):
        expected = oracle["trades"][index] if index < len(oracle["trades"]) else None
        actual = native_trades[index] if index < len(native_trades) else None
        if expected is None or actual is None:
            pnl_mismatches.append({"index": index, "direct": expected, "native": actual})
            continue
        fields = [
            field
            for field in ("entry_sequence", "exit_sequence", "entry_ts_init", "exit_ts_init")
            if expected[field] != actual[field]
        ]
        for field in ("entry_price", "exit_price", "quantity", "gross_pnl"):
            if not _close(expected[field], actual[field]):
                fields.append(field)
        if fields:
            pnl_mismatches.append({"index": index, "fields": fields, "direct": expected, "native": actual})

    commission_total = sum(fill["commission"] for fill in strategy.fills)
    checks = {
        "factor_values_match": not factor_mismatches,
        "fill_actions_match": not action_mismatches,
        "round_trip_pnl_matches": not pnl_mismatches,
        "commissions_are_zero": _close(commission_total, 0.0),
        "no_order_rejections": not strategy.rejections,
        "open_position_state_matches": (oracle["open_entry"] is None) == (native_open is None),
    }
    native_missing_exit = sum(row["action"] == "exit_due_missing_bid" for row in strategy.callbacks)
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "counts": {
            "direct_callback_rows": len(expected_rows),
            "native_callbacks": len(strategy.callbacks),
            "fills": len(strategy.fills),
            "completed_round_trips": len(native_trades),
            "direct_missing_entry_ask_callbacks": oracle["missing_entry_ask_callbacks"],
            "direct_exit_due_missing_bid_callbacks": oracle["exit_due_missing_bid_callbacks"],
            "native_exit_due_missing_bid_callbacks": native_missing_exit,
            "fills_report_rows": len(fills_report),
            "positions_report_rows": len(positions_report),
        },
        "gross_pnl": {
            "direct": sum(row["gross_pnl"] for row in oracle["trades"]),
            "native": sum(row["gross_pnl"] for row in native_trades),
            "commission": commission_total,
        },
        "open_position": {"direct": oracle["open_entry"], "native": native_open},
        "factor_mismatches": factor_mismatches[:20],
        "action_mismatches": action_mismatches[:20],
        "pnl_mismatches": pnl_mismatches[:20],
        "rejections": strategy.rejections[:20],
        "trade_digest": _digest(native_trades),
    }


def _iso(second: int) -> str:
    minute, second = divmod(second, 60)
    return f"2026-01-01T00:{minute:02d}:{second:02d}Z"


def write_synthetic_event(root: Path, name: str, rows: list[dict[str, Any]]) -> tuple[Path, str, str]:
    condition = "0x" + hashlib.sha256(name.encode()).hexdigest()
    yes = str(int(hashlib.sha256((name + "-yes").encode()).hexdigest(), 16))
    no = str(int(hashlib.sha256((name + "-no").encode()).hexdigest(), 16))
    for row in rows:
        row["market"] = condition.encode()
        row["asset_id"] = yes
    event_dir = root / name
    event_dir.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False)
    market = {
        "conditionId": condition,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([yes, no]),
        "orderPriceMinTickSize": "0.001",
        "feeSchedule": json.dumps({"rate": "0"}),
        "umaResolutionStatus": "open",
        "closedTime": "2026-01-02T00:00:00Z",
    }
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": [market]}), encoding="utf-8")
    (event_dir / "event_index.json").write_text(json.dumps({"markets": [{"conditionId": condition}]}), encoding="utf-8")
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-depth-pnl-parity"}), encoding="utf-8")
    return event_dir, condition, yes


def _base(event_type: str, second: int, **values: Any) -> dict[str, Any]:
    row = {
        "timestamp": _iso(second), "timestamp_received": _iso(second), "event_type": event_type,
        "market": None, "asset_id": None, "bids": None, "asks": None, "side": None,
        "price": None, "size": None, "best_bid": None, "best_ask": None,
        "old_tick_size": None, "new_tick_size": None,
    }
    row.update(values)
    return row


def synthetic_cases() -> dict[str, list[dict[str, Any]]]:
    return {
        "normal_round_trip": [
            _base("book", 0, bids=json.dumps([["0.45", "20"]]), asks=json.dumps([["0.55", "5"]])),
            _base("price_change", 120, side="BUY", price="0.45", size="10"),
        ],
        "missing_entry_ask_at_0999": [
            _base("tick_size_change", 0, old_tick_size="0.01", new_tick_size="0.001"),
            _base("book", 1, bids=json.dumps([["0.999", "20"]]), asks=json.dumps([])),
            _base("price_change", 121, side="BUY", price="0.999", size="21"),
        ],
        "missing_exit_bid_then_recovers": [
            _base("book", 0, bids=json.dumps([["0.45", "20"]]), asks=json.dumps([["0.55", "5"]])),
            _base("book", 120, bids=json.dumps([]), asks=json.dumps([["0.55", "5"]])),
            _base("book", 130, bids=json.dumps([["0.44", "20"]]), asks=json.dumps([["0.55", "5"]])),
        ],
    }


def run_synthetic() -> list[dict[str, Any]]:
    results = []
    with tempfile.TemporaryDirectory(prefix="depth-pnl-parity-") as temp:
        for name, rows in synthetic_cases().items():
            event_dir, condition, asset = write_synthetic_event(Path(temp), name, rows)
            result = compare_case(load_dataset(event_dir, condition, asset), asset)
            result["case"] = name
            results.append(result)
    return results


def shanghai_yes_cases() -> list[dict[str, str]]:
    index = json.loads((SHANGHAI_DIR / "event_index.json").read_text(encoding="utf-8"))
    cases = []
    for position, market in enumerate(index["markets"]):
        yes_token = str(market["yesToken"])
        cases.append(
            {
                "outcome_index": position,
                "question": str(market.get("question") or f"outcome-{position}"),
                "condition_id": str(market["conditionId"]),
                "asset_id": yes_token,
            },
        )
    return cases


def run_shanghai(limit: int = 1) -> list[dict[str, Any]]:
    results = []
    for case in shanghai_yes_cases()[:limit]:
        result = compare_case(load_dataset(SHANGHAI_DIR, case["condition_id"], case["asset_id"]), case["asset_id"])
        result.update(case)
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--shanghai-only", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--shanghai-limit", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    if not (args.synthetic_only or args.shanghai_only or args.all):
        parser.error("choose --synthetic-only, --shanghai-only or --all")
    payload: dict[str, Any] = {
        "scope": "shanghai_depth_factor_pnl_parity",
        "threshold": THRESHOLD,
        "hold_seconds": HOLD_SECONDS,
        "quantity": QUANTITY,
        "fee_mode": "forced_zero",
    }
    if args.synthetic_only or args.all:
        payload["synthetic"] = run_synthetic()
    if args.shanghai_only or args.all:
        payload["shanghai"] = run_shanghai(limit=args.shanghai_limit)
    payload["summary_signature"] = _digest(payload)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "depth_pnl_parity.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    statuses = [row["status"] for key in ("synthetic", "shanghai") for row in payload.get(key, [])]
    print(json.dumps({"output": str(output), "statuses": statuses, "signature": payload["summary_signature"]}, indent=2))
    return 0 if statuses and all(status == "pass" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
