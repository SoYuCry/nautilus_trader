"""Filter immutable hourly Parquet into small weather-only daily replay shards."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
from pathlib import Path
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from fetch_inputs import ROOT, save
from inventory import DATA

COLS=['event_type','timestamp_received','sequence','timestamp','market','asset_id','best_bid','best_ask','bids','asks','price','size','side','fee_rate_bps']
TYPES=['book','best_bid_ask','last_trade_price']

def mapping():
    result={}
    for event in json.loads((ROOT/'events.json').read_text(encoding='utf8')):
        for c in event['conditions']:
            if c['upper'] is None:continue
            intervals=[]
            for i,w in enumerate(event['weather']):
                end=min(w['expires'],event['end'],event['weather'][i+1]['time'] if i+1<len(event['weather']) else event['end'])
                start=max(w['time'],event['start'])
                if w['maximum']>c['upper'] and end>start:intervals.append((start,end))
            if intervals:result[int(c['no_token']).to_bytes(32,'big')]=(intervals,bytes.fromhex(c['condition_id'][2:]))
    return result

def one(path,maps):
    out=ROOT/'sampled'/path.name
    if out.exists():return
    hour=dt.datetime.strptime(path.stem,'%Y-%m-%dT%H')
    start=int(hour.replace(tzinfo=dt.timezone.utc).timestamp()*1e9)
    end=start+3_600_000_000_000
    selected={t:m for t,(intervals,m) in maps.items() if any(a<end+5_000_000_000 and b>start for a,b in intervals)}
    markets=sorted(set(selected.values()))
    import bisect
    f=pq.ParquetFile(path,pre_buffer=True)
    groups=[]
    for i in range(f.num_row_groups):
        g=f.metadata.row_group(i)
        s=g.column(0).statistics
        if s and s.has_min_max and not any(s.min<=x<=s.max for x in TYPES):continue
        s=g.column(4).statistics
        if s and s.has_min_max:
            j=bisect.bisect_left(markets,s.min)
            if j==len(markets) or markets[j]>s.max:continue
        groups.append(i)
    out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_suffix('.writing')
    writer=None
    count=0
    ids=pa.array(list(selected),type=pa.binary())
    for index,i in enumerate(groups):
        # Read narrow identifiers before expensive depth columns; skip irrelevant groups.
        keys=f.read_row_group(i,columns=['asset_id'],use_threads=False)
        mask=pc.is_in(keys['asset_id'],value_set=ids)
        if not pc.any(mask).as_py():continue
        stats=f.metadata.row_group(i).column(0).statistics
        columns=COLS if not stats or stats.min!=stats.max or stats.min=='book' else [c for c in COLS if c not in ['bids','asks']]
        t=f.read_row_group(i,columns=columns,use_threads=False).filter(mask)
        for c in ['bids','asks']:
            if c not in t.column_names:t=t.append_column(c,pa.nulls(len(t),type=f.schema_arrow.field(c).type))
        t=t.select(COLS)
        t=t.filter(pc.is_in(t['event_type'],value_set=pa.array(TYPES)))
        if not len(t):continue
        if writer is None:writer=pq.ParquetWriter(temp,t.schema,compression='zstd')
        writer.write_table(t)
        count+=len(t)
        if index%50==0:print(path.stem,'group',index,'of',len(groups),'rows',count,flush=True)
    if writer:
        writer.close()
        temp.replace(out)
    else:
        pq.write_table(pa.Table.from_batches([],schema=pa.schema([f.schema_arrow.field(c) for c in COLS])),out)
    save(out.with_suffix('.json'),{'source':str(path),'rows':count,'candidate_groups':len(groups),'total_groups':f.num_row_groups})
    print(path.stem,count,flush=True)

if __name__=='__main__':
    maps=mapping()
    print('tokens',len(maps),flush=True)
    if not maps:raise RuntimeError('No Gamma mappings')
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f in as_completed([pool.submit(one,p,maps) for p in sorted(DATA.glob('*/*/*.parquet')) if p.stem>='2026-10-15T10']):f.result()
    save(ROOT/'extraction-complete.json',{'status':'completed'})
