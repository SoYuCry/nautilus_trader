"""Offline Shanghai capacity and bounded first-taker estimates on accepted native depth."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import importlib.util
import json
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import as_completed
from datetime import date
from decimal import ROUND_DOWN
from decimal import Decimal as D
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ZERO = (D(0), D(0), D(0), D(0))


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".writing")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def qualifying_levels(asks, rate, minimum_edge=D(0), margin=D(0)):
    """Use the live capacity edge formula, retaining only marketable NO asks."""
    result = []
    for price, size in asks.items():
        p, q = D(str(price)), D(str(size))
        if not D(0) < p <= D("0.999") or q <= 0:
            continue
        f = rate * p * (1 - p)
        if 1 - p - f - D("0.00001") - margin >= minimum_edge:
            result.append((p, q, f))
    return sorted(result)


def total(levels, minimum):
    q = sum((q for _, q, _ in levels), D(0))
    if q < minimum:
        return ZERO
    cost = sum((p * q for p, q, _ in levels), D(0))
    fee = sum((f * q for _, q, f in levels), D(0))
    return q, cost, fee, q - cost - fee


def bounded_taker(levels, remaining, order_limit, condition_limit, minimum):
    """Ideal first best-ask fill under live sizing; no order/fill lifecycle claim."""
    if not levels:
        return None
    p, depth, unit_fee = levels[0]
    reserve = p + unit_fee + D("0.00001")
    q = min(depth, order_limit, condition_limit, remaining / reserve)
    q = q.quantize(D("0.01"), rounding=ROUND_DOWN)
    if q < minimum:
        return None
    return {
        "price": p,
        "quantity": q,
        "cost": q * p,
        "fee": q * unit_fee,
        "attempt_reserve": q * reserve,
        "conditional_profit": q * (1 - p - unit_fee),
    }


def values(v):
    return dict(zip(("shares", "notional", "estimated_fee", "conditional_profit"), v, strict=True))


def weather_updates(records):
    """A same-receipt normalization error overrides the preceding poll atomically."""
    grouped = {}
    for record in records:
        stamp = int(record["received_ns"])
        if stamp not in grouped or record.get("kind") == "metar_error":
            grouped[stamp] = record
    return [grouped[stamp] for stamp in sorted(grouped)]


def run_event(input_path, output_dir):
    # Process-local thread limits; no shared services or environment are changed
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    import collector_native as native
    import pyarrow as pa

    from nautilus_trader.model import BookAction
    from nautilus_trader.model import BookType
    from nautilus_trader.model import NautilusDataType
    from nautilus_trader.model import OrderBook
    from nautilus_trader.persistence import ParquetDataCatalog
    from polymarket.event_catalog.native import restore_order_book_batches

    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    cfg = load(input_path)
    for key in ("station", "timezone", "date", "metar_rule"):
        if not cfg.get(key):
            raise ValueError(f"Explicit per-Event {key} is required")
    if cfg.get("temperature_unit", "C") != "C":
        raise ValueError("Convert reviewed bucket thresholds to Celsius before analysis; Fahrenheit rules are not inferred")
    slug = cfg["event_slug"]
    output = Path(output_dir) / f"{slug}.json"
    catalog_path = Path(cfg["catalog_path"])
    manifest = load(catalog_path / "manifest.json")
    start = native.iso_ns(manifest["analysis_window"]["start"])
    end = native.iso_ns(manifest["analysis_window"]["end"])
    raw_end = manifest["captured_window"]["received_max_ns"]
    start = max(start, manifest["captured_window"].get("received_min_ns", start))
    # Include a final serialized batch at the same raw receive cutoff
    serialized_end = raw_end + manifest["audit"]["max_adjustment_ns"] + 1
    end = min(end, serialized_end)
    weather_module = ROOT / "weather_metar_frozen.py"
    spec = importlib.util.spec_from_file_location("capacity_frozen_weather", weather_module)
    weather = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(weather)
    limits = cfg.get("limits", {})
    edge, margin = D(limits.get("minimum_edge") or "0"), D(limits.get("safety_margin") or "0")
    cash = D(limits.get("max_event_cost") or "100")
    order_limit = D(limits.get("max_order_quantity") or "5")
    condition_limit = D(limits.get("max_condition_quantity") or "5")
    cat = ParquetDataCatalog(str(catalog_path))
    instruments = {str(i.raw_symbol): i for i in cat.instruments()}
    conditions = {}
    stream_heap = []
    sequence = 0
    actual_payouts = {}
    for item in cfg["conditions"]:
        token = item["no_token_id"]
        instrument = instruments[token]
        iid = str(instrument.id)
        conditions[token] = {
            **item,
            "book": OrderBook(instrument.id, BookType.L2_MBP),
            "initialized": False,
            "closed": False,
            "contribution": ZERO,
            "gross": ZERO,
        }
        deltas = cat.query_order_book_deltas([iid])
        batches = restore_order_book_batches(deltas)
        del deltas
        iterator = iter(batches)
        first = next(iterator, None)
        if first is not None:
            heapq.heappush(stream_heap, (first.ts_init, sequence, token, first, iterator))
            sequence += 1
        closes = cat.query(NautilusDataType.InstrumentClose, identifiers=[iid])
        for close in closes:
            actual_payouts[token] = close.close_price.as_decimal()
            heapq.heappush(stream_heap, (close.ts_init, sequence, token, close, iter(())))
            sequence += 1
    polls = weather_updates(cfg["weather"])
    for poll_index, poll in enumerate(polls):
        heapq.heappush(
            stream_heap, (int(poll["received_ns"]), -len(polls) + poll_index, "", poll, iter(()))
        )
    current_weather, expiry = None, -1
    aggregate, gross_aggregate = ZERO, ZERO
    peak = {"ts_init": None, **values(ZERO)}
    gross_peak = {"ts_init": None, **values(ZERO)}
    integral = [D(0)] * 4
    weather_ns = 0
    last = start
    bounded, first_sweeps = {}, {}
    weather_errors = []
    observed_batches = 0
    unknown_fees = sorted(t for t, s in conditions.items() if s.get("fee_rate") is None)

    def integrate(now):
        nonlocal last, weather_ns
        until = min(max(now, start), end)
        if until > last:
            dt = until - last
            if current_weather is not None:
                weather_ns += dt
                for j in range(4):
                    integral[j] += aggregate[j] * dt
            last = until

    def recompute(token, now):
        nonlocal aggregate, gross_aggregate, peak, gross_peak, cash
        state = conditions[token]
        old, old_gross = state["contribution"], state["gross"]
        contribution = gross = ZERO
        upper = state.get("upper_bound")
        eligible = (
            current_weather is not None
            and now < expiry
            and start <= now < end
            and upper is not None
            and current_weather.maximum > D(upper)
            and state["initialized"]
            and not state["closed"]
        )
        if eligible:
            minimum = D(state.get("minimum_quantity") or "5")
            asks = state["book"].asks_to_dict()
            gross = total(qualifying_levels(asks, D(0)), minimum)
            if state.get("fee_rate") is not None:
                levels = qualifying_levels(asks, D(state["fee_rate"]), edge, margin)
                contribution = total(levels, minimum)
                if contribution[0] and token not in first_sweeps:
                    first_sweeps[token] = {
                        "token_id": token,
                        "condition_id": state["condition_id"],
                        "ts_init": now,
                        **values(contribution),
                    }
                if token not in bounded:
                    fill = bounded_taker(levels, cash, order_limit, condition_limit, minimum)
                    if fill:
                        cash -= fill["attempt_reserve"]
                        bounded[token] = {
                            "token_id": token,
                            "condition_id": state["condition_id"],
                            "ts_init": now,
                            **fill,
                        }
        aggregate = tuple(a - b + c for a, b, c in zip(aggregate, old, contribution, strict=True))
        gross_aggregate = tuple(
            a - b + c for a, b, c in zip(gross_aggregate, old_gross, gross, strict=True)
        )
        state["contribution"], state["gross"] = contribution, gross

    def record_peak(now):
        nonlocal peak, gross_peak
        if aggregate[1] + aggregate[2] > D(peak["notional"]) + D(peak["estimated_fee"]):
            peak = {"ts_init": now, **values(aggregate)}
        if gross_aggregate[1] > D(gross_peak["notional"]):
            gross_peak = {"ts_init": now, **values(gross_aggregate)}

    while stream_heap:
        now, _, token, record, iterator = heapq.heappop(stream_heap)
        if now >= end:
            break
        if current_weather is not None and expiry <= now:
            integrate(expiry)
            current_weather = None
            for t in conditions:
                recompute(t, expiry)
        integrate(now)
        if not token:
            try:
                if record.get("kind") == "metar_error":
                    raise ValueError("recorded_metar_error")
                current_weather = weather.normalize_metar(
                    record["rows"],
                    station=cfg.get("station", "ZSPD"),
                    event_id=slug,
                    rule_id=cfg.get("rule_id", "recorded_event_rule"),
                    day=date.fromisoformat(cfg["date"]),
                    now_ns=now,
                    revision=now,
                    timezone=cfg.get("timezone", "Asia/Shanghai"),
                    metar_rule=cfg.get("metar_rule") or "decoded-temperature",
                )
                expiry = min(now + 120_000_000_000, current_weather.received_ns + 4_500_000_000_000)
            except (ValueError, KeyError, TypeError) as error:
                current_weather = None
                weather_errors.append({"received_ns": now, "reason": str(error)})
            for t in conditions:
                recompute(t, now)
        elif hasattr(record, "deltas"):
            state = conditions[token]
            state["book"].apply_deltas(record)
            if any(d.action == BookAction.CLEAR for d in record.deltas):
                state["initialized"] = True
            observed_batches += 1
            recompute(token, now)
        else:
            conditions[token]["closed"] = True
            recompute(token, now)
        record_peak(now)
        following = next(iterator, None)
        if following is not None:
            heapq.heappush(stream_heap, (following.ts_init, sequence, token, following, iterator))
            sequence += 1
    if current_weather is not None and expiry < end:
        integrate(expiry)
        current_weather = None
    integrate(end)
    for row in list(bounded.values()) + list(first_sweeps.values()):
        payout = actual_payouts.get(row["token_id"])
        qty = row.get("quantity", row.get("shares"))
        cost = row.get("cost", row.get("notional"))
        fee = row.get("fee", row.get("estimated_fee"))
        row["recorded_no_payout"] = payout
        row["settlement_profit"] = None if payout is None else payout * qty - cost - fee
    result = {
        "event_slug": slug,
        "date": cfg["date"],
        "status": "completed",
        "input_path": str(input_path),
        "input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
        "catalog_path": str(catalog_path),
        "weather_polls": len(polls),
        "weather_source_records": len(cfg["weather"]),
        "observed_analysis_start_ns": start,
        "observed_analysis_end_ns": end,
        "coverage_status": "weather_available" if weather_ns else "no_valid_recorded_weather",
        "weather_valid_hours": str(D(weather_ns) / D(3_600_000_000_000)),
        "no_book_batches": observed_batches,
        "peak_simultaneous_capacity": peak,
        "gross_peak_without_fee_filter": gross_peak,
        "mean_capacity_during_valid_weather": None
        if not weather_ns
        else values(tuple(v / weather_ns for v in integral)),
        "first_opportunity_sweep_per_condition": list(first_sweeps.values()),
        "bounded_first_taker_estimate": list(bounded.values()),
        "bounded_remaining_attempt_budget": cash,
        "unknown_fee_tokens": unknown_fees,
        "weather_errors": weather_errors,
        "analysis_window": manifest["analysis_window"],
        "limitations": [
            "Instantaneous capacities are not added across repeated snapshots.",
            "First sweeps are one per condition and are a hypothetical zero-latency fill scenario.",
            "Bounded taker estimate excludes makers, order failures, competition and existing positions.",
            "It is not an exact native execution backtest or actual account profit.",
            "Fees follow saved configuration or labeled metadata scenario, not independently verified historical fills.",
            "Missing weather or unknown fees are coverage gaps, not verified zero opportunity.",
        ],
    }
    save(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    files = sorted(args.inputs.glob("highest-temperature-in-*.json"))
    if not files or args.workers < 1 or args.output.exists():
        raise ValueError("Expected Event inputs, positive workers and a new result directory")
    args.output.mkdir(parents=True)
    results, failures = [], []
    with ProcessPoolExecutor(max_workers=min(args.workers, len(files))) as pool:
        futures = {pool.submit(run_event, path, args.output): path for path in files}
        for future in as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(path.stem, "completed", result["weather_polls"], flush=True)
            except Exception as error:
                failures.append({"event": path.stem, "error": repr(error)})
                print(path.stem, repr(error), flush=True)
            save(
                args.output / "status.json",
                {
                    "completed": len(results),
                    "failures": failures,
                    "worker_limit": args.workers,
                    "active_slots": min(args.workers, len(files)),
                },
            )
    save(
        args.output / "summary.json",
        {"events": sorted(results, key=lambda r: r["date"]), "failures": failures},
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
