"""Read-only AWC METAR input; no assumption of equivalence to settlement data."""

from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
import json
import re
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from polymarket.strategies.weather_observed_high import WeatherObservation

AWC_ROUTINE_CELSIUS = "awc-routine-rounded-celsius-v1"


def routine_temperature(raw: str, station: str) -> Decimal | None:
    """Read the routine report's T remark, falling back to its body temperature."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Missing raw METAR for AWC settlement rule")
    tokens = raw.strip().rstrip("=").split()
    if tokens[0] == "SPECI":
        return None
    station_index = 1 if tokens[0] == "METAR" else 0
    if len(tokens) <= station_index or tokens[station_index] != station:
        raise ValueError("Raw METAR station mismatch")
    remark_index = tokens.index("RMK") if "RMK" in tokens else len(tokens)
    precise = [
        token
        for token in tokens[remark_index + 1 :]
        if re.fullmatch(r"T[01]\d{3}[01]\d{3}", token)
    ]
    if len(set(precise)) > 1:
        raise ValueError("Conflicting hourly temperature remarks")
    if precise:
        group = precise[0]
        return Decimal(int(group[2:5])) / 10 * (-1 if group[1] == "1" else 1)
    temperatures = []
    for token in tokens[station_index + 1 : remark_index]:
        match = re.fullmatch(r"(M?\d{2})/(?:M?\d{2}|/{0,2})", token)
        if match:
            temperatures.append(Decimal(match[1].replace("M", "-")))
    if len(set(temperatures)) > 1:
        raise ValueError("Conflicting body temperatures")
    return temperatures[0] if temperatures else None


def fetch_metar(station: str) -> list[dict]:
    if not re.fullmatch(r"[A-Z]{4}", station):
        raise ValueError("Expected four-letter ICAO station")
    request = Request(
        f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=48",
        headers={
            "User-Agent": "PolymarketWeatherResearch/0.1",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError("METAR data unavailable")
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("METAR response too large")
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("Expected METAR list")
    return rows


def normalize_metar(
    rows,
    *,
    station,
    event_id,
    rule_id,
    day,
    now_ns,
    revision,
    max_age_seconds=4500,
    timezone="Asia/Shanghai",
    metar_rule="decoded-temperature",
):
    """Use station-local observation day; freshness is observation time, not polling time.

    The maximum is only a lower bound observed in these reports. The configured
    settlement rule must explicitly permit using this source and integer Celsius.
    """
    if metar_rule not in ("decoded-temperature", AWC_ROUTINE_CELSIUS):
        raise ValueError("Unsupported METAR rule")
    selected = {}
    for row in rows:
        if row.get("icaoId") != station:
            continue
        timestamp = int(row["obsTime"])
        if datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).date() != day:
            continue
        if timestamp * 1_000_000_000 > now_ns:
            raise ValueError("Future weather observation")
        if metar_rule == AWC_ROUTINE_CELSIUS:
            raw = row.get("rawOb")
            # A special report never replaces a routine observation at the same timestamp
            if isinstance(raw, str) and raw.split()[:1] == ["SPECI"]:
                continue
            temperature = routine_temperature(raw, station)
        else:
            temperature = Decimal(str(row["temp"]))
        if temperature is not None and (
            not temperature.is_finite() or not -90 <= temperature <= 60
        ):
            raise ValueError("Invalid temperature")
        receipt = datetime.fromisoformat(row["receiptTime"].replace("Z", "+00:00"))
        if receipt.utcoffset() is None:
            raise ValueError("Missing receipt timezone")
        previous = selected.get(timestamp)
        if previous and receipt == previous[0] and temperature != previous[1]:
            raise ValueError("Conflicting weather reports")
        if not previous or receipt > previous[0]:
            selected[timestamp] = (receipt, temperature)
    # Keep a newer temperature-less correction until deduplication has removed the old value
    selected = {
        stamp: value for stamp, value in selected.items() if value[1] is not None
    }
    if not selected:
        raise ValueError("No observations for configured date")
    latest_ns = max(selected) * 1_000_000_000
    if now_ns - latest_ns > max_age_seconds * 1_000_000_000:
        raise ValueError("Weather observations stale")
    maximum = max(value[1] for value in selected.values())
    if metar_rule == AWC_ROUTINE_CELSIUS:
        # Halfway values round toward the higher integer, including -8.5 -> -8
        maximum = (maximum + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)
    return WeatherObservation(
        event_id,
        rule_id,
        day,
        maximum,
        latest_ns,
        revision,
    )
