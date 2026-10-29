"""Half-hour/condition breakdown of recorded post-trigger NO trades, exact decimals."""
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal as D
import datetime as dt
import csv
import json
from zoneinfo import ZoneInfo
import pyarrow.parquet as pq
import post_signal_volume as volume
from fetch_inputs import ROOT,save

BASE=ROOT/'live_validation_2526/live'
OUT=BASE/'results/halfhour'
TZ=ZoneInfo('Asia/Shanghai')
STEP=1800_000000

def us(d):
    delta=d-dt.datetime(1970,1,1,tzinfo=dt.timezone.utc)
    return (delta.days*86400+delta.seconds)*1000000+delta.microseconds

def local(t):
    return dt.datetime.fromtimestamp(t//1000000,TZ).strftime('%Y-%m-%d %H:%M:%S') if t is not None else None

def export(name,rows):
    save(OUT/f'{name}.json',rows)
    with (OUT/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    volume.ROOT=BASE
    states=volume.configs()
    events=json.loads((BASE/'events.json').read_text(encoding='utf8'))
    by_token={}
    for e in events:
        e['times']=[w['time'] for w in e['weather']]
        for c in e['conditions']:by_token[int(c['no_token']).to_bytes(32,'big')]=(e,c)
    buckets=defaultdict(list);seen=set();trades=[]
    for path in sorted((BASE/'trade_only').glob('*.parquet')):
        for r in pq.read_table(path).to_pylist():
            token=r['asset_id'];s=states[token];e,c=by_token[token]
            t=us(r['timestamp'] or r['timestamp_received'])
            if s['trigger_us'] is None or t<s['trigger_us'] or (s['close_us'] is not None and t>s['close_us']):continue
            if r['price'] is None or r['size'] is None or r['size']<=0:continue
            key=(token,r['transaction_hash'] or r['sequence'],t,r['price'],r['size'],r['side'])
            if key in seen:continue
            seen.add(key)
            i=bisect_right(e['times'],t*1000)-1
            w=e['weather'][i] if i>=0 else None
            active=bool(w and e['start']<=t*1000<e['end'] and t*1000<w['expires'] and c['upper'] is not None and w['maximum']>c['upper'])
            p=r['price'];q=r['size'];rate=D(str(c['rate'])) if c['rate'] is not None else None
            eligible=bool(rate is not None and 0<p<=D('.999') and 1-p-rate*p*(1-p)-D('.00001')>=0)
            row={'event_date':e['day'],'city':e['city'],'bucket':c['bucket'],'condition_id':c['condition_id'],'trigger_beijing':local(s['trigger_us']),'trade_beijing':local(t),'trade_time_us':t,'price':p,'shares':q,'notional_pusd':p*q,'weather_active_at_trade':active,'price_eligible':eligible,'source_side':r['side'],'transaction_hash':r['transaction_hash'].hex() if r['transaction_hash'] else None}
            trades.append(row);buckets[token,t//STEP*STEP].append(row)
    rows=[]
    for token,s in states.items():
        e,c=by_token[token]
        final=max([e['end']//1000]+[t+STEP for tok,t in buckets if tok==token])
        for start in range(e['start']//1000,final,STEP):
            rr=buckets[token,start]
            q=sum((x['shares'] for x in rr),D(0));amount=sum((x['notional_pusd'] for x in rr),D(0))
            # Exact overlap with signal-valid intervals, including poll TTL/errors/restarts.
            active_ns=0
            for i,w in enumerate(e['weather']):
                end=min(w['expires'],e['end'],e['weather'][i+1]['time'] if i+1<len(e['weather']) else e['end'])
                if c['upper'] is not None and w['maximum']>c['upper']:
                    active_ns+=max(0,min(end,(start+STEP)*1000)-max(w['time'],start*1000,e['start']))
            rows.append({'event_date':e['day'],'city':e['city'],'bucket':c['bucket'],'condition_id':s['condition_id'],'is_lowest_temperature_bucket':s['is_lowest_temperature_bucket'],'window_start_beijing':local(start),'window_end_beijing':local(start+STEP),'trigger_beijing':local(s['trigger_us']),'valid_signal_minutes':D(active_ns)/D(60_000000000),'trade_count':len(rr),'shares':q,'notional_pusd':amount,'vwap':amount/q if q else None,'min_price':min((x['price'] for x in rr),default=None),'max_price':max((x['price'] for x in rr),default=None),'active_signal_trade_count':sum(x['weather_active_at_trade'] for x in rr),'active_signal_notional_pusd':sum((x['notional_pusd'] for x in rr if x['weather_active_at_trade'] and x['price_eligible']),D(0))})
    rows.sort(key=lambda r:(r['event_date'],r['city'],r['window_start_beijing'],r['bucket']))
    trades.sort(key=lambda r:r['trade_time_us'])
    export('all_windows',rows);export('windows_with_trades',[r for r in rows if r['trade_count']]);export('trades',trades)
    previous=json.loads((BASE/'results/post_signal_volume/conditions.json').read_text(encoding='utf8'))
    for s in previous:
        total=sum((r['notional_pusd'] for r in rows if r['condition_id']==s['condition_id']),D(0))
        assert abs(total-D(str(s['notional_pusd'])))<D('.000001')
    lines=['# 实录 AWC：每半小时、每温度桶成交明细','',
           '时间均为北京时间，金额为 pUSD。仅计首次实录信号触发后的 NO 成交，沿用此前数据截止及去重口径。下表仅展示有成交的窗口；all_windows.csv 含每个桶每半小时的零成交行，trades.csv 含逐笔记录。','',
           '**无法据此判断未成交是队列靠后。** 数据没有你的委托价格、订单确认/撤单记录或队列位置。即使同价成交，也需确认订单当时已在场、未撤销且可成交。source_side 是原始成交消息字段，不推断它一定代表你的可成交方向。','',
           '25 日实录从 19:44 开始，之前的零成交行只表示本次统计未纳入，不能视为市场零成交。valid_signal_minutes 为该窗口内实录信号有效分钟；active_signal_notional_pusd 只计成交当时信号仍有效且价格符合条件的金额。','',
           '| 日期/城市 | 温度桶 | 半小时窗口 | 笔数 | NO 份数 | NO 均价 | 成交额 | 信号有效时成交额 |',
           '|---|---|---|---:|---:|---:|---:|---:|']
    for r in rows:
        if not r['trade_count']:continue
        city={'beijing':'北京','shanghai':'上海'}[r['city']]
        lines.append(f"| {r['event_date']} {city} | {r['bucket']} | {r['window_start_beijing'][11:16]}–{r['window_end_beijing'][11:16]} | {r['trade_count']} | {r['shares']:.2f} | {r['vwap']:.4f} | {r['notional_pusd']:.5f} | {r['active_signal_notional_pusd']:.5f} |")
    (OUT/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print('\n'.join(lines));print('ROWS',len(rows),'NONZERO',sum(bool(r['trade_count']) for r in rows),'TRADES',len(trades))

if __name__=='__main__':main()
