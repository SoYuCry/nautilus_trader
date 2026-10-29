"""Fixed-rule, next-minute-open event replay; hourly research snapshot bounds."""
import csv
import json
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "research/hynix_premium"
CACHE = OUT / "minute_events"
MINUTE = 60000
SYMBOLS = ["SKHYUSDT", "SKHYNIXUSDT"]


def download(job):
    symbol, op = job
    path = CACHE / f"{symbol}_{op}.json"
    if path.exists():
        rows = json.loads(path.read_text())
    else:
        params = dict(symbol=symbol, interval="1m", startTime=op - 60 * MINUTE,
                      endTime=op + 182 * MINUTE - 1, limit=499)
        url = "https://fapi.binance.com/fapi/v1/klines?" + urllib.parse.urlencode(params)
        for attempt in range(2):
            try:
                with urllib.request.urlopen(url, timeout=8) as response:
                    rows = json.load(response)
                break
            except Exception:
                if attempt:
                    raise
                time.sleep(.3)
        if not isinstance(rows, list):
            raise ValueError(str(rows))
        path.write_text(json.dumps(rows), encoding="utf-8")
    mapped = {r[0]: r for r in rows}
    expected = set(range(op - 60 * MINUTE, op + 182 * MINUTE, MINUTE))
    if set(mapped) != expected or len(rows) != len(expected):
        raise ValueError(f"Incomplete minute window: {len(rows)}/242")
    return mapped


def iso(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).isoformat()


def replay(market, op, us, kr, funding):
    def premium(t, field=4):
        return 100 * (10 * float(us[t][field]) / float(kr[t][field]) - 1)

    base = statistics.median(premium(t) for t in range(op - 60 * MINUTE, op, MINUTE))
    for signal in range(op, op + 60 * MINUTE, MINUTE):
        deviation = premium(signal) - base
        if abs(deviation) < 1:
            continue
        direction = 1 if deviation > 0 else -1  # 1: short US, long KR
        entry = signal + MINUTE
        ue, ke = float(us[entry][1]), float(kr[entry][1])
        ep = premium(entry, 1)
        reason = "timeout"
        for check in range(entry, entry + 120 * MINUTE, MINUTE):
            p = premium(check)
            if direction * (p - base) <= .1:
                reason = "revert"
                break
            if direction * (p - ep) >= 1:
                reason = "stop"
                break
        exit_t = check + MINUTE
        ux, kx = float(us[exit_t][1]), float(kr[exit_t][1])
        qu, qk = 5000 / ue, 5000 / ke
        gross = direction * (qu * (ue - ux) + qk * (kx - ke))
        turnover = 10000 + qu * ux + qk * kx
        fees, slip = turnover * .0005, turnover * .0002
        carry = 0
        for side, qty, records in [(direction, qu, funding[0]), (-direction, qk, funding[1])]:
            for r in records:
                if entry < int(r["fundingTime"]) <= exit_t:
                    mark = float(r["markPrice"])
                    assert mark > 0
                    carry += side * qty * mark * float(r["fundingRate"])
        # Close-sampled adverse excursion, including entry costs, not tick worst case.
        worst = min(direction * (qu * (ue - float(us[t][4])) + qk * (float(kr[t][4]) - ke))
                    for t in range(entry, exit_t, MINUTE))
        assert signal < entry < exit_t and exit_t - entry <= 120 * MINUTE
        return dict(market=market, event_utc=iso(op), baseline_pct=base,
                    signal_utc=iso(signal + MINUTE), entry_utc=iso(entry), exit_utc=iso(exit_t),
                    direction="short_US_long_KR" if direction == 1 else "long_US_short_KR",
                    entry_premium_pct=ep, exit_premium_pct=premium(exit_t, 1),
                    minutes=(exit_t - entry) / MINUTE, reason=reason,
                    gross_usdt=gross, funding_usdt=carry, fees_usdt=fees,
                    slippage_usdt=slip, net_usdt=gross + carry - fees - slip,
                    worst_close_price_pnl_usdt=worst)
    return None


def summarize(trades):
    pnl = [r["net_usdt"] for r in trades]
    equity = peak = 10000
    drawdown = 0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(trades=len(trades), winners=sum(p > 0 for p in pnl),
                net_usdt=sum(pnl), return_pct=sum(pnl) / 100,
                gross_usdt=sum(r["gross_usdt"] for r in trades),
                funding_usdt=sum(r["funding_usdt"] for r in trades),
                costs_usdt=sum(r["fees_usdt"] + r["slippage_usdt"] for r in trades),
                average_net_usdt=statistics.mean(pnl) if pnl else None,
                worst_trade_usdt=min(pnl) if pnl else None,
                best_trade_usdt=max(pnl) if pnl else None,
                trade_close_drawdown_usdt=drawdown,
                exits={k: sum(r["reason"] == k for r in trades) for k in ["revert", "stop", "timeout"]})


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    snapshot = json.loads((OUT / "summary.json").read_text())
    end = int(datetime.fromisoformat(snapshot["last_bar_close_utc"]).timestamp() * 1000)
    funds = [json.loads((OUT / f"{s}_funding.json").read_text(encoding="utf-8-sig")) for s in SYMBOLS]
    events = []
    day = datetime(2026, 7, 13, tzinfo=timezone.utc)
    # All sample dates are in US daylight saving time: 09:30 ET = 13:30 UTC.
    holidays = {"KR": {"2026-07-17", "2026-08-17"}, "US": {"2026-09-07"}}
    while int(day.timestamp() * 1000) < end:
        for market, hour, minute in [("KR", 0, 0), ("US", 13, 30)]:
            op = int(day.replace(hour=hour, minute=minute).timestamp() * 1000)
            if day.weekday() < 5 and day.date().isoformat() not in holidays[market] and op + 182 * MINUTE <= end:
                events.append((market, op))
        day += timedelta(days=1)
    jobs = [(s, op) for _, op in events for s in SYMBOLS]
    data, errors = {}, {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(download, job): job for job in jobs}
        for count, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            try:
                data[job] = future.result()
            except Exception as exc:
                errors[str(job)] = str(exc)
            if count % 20 == 0:
                print(f"Minute windows {count}/{len(jobs)}; failed {len(errors)}", flush=True)
    trades, missing, no_signal = [], [], 0
    for market, op in events:
        if any((s, op) not in data for s in SYMBOLS):
            missing.append(dict(market=market, event=iso(op)))
            continue
        result = replay(market, op, data[(SYMBOLS[0], op)], data[(SYMBOLS[1], op)], funds)
        if result:
            trades.append(result)
        else:
            no_signal += 1
    split = datetime(2026, 8, 15, tzinfo=timezone.utc).isoformat()
    report = dict(planned_events=len(events), missing_events=missing, no_signal_events=no_signal,
                  download_errors=errors, split_utc=split,
                  all=summarize(trades),
                  earlier=summarize([r for r in trades if r["entry_utc"] < split]),
                  later=summarize([r for r in trades if r["entry_utc"] >= split]),
                  by_market={m: summarize([r for r in trades if r["market"] == m]) for m in ["KR", "US"]})
    (OUT / "open_reversion_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (OUT / "open_reversion_trades.csv").open("w", newline="", encoding="utf-8") as f:
        if trades:
            writer = csv.DictWriter(f, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
