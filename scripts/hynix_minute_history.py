"""Build a complete minute snapshot using public archives and REST gap filling."""
import csv
import io
import json
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "research/hynix_premium"
CACHE = OUT / "minute_full_cache"
SYMBOLS = ["SKHYUSDT", "SKHYNIXUSDT"]
MINUTE = 60000


def get_bytes(url):
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.read()
        except Exception:
            if attempt:
                raise
            time.sleep(.2)


def archive(job):
    symbol, period, frequency = job
    path = CACHE / f"{symbol}-1m-{period}.zip"
    if not path.exists():
        url = f"https://data.binance.vision/data/futures/um/{frequency}/klines/{symbol}/1m/{path.name}"
        blob = get_bytes(url)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.testzip()
        path.write_bytes(blob)
    with zipfile.ZipFile(path) as z:
        rows = list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")))
    result = []
    for r in rows:
        if not r or not r[0].isdigit():
            continue
        r[0], r[6] = int(r[0]), int(r[6])
        if r[0] > 10**14:
            r[0] //= 1000
            r[6] //= 1000
        result.append(r)
    return result


def gap(job):
    symbol, start, end = job
    path = CACHE / f"{symbol}_{start}_{end}.json"
    if path.exists():
        return json.loads(path.read_text())
    query = urllib.parse.urlencode(dict(symbol=symbol, interval="1m", startTime=start,
                                       endTime=end - 1, limit=1500))
    rows = json.loads(get_bytes("https://fapi.binance.com/fapi/v1/klines?" + query))
    if not isinstance(rows, list):
        raise ValueError(rows)
    path.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    end = int(datetime.fromisoformat(json.loads((OUT / "summary.json").read_text())["last_bar_close_utc"]).timestamp() * 1000)
    start = int(datetime(2026, 7, 12, 23, tzinfo=timezone.utc).timestamp() * 1000)
    data = {s: {} for s in SYMBOLS}
    for symbol in SYMBOLS:
        for path in (OUT / "minute_events").glob(f"{symbol}_*.json"):
            for r in json.loads(path.read_text(encoding="utf-8-sig")):
                data[symbol][r[0]] = r
    jobs = [(s, month, "monthly") for s in SYMBOLS for month in ["2026-07", "2026-08"]]
    jobs += [(s, f"2026-09-{day:02d}", "daily") for s in SYMBOLS for day in range(1, 11)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(archive, j): j for j in jobs}
        for future in as_completed(futures):
            j = futures[future]
            try:
                data[j[0]].update({r[0]: r for r in future.result()})
                print(f"Archive ready {j[0]} {j[1]}", flush=True)
            except Exception:
                print(f"Archive unavailable {j[0]} {j[1]}; use REST", flush=True)
    # Reuse previously completed REST chunks on subsequent attempts.
    for symbol in SYMBOLS:
        for path in CACHE.glob(f"{symbol}_*.json"):
            data[symbol].update({r[0]: r for r in json.loads(path.read_text())})
    jobs = []
    for symbol in SYMBOLS:
        cursor = start
        while cursor < end:
            if cursor in data[symbol]:
                cursor += MINUTE
                continue
            first = cursor
            while cursor < end and cursor not in data[symbol] and cursor - first < 1500 * MINUTE:
                cursor += MINUTE
            jobs.append((symbol, first, cursor))
    print(f"REST gaps {len(jobs)}", flush=True)
    errors = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(gap, j): j for j in jobs}
        for future in as_completed(futures):
            j = futures[future]
            try:
                data[j[0]].update({r[0]: r for r in future.result()})
            except Exception as exc:
                errors.append((j, str(exc)))
    missing = {s: sum(t not in data[s] for t in range(start, end, MINUTE)) for s in SYMBOLS}
    print(json.dumps(dict(missing=missing, errors=errors)), flush=True)
    if any(missing.values()):
        raise RuntimeError("Incomplete history; rerun to retry cached gaps")
    for symbol in SYMBOLS:
        rows = [data[symbol][t] for t in range(start, end, MINUTE)]
        assert all(int(r[6]) < end and float(r[1]) > 0 and float(r[4]) > 0 for r in rows)
        (OUT / f"{symbol}_1m_full.json").write_text(json.dumps(rows), encoding="utf-8")
    print(f"Complete: {len(rows)} minute bars per leg", flush=True)


if __name__ == "__main__":
    main()
