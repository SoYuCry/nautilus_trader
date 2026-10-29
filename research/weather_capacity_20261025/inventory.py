"""Read only sparse listing/resolution groups, using Parquet statistics."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import json
from pathlib import Path
import pyarrow.parquet as pq
import pyarrow.compute as pc
from fetch_inputs import ROOT, save

DATA=Path(r'C:\Projects\PolyReaper\data\external\pendulumflow\v3_1012-latest')

def one(path):
    out=ROOT/'inputs'/'listings'/f'{path.stem}.json'
    if out.exists(): return
    f=pq.ParquetFile(path)
    groups=[]
    counts=Counter()
    for i in range(f.num_row_groups):
        g=f.metadata.row_group(i)
        s=g.column(0).statistics
        if s and s.has_min_max and s.min==s.max: counts[s.min]+=g.num_rows
        if not s or not s.has_min_max or any(s.min<=x<=s.max for x in ['new_market','market_resolved']): groups.append(i)
    rows=[]
    if groups:
        t=f.read_row_groups(groups,columns=['event_type','timestamp_received','market','assets_ids','outcomes','question','slug','winning_asset_id','winning_outcome'])
        t=t.filter(pc.is_in(t['event_type'],value_set=__import__('pyarrow').array(['new_market','market_resolved'])))
        for r in t.to_pylist():
            if 'temperature' not in (r.get('question') or '').lower() and 'temperature' not in (r.get('slug') or ''): continue
            for k,v in list(r.items()):
                if isinstance(v,bytes):r[k]='0x'+v.hex()
                elif isinstance(v,list):r[k]=['0x'+x.hex() if isinstance(x,bytes) else x for x in v]
            rows.append(r)
    save(out,{'source':str(path),'size':path.stat().st_size,'rows':f.metadata.num_rows,'groups':f.num_row_groups,'types':counts,'listings':rows})

if __name__=='__main__':
    paths=sorted(DATA.glob('*/*/*.parquet'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for n,f in enumerate(as_completed([pool.submit(one,p) for p in paths]),1):
            f.result()
            if n%20==0:print(n,len(paths),flush=True)
