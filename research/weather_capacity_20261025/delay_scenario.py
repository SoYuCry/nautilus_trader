"""Recompute the existing daily table with +60 seconds of AWC availability delay.

Reuse the original frozen shards and fee metadata, so added downloads do not confound
the comparison. Observation expiry and market timestamps are not shifted.
"""
import csv
import argparse
import json
import os
from pathlib import Path
import datetime as dt
import pyarrow as pa
import pyarrow.parquet as pq
import extract_books
import sample_capacity
import post_signal_volume
from fetch_inputs import ROOT,save

SCENARIO=ROOT/'scenarios'/'receipt_plus_60s'
DELAY_SECONDS=60

def link_files(source,target,pattern='*'):
    target.mkdir(parents=True,exist_ok=True)
    for path in source.glob(pattern):
        if path.is_file() and not (target/path.name).exists():os.link(path,target/path.name)

def write_rows(name,rows):
    save(SCENARIO/'results'/f'{name}.json',rows)
    with (SCENARIO/'results'/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    events=json.loads((ROOT/'events.json').read_text(encoding='utf8'))
    original_maps=extract_books.mapping()
    for e in events:
        for w in e['weather']:w['time']+=DELAY_SECONDS*1_000_000_000
        e['availability']=f'AWC receiptTime + {DELAY_SECONDS} seconds; observation expiry unchanged'
    save(SCENARIO/'events.json',events)
    for folder in ['sampled','trade_only','inputs/listings','inputs/gamma']:
        link_files(ROOT/folder,SCENARIO/folder)
    save(SCENARIO/'extraction-complete.json',{'status':'frozen_original_shards'})
    # Old extraction filtered eligible tokens per hour; add any tokens newly needed
    # because a delayed correction extends a signal across an hour boundary.
    extract_books.ROOT=SCENARIO
    delayed_maps=extract_books.mapping()
    supplemented=[]
    for shard in sorted((SCENARIO/'sampled').glob('*.parquet')):
        start=int(dt.datetime.strptime(shard.stem,'%Y-%m-%dT%H').replace(tzinfo=dt.timezone.utc).timestamp()*1e9)
        end=start+3_600_000_000_000
        def active(maps):return {t for t,(ints,market) in maps.items() if any(a<end+5_000_000_000 and b>start for a,b in ints)}
        extra=active(delayed_maps)-active(original_maps)
        if extra:
            temp_root=SCENARIO/'supplement'
            extract_books.ROOT=temp_root
            raw=extract_books.DATA/shard.stem[:10]/shard.stem[-2:]/shard.name
            extract_books.one(raw,{t:delayed_maps[t] for t in extra})
            old=pq.read_table(shard);new=pq.read_table(temp_root/'sampled'/shard.name)
            # Some early original shards already contained more tokens than the filter.
            import pyarrow.compute as pc
            new=new.filter(pc.invert(pc.is_in(new['asset_id'],value_set=pc.unique(old['asset_id']))))
            temp=shard.with_suffix('.writing');pq.write_table(pa.concat_tables([old,new]),temp,compression='zstd');temp.replace(shard)
            supplemented.append({'hour':shard.stem,'new_rows':len(new)})
    sample_capacity.ROOT=SCENARIO
    sample_capacity.main()
    post_signal_volume.ROOT=SCENARIO
    post_signal_volume.OUT=SCENARIO/'results'/'post_signal_volume'
    raw_root=post_signal_volume.DATA
    frozen_raw=[raw_root/p.stem[:10]/p.stem[-2:]/p.name for p in sorted((ROOT/'trade_only').glob('*.parquet'))]
    class FrozenFiles:
        def glob(self,pattern):return frozen_raw
    post_signal_volume.DATA=FrozenFiles()
    post_signal_volume.main()
    daily=json.loads((SCENARIO/'results/daily.json').read_text())
    cities=json.loads((SCENARIO/'results/cities.json').read_text())
    conditions=json.loads((SCENARIO/'results/conditions.json').read_text())
    lowest={(e['day'],e['city'],min((c for c in e['conditions'] if c['upper'] is not None),key=lambda c:c['upper'])['condition_id']) for e in events}
    for c in conditions:c['is_lowest_temperature_bucket']=(c['date'],c['city'],c['condition_id']) in lowest
    for r in daily+cities:
        cc=[c for c in conditions if c['date']==r['date'] and ('city' not in r or c['city']==r['city'])]
        r['opportunity_cities']=len({c['city'] for c in cc if (c['first_shares'] or 0)>0})
        for prefix in ['first','best_sample']:
            r[prefix+'_investment_excluding_lowest_pusd']=sum(c[prefix+'_investment_pusd'] or 0 for c in cc if not c['is_lowest_temperature_bucket'])
    for name,rows in [('daily',daily),('cities',cities),('conditions',conditions)]:write_rows(name,rows)
    volume={r['date']:r for r in json.loads((SCENARIO/'results/post_signal_volume/daily.json').read_text())}
    baseline={r['date']:r for r in json.loads((ROOT/'results/daily_combined.json').read_text())}
    combined=[];comparison=[]
    for r in daily:
        v=volume[r['date']]
        row={'date':r['date'],'opportunity_cities':r['opportunity_cities'],'first_capacity_pusd':r['first_investment_pusd'],'first_excluding_lowest_pusd':r['first_investment_excluding_lowest_pusd'],'best_snapshot_sum_pusd':r['best_sample_investment_pusd'],'best_snapshot_excluding_lowest_pusd':r['best_sample_investment_excluding_lowest_pusd'],'post_signal_turnover_pusd':v['notional_pusd'],'turnover_excluding_lowest_pusd':v['notional_pusd_excluding_lowest']}
        combined.append(row)
        change={'date':r['date']}
        for k,value in row.items():
            if k=='date':continue
            old=baseline[r['date']][k]
            change[k+'_baseline']=old;change[k+f'_delay{DELAY_SECONDS}']=value;change[k+'_change_pct']=(value/old-1)*100 if old else None
        comparison.append(change)
        assert row['post_signal_turnover_pusd']<=baseline[r['date']]['post_signal_turnover_pusd']+1e-6
        assert abs(sum(c['first_investment_pusd'] for c in cities if c['date']==r['date'])-row['first_capacity_pusd'])<1e-5
    write_rows('daily_combined',combined);write_rows('comparison',comparison)
    sample_audit=json.loads((SCENARIO/'results/audit.json').read_text())
    baseline_audit=json.loads((ROOT/'results/audit.json').read_text())
    if not supplemented:assert sample_audit['counts']==baseline_audit['counts']
    save(SCENARIO/'scenario.json',{'delay_seconds':DELAY_SECONDS,'observation_expiry_shifted':False,'sample_hours':sample_audit['hours'],'trade_hours':len(frozen_raw),'supplemented':supplemented,'baseline_unchanged':True})
    print('SCENARIO_COMPLETED',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--delay-seconds',type=int,default=60)
    args=parser.parse_args()
    if args.delay_seconds<0:parser.error('Delay must be nonnegative')
    DELAY_SECONDS=args.delay_seconds
    SCENARIO=ROOT/'scenarios'/f'receipt_plus_{DELAY_SECONDS}s'
    main()
