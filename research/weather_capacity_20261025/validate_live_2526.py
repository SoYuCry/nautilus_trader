"""Compare recorded local AWC availability with historical +120s on identical books."""
import csv
import datetime as dt
from decimal import Decimal,ROUND_FLOOR
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor,as_completed
from zoneinfo import ZoneInfo
import extract_books
import sample_capacity
import post_signal_volume
from prepare import routine_temperature
from fetch_inputs import ROOT,save

BASE=ROOT/'live_validation_2526'
OLD=ROOT/'beijing_shanghai_20261025_26_delay120'
BUNDLE=ROOT/'live-awc-20261025-26-compact'
sys.path.insert(0,str(BUNDLE))
from replay_awc import iter_events,verify

def iso_ns(text):
    x=dt.datetime.fromisoformat(text.replace('Z','+00:00'))
    epoch=dt.datetime(1970,1,1,tzinfo=dt.timezone.utc)
    delta=x-epoch
    return (delta.days*86400+delta.seconds)*1000000000+delta.microseconds*1000

def normalize(rows,e,t):
    selected={}
    for r in rows:
        if r.get('icaoId')!=e['station']:continue
        obs=int(r['obsTime'])*1000000000
        if dt.datetime.fromtimestamp(obs//1000000000,ZoneInfo(e['timezone'])).date().isoformat()!=e['day']:continue
        if obs>t:raise ValueError('future observation')
        raw=r.get('rawOb','')
        if raw.split()[:1]==['SPECI']:continue
        temp=routine_temperature(raw,e['station'])
        receipt=iso_ns(r['receiptTime'])
        if obs not in selected or receipt>selected[obs][0]:selected[obs]=(receipt,temp)
        elif receipt==selected[obs][0] and temp!=selected[obs][1]:raise ValueError('conflicting weather')
    selected={k:v for k,v in selected.items() if v[1] is not None}
    if not selected:raise ValueError('no observation')
    latest=max(selected)
    if t-latest>4500_000000000:raise ValueError('stale observation')
    maximum=max(v[1] for v in selected.values())
    if e['unit']=='F':maximum=maximum*9/5+32
    maximum=int((maximum+Decimal('.5')).to_integral_value(rounding=ROUND_FLOOR))
    return maximum,min(t+120_000000000,latest+4500_000000000,e['end'])

def files_link(source,target):
    target.mkdir(parents=True,exist_ok=True)
    for p in source.glob('*'):
        if p.is_file() and not (target/p.name).exists():os.link(p,target/p.name)

def prepare_live():
    save(BASE/'bundle-verification.json',verify(BUNDLE))
    historical=json.loads((OLD/'events.json').read_text(encoding='utf8'))
    live=json.loads(json.dumps(historical));by_slug={e['slug']:e for e in live}
    records=[x for day in ['2026-10-25','2026-10-26'] for x in iter_events(day)]
    exits={x['run_id']:x['record']['recorded_at_ns'] for x in records if x['kind']=='runtime_exit'}
    starts={}
    for x in records:
        slug=x.get('event_id')
        if slug in by_slug and x['kind']=='metar_poll':
            k=(slug,x['run_id']);starts[k]=min(starts.get(k,2**63),x['record']['received_ns'])
    summary=[];delays=[];matches=[]
    history={}
    for p in (OLD/'inputs/awc').glob('*/*.json'):
        if p.name.endswith('.request.json'):continue
        for r in json.loads(p.read_text(encoding='utf8')):history.setdefault((r['icaoId'],int(r['obsTime'])),[]).append(r)
    unique={};first_seen={}
    for e in live:
        changes=[];errors=[];polls=[]
        for x in records:
            if x.get('event_id')!=e['slug']:continue
            r=x['record'];kind=x['kind']
            if kind=='metar_poll':
                t=r['received_ns'];polls.append(t)
                try:maximum,expiry=normalize(r['rows'],e,t)
                except (ValueError,KeyError,TypeError) as err:
                    maximum=-1000;expiry=t;errors.append({'time':t,'reason':str(err)})
                end=exits.get(x['run_id'],e['end'])
                later=[v for (slug,run),v in starts.items() if slug==e['slug'] and run!=x['run_id'] and v>starts[e['slug'],x['run_id']]]
                if later:end=min(end,min(later))
                changes.append({'time':t,'maximum':maximum,'expires':min(expiry,end),'run_id':x['run_id']})
                for row in r['rows']:
                    if row.get('icaoId')!=e['station']:continue
                    key=(row['icaoId'],int(row['obsTime']),row['receiptTime'],row.get('rawOb'))
                    unique[key]=row
                    first_seen.setdefault((x['run_id'],key),t)
            elif kind=='metar_error':
                # Errors may carry request-start received_ns; revoke at recording time.
                t=r.get('recorded_at_ns',x['timestamp_ns'])
                changes.append({'time':t,'maximum':-1000,'expires':t,'run_id':x['run_id']})
                errors.append({'time':t,'reason':r.get('error_type','recorded_error')})
        changes.sort(key=lambda w:(w['time'],w['maximum']==-1000))
        e['weather']=changes;e['weather_errors']=errors;e['availability']='Original local received_ns; 120s poll TTL; errors revoked at recorded_at_ns; run expiry preserved'
        summary.append({'date':e['day'],'city':e['city'],'polls':len(polls),'first_received_ns':min(polls),'last_received_ns':max(polls),'errors':len(errors)})
    for key,row in unique.items():
        candidates=history.get(key[:2],[])
        matches.append({'station':key[0],'obsTime':key[1],'matched':any(r.get('receiptTime')==row.get('receiptTime') and r.get('rawOb')==row.get('rawOb') and Decimal(str(r.get('temp')))==Decimal(str(row.get('temp'))) for r in candidates),'available':bool(candidates)})
    # Exclude the initial historical window from local-vs-upstream latency samples.
    for (run,key),received in first_seen.items():
        start=min(v for (slug,r),v in starts.items() if r==run)
        receipt=iso_ns(key[2])
        if receipt>=start:delays.append({'station':key[0],'obsTime':key[1],'delay_seconds':(received-receipt)/1e9})
    save(BASE/'live_coverage.json',summary);save(BASE/'receipt_comparison.json',{'observations':matches,'new_observation_delays':delays})
    return historical,live

def run_scenario(name,events,paths):
    root=BASE/name
    save(root/'events.json',events)
    for folder in ['sampled','inputs/listings']:files_link(BASE/'shared'/folder,root/folder)
    files_link(OLD/'inputs/gamma',root/'inputs/gamma')
    files_link(BASE/'shared/trade_only',root/'trade_only')
    save(root/'extraction-complete.json',{'status':'complete'})
    sample_capacity.ROOT=root;sample_capacity.main()
    post_signal_volume.ROOT=root;post_signal_volume.OUT=root/'results/post_signal_volume'
    class Frozen:
        def glob(self,p):return paths
    post_signal_volume.DATA=Frozen()
    post_signal_volume.main()
    cs=json.loads((root/'results/conditions.json').read_text())
    vs={(x['date'],x['city']):x for x in json.loads((root/'results/post_signal_volume/cities.json').read_text())}
    combined=[]
    for e in events:
        rr=[x for x in cs if x['date']==e['day'] and x['city']==e['city']]
        bottom=min((c for c in e['conditions'] if c['upper'] is not None),key=lambda c:c['upper'])['condition_id']
        v=vs[e['day'],e['city']]
        row={'date':e['day'],'city':e['city'],'scenario':name,'opportunity_conditions':sum(bool(x['first_shares']) for x in rr),'covered_conditions':sum(x['status']=='estimated' for x in rr)}
        for prefix in ['first','best_sample']:
            row[prefix+'_capacity_pusd']=sum(x[prefix+'_investment_pusd'] or 0 for x in rr)
            row[prefix+'_excluding_lowest_pusd']=sum(x[prefix+'_investment_pusd'] or 0 for x in rr if x['condition_id']!=bottom)
        row.update(turnover_pusd=v['notional_pusd'],turnover_excluding_lowest_pusd=v['notional_pusd_excluding_lowest'],partial_conditions=v['partial_conditions'])
        combined.append(row)
    save(root/'results/combined.json',combined)
    return combined

def main():
    historical,live=prepare_live()
    raw_root=extract_books.DATA
    paths=sorted(p for p in raw_root.glob('*/*/*.parquet') if p.stem>='2026-10-24T16')
    identities=[(p,p.stat().st_size,p.stat().st_mtime_ns) for p in paths]
    maps={}
    for name,events in [('historical120',historical),('live',live)]:
        root=BASE/name;save(root/'events.json',events);extract_books.ROOT=root
        for t,(intervals,m) in extract_books.mapping().items():
            if t in maps:maps[t][0].extend(intervals)
            else:maps[t]=(intervals,m)
    shared=BASE/'shared';extract_books.ROOT=shared
    for p in paths:save(shared/'inputs/listings'/f'{p.stem}.json',{'source':str(p)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(extract_books.one,p,maps) for p in paths]):f.result()
    # Extract trades from the earlier trigger of either scenario.
    save(shared/'events.json',historical);files_link(OLD/'inputs/gamma',shared/'inputs/gamma')
    post_signal_volume.ROOT=shared;states=post_signal_volume.configs()
    post_signal_volume.ROOT=BASE/'live'
    files_link(OLD/'inputs/gamma',BASE/'live/inputs/gamma')
    live_states=post_signal_volume.configs()
    for token,state in states.items():
        times=[v for v in [state['trigger_us'],live_states[token]['trigger_us']] if v is not None]
        state['trigger_us']=min(times) if times else None
    post_signal_volume.ROOT=shared
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(post_signal_volume.extract,p,states) for p in paths]):f.result()
    combined=run_scenario('historical120',historical,paths)+run_scenario('live',live,paths)
    for p,size,mtime in identities:assert p.stat().st_size==size and p.stat().st_mtime_ns==mtime,f'Input changed {p}'
    combined.sort(key=lambda r:(r['date'],r['city'],r['scenario']))
    save(BASE/'comparison.json',combined)
    with (BASE/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(combined[0]));w.writeheader();w.writerows(combined)
    print('COMPLETED',json.dumps(combined),flush=True)

if __name__=='__main__':main()
