"""
PMXT wave-minus1 ordering/parity evidence program.

Research-only evidence generator for the chain:
PMXTEventV1Adapter -> PolymarketL2DatasetV1/O1 -> convert_dataset_to_nautilus
-> Nautilus native objects/O3 -> engine callbacks.

This module deliberately does not compute factors, outcomes, strategy returns, fills,
positions, cash, fees, or PnL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Iterable
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from polymarket._core.models import PolymarketL2DatasetV1
from polymarket._core.nautilus_native import convert_dataset_to_nautilus
from polymarket._core.nautilus_native import load_binary_option_from_config
from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter
from polymarket.replay_contract import PMXT_REPLAY_CLOCK
from polymarket.replay_contract import PMXT_RESEARCH_MODE
from polymarket.replay_contract import build_replay_provenance
from polymarket.replay_contract import replay_timestamp
from polymarket.replay_contract import verify_pmxt_replay_clock_order


PROGRAM = "pmxt-wave-minus1-ordering-parity"
PROGRAM_VERSION = "2026-07-13.v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
DEFAULT_INVENTORY_DIR = REPO_ROOT / "polymarket" / "research" / "2026-07-13-pmxt-wave-minus1-inventory" / "outputs"

CONDITION_ID = "0xorderingparity"
YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"  # noqa: S105
NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"  # noqa: S105
OTHER_TOKEN = "3333333333333333333333333333333333333333333333333333333333333333"  # noqa: S105


def _iso(second: int, micros: int = 0) -> str:
    return datetime(2026, 1, 1, 0, 0, second, micros, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if hasattr(value, "value"):
        return str(value)
    return str(value)


def stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_json_default)


def signature(data: Any) -> str:
    return hashlib.sha256(stable_json(data).encode("utf-8")).hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")


def synthetic_rows() -> list[dict[str, Any]]:
    """Build generic synthetic fixture rows with an independently computed O1 oracle."""
    base = {
        "market": CONDITION_ID.encode(),
        "asset_id": YES_TOKEN,
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
    rows = [
        # Physical row 0 sorts late: proves O1 is not parquet/file order.
        {**base, "event_type": "price_change", "timestamp_received": _iso(1), "timestamp": _iso(5), "side": "BUY", "price": "0.520", "size": "8"},
        # Snapshot book.
        {**base, "event_type": "book", "timestamp_received": _iso(4), "timestamp": _iso(1), "bids": json.dumps([["0.40", "10"]]), "asks": json.dumps([["0.60", "9"]]), "best_bid": "0.40", "best_ask": "0.60"},
        # Crossed book: materialized as plumbing evidence, not execution truth.
        {**base, "event_type": "book", "timestamp_received": _iso(3), "timestamp": _iso(3), "bids": json.dumps([["0.70", "1"]]), "asks": json.dumps([["0.60", "1"]]), "best_bid": "0.70", "best_ask": "0.60"},
        # Tick-size change precedes fully tied trade/delta by original row ordinal.
        {**base, "event_type": "tick_size_change", "timestamp_received": _iso(2), "timestamp": _iso(2), "old_tick_size": "0.01", "new_tick_size": "0.001"},
        # Other token/condition row is O2 diagnostic material only for selected-token O1.
        {**base, "event_type": "book", "asset_id": OTHER_TOKEN, "timestamp_received": _iso(0), "timestamp": _iso(0), "bids": json.dumps([["0.10", "1"]]), "asks": json.dumps([["0.90", "1"]])},
        # Full tie with row 3 and 6; deterministic fallback only.
        {**base, "event_type": "last_trade_price", "timestamp_received": _iso(2), "timestamp": _iso(2), "side": "SELL", "price": "0.420", "size": "3"},
        {**base, "event_type": "price_change", "timestamp_received": _iso(2), "timestamp": _iso(2), "side": "BUY", "price": "0.501", "size": "7"},
        # Empty book: conversion should skip native data for this step.
        {**base, "event_type": "book", "timestamp_received": _iso(6), "timestamp": _iso(4), "bids": json.dumps([]), "asks": json.dumps([])},
    ]
    return rows


def write_event_dir(event_dir: Path, rows: list[dict[str, Any]]) -> Path:
    event_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(event_dir / "orderbook.parquet", index=False)
    gamma_market = {
        "conditionId": CONDITION_ID,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
        "feeSchedule": json.dumps({"rate": "0"}),
        "umaResolutionStatus": "open",
        "closedTime": "2026-01-02T00:00:00Z",
        "orderPriceMinTickSize": "0.001",
    }
    (event_dir / "gamma_event.raw.json").write_text(json.dumps({"markets": [gamma_market]}), encoding="utf-8")
    (event_dir / "event_index.json").write_text(
        json.dumps({"markets": [{"conditionId": CONDITION_ID, "yesToken": YES_TOKEN, "noToken": NO_TOKEN}]}),
        encoding="utf-8",
    )
    (event_dir / "manifest.json").write_text(json.dumps({"source": "synthetic-ordering-parity"}), encoding="utf-8")
    return event_dir


def load_pmxt_event(event_dir: Path, *, condition_id: str, asset_id: str) -> PolymarketL2DatasetV1:
    return PMXTEventV1Adapter(repo_root=REPO_ROOT).load(
        {"input": {"event_dir": str(event_dir), "dataset_id": event_dir.name, "condition_id": condition_id, "asset_id": asset_id}},
    )


def expected_o1_source_row_indices(rows: list[Mapping[str, Any]], *, condition_id: str, asset_id: str) -> list[int]:
    selected: list[tuple[datetime, datetime, int]] = []
    for ordinal, row in enumerate(rows):
        market = row["market"].decode("utf-8") if isinstance(row["market"], bytes) else str(row["market"])
        if market != condition_id or str(row["asset_id"]) != asset_id:
            continue
        received = pd.Timestamp(row["timestamp_received"]).to_pydatetime()
        source = pd.Timestamp(row["timestamp"]).to_pydatetime() if row.get("timestamp") is not None else received
        selected.append((source, received, ordinal))
    return [ordinal for _, _, ordinal in sorted(selected, key=lambda item: item)]


def update_identity(update: Any) -> dict[str, Any]:
    return {
        "event_type": update.event_type,
        "market": update.market,
        "asset_id": update.asset_id,
        "side": update.side,
        "price": update.price,
        "size": update.size,
        "bids": [(level.price, level.size) for level in update.bids],
        "asks": [(level.price, level.size) for level in update.asks],
        "old_tick_size": update.old_tick_size,
        "new_tick_size": update.new_tick_size,
    }


def o1_records(dataset: PolymarketL2DatasetV1) -> list[dict[str, Any]]:
    records = []
    for step in dataset.steps:
        update = step.updates[0]
        records.append(
            {
                "o": "O1",
                "sequence": step.sequence,
                "source_row_index": step.source_row_index,
                "replay_timestamp": replay_timestamp(step),
                "timestamp": step.timestamp,
                "timestamp_received": step.timestamp_received,
                "identity": update_identity(update),
            },
        )
    return records


def o2_event_wide_diagnostic_records(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    materialized = []
    for ordinal, row in enumerate(rows):
        received = pd.Timestamp(row["timestamp_received"]).to_pydatetime()
        source = pd.Timestamp(row["timestamp"]).to_pydatetime() if row.get("timestamp") is not None else received
        market = row["market"].decode("utf-8") if isinstance(row["market"], bytes) else str(row["market"])
        materialized.append(
            {
                "o": "O2",
                "diagnostic_only": True,
                "source_row_index": ordinal,
                "key": [source, received, ordinal],
                "event_type": row["event_type"],
                "market": market,
                "asset_id": str(row["asset_id"]),
            },
        )
    return sorted(materialized, key=lambda item: item["key"])


def native_identity(item: Any) -> dict[str, Any]:
    if isinstance(item, OrderBookDeltas):
        return {
            "native_type": "OrderBookDeltas",
            "ts_event": item.ts_event,
            "ts_init": item.ts_init,
            "sequences": [delta.sequence for delta in item.deltas],
            "delta_count": len(item.deltas),
            "actions": [str(delta.action) for delta in item.deltas],
        }
    if isinstance(item, TradeTick):
        return {"native_type": "TradeTick", "ts_event": item.ts_event, "ts_init": item.ts_init, "price": item.price, "size": item.size, "trade_id": str(item.trade_id)}
    if isinstance(item, InstrumentClose):
        return {"native_type": "InstrumentClose", "ts_event": item.ts_event, "ts_init": item.ts_init, "close_price": item.close_price}
    return {"native_type": type(item).__name__, "ts_init": getattr(item, "ts_init", None)}


def convert_o3(dataset: PolymarketL2DatasetV1, *, asset_id: str) -> dict[str, Any]:
    instrument = load_binary_option_from_config({}, dataset=dataset, selected_asset_id=asset_id)
    conversion = convert_dataset_to_nautilus(dataset, instrument=instrument, selected_asset_id=asset_id, replay_clock=PMXT_REPLAY_CLOCK)
    records = [{"o": "O3", "input_index": index, **native_identity(item)} for index, item in enumerate(conversion.data)]
    return {
        "instrument_id": str(instrument.id),
        "records": records,
        "counts": dict(Counter(record["native_type"] for record in records)),
        "order_signature": signature(records),
        "skipped_updates": list(conversion.skipped_updates),
        "tick_size_changes": [list(item) for item in conversion.tick_size_changes],
        "ts_init_audit": dict(conversion.ts_init_audit or {}),
    }


class OrderingProbeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId


class OrderingProbeStrategy(Strategy):
    def __init__(self, instrument_id: str) -> None:
        super().__init__(OrderingProbeConfig(instrument_id=InstrumentId.from_str(instrument_id)))
        self.callbacks: list[dict[str, Any]] = []

    def on_start(self) -> None:
        self.subscribe_order_book_deltas(self.config.instrument_id, BookType.L2_MBP)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        self.callbacks.append({"callback_type": "OrderBookDeltas", "ts_init": deltas.ts_init, "ts_event": deltas.ts_event, "sequences": [delta.sequence for delta in deltas.deltas]})

    def on_trade_tick(self, tick: TradeTick) -> None:
        self.callbacks.append({"callback_type": "TradeTick", "ts_init": tick.ts_init, "ts_event": tick.ts_event, "price": str(tick.price), "size": str(tick.size)})


def engine_callback_probe(dataset: PolymarketL2DatasetV1, *, asset_id: str) -> dict[str, Any]:
    instrument = load_binary_option_from_config({}, dataset=dataset, selected_asset_id=asset_id)
    conversion = convert_dataset_to_nautilus(dataset, instrument=instrument, selected_asset_id=asset_id, replay_clock=PMXT_REPLAY_CLOCK)
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("ORDER-PARITY-001"),
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=False,
        ),
    )
    strategy = OrderingProbeStrategy(str(instrument.id))
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
        order_book_deltas = [item for item in conversion.data if isinstance(item, OrderBookDeltas)]
        trade_ticks = [item for item in conversion.data if isinstance(item, TradeTick)]
        if order_book_deltas:
            engine.add_data(order_book_deltas, sort=False)
        if trade_ticks:
            engine.add_data(trade_ticks, sort=False)
        engine.sort_data()
        engine.add_strategy(strategy)
        engine.run()
    finally:
        engine.dispose()
    native_callback_input = [record for record in (native_identity(item) for item in conversion.data) if record["native_type"] in {"OrderBookDeltas", "TradeTick"}]
    return {
        "observable": "Nautilus strategy callbacks",
        "engine_contract": "BacktestEngine.sort_data merges separately-added native data by ts_init; callback stream is compared to that native input contract.",
        "callbacks": strategy.callbacks,
        "callback_order_signature": signature(strategy.callbacks),
        "native_callback_input_signature": signature(native_callback_input),
        "callback_count": len(strategy.callbacks),
        "native_callback_input_count": len(native_callback_input),
        "matches_native_input_contract": [item["ts_init"] for item in strategy.callbacks] == [item["ts_init"] for item in native_callback_input],
        "legal_reorder": "none_observed" if [item["ts_init"] for item in strategy.callbacks] == [item["ts_init"] for item in native_callback_input] else "engine_ts_init_sort",
    }


def source_quality_from_warning(dataset: PolymarketL2DatasetV1) -> dict[str, Any]:
    return dict(dataset.metadata.source_quality)


def invalid_empty_crossed_cases(base_dir: Path) -> list[dict[str, Any]]:
    cases = []
    for name, rows in {
        "invalid_price_rejected": [{**synthetic_rows()[1], "bids": json.dumps([["1.01", "1"]]), "asks": json.dumps([["0.99", "1"]])}],
        "empty_book_skipped": [{**synthetic_rows()[7]}],
        "crossed_book_materialized": [{**synthetic_rows()[2]}],
    }.items():
        event_dir = write_event_dir(base_dir / name, rows)
        try:
            dataset = load_pmxt_event(event_dir, condition_id=CONDITION_ID, asset_id=YES_TOKEN)
            o3 = convert_o3(dataset, asset_id=YES_TOKEN)
            cases.append({"case": name, "status": "converted", "native_counts": o3["counts"], "skipped_updates": o3["skipped_updates"]})
        except Exception as exc:
            cases.append({"case": name, "status": "error", "error_type": type(exc).__name__, "message": str(exc)})
    return cases


def build_synthetic_evidence(output_dir: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pmxt-ordering-parity-") as temp:
        temp_dir = Path(temp)
        rows = synthetic_rows()
        event_dir = write_event_dir(temp_dir / "synthetic-pmxt-event", rows)
        dataset = load_pmxt_event(event_dir, condition_id=CONDITION_ID, asset_id=YES_TOKEN)
        o1 = o1_records(dataset)
        expected_indices = expected_o1_source_row_indices(rows, condition_id=CONDITION_ID, asset_id=YES_TOKEN)
        o2 = o2_event_wide_diagnostic_records(rows)
        o3 = convert_o3(dataset, asset_id=YES_TOKEN)
        callback = engine_callback_probe(dataset, asset_id=YES_TOKEN)
        replay_check = verify_pmxt_replay_clock_order(dataset)
        provenance = build_replay_provenance(mode=PMXT_RESEARCH_MODE, dataset_metadata=dataset.metadata)
        invalid_cases = invalid_empty_crossed_cases(temp_dir / "case-events")

    evidence = {
        "program": PROGRAM,
        "program_version": PROGRAM_VERSION,
        "scope": {
            "research_only": True,
            "performance_claims_allowed": False,
            "execution_claims_allowed": False,
            "factor_outcome_strategy_pnl_computed": False,
            "generic_nautilus_engine_architecture_modified": False,
        },
        "claim_scope": "plumbing_only",
        "max_verification_level": "nautilus_plumbing_verified",
        "orders": {
            "O1": "selected-token deterministic replay order; key=(source timestamp,timestamp_received,original row ordinal)",
            "O2": "event-wide diagnostic/materialization order only; not execution truth",
            "O3": "Nautilus native object / engine callback observable order",
        },
        "o1_expected_source_row_indices": expected_indices,
        "o1_actual_source_row_indices": [record["source_row_index"] for record in o1],
        "o1_matches_independent_expected_order": [record["source_row_index"] for record in o1] == expected_indices,
        "o1": {"count": len(o1), "records": o1, "order_signature": signature(o1), "source_quality": source_quality_from_warning(dataset), "replay_clock_check": replay_check},
        "o2": {"diagnostic_only": True, "count": len(o2), "records": o2, "order_signature": signature(o2), "not_execution_truth": True},
        "o3_native": o3,
        "o3_engine_callbacks": callback,
        "conversion_preserves_o1_to_native_order": o3["ts_init_audit"]["adjusted_event_count"] >= 0 and o3["records"] == sorted(o3["records"], key=lambda item: item["ts_init"]),
        "ambiguity": {
            "ordering_ambiguous": True,
            "stable_fallback_scope": "reproducibility_only_not_true_sequence",
            "source_quality": source_quality_from_warning(dataset),
            "provenance": provenance,
        },
        "invalid_crossed_empty_book_handling": invalid_cases,
    }
    write_json(output_dir / "synthetic_ordering_parity.json", evidence)
    return evidence


def _real_event_token_candidates(
    inventory_dir: Path,
    *,
    max_candidates: int = 48,
) -> Iterable[tuple[Path, str, str, dict[str, Any]]]:
    events_path = inventory_dir / "event_inventory.parquet"
    tokens_path = inventory_dir / "token_inventory.parquet"
    if not events_path.exists() or not tokens_path.exists():
        return
    events = pd.read_parquet(events_path)
    tokens = pd.read_parquet(tokens_path)
    yielded = 0
    # Bounded smoke: prefer smaller local events, but never scan the 441-event
    # universe.  The bound is recorded in the output artifact.
    for _, event in events.sort_values(["rows_written", "event_slug"]).head(12).iterrows():
        raw_paths = event.get("paths") or {}
        event_dir = Path(str(raw_paths.get("event_dir") if isinstance(raw_paths, Mapping) else ""))
        if not (event_dir / "orderbook.parquet").exists():
            continue
        event_tokens = tokens[tokens["event_slug"] == event["event_slug"]].sort_values(["market_index", "token_side"])
        for _, token in event_tokens.iterrows():
            yield event_dir, str(token["condition_id"]), str(token["asset_id"]), {
                "event_slug": event["event_slug"],
                "rows_written": int(event["rows_written"]),
                "market_index": int(token["market_index"]),
                "token_side": str(token["token_side"]),
            }
            yielded += 1
            if yielded >= max_candidates:
                return

def build_real_smoke_evidence(output_dir: Path, *, inventory_dir: Path = DEFAULT_INVENTORY_DIR, max_o1_records: int = 50) -> dict[str, Any]:
    attempted: list[dict[str, Any]] = []
    for event_dir, condition_id, asset_id, identity in _real_event_token_candidates(inventory_dir):
        attempt = {"event_dir": str(event_dir), "condition_id": condition_id, "asset_id": asset_id, **identity}
        try:
            dataset = load_pmxt_event(event_dir, condition_id=condition_id, asset_id=asset_id)
            o1 = o1_records(dataset)
            o3 = convert_o3(dataset, asset_id=asset_id)
            replay_check = verify_pmxt_replay_clock_order(dataset)
        except Exception as exc:
            attempted.append(attempt | {"status": "rejected", "error_type": type(exc).__name__, "message": str(exc)[:500]})
            continue
        evidence = {
            "program": PROGRAM,
            "program_version": PROGRAM_VERSION,
            "status": "pass",
            "bounded_smoke": True,
            "candidate_bound": 48,
            "candidate_attempts": len(attempted) + 1,
            "rejected_candidates": attempted[:10],
            "scanned_full_441_events": False,
            "real_fixture": attempt,
            "performance_claims_allowed": False,
            "claim_scope": "plumbing_only" if dataset.metadata.source_quality.get("orderingAmbiguousRows", 0) else "research_replay_plumbing",
            "o1": {"count": len(o1), "head_records": o1[:max_o1_records], "order_signature": signature(o1), "source_quality": source_quality_from_warning(dataset), "replay_clock_check": replay_check},
            "o3_native": {key: value for key, value in o3.items() if key != "records"} | {"head_records": o3["records"][:max_o1_records]},
            "conversion_preserves_o1_to_native_order": o3["records"] == sorted(o3["records"], key=lambda item: item["ts_init"]),
            "notes": [
                "Bounded smoke uses one curated PMXT event/token selected from G001 inventory; it does not scan the 441-event universe.",
                "No factors, outcomes, strategies, fills, positions, cash, fees, PnL, or performance conclusions are computed.",
            ],
        }
        write_json(output_dir / "real_curated_smoke_parity.json", evidence)
        return evidence

    evidence = {
        "status": "skipped",
        "reason": "no bounded curated PMXT candidate converted successfully",
        "candidate_bound": 48,
        "candidate_attempts": len(attempted),
        "rejected_candidates": attempted[:10],
        "performance_claims_allowed": False,
    }
    write_json(output_dir / "real_curated_smoke_parity.json", evidence)
    return evidence

def build_all(output_dir: Path = DEFAULT_OUTPUT_DIR, *, inventory_dir: Path = DEFAULT_INVENTORY_DIR) -> dict[str, Any]:
    synthetic = build_synthetic_evidence(output_dir)
    real = build_real_smoke_evidence(output_dir, inventory_dir=inventory_dir)
    summary = {
        "program": PROGRAM,
        "program_version": PROGRAM_VERSION,
        "generated_at": datetime.now(tz=UTC),
        "performance_claims_allowed": False,
        "claim_scope": "plumbing_only",
        "max_verification_level": "nautilus_plumbing_verified",
        "outputs": {
            "synthetic": str(output_dir / "synthetic_ordering_parity.json"),
            "real_curated_smoke": str(output_dir / "real_curated_smoke_parity.json"),
        },
        "checks": {
            "synthetic_o1_expected_order": synthetic["o1_matches_independent_expected_order"],
            "synthetic_o1_to_native_monotonic": synthetic["conversion_preserves_o1_to_native_order"],
            "synthetic_o3_callbacks_match_native_input_contract": synthetic["o3_engine_callbacks"]["matches_native_input_contract"],
            "real_smoke_status": real.get("status"),
        },
    }
    write_json(output_dir / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--inventory-dir", type=Path, default=DEFAULT_INVENTORY_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = build_all(args.output_dir, inventory_dir=args.inventory_dir)
    print(json.dumps(summary, indent=2, sort_keys=True, default=_json_default))


if __name__ == "__main__":
    main()
