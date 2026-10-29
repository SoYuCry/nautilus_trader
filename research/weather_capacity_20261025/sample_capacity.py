"""Daily AWC capacity estimates at full-depth snapshots validated by a near-time BBO.

No snapshot volume summation: each condition contributes once to each daily scenario.
First opportunity and best sampled opportunity are separate additive timing scenarios.
"""
from bisect import bisect_right
from collections import Counter
import csv
import datetime as dt
import json
import math
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from fetch_inputs import ROOT, save
from replay import measure, record, ZERO

MAX_QUOTE_LAG_NS=5_000_000_000

def prune_snapshot(asks,bid,ask):
    if bid is None or ask is None or not 0<=bid<ask<=1:return None,0
    boundary=int(ask*10000)
    kept={p:q for p,q in asks.items() if p>=boundary}
    return kept,len(asks)-len(kept)

def signal_at(event,t):
    if not event['start']<=t<event['end']:return None
    i=bisect_right(event['weather_times'],t)-1
    if i<0:return None
    w=event['weather'][i]
    return w['maximum'] if t<w['expires'] else None

def csv_save(name,rows):
    save(ROOT/'results'/f'{name}.json',rows)
    if rows:
        with (ROOT/'results'/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    events=json.loads((ROOT/'events.json').read_text(encoding='utf8'))
    states={};counts=Counter();last_data=None;first_data=None
    for e in events:
        e['weather_times']=[w['time'] for w in e['weather']]
        for c in e['conditions']:
            states[int(c['no_token']).to_bytes(32,'big')]={**c,'event':e,'pending':None,'snapshots':0,'validated':0,'weather_samples':0,'eligible_samples':0,'first':None,'first_time':None,'peak':ZERO,'peak_time':None,'trade':ZERO,'pruned':0,'quote_lags':[],'sample_start':None,'sample_end':None}

    def evaluate(s,pending,at,bid,ask):
        asks,n=prune_snapshot(pending['asks'],bid,ask)
        if asks is None:return False
        s['validated']+=1;s['pruned']+=n;counts['deleted_ask_levels']+=n
        s['quote_lags'].append((at-pending['time'])/1e9)
        e=s['event'];maximum=signal_at(e,at)
        if maximum is None:return True
        s['weather_samples']+=1
        s['sample_start']=at if s['sample_start'] is None else s['sample_start'];s['sample_end']=at
        if s['upper'] is None or maximum<=s['upper']:return True
        s['eligible_samples']+=1
        v=measure(asks,s['rate'],s['minimum'])
        if v[0]>0:
            if s['first'] is None:s['first']=v;s['first_time']=at
            if v[1]+v[2]>s['peak'][1]+s['peak'][2]:s['peak']=v;s['peak_time']=at
        return True

    seen_files=set()
    while True:
        # Can start while extraction runs: only read a contiguous sequence of complete shards.
        paths=sorted((ROOT/'sampled').glob('*.parquet'))
        manifests=sorted((ROOT/'inputs'/'listings').glob('*.json'))
        expected=[p.stem for p in manifests if p.stem>='2026-10-15T10']
        available={p.stem:p for p in paths}
        progress=False
        for stamp in expected:
            if stamp in seen_files:continue
            if stamp not in available:break
            path=available[stamp];table=pq.read_table(path)
            table=table.sort_by([('timestamp_received','ascending'),('sequence','ascending')])
            table=table.set_column(table.schema.get_field_index('timestamp_received'),'timestamp_received',table['timestamp_received'].cast(pa.int64()))
            for batch in table.to_batches(max_chunksize=16384):
                for r in batch.to_pylist():
                    t=r['timestamp_received']*1000
                    first_data=t if first_data is None else min(first_data,t);last_data=t if last_data is None else max(last_data,t)
                    s=states.get(r['asset_id'])
                    if s is None:continue
                    counts[r['event_type']]+=1
                    if r['event_type']=='book':
                        s['snapshots']+=1
                        asks={int(x['price']*10000):int(x['size']*1000000) for x in (r['asks'] or []) if x['size']>0}
                        s['pending']={'time':t,'asks':asks}
                        if evaluate(s,s['pending'],t,r['best_bid'],r['best_ask']):s['pending']=None
                    elif r['event_type']=='best_bid_ask' and s['pending'] is not None:
                        pending=s['pending']
                        if t-pending['time']>MAX_QUOTE_LAG_NS:s['pending']=None;counts['expired_snapshot_quotes']+=1
                        elif t>=pending['time'] and evaluate(s,pending,t,r['best_bid'],r['best_ask']):s['pending']=None
                    elif r['event_type']=='last_trade_price':
                        # Supplemental realized turnover, never relabeled as executable capacity.
                        maximum=signal_at(s['event'],t)
                        if maximum is not None and s['upper'] is not None and maximum>s['upper'] and r['price'] is not None and r['size'] is not None:
                            v=measure({int(r['price']*10000):int(r['size']*1000000)},s['rate'],0)
                            s['trade']=tuple(a+b for a,b in zip(s['trade'],v))
            seen_files.add(stamp);progress=True
            print(stamp,len(table),'validated',sum(s['validated'] for s in states.values()),flush=True)
            save(ROOT/'sample-status.json',{'hours':len(seen_files),'expected':len(expected),'last_hour':stamp,'counts':counts})
        if len(seen_files)==len(expected):break
        if (ROOT/'extraction-complete.json').exists():
            missing=set(expected)-seen_files
            raise RuntimeError(f'Missing extracted shards: {sorted(missing)}')
        if not progress:
            import time;time.sleep(5)

    conditions=[]
    for s in states.values():
        e=s['event']
        never_eligible=s['upper'] is None or not any(w['maximum']>s['upper'] and w['time']<min(w['expires'],e['end']) for w in e['weather'])
        status='estimated' if s['weather_samples'] or never_eligible else 'no_validated_snapshot_with_weather'
        if s['rate'] is None or s['minimum'] is None:status='unknown_fee_or_minimum'
        first=record(s['first'] or ZERO,s['first_time']);peak=record(s['peak'],s['peak_time'])
        if status!='estimated':
            first={k:None for k in first};peak={k:None for k in peak}
        for val in (first,peak):
            val['average_price']=val['notional_pusd']/val['shares'] if val['shares'] else None
        conditions.append({'date':e['day'],'city':e['city'],'condition_id':s['condition_id'],'bucket':s['bucket'],'station':e['station'],'unit':e['unit'],'status':status,'fee_rate_scenario':s['rate'],'snapshots':s['snapshots'],'validated_snapshots':s['validated'],'weather_samples':s['weather_samples'],'eligible_samples':s['eligible_samples'],'deleted_ask_levels':s['pruned'],'mean_quote_lag_seconds':sum(s['quote_lags'])/len(s['quote_lags']) if s['quote_lags'] else None,'sample_start_ns':s['sample_start'],'sample_end_ns':s['sample_end'],**{'first_'+k:v for k,v in first.items()},**{'best_sample_'+k:v for k,v in peak.items()},**{'turnover_proxy_'+k:v for k,v in record(s['trade']).items()}})
    conditions.sort(key=lambda r:(r['date'],r['city'],r['condition_id']))
    csv_save('conditions',conditions)

    def aggregate(rows,**keys):
        out={**keys,'conditions':len(rows),'covered_conditions':sum(r['status']=='estimated' for r in rows),'opportunity_conditions':sum(bool(r['first_shares']) for r in rows),'coverage':'complete_sample_coverage' if all(r['status']=='estimated' for r in rows) else 'partial_sample_coverage'}
        for prefix in ['first','best_sample','turnover_proxy']:
            for k in ['shares','notional_pusd','fee_pusd','investment_pusd','conditional_profit_pusd']:
                out[f'{prefix}_{k}']=sum(r[f'{prefix}_{k}'] or 0 for r in rows)
            q=out[f'{prefix}_shares'];out[f'{prefix}_average_price']=out[f'{prefix}_notional_pusd']/q if q else None
        return out
    cities=[];daily=[]
    for day in sorted({r['date'] for r in conditions}):
        rows=[r for r in conditions if r['date']==day]
        daily.append(aggregate(rows,date=day,cities=len({r['city'] for r in rows})))
        for city in sorted({r['city'] for r in rows}):cities.append(aggregate([r for r in rows if r['city']==city],date=day,city=city))
    csv_save('cities',cities);csv_save('daily',daily)
    save(ROOT/'results'/'audit.json',{'counts':counts,'first_received_ns':first_data,'last_received_ns':last_data,'hours':len(seen_files),'max_snapshot_to_quote_lag_seconds':5,'method':'One first and one best BBO-validated full snapshot per condition; independent timing scenarios; neither sums repeated snapshots nor claims synchronized peaks. Historical quantities can change during the <=5 second quote validation lag.','availability':'AWC receiptTime proxy, not locally recorded polling receipt. Assumes continuously available polling; no historical poll error/TTL replay.','fees':'Retrieved Gamma feeSchedule scenario; historical fee effectiveness unverified.','settlement':'Conditional profit assumes NO payout 1; no actual realized profit claim.'})

if __name__=='__main__':main()
