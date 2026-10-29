import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from decimal import Decimal
import pyarrow.parquet as pq

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
OUT = BASE / 'lifecycle_appendix'
OUT.mkdir(exist_ok=True)
manifest = []

def copy(src, name):
    target = OUT / name
    shutil.copy2(src, target)
    manifest.append({'file': name, 'source': str(src), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})

def default(v):
    if isinstance(v, bytes): return '0x' + v.hex()
    if isinstance(v, datetime): return v.isoformat()
    if isinstance(v, Decimal): return str(v)
    raise TypeError(type(v).__name__)

def save(name, rows):
    (OUT / (name + '.json')).write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=default), encoding='utf8')
    if rows:
        with (OUT / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows([{k: default(v) if isinstance(v, (bytes, datetime, Decimal)) else v for k, v in r.items()} for r in rows])

for name in ['refined.json', 'refined.csv', 'estimates.json', 'estimates.csv', 'resolution-state-beijing16.json']:
    copy(BASE / name, name)
for name in ['trades.json', 'trades.csv', 'all_windows.json', 'all_windows.csv', 'windows_with_trades.json', 'windows_with_trades.csv', 'REPORT.md']:
    copy(ROOT / 'live_validation_2526/live/results/halfhour' / name, 'halfhour_' + name)
copy(ROOT / 'live_validation_2526/live/events.json', 'live_awc_events.json')
markets = {}
for city in ['beijing', 'shanghai']:
    name = f'highest-temperature-in-{city}-on-october-26-2026.json'
    src = ROOT / 'beijing_shanghai_20261025_26_delay120/inputs/gamma' / name
    copy(src, name)
    events = json.loads(src.read_text(encoding='utf8'))
    if isinstance(events, dict): events = [events]
    for event in events:
        for m in event['markets']:
            markets[m['conditionId']] = (city, m)

refined = json.loads((BASE / 'refined.json').read_text(encoding='utf8'))
ends = {
    'beijing': ['00:23:04.130','00:23:04.250','00:23:04.527','00:23:03.995',None,'00:23:04.381'],
    'shanghai': ['00:23:01.270','00:23:01.104','00:23:01.547','00:23:01.420','00:23:00.445','00:23:00.674'],
}
positions = {'beijing':0, 'shanghai':0}
orders = []
for r in refined:
    city = r['city']; idx = positions[city]; positions[city] += 1
    m = markets[r['condition_id']][1]
    end = ends[city][idx]
    orders.append({'city':city,'bucket':r['bucket'],'condition_id':r['condition_id'],
        'side':'BUY','outcome':'NO','price':'0.999','shares':'5',
        'ack_beijing':r['ack_beijing'],'last_status_screenshot':'Expired' if end else 'Accepted',
        'terminal_beijing':f'2026-10-27T{end}+08:00' if end else None,
        'gamma_closed_time_original':m.get('closedTime'),'gamma_custom_liveness_seconds':m.get('customLiveness'),
        'same_price_depth_shares':r['replayed_bid999_shares'],
        'order_source':'user screenshot transcription; not native exchange order log'})
save('orders_and_gamma', orders)
save('gamma_conditions', [{'city':city, **{k:m.get(k) for k in ['conditionId','questionID','groupItemTitle','closedTime','endDate','closed','acceptingOrders','umaResolutionStatus','customLiveness','negRisk','negRiskRequestID']}} for city,m in markets.values()])

raw = Path(r'C:\Projects\PolyReaper\data\external\pendulumflow\v3_1012-latest\2026-10-26\16\2026-10-26T16.parquet')
pf = pq.ParquetFile(raw)
event_idx = pf.schema_arrow.get_field_index('event_type')
rows = []
for i in range(pf.num_row_groups):
    stats = pf.metadata.row_group(i).column(event_idx).statistics
    if stats and stats.has_min_max and not (stats.min <= 'market_resolved' <= stats.max): continue
    for r in pf.read_row_group(i, columns=['event_type','timestamp','timestamp_received','market','winning_outcome']).to_pylist():
        cid = default(r['market']) if isinstance(r['market'],bytes) else r['market']
        if r['event_type'] != 'market_resolved' or cid not in markets: continue
        r['condition_id'] = cid
        r['city'] = markets[cid][0]
        rows.append(r)
save('market_resolved_raw', rows)
manifest.append({'file':'market_resolved_raw.json / .csv','source':str(raw),'filter':'event_type=market_resolved; conditionId in saved Beijing/Shanghai 2026-10-26 Gamma markets','rows':len(rows)})
image = Path(r'C:\Users\xhth\AppData\Local\Temp\codex-clipboard-uJsxOb.png')
if image.exists(): copy(image, 'orders_screenshot.png')
for file in OUT.iterdir():
    if file.is_file() and file.name != 'manifest.json' and not any(x['file']==file.name for x in manifest):
        manifest.append({'file':file.name,'source':'derived by archive_lifecycle.py','sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
(OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
print(json.dumps({'orders':len(orders),'resolved_records':len(rows),'files':len(list(OUT.iterdir())), 'liveness_values':sorted({str(r['gamma_custom_liveness_seconds']) for r in orders})}))
