"""PMXT event-directory adapter for Polymarket v1 replay datasets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd

from polymarket._core.models import DatasetMetadataV1
from polymarket._core.models import L2ReplayStepV1
from polymarket._core.models import L2UpdateV1
from polymarket._core.models import MarketMetadataV1
from polymarket._core.models import PolymarketL2DatasetV1
from polymarket.adapters.utils import as_decimal
from polymarket.adapters.utils import as_utc_datetime
from polymarket.adapters.utils import normalize_side
from polymarket.adapters.utils import optional_utc_datetime
from polymarket.adapters.utils import parse_jsonish
from polymarket.adapters.utils import parse_levels
from polymarket.adapters.utils import repo_relative_or_absolute


class PMXTEventV1Adapter:
    """Load a PMXT event folder into the canonical v1 L2 replay contract."""

    adapter_name = "pmxt_event_v1"
    adapter_version = "v1"
    _ORDERBOOK_COLUMNS = (
        "event_type",
        "market",
        "asset_id",
        "timestamp_received",
        "timestamp",
        "bids",
        "asks",
        "side",
        "price",
        "size",
        "best_bid",
        "best_ask",
        "old_tick_size",
        "new_tick_size",
    )
    _DIAGNOSTIC_COLUMNS = ("market", "asset_id")

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root or Path.cwd()

    def load(self, config: Mapping[str, Any]) -> PolymarketL2DatasetV1:
        input_config = config.get("input", config)
        event_dir = self._required_path(input_config, "event_dir")
        condition_id = self._required_text(input_config, "condition_id")
        asset_id = self._required_text(input_config, "asset_id")

        files = self._resolve_files(event_dir)
        event_index = self._read_json(files["event_index"])
        gamma_event = self._read_json(files["gamma_event"])

        orderbook = self._read_selected_orderbook(
            files["orderbook"],
            condition_id=condition_id,
            asset_id=asset_id,
            event_index=event_index,
        )
        if orderbook.empty:
            raise ValueError(f"pmxt_event_v1 orderbook.parquet has no rows: {files['orderbook']}")
        orderbook = orderbook.copy()
        orderbook["_original_row_index"] = range(len(orderbook))
        orderbook["_canonical_market"] = orderbook["market"].map(self._canonical_market)
        orderbook["_canonical_asset_id"] = orderbook["asset_id"].map(self._canonical_asset_id)

        selected = orderbook[
            (orderbook["_canonical_market"] == condition_id)
            & (orderbook["_canonical_asset_id"] == asset_id)
        ].copy()
        if selected.empty:
            self._raise_empty_selection(
                condition_id=condition_id,
                asset_id=asset_id,
                orderbook=orderbook,
                event_index=event_index,
            )

        selected["_timestamp_received_dt"] = selected["timestamp_received"].map(as_utc_datetime)
        selected["_source_timestamp_dt"] = selected["timestamp"].map(optional_utc_datetime)
        selected_ordering_diagnostic = self._selected_ordering_diagnostic(selected)
        selected = selected.sort_values(
            by=["_timestamp_received_dt", "_original_row_index"],
            kind="mergesort",
        )
        selected_ordering_diagnostic.update(self._selected_ordering_diagnostic_after_sort(selected))

        steps: list[L2ReplayStepV1] = []
        for sequence, (_, row) in enumerate(selected.iterrows(), start=1):
            update = self._row_to_update(row)
            steps.append(
                L2ReplayStepV1(
                    sequence=sequence,
                    timestamp_received=row["_timestamp_received_dt"],
                    timestamp=optional_utc_datetime(row.get("timestamp")),
                    updates=(update,),
                ),
            )

        market_metadata = self._gamma_market_metadata(gamma_event, condition_id=condition_id)
        metadata = DatasetMetadataV1(
            dataset_id=str(input_config.get("dataset_id") or event_dir.name),
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            source_type="pmxt_event",
            source_files=tuple(
                repo_relative_or_absolute(files[key], repo_root=self.repo_root)
                for key in ("orderbook", "event_index", "gamma_event", "manifest")
            ),
            assumptions=(
                "PMXT orderbook.parquet rows are pre-contract WebSocket-derived observations normalized by adapter only.",
                "Replay chronology uses receive-time ordering: stable sort by timestamp_received then original row index.",
                "One selected PMXT orderbook row is emitted as one canonical replay step.",
            ),
            warnings=(
                "PMXT caveat: source timestamps may invert relative to receive-time replay order; data_health should audit source-time diagnostics separately.",
                self._format_ordering_diagnostic(selected_ordering_diagnostic),
                "PMXT ordering diagnostic: sequence values are reassigned after stable timestamp_received/original row sort and do not preserve original PMXT row numbering.",
                "PMXT best_bid/best_ask values are preserved as audit/proxy fields only and are not trusted for filtering or execution truth.",
            ),
            market_metadata=market_metadata,
        )
        return PolymarketL2DatasetV1(metadata=metadata, steps=tuple(steps))

    def _read_selected_orderbook(
        self,
        path: Path,
        *,
        condition_id: str,
        asset_id: str,
        event_index: Mapping[str, Any],
    ) -> pd.DataFrame:
        """Read only selected PMXT orderbook rows/columns when parquet filtering permits it."""
        columns = list(self._ORDERBOOK_COLUMNS)
        filtered_frames: list[pd.DataFrame] = []
        for market_filter_value in (condition_id.encode(), condition_id):
            frame = self._read_parquet_columns(
                path,
                columns=columns,
                filters=[("market", "==", market_filter_value), ("asset_id", "==", asset_id)],
            )
            if not frame.empty:
                return frame
            filtered_frames.append(frame)

        diagnostics = self._read_parquet_columns(path, columns=list(self._DIAGNOSTIC_COLUMNS))
        if diagnostics.empty:
            return filtered_frames[0]
        diagnostics = diagnostics.copy()
        diagnostics["_canonical_market"] = diagnostics["market"].map(self._canonical_market)
        diagnostics["_canonical_asset_id"] = diagnostics["asset_id"].map(self._canonical_asset_id)
        diagnostic_selection = diagnostics[
            (diagnostics["_canonical_market"] == condition_id)
            & (diagnostics["_canonical_asset_id"] == asset_id)
        ]
        if diagnostic_selection.empty:
            self._raise_empty_selection(
                condition_id=condition_id,
                asset_id=asset_id,
                orderbook=diagnostics,
                event_index=event_index,
            )

        # Fallback for parquet engines/files that cannot push down the encoded
        # market predicate even though diagnostics prove the selection exists.
        unfiltered = self._read_parquet_columns(path, columns=columns)
        unfiltered = unfiltered.copy()
        unfiltered["_canonical_market"] = unfiltered["market"].map(self._canonical_market)
        unfiltered["_canonical_asset_id"] = unfiltered["asset_id"].map(self._canonical_asset_id)
        return unfiltered[
            (unfiltered["_canonical_market"] == condition_id)
            & (unfiltered["_canonical_asset_id"] == asset_id)
        ].drop(columns=["_canonical_market", "_canonical_asset_id"])

    @staticmethod
    def _read_parquet_columns(
        path: Path,
        *,
        columns: list[str],
        filters: list[tuple[str, str, Any]] | None = None,
    ) -> pd.DataFrame:
        kwargs: dict[str, Any] = {"columns": columns}
        if filters is not None:
            kwargs["filters"] = filters
        try:
            return pd.read_parquet(path, **kwargs)
        except (TypeError, ValueError, NotImplementedError):
            if filters is None:
                raise
            return pd.read_parquet(path, columns=columns)

    @classmethod
    def _selected_ordering_diagnostic(cls, selected: pd.DataFrame) -> dict[str, Any]:
        receive_times = list(selected["_timestamp_received_dt"])
        source_times = [value for value in selected["_source_timestamp_dt"] if value is not None]
        duplicate_counts = selected["_timestamp_received_dt"].value_counts(dropna=False)
        duplicate_groups = duplicate_counts[duplicate_counts > 1]
        return {
            "selected_rows": len(selected),
            "global_physical_receive_time_inversions": "not_computed_filtered_adapter",
            "selected_receive_time_inversions_before_sort": cls._adjacent_inversion_count(receive_times),
            "selected_source_time_inversions_before_sort": cls._adjacent_inversion_count(source_times),
            "duplicate_timestamp_received_group_count": len(duplicate_groups),
            "max_rows_per_timestamp_received": int(duplicate_counts.max()) if not duplicate_counts.empty else 0,
            "stable_sort_key": "timestamp_received,_original_row_index",
        }

    @classmethod
    def _selected_ordering_diagnostic_after_sort(cls, selected: pd.DataFrame) -> dict[str, Any]:
        receive_times = list(selected["_timestamp_received_dt"])
        source_times = [value for value in selected["_source_timestamp_dt"] if value is not None]
        receive_inversions = cls._adjacent_inversion_count(receive_times)
        return {
            "selected_receive_time_inversions_after_sort": receive_inversions,
            "selected_source_time_inversions_after_sort": cls._adjacent_inversion_count(source_times),
            "selected_receive_time_monotonic_after_sort": receive_inversions == 0,
        }

    @staticmethod
    def _adjacent_inversion_count(values: Sequence[Any]) -> int:
        return sum(1 for previous, current in pairwise(values) if current < previous)

    @staticmethod
    def _format_ordering_diagnostic(diagnostic: Mapping[str, Any]) -> str:
        ordered_keys = (
            "selected_rows",
            "global_physical_receive_time_inversions",
            "selected_receive_time_inversions_before_sort",
            "selected_receive_time_inversions_after_sort",
            "selected_source_time_inversions_before_sort",
            "selected_source_time_inversions_after_sort",
            "duplicate_timestamp_received_group_count",
            "max_rows_per_timestamp_received",
            "stable_sort_key",
            "selected_receive_time_monotonic_after_sort",
        )
        formatted_values = []
        for key in ordered_keys:
            value = diagnostic[key]
            if isinstance(value, bool):
                value = str(value).lower()
            formatted_values.append(f"{key}={value}")
        return "PMXT ordering diagnostic: " + ", ".join(formatted_values) + "."

    def _required_path(self, input_config: Mapping[str, Any], key: str) -> Path:
        if input_config.get(key) is None:
            raise ValueError(f"pmxt_event_v1 adapter input requires {key}")
        path = Path(str(input_config[key]))
        if not path.is_absolute():
            path = self.repo_root / path
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    @staticmethod
    def _required_text(input_config: Mapping[str, Any], key: str) -> str:
        value = input_config.get(key)
        if value is None or str(value).strip() == "":
            raise ValueError(f"pmxt_event_v1 adapter input requires {key}")
        return str(value)

    @staticmethod
    def _resolve_files(event_dir: Path) -> dict[str, Path]:
        required = {
            "orderbook": event_dir / "orderbook.parquet",
            "event_index": event_dir / "event_index.json",
            "gamma_event": event_dir / "gamma_event.raw.json",
            "manifest": event_dir / "manifest.json",
        }
        missing = [path.name for path in required.values() if not path.exists()]
        if missing:
            raise FileNotFoundError(f"pmxt_event_v1 missing required file(s) in {event_dir}: {', '.join(missing)}")
        return required

    @staticmethod
    def _read_json(path: Path) -> Mapping[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, Mapping):
            raise ValueError(f"pmxt_event_v1 expected JSON object in {path}")
        return data

    @staticmethod
    def _canonical_market(value: Any) -> str:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return "0x" + value.hex()
        text = str(value)
        if text.startswith("b'") and text.endswith("'"):
            try:
                decoded = text[2:-1].encode("latin1").decode("unicode_escape")
                return decoded
            except UnicodeDecodeError:
                return text
        if text.startswith('b"') and text.endswith('"'):
            try:
                decoded = text[2:-1].encode("latin1").decode("unicode_escape")
                return decoded
            except UnicodeDecodeError:
                return text
        return text

    @staticmethod
    def _canonical_asset_id(value: Any) -> str:
        if pd.isna(value):
            return ""
        return str(value)

    @staticmethod
    def _raise_empty_selection(
        *,
        condition_id: str,
        asset_id: str,
        orderbook: pd.DataFrame,
        event_index: Mapping[str, Any],
    ) -> None:
        available_markets = sorted(str(value) for value in orderbook["_canonical_market"].dropna().unique())
        same_condition = orderbook[orderbook["_canonical_market"] == condition_id]
        if same_condition.empty:
            index_hints = PMXTEventV1Adapter._condition_hints(event_index)
            raise ValueError(
                "pmxt_event_v1 selected condition_id/market has no rows: "
                f"{condition_id!r}; available markets: {available_markets}; "
                f"event_index available condition/token hints: {index_hints}",
            )
        available_tokens = sorted(str(value) for value in same_condition["_canonical_asset_id"].dropna().unique())
        raise ValueError(
            "pmxt_event_v1 selected asset_id/token has no rows for condition_id "
            f"{condition_id!r}: {asset_id!r}; available tokens: {available_tokens}; "
            f"event_index available condition/token hints: {PMXTEventV1Adapter._condition_hints(event_index)}",
        )

    @staticmethod
    def _condition_hints(event_index: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_markets = event_index.get("markets", [])
        if not isinstance(raw_markets, list):
            return []
        hints: list[dict[str, Any]] = []
        for item in raw_markets:
            if isinstance(item, Mapping):
                hints.append(
                    {
                        key: item.get(key)
                        for key in ("conditionId", "condition_id", "yesToken", "noToken", "clobTokenIds")
                        if item.get(key) is not None
                    },
                )
        return hints

    @staticmethod
    def _row_to_update(row: pd.Series) -> L2UpdateV1:
        event_type = str(row.get("event_type"))
        canonical_event_type = "trade" if event_type == "last_trade_price" else event_type
        market = PMXTEventV1Adapter._canonical_market(row.get("market"))
        asset_id = str(row.get("asset_id"))
        if canonical_event_type == "book":
            return L2UpdateV1(
                event_type="book",
                market=market,
                asset_id=asset_id,
                bids=parse_levels(row.get("bids")),
                asks=parse_levels(row.get("asks")),
                best_bid=as_decimal(row.get("best_bid")),
                best_ask=as_decimal(row.get("best_ask")),
            )
        if canonical_event_type == "price_change":
            return L2UpdateV1(
                event_type="price_change",
                market=market,
                asset_id=asset_id,
                side=normalize_side(row.get("side")),
                price=as_decimal(row.get("price")),
                size=as_decimal(row.get("size")),
                best_bid=as_decimal(row.get("best_bid")),
                best_ask=as_decimal(row.get("best_ask")),
            )
        if canonical_event_type == "trade":
            return L2UpdateV1(
                event_type="trade",
                market=market,
                asset_id=asset_id,
                side=normalize_side(row.get("side")),
                price=as_decimal(row.get("price")),
                size=as_decimal(row.get("size")),
                best_bid=as_decimal(row.get("best_bid")),
                best_ask=as_decimal(row.get("best_ask")),
            )
        if canonical_event_type == "tick_size_change":
            return L2UpdateV1(
                event_type="tick_size_change",
                market=market,
                asset_id=asset_id,
                old_tick_size=as_decimal(row.get("old_tick_size")),
                new_tick_size=as_decimal(row.get("new_tick_size")),
                best_bid=as_decimal(row.get("best_bid")),
                best_ask=as_decimal(row.get("best_ask")),
            )
        raise ValueError(f"pmxt_event_v1 unsupported PMXT event_type: {event_type!r}")

    @staticmethod
    def _gamma_market_metadata(
        gamma_event: Mapping[str, Any],
        *,
        condition_id: str,
    ) -> tuple[MarketMetadataV1, ...]:
        markets = gamma_event.get("markets", [])
        if isinstance(markets, Mapping):
            markets = [markets]
        if not isinstance(markets, list):
            raise ValueError("pmxt_event_v1 gamma_event.raw.json requires markets list")
        market = next(
            (
                item
                for item in markets
                if isinstance(item, Mapping)
                and str(item.get("conditionId") or item.get("condition_id")) == condition_id
            ),
            None,
        )
        if market is None:
            available = [
                str(item.get("conditionId") or item.get("condition_id"))
                for item in markets
                if isinstance(item, Mapping)
            ]
            raise ValueError(
                f"pmxt_event_v1 gamma metadata has no market for condition_id {condition_id!r}; "
                f"available condition_id values: {available}",
            )

        outcomes = PMXTEventV1Adapter._json_array(market.get("outcomes"), "outcomes")
        outcome_prices = PMXTEventV1Adapter._json_array(market.get("outcomePrices"), "outcomePrices")
        clob_token_ids = PMXTEventV1Adapter._json_array(market.get("clobTokenIds"), "clobTokenIds")
        if not (len(outcomes) == len(outcome_prices) == len(clob_token_ids)):
            raise ValueError(
                "pmxt_event_v1 gamma metadata length mismatch: outcomes, outcomePrices, "
                f"and clobTokenIds lengths are {len(outcomes)}, {len(outcome_prices)}, {len(clob_token_ids)}",
            )
        if len(outcomes) != 2:
            raise ValueError(f"pmxt_event_v1 requires binary outcomes; got {len(outcomes)} outcomes")

        fee_schedule = parse_jsonish(market.get("feeSchedule")) or {}
        if not isinstance(fee_schedule, Mapping):
            raise ValueError("pmxt_event_v1 gamma feeSchedule must be a JSON object or object")
        taker_fee = as_decimal(fee_schedule.get("rate"))
        resolution_time = optional_utc_datetime(market.get("closedTime"))
        minimum_tick_size = as_decimal(
            market.get("orderPriceMinTickSize")
            or market.get("minimum_tick_size")
            or market.get("min_tick_size")
            or market.get("tick_size"),
        )

        items: list[MarketMetadataV1] = []
        for outcome, token_id, payout_value in zip(outcomes, clob_token_ids, outcome_prices, strict=True):
            payout = as_decimal(payout_value)
            items.append(
                MarketMetadataV1(
                    condition_id=condition_id,
                    token_id=str(token_id),
                    outcome=str(outcome),
                    maker_fee=Decimal(0),
                    taker_fee=taker_fee,
                    fee_source="gamma_event.raw.market.feeSchedule.rate" if taker_fee is not None else "unknown",
                    minimum_tick_size=minimum_tick_size,
                    tick_size_source="gamma_event.raw.market.orderPriceMinTickSize"
                    if minimum_tick_size is not None
                    else "unknown",
                    resolution_status=str(market.get("umaResolutionStatus"))
                    if market.get("umaResolutionStatus") is not None
                    else None,
                    resolution_time=resolution_time,
                    token_payout=payout,
                    winner=(payout == Decimal(1)) if payout is not None else None,
                    resolution_source="gamma_event.raw.market.outcomePrices",
                ),
            )
        return tuple(items)

    @staticmethod
    def _json_array(value: Any, field_name: str) -> Sequence[Any]:
        parsed = parse_jsonish(value)
        if not isinstance(parsed, list):
            raise ValueError(f"pmxt_event_v1 gamma metadata field {field_name} must be a JSON array or array")
        return parsed
