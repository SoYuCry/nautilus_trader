"""Finite-capital averaging-down replay using only completed minute signals."""
import csv
import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "research/hynix_premium"
MINUTE = 60000
COST = .0007
LEG_ADD = 1000
MAX_LEVELS = 5
TARGET = .005


def iso(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).isoformat()


def run(us, kr, funds, start_date="2026-07-13", config=None):
    config = config or {}
    target_rate = config.get("target", TARGET)
    reversal = config.get("reversal_pp", 0)
    wait_minutes = config.get("wait_minutes", 0)
    add_spacing = config.get("add_spacing_pp", 1)
    add_cooldown = config.get("add_cooldown_minutes", 0)
    confirm_add = config.get("confirm_add", False)
    times = sorted(us)
    assert times == sorted(kr)
    assert all(b - a == MINUTE for a, b in zip(times, times[1:]))
    openings = {}
    holidays = {"KR": {"2026-07-17", "2026-08-17"}, "US": {"2026-09-07"}}
    day = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = times[-1] + MINUTE
    while day.timestamp() * 1000 < end:
        for market, h, m in [("KR", 0, 0), ("US", 13, 30)]:
            op = int(day.replace(hour=h, minute=m).timestamp() * 1000)
            if day.weekday() < 5 and day.date().isoformat() not in holidays[market] and op + 60 * MINUTE <= end:
                openings[op] = market
        day += timedelta(days=1)
    premium = {t: 100 * (10 * float(us[t][4]) / float(kr[t][4]) - 1) for t in times}
    funding = sorted((int(r["fundingTime"]), side, float(r["markPrice"]), float(r["fundingRate"]))
                     for side, rows in enumerate(funds) for r in rows)
    fi = 0
    position = pending = event = None
    event_high = event_low = None
    used_events = set()
    completed, additions, equity_rows = [], [], []
    realized = 0
    peak = 10000
    max_dd = 0
    max_gross = 0
    min_equity = 10000
    worst_open = 0
    max_levels = 0
    exhausted = False

    def valuation(p, u, k):
        gross = p["direction"] * (p["entry_us_notional"] - p["qu"] * u + p["qk"] * k - p["entry_kr_notional"])
        exit_cost = COST * (p["qu"] * u + p["qk"] * k)
        return gross, exit_cost, gross + p["funding"] - p["entry_cost"] - exit_cost

    def apply_funding(boundary):
        nonlocal fi
        while fi < len(funding) and funding[fi][0] <= boundary:
            ft, side, mark, rate = funding[fi]
            if position is not None and position["entry_t"] < ft:
                assert mark > 0
                sign = position["direction"] * (1 if side == 0 else -1)
                position["funding"] += sign * position["qu" if side == 0 else "qk"] * mark * rate
            fi += 1

    def close_record(p, t, u, k, reason):
        gross, exit_cost, net = valuation(p, u, k)
        return dict(entry_utc=iso(p["entry_t"]), exit_or_mark_utc=iso(t),
                    market=p["market"], direction=p["direction"], layers=p["levels"],
                    holding_hours=(t - p["entry_t"]) / 3600000,
                    first_premium_pct=p["first_premium"], exit_premium_pct=100 * (10 * u / k - 1),
                    gross_usdt=gross, funding_usdt=p["funding"],
                    entry_cost_usdt=p["entry_cost"], exit_cost_usdt=exit_cost,
                    net_usdt=net, target_usdt=target_rate * 2 * LEG_ADD * p["levels"],
                    worst_open_net_usdt=p["worst"], reason=reason)

    for t in times:
        apply_funding(t)  # Boundary funding paid before orders at the same boundary.
        u, k = float(us[t][1]), float(kr[t][1])
        if pending is not None:
            action, details = pending
            if action == "close":
                row = close_record(position, t, u, k, "profit_signal")
                completed.append(row)
                realized += row["net_usdt"]
                position = None
            else:
                if action == "entry":
                    direction, market, event_t = details
                    position = dict(entry_t=t, direction=direction, market=market,
                                    first_premium=100 * (10 * u / k - 1),
                                    qu=0, qk=0, entry_us_notional=0, entry_kr_notional=0,
                                    entry_cost=0, funding=0, levels=0, worst=0)
                    used_events.add(event_t)
                position["qu"] += LEG_ADD / u
                position["qk"] += LEG_ADD / k
                position["entry_us_notional"] += LEG_ADD
                position["entry_kr_notional"] += LEG_ADD
                position["entry_cost"] += 2 * LEG_ADD * COST
                position["levels"] += 1
                position["last_add_t"] = t
                position["extreme"] = 100 * (10 * u / k - 1)
                max_levels = max(max_levels, position["levels"])
                additions.append(dict(time_utc=iso(t), cycle_entry_utc=iso(position["entry_t"]),
                                      layer=position["levels"], action=action, us_price=u, kr_price=k))
            pending = None
        if t in openings:
            # Only pre-event closes; fixed throughout the event.
            baseline = statistics.median(premium[x] for x in range(t - 60 * MINUTE, t, MINUTE))
            event = (t, openings[t], baseline)
            event_high = event_low = premium[t]
        if event is not None and t < event[0] + 60 * MINUTE:
            event_high = max(event_high, premium[t])
            event_low = min(event_low, premium[t])
        u, k = float(us[t][4]), float(kr[t][4])
        net = 0
        if position is not None:
            gross, exit_cost, net = valuation(position, u, k)
            position["worst"] = min(position["worst"], net)
            worst_open = min(worst_open, net)
            max_gross = max(max_gross, position["qu"] * u + position["qk"] * k)
            direction = position["direction"]
            position["extreme"] = max(position["extreme"], premium[t]) if direction == 1 else min(position["extreme"], premium[t])
            add_reversed = (direction * (position["extreme"] - premium[t]) >= reversal
                            and direction * (premium[t] - premium[t - 3 * MINUTE]) < 0) if confirm_add else True
            target = target_rate * 2 * LEG_ADD * position["levels"]
            if net >= target:
                pending = ("close", None)
            elif (position["levels"] < MAX_LEVELS and net < 0
                  and direction * (premium[t] - position["first_premium"]) >= position["levels"] * add_spacing
                  and t + MINUTE - position["last_add_t"] >= add_cooldown * MINUTE
                  and add_reversed):
                pending = ("add", None)
        elif event is not None and not exhausted:
            event_t, market, baseline = event
            if event_t + wait_minutes * MINUTE <= t < event_t + 60 * MINUTE and event_t not in used_events:
                dev = premium[t] - baseline
                direction = 1 if dev > 0 else -1
                extreme = event_high if direction == 1 else event_low
                entry_reversed = (direction * (extreme - premium[t]) >= reversal
                                  and direction * (premium[t] - premium[t - 3 * MINUTE]) < 0) if reversal else True
                if abs(dev) >= 1 and entry_reversed:
                    pending = ("entry", (1 if dev > 0 else -1, market, event_t))
        equity = 10000 + realized + net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        min_equity = min(min_equity, equity)
        if equity <= 0:
            exhausted = True
            break
        if t % (60 * MINUTE) == 0 or t == times[-1]:
            equity_rows.append(dict(time_utc=iso(t + MINUTE), estimated_liquidation_equity=equity,
                                    realized_pnl=realized, open_net_pnl=net,
                                    levels=position["levels"] if position else 0))
    apply_funding(t + MINUTE)
    open_record = close_record(position, t + MINUTE, u, k, "sample_end_mark") if position else None
    open_net = open_record["net_usdt"] if open_record else 0
    terminal_equity = 10000 + realized + open_net
    min_equity = min(min_equity, terminal_equity)
    max_dd = max(max_dd, peak - terminal_equity)
    total_gross = sum(r["gross_usdt"] for r in completed) + (open_record["gross_usdt"] if open_record else 0)
    total_funding = sum(r["funding_usdt"] for r in completed) + (open_record["funding_usdt"] if open_record else 0)
    total_cost = sum(r["entry_cost_usdt"] + r["exit_cost_usdt"] for r in completed)
    if open_record:
        total_cost += open_record["entry_cost_usdt"] + open_record["exit_cost_usdt"]
    assert abs(realized + open_net - (total_gross + total_funding - total_cost)) < 1e-7
    summary = dict(completed_cycles=len(completed), profitable_closed_cycles=sum(r["net_usdt"] > 0 for r in completed),
                   realized_net_usdt=realized, open_net_usdt=open_net,
                   total_net_usdt=realized + open_net, return_pct=(realized + open_net) / 100,
                   gross_price_pnl_usdt=total_gross, funding_usdt=total_funding, costs_usdt=total_cost,
                   max_drawdown_usdt=max_dd, min_equity_usdt=min_equity,
                   worst_open_net_usdt=worst_open, max_layers=max_levels, max_mark_gross_notional=max_gross,
                   addition_orders=sum(r["action"] == "add" for r in additions),
                   max_holding_hours=max([r["holding_hours"] for r in completed] + ([open_record["holding_hours"]] if open_record else [0])),
                   capital_exhausted=exhausted, open_position=open_record, completed=completed)
    return summary, additions, equity_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-07-13", help="First event date (UTC)")
    parser.add_argument("--suffix", default="", help="Output filename suffix")
    args = parser.parse_args()
    maps = [{r[0]: r for r in json.loads((OUT / f"{s}_1m_full.json").read_text())} for s in ["SKHYUSDT", "SKHYNIXUSDT"]]
    first = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    maps = [{t: r for t, r in x.items() if t >= first - 60 * MINUTE} for x in maps]
    assert maps[0] and min(maps[0]) == first - 60 * MINUTE, "Missing pre-start baseline"
    funds = [json.loads((OUT / f"{s}_funding.json").read_text(encoding="utf-8-sig")) for s in ["SKHYUSDT", "SKHYNIXUSDT"]]
    summary, adds, equity = run(*maps, funds, start_date=args.start)
    summary["start_date_utc"] = args.start
    (OUT / f"average_down_summary{args.suffix}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for name, rows in [("average_down_orders.csv", adds), ("average_down_equity.csv", equity), ("average_down_cycles.csv", summary["completed"] + ([summary["open_position"]] if summary["open_position"] else []))]:
        name = Path(name).stem + args.suffix + ".csv"
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    print(json.dumps({k: v for k, v in summary.items() if k != "completed"}, indent=2))


if __name__ == "__main__":
    main()
