"""Shared adapter parsing helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from polymarket._core.models import LevelV1


def is_missing_scalar(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def as_decimal(value: Any) -> Decimal | None:
    if is_missing_scalar(value):
        return None
    text = str(value)
    if text == "":
        return None
    return Decimal(text)


def as_bool(value: Any) -> bool | None:
    """Parse a metadata boolean without Python's truthy-string footgun."""

    if is_missing_scalar(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "t", "yes", "y", "1"}:
            return True
        if text in {"false", "f", "no", "n", "0"}:
            return False
    raise ValueError(f"cannot parse boolean metadata value: {value!r}")


def as_utc_datetime(value: Any) -> datetime:
    if value is None:
        raise ValueError("datetime value is required")
    epoch_value = _coerce_epoch_timestamp(value)
    if epoch_value is not None:
        ts = pd.to_datetime(epoch_value[0], unit=epoch_value[1], utc=True)
        return ts.to_pydatetime()
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(UTC)
    else:
        ts = ts.tz_convert(UTC)
    return ts.to_pydatetime()


def _coerce_epoch_timestamp(value: Any) -> tuple[int, str] | None:
    """Return ``(value, unit)`` for exchange epoch timestamps.

    Polymarket WebSocket source timestamps are commonly millisecond epoch
    strings (for example ``"1782440717084"``).  Pandas treats a digit-only
    string as a calendar-like string and can overflow, so handle numeric epoch
    values before the generic timestamp parser.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.lstrip("-").isdigit():
            return None
        number = int(text)
    else:
        return None

    magnitude = abs(number)
    if magnitude >= 10**18:
        return number, "ns"
    if magnitude >= 10**15:
        return number, "us"
    if magnitude >= 10**12:
        return number, "ms"
    if magnitude >= 10**9:
        return number, "s"
    return None


def optional_utc_datetime(value: Any) -> datetime | None:
    if is_missing_scalar(value):
        return None
    return as_utc_datetime(value)


def parse_jsonish(value: Any) -> Any:
    if is_missing_scalar(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def parse_levels(value: Any) -> tuple[LevelV1, ...]:
    parsed = parse_jsonish(value)
    if parsed is None:
        return ()
    levels: list[LevelV1] = []
    for item in parsed:
        if isinstance(item, dict):
            price = item.get("price")
            size = item.get("size")
        else:
            price, size = item[0], item[1]
        price_decimal = as_decimal(price)
        size_decimal = as_decimal(size)
        if price_decimal is None or size_decimal is None or size_decimal <= 0:
            continue
        levels.append(LevelV1(price=price_decimal, size=size_decimal))
    return tuple(levels)


def normalize_side(value: Any) -> str | None:
    if is_missing_scalar(value):
        return None
    text = str(value).upper()
    if text in {"BUY", "BID", "YES"}:
        return "BUY"
    if text in {"SELL", "ASK", "NO"}:
        return "SELL"
    return None


def repo_relative_or_absolute(path: Path, *, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None

