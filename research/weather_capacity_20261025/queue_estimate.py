"""Estimate same-price quantity near user-reported order acknowledgments."""
import datetime as dt
import json,csv
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from fetch_inputs import ROOT,save
from inventory import DATA
from zoneinfo import ZoneInfo

OUT=ROOT/'queue_estimate_20261026'
TIMES={'shanghai':{18:'08:06:15.056',19:'08:06:15.051',20:'09:05:15.521',21:'09:36:30.177',22:'11:37:02.661',23:'14:06:02.040'},'beijing':{13:'11:06:37.247',14:'11:06:37.240',15:'11:36:22.463',16:'12:35:46.070',17:'12:35:46.057',18:'15:05:55.787'}}
TZ=ZoneInfo('Asia/Shanghai')
def local(t):return t.astimezone(TZ).isoformat(timespec='milliseconds')

def main():
    orders={}
    for e in json.loads((ROOT/'live_validation_2526/live/events.json').read_text(encoding='utf8')):
        if e['day']!='2026-10-26':continue
        for c in e['conditions']:
            if c['upper'] not in TIMES[e['city']]:continue
            orders[int(c['no_token']).to_bytes(32,'big')]={'city':e['city'],'bucket':c['bucket'],'condition_id':c['condition_id'],'ack':dt.datetime.fromisoformat('2026-10-26T'+TIMES[e['city']][c['upper']]).replace(tzinfo=TZ),'before':None,'after':None}
    def read(p):
        f=pq.ParquetFile(p,pre_buffer=True);groups=[]
        for i in range(f.num_row_groups):
            s=f.metadata.row_group(i).column(0).statistics
            if not s or not s.has_min_max or s.min<='book'<=s.max:groups.append(i)
        t=f.read_row_groups(groups,columns=['event_type','timestamp_received','asset_id','bids','best_bid','best_ask'],use_threads=False)
        return t.filter(pc.and_(pc.equal(t['event_type'],'book'),pc.is_in(t['asset_id'],value_set=pa.array(list(orders),type=pa.binary())))).to_pylist()
    paths=sorted(p for p in DATA.glob('*/*/*.parquet') if '2026-10-25T20'<=p.stem<='2026-10-26T08')
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rows in pool.map(read,paths):
            for r in rows:
                o=orders[r['asset_id']];t=r['timestamp_received'];key='before' if t<o['ack'] else 'after'
                prev=o[key]
                if prev is None or (key=='before' and t>prev['timestamp_received']) or (key=='after' and t<prev['timestamp_received']):o[key]=r
    trades=json.loads((ROOT/'live_validation_2526/live/results/halfhour/trades.json').read_text(encoding='utf8'))
    result=[]
    for o in orders.values():
        row={k:o[k] for k in ['city','bucket','condition_id']};row['ack_beijing']=local(o['ack'])
        for key in ['before','after']:
            r=o[key];q=None
            if r is not None:
                q=sum((x['size'] for x in r['bids'] or [] if x['price']==D('.999')),D(0))
            row[key+'_snapshot_beijing']=local(r['timestamp_received']) if r else None
            row[key+'_seconds_from_ack']=(r['timestamp_received']-o['ack']).total_seconds() if r else None
            row[key+'_bid999_shares']=q
            row[key+'_better_bid_shares']=sum((x['size'] for x in r['bids'] or [] if x['price']>D('.999')),D(0)) if r else None
        row['later_sell_trade_shares']=sum((D(r['shares']) for r in trades if r['event_date']=='2026-10-26' and r['condition_id']==o['condition_id'] and r['source_side']=='SELL' and D(r['price'])==D('.999') and dt.datetime.fromtimestamp(r['trade_time_us']/1e6,dt.timezone.utc)>o['ack']),D(0))
        result.append(row)
    save(OUT/'estimates.json',result)
    with (OUT/'estimates.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(result[0]));w.writeheader();w.writerows(result)
    print(json.dumps(result,default=str,ensure_ascii=False))
if __name__=='__main__':main()
