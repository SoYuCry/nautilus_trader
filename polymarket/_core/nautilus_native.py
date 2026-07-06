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
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_condition_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import InstrumentCloseType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from polymarket._core.models import L2ReplayStepV1
from polymarket._core.models import L2UpdateV1
from polymarket._core.models import MarketMetadataV1
from polymarket._core.models import PolymarketL2DatasetV1


NativePolymarketData = OrderBookDeltas | TradeTick | InstrumentClose


@dataclass(frozen=True, slots=True)
class EffectiveTickSizeChangeV1:
    """Effective Polymarket tick-size change for one selected outcome token."""

    sequence: int
    effective_from_ts_init: int
    old_tick_size: Decimal
    new_tick_size: Decimal


@dataclass(frozen=True, slots=True)
class SettlementMetadataV1:
    """Resolved token payout mapped to Nautilus settlement-price inputs."""

    condition_id: str
    token_id: str
    resolution_time_ns: int
    payout: Decimal
    source: str
    status: str | None


@dataclass(frozen=True, slots=True)
class NautilusConversionResultV1:
    """Converted Nautilus-native payload for one Polymarket outcome token."""

    instrument: BinaryOption
    data: tuple[NativePolymarketData, ...]
    skipped_updates: tuple[str, ...]
    tick_size_changes: tuple[tuple[str, str], ...]
    initial_tick_size: Decimal
    effective_tick_size_changes: tuple[EffectiveTickSizeChangeV1, ...]
    settlement: SettlementMetadataV1 | None


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

        instrument = BinaryOption.from_dict(json.loads(path.read_text(encoding="utf-8")))
        _ensure_instrument_replay_precision(instrument, dataset=dataset)
        return instrument

    first_update = _first_update(dataset, selected_asset_id=selected_asset_id)
    condition_id = str(config.get("condition_id") or first_update.market)
    token_id = str(config.get("token_id") or selected_asset_id or first_update.asset_id)
    instrument_id = get_polymarket_instrument_id(condition_id, token_id)
    raw_symbol = Symbol(token_id)
    finest_price_increment = _finest_price_increment(
        dataset,
        condition_id=condition_id,
        token_id=token_id,
    )
    raw_price_increment = config.get("price_increment")
    selected_price_increment = (
        finest_price_increment
        if raw_price_increment is None
        else Decimal(str(raw_price_increment))
    )
    if selected_price_increment > finest_price_increment:
        raise ValueError(
            "instrument.price_increment is too coarse for Polymarket replay data: "
            f"configured={selected_price_increment}, finest_required={finest_price_increment}. "
            "Use the finest replay precision and rely on the effective tick-size guard "
            "for strategy order legality.",
        )
    price_increment = Price.from_str(str(selected_price_increment))
    size_increment = Quantity.from_str(str(config.get("size_increment", "0.000001")))
    now_ns = datetime_to_nanos(datetime.now(tz=UTC))
    expiration = config.get("expiration")
    expiration_ns = (
        datetime_to_nanos(datetime.fromisoformat(str(expiration).replace("Z", "+00:00")))
        if expiration
        else datetime_to_nanos(datetime.now(tz=UTC) + timedelta(days=3650))
    )
    maker_fee, taker_fee, fee_source = _resolve_fee_fields(
        config,
        dataset=dataset,
        condition_id=condition_id,
        token_id=token_id,
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
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        ts_event=now_ns,
        ts_init=now_ns,
        info={
            "condition_id": condition_id,
            "token_id": token_id,
            "fee_source": fee_source,
            "effective_initial_tick_size": str(
                _initial_tick_size(dataset, condition_id=condition_id, token_id=token_id),
            ),
            "source": "polymarket._core.nautilus_native.load_binary_option_from_config",
        },
    )


