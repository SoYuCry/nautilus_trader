"""Observed NO trade turnover from first AWC trigger through close/data cutoff."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import csv
import datetime as dt
import json
from pathlib import Path
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from fetch_inputs import ROOT,save
from inventory import DATA

OUT=ROOT/'results'/'post_signal_volume'
COLS=['event_type','timestamp_received','timestamp','sequence','market','asset_id','price','size','side','transaction_hash']

def stamp(s):
    return int(dt.datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000000)

def configs():
    metadata={}
    for p in (ROOT/'inputs/gamma').glob('*.json'):
        if p.name.endswith('.request.json'):continue
        for e in json.loads(p.read_text(encoding='utf8')):
            for m in e['markets']:metadata[m['conditionId']]=m
    states={}
    for e in json.loads((ROOT/'events.json').read_text(encoding='utf8')):
        bottom=min(c['upper'] for c in e['conditions'] if c['upper'] is not None)
        for c in e['conditions']:
            times=[max(w['time'],e['start']) for w in e['weather'] if c['upper'] is not None and w['maximum']>c['upper'] and max(w['time'],e['start'])<min(w['expires'],e['end'])]
            m=metadata[c['condition_id']]
            close=stamp(m['closedTime']) if m.get('closedTime') else None
            states[int(c['no_token']).to_bytes(32,'big')]={'date':e['day'],'city':e['city'],'condition_id':c['condition_id'],'bucket':c['bucket'],'is_lowest_temperature_bucket':c['upper']==bottom,'trigger_us':min(times)//1000 if times else None,'close_us':close,'rate':c['rate'],'trade_count':0,'shares':0.,'notional_pusd':0.,'eligible_shares':0.,'eligible_notional_pusd':0.,'eligible_fee_pusd':0.,'first_trade_us':None,'last_trade_us':None}
    return states

def extract(path,states):
    out=ROOT/'trade_only'/path.name
    if out.exists():return out
    hour=dt.datetime.strptime(path.stem,'%Y-%m-%dT%H').replace(tzinfo=dt.timezone.utc)
    start=int(hour.timestamp()*1000000);end=start+3600_000000
    tokens=[t for t,s in states.items() if s['trigger_us'] is not None and s['trigger_us']<end and (s['close_us'] is None or s['close_us']>=start)]
    f=pq.ParquetFile(path,pre_buffer=True)
    groups=[]
    for i in range(f.num_row_groups):
        stats=f.metadata.row_group(i).column(0).statistics
        if tokens and (not stats or not stats.has_min_max or stats.min<='last_trade_price'<=stats.max):groups.append(i)
    if groups:
        table=f.read_row_groups(groups,columns=COLS,use_threads=False)
        table=table.filter(pc.and_(pc.equal(table['event_type'],'last_trade_price'),pc.is_in(table['asset_id'],value_set=pa.array(tokens,type=pa.binary()))))
    else:table=pa.Table.from_batches([],schema=pa.schema([f.schema_arrow.field(c) for c in COLS]))
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix('.writing');pq.write_table(table,tmp,compression='zstd');tmp.replace(out)
    return out

def output(name,rows):
    save(OUT/f'{name}.json',rows)
    with (OUT/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    states=configs()
    paths=sorted(p for p in DATA.glob('*/*/*.parquet') if p.stem>='2026-10-15T10')
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(extract,p,states) for p in paths]):future.result()
    # Hourly file coverage, including holes, independently of whether trades occurred.
    coverage=[]
    for p in paths:
        h=int(dt.datetime.strptime(p.stem,'%Y-%m-%dT%H').replace(tzinfo=dt.timezone.utc).timestamp()*1000000)
        coverage.append((h,h+3600_000000))
    cutoff=max(b for a,b in coverage)
    seen=set();audit=Counter()
    for path in paths:
        table=pq.read_table(ROOT/'trade_only'/path.name)
        for r in table.to_pylist():
            s=states[r['asset_id']]
            t=int((r['timestamp'] or r['timestamp_received']).timestamp()*1000000)
            if s['trigger_us'] is None or t<s['trigger_us'] or (s['close_us'] is not None and t>s['close_us']):continue
            if r['price'] is None or r['size'] is None or r['size']<=0:continue
            # Best-effort duplicate WS trade reports; hash+fill fields rather than hash alone.
            key=(r['asset_id'],r['transaction_hash'] or r['sequence'],t,r['price'],r['size'],r['side'])
            if key in seen:audit['duplicate_trade_reports']+=1;continue
            seen.add(key)
            if r['timestamp'] is None:audit['receive_time_fallbacks']+=1
            p=float(r['price']);q=float(r['size'])
            s['trade_count']+=1;s['shares']+=q;s['notional_pusd']+=p*q
            s['first_trade_us']=min(s['first_trade_us'],t) if s['first_trade_us'] is not None else t
            s['last_trade_us']=max(s['last_trade_us'],t) if s['last_trade_us'] is not None else t
            rate=s['rate']
            if rate is not None and 0<p<=.999 and 1-p-rate*p*(1-p)-.00001>=-1e-12:
                s['eligible_shares']+=q;s['eligible_notional_pusd']+=p*q;s['eligible_fee_pusd']+=q*rate*p*(1-p)
    rows=[]
    for s in states.values():
        start=s['trigger_us'];end=min(s['close_us'] or cutoff,cutoff)
        duration=sum(max(0,min(b,end)-max(a,start)) for a,b in coverage) if start is not None else 0
        missing=max(0,end-start-duration) if start is not None else 0
        s['coverage']='no_signal' if start is None else 'observed_through_close' if s['close_us'] is not None and s['close_us']<=cutoff and not missing else 'partial_to_data_cutoff_or_gaps'
        s['missing_hours']=missing/3600_000000
        s['vwap']=s['notional_pusd']/s['shares'] if s['shares'] else None
        s['eligible_vwap']=s['eligible_notional_pusd']/s['eligible_shares'] if s['eligible_shares'] else None
        s['eligible_conditional_profit_pusd']=s['eligible_shares']-s['eligible_notional_pusd']-s['eligible_fee_pusd']
        for key in ['trigger_us','close_us','first_trade_us','last_trade_us']:
            s[key.replace('_us','_utc')]=dt.datetime.fromtimestamp(s[key]/1e6,dt.timezone.utc).isoformat() if s[key] is not None else None
        rows.append(s)
    rows.sort(key=lambda r:(r['date'],r['city'],r['condition_id']))
    output('conditions',rows)
    def aggregate(rr,**keys):
        result={**keys,'conditions':len(rr),'triggered_conditions':sum(r['trigger_us'] is not None for r in rr),'partial_conditions':sum(r['coverage'].startswith('partial') for r in rr)}
        for k in ['trade_count','shares','notional_pusd','eligible_shares','eligible_notional_pusd','eligible_fee_pusd','eligible_conditional_profit_pusd']:
            result[k]=sum(r[k] for r in rr)
            result[k+'_excluding_lowest']=sum(r[k] for r in rr if not r['is_lowest_temperature_bucket'])
        result['vwap']=result['notional_pusd']/result['shares'] if result['shares'] else None
        result['eligible_vwap']=result['eligible_notional_pusd']/result['eligible_shares'] if result['eligible_shares'] else None
        return result
    daily=[];cities=[]
    for day in sorted({r['date'] for r in rows}):
        rr=[r for r in rows if r['date']==day]
        daily.append(aggregate(rr,date=day))
        for city in sorted({r['city'] for r in rr}):cities.append(aggregate([r for r in rr if r['city']==city],date=day,city=city))
    output('daily',daily);output('cities',cities)
    for d in daily:
        assert abs(sum(c['notional_pusd'] for c in cities if c['date']==d['date'])-d['notional_pusd'])<1e-5
    save(OUT/'audit.json',{'counts':audit,'hour_files':len(paths),'cutoff_hour_utc':dt.datetime.fromtimestamp(cutoff/1e6,dt.timezone.utc).isoformat(),'method':'NO token trade notifications from first AWC trigger to Gamma closedTime or observed cutoff. No later weather expiry/day boundary filtering. All prices for raw turnover; separate strategy-price-eligible subset. Best-effort dedup by token/hash/exchange timestamp/price/size/side; equal fills in the same transaction cannot be distinguished without a trade ID. No YES+NO summation. Missing hourly files are reported; completeness within each file unverified.'})
    print(json.dumps(daily,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
