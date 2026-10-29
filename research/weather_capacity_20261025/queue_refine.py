"""Replay source NO bid updates from last full snapshot to order acknowledgment."""
import json,csv
import datetime as dt
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from fetch_inputs import ROOT,save
from inventory import DATA

OUT=ROOT/'queue_estimate_20261026'

def main():
    rows=json.loads((OUT/'estimates.json').read_text(encoding='utf8'))
    tokens={c['condition_id']:int(c['no_token']).to_bytes(32,'big') for e in json.loads((ROOT/'live_validation_2526/live/events.json').read_text(encoding='utf8')) for c in e['conditions']}
    def run(o):
        end=dt.datetime.fromisoformat(o['ack_beijing']).astimezone(dt.timezone.utc)
        start=dt.datetime.fromisoformat(o['before_snapshot_beijing']).astimezone(dt.timezone.utc) if o['before_snapshot_beijing'] else end-dt.timedelta(hours=12)
        # Snapshot display was rounded to milliseconds; include that entire millisecond.
        token=tokens[o['condition_id']];market=bytes.fromhex(o['condition_id'][2:]);parts=[]
        for path in sorted(DATA.glob('*/*/*.parquet')):
            h=dt.datetime.strptime(path.stem,'%Y-%m-%dT%H').replace(tzinfo=dt.timezone.utc)
            if h+dt.timedelta(hours=1)<=start or h>end:continue
            f=pq.ParquetFile(path,pre_buffer=True)
            for i in range(f.num_row_groups):
                g=f.metadata.row_group(i);s=g.column(0).statistics;m=g.column(4).statistics
                if s and s.has_min_max and not any(s.min<=x<=s.max for x in ['book','price_change','best_bid_ask']):continue
                if m and m.has_min_max and not m.min<=market<=m.max:continue
                t=f.read_row_group(i,columns=['event_type','timestamp_received','sequence','asset_id','bids','price','size','side','best_bid','best_ask'],use_threads=False)
                mask=pc.and_(pc.equal(t['asset_id'],pa.scalar(token,pa.binary())),pc.and_(pc.greater_equal(t['timestamp_received'],pa.scalar(start,type=t.schema.field('timestamp_received').type)),pc.less(t['timestamp_received'],pa.scalar(end,type=t.schema.field('timestamp_received').type))))
                parts.extend(t.filter(mask).to_pylist())
        parts.sort(key=lambda r:(r['timestamp_received'],r['sequence']))
        bids={};initialized=False;full=False;updates=0;deleted=0;last=None
        for r in parts:
            if r['event_type']=='book':
                bids={x['price']:x['size'] for x in r['bids'] or [] if x['size']>0};initialized=True;full=True
            elif r['event_type']=='price_change' and r['side']=='BUY' and (initialized or r['price']==D('.999')):
                initialized=True
                if r['size']>0:bids[r['price']]=r['size']
                else:bids.pop(r['price'],None)
            if initialized:
                b,a=r['best_bid'],r['best_ask']
                if b is not None and a is not None and 0<=b<a<=1:
                    bad=[p for p in bids if p>b]
                    for p in bad:del bids[p];deleted+=1
                updates+=1;last=r['timestamp_received']
        o['replayed_bid999_shares']=str(bids.get(D('.999'),D(0))) if initialized else None
        o['replayed_better_bid_shares']=str(sum((q for p,q in bids.items() if p>D('.999')),D(0))) if full else None
        o['replay_updates']=updates;o['pruned_bid_levels']=deleted
        o['last_feed_seconds_before_ack']=(end-last).total_seconds() if last else None
        o['replay_status']=('observed_depth_before_ack_not_exact_queue' if full else 'absolute_999_level_update_only') if initialized else 'no_prior_snapshot'
        return o
    with ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(run,rows))
    save(OUT/'refined.json',result)
    fields=list(dict.fromkeys(k for r in result for k in r))
    with (OUT/'refined.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(result)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
