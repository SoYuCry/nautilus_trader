"""Unlimited NO depth capacity, deletion-only BBO pruning, no repeated snapshot sums."""
import csv
import heapq
import json
from collections import Counter
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from fetch_inputs import ROOT, save

ZERO=(0.,0.,0.,0.) # shares, notional, fee, conditional profit

def measure(asks, rate, minimum):
    if rate is None or minimum is None:return ZERO
    q=cost=fee=0.
    for price,size in asks.items():
        p=price/10000
        if 0<p<=.999 and size>0 and 1-p-rate*p*(1-p)-.00001>=-1e-12:
            v=size/1e6;q+=v;cost+=p*v;fee+=rate*p*(1-p)*v
    return (q,cost,fee,q-cost-fee) if q>=minimum else ZERO

def apply(s,r):
    kind=r['event_type']; bids=s['bids'];asks=s['asks']
    if kind=='book':
        bids.clear();asks.clear()
        for key,book in [('bids',bids),('asks',asks)]:
            for x in r[key] or []:
                size=int(x['size']*1000000)
                if size>0:book[int(x['price']*10000)]=size
        s['initialized']=True
    elif kind=='price_change' and r['price'] is not None and r['size'] is not None:
        if r['side'] not in ['BUY','SELL']:raise ValueError('Unknown side')
        book=bids if r['side']=='BUY' else asks
        price=int(r['price']*10000);size=int(r['size']*1000000)
        if size>0:book[price]=size
        else:book.pop(price,None)
    # Supplied deletion-only rule: absent/incomplete/crossed quotes do not prune.
    bid,ask=r['best_bid'],r['best_ask']
    deleted=0
    if bid is not None and ask is not None and 0<=bid<ask<=1:
        bid=int(bid*10000);ask=int(ask*10000)
        for book,prices in [(bids,[p for p in bids if p>bid]),(asks,[p for p in asks if p<ask])]:
            for p in prices:del book[p];deleted+=1
    return deleted

def record(v,t=None):
    return {'shares':v[0],'notional_pusd':v[1],'fee_pusd':v[2],'investment_pusd':v[1]+v[2],'conditional_profit_pusd':v[3], 'time_ns':t}

