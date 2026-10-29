"""Observed-high signals with bounded No taker and fixed-tail maker intents.

This module has no network or automatic startup behavior. Callbacks must run on
one runner thread. Source normalization and live reconciliation are external.
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_DOWN


D = Decimal


def _finite(*values: Decimal) -> None:
    if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
        raise ValueError("Expected finite Decimal values")


@dataclass(frozen=True)
class WeatherObservation:
    event_id: str
    rule_id: str
    day: date
    maximum: Decimal
    received_ns: int
    revision: int
    confirmed: bool = True


@dataclass(frozen=True)
class TemperatureMarket:
    condition_id: str
    no_instrument_id: str
    upper_bound: Decimal | None
    tick_size: Decimal
    minimum_quantity: Decimal
    fee_rate: Decimal

    def __post_init__(self):
        _finite(self.tick_size, self.minimum_quantity, self.fee_rate)
        if self.upper_bound is not None:
            _finite(self.upper_bound)
        if not 0 < self.tick_size < 1 or self.minimum_quantity <= 0:
            raise ValueError("Invalid market increments")
        if not 0 <= self.fee_rate <= 1:
            raise ValueError("Invalid fee rate")


@dataclass(frozen=True)
class TakerLimits:
    max_event_cost: Decimal
    max_condition_quantity: Decimal
    max_order_quantity: Decimal
    minimum_edge: Decimal = D("0")
    safety_margin: Decimal = D("0")
    max_book_age_ns: int = 5_000_000_000
    max_weather_age_ns: int = 300_000_000_000

    def __post_init__(self):
        _finite(
            self.max_event_cost,
            self.max_condition_quantity,
            self.max_order_quantity,
            self.minimum_edge,
            self.safety_margin,
        )
        if (
            min(
                self.max_event_cost,
                self.max_condition_quantity,
                self.max_order_quantity,
            )
            <= 0
        ):
            raise ValueError("Positive risk limits required")
        if min(self.minimum_edge, self.safety_margin) < 0:
            raise ValueError("Negative edge or margin")
        if min(self.max_book_age_ns, self.max_weather_age_ns) <= 0:
            raise ValueError("Positive freshness limits required")


@dataclass(frozen=True)
class NoAsk:
    condition_id: str
    price: Decimal
    quantity: Decimal
    received_ns: int
    sequence: int
    ready: bool = True
    accepting_orders: bool = True


@dataclass(frozen=True)
class BuyNoIntent:
    key: str
    instrument_id: str
    condition_id: str
    price: Decimal
    quantity: Decimal
    reserved_cost: Decimal
    edge: Decimal
    maker: bool = False

    def native_order(self, order_factory, *, client_order_id=None):
        """Build native IOC or post-only GTC; this does not submit an order."""
        from nautilus_trader.model import (
            InstrumentId,
            OrderSide,
            Price,
            Quantity,
            TimeInForce,
        )

        return order_factory.limit(
            instrument_id=InstrumentId.from_str(self.instrument_id),
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str(str(self.quantity)),
            price=Price.from_str(str(self.price)),
            time_in_force=TimeInForce.GTC if self.maker else TimeInForce.IOC,
            post_only=self.maker,
            client_order_id=client_order_id,
        )


class ObservedHighTaker:
    """Reserve before dispatch; unresolved outcomes retain their full reservation.

    Limits are cumulative session limits, not mark-to-market exposure. A fresh
    instance must not be armed on restart until its previous ledger is reconciled.
    """

    def __init__(
        self,
        event_id: str,
        day: date,
        markets: list[TemperatureMarket],
        limits: TakerLimits,
        rule_id: str | None = None,
    ):
        self.event_id = event_id
        self.day = day
        self.rule_id = rule_id
        self.limits = limits
        self.markets = {m.condition_id: m for m in markets}
        if len(self.markets) != len(markets):
            raise ValueError("Duplicate condition")
        if len({m.no_instrument_id for m in markets}) != len(markets):
            raise ValueError("Duplicate No instrument")
        self.observation = None
        self.books = {}
        self.pending = {}
        self.used_quantity = {m.condition_id: D(0) for m in markets}
        self.used_cost = D(0)
        self.attempted = set()
        self.last_decisions = {}

    def update_market_rules(self, condition_id, *, tick_size, minimum_quantity):
        """Validate and replace mutable rules without releasing orders or reservations."""
        market = self.markets[condition_id]
        updated = replace(
            market, tick_size=tick_size, minimum_quantity=minimum_quantity
        )
        self.markets[condition_id] = updated
        return updated != market

    def on_weather(
        self,
        observation: WeatherObservation,
        now_ns: int,
        available_cash: Decimal,
        *,
        execution_ready: bool = False,
    ) -> list[BuyNoIntent]:
        _finite(observation.maximum)
        if not self.rule_id or (
            observation.event_id,
            observation.rule_id,
            observation.day,
        ) != (self.event_id, self.rule_id, self.day):
            return []
        if observation.received_ns > now_ns or observation.revision < 0:
            return []
        if self.observation and observation.revision <= self.observation.revision:
            return []
        # A new revision may lower the maximum or revoke confirmation
        self.observation = observation
        return self._evaluate(now_ns, available_cash, execution_ready)

    def on_book(
        self,
        book: NoAsk,
        now_ns: int,
        available_cash: Decimal,
        *,
        execution_ready: bool = False,
    ) -> list[BuyNoIntent]:
        _finite(book.price, book.quantity)
        if book.condition_id not in self.markets:
            raise ValueError("Unknown condition")
        if not 0 < book.price < 1 or book.quantity < 0 or book.received_ns > now_ns:
            raise ValueError("Invalid book")
        previous = self.books.get(book.condition_id)
        if previous and book.sequence <= previous.sequence:
            return []
        self.books[book.condition_id] = book
        return self._evaluate(now_ns, available_cash, execution_ready)

    def _evaluate(self, now_ns, available_cash, execution_ready):
        _finite(available_cash)
        obs = self.observation
        self.last_decisions = {}
        if not execution_ready:
            self.last_decisions = dict.fromkeys(self.markets, "execution_not_ready")
            return []
        if not obs or not obs.confirmed:
            self.last_decisions = dict.fromkeys(self.markets, "weather_unconfirmed")
            return []
        if not 0 <= now_ns - obs.received_ns <= self.limits.max_weather_age_ns:
            self.last_decisions = dict.fromkeys(
                self.markets, "weather_observation_stale"
            )
            return []
        intents = []
        reserved = sum((i.reserved_cost for i in self.pending.values()), D(0))
        budget = min(
            available_cash - reserved,
            self.limits.max_event_cost - self.used_cost - reserved,
        )
        for condition, market in self.markets.items():
            book = self.books.get(condition)
            if market.upper_bound is None or obs.maximum <= market.upper_bound:
                self.last_decisions[condition] = "temperature_not_excluded"
                continue
            if not book or not book.ready:
                self.last_decisions[condition] = "book_not_ready"
                continue
            if not book.accepting_orders:
                self.last_decisions[condition] = "market_not_accepting"
                continue
            if not 0 <= now_ns - book.received_ns <= self.limits.max_book_age_ns:
                self.last_decisions[condition] = "book_stale"
                continue
            if any(i.condition_id == condition for i in self.pending.values()):
                self.last_decisions[condition] = "order_pending"
                continue
            attempt = (condition, book.sequence)
            maker = book.quantity == 0 or book.price > D("0.999")
            price = D("0.999") if maker else book.price
            if attempt in self.attempted:
                self.last_decisions[condition] = "book_already_attempted"
                continue
            if price % market.tick_size:
                self.last_decisions[condition] = "price_off_tick"
                continue
            # Reserve one fee quantum per share above the configured formula;
            # rebates are deliberately excluded from the decision.
            unit_cost = (
                price
                if maker
                else price + market.fee_rate * price * (1 - price) + D("0.00001")
            )
            edge = 1 - unit_cost - (D(0) if maker else self.limits.safety_margin)
            if not maker and edge < self.limits.minimum_edge:
                self.last_decisions[condition] = "edge_below_limit"
                continue
            if budget <= 0:
                self.last_decisions[condition] = "cash_or_event_budget_exhausted"
                continue
            quantity = min(
                self.limits.max_order_quantity if maker else book.quantity,
                self.limits.max_order_quantity,
                self.limits.max_condition_quantity - self.used_quantity[condition],
                budget / unit_cost,
            ).quantize(D("0.01"), rounding=ROUND_DOWN)
            if quantity < max(market.minimum_quantity, D(1)):
                self.last_decisions[condition] = "quantity_below_minimum"
                continue
            key = f"{self.event_id}:{condition}:{book.sequence}"
            intent = BuyNoIntent(
                key,
                market.no_instrument_id,
                condition,
                price,
                quantity,
                quantity * unit_cost,
                edge,
                maker,
            )
            self.pending[key] = intent
            self.last_decisions[condition] = "maker_intent" if maker else "taker_intent"
            self.attempted.add(attempt)
            budget -= intent.reserved_cost
            intents.append(intent)
        return intents

    def settle(self, key: str, filled_quantity: Decimal, total_cost: Decimal) -> None:
        """Apply authoritative terminal totals, including fees, exactly once.

        Do not call on acceptance, timeout, or a partial fill while still open.
        Late/uncertain results must be reconciled before releasing reservations.
        """
        _finite(filled_quantity, total_cost)
        intent = self.pending[key]
        if not 0 <= filled_quantity <= intent.quantity or total_cost < 0:
            raise ValueError("Invalid terminal totals")
        if (filled_quantity == 0) != (total_cost == 0):
            raise ValueError("Inconsistent terminal totals")
        self.used_quantity[intent.condition_id] += filled_quantity
        self.used_cost += total_cost
        del self.pending[key]
