"""Bind actual event rules and archived AWC to explicit estimated availability."""
import ast
import datetime as dt
from decimal import Decimal, ROUND_FLOOR
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from fetch_inputs import ROOT, save

# Reuse the supplied routine METAR parser verbatim, without importing native runtime.
source=ROOT/'weather-capacity-tools-20261025'/'weather_metar_frozen.py'
tree=ast.parse(source.read_text(encoding='utf8'))
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='routine_temperature')
exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'))

def ns(value):
    return int(dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1_000_000_000)

def signals(station,timezone,day,unit):
    rows={}
    for p in (ROOT/'inputs'/'awc'/station).glob('*.json'):
        if p.name.endswith('.request.json'):continue
        for r in json.loads(p.read_text(encoding='utf8')):
            if r.get('icaoId')!=station:continue
            if dt.datetime.fromtimestamp(r['obsTime'],ZoneInfo(timezone)).date().isoformat()!=day:continue
            rows[(r['obsTime'],r['receiptTime'],r.get('rawOb'))]=r
    selected={}
    changes=[]
    errors=[]
    for r in sorted(rows.values(),key=lambda r:(ns(r['receiptTime']),r['obsTime'])):
        try:
            if r.get('metarType')=='SPECI' or (r.get('rawOb') or '').startswith('SPECI'):continue
            t=routine_temperature(r['rawOb'],station)
            received=max(ns(r['receiptTime']),int(r['obsTime'])*1_000_000_000)
            selected[r['obsTime']]=t
            good={k:v for k,v in selected.items() if v is not None}
            if not good:continue
            maximum=max(good.values())
            if unit=='F':maximum=maximum*Decimal(9)/5+32
            maximum=int((maximum+Decimal('0.5')).to_integral_value(rounding=ROUND_FLOOR))
            expires=max(good)*1_000_000_000+4_500_000_000_000
            changes.append({'time':received,'maximum':maximum,'expires':expires})
        except Exception as e:errors.append(str(e))
    return changes,errors

def main(start_day=16,end_day=23):
    catalog=json.loads((ROOT/'inputs'/'station-catalog.json').read_text(encoding='utf8'))['stations']
    catalog['KDEN']={'tz':'America/Denver','city':'Denver'} # Actual October 23 rule names KDEN.
    events=[]; gaps=[]
    for p in sorted((ROOT/'inputs'/'gamma').glob('*.json')):
        if p.name.endswith('.request.json'):continue
        for e in json.loads(p.read_text(encoding='utf8')):
            slug=e['slug']; match=re.fullmatch(r'highest-temperature-in-(.+)-on-october-(\d+)-2026',slug)
            if not match:continue
            city=match[1]; day=f'2026-10-{int(match[2]):02d}'
            if not start_day<=int(match[2])<=end_day:continue
            description=e.get('description','')
            station_match=re.search(r'[?&](?:site|ids)=([a-zA-Z]{4})',description)
            if not station_match:
                station_match=re.search(r'wunderground\.com/history/daily/[^\s]+/([A-Z]{4})',description)
            station=station_match[1].upper() if station_match else None
            if station not in catalog:
                gaps.append({'event':slug,'reason':'no verified AWC station in actual market rule','station':station});continue
            timezone=catalog[station]['tz']
            unit='F' if 'degrees Fahrenheit' in description else 'C' if 'degrees Celsius' in description else None
            if unit is None:
                gaps.append({'event':slug,'reason':'unknown temperature unit'});continue
            start=dt.datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(timezone))
            end=start+dt.timedelta(days=1)
            weather,errors=signals(station,timezone,day,unit)
            conditions=[]
            for m in e['markets']:
                title=m['groupItemTitle']; outcomes=json.loads(m['outcomes']);tokens=json.loads(m['clobTokenIds'])
                nums=re.match(r'(-?\d+)(?:\s*[-–]\s*(-?\d+))?',title)
                if not nums:raise ValueError((slug,title))
                upper=None if 'higher' in title.lower() or 'above' in title.lower() else int(nums[2] or nums[1])
                schedule=m.get('feeSchedule') or {}
                rate=0 if m.get('feesEnabled') is False else schedule.get('rate') if schedule.get('exponent')==1 else None
                conditions.append({'condition_id':m['conditionId'],'no_token':tokens[outcomes.index('No')], 'bucket':title,'upper':upper,'rate':rate,'minimum':m.get('orderMinSize'),'fee_evidence':'Gamma retrieved at analysis time; not verified historical schedule'})
            events.append({'slug':slug,'city':city,'day':day,'station':station,'timezone':timezone,'unit':unit,'start':int(start.timestamp()*1e9),'end':int(end.timestamp()*1e9),'weather':weather,'weather_errors':errors,'conditions':conditions,'availability':'AWC receiptTime proxy; historical polling availability and errors unknown','temperature_rule':'routine METAR T remark/body; convert to market unit then round half toward positive infinity'})
    save(ROOT/'events.json',events)
    save(ROOT/'gaps.json',gaps)
    print('events',len(events),'conditions',sum(len(e['conditions']) for e in events),'no weather',sum(not e['weather'] for e in events),'gaps',len(gaps))

if __name__=='__main__':main()
