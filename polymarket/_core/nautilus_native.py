"""
Nautilus-native bridge for Polymarket research data.

This module is intentionally *not* a backtest engine.  Its job is to convert the
repository-local Polymarket source-normalization model into NautilusTrader data
objects that can be passed to :class:`nautilus_trader.backtest.engine.BacktestEngine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Literal
from typing import Mapping

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
from polymarket._core.tick_size import resolve_effective_tick_size
from polymarket.replay_contract import PMXT_REPLAY_CLOCK
from polymarket.replay_contract import RECEIVE_TIME_REPLAY_CLOCK
from polymarket.replay_contract import replay_timestamp


NativePolymarketData = OrderBookDeltas | TradeTick | InstrumentClose
SettlementModeV1 = Literal["official", "inferred", "open"]
ReplayClockV1 = Literal["timestamp_received", "pmxt_replay_timestamp"]

POLYMARKET_FINE_PRICE_INCREMENT = Decimal("0.001")
POLYMARKET_INITIAL_EFFECTIVE_TICK_SIZE = Decimal("0.01")
SETTLEMENT_INFERENCE_LOW = Decimal("0.01")
SETTLEMENT_INFERENCE_HIGH = Decimal("0.99")
OFFICIAL_SETTLEMENT_STATUSES = frozenset({"resolved", "settled", "closed", "final", "finalized"})


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
    mode: Literal["official", "inferred"]
    resolution_time_ns: int
    payout: Decimal
    source: str
    status: str | None
    reason: str
    evidence: tuple[tuple[str, str], ...] = ()


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
    settlement_mode: SettlementModeV1
    settlement_reason: str
    settlement_evidence: tuple[tuple[str, str], ...]
    ts_init_audit: dict[str, Any] = None  # type: ignore[assignment]


def datetime_to_nanos(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return dt_to_unix_nanos(value)


def load_binary_option_from_config(
    config: Mapping[str, Any],
    *,
    dataset: PolymarketL2DatasetV1,
    selected_asset_id: str | None = None,
    config_base_dir: Path | None = None,
) -> BinaryOption:
    """Build a Nautilus ``BinaryOption`` from explicit config or dataset IDs."""
    if config.get("dict_path") is not None:
        path = Path(str(config["dict_path"]))
        if not path.is_absolute():
            path = (config_base_dir or Path.cwd()) / path
        import json

        instrument = BinaryOption.from_dict(json.loads(path.read_text(encoding="utf-8")))
        _ensure_instrument_replay_precision(instrument, dataset=dataset)
        return instrument

    first_update = _first_update(dataset, selected_asset_id=selected_asset_id)
    condition_id = str(config.get("condition_id") or first_update.market)
    token_id = str(config.get("token_id") or selected_asset_id or first_update.asset_id)
    metadata = _find_market_metadata(dataset, condition_id=condition_id, token_id=token_id)
    instrument_id = get_polymarket_instrument_id(condition_id, token_id)
    raw_symbol = Symbol(token_id)
    raw_price_increment = config.get("price_increment")
    selected_price_increment = (
        POLYMARKET_FINE_PRICE_INCREMENT
        if raw_price_increment is None
        else Decimal(str(raw_price_increment))
    )
    if selected_price_increment > POLYMARKET_FINE_PRICE_INCREMENT:
        raise ValueError(
            "instrument.price_increment is too coarse for Polymarket v1 replay: "
            f"configured={selected_price_increment}, required={POLYMARKET_FINE_PRICE_INCREMENT}. "
            "Polymarket binary options use 0.001 as the static expression precision; "
            "effective order/data legality is enforced by the tick-size timeline.",
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
        outcome=str(config.get("outcome") or (metadata.outcome if metadata is not None else None) or "Yes"),
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
            "static_expression_price_increment": str(POLYMARKET_FINE_PRICE_INCREMENT),
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

    return Decimal(0), Decimal(0), "default_zero"


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
    _first_update(dataset, selected_condition_id=condition_id, selected_asset_id=token_id)
    if selected_price_increment > POLYMARKET_FINE_PRICE_INCREMENT:
        raise ValueError(
            "instrument.price_increment is too coarse for Polymarket v1 replay: "
            f"configured={selected_price_increment}, required={POLYMARKET_FINE_PRICE_INCREMENT}. "
            "Polymarket binary options use 0.001 as the static expression precision; "
            "effective order/data legality is enforced by the tick-size timeline.",
        )


def _initial_tick_size(
    dataset: PolymarketL2DatasetV1,
    *,
    condition_id: str,
    token_id: str,
) -> Decimal:
    return POLYMARKET_INITIAL_EFFECTIVE_TICK_SIZE


def convert_dataset_to_nautilus(
    dataset: PolymarketL2DatasetV1,
    *,
    instrument: BinaryOption,
    selected_asset_id: str | None = None,
    fail_on_tick_size_change: bool = False,
    replay_clock: ReplayClockV1 = RECEIVE_TIME_REPLAY_CLOCK,
) -> NautilusConversionResultV1:
    """
    Convert normalized Polymarket L2 data into Nautilus-native data objects.

    ``replay_clock`` selects the step clock used for ``ts_event``/``ts_init``:

    - ``timestamp_received`` (default): strict-capture receive-time replay.
      Source timestamps remain diagnostics/provenance and must not reorder
      replay or create a look-ahead path.
    - ``pmxt_replay_timestamp``: the shared PMXT research contract clock
      (source ``timestamp`` with ``timestamp_received`` fallback), matching the
      order the PMXT adapter sorted on and the clock factor research replays
      on.  Only the explicit pmxt_research runner mode selects this.
    """
    if replay_clock not in (RECEIVE_TIME_REPLAY_CLOCK, PMXT_REPLAY_CLOCK):
        raise ValueError(f"unsupported replay_clock: {replay_clock!r}")

    def step_event_ns(step: L2ReplayStepV1) -> int:
        if replay_clock == PMXT_REPLAY_CLOCK:
            return datetime_to_nanos(replay_timestamp(step))
        return datetime_to_nanos(step.timestamp_received)

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
    last_replay_clock_ns: int | None = None
    terminal_bid_levels: dict[Decimal, Decimal] = {}
    terminal_ask_levels: dict[Decimal, Decimal] = {}
    terminal_last_trade: Decimal | None = None

    ts_init_adjusted_count = 0
    max_ts_init_offset_ns = 0

    def audited_monotonic_ts(candidate: int) -> int:
        """Audited allocator: every synthetic +1ns serialization is counted."""
        nonlocal ts_init_adjusted_count, max_ts_init_offset_ns
        adjusted = _next_monotonic_ts_init(candidate, last_ts_init)
        if adjusted != candidate:
            ts_init_adjusted_count += 1
            max_ts_init_offset_ns = max(max_ts_init_offset_ns, adjusted - candidate)
        return adjusted

    def next_ts_init(step: L2ReplayStepV1) -> int:
        nonlocal last_ts_init, last_replay_clock_ns
        clock_ns = step_event_ns(step)
        ts_init = audited_monotonic_ts(clock_ns)
        last_ts_init = ts_init
        last_replay_clock_ns = clock_ns
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
            ts_event=step_event_ns(step),
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
                terminal_last_trade = _update_terminal_market_state(
                    update,
                    bid_levels=terminal_bid_levels,
                    ask_levels=terminal_ask_levels,
                    last_trade=terminal_last_trade,
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
                        ts_event=step_event_ns(step),
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
                if update.new_tick_size != POLYMARKET_FINE_PRICE_INCREMENT:
                    raise ValueError(
                        "tick_size_change new_tick_size does not match Polymarket v1 "
                        f"fine precision at sequence={step.sequence}, asset_id={update.asset_id!r}: "
                        f"expected={POLYMARKET_FINE_PRICE_INCREMENT}, new_tick_size={update.new_tick_size}",
                    )
                effective_tick_size_changes.append(
                    EffectiveTickSizeChangeV1(
                        sequence=step.sequence,
                        effective_from_ts_init=audited_monotonic_ts(step_event_ns(step)),
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

    settlement, settlement_mode, settlement_reason, settlement_evidence = _selected_settlement_metadata(
        dataset,
        condition_id=selected_condition_id,
        token_id=selected_asset_id,
        bid_levels=terminal_bid_levels,
        ask_levels=terminal_ask_levels,
        last_trade=terminal_last_trade,
        last_ts_init=last_ts_init,
    )
    if settlement is not None:
        # Compare against the real replay clock, not the synthetic ts_init: the
        # +1ns serialization of tied events must not make a settlement at the
        # final replay timestamp look like it happened "before" the data.
        if last_replay_clock_ns is not None and settlement.resolution_time_ns < last_replay_clock_ns:
            raise ValueError(
                "resolved Polymarket token settlement time is before the last replay "
                "clock timestamp for the selected token: "
                f"resolution_time_ns={settlement.resolution_time_ns}, "
                f"last_replay_clock_ns={last_replay_clock_ns}",
            )
        data.append(
            InstrumentClose(
                instrument_id=instrument.id,
                close_price=instrument.make_price(float(settlement.payout)),
                close_type=InstrumentCloseType.CONTRACT_EXPIRED,
                ts_event=settlement.resolution_time_ns,
                ts_init=audited_monotonic_ts(settlement.resolution_time_ns),
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
        settlement_mode=settlement_mode,
        settlement_reason=settlement_reason,
        settlement_evidence=settlement_evidence,
        ts_init_audit={
            "ts_init_policy": (
                "synthetic_monotonic_source_time"
                if replay_clock == PMXT_REPLAY_CLOCK
                else "synthetic_monotonic_receive_time"
            ),
            "replay_clock": replay_clock,
            "adjusted_event_count": ts_init_adjusted_count,
            "max_synthetic_offset_ns": max_ts_init_offset_ns,
            "note": (
                "Events sharing a replay-clock timestamp are serialized by +1ns "
                "increments so Nautilus ts_init stays strictly increasing; the "
                "strategy observes these events sequentially even though the "
                "source clock cannot distinguish their order."
            ),
        },
    )


def _step_to_order_book_deltas(
    step: L2ReplayStepV1,
    updates: list[L2UpdateV1],
    *,
    instrument: BinaryOption,
    ts_event: int,
    ts_init: int,
) -> OrderBookDeltas | None:
    deltas: list[OrderBookDelta] = []
    for update in updates:
        if update.event_type == "book":
            deltas.extend(
                _snapshot_to_deltas(step, update, instrument=instrument, ts_event=ts_event, ts_init=ts_init),
            )
        elif update.event_type == "price_change":
            delta = _price_change_to_delta(step, update, instrument=instrument, ts_event=ts_event, ts_init=ts_init)
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
    ts_event: int,
    ts_init: int,
) -> list[OrderBookDelta]:
    if not update.bids and not update.asks:
        return []

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
                ts_event=ts_event,
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
                ts_event=ts_event,
                ts_init=ts_init,
            ),
        )
    return deltas


def _price_change_to_delta(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    *,
    instrument: BinaryOption,
    ts_event: int,
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
        ts_event=ts_event,
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
    ts_event: int,
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
        ts_event=ts_event,
        ts_init=ts_init,
    )


def _trade_to_tick(
    step: L2ReplayStepV1,
    update: L2UpdateV1,
    *,
    instrument: BinaryOption,
    ts_event: int,
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
    timestamp = str(int(ts_event / 1_000_000))
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
        ts_event=ts_event,
        ts_init=ts_init,
    )


def _next_monotonic_ts_init(candidate: int, previous: int | None) -> int:
    if previous is not None and candidate <= previous:
        return previous + 1
    return candidate


def _update_terminal_market_state(
    update: L2UpdateV1,
    *,
    bid_levels: dict[Decimal, Decimal],
    ask_levels: dict[Decimal, Decimal],
    last_trade: Decimal | None,
) -> Decimal | None:
    if update.event_type == "book":
        bid_levels.clear()
        ask_levels.clear()
        bid_levels.update({level.price: level.size for level in update.bids if level.size > 0})
        ask_levels.update({level.price: level.size for level in update.asks if level.size > 0})
        return last_trade
    if update.event_type == "price_change" and update.price is not None and update.size is not None:
        levels = bid_levels if update.side == "BUY" else ask_levels if update.side == "SELL" else None
        if levels is not None:
            if update.size > 0:
                levels[update.price] = update.size
            else:
                levels.pop(update.price, None)
        return last_trade
    if update.event_type == "trade" and update.price is not None:
        return update.price
    return last_trade


def _selected_settlement_metadata(
    dataset: PolymarketL2DatasetV1,
    *,
    condition_id: str,
    token_id: str,
    bid_levels: Mapping[Decimal, Decimal],
    ask_levels: Mapping[Decimal, Decimal],
    last_trade: Decimal | None,
    last_ts_init: int | None,
) -> tuple[SettlementMetadataV1 | None, SettlementModeV1, str, tuple[tuple[str, str], ...]]:
    metadata = _find_market_metadata(dataset, condition_id=condition_id, token_id=token_id)
    official = _official_settlement_metadata(metadata, condition_id=condition_id, token_id=token_id)
    if official is not None:
        return official, "official", official.reason, official.evidence
    inferred = _infer_terminal_settlement_metadata(
        condition_id=condition_id,
        token_id=token_id,
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        last_trade=last_trade,
        last_ts_init=last_ts_init,
    )
    if inferred is not None:
        return inferred, "inferred", inferred.reason, inferred.evidence
    evidence = _terminal_market_evidence(bid_levels=bid_levels, ask_levels=ask_levels, last_trade=last_trade)
    return (
        None,
        "open",
        "no official settlement metadata and terminal market data did not clearly converge to 0 or 1",
        tuple(evidence.items()),
    )


def _official_settlement_metadata(
    metadata: MarketMetadataV1 | None,
    *,
    condition_id: str,
    token_id: str,
) -> SettlementMetadataV1 | None:
    if metadata is None:
        return None
    status = metadata.resolution_status.strip().lower() if metadata.resolution_status is not None else None
    has_official_signal = (
        metadata.token_payout is not None
        or metadata.winner is not None
        or metadata.resolution_time is not None
        or status in OFFICIAL_SETTLEMENT_STATUSES
    )
    if not has_official_signal:
        return None
    payout = metadata.token_payout
    if payout is None and metadata.winner is not None:
        payout = Decimal(1) if metadata.winner else Decimal(0)
    if payout is None:
        raise ValueError(
            "official/resolved Polymarket token metadata requires token_payout or winner "
            f"for condition_id={condition_id!r}, token_id={token_id!r}",
        )
    if metadata.resolution_time is None:
        raise ValueError(
            "official/resolved Polymarket token metadata requires resolution_time "
            f"for condition_id={condition_id!r}, token_id={token_id!r}",
        )
    if payout < 0 or payout > 1:
        raise ValueError(
            "resolved Polymarket token payout must be in [0, 1]: "
            f"condition_id={condition_id!r}, token_id={token_id!r}, "
            f"payout={payout}",
        )
    if metadata.winner is not None and payout in {Decimal(0), Decimal(1)}:
        expected = Decimal(1) if metadata.winner else Decimal(0)
        if payout != expected:
            raise ValueError(
                "resolved Polymarket token winner and payout disagree: "
                f"condition_id={condition_id!r}, token_id={token_id!r}, "
                f"winner={metadata.winner}, payout={payout}",
            )
    return SettlementMetadataV1(
        condition_id=condition_id,
        token_id=token_id,
        mode="official",
        resolution_time_ns=datetime_to_nanos(metadata.resolution_time),
        payout=payout,
        source=metadata.resolution_source,
        status=metadata.resolution_status,
        reason="official resolution metadata supplied payout",
        evidence=(
            ("resolution_status", str(metadata.resolution_status)),
            ("resolution_source", metadata.resolution_source),
            ("winner", str(metadata.winner)),
            ("payout", str(payout)),
        ),
    )


def _infer_terminal_settlement_metadata(
    *,
    condition_id: str,
    token_id: str,
    bid_levels: Mapping[Decimal, Decimal],
    ask_levels: Mapping[Decimal, Decimal],
    last_trade: Decimal | None,
    last_ts_init: int | None,
) -> SettlementMetadataV1 | None:
    if last_ts_init is None:
        return None
    evidence = _terminal_market_evidence(
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        last_trade=last_trade,
    )
    raw_mark = evidence.get("terminal_mark")
    if raw_mark is None:
        return None
    mark = Decimal(raw_mark)
    if mark >= SETTLEMENT_INFERENCE_HIGH:
        payout = Decimal(1)
        reason = (
            f"no official settlement metadata; inferred payout=1 because terminal_mark={mark} "
            f">= {SETTLEMENT_INFERENCE_HIGH}"
        )
    elif mark <= SETTLEMENT_INFERENCE_LOW:
        payout = Decimal(0)
        reason = (
            f"no official settlement metadata; inferred payout=0 because terminal_mark={mark} "
            f"<= {SETTLEMENT_INFERENCE_LOW}"
        )
    else:
        return None
    return SettlementMetadataV1(
        condition_id=condition_id,
        token_id=token_id,
        mode="inferred",
        resolution_time_ns=last_ts_init + 1,
        payout=payout,
        source="terminal_market_price_inference",
        status="inferred",
        reason=reason,
        evidence=tuple(evidence.items()),
    )


def _terminal_market_evidence(
    *,
    bid_levels: Mapping[Decimal, Decimal],
    ask_levels: Mapping[Decimal, Decimal],
    last_trade: Decimal | None,
) -> dict[str, str]:
    best_bid = max(bid_levels) if bid_levels else None
    best_ask = min(ask_levels) if ask_levels else None
    evidence: dict[str, str] = {
        "inference_low_threshold": str(SETTLEMENT_INFERENCE_LOW),
        "inference_high_threshold": str(SETTLEMENT_INFERENCE_HIGH),
    }
    if best_bid is not None:
        evidence["terminal_best_bid"] = str(best_bid)
    if best_ask is not None:
        evidence["terminal_best_ask"] = str(best_ask)
    if last_trade is not None:
        evidence["terminal_last_trade"] = str(last_trade)
    if best_bid is not None and best_ask is not None:
        evidence["terminal_mark_source"] = "bbo_mid"
        evidence["terminal_mark"] = str((best_bid + best_ask) / Decimal(2))
    elif last_trade is not None:
        evidence["terminal_mark_source"] = "last_trade"
        evidence["terminal_mark"] = str(last_trade)
    elif best_bid is not None:
        evidence["terminal_mark_source"] = "best_bid"
        evidence["terminal_mark"] = str(best_bid)
    elif best_ask is not None:
        evidence["terminal_mark_source"] = "best_ask"
        evidence["terminal_mark"] = str(best_ask)
    return evidence


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
    """
    Install a Polymarket v1 submit-time price guard on one strategy instance.

    The Nautilus instrument uses the finest price increment required for replay
    precision, but Polymarket's effective minimum tick can be coarser earlier in
    the market life.  This guard rejects strategy order prices that would have
    been illegal at the strategy-visible clock time.

    The guard is deliberately installed on the strategy instance, not Nautilus
    core classes, so the contract is local to Polymarket research runs.  The
    patched methods are ``submit_order``, ``submit_order_list``, and
    ``modify_order``; regression tests cover all three paths.
    """
    original_submit_order = strategy.submit_order
    original_submit_order_list = strategy.submit_order_list
    original_modify_order = strategy.modify_order
    ordered_changes = tuple(sorted(changes, key=lambda item: item.effective_from_ts_init))

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
        effective_tick = resolve_effective_tick_size(
            initial_tick_size=initial_tick_size,
            changes=ordered_changes,
            ts_ns=ts_now,
        )
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

