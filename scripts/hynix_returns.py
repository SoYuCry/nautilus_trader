"""Illustrative mean-reversion calculation, not an optimized trading strategy."""
import csv
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "research" / "hynix_premium"
HOUR = 3600000
COST = .0007  # assumed 5bp fee + 2bp slippage per leg per execution
CAPITAL = 10000


def funding(symbol, start, end):
    rows = []
    cursor = start
    while cursor <= end:
        url = "https://fapi.binance.com/fapi/v1/fundingRate?" + urllib.parse.urlencode(
            dict(symbol=symbol, startTime=cursor, endTime=end, limit=1000))
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    batch = json.load(response)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1)
        if not isinstance(batch, list):
            raise ValueError(batch)
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            raise ValueError("Funding pagination stalled")
        cursor = next_cursor
    (OUT / f"{symbol}_funding.json").write_text(json.dumps(rows), encoding="utf-8")
    return rows


def iso(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).isoformat()


def main():
    symbols = ["SKHYUSDT", "SKHYNIXUSDT"]
    raw = [json.loads((OUT / f"{s}_raw.json").read_text()) for s in symbols]
    maps = [{r[0]: r for r in rows} for rows in raw]
    times = sorted(maps[0].keys() & maps[1].keys())
    assert all(b - a == HOUR for a, b in zip(times, times[1:])), "Missing bars"
    start, end = times[0], times[-1] + HOUR
    if "--cached-funding" in sys.argv:
        funds = [json.loads((OUT / f"{s}_funding.json").read_text(encoding="utf-8-sig")) for s in symbols]
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            funds = list(pool.map(lambda s: funding(s, start, end), symbols))
    for records in funds:
        assert records and isinstance(records, list), "No funding data"
        regular = sorted(int(r["fundingTime"]) for r in records if r.get("rateType", "Regular") == "Regular")
        assert regular[0] - start <= 8 * HOUR and end - regular[-1] <= 8 * HOUR, "Funding coverage incomplete"
        assert all(b - a <= 8 * HOUR + 1000 for a, b in zip(regular, regular[1:])), "Funding gap"
    closes = [[float(m[t][4]) for t in times] for m in maps]
    prem = [10 * u / k - 1 for u, k in zip(*closes)]
    # Signal uses preceding 168 observations, then execution at next bar open.
    trades = []
    position = None
    zvalues = []
    for i, t in enumerate(times):
        if i < 168:
            zvalues.append(None)
            continue
        window = prem[i - 168:i]
        sd = statistics.stdev(window)
        z = (prem[i] - statistics.mean(window)) / sd if sd else 0
        zvalues.append(z)
        if position is not None and (z <= 0 or i - position[0] >= 168 or i == len(times) - 1):
            entry_i, entry_t, entry_us, entry_kr = position
            exit_t = t + HOUR
            final = i == len(times) - 1
            exit_us = closes[0][i] if final else float(maps[0][times[i + 1]][1])
            exit_kr = closes[1][i] if final else float(maps[1][times[i + 1]][1])
            qu, qk = 5000 / entry_us, 5000 / entry_kr
            gross = qu * (entry_us - exit_us) + qk * (exit_kr - entry_kr)
            carry = 0
            for side, qty, records in [(1, qu, funds[0]), (-1, qk, funds[1])]:
                for row in records:
                    ft = int(row["fundingTime"])
                    # Positions opened at a funding boundary exclude that event;
                    # positions exiting on a boundary include it, conservatively.
                    if entry_t < ft <= exit_t:
                        mark = float(row["markPrice"])
                        if mark <= 0:
                            raise ValueError("Missing funding mark price")
                        carry += side * qty * mark * float(row["fundingRate"])
            cost = COST * (10000 + qu * exit_us + qk * exit_kr)
            net = gross + carry - cost
            trades.append(dict(entry_utc=iso(entry_t), exit_utc=iso(exit_t),
                               hours=(exit_t - entry_t) / HOUR,
                               entry_premium_pct=(10 * entry_us / entry_kr - 1) * 100,
                               exit_premium_pct=(10 * exit_us / exit_kr - 1) * 100,
                               gross_usdt=gross, funding_usdt=carry, cost_usdt=cost,
                               net_usdt=net, net_return_pct=net / CAPITAL * 100,
                               exit_reason="sample_end" if final else "mean" if z <= 0 else "time_limit"))
            position = None
        elif position is None and z >= 2 and i < len(times) - 1:
            nt = times[i + 1]
            position = (i + 1, nt, float(maps[0][nt][1]), float(maps[1][nt][1]))
    with (OUT / "illustrative_trades.csv").open("w", newline="", encoding="utf-8") as f:
        if trades:
            writer = csv.DictWriter(f, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
    p0 = prem[-1]
    scenarios = []
    for target in [statistics.median(prem), .35, .30, 0, .50]:
        gross = 5000 * (p0 - target) / (1 + p0)
        cost = COST * (15000 + 5000 * (1 + target) / (1 + p0))
        scenarios.append(dict(target_premium_pct=100 * target,
                              gross_usdt=gross, cost_usdt=cost,
                              net_before_funding_usdt=gross - cost,
                              return_before_funding_pct=(gross - cost) / CAPITAL * 100))
    carry_estimates = {}
    for days in [7, 30]:
        net = 0
        counts = []
        for sign, records in [(1, funds[0]), (-1, funds[1])]:
            selected = [r for r in records if end - days * 24 * HOUR < int(r["fundingTime"]) <= end]
            counts.append(len(selected))
            net += sign * 5000 * sum(float(r["fundingRate"]) for r in selected)
        carry_estimates[f"last_{days}d"] = dict(fixed_notional_net_usdt=net, events_per_leg=counts,
                                                daily_usdt=net / days)
    result = dict(capital_usdt=CAPITAL, initial_notional_each_leg=5000,
                  assumed_fee_bps=5, assumed_slippage_bps=2,
                  funding_counts=[len(f) for f in funds],
                  latest_premium_pct=p0 * 100, scenarios=scenarios,
                  recent_funding_fixed_notional=carry_estimates,
                  illustrative_backtest=dict(trades=len(trades),
                    winners=sum(r["net_usdt"] > 0 for r in trades),
                    gross_usdt=sum(r["gross_usdt"] for r in trades),
                    funding_usdt=sum(r["funding_usdt"] for r in trades),
                    costs_usdt=sum(r["cost_usdt"] for r in trades),
                    net_usdt=sum(r["net_usdt"] for r in trades),
                    return_on_initial_capital_pct=sum(r["net_usdt"] for r in trades) / CAPITAL * 100),
                  trade_details=trades)
    (OUT / "returns_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
