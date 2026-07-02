"""Nautilus-native bridge for Polymarket research data.

This module is intentionally *not* a backtest engine.  Its job is to convert the
repository-local Polymarket source-normalization model into NautilusTrader data
objects that can be passed to :class:`nautilus_trader.backtest.engine.BacktestEngine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.parsing import determine_trade_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from polymarket.models import L2ReplayStepV1
from polymarket.models import L2UpdateV1
from polymarket.models import PolymarketL2DatasetV1


NativePolymarketData = OrderBookDeltas | TradeTick


@dataclass(frozen=True, slots=True)
class NautilusConversionResultV1:
    """Converted Nautilus-native payload for one Polymarket outcome token."""

    instrument: BinaryOption
    data: tuple[NativePolymarketData, ...]
    skipped_updates: tuple[str, ...]
    tick_size_changes: tuple[tuple[str, str], ...]


def datetime_to_nanos(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return dt_to_unix_nanos(value)


def load_binary_option_from_config(
    config: Mapping[str, Any],
    *,
    dataset: PolymarketL2DatasetV1,
    selected_asset_id: str | None = None,
) -> BinaryOption:
    """Build a Nautilus ``BinaryOption`` from explicit config or dataset IDs."""

    if config.get("dict_path") is not None:
        path = Path(str(config["dict_path"]))
        if not path.is_absolute():
            path = Path.cwd() / path
        import json

        return BinaryOption.from_dict(json.loads(path.read_text(encoding="utf-8")))

    first_update = _first_update(dataset, selected_asset_id=selected_asset_id)
    condition_id = str(config.get("condition_id") or first_update.market)
    token_id = str(config.get("token_id") or selected_asset_id or first_update.asset_id)
    instrument_id = get_polymarket_instrument_id(condition_id, token_id)
    raw_symbol = Symbol(token_id)
    price_increment = Price.from_str(str(config.get("price_increment", "0.01")))
    size_increment = Quantity.from_str(str(config.get("size_increment", "0.000001")))
    now_ns = datetime_to_nanos(datetime.now(tz=UTC))
    expiration = config.get("expiration")
    expiration_ns = (
        datetime_to_nanos(datetime.fromisoformat(str(expiration).replace("Z", "+00:00")))
        if expiration
        else datetime_to_nanos(datetime.now(tz=UTC) + timedelta(days=3650))
    )

    return BinaryOption(
        instrument_id=instrument_id,
        raw_symbol=raw_symbol,
        outcome=str(config.get("outcome", "Yes")),
        description=str(config.get("description", dataset.metadata.dataset_id)),
        asset_class=AssetClass.ALTERNATIVE,
        currency=pUSD,
        price_precision=price_increment.precision,
        price_increment=price_increment,
        size_precision=size_increment.precision,
        size_increment=size_increment,
        activation_ns=int(config.get("activation_ns", 0)),
        expiration_ns=expiration_ns,
        max_quantity=None,
        min_quantity=None,
        maker_fee=Decimal(str(config.get("maker_fee", "0"))),
        taker_fee=Decimal(str(config.get("taker_fee", "0"))),
        ts_event=now_ns,
        ts_init=now_ns,
        info={
            "condition_id": condition_id,
            "token_id": token_id,
            "source": "polymarket.nautilus_native.load_binary_option_from_config",
        },
    )


def convert_dataset_to_nautilus(
    dataset: PolymarketL2DatasetV1,
    *,
    instrument: BinaryOption,
    selected_asset_id: str | None = None,
    fail_on_tick_size_change: bool = True,
) -> NautilusConversionResultV1:
    """Convert normalized Polymarket L2 data into Nautilus-native data objects."""

    data: list[NativePolymarketData] = []
    skipped: list[str] = []
    tick_size_changes: list[tuple[str, str]] = []
    selected_asset_id = selected_asset_id or _token_id_from_instrument(instrument.id)
    last_ts_init: int | None = None

    def next_ts_init(step: L2ReplayStepV1) -> int:
        nonlocal last_ts_init
        ts_init = datetime_to_nanos(step.timestamp_received)
        if last_ts_init is not None and ts_init <= last_ts_init:
            ts_init = last_ts_init + 1
        last_ts_init = ts_init
        return ts_init

    for step in dataset.steps:
        relevant = [
            update
            for update in step.updates
            if update.asset_id == selected_asset_id or selected_asset_id is None
        ]
        if not relevant:
            continue

        deltas = _step_to_order_book_deltas(
            step,
            relevant,
            instrument=instrument,
            ts_init=next_ts_init(step),
        )
        if deltas is not None:
            data.append(deltas)

        for update in relevant:
            if update.event_type == "trade":
                data.append(
                    _trade_to_tick(
                        step,
                        update,
                        instrument=instrument,
                        ts_init=next_ts_init(step),
                    ),
                )
            elif update.event_type == "tick_size_change":
                old_tick = str(update.old_tick_size)
                new_tick = str(update.new_tick_size)
                tick_size_changes.append((old_tick, new_tick))
                if fail_on_tick_size_change:
                    raise NotImplementedError(
                        "Nautilus-native historical replay for dynamic Polymarket "
                        "tick_size_change needs an instrument epoch model; refusing "
                        "to silently replay it with a future or stale tick size.",
                    )
                skipped.append(f"tick_size_change {old_tick}->{new_tick}")
            elif update.event_type not in {"book", "price_change"}:
                skipped.append(update.event_type)

    return NautilusConversionResultV1(
        instrument=instrument,
        data=tuple(data),
        skipped_updates=tuple(skipped),
        tick_size_changes=tuple(tick_size_changes),
    )


def _step_to_order_book_deltas(
    step: L2ReplayStepV1,
    updates: list[L2UpdateV1],
    *,
    instrument: BinaryOption,
    ts_init: int,
) -> OrderBookDeltas | None:
    deltas: list[OrderBookDelta] = []
    for update in updates:
        if update.event_type == "book":
            deltas.extend(_snapshot_to_deltas(step, update, instrument=instrument, ts_init=ts_init))
        elif update.event_type == "price_change":
            delta = _price_change_to_delta(step, update, instrument=instrument, ts_init=ts_init)
            if delta is not None:
                deltas.append(delta)

    if not deltas:
        return None

    last = deltas[-1]
    deltas[-1] = OrderBookDelta(
        instrument_id=last.instrument_id,
        action=last.action,
        order=last.order,
        flags=last.flags | RecordFlag.F_LAST,
        sequence=last.sequence,
        ts_event=last.ts_event,
        ts_init=last.ts_init,
    )
    return OrderBookDeltas(instrument.id, deltas)


def _snapshot_to_deltas(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    *,
    instrument: BinaryOption,
    ts_init: int,
) -> list[OrderBookDelta]:
    if not update.bids and not update.asks:
        return []

    ts_event = _ts_event(step)
    deltas = [
        OrderBookDelta(
            instrument_id=instrument.id,
            action=BookAction.CLEAR,
            order=None,
            flags=RecordFlag.F_SNAPSHOT,
            sequence=step.sequence,
            ts_event=ts_event,
            ts_init=ts_init,
        ),
    ]
    for level in update.bids:
        deltas.append(
            _level_delta(
                step,
                instrument=instrument,
                side=OrderSide.BUY,
                action=BookAction.ADD,
                price=level.price,
                size=level.size,
                flags=RecordFlag.F_SNAPSHOT,
                ts_init=ts_init,
            ),
        )
    for level in update.asks:
        deltas.append(
            _level_delta(
                step,
                instrument=instrument,
                side=OrderSide.SELL,
                action=BookAction.ADD,
                price=level.price,
                size=level.size,
                flags=RecordFlag.F_SNAPSHOT,
                ts_init=ts_init,
            ),
        )
    return deltas


def _price_change_to_delta(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    *,
    instrument: BinaryOption,
    ts_init: int,
) -> OrderBookDelta | None:
    if update.price is None or update.size is None:
        return None
    if update.side == "BUY":
        side = OrderSide.BUY
    elif update.side == "SELL":
        side = OrderSide.SELL
    else:
        raise ValueError(f"unsupported price_change side: {update.side!r}")
    action = BookAction.UPDATE if update.size > 0 else BookAction.DELETE
    return _level_delta(
        step,
        instrument=instrument,
        side=side,
        action=action,
        price=update.price,
        size=update.size,
        flags=0,
        ts_init=ts_init,
    )


def _level_delta(
    step: L2ReplayStepV1,
    *,
    instrument: BinaryOption,
    side: OrderSide,
    action: BookAction,
    price: Decimal,
    size: Decimal,
    flags: int,
    ts_init: int,
) -> OrderBookDelta:
    order = BookOrder(
        side=side,
        price=instrument.make_price(float(price)),
        size=instrument.make_qty(float(size)),
        order_id=0,
    )
    return OrderBookDelta(
        instrument_id=instrument.id,
        action=action,
        order=order,
        flags=flags,
        sequence=step.sequence,
        ts_event=_ts_event(step),
        ts_init=ts_init,
    )


def _trade_to_tick(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    *,
    instrument: BinaryOption,
    ts_init: int,
) -> TradeTick:
    if update.price is None or update.size is None:
        raise ValueError("trade update requires price and size")
    if update.side == "BUY":
        aggressor = AggressorSide.BUYER
        polymarket_side = PolymarketOrderSide.BUY
    elif update.side == "SELL":
        aggressor = AggressorSide.SELLER
        polymarket_side = PolymarketOrderSide.SELL
    else:
        raise ValueError(f"unsupported trade side: {update.side!r}")
    timestamp = str(int(_ts_event(step) / 1_000_000))
    trade_id = determine_trade_id(
        asset_id=update.asset_id,
        side=polymarket_side,
        price=str(update.price),
        size=str(update.size),
        timestamp=timestamp,
    )
    return TradeTick(
        instrument_id=instrument.id,
        price=instrument.make_price(float(update.price)),
        size=instrument.make_qty(float(update.size)),
        aggressor_side=aggressor,
        trade_id=trade_id,
        ts_event=_ts_event(step),
        ts_init=ts_init,
    )


def _ts_event(step: L2ReplayStepV1) -> int:
    return datetime_to_nanos(step.timestamp or step.timestamp_received)


def _first_update(dataset: PolymarketL2DatasetV1, *, selected_asset_id: str | None) -> L2UpdateV1:
    for step in dataset.steps:
        for update in step.updates:
            if selected_asset_id is None or update.asset_id == selected_asset_id:
                return update
    raise ValueError("dataset has no updates for selected asset")


def _token_id_from_instrument(instrument_id: InstrumentId) -> str:
    return get_polymarket_token_id(instrument_id)
