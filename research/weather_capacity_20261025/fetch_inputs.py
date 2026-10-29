"""Freeze public AWC and Gamma responses for the daily capacity study."""
import concurrent.futures as cf
import datetime as dt
import json
from pathlib import Path
import re
import threading
import time
import requests

ROOT = Path(__file__).resolve().parent
CATALOG = Path(r'C:\Projects\PolyReaper\research\2026-09-18-polymarket-weather-city-history\station-catalog.json')
LOCK = threading.Lock()
NEXT = 0.0

def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.writing')
    temp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding='utf8')
    for attempt in range(6):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt==5:raise
            time.sleep(0.1*2**attempt)

def get(url, params, path, awc=False):
    global NEXT
    if path.exists():
        return json.loads(path.read_text(encoding='utf8'))
    for attempt in range(4):
        if awc:
            with LOCK:
                pause = max(0, NEXT - time.monotonic())
                NEXT = max(NEXT, time.monotonic()) + 0.8
            time.sleep(pause)
        try:
            r = requests.get(url, params=params, timeout=45, headers={'User-Agent':'WeatherCapacityResearch/1.0'})
            r.raise_for_status()
            body = r.json() if r.content else []
            save(path, body)
            save(path.with_suffix('.request.json'), {'url':r.url,'retrieved_at':dt.datetime.now(dt.timezone.utc).isoformat(),'status':r.status_code})
            return body
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2**attempt)

def weather(station, end, hours=72):
    path = ROOT/'inputs'/'awc'/station/f'{end:%Y%m%dT%H}-{hours}h.json'
    rows = get('https://aviationweather.gov/api/data/metar', {'ids':station,'format':'json','date':end.isoformat(),'hours':hours}, path, True)
    if len(rows) >= 400:
        if hours <= 1:
            raise ValueError('AWC response still truncated at one hour')
        return weather(station,end,hours/2)+weather(station,end-dt.timedelta(hours=hours/2),hours/2)
    return len(rows)

def gamma(slug):
    return get('https://gamma-api.polymarket.com/events',{'slug':slug},ROOT/'inputs'/'gamma'/f'{slug}.json')

def main():
    catalog=json.loads(CATALOG.read_text(encoding='utf8'))
    save(ROOT/'inputs'/'station-catalog.json',catalog)
    cities={re.search(r'highest-temperature-in-(.+)-on-',v['event_slugs'][0])[1] for v in catalog['stations'].values()}
    cities.add('hong-kong')
    tasks=[]
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for station in catalog['stations']:
            for day in [14,17,20,23,25]:
                tasks.append(pool.submit(weather,station,dt.datetime(2026,10,day,tzinfo=dt.timezone.utc),72 if day!=25 else 48))
        for city in sorted(cities):
            for day in range(12,24):
                tasks.append(pool.submit(gamma,f'highest-temperature-in-{city}-on-october-{day}-2026'))
        failures=[]
        for n,f in enumerate(cf.as_completed(tasks),1):
            try: f.result()
            except Exception as e: failures.append(repr(e))
            if n%20==0 or n==len(tasks):
                save(ROOT/'inputs'/'fetch-status.json',{'done':n,'total':len(tasks),'failures':failures})
                print(n,len(tasks),'failures',len(failures),flush=True)

if __name__=='__main__': main()
