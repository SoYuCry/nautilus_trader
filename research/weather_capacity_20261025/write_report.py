"""Write the concise daily capacity deliverable after successful calculation."""
import json
import csv
from pathlib import Path

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results'

def main():
    daily=json.loads((OUT/'daily.json').read_text(encoding='utf8'))
    cities=json.loads((OUT/'cities.json').read_text(encoding='utf8'))
    conditions=json.loads((OUT/'conditions.json').read_text(encoding='utf8'))
    audit=json.loads((OUT/'audit.json').read_text(encoding='utf8'))
    events=json.loads((ROOT/'events.json').read_text(encoding='utf8'))
    lowest=set()
    for event in events:
        finite=[c for c in event['conditions'] if c['upper'] is not None]
        lower=min(c['upper'] for c in finite)
        chosen=[c for c in finite if c['upper']==lower]
        assert len(chosen)==1, event['slug']
        lowest.add((event['day'],event['city'],chosen[0]['condition_id']))
    assert len(lowest)==len(events)
    for r in conditions:
        r['is_lowest_temperature_bucket']=(r['date'],r['city'],r['condition_id']) in lowest
    for r in daily+cities:
        retained=[c for c in conditions if c['date']==r['date'] and ('city' not in r or c['city']==r['city']) and not c['is_lowest_temperature_bucket']]
        for prefix in ['first','best_sample']:
            field=prefix+'_investment_pusd'
            r[prefix+'_investment_excluding_lowest_pusd']=sum(c[field] or 0 for c in retained)
    for name,rows in [('daily',daily),('cities',cities),('conditions',conditions)]:
        (OUT/f'{name}.json').write_text(json.dumps(rows,ensure_ascii=False),encoding='utf8')
        with (OUT/f'{name}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    for d in daily:
        for prefix in ['first','best_sample']:
            field=prefix+'_investment_excluding_lowest_pusd'
            assert abs(sum(c[field] for c in cities if c['date']==d['date'])-d[field])<1e-5
    for d in daily:
        cr=[c for c in cities if c['date']==d['date']]
        rr=[c for c in conditions if c['date']==d['date']]
        for field in ['first_investment_pusd','first_shares','first_conditional_profit_pusd','best_sample_investment_pusd']:
            assert abs(sum(c[field] for c in cr)-d[field])<1e-5
            assert abs(sum(c[field] or 0 for c in rr)-d[field])<1e-5
    lines=['# 2026 年 10 月 16～23 日天气市场容量粗估','',
        '主口径：AWC 信号成立后，每个 condition 第一次取得有效、经 BBO 剪枝的全深度快照，假设一次扫掉所有符合价格和收益条件的 NO 卖盘。按市场所属当地日期汇总。取消每单/condition 5 份和每 Event 100 pUSD 上限；保留交易所最小数量要求。金额单位为 pUSD。','',
        '这是每日一次机会的容量估计，能从 condition 加总到城市和全市场。没有把持续存在的挂单反复累加；它也不代表全天所有新增流动性。','',
        '| 日期 | 首次机会投入 | 剔除最低温度档后首次投入 | NO 份数 | 条件利润 | 各 condition 最佳样本投入之和 | 可评估 condition |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for r in daily:
        lines.append(f"| {r['date']} | {r['first_investment_pusd']:,.2f} | {r['first_investment_excluding_lowest_pusd']:,.2f} | {r['first_shares']:,.2f} | {r['first_conditional_profit_pusd']:,.2f} | {r['best_sample_investment_pusd']:,.2f} | {r['covered_conditions']}/{r['conditions']} |")
    lines += ['',
        '剔除最低温度档：每个城市、每个市场日期，剔除元数据中上界最低的一个 condition（通常为“某温度及以下”）。不重新挑选剩余 condition 的首次机会。daily/cities 同时增加首次与最佳样本的剔除后容量列；conditions 增加 is_lowest_temperature_bucket 标记。缺口处理沿用原口径。', '',
        '最佳样本投入之和是各 condition 在不同时间的最佳快照相加，作为择时容量参考，不是同一时刻可投入资金，也不是全天容量上限。', '',
        '交付文件：`daily.csv` 每日总计；`cities.csv` 每日城市；`conditions.csv` 每日 condition，含温度桶、站点、首次/最佳样本时间、份数、均价、费用、条件利润和覆盖状态。各表提供同名 JSON。', '',
        '覆盖与估计假设：', '',
        '- 覆盖 48 个城市、8 天、4,224 个 condition。香港使用天文台观测，未用机场 AWC 替代，未纳入总计。丹佛 23 日按当日规则使用 KDEN，其他日期使用对应规则站点。',
        '- AWC 使用历史 receiptTime 作为可用时间代理。缺少当时本机轮询日志，假设持续获取；不模拟真实轮询中断和 120 秒轮询 TTL。观测仍按 4,500 秒新鲜度限制。历史修订记录可能不完整。',
        '- 使用 routine METAR 的 T remark，缺少时用报文主体温度；按市场摄氏/华氏单位转换与取整。23 日规则明确使用该口径；较早 NOAA/Wunderground 市场按 AWC 代理情景估计，不能保证与最终结算源完全等价。',
        '- 快照以其自带有效 BBO 或随后 5 秒内的首个有效 BBO 验证，删除低于有效 best ask 的矛盾旧卖档；不补数量，不把全深度压成一档。快照到 BBO 的间隔内数量可能变化，因此属于粗估。',
        '- 费用使用下载时 Gamma feeSchedule 的配置情景，未验证历史生效费率。条件利润假设 NO 最终兑付 1，不是已实现收益。',
        '- 最高无上界桶和天气未突破的桶记为无策略机会；天气曾突破但没有有效盘口样本的桶标为缺口，明细金额留空，总表是已覆盖部分。',
        '- 23 日只能统计现有行情覆盖部分。不同城市的当地日对应不同 UTC 区间，不能把缺少的后半日外推成完整一天。',
        '- turnover_proxy 字段只作信号后合格历史成交额参考；成交有换手和方向问题，不作为可执行容量。', '',
        f"审计：处理 {audit['hours']} 个小时文件；删除 {audit['counts'].get('deleted_ask_levels',0):,} 个矛盾卖档。7 项核心检查通过；每日、城市、condition 主指标加总一致。", '',
        '数据源：本地 Pendulumflow v3_1012-latest；AWC 历史 METAR；Gamma Event 元数据。下载响应与请求证据位于 `../inputs/`。', '',
        '运行：先 `python fetch_inputs.py`、`python prepare.py`，然后 `python extract_books.py`、`python sample_capacity.py`、`python write_report.py`。现有下载和提取文件可复用。']
    (OUT/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print('\n'.join(lines[:17]))

if __name__=='__main__':main()