def main():
    events=json.loads((ROOT/'events.json').read_text(encoding='utf8'))
    states={}; es={}; daily={};queue=[];serial=0;counts=Counter();dirty=set();dirty_days=set()
    for e in events:
        e.update(value=ZERO,peak=ZERO,peak_time=None,maximum=None,expiry=0,first=ZERO,integral=[0.]*4,valid_ns=0,last=e['start'],tokens=[])
        es[e['slug']]=e
        daily.setdefault(e['day'],{'value':ZERO,'peak':ZERO,'peak_time':None})
        for c in e['conditions']:
            token=int(c['no_token']).to_bytes(32,'big')
            states[token]={**c,'event':e,'bids':{},'asks':{},'initialized':False,'value':ZERO,'peak':ZERO,'peak_time':None,'first':None,'first_time':None,'rows':0}
            e['tokens'].append(token)
        for w in e['weather']:
            if w['time']<e['end'] and w['expires']>w['time']:
                serial+=1;heapq.heappush(queue,(w['time'],serial,e['slug'],'weather',w))
                serial+=1;heapq.heappush(queue,(min(w['expires'],e['end']),serial,e['slug'],'expiry',w['expires']))
        serial+=1;heapq.heappush(queue,(e['end'],serial,e['slug'],'end',None))

    def integrate(e,t):
        until=min(max(t,e['start']),e['end'])
        if until>e['last']:
            if e['maximum'] is not None:
                duration=until-e['last'];e['valid_ns']+=duration
                e['integral']=[a+v*duration for a,v in zip(e['integral'],e['value'])]
            e['last']=until

    def recompute(s,t):
        e=s['event'];integrate(e,t)
        old=s['value'];v=ZERO
        if e['start']<=t<e['end'] and e['maximum'] is not None and t<e['expiry'] and s['upper'] is not None and e['maximum']>s['upper'] and s['initialized']:
            v=measure(s['asks'],s['rate'],s['minimum'])
        s['value']=v
        if v[1]+v[2]>s['peak'][1]+s['peak'][2]:s['peak']=v;s['peak_time']=t
        if v[0]>0 and s['first'] is None:s['first']=v;s['first_time']=t
        e['value']=tuple(a+b-c for a,b,c in zip(e['value'],v,old))
        d=daily[e['day']];d['value']=tuple(a+b-c for a,b,c in zip(d['value'],v,old))
        dirty.add(e['slug']);dirty_days.add(e['day'])

    def peaks(t):
        for slug in dirty:
            e=es[slug]
            if e['value'][1]+e['value'][2]>e['peak'][1]+e['peak'][2]+1e-8:e['peak']=e['value'];e['peak_time']=t
        for day in dirty_days:
            d=daily[day]
            if d['value'][1]+d['value'][2]>d['peak'][1]+d['peak'][2]+1e-8:d['peak']=d['value'];d['peak_time']=t
        dirty.clear();dirty_days.clear()

    def weather_until(t):
        while queue and queue[0][0]<=t:
            at=queue[0][0]
            while queue and queue[0][0]==at:
                _,_,slug,kind,w=heapq.heappop(queue);e=es[slug];integrate(e,at)
                if kind=='weather':e['maximum']=w['maximum'];e['expiry']=w['expires']
                elif kind=='end' or e['expiry']==w:e['maximum']=None
                for token in e['tokens']:recompute(states[token],at)
            peaks(at)

    for path in sorted((ROOT/'filtered').glob('*.parquet')):
        table=pq.read_table(path)
        table=table.sort_by([('timestamp_received','ascending'),('sequence','ascending')])
        # Datetime conversion would lose ns; original receive timestamps are microseconds.
        table=table.set_column(table.schema.get_field_index('timestamp_received'),'timestamp_received',table['timestamp_received'].cast(pa.int64()))
        last=None
        for batch in table.to_batches(max_chunksize=32768):
            for r in batch.to_pylist():
                t=r['timestamp_received']*1000
                if last is not None and t!=last:peaks(last)
                weather_until(t);last=t
                s=states.get(r['asset_id'])
                if s is None:continue
                s['rows']+=1
                if r['event_type']=='last_trade_price':continue
                counts['deleted_levels']+=apply(s,r);counts[r['event_type']]+=1
                recompute(s,t)
        if last is not None:peaks(last)
        print(path.stem,len(table),'pruned',counts['deleted_levels'],flush=True)
        save(ROOT/'replay-status.json',{'last_hour':path.stem,'counts':counts})
    while queue:weather_until(queue[0][0])
    conditions=[];cities=[];totals=[]
    for s in states.values():
        e=s['event'];conditions.append({'date':e['day'],'city':e['city'],'condition_id':s['condition_id'],'bucket':s['bucket'],'station':e['station'],'status':'no_weather' if not e['weather'] else 'no_book' if not s['initialized'] else 'unknown_fee_or_minimum' if s['rate'] is None or s['minimum'] is None else 'estimated','book_rows':s['rows'],**{'first_'+k:v for k,v in record(s['first'] or ZERO,s['first_time']).items()},**{'peak_'+k:v for k,v in record(s['peak'],s['peak_time']).items()}})
    for e in es.values():
        first=tuple(sum((states[t]['first'] or ZERO)[j] for t in e['tokens']) for j in range(4))
        e['first']=first
        cities.append({'date':e['day'],'city':e['city'],'station':e['station'],'conditions':len(e['tokens']),'weather_reports':len(e['weather']),'valid_weather_hours':e['valid_ns']/3.6e12,**{'first_'+k:v for k,v in record(first).items()},**{'peak_'+k:v for k,v in record(e['peak'],e['peak_time']).items()},**{'mean_'+k:v for k,v in record(tuple(x/e['valid_ns'] for x in e['integral']) if e['valid_ns'] else ZERO).items()}})
    for day,d in daily.items():
        selected=[e for e in events if e['day']==day]
        first=tuple(sum(e['first'][j] for e in selected) for j in range(4))
        totals.append({'date':day,'cities':len(selected),'conditions':sum(len(e['tokens']) for e in selected),**{'first_'+k:v for k,v in record(first).items()},**{'peak_'+k:v for k,v in record(d['peak'],d['peak_time']).items()}})
    for name,rows in [('conditions',conditions),('cities',cities),('daily',totals)]:
        rows.sort(key=lambda r:(r['date'],r.get('city',''),r.get('condition_id','')))
        save(ROOT/'results'/f'{name}.json',rows)
        with (ROOT/'results'/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    save(ROOT/'results'/'audit.json',dict(counts))

if __name__=='__main__':main()
