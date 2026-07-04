"""Local raw Polymarket WebSocket NDJSON adapter."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket.adapters.utils import (
    as_decimal,
    as_utc_datetime,
    normalize_side,
    optional_utc_datetime,
    parse_levels,
    repo_relative_or_absolute,
)
from polymarket._core.models import (
    DatasetMetadataV1,
    L2ReplayStepV1,
    L2UpdateV1,
    MarketMetadataV1,
    PolymarketL2DatasetV1,
)


class LiveWsV1Adapter:
    adapter_name = "live_ws_v1"
    adapter_version = "v1"
    control_event_types = frozenset({"ping", "pong"})

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root or Path.cwd()

    def load(self, config: Mapping[str, Any]) -> PolymarketL2DatasetV1:
        input_config = config.get("input", config)
        raw_path = input_config.get("ndjson_path") or input_config.get("path")
        if raw_path is None and input_config.get("ndjson_path_env") is not None:
            env_name = str(input_config["ndjson_path_env"])
            raw_path = os.environ.get(env_name)
            if raw_path is None:
                raise FileNotFoundError(f"environment variable {env_name} is not set")
        ndjson_path = Path(str(raw_path))
        if not ndjson_path.is_absolute():
            ndjson_path = self.repo_root / ndjson_path
        if not ndjson_path.exists():
            raise FileNotFoundError(ndjson_path)

        market_metadata, metadata_files = self._load_market_metadata(input_config)
        steps: list[L2ReplayStepV1] = []
        skipped_control_messages = 0
        with ndjson_path.open(encoding="utf-8-sig") as f:
            for local_index, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                message = self._extract_message(payload)
                updates = tuple(self._message_to_updates(message))
                if not updates:
                    skipped_control_messages += 1
                    continue
                timestamp_received = self._received_timestamp(payload, message)
                source_ts = optional_utc_datetime(message.get("timestamp"))
                steps.append(
                    L2ReplayStepV1(
                        sequence=int(payload.get("local_msg_index", local_index)),
                        timestamp_received=timestamp_received,
                        timestamp=source_ts,
                        updates=updates,
                    ),
                )

        warnings = ["No official Polymarket message id is assumed; local capture order is local evidence only."]
        if skipped_control_messages:
            warnings.append(f"Skipped {skipped_control_messages} explicit live WS control message(s).")
        metadata = DatasetMetadataV1(
            dataset_id=str(input_config.get("dataset_id") or ndjson_path.stem),
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            source_type="live_raw_ws",
            source_files=(
                repo_relative_or_absolute(ndjson_path, repo_root=self.repo_root),
                *metadata_files,
            ),
            assumptions=("One local raw WebSocket message is one replay step.",),
            warnings=tuple(warnings),
            market_metadata=market_metadata,
        )
        return PolymarketL2DatasetV1(metadata=metadata, steps=tuple(steps))

    def _load_market_metadata(
        self,
        input_config: Mapping[str, Any],
    ) -> tuple[tuple[MarketMetadataV1, ...], tuple[str, ...]]:
        raw_metadata = input_config.get("market_metadata")
        metadata_files: tuple[str, ...] = ()
        if input_config.get("market_metadata_path") is not None:
            metadata_path = Path(str(input_config["market_metadata_path"]))
            if not metadata_path.is_absolute():
                metadata_path = self.repo_root / metadata_path
            if not metadata_path.exists():
                raise FileNotFoundError(metadata_path)
            raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            metadata_files = (repo_relative_or_absolute(metadata_path, repo_root=self.repo_root),)

        if raw_metadata is None:
            return (), metadata_files

        if isinstance(raw_metadata, Mapping) and "markets" in raw_metadata:
            raw_markets = raw_metadata["markets"]
        else:
            raw_markets = raw_metadata

        if isinstance(raw_markets, Mapping):
            raw_markets = [raw_markets]
        if not isinstance(raw_markets, list):
            raise ValueError("live_ws_v1 market_metadata must be an object, a list, or {'markets': [...]}")

        return tuple(self._market_metadata_item(item) for item in raw_markets), metadata_files

    @staticmethod
    def _market_metadata_item(item: Mapping[str, Any]) -> MarketMetadataV1:
        condition_id = item.get("condition_id") or item.get("market") or item.get("conditionId")
        if condition_id is None:
            raise ValueError("market_metadata item requires condition_id/market")
        token_id = item.get("token_id")
        if token_id is None:
            token_id = item.get("asset_id")
        fee_schedule = item.get("feeSchedule") or item.get("fee_schedule") or {}
        if not isinstance(fee_schedule, Mapping):
            fee_schedule = {}
        raw_taker_fee = item.get("taker_fee")
        if raw_taker_fee is None:
            raw_taker_fee = item.get("fee_rate")
        if raw_taker_fee is None:
            raw_taker_fee = item.get("rate")
        if raw_taker_fee is None:
            raw_taker_fee = fee_schedule.get("rate")
        raw_maker_fee = item.get("maker_fee", "0")
        return MarketMetadataV1(
            condition_id=str(condition_id),
            token_id=str(token_id) if token_id is not None else None,
            maker_fee=as_decimal(raw_maker_fee) or Decimal("0"),
            taker_fee=as_decimal(raw_taker_fee) if raw_taker_fee is not None else None,
            fee_source=str(item.get("fee_source") or "market_metadata"),
            category=str(item["category"]) if item.get("category") is not None else None,
        )

    @staticmethod
    def _extract_message(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = payload.get("raw_json") or payload.get("message") or payload.get("data")
        if raw is None:
            return payload
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, list):
            if len(raw) != 1:
                raise ValueError("live_ws_v1 expects one message per NDJSON line")
            raw = raw[0]
        if not isinstance(raw, Mapping):
            raise ValueError(f"unsupported raw WebSocket payload: {type(raw)!r}")
        return raw

    @staticmethod
    def _received_timestamp(payload: Mapping[str, Any], message: Mapping[str, Any]) -> Any:
        for key in ("recv_wall_time_utc", "timestamp_received", "received_at"):
            if payload.get(key) is not None:
                return as_utc_datetime(payload[key])
        if message.get("timestamp_received") is not None:
            return as_utc_datetime(message["timestamp_received"])
        raise ValueError(
            "live_ws_v1 requires an explicit receive timestamp "
            "(recv_wall_time_utc/timestamp_received/received_at); "
            "do not substitute Polymarket source timestamp for replay order",
        )

    def _message_to_updates(self, message: Mapping[str, Any]) -> list[L2UpdateV1]:
        event_type = str(message.get("event_type") or message.get("type") or "")
        normalized_event_type = event_type.lower()
        if normalized_event_type in self.control_event_types:
            return []
        if event_type == "price_change":
            return self._price_change_updates(message)
        if event_type == "book":
            return self._book_updates(message)
        if event_type == "last_trade_price":
            return [self._trade_update(message)]
        if event_type == "tick_size_change":
            return [self._tick_size_update(message)]
        raise ValueError(f"unsupported live WS event_type: {event_type!r}")

    @staticmethod
    def _price_change_updates(message: Mapping[str, Any]) -> list[L2UpdateV1]:
        changes = message.get("price_changes") or message.get("changes") or []
        if not changes:
            raise ValueError("live_ws_v1 price_change message has no price_changes/changes")
        updates: list[L2UpdateV1] = []
        for change in changes:
            market = str(change.get("market") or message.get("market"))
            asset_id = str(change.get("asset_id") or change.get("asset") or message.get("asset_id"))
            updates.append(
                L2UpdateV1(
                    event_type="price_change",
                    market=market,
                    asset_id=asset_id,
                    side=normalize_side(change.get("side")),  # type: ignore[arg-type]
                    price=as_decimal(change.get("price")),
                    size=as_decimal(change.get("size")),
                    best_bid=as_decimal(change.get("best_bid") or message.get("best_bid")),
                    best_ask=as_decimal(change.get("best_ask") or message.get("best_ask")),
                ),
            )
        return updates

    @staticmethod
    def _book_updates(message: Mapping[str, Any]) -> list[L2UpdateV1]:
        return [
            L2UpdateV1(
                event_type="book",
                market=str(message.get("market")),
                asset_id=str(message.get("asset_id") or message.get("asset")),
                bids=parse_levels(message.get("bids")),
                asks=parse_levels(message.get("asks")),
                best_bid=as_decimal(message.get("best_bid")),
                best_ask=as_decimal(message.get("best_ask")),
            ),
        ]

    @staticmethod
    def _trade_update(message: Mapping[str, Any]) -> L2UpdateV1:
        return L2UpdateV1(
            event_type="trade",
            market=str(message.get("market")),
            asset_id=str(message.get("asset_id") or message.get("asset")),
            price=as_decimal(message.get("price")),
            size=as_decimal(message.get("size")),
            side=normalize_side(message.get("side")),  # type: ignore[arg-type]
        )

    @staticmethod
    def _tick_size_update(message: Mapping[str, Any]) -> L2UpdateV1:
        return L2UpdateV1(
            event_type="tick_size_change",
            market=str(message.get("market")),
            asset_id=str(message.get("asset_id") or message.get("asset")),
            old_tick_size=as_decimal(message.get("old_tick_size")),
            new_tick_size=as_decimal(message.get("new_tick_size")),
        )

