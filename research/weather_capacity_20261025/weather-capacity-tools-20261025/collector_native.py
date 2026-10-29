"""Task-local collector -> native records; no shared adapter or pruning changes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections import Counter
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Iterator

from nautilus_trader.model import AggressorSide
from nautilus_trader.model import AssetClass
from nautilus_trader.model import BinaryOption
from nautilus_trader.model import BookAction
from nautilus_trader.model import BookOrder
from nautilus_trader.model import Currency
from nautilus_trader.model import InstrumentClose
from nautilus_trader.model import InstrumentCloseType
from nautilus_trader.model import OrderBookDelta
from nautilus_trader.model import OrderBookDeltas
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import RecordFlag
from nautilus_trader.model import Symbol
from nautilus_trader.model import TradeTick
from polymarket._core.identifiers import determine_trade_id
from polymarket._core.identifiers import get_polymarket_instrument_id


PRUNER_PATH = Path(__file__).resolve().parent / "bbo_prune.py"

_spec = importlib.util.spec_from_file_location("temporary_weather_frozen_pruner", PRUNER_PATH)
_pruner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pruner)
PrunedBook, RULE = _pruner.PrunedBook, _pruner.RULE
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def iso_ns(value: str) -> int:
    """Parse recorded timezone-qualified ISO timestamps without losing nanoseconds."""
    match = re.fullmatch(r"(.+?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", value)
    if match is None:
        raise ValueError(f"Expected timezone-qualified receipt timestamp: {value!r}")
    whole, fraction, zone = match.groups()
    fraction = fraction or ""
    if len(fraction) > 9 and any(c != "0" for c in fraction[9:]):
        raise ValueError("Receipt timestamp has sub-nanosecond precision")
    delta = datetime.fromisoformat(whole + zone).astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int(fraction[:9].ljust(9, "0"))


def decimal(value: Any) -> Decimal:
    if isinstance(value, float):
        raise TypeError("Float price/quantity is forbidden; decode JSON with parse_float=Decimal")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite price/quantity")
    return result


def precision(value: Any) -> int:
    result = max(0, -decimal(value).as_tuple().exponent)
    if result > 16:
        raise ValueError(f"Recorded precision exceeds native precision: {value!r}")
    return result


def source_ns(message: dict) -> int:
    if message.get("timestamp") is None:
        raise ValueError(f"Missing source timestamp for {message.get('event_type')}")
    value = decimal(message["timestamp"]) * 1_000_000
    if value != value.to_integral_value():
        raise ValueError("Source timestamp has sub-nanosecond precision")
    return int(value)


def messages(raw: Path) -> Iterator[tuple[dict, dict]]:
    """Preserve original capture order; never sort by source timestamp."""
    previous_seq = previous_monotonic = None
    with raw.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if not line.endswith("\n"):
                raise ValueError(f"Frozen raw has an unterminated line: {line_number}")
            envelope = json.loads(line, parse_float=Decimal)
            seq = int(envelope["capture_seq"])
            monotonic = envelope.get("received_monotonic_ns")
            if previous_seq is not None and seq <= previous_seq:
                raise ValueError(f"Capture sequence inversion/duplicate at line {line_number}")
            if (
                monotonic is not None
                and previous_monotonic is not None
                and int(monotonic) < previous_monotonic
            ):
                raise ValueError(f"Receive monotonic clock inversion at line {line_number}")
            base = {
                "raw_line": line_number,
                "capture_seq": seq,
                "capture_seq_gap_before": 0 if previous_seq is None else seq - previous_seq - 1,
                "timestamp_received": envelope["timestamp_received"],
                "received_ns": iso_ns(envelope["timestamp_received"]),
                "received_monotonic_ns": monotonic,
            }
            previous_seq = seq
            if monotonic is not None:
                previous_monotonic = int(monotonic)
            frame = envelope["frame"]
            for index, message in enumerate(frame if isinstance(frame, list) else [frame]):
                if not isinstance(message, dict):
                    raise ValueError(f"Frame item is not an object at line {line_number}")
                yield {**base, "frame_item_index": index}, message


def token_groups(message: dict) -> list[tuple[str, list[tuple[int, dict]]]]:
    if message.get("event_type") == "price_change":
        groups = {}
        for index, change in enumerate(message.get("price_changes") or []):
            groups.setdefault(str(change["asset_id"]), []).append((index, change))
        return list(groups.items())
    if message.get("asset_id") is not None:
        return [(str(message["asset_id"]), [(0, message)])]
    return []


def level_pairs(message: dict, side: str) -> Iterator[tuple[Any, Any]]:
    for item in message.get(side) or []:
        yield (item["price"], item["size"]) if isinstance(item, dict) else (item[0], item[1])


def scan(raw: Path, metadata: dict) -> dict:
    """Discover native expression precision and evidence without admitting fake metadata."""
    tokens = {}
    for condition in metadata["conditions"]:
        condition_id = str(condition["condition_id"])
        if not condition.get("scheduled_expiration"):
            raise ValueError(f"Metadata gap: no recorded scheduled expiration for {condition_id}")
        if not condition.get("metadata_evidence"):
            raise ValueError(f"Metadata evidence absent for {condition_id}")
        outcomes = {token["outcome"].upper() for token in condition["tokens"]}
        if outcomes != {"YES", "NO"} or len(condition["tokens"]) != 2:
            raise ValueError(f"Expected all YES/NO tokens for {condition_id}")
        for token in condition["tokens"]:
            token_id = str(token["token_id"])
            if token_id in tokens:
                raise ValueError(f"Duplicate token mapping: {token_id}")
            tokens[token_id] = {
                "condition_id": condition_id,
                "outcome": token["outcome"],
                "scheduled_expiration": condition["scheduled_expiration"],
                "metadata_evidence": condition["metadata_evidence"],
                "fee_status": condition.get("fee_status", "unknown"),
                "fee_schedule": condition.get("fee_schedule"),
                "price_precision": 0,
                "size_precision": 0,
                "first_book": None,
                "initial_tick_size": None,
                "updates_before_first_book": 0,
                "message_count": 0,
            }
    kinds = Counter()
    received_min = received_max = source_min = source_max = None
    for position, message in messages(raw):
        kind = str(message.get("event_type"))
        kinds[kind] += 1
        received = position["received_ns"]
        received_min = received if received_min is None else min(received_min, received)
        received_max = received if received_max is None else max(received_max, received)
        groups = token_groups(message)
        for token_id, changes in groups:
            if token_id not in tokens:
                raise ValueError(f"Unmapped raw token (never silently filtered): {token_id}")
            state = tokens[token_id]
            condition_id = str(message.get("market") or message.get("condition_id") or "")
            if condition_id != state["condition_id"]:
                raise ValueError(f"Condition/token mapping mismatch: {condition_id}, {token_id}")
            state["message_count"] += 1
            if kind in {"book", "price_change", "last_trade_price", "tick_size_change"}:
                ts = source_ns(message)
                source_min = ts if source_min is None else min(source_min, ts)
                source_max = ts if source_max is None else max(source_max, ts)
            if kind == "book" and state["first_book"] is None:
                tick = message.get("tick_size")
                state["first_book"] = {
                    **position,
                    "ts_event": source_ns(message),
                    "tick_size": None if tick is None else str(decimal(tick)),
                }
                if state["updates_before_first_book"] == 0 and tick is not None:
                    state["initial_tick_size"] = str(decimal(tick))
            elif kind in {"price_change", "last_trade_price"} and state["first_book"] is None:
                state["updates_before_first_book"] += 1
            pairs = []
            if kind == "book":
                pairs = list(level_pairs(message, "bids")) + list(level_pairs(message, "asks"))
            elif kind in {"price_change", "last_trade_price"}:
                pairs = [(change["price"], change["size"]) for _, change in changes]
            for price, size in pairs:
                p, q = decimal(price), decimal(size)
                if not 0 <= p <= 1 or q < 0:
                    raise ValueError(f"Invalid observed level: {price}, {size}")
                state["price_precision"] = max(state["price_precision"], precision(price))
                state["size_precision"] = max(state["size_precision"], precision(size))
            for field in ("tick_size", "old_tick_size", "new_tick_size"):
                if message.get(field) is not None:
                    if decimal(message[field]) <= 0:
                        raise ValueError("Nonpositive recorded tick")
                    state["price_precision"] = max(
                        state["price_precision"], precision(message[field])
                    )
    if not kinds:
        raise ValueError("Frozen raw is empty")
    return {
        "tokens": tokens,
        "message_types": dict(kinds),
        "received_min_ns": received_min,
        "received_max_ns": received_max,
        "source_min_ns": source_min,
        "source_max_ns": source_max,
    }


def instruments_from_scan(scanned: dict) -> list[BinaryOption]:
    instruments = []
    for token_id, state in scanned["tokens"].items():
        pp, qp = state["price_precision"], state["size_precision"]
        instruments.append(
            BinaryOption(
                instrument_id=get_polymarket_instrument_id(state["condition_id"], token_id),
                raw_symbol=Symbol(token_id),
                asset_class=AssetClass.ALTERNATIVE,
                currency=Currency.from_str("pUSD"),
                activation_ns=0,
                expiration_ns=iso_ns(state["scheduled_expiration"]),
                price_precision=pp,
                size_precision=qp,
                price_increment=Price.from_str(format(Decimal(1).scaleb(-pp), "f")),
                size_increment=Quantity.from_str(format(Decimal(1).scaleb(-qp), "f")),
                ts_event=0,
                ts_init=0,
                outcome=state["outcome"],
                info={
                    "condition_id": state["condition_id"],
                    "token_id": token_id,
                    "expiration_source": "recorded_metadata.scheduled_expiration",
                    "expiration_semantics": "scheduled_only_not_actual_close_or_data_cutoff",
                    "scheduled_expiration": state["scheduled_expiration"],
                    "effective_initial_tick_size": state["initial_tick_size"],
                    "first_recorded_book": state["first_book"],
                    "expression_precision_policy": "observed_decimal_precision_not_effective_tick",
                    "fee_status": state["fee_status"],
                    "fee_schedule": state["fee_schedule"],
                    "actual_settlement_status": "not_inferred",
                    "source": "temporary_weather.collector_native",
                    "metadata_evidence": state["metadata_evidence"],
                },
            )
        )
    return instruments


def messages_with_http(raw: Path, resolutions: list) -> Iterator[tuple[dict, dict]]:
    """Merge recorded HTTP availability, never sort raw messages by source time."""
    pending = iter(sorted(resolutions, key=lambda v: iso_ns(v["evidence"]["received_at"])))
    resolution = next(pending, None)

    def http_message(value):
        received = value["evidence"]["received_at"]
        return (
            {
                "origin": "saved_http",
                "raw_line": None,
                "capture_seq": None,
                "frame_item_index": 0,
                "timestamp_received": received,
                "received_ns": iso_ns(received),
                "received_monotonic_ns": None,
                "metadata_evidence": value["evidence"],
            },
            {
                "event_type": "market_resolved",
                "market": value["condition_id"],
                "winning_asset_id": value["winning_asset_id"],
                "timestamp": str(Decimal(iso_ns(value["actual_close_time"])) / 1_000_000),
                "resolution_origin": "saved_http_explicit_resolution",
            },
        )

    for position, message in messages(raw):
        while (
            resolution is not None
            and iso_ns(resolution["evidence"]["received_at"]) <= position["received_ns"]
        ):
            yield http_message(resolution)
            resolution = next(pending, None)
        yield position, message
    while resolution is not None:
        yield http_message(resolution)
        resolution = next(pending, None)


def native_stream(
    raw: Path, instruments: list, audit: dict, trace: Any, http_resolutions=()
) -> Iterator[Any]:
    """Emit one native batch per original message/token and keep every observed level."""
    by_token = {str(item.raw_symbol): item for item in instruments}
    books = {token: PrunedBook() for token in by_token}
    counts = Counter()
    previous = -1
    previous_received = None
    sequence = defaultdict(int)
    initialized = set()
    closed = set()
    resolved_winners = {}
    audit.update(
        {"adjusted_record_count": 0, "max_adjustment_ns": 0, "receive_wall_clock_regressions": 0}
    )

    def allocate(received: int) -> int:
        nonlocal previous
        value = max(received, previous + 1)
        if value != received:
            audit["adjusted_record_count"] += 1
            audit["max_adjustment_ns"] = max(audit["max_adjustment_ns"], value - received)
        previous = value
        return value

    def record(position: dict, source_message: dict, **fields: Any) -> None:
        trace.write(
            json.dumps(
                {**position, "event_type": source_message.get("event_type"), **fields}, default=str
            )
            + "\n"
        )

    def make_price(instrument: Any, value: Any) -> Price:
        number = decimal(value)
        text = format(number, f".{instrument.price_precision}f")
        if Decimal(text) != number:
            raise ValueError("Native price precision would round source")
        return Price.from_str(text)

    def make_qty(instrument: Any, value: Any) -> Quantity:
        number = decimal(value)
        text = format(number, f".{instrument.size_precision}f")
        if Decimal(text) != number:
            raise ValueError("Native quantity precision would round source")
        return Quantity.from_str(text)

    for position, message in messages_with_http(raw, http_resolutions):
        received = position["received_ns"]
        if position["frame_item_index"] == 0:
            if previous_received is not None and received < previous_received:
                audit["receive_wall_clock_regressions"] += 1
            previous_received = received
        kind = message.get("event_type")
        if kind == "market_resolved":
            condition_id = str(message.get("market") or message.get("condition_id") or "")
            winner = str(message.get("winning_asset_id") or "")
            members = [item for item in instruments if item.info["condition_id"] == condition_id]
            if not members or winner not in {str(item.raw_symbol) for item in members}:
                raise ValueError("Unmapped actual market_resolved winner/condition")
            if condition_id in resolved_winners and resolved_winners[condition_id] != winner:
                raise ValueError(
                    f"Conflicting actual winners for condition {condition_id}: "
                    f"{resolved_winners[condition_id]} vs {winner}"
                )
            resolved_winners[condition_id] = winner
            for item in members:
                token = str(item.raw_symbol)
                if token in closed:
                    record(
                        position, message, token_id=token, status="duplicate_actual_close_evidence"
                    )
                    continue
                ts_init = allocate(received)
                close = InstrumentClose(
                    item.id,
                    make_price(item, "1" if token == winner else "0"),
                    InstrumentCloseType.CONTRACT_EXPIRED,
                    source_ns(message),
                    ts_init,
                )
                counts["InstrumentClose"] += 1
                closed.add(token)
                record(
                    position,
                    message,
                    token_id=token,
                    ts_event=close.ts_event,
                    ts_init=ts_init,
                    adjustment_ns=ts_init - received,
                    actual_settlement_evidence=message,
                )
                yield close
            continue
        groups = token_groups(message)
        if not groups:
            record(
                position, message, status="non_native_message_preserved_in_trace", message=message
            )
        for token_id, changes in groups:
            item = by_token[token_id]
            sequence[token_id] += 1
            ts_event = (
                source_ns(message)
                if kind in {"book", "price_change", "last_trade_price", "tick_size_change"}
                else None
            )
            base = {
                "token_id": token_id,
                "condition_id": item.info["condition_id"],
                "change_indices": [index for index, _ in changes],
                "ts_event": ts_event,
            }
            if kind == "tick_size_change":
                record(
                    position, message, **base, status="recorded_tick_notification", message=message
                )
                continue
            if kind not in {"book", "price_change", "last_trade_price"}:
                record(
                    position,
                    message,
                    **base,
                    status="non_native_message_preserved_in_trace",
                    message=message,
                )
                continue
            ts_init = allocate(received)
            base.update(
                {
                    "ts_init": ts_init,
                    "adjustment_ns": ts_init - received,
                    "sequence": sequence[token_id],
                }
            )
            if kind == "last_trade_price":
                side = message.get("side")
                if side not in {"BUY", "SELL"}:
                    raise ValueError(f"Unknown trade side: {side}")
                tick = TradeTick(
                    item.id,
                    make_price(item, message["price"]),
                    make_qty(item, message["size"]),
                    AggressorSide.BUY if side == "BUY" else AggressorSide.SELL,
                    determine_trade_id(
                        token_id,
                        is_buy=side == "BUY",
                        price=str(message["price"]),
                        size=str(message["size"]),
                        timestamp=str(ts_event // 1_000_000),
                    ),
                    ts_event,
                    ts_init,
                )
                record(position, message, **base, native_kind="TradeTick")
                counts["TradeTick"] += 1
                yield tick
                continue
            seq = sequence[token_id]
            deltas = []
            quote = None
            if kind == "book":
                initialized.add(token_id)
                flags = int(RecordFlag.F_SNAPSHOT)
                deltas.append(
                    OrderBookDelta(
                        item.id,
                        BookAction.CLEAR,
                        BookOrder(None, make_price(item, "0"), make_qty(item, "0"), 0),
                        flags,
                        seq,
                        ts_event,
                        ts_init,
                    )
                )
                for side, field in ((OrderSide.BUY, "bids"), (OrderSide.SELL, "asks")):
                    for price, size in level_pairs(message, field):
                        deltas.append(
                            OrderBookDelta(
                                item.id,
                                BookAction.ADD,
                                BookOrder(side, make_price(item, price), make_qty(item, size), 0),
                                flags,
                                seq,
                                ts_event,
                                ts_init,
                            )
                        )
                base["recorded_book_tick_size"] = message.get("tick_size")
                base["observed_bid_levels"] = len(message.get("bids") or [])
                base["observed_ask_levels"] = len(message.get("asks") or [])
            else:
                for index, change in changes:
                    side = change.get("side")
                    if side not in {"BUY", "SELL"}:
                        raise ValueError(f"Unknown price-change side: {side}")
                    size = decimal(change["size"])
                    deltas.append(
                        OrderBookDelta(
                            item.id,
                            BookAction.UPDATE if size > 0 else BookAction.DELETE,
                            BookOrder(
                                OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                                make_price(item, change["price"]),
                                make_qty(item, size),
                                0,
                            ),
                            0,
                            seq,
                            ts_event,
                            ts_init,
                        )
                    )
                quote = (changes[-1][1].get("best_bid"), changes[-1][1].get("best_ask"))
            last = deltas[-1]
            deltas[-1] = OrderBookDelta(
                item.id,
                last.action,
                last.order,
                last.flags | int(RecordFlag.F_LAST),
                last.sequence,
                last.ts_event,
                last.ts_init,
            )
            original_count = len(deltas)
            batch = books[token_id].apply(OrderBookDeltas(item.id, deltas), quote)
            record(
                position,
                message,
                **base,
                native_kind="OrderBookDeltas",
                initialized=token_id in initialized,
                observed_delta_count=original_count,
                output_delta_count=len(batch.deltas),
                carried_bbo=quote,
            )
            counts["OrderBookDeltas"] += 1
            counts["OrderBookDeltaRows"] += len(batch.deltas)
            if token_id not in initialized:
                counts["batches_before_initialization"] += 1
            yield batch
    audit["counts"] = dict(counts)
    audit["pruning"] = {token: dict(book.counts) for token, book in books.items()}
    audit["tokens_without_snapshot"] = sorted(set(by_token) - initialized)
    audit["tokens_without_actual_close"] = sorted(set(by_token) - closed)
