"""Research-only smoke for direct factor replay -> Nautilus strategy callbacks."""

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

# Run this file outside the repository root so the installed compiled Nautilus
# package is imported before the local Python sources are added to sys.path.
REPO_ROOT = Path(__file__).resolve().parents[3]
import nautilus_trader.core.data  # noqa: E402,F401

sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE  # noqa: E402
from nautilus_trader.backtest.config import BacktestEngineConfig  # noqa: E402
from nautilus_trader.backtest.engine import BacktestEngine  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import pUSD  # noqa: E402
from nautilus_trader.model.data import OrderBookDeltas  # noqa: E402
from nautilus_trader.model.data import TradeTick  # noqa: E402
from nautilus_trader.model.enums import AccountType, BookType, OmsType  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId, TraderId  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.trading.config import StrategyConfig  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402
from polymarket._core.models import PolymarketL2DatasetV1  # noqa: E402
from polymarket._core.nautilus_native import convert_dataset_to_nautilus  # noqa: E402
from polymarket._core.nautilus_native import load_binary_option_from_config  # noqa: E402
from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.replay_contract import PMXT_REPLAY_CLOCK  # noqa: E402


OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
REBUILD_ROOT = Path(r"C:\Projects\PolyReaper\data\curated\polymarket\events-rebuild")
FACTOR_PROTOCOL_PATH = (
    REPO_ROOT / "polymarket" / "research" / "2026-07-14-pmxt-weather-factor-wave0" / "factor_protocol.py"
)
PILOT_PATH = (
    REPO_ROOT
    / "polymarket"
    / "research"
    / "2026-07-15-pmxt-weather-next-stage-experiments-rebuild"
    / "compact"
    / "pilot_6_event"
    / "direct_native_parity.json"
)
SIGNAL_THRESHOLD = 0.10


def _load_factor_protocol() -> Any:
    spec = importlib.util.spec_from_file_location("weather_factor_protocol_callback_smoke", FACTOR_PROTOCOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load factor protocol: {FACTOR_PROTOCOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FACTOR_PROTOCOL = _load_factor_protocol()


class FactorProbeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId


class FactorProbeStrategy(Strategy):
    """Observe the same top-of-book factor a real Nautilus strategy would see."""

    def __init__(self, instrument_id: str) -> None:
        super().__init__(FactorProbeConfig(instrument_id=InstrumentId.from_str(instrument_id)))
        self.callbacks: list[dict[str, Any]] = []
        self._last_book_signature: tuple[Any, ...] | None = None

    def on_start(self) -> None:
        self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        book = self.cache.order_book(self.config.instrument_id)
        if book is None:
            raise RuntimeError(f"Order book missing in callback: {self.config.instrument_id}")
        bid_price = book.best_bid_price()
        ask_price = book.best_ask_price()
        bid_size = book.best_bid_size()
        ask_size = book.best_ask_size()
        bid = float(str(bid_price)) if bid_price is not None else math.nan
        ask = float(str(ask_price)) if ask_price is not None else math.nan
        bid_qty = float(str(bid_size)) if bid_size is not None else math.nan
        ask_qty = float(str(ask_size)) if ask_size is not None else math.nan
        valid = math.isfinite(bid) and math.isfinite(ask) and bid < ask
        denominator = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / denominator if valid and denominator > 0 else math.nan
        book_signature = (
            tuple((str(level.price), float(level.size())) for level in book.bids()),
            tuple((str(level.price), float(level.size())) for level in book.asks()),
        )
        actual_mutation = book_signature != self._last_book_signature
        self._last_book_signature = book_signature
        sequences = [int(delta.sequence) for delta in deltas.deltas]
        if not sequences or len(set(sequences)) != 1:
            raise RuntimeError(f"Expected one source sequence per callback, got {sequences}")
        self.callbacks.append(
            {
                "callback_type": "OrderBookDeltas",
                "sequence": sequences[0],
                "ts_init": int(deltas.ts_init),
                "ts_event": int(deltas.ts_event),
                "bid1": bid,
                "ask1": ask,
                "depth_imbalance_1": imbalance,
                "actual_mutation": actual_mutation,
                "ranking_observation": actual_mutation and valid,
            },
        )

    def on_trade_tick(self, tick: TradeTick) -> None:
        self.callbacks.append(
            {
                "callback_type": "TradeTick",
                "sequence": None,
                "ts_init": int(tick.ts_init),
                "ts_event": int(tick.ts_event),
            },
        )


def _signal(value: float) -> int:
    if not math.isfinite(value):
        return 0
    if value >= SIGNAL_THRESHOLD:
        return 1
    if value <= -SIGNAL_THRESHOLD:
        return -1
    return 0


def _close(left: float, right: float, tolerance: float = 1e-12) -> bool:
    if math.isnan(left) and math.isnan(right):
        return True
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


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


def direct_observations(dataset: PolymarketL2DatasetV1) -> list[dict[str, Any]]:
    panel = FACTOR_PROTOCOL.build_factor_panel(dataset, horizons_seconds=(), include_labels=False)
    rows: list[dict[str, Any]] = []
    for row in panel.itertuples(index=False):
        rows.append(
            {
                "sequence": int(row.sequence),
                "actual_mutation": bool(row.actual_mutation),
                "ranking_observation": bool(row.ranking_observation),
                "bid1": float(row.bid1),
                "ask1": float(row.ask1),
                "depth_imbalance_1": float(row.depth_imbalance_1),
                "signal": _signal(float(row.depth_imbalance_1)) if bool(row.ranking_observation) else 0,
            },
        )
    return rows


def native_callbacks(dataset: PolymarketL2DatasetV1, asset_id: str) -> tuple[list[dict[str, Any]], list[int]]:
    instrument = load_binary_option_from_config({}, dataset=dataset, selected_asset_id=asset_id)
    conversion = convert_dataset_to_nautilus(
        dataset,
        instrument=instrument,
        selected_asset_id=asset_id,
        replay_clock=PMXT_REPLAY_CLOCK,
    )
    expected_ts = [
        int(item.ts_init)
        for item in conversion.data
        if isinstance(item, (OrderBookDeltas, TradeTick))
    ]
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("FACTOR-CALLBACK-001"),
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        ),
    )
    strategy = FactorProbeStrategy(str(instrument.id))
    try:
        engine.add_venue(
            venue=POLYMARKET_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=pUSD,
            starting_balances=[Money.from_str("10000 pUSD")],
            book_type=BookType.L2_MBP,
            trade_execution=False,
            liquidity_consumption=False,
            queue_position=False,
        )
        engine.add_instrument(instrument)
        deltas = [item for item in conversion.data if isinstance(item, OrderBookDeltas)]
        trades = [item for item in conversion.data if isinstance(item, TradeTick)]
        if deltas:
            engine.add_data(deltas, sort=False)
        if trades:
            engine.add_data(trades, sort=False)
        engine.sort_data()
        engine.add_strategy(strategy)
        engine.run()
        callbacks = list(strategy.callbacks)
    finally:
        engine.dispose()
    return callbacks, expected_ts


