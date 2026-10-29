"""Four fixed mechanism variants, selection on Aug 20-31 only."""
import csv
import json
from datetime import datetime, timezone

from hynix_average_down import MINUTE, OUT, run

CONFIGS = {
    "baseline": {},
    "confirm": {"reversal_pp": .2, "wait_minutes": 5},
    "confirm_spaced": {"reversal_pp": .2, "wait_minutes": 5,
                       "add_spacing_pp": 1.5, "add_cooldown_minutes": 30, "confirm_add": True},
    "confirm_spaced_quick": {"reversal_pp": .2, "wait_minutes": 5,
                             "add_spacing_pp": 1.5, "add_cooldown_minutes": 30,
                             "confirm_add": True, "target": .003},
}


def stamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


def main():
    maps = [{r[0]: r for r in json.loads((OUT / f"{s}_1m_full.json").read_text())}
            for s in ["SKHYUSDT", "SKHYNIXUSDT"]]
    funds = [json.loads((OUT / f"{s}_funding.json").read_text(encoding="utf-8-sig"))
             for s in ["SKHYUSDT", "SKHYNIXUSDT"]]

    def evaluate(start, end, cfg):
        sliced = [{t: r for t, r in x.items() if stamp(start) - 60 * MINUTE <= t < end} for x in maps]
        return run(*sliced, funds, start_date=start, config=cfg)

    training = {}
    for name, cfg in CONFIGS.items():
        result, _, _ = evaluate("2026-08-20", stamp("2026-09-01"), cfg)
        training[name] = {k: v for k, v in result.items() if k not in ["completed", "open_position"]}
        training[name]["selection_score"] = result["total_net_usdt"] - .5 * result["max_drawdown_usdt"]
    selected = max(training, key=lambda name: training[name]["selection_score"])
    end = max(maps[0]) + MINUTE
    results = {}
    for name in dict.fromkeys(["baseline", selected]):
        cfg = CONFIGS[name]
        full, orders, equity = evaluate("2026-08-20", end, cfg)
        later, _, _ = evaluate("2026-09-01", end, cfg)
        results[name] = dict(full=full, september_fresh_account=later)
        for label, rows in [("orders", orders), ("equity", equity),
                            ("cycles", full["completed"] + ([full["open_position"]] if full["open_position"] else []))]:
            with (OUT / f"v2_{name}_{label}.csv").open("w", newline="", encoding="utf-8") as f:
                if rows:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
    report = dict(configs=CONFIGS, selection_period="2026-08-20 to 2026-08-31 UTC",
                  selection_rule="net P&L including open positions minus 0.5 * maximum equity drawdown",
                  selected=selected, training=training, results=results)
    (OUT / "optimization_v2.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    compact = dict(selected=selected,
                   training={n: {k: r[k] for k in ["total_net_usdt", "max_drawdown_usdt", "completed_cycles", "selection_score"]} for n, r in training.items()},
                   results={n: {part: {k: r[k] for k in ["total_net_usdt", "realized_net_usdt", "open_net_usdt", "max_drawdown_usdt", "completed_cycles", "max_holding_hours", "max_layers"]} for part, r in parts.items()} for n, parts in results.items()})
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
