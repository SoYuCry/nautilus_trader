"""
Target Polymarket L2 data contract for research backtests.

These types describe the standard format we want the data/IT side to deliver.
Temporary adapters and one-off scripts may patch pre-contract inputs into this
shape, but the backtest stack should treat this model as the fixed boundary, not
as an open-ended compatibility layer for arbitrary data sources.

The Nautilus-native bridge consumes only these contract steps and must not
branch on upstream source quirks.  Matching, fills, cash, positions, and PnL
belong to NautilusTrader's native backtest engine, not this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Literal


EventTypeV1 = Literal["book", "price_change", "trade", "tick_size_change"]
BookSideV1 = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class LevelV1:
    """One price level in canonical L2 book form."""

    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class L2UpdateV1:
    """One canonical update inside an atomic replay step."""

    event_type: EventTypeV1
    market: str
    asset_id: str
    side: BookSideV1 | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    bids: tuple[LevelV1, ...] = ()
    asks: tuple[LevelV1, ...] = ()
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    old_tick_size: Decimal | None = None
    new_tick_size: Decimal | None = None


@dataclass(frozen=True, slots=True)
class L2ReplayStepV1:
    """One atomic replay step in the required data contract."""

    sequence: int
    timestamp_received: datetime
    timestamp: datetime | None
    updates: tuple[L2UpdateV1, ...]


@dataclass(frozen=True, slots=True)
class MarketMetadataV1:
    """
    Market-level metadata snapshot needed to price and audit backtests.

    This is dataset-level metadata, not replay data.  For fee modelling, the
    target feed should preserve the effective Polymarket fee schedule observed
    for the market instead of forcing strategy configs to hard-code a guess.
    """

    condition_id: str
    token_id: str | None = None
    outcome: str | None = None
    maker_fee: Decimal = Decimal(0)
    taker_fee: Decimal | None = None
    fee_source: str = "unknown"
    category: str | None = None
    minimum_tick_size: Decimal | None = None
    tick_size_source: str = "unknown"
    resolution_status: str | None = None
    resolution_time: datetime | None = None
    token_payout: Decimal | None = None
    winner: bool | None = None
    resolution_source: str = "unknown"


@dataclass(frozen=True, slots=True)
class DatasetMetadataV1:
    """
    Source metadata for reporting and audit only.

    Replay chronology and L2 conversion must not branch on provenance fields.
    Market metadata is allowed to supply explicit instrument economics such as
    fees, because those are part of the data contract rather than source quirks.
    """

    dataset_id: str
    adapter_name: str
    adapter_version: str
    source_type: str
    source_files: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_quality: dict[str, Any] = field(default_factory=dict)
    market_metadata: tuple[MarketMetadataV1, ...] = ()


@dataclass(frozen=True, slots=True)
class PolymarketL2DatasetV1:
    """Dataset in the required Polymarket v1 L2 replay contract."""

    metadata: DatasetMetadataV1
    steps: tuple[L2ReplayStepV1, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("PolymarketL2DatasetV1 requires at least one replay step")
