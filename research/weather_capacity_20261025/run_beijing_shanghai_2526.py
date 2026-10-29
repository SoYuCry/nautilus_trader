"""Reuse capacity tools for Beijing/Shanghai Oct 25-26, AWC delay 120s."""
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor,as_completed
import fetch_inputs
import prepare
import extract_books
import sample_capacity
import post_signal_volume

ROOT=fetch_inputs.ROOT/'beijing_shanghai_20261025_26_delay120'
DATA=extract_books.DATA

def main():
    original=fetch_inputs.ROOT
    fetch_inputs.ROOT=ROOT
    fetch_inputs.save(ROOT/'inputs/station-catalog.json',json.loads((original/'inputs/station-catalog.json').read_text(encoding='utf8')))
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs=[pool.submit(fetch_inputs.gamma,f'highest-temperature-in-{city}-on-october-{day}-2026') for city in ['beijing','shanghai'] for day in [25,26]]
        jobs +=[pool.submit(fetch_inputs.weather,station,dt.datetime(2026,10,27,tzinfo=dt.timezone.utc),72) for station in ['ZBAA','ZSPD']]
        for f in as_completed(jobs):f.result()
    prepare.ROOT=ROOT
    prepare.main(25,26)
    events=json.loads((ROOT/'events.json').read_text(encoding='utf8'))
    assert len(events)==4 and all(e['weather'] for e in events)
    for e in events:
        for w in e['weather']:w['time']+=120_000_000_000
        e['availability']='AWC receiptTime + 120 seconds'
    fetch_inputs.save(ROOT/'events.json',events)
    paths=sorted(p for p in DATA.glob('*/*/*.parquet') if p.stem>='2026-10-24T16')
    identities=[{'path':str(p),'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in paths]
    fetch_inputs.save(ROOT/'input-files.json',identities)
    for p in paths:fetch_inputs.save(ROOT/'inputs/listings'/f'{p.stem}.json',{'source':str(p)})
    extract_books.ROOT=ROOT
    maps=extract_books.mapping()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(extract_books.one,p,maps) for p in paths]):f.result()
    fetch_inputs.save(ROOT/'extraction-complete.json',{'status':'completed'})
    sample_capacity.ROOT=ROOT
    sample_capacity.main()
    post_signal_volume.ROOT=ROOT
    post_signal_volume.OUT=ROOT/'results/post_signal_volume'
    class FrozenPaths:
        def glob(self,pattern):return paths
    post_signal_volume.DATA=FrozenPaths()
    post_signal_volume.main()
    for item in identities:
        p=__import__('pathlib').Path(item['path'])
        assert p.stat().st_size==item['size'] and p.stat().st_mtime_ns==item['mtime_ns'],f'Source changed: {p}'
    conditions=json.loads((ROOT/'results/conditions.json').read_text())
    cities=json.loads((ROOT/'results/cities.json').read_text())
    volumes={(r['date'],r['city']):r for r in json.loads((ROOT/'results/post_signal_volume/cities.json').read_text())}
    combined=[]
    for e in events:
        bottom=min((c for c in e['conditions'] if c['upper'] is not None),key=lambda c:c['upper'])['condition_id']
        rr=[r for r in conditions if r['date']==e['day'] and r['city']==e['city']]
        v=volumes[e['day'],e['city']]
        row={'date':e['day'],'city':e['city'],'opportunity_conditions':sum((r['first_shares'] or 0)>0 for r in rr),'covered_conditions':sum(r['status']=='estimated' for r in rr),'total_conditions':len(rr)}
        for prefix in ['first','best_sample']:
            row[prefix+'_capacity_pusd']=sum(r[prefix+'_investment_pusd'] or 0 for r in rr)
            row[prefix+'_excluding_lowest_pusd']=sum(r[prefix+'_investment_pusd'] or 0 for r in rr if r['condition_id']!=bottom)
        row.update(post_signal_turnover_pusd=v['notional_pusd'],turnover_excluding_lowest_pusd=v['notional_pusd_excluding_lowest'],turnover_partial_conditions=v['partial_conditions'])
        combined.append(row)
    combined.sort(key=lambda r:(r['date'],r['city']))
    import csv
    fetch_inputs.save(ROOT/'results/combined.json',combined)
    with (ROOT/'results/combined.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(combined[0]));w.writeheader();w.writerows(combined)
    print('COMPLETED',json.dumps(combined),flush=True)

if __name__=='__main__':main()
