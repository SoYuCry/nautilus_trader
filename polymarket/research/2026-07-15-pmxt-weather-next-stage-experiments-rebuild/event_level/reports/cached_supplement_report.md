# 36 Event 缓存补充实验

> 只读既有 anchor/distribution 缓存；未扩 Event、未重跑 PMXT replay、未做策略或 PnL。

## P0-1 Distribution 信号稳定性

- Raw median Event IC=0.074，Event bootstrap 90% CI=[0.070, 0.079]。
- 排除 |sum(mid)-1|>0.10 后：median IC=0.072，90% CI=[0.066, 0.079]。
- 5 组 leave-one-date-out：最小/最大 median IC=0.073/0.077；是否全部正向：是。
- 负 IC Event：0 个。

| date | city | cohort | Event IC |
| --- | --- | --- | ---: |

**判定：** 可以表述为“跨日期稳定的弱结构信号”，仍不是 Alpha。

## P0-2 概率和异常尾部归因

固定口径：fresh BBO <= 120s；spread max <= 0.10；同一分钟 Outcome source-time span <= 60s。

| filter | snapshots | events | P95 | >0.10 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raw | 109767 | 36 | 0.097 | 4.7% | 4.370 |
| Complete outcomes | 101379 | 34 | 0.086 | 3.6% | 4.370 |
| Fresh BBO <=120s | 33074 | 36 | 0.110 | 6.1% | 4.370 |
| Spread max <=0.10 | 98764 | 36 | 0.077 | 1.9% | 0.985 |
| Synchronized <=60s | 31655 | 36 | 0.108 | 5.9% | 4.370 |
| Complete + fresh | 30481 | 34 | 0.086 | 3.1% | 4.370 |
| Complete + fresh + spread + sync | 25898 | 34 | 0.080 | 1.8% | 0.249 |

**判定：** 完整、新鲜、spread 受控且时间同步的报价下仍存在明显尾部，值得继续做结构性定价诊断。
Raw 异常占比=4.7%；严格同步完整子样本异常占比=1.8%（若无样本则为 NaN）。

- Raw >0.10：5200 个 snapshot / 36 Event；其中 complete=69.5%、fresh=39.0%、spread-controlled=36.7%、synchronized=36.1%。
- Raw 异常前三：{('highest-temperature-in-wuhan-on-june-10-2026', 'Wuhan'): 1078, ('highest-temperature-in-san-francisco-on-june-7-2026', 'San Francisco'): 873, ('highest-temperature-in-seoul-on-june-10-2026', 'Seoul'): 660}。
- 严格过滤后 >0.10：460 个 snapshot / 22 Event；前三：{('highest-temperature-in-taipei-on-june-7-2026', 'Taipei'): 70, ('highest-temperature-in-kuala-lumpur-on-june-9-2026', 'Kuala Lumpur'): 59, ('highest-temperature-in-chengdu-on-june-10-2026', 'Chengdu'): 50}。
- 归因结论：绝大多数极端尾部随完整性、spread 与同步过滤消失；残余偏离仍存在，但集中于少数 Event，应做逐 Event 报价语义核验，不能直接解释为套利。

## 可选 P1：Event 内可执行边界

本轮不作正式执行结论。缓存只有 BBO 价格，没有每条腿 BBO size 与可验证 fee rate，无法可靠计算最小多腿容量和 fee 后边界；避免把 mid/BBO 偏离误称为套利。