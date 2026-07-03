"""Inspect PolymarketL2DatasetV1 -> Nautilus-native data conversion.

This is a debug helper for the critical projection boundary:

    PolymarketL2DatasetV1
        -> OrderBookDeltas / TradeTick

It intentionally does not run BacktestEngine or strategy code.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag

from polymarket.adapters.live_ws_v1 import LiveWsV1Adapter
from polymarket.data_health import analyze_dataset_health
from polymarket._core.nautilus_native import convert_dataset_to_nautilus
from polymarket._core.nautilus_native import load_binary_option_from_config


REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--max-items", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = args.config if args.config.is_absolute() else REPO_ROOT / args.config
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    adapter_config = config.get("adapter") or {}
    if adapter_config.get("name") != LiveWsV1Adapter.adapter_name:
        raise ValueError("inspect_nautilus_conversion currently expects adapter.name=live_ws_v1")

    dataset = LiveWsV1Adapter(repo_root=REPO_ROOT).load(adapter_config)
    selected_asset_id = (config.get("selection") or {}).get("asset_id")
    instrument = load_binary_option_from_config(
        config.get("instrument") or {},
        dataset=dataset,
        selected_asset_id=selected_asset_id,
    )
    health_config = config.get("data_health") or {}
    health = analyze_dataset_health(
        dataset,
        future_tolerance_ms=float(health_config.get("future_tolerance_ms", 50.0)),
        delay_warning_ms=float(health_config.get("delay_warning_ms", 1_000.0)),
    )
    conversion = convert_dataset_to_nautilus(
        dataset,
        instrument=instrument,
        selected_asset_id=selected_asset_id,
        fail_on_tick_size_change=bool((config.get("replay") or {}).get("fail_on_tick_size_change", True)),
    )

    summary = {
        "config": _repo_relative(config_path),
        "instrument": {
            "id": str(instrument.id),
            "raw_symbol": str(instrument.raw_symbol),
            "price_increment": str(instrument.price_increment),
            "size_increment": str(instrument.size_increment),
        },
        "dataset": {
            "step_count": len(dataset.steps),
            "adapter_name": dataset.metadata.adapter_name,
            "source_files": list(dataset.metadata.source_files),
        },
        "data_health_ok": health.ok,
        "data_health_issue_codes": [issue.code for issue in health.issues],
        "conversion": {
            "data_count": len(conversion.data),
            "skipped_updates": list(conversion.skipped_updates),
            "tick_size_changes": [list(pair) for pair in conversion.tick_size_changes],
            "items": [_native_item_to_dict(item) for item in conversion.data[: args.max_items]],
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _native_item_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, OrderBookDeltas):
        return {
            "type": "OrderBookDeltas",
            "instrument_id": str(item.instrument_id),
            "is_snapshot": bool(item.is_snapshot),
            "sequence": int(item.sequence),
            "ts_event": _ts_dict(item.ts_event),
            "ts_init": _ts_dict(item.ts_init),
            "deltas": [_delta_to_dict(delta) for delta in item.deltas],
        }
    if isinstance(item, TradeTick):
        return {
            "type": "TradeTick",
            "instrument_id": str(item.instrument_id),
            "price": str(item.price),
            "size": str(item.size),
            "aggressor_side": _aggressor_side_label(item.aggressor_side),
            "trade_id": str(item.trade_id),
            "ts_event": _ts_dict(item.ts_event),
            "ts_init": _ts_dict(item.ts_init),
        }
    return {"type": type(item).__name__, "repr": repr(item)}


def _delta_to_dict(delta: Any) -> dict[str, Any]:
    order = delta.order
    return {
        "action": _book_action_label(delta.action),
        "side": _order_side_label(order.side),
        "price": str(order.price),
        "size": str(order.size),
        "flags": _record_flags(delta.flags),
        "sequence": int(delta.sequence),
        "ts_event": _ts_dict(delta.ts_event),
        "ts_init": _ts_dict(delta.ts_init),
    }


def _ts_dict(value: int) -> dict[str, Any]:
    seconds, nanos = divmod(int(value), 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1_000)
    return {"ns": int(value), "iso": dt.isoformat().replace("+00:00", "Z")}


def _book_action_label(value: Any) -> str:
    mapping = {
        BookAction.ADD: "ADD",
        BookAction.UPDATE: "UPDATE",
        BookAction.DELETE: "DELETE",
        BookAction.CLEAR: "CLEAR",
    }
    return mapping.get(value, str(value))


def _order_side_label(value: Any) -> str:
    mapping = {
        OrderSide.BUY: "BUY",
        OrderSide.SELL: "SELL",
        OrderSide.NO_ORDER_SIDE: "NO_ORDER_SIDE",
    }
    return mapping.get(value, str(value))


def _aggressor_side_label(value: Any) -> str:
    mapping = {
        AggressorSide.BUYER: "BUYER",
        AggressorSide.SELLER: "SELLER",
        AggressorSide.NO_AGGRESSOR: "NO_AGGRESSOR",
    }
    return mapping.get(value, str(value))


def _record_flags(value: Any) -> list[str]:
    raw = int(value)
    flags: list[str] = []
    if raw & int(RecordFlag.F_SNAPSHOT):
        flags.append("F_SNAPSHOT")
    if raw & int(RecordFlag.F_LAST):
        flags.append("F_LAST")
    if not flags and raw:
        flags.append(str(raw))
    return flags


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    main()