def compare_dataset(dataset: PolymarketL2DatasetV1, asset_id: str) -> dict[str, Any]:
    direct = direct_observations(dataset)
    callbacks, expected_ts = native_callbacks(dataset, asset_id)
    book_callbacks = {row["sequence"]: row for row in callbacks if row["callback_type"] == "OrderBookDeltas"}
    callback_ts = [row["ts_init"] for row in callbacks]
    ranking = [row for row in direct if row["ranking_observation"]]
    missing_sequences = [row["sequence"] for row in ranking if row["sequence"] not in book_callbacks]
    state_mismatches: list[dict[str, Any]] = []
    for row in ranking:
        callback = book_callbacks.get(row["sequence"])
        if callback is None:
            continue
        fields = [name for name in ("bid1", "ask1", "depth_imbalance_1") if not _close(row[name], callback[name])]
        if fields:
            state_mismatches.append(
                {
                    "sequence": row["sequence"],
                    "fields": fields,
                    "direct": {name: row[name] for name in fields},
                    "native": {name: callback[name] for name in fields},
                },
            )
    direct_signal_sequences = [row["sequence"] for row in ranking if row["signal"] != 0]
    native_signal_sequences = [
        row["sequence"]
        for row in callbacks
        if row.get("ranking_observation")
        and _signal(row["depth_imbalance_1"]) != 0
    ]
    ranking_sequences = [row["sequence"] for row in ranking]
    callback_ranking_sequences = [row["sequence"] for row in callbacks if row.get("sequence") in set(ranking_sequences)]
    no_op_sequences = [row["sequence"] for row in direct if not row["actual_mutation"]]
    mutation_mismatches = [
        row["sequence"]
        for row in direct
        if row["sequence"] in book_callbacks
        and row["actual_mutation"] != book_callbacks[row["sequence"]]["actual_mutation"]
    ]
    no_op_signal_leaks = [
        row["sequence"]
        for row in callbacks
        if row.get("sequence") in set(no_op_sequences) and row.get("ranking_observation")
    ]
    checks = {
        "callback_ts_matches_native_input": callback_ts == expected_ts,
        "callback_ts_monotonic": callback_ts == sorted(callback_ts),
        "ranking_sequences_preserved": callback_ranking_sequences == ranking_sequences,
        "ranking_sequences_complete": not missing_sequences,
        "callback_book_state_matches_direct": not state_mismatches,
        "callback_mutation_semantics_match_direct": not mutation_mismatches,
        "signal_trigger_sequences_match": direct_signal_sequences == native_signal_sequences,
        "no_op_not_ranked_or_signaled": not no_op_signal_leaks,
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "counts": {
            "direct_rows": len(direct),
            "ranking_observations": len(ranking),
            "native_callbacks": len(callbacks),
            "native_book_callbacks": len(book_callbacks),
            "no_op_rows": len(no_op_sequences),
            "signal_triggers": len(direct_signal_sequences),
        },
        "missing_sequences": missing_sequences[:20],
        "state_mismatches": state_mismatches[:20],
        "mutation_mismatches": mutation_mismatches[:20],
        "signal_sequence_digest": _digest(direct_signal_sequences),
        "signal_sequence_sample": {
            "first": direct_signal_sequences[:10],
            "last": direct_signal_sequences[-10:],
        },
    }