def _resolve_fee_fields(
    config: Mapping[str, Any],
    *,
    dataset: PolymarketL2DatasetV1,
    condition_id: str,
    token_id: str,
) -> tuple[Decimal, Decimal, str]:
    if config.get("maker_fee") is not None or config.get("taker_fee") is not None:
        return (
            Decimal(str(config.get("maker_fee", "0"))),
            Decimal(str(config.get("taker_fee", "0"))),
            str(config.get("fee_source") or "instrument_config"),
        )

    metadata = _find_market_metadata(dataset, condition_id=condition_id, token_id=token_id)
    if metadata is not None and metadata.taker_fee is not None:
        return metadata.maker_fee, metadata.taker_fee, metadata.fee_source

    return Decimal("0"), Decimal("0"), "default_zero"


def _find_market_metadata(
    dataset: PolymarketL2DatasetV1,
    *,
    condition_id: str,
    token_id: str,
) -> MarketMetadataV1 | None:
    condition_matches = [
        item for item in dataset.metadata.market_metadata if item.condition_id == condition_id
    ]
    for item in condition_matches:
        if item.token_id == token_id:
            return item
    for item in condition_matches:
        if item.token_id is None:
            return item
    return None


def _ensure_instrument_replay_precision(
    instrument: BinaryOption,
    *,
    dataset: PolymarketL2DatasetV1,
) -> None:
    condition_id = get_polymarket_condition_id(instrument.id)
    token_id = _token_id_from_instrument(instrument.id)
    selected_price_increment = _decimal_from_price_like(instrument.price_increment)
    finest_price_increment = _finest_price_increment(
        dataset,
        condition_id=condition_id,
        token_id=token_id,
    )
    if selected_price_increment > finest_price_increment:
        raise ValueError(
            "instrument.price_increment is too coarse for Polymarket replay data: "
            f"configured={selected_price_increment}, finest_required={finest_price_increment}. "
            "Use the finest replay precision and rely on the effective tick-size guard "
            "for strategy order legality.",
        )


def _initial_tick_size(
    dataset: PolymarketL2DatasetV1,
    *,
    condition_id: str,
    token_id: str,
) -> Decimal:
    metadata = _find_market_metadata(dataset, condition_id=condition_id, token_id=token_id)
    if metadata is not None and metadata.minimum_tick_size is not None:
        return metadata.minimum_tick_size
    return Decimal("0.01")


def _finest_price_increment(
    dataset: PolymarketL2DatasetV1,
    *,
    condition_id: str,
    token_id: str,
) -> Decimal:
    increments = {_initial_tick_size(dataset, condition_id=condition_id, token_id=token_id)}
    for step in dataset.steps:
        for update in step.updates:
            if update.market != condition_id or update.asset_id != token_id:
                continue
            if update.event_type != "tick_size_change":
                continue
            if update.old_tick_size is not None:
                increments.add(update.old_tick_size)
            if update.new_tick_size is not None:
                increments.add(update.new_tick_size)
    return min(increments)


