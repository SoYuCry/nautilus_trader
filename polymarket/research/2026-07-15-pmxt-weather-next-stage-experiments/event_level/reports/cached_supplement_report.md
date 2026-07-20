# 36 Event 缓存补充实验

> 只读既有 anchor/distribution 缓存；未扩 Event、未重跑 PMXT replay、未做策略或 PnL。

## P0-1 Distribution 信号稳定性

- Raw median Event IC=0.073，Event bootstrap 90% CI=[0.052, 0.083]。
- 排除 |sum(mid)-1|>0.10 后：median IC=0.064，90% CI=[0.051, 0.077]。
- 9 组 leave-one-date-out：最小/最大 median IC=0.061/0.075；是否全部正向：是。
- 负 IC Event：4 个。

| date | city | cohort | Event IC |
| --- | --- | --- | ---: |
| 2026-06-06 | Mexico City | degraded | -0.079 |
| 2026-06-07 | Chengdu | degraded | -0.037 |
| 2026-06-06 | Singapore | degraded | -0.035 |
| 2026-06-05 | Denver | clean | -0.008 |

**判定：** 可以表述为“跨日期稳定的弱结构信号”，仍不是 Alpha。

## P0-2 概率和异常尾部归因

固定口径：fresh BBO <= 120s；spread max <= 0.10；同一分钟 Outcome source-time span <= 60s。

| filter | snapshots | events | P95 | >0.10 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raw | 60281 | 36 | 0.135 | 7.2% | 3.935 |
| Complete outcomes | 38105 | 23 | 0.085 | 2.9% | 2.495 |
| Fresh BBO <=120s | 28087 | 36 | 0.140 | 8.1% | 3.935 |
| Spread max <=0.10 | 54859 | 36 | 0.119 | 5.9% | 0.998 |
| Synchronized <=60s | 26976 | 36 | 0.140 | 8.1% | 3.935 |
| Complete + fresh | 18029 | 22 | 0.083 | 3.0% | 2.495 |
| Complete + fresh + spread + sync | 14616 | 22 | 0.077 | 1.4% | 0.180 |

**判定：** 完整、新鲜、spread 受控且时间同步的报价下仍存在明显尾部，值得继续做结构性定价诊断。
Raw 异常占比=7.2%；严格同步完整子样本异常占比=1.4%（若无样本则为 NaN）。

- Raw >0.10：4333 个 snapshot / 33 Event；其中 complete=25.2%、fresh=52.4%、spread-controlled=75.1%、synchronized=50.2%。
- Raw 异常前三：{('highest-temperature-in-hong-kong-on-june-5-2026', 'Hong Kong'): 1899, ('highest-temperature-in-wuhan-on-june-10-2026', 'Wuhan'): 1043, ('highest-temperature-in-shenzhen-on-june-9-2026', 'Shenzhen'): 268}。
- 严格过滤后 >0.10：205 个 snapshot / 11 Event；前三：{('highest-temperature-in-manila-on-june-11-2026', 'Manila'): 102, ('highest-temperature-in-mexico-city-on-june-6-2026', 'Mexico City'): 23, ('highest-temperature-in-amsterdam-on-june-8-2026', 'Amsterdam'): 21}。
- 归因结论：绝大多数极端尾部随完整性、spread 与同步过滤消失；残余偏离仍存在，但集中于少数 Event，应做逐 Event 报价语义核验，不能直接解释为套利。

## 可选 P1：Event 内可执行边界

本轮不作正式执行结论。缓存只有 BBO 价格，没有每条腿 BBO size 与可验证 fee rate，无法可靠计算最小多腿容量和 fee 后边界；避免把 mid/BBO 偏离误称为套利。