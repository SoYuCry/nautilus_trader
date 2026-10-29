"""Download Binance hourly closes and plot the US ADR / Korean share premium.

Requires matplotlib. Run: python scripts/hynix_premium.py
Uses completed bars only, exact timestamp alignment, and no forward filling.
"""

import csv
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt


OUT = Path(__file__).resolve().parents[1] / "research" / "hynix_premium"
HOUR = 3_600_000


def fetch(symbol, start, end):
    rows = []
    cursor = start
    while cursor < end:
        query = urllib.parse.urlencode(dict(
            symbol=symbol, interval="1h", startTime=cursor,
            endTime=end - 1, limit=1000,
        ))
        url = "https://fapi.binance.com/fapi/v1/klines?" + query
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
            raise ValueError(f"Unexpected response: {batch}")
        if not batch:
            break
        if batch[-1][0] < cursor:
            raise ValueError("Pagination did not advance")
        rows.extend(batch)
        cursor = batch[-1][0] + HOUR
    (OUT / f"{symbol}_raw.json").write_text(json.dumps(rows), encoding="utf-8")
    result = {}
    for row in rows:
        if start <= row[0] < end and row[6] < end:
            if row[0] in result or float(row[4]) <= 0:
                raise ValueError("Duplicate timestamp or non-positive close")
            result[row[0]] = float(row[4])
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    end = int(now.timestamp() * 1000) // HOUR * HOUR
    # Include the first partial listing hour (SKHY launched at 15:50 UTC).
    start = int(datetime(2026, 7, 10, 15, tzinfo=timezone.utc).timestamp() * 1000)
    symbols = ["SKHYUSDT", "SKHYNIXUSDT"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        us, kr = list(pool.map(lambda s: fetch(s, start, end), symbols))
    expected = list(range(start, end, HOUR))
    common = sorted(us.keys() & kr.keys())
    if not common:
        raise ValueError("No aligned observations")
    premium = {t: (10 * us[t] / kr[t] - 1) * 100 for t in common}
    with (OUT / "premium_hourly.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["bar_open_utc", "bar_close_utc", "skhy_close_usdt", "skhynix_close_usdt", "us_adr_premium_pct"])
        for t in common:
            writer.writerow([datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(),
                             datetime.fromtimestamp((t + HOUR) / 1000, timezone.utc).isoformat(),
                             us[t], kr[t], premium[t]])
    dates = [datetime.fromtimestamp((t + HOUR) / 1000, timezone.utc) for t in expected]
    nan = float("nan")
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 7.5), sharex=True,
                                  gridspec_kw={"height_ratios": [1, 1.5]})
    fig.suptitle("SK hynix | US ADR vs Korean equity perpetuals", fontsize=18, x=.08, ha="left")
    ax1.plot(dates, [us.get(t, nan) * 10 for t in expected], label="10 x SKHYUSDT (US ADR)", color="#2463b4", lw=1.3)
    ax1.plot(dates, [kr.get(t, nan) for t in expected], label="SKHYNIXUSDT (Korea)", color="#e58b2b", lw=1.3)
    ax1.set_ylabel("USDT / ordinary-share equivalent")
    ax1.legend(loc="upper left", frameon=False, ncol=2)
    values = [premium.get(t, nan) for t in expected]
    ax2.plot(dates, values, color="#2463b4", lw=1.2)
    ax2.axhline(0, color="#555555", lw=.8)
    ax2.set_ylabel("US ADR premium (%)")
    ax2.set_xlabel("Hourly bar close (UTC)")
    locator = mdates.AutoDateLocator()
    ax2.xaxis.set_major_locator(locator)
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for ax in (ax1, ax2):
        ax.grid(alpha=.18)
        ax.axvline(datetime(2026, 8, 7, 2, tzinfo=timezone.utc), color="#999999", ls="--", lw=1)
    ax2.text(datetime(2026, 8, 7, 2, tzinfo=timezone.utc), .03,
             " KRW conversion update", transform=ax2.get_xaxis_transform(), fontsize=9)
    ordered = sorted(premium.values())
    n = len(ordered)
    median = (ordered[(n - 1) // 2] + ordered[n // 2]) / 2
    stats = dict(retrieved_at_utc=now.isoformat(), branch="codex/research-hynix-premium",
                 expected_bars=len(expected), aligned_bars=len(common),
                 missing_us=len(expected) - len(us), missing_kr=len(expected) - len(kr),
                 first_bar_close_utc=datetime.fromtimestamp((common[0] + HOUR) / 1000, timezone.utc).isoformat(),
                 last_bar_close_utc=datetime.fromtimestamp((common[-1] + HOUR) / 1000, timezone.utc).isoformat(),
                 latest_premium_pct=premium[common[-1]], median_premium_pct=median,
                 min_premium_pct=min(ordered), max_premium_pct=max(ordered),
                 formula="100 * (10 * SKHYUSDT.close / SKHYNIXUSDT.close - 1)")
    fig.text(.08, .025, f"Last {stats['latest_premium_pct']:.2f}%  |  Median {median:.2f}%  |  Range {min(ordered):.2f}% to {max(ordered):.2f}%\n"
             "Trade-price ratio; pre-Aug 7 FX methodology differs. First listing bar is partial. Excludes funding and costs.", fontsize=10, color="#555555")
    fig.tight_layout(rect=(0, .08, 1, .95))
    fig.savefig(OUT / "premium.png", dpi=170)
    fig.savefig(OUT / "premium.svg")
    (OUT / "summary.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2), flush=True)


if __name__ == "__main__":
    main()