def convert_dataset_to_nautilus(
    dataset: PolymarketL2DatasetV1,
    *,
    instrument: BinaryOption,
    selected_asset_id: str | None = None,
    fail_on_tick_size_change: bool = False,
) -> NautilusConversionResultV1:
    """Convert normalized Polymarket L2 data into Nautilus-native data objects."""

    data: list[NativePolymarketData] = []
    skipped: list[str] = []
    tick_size_changes: list[tuple[str, str]] = []
    effective_tick_size_changes: list[EffectiveTickSizeChangeV1] = []
    selected_condition_id, selected_asset_id = _resolve_selection(
        dataset,
        instrument=instrument,
        selected_asset_id=selected_asset_id,
    )
    current_tick_size = _initial_tick_size(
        dataset,
        condition_id=selected_condition_id,
        token_id=selected_asset_id,
    )
    last_ts_init: int | None = None

    def next_ts_init(step: L2ReplayStepV1) -> int:
        nonlocal last_ts_init
        ts_init = datetime_to_nanos(step.timestamp_received)
        if last_ts_init is not None and ts_init <= last_ts_init:
            ts_init = last_ts_init + 1
        last_ts_init = ts_init
        return ts_init

    def append_book_updates(
        step: L2ReplayStepV1,
        book_updates: list[L2UpdateV1],
    ) -> None:
        if not book_updates:
            return
        deltas = _step_to_order_book_deltas(
            step,
            book_updates,
            instrument=instrument,
            ts_init=next_ts_init(step),
        )
        if deltas is not None:
            data.append(deltas)
        book_updates.clear()

    for step in dataset.steps:
        relevant = [
            update
            for update in step.updates
            if update.market == selected_condition_id and update.asset_id == selected_asset_id
        ]
        if not relevant:
            continue

        pending_book_updates: list[L2UpdateV1] = []
        for update in relevant:
            if update.event_type in {"book", "price_change", "trade"}:
                _validate_update_prices_on_effective_tick(
                    step,
                    update,
                    effective_tick_size=current_tick_size,
                )
            if update.event_type in {"book", "price_change"}:
                pending_book_updates.append(update)
            elif update.event_type == "trade":
                append_book_updates(step, pending_book_updates)
                data.append(
                    _trade_to_tick(
                        step,
                        update,
                        instrument=instrument,
                        ts_init=next_ts_init(step),
                    ),
                )
            elif update.event_type == "tick_size_change":
                append_book_updates(step, pending_book_updates)
                if fail_on_tick_size_change:
                    raise NotImplementedError(
                        "tick_size_change replay support was disabled by "
                        "replay.fail_on_tick_size_change=true",
                    )
                if update.old_tick_size is None or update.new_tick_size is None:
                    raise ValueError(
                        "tick_size_change update requires old_tick_size and new_tick_size "
                        f"at sequence={step.sequence}, asset_id={update.asset_id!r}",
                    )
                old_tick = str(update.old_tick_size)
                new_tick = str(update.new_tick_size)
                tick_size_changes.append((old_tick, new_tick))
                if update.old_tick_size != current_tick_size:
                    raise ValueError(
                        "tick_size_change old_tick_size does not match current effective "
                        f"tick size at sequence={step.sequence}, asset_id={update.asset_id!r}: "
                        f"current={current_tick_size}, old_tick_size={update.old_tick_size}",
                    )
                effective_tick_size_changes.append(
                    EffectiveTickSizeChangeV1(
                        sequence=step.sequence,
                        effective_from_ts_init=_next_monotonic_ts_init(
                            _ts_event(step),
                            last_ts_init,
                        ),
                        old_tick_size=update.old_tick_size,
                        new_tick_size=update.new_tick_size,
                    ),
                )
                current_tick_size = update.new_tick_size
                skipped.append(f"tick_size_change {old_tick}->{new_tick} (timeline_applied)")
            elif update.event_type not in {"book", "price_change"}:
                append_book_updates(step, pending_book_updates)
                skipped.append(update.event_type)
        append_book_updates(step, pending_book_updates)

    settlement = _selected_settlement_metadata(
        dataset,
        condition_id=selected_condition_id,
        token_id=selected_asset_id,
    )
    if settlement is not None:
        if last_ts_init is not None and settlement.resolution_time_ns < last_ts_init:
            raise ValueError(
                "resolved Polymarket token settlement time is before the last replay "
                "timestamp_received for the selected token: "
                f"resolution_time_ns={settlement.resolution_time_ns}, last_replay_ts_init={last_ts_init}",
            )
        data.append(
            InstrumentClose(
                instrument_id=instrument.id,
                close_price=instrument.make_price(float(settlement.payout)),
                close_type=InstrumentCloseType.CONTRACT_EXPIRED,
                ts_event=settlement.resolution_time_ns,
                ts_init=_next_monotonic_ts_init(settlement.resolution_time_ns, last_ts_init),
            ),
        )

    return NautilusConversionResultV1(
        instrument=instrument,
        data=tuple(data),
        skipped_updates=tuple(skipped),
        tick_size_changes=tuple(tick_size_changes),
        initial_tick_size=_initial_tick_size(
            dataset,
            condition_id=selected_condition_id,
            token_id=selected_asset_id,
        ),
        effective_tick_size_changes=tuple(effective_tick_size_changes),
        settlement=settlement,
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
) -> OrderBookDelta:
    if update.price is None or update.size is None:
        missing = [
            field
            for field, value in (("price", update.price), ("size", update.size))
            if value is None
        ]
        raise ValueError(
            "price_change update is missing required field(s) "
            f"{', '.join(missing)} at sequence={step.sequence}, asset_id={update.asset_id!r}",
        )
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