@dataclass(frozen=True)
class Case:
    event_slug: str
    leg: str
    condition_id: str
    asset_id: str


def real_cases() -> list[Case]:
    records = json.loads(PILOT_PATH.read_text(encoding="utf-8"))
    return [
        Case(row["event_slug"], row["leg"], row["condition_id"], row["asset_id"])
        for row in records
        if row["status"] == "pass"
    ]


def _iso(second: int) -> str:
    return f"2026-01-01T00:00:{second:02d}Z"


def synthetic_event(root: Path) -> tuple[Path, str, str]:
    condition = "0x" + "1" * 64
    yes = "2" * 64
    no = "3" * 64
    base = {
        "market": condition.encode(), "asset_id": yes, "bids": None, "asks": None,
        "side": None, "price": None, "size": None, "best_bid": None, "best_ask": None,
        "old_tick_size": None, "new_tick_size": None,
    }
    rows = [
        {**base, "event_type": "price_change", "timestamp": _iso(2), "timestamp_received": _iso(2), "side": "SELL", "price": "0.55", "size": "8"},
        {**base, "event_type": "book", "timestamp": _iso(1), "timestamp_received": _iso(1), "bids": json.dumps([["0.45", "10"]]), "asks": json.dumps([["0.55", "10"]])},
        {**base, "event_type": "price_change", "timestamp": _iso(2), "timestamp_received": _iso(2), "side": "BUY", "price": "0.45", "size": "12"},
        {**base, "event_type": "price_change", "timestamp": _iso(3), "timestamp_received": _iso(3), "side": "SELL", "price": "0.55", "size": "8"},
        {**base, "event_type": "last_trade_price", "timestamp": _iso(4), "timestamp_received": _iso(4), "side": "BUY", "price": "0.55", "size": "1"},
    ]
    event_dir = root / "synthetic-factor-callback"
    event_dir.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False)
    market = {
        "conditionId": condition, "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([yes, no]), "orderPriceMinTickSize": "0.01",
        "feeSchedule": json.dumps({"rate": "0"}),
        "umaResolutionStatus": "open", "closedTime": "2026-01-02T00:00:00Z",
    }
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": [market]}), encoding="utf-8")
    (event_dir / "event_index.json").write_text(json.dumps({"markets": [{"conditionId": condition}]}), encoding="utf-8")
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic"}), encoding="utf-8")
    return event_dir, condition, yes


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def run_synthetic() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="factor-callback-smoke-") as temp:
        event_dir, condition, asset = synthetic_event(Path(temp))
        result = compare_dataset(load_dataset(event_dir, condition, asset), asset)
    result["case"] = "synthetic"
    return result


def run_real() -> list[dict[str, Any]]:
    results = []
    for case in real_cases():
        result = compare_dataset(load_dataset(REBUILD_ROOT / case.event_slug, case.condition_id, case.asset_id), case.asset_id)
        result.update({"event_slug": case.event_slug, "leg": case.leg, "asset_id": case.asset_id})
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--real-only", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    if not (args.synthetic_only or args.real_only or args.all):
        parser.error("choose --synthetic-only, --real-only or --all")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"scope": "factor_callback_replay_smoke", "signal_threshold": SIGNAL_THRESHOLD}
    if args.synthetic_only or args.all:
        payload["synthetic"] = run_synthetic()
    if args.real_only or args.all:
        payload["real"] = run_real()
    payload["summary_signature"] = _digest(payload)
    path = args.output_dir / "factor_callback_smoke.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    statuses = []
    if "synthetic" in payload:
        statuses.append(payload["synthetic"]["status"])
    statuses.extend(row["status"] for row in payload.get("real", []))
    return 0 if statuses and all(status == "pass" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