def _validate_update_prices_on_effective_tick(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    *,
    effective_tick_size: Decimal,
) -> None:
    prices: list[Decimal] = []
    if update.price is not None:
        prices.append(update.price)
    prices.extend(level.price for level in update.bids)
    prices.extend(level.price for level in update.asks)
    for price in prices:
        _validate_price_on_tick(
            price,
            tick_size=effective_tick_size,
            sequence=step.sequence,
            asset_id=update.asset_id,
        )


def _validate_price_on_tick(
    price: Decimal,
    *,
    tick_size: Decimal,
    sequence: int,
    asset_id: str,
) -> None:
    if tick_size <= 0:
        raise ValueError(f"effective tick size must be positive, got {tick_size}")
    if price < 0 or price > 1:
        raise ValueError(
            f"Polymarket price must be in [0, 1] at sequence={sequence}, "
            f"asset_id={asset_id!r}, price={price}",
        )
    quotient = price / tick_size
    if quotient != quotient.to_integral_value():
        raise ValueError(
            "price violates effective tick size: "
            f"sequence={sequence}, asset_id={asset_id!r}, price={price}, "
            f"effective_tick_size={tick_size}",
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
    # Historical replay is receive-time ordered.  Source timestamps remain
    # diagnostics/provenance in data_health.json; they must not reorder replay
    # or create a look-ahead path when they are future-stamped or inverted.
    return datetime_to_nanos(step.timestamp_received)


def _next_monotonic_ts_init(candidate: int, previous: int | None) -> int:
    if previous is not None and candidate <= previous:
        return previous + 1
    return candidate


def _selected_settlement_metadata(
    dataset: PolymarketL2DatasetV1,
    *,
    condition_id: str,
    token_id: str,
) -> SettlementMetadataV1 | None:
    metadata = _find_market_metadata(dataset, condition_id=condition_id, token_id=token_id)
    if metadata is None or metadata.token_payout is None:
        return None
    if metadata.resolution_time is None:
        raise ValueError(
            "resolved Polymarket token metadata requires resolution_time "
            f"for condition_id={condition_id!r}, token_id={token_id!r}",
        )
    if metadata.token_payout < 0 or metadata.token_payout > 1:
        raise ValueError(
            "resolved Polymarket token payout must be in [0, 1]: "
            f"condition_id={condition_id!r}, token_id={token_id!r}, "
            f"payout={metadata.token_payout}",
        )
    return SettlementMetadataV1(
        condition_id=condition_id,
        token_id=token_id,
        resolution_time_ns=datetime_to_nanos(metadata.resolution_time),
        payout=metadata.token_payout,
        source=metadata.resolution_source,
        status=metadata.resolution_status,
    )


def _first_update(
    dataset: PolymarketL2DatasetV1,
    *,
    selected_asset_id: str | None,
    selected_condition_id: str | None = None,
) -> L2UpdateV1:
    for step in dataset.steps:
        for update in step.updates:
            if selected_condition_id is not None and update.market != selected_condition_id:
                continue
            if selected_asset_id is None or update.asset_id == selected_asset_id:
                return update
    if selected_condition_id is None:
        raise ValueError(f"dataset has no updates for selected asset: asset_id={selected_asset_id!r}")
    raise ValueError(
        "dataset has no updates for selected Polymarket instrument: "
        f"condition_id={selected_condition_id!r}, asset_id={selected_asset_id!r}",
    )


def _resolve_selection(
    dataset: PolymarketL2DatasetV1,
    *,
    instrument: BinaryOption,
    selected_asset_id: str | None,
) -> tuple[str, str]:
    instrument_condition_id = get_polymarket_condition_id(instrument.id)
    instrument_token_id = _token_id_from_instrument(instrument.id)
    resolved_asset_id = selected_asset_id or instrument_token_id
    if selected_asset_id is not None and selected_asset_id != instrument_token_id:
        raise ValueError(
            "selected_asset_id does not match Nautilus instrument token_id: "
            f"selected_asset_id={selected_asset_id!r}, instrument_token_id={instrument_token_id!r}",
        )
    _first_update(dataset, selected_condition_id=instrument_condition_id, selected_asset_id=resolved_asset_id)
    return instrument_condition_id, resolved_asset_id


def _token_id_from_instrument(instrument_id: InstrumentId) -> str:
    return get_polymarket_token_id(instrument_id)


def install_effective_tick_size_order_guard(
    strategy: Any,
    *,
    initial_tick_size: Decimal,
    changes: tuple[EffectiveTickSizeChangeV1, ...],
) -> dict[str, Any]:
    """Install a Polymarket v1 submit-time price guard on one strategy instance.

    The Nautilus instrument uses the finest price increment required for replay
    precision, but Polymarket's effective minimum tick can be coarser earlier in
    the market life.  This guard rejects strategy order prices that would have
    been illegal at the strategy-visible clock time.
    """

    original_submit_order = strategy.submit_order
    original_submit_order_list = strategy.submit_order_list
    original_modify_order = strategy.modify_order
    ordered_changes = tuple(sorted(changes, key=lambda item: item.effective_from_ts_init))

    def tick_at(ts_now: int) -> Decimal:
        tick = initial_tick_size
        for change in ordered_changes:
            if ts_now >= change.effective_from_ts_init:
                tick = change.new_tick_size
            else:
                break
        return tick

    def validate_order(order: Any) -> None:
        validate_prices(
            (
                getattr(order, "price", None),
                getattr(order, "trigger_price", None),
            ),
            asset_id=str(getattr(order, "instrument_id", "")),
        )

    def validate_prices(prices: tuple[Any, ...], *, asset_id: str) -> None:
        ts_now = int(strategy.clock.timestamp_ns())
        effective_tick = tick_at(ts_now)
        for price in prices:
            if price is None:
                continue
            _validate_price_on_tick(
                _decimal_from_price_like(price),
                tick_size=effective_tick,
                sequence=-1,
                asset_id=asset_id,
            )

    def guarded_submit_order(order: Any, *args: Any, **kwargs: Any) -> Any:
        validate_order(order)
        return original_submit_order(order, *args, **kwargs)

    def guarded_submit_order_list(order_list: Any, *args: Any, **kwargs: Any) -> Any:
        for order in _iter_order_list_items(order_list):
            validate_order(order)
        return original_submit_order_list(order_list, *args, **kwargs)

    def guarded_modify_order(order: Any, *args: Any, **kwargs: Any) -> Any:
        price = kwargs.get("price")
        trigger_price = kwargs.get("trigger_price")
        if len(args) >= 2 and price is None:
            price = args[1]
        if len(args) >= 3 and trigger_price is None:
            trigger_price = args[2]
        validate_prices(
            (price, trigger_price),
            asset_id=str(getattr(order, "instrument_id", "")),
        )
        return original_modify_order(order, *args, **kwargs)

    strategy.submit_order = guarded_submit_order
    strategy.submit_order_list = guarded_submit_order_list
    strategy.modify_order = guarded_modify_order
    return {
        "enabled": True,
        "guarded_methods": ["submit_order", "submit_order_list", "modify_order"],
        "initial_tick_size": str(initial_tick_size),
        "changes": [
            {
                "sequence": change.sequence,
                "effective_from_ts_init": change.effective_from_ts_init,
                "old_tick_size": str(change.old_tick_size),
                "new_tick_size": str(change.new_tick_size),
            }
            for change in ordered_changes
        ],
    }


def _iter_order_list_items(order_list: Any) -> tuple[Any, ...]:
    orders = getattr(order_list, "orders", None)
    if orders is not None:
        return tuple(orders)
    try:
        return tuple(order_list)
    except TypeError:
        return (order_list,)


def _decimal_from_price_like(value: Any) -> Decimal:
    if hasattr(value, "as_decimal"):
        return Decimal(str(value.as_decimal()))
    text = str(value).split()[0]
    return Decimal(text)

