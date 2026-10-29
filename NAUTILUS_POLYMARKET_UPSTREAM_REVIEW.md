# NautilusTrader Polymarket：三项实盘问题的上游评审材料

整理日期：2026-10-21。用途：供人工判断是否向 NautilusTrader 提 issue 或 PR。
本文汇总已有诊断，不代表已经提交上游、完成修复或批准部署。

## 结论与责任边界

| 问题 | 当前责任判断 | 是否由策略造成 | 建议的上游处理方式 |
| --- | --- | --- | --- |
| 多档 taker 成交被记成顶层单价 | 本地 Nautilus Polymarket adapter 的成交解析缺陷 | 否 | 先在上游当前版本复现，再提交聚焦的 adapter 修复 |
| 截断后的 API 均价无法通过原生恢复比较 | Polymarket 报告精度与 Nautilus 校验语义不兼容 | 否 | 先讨论接口精度合同和比较语义，再决定补丁；不宜直接提高全局容差 |
| 明确零费率被回退为 0.10 | 本地 Nautilus Polymarket adapter 的费率分类/补全缺陷 | 否 | 范围最小，适合优先提交独立 adapter 修复 |

“本地 Nautilus”指当前安装的 Rust/PyO3 原生运行库及对应本地源码。
尚未在官方最新 develop 上复现，所以不能把本文直接表述成“官方当前版本已确认存在三个 bug”。
三个问题应分别讨论；多档成交经济值错误不能被归入普通价格舍入误差。

## 环境、样本与已经做过的验证

- 应用为薄策略，订单提交、账户、Position、持久化和恢复使用 Nautilus 原生机制。
- 实盘 Python 环境：`/data/liuchang/nautilus_trader/python/.venv`。
- 已安装原生包来源标签为 runtime-v2-2bccb0cb92；完整二进制构建来源未独立证明。
- 运行中双城应用 manifest 记录的 Git SHA 为 080720a882；源码更新未通过本次工作重启部署。
- 本地相关源码与基线 2bccb0cb92 的相应文件没有差异；这不代替官方最新版本复现。
- 读取了 22 个原生持久化 Position 记录：21 个仍持有、1 个已平仓。
  使用已安装原生模型回放，21 个持仓按标的归并为 20 个。
- 对相同 20 个标的取得完整公开 positions 页；公开 trades/activity 用于独立数值对照。
- 当前上海/北京 15 个持仓标的的数量和均价完全一致；全球批次另 5 个标的中有 4 个均价超容差。
- 查询后再次读取相关缓存 fills/snapshots，记录没有变化。没有写 Redis、改成本、运行批次或重启。

“原生持久化回放”不是读取运行进程内存；“离线比较超容差”不是一次真实启动失败。
历史上实际捕获的恢复失败是包含伦敦持仓的 Batch03；其订单审计为空，没有提交订单。

## 问题一：多档 taker 成交使用了错误的整笔价格

### 现象和证据

买 No 的一个 taker trade 可以匹配多个 maker 档位。以下两笔的 maker 明细、缓存 fill
和公开交易记录通过 transaction hash 对应：

| 样本 | maker 明细换算后的 No 成交 | 应有数量加权价 | 原生 OrderFilled.last_px | 公开 Data API trade 价格 |
| --- | --- | --- | --- | --- |
| Busan | 29.99 份 × 0.991；15.040125 份 × 0.992 | 约 0.991334001404615 | 0.9910 | 0.9913340014 |
| Qingdao | 35 份 × 0.992；5 份 × 0.994 | 0.99225 | 0.9920 | 0.99225 |

这两笔是互补 outcome 的匹配：taker 买 No，maker 买 Yes。
捕获样本中的 No 价格等于 1 减去相应 Yes 价格；不能把这种换算无条件推广到不相关 token。
例如 Qingdao 的 maker Yes 价格为 0.008 和 0.006，因此对应 No 价格为 0.992 和 0.994。

原生缓存的整笔成交价恰好等于 WS trade 顶层 price，忽略了不同 maker 档位。
数量没有因此变成零或少记，但成本、平均开仓价、费用估算和 PnL 会受到影响。

### 已定位的本地调用链

```text
WS trade -> normalize_stream ┐
                            ├-> admit_taker_leg
CLOB REST -> normalize_rest ┘     last_px = trade.taker_price
                                 commission = compute_commission(size, top_price, ...)
                                      -> AdmittedLeg -> OrderFilled / FillReport -> Position
```

关键文件与函数，行号对应诊断时本地版本：

- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/execution/settlement/admission.rs:347`
  — admit_taker_leg。
- 同文件 normalize_stream（约 272 行）、normalize_rest（约 304 行）。
- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/execution/reconciliation.rs`
  — 恢复报告通过 admit_trade_legs(TradeEvidence::Rest(...)) 复用同一 admission。

本轮没有获取这些成交的认证 CLOB REST 响应。已确认的是共享代码路径，
不能把公开 Data API trades 当成认证 CLOB REST 的原始响应。

### 最小数值复现

以下字段是去除身份信息的经济值子集，不是可直接投入完整原生 WS parser 的有效报文：

```json
{
  "trader_side": "TAKER",
  "side": "BUY",
  "outcome": "No",
  "size": "40",
  "price": "0.992",
  "maker_orders": [
    {"side": "BUY", "outcome": "Yes", "matched_amount": "35", "price": "0.008"},
    {"side": "BUY", "outcome": "Yes", "matched_amount": "5", "price": "0.006"}
  ]
}
```

```python
from decimal import Decimal as D

quantity = D("35") + D("5")
notional = D("35") * (1 - D("0.008")) + D("5") * (1 - D("0.006"))
assert quantity == D("40")
assert notional == D("39.690")
assert notional / quantity == D("0.99225")
assert D("40") * D("0.992") == D("39.680")
```

整笔按 0.992 记价会少记 0.010 名义成本。费用另算，不能用手续费解释这个差额。

### 候选修复与必要回归

修复应位于现有 adapter 的共享成交解析边界，不应由策略补写 Position 或建立第二套账本。
需要验证 maker 集合是否完整、数量合计是否对应 taker、同/互补 outcome 的价格方向，
并选择能正确表达逐腿经济值或加权均价的原生 fill 表示。

加权均价不一定落在下单 tick 上；例如 0.99225 不应先被粗略量化为 0.992。
若使用单个汇总 fill，必须处理原生 Price 的可表示精度并保留正确总量、TradeId 和去重行为。
若使用逐腿 fills，必须验证新增 trade ID 语义、重连、持久化回放及重复事件处理。

建议回归：单档、多档、同 outcome、互补 outcome、BUY/SELL、maker 路径、缺失/重复腿、
数量不一致、非 tick 均价、WS/REST 一致性、MATCHED/CONFIRMED 重复通知与失败回滚。
修正未来解析不会自动修好既有持久化 fill，历史修复必须另行审计。

## 问题二：报告均价经过截断，原生恢复却按精确点值比较

### 现象

London 买入 40 份，原生 fill 与 maker 明细支持价格 0.975，手续费单独为 0.04875。
原生 Position 从持久化回放得到 avg_px_open=0.975；后续公开 v1/v2 positions 返回 0.9749。

本地默认比较为：

```text
abs(cached_average - reported_average) / abs(reported_average) <= 0.0001
abs(0.975 - 0.9749) / 0.9749 ≈ 0.000102574623 > 0.0001
```

数量相同仍会失败，报错为：

```text
position recovery did not restore the reported average entry price
```

### 已复现的 API 计算链

额外字段 grossInitialValue 揭示了中间精度：

| 步骤 | 数值 |
| --- | --- |
| activity 中记录的含费金额 | 39.04875 |
| 原始含费均价 | 39.04875 / 40 = 0.97621875 |
| 含费均价先截至六位 | 0.976218 |
| 乘回数量，符合 v1 grossInitialValue | 39.04872 |
| 每份手续费 | 0.04875 / 40 = 0.00121875 |
| 截断后的含费均价减每份手续费 | 0.97499925 |
| 净均价截至四位，符合 avgPrice / avg_price | 0.9749 |
| 净成本截至四位，符合 initialValue / entry_cost_usdc | 38.9999 |

这条链可以完全用整数和 Decimal 复现，不需要假设实际成交价改变，也不依赖二进制浮点误差。

```python
from decimal import Decimal as D, ROUND_DOWN

floor6 = lambda x: x.quantize(D("0.000001"), rounding=ROUND_DOWN)
floor4 = lambda x: x.quantize(D("0.0001"), rounding=ROUND_DOWN)
gross_average = floor6(D("39.04875") / D("40"))
net_average = gross_average - D("0.04875") / D("40")
assert gross_average * D("40") == D("39.04872")
assert net_average == D("0.97499925")
assert floor4(net_average) == D("0.9749")
assert floor4(net_average * D("40")) == D("38.9999")
```

同一模型对 20 个买入后仍持有的标的进行了交叉验证：每笔 gross cash/share 先作六位整数除法，
再累计六位数量加权均价，扣除接口报告的 attributed fee，最后按各字段输出精度截断。
模型复现 v1 四个字段共 80 项，以及 v2 对应四个字段共 80 项。
手续费是观察输入；v2 total_cost 是其文档规定的输出净成本与输出手续费之和，
这些字段不是 160 次相互独立的实验，也不构成链上现金流验证。

### 责任判断与尚未证明的内容

这是接口报告精度与原生校验语义的不兼容。策略没有参与原生均价恢复计算。
现有证据不意味着 Polymarket 将均价截断本身违反其合同，也不能据此宣布 Nautilus
所有 venue 的默认容差错误。

截断模型有较强样本支持，但没有拿到当前 Data API 后端实现，且样本不覆盖部分卖出、转入、
转出、修正或完整平仓后重新开仓。公开历史 PnL subgraph 的整数算法只能作参考，
不能证明它就是当前 API 的部署代码。

### 本地相关位置

- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/http/models.rs:677`
  — DataApiPosition，读取 avg_price。
- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/execution/reconciliation.rs:1041`
  — 将 API 均价传入 PositionStatusReport。
- `/data/liuchang/nautilus_trader/crates/live/src/execution/manager.rs:1464`
  — position_report_prices_match。
- `/data/liuchang/nautilus_trader/crates/execution/src/reconciliation/positions.rs:855`
  — position_prices_match，默认相对容差 0.0001。

### 上游讨论和修复方向

先确定 venue 的报告精度、成本语义及可接受误差范围，再讨论精度感知的比较方式。
任何误差区间都必须有明确依据，并继续拒绝真实成本不一致。
不应通过全局提高容差、清空报告均价、跳过所有持仓报告，或把缓存成本改为 API 值来解决。
也不能用已截断的 entry_cost / current_size 当作无损恢复的真实成交均价。

建议测试：截断边界两侧、真实超界差异、低价市场、数量一致但均价不同、多个策略持仓聚合、
部分退出、费用独立核算、缺失均价以及其他 venue 行为不变。
本问题适合先提交设计讨论；目前尚无已经验证的通用修复补丁。

## 问题三：明确零费率被补全为 0.10

### 可复现输入和输出

当次公开 Gamma 对两城当日样本返回：

```json
{
  "feeType": "zero_fees",
  "feesEnabled": true,
  "feeSchedule": {"rate": 0, "exponent": 1, "rebateRate": 0, "takerOnly": true}
}
```

同一市场的首个 token 和所持 No token 的 CLOB fee-rate 响应均为：

```json
{"base_fee": 1000}
```

缓存中原始 gamma_market 保留 rate=0，规范化 info.fee_schedule 却是：

```json
{"rate": "0.10", "exponent": "1", "rebateRate": "0.25", "takerOnly": true}
```

0.10 是费用曲线系数，不是“每笔直接扣名义金额的 10%”。

### 根因路径

1. classify_fee_type 未识别 zero_fees，无法优先归类为 FeeFree。
2. classify_market 按天气标签回退为 Standard。
3. enrich_market_fee_schedule 用该类别的 rebate=0.25 覆盖明确返回的 rebate=0。
4. enrich_markets 看到 rate=0 且 rebate 非零，将它视作需要 CLOB 补全的费率。
5. base_fee=1000 除以 10000 得到 0.10，随后按 token 缓存并写入 instrument。

相关本地位置：

- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/http/parse.rs:317`
  — classify_fee_type。
- 同文件 enrich_market_fee_schedule（约 231 行）、classify_market。
- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/http/gamma.rs:506`
  — enrich_markets 和 fee_rate_cache。
- `/data/liuchang/nautilus_trader/crates/adapters/polymarket/src/execution/parse.rs:447`
  — instrument_taker_fee 读取规范化 schedule。

### 影响与证据边界

北京当日几笔成交的公开 activity 金额恰好等于名义金额，positions 的 attributed fee 也为 0；
原生例如对 5 份、0.998 的成交记录了 0.001000 pUSD。
这与错误补成的 0.10 系数相符，说明均价正确并不保证 commission 正确。

两城 15 个持仓样本中，10 个原生 commission 与公开 attributed fee 不同：
原生合计 0.009980，接口合计 0.00493，相差 0.005050 pUSD。
其中还包含舍入末位差异，不能把全部差额都归因于零费率回退。
这也不是钱包总现金对账；当前 metadata 不能倒推每笔成交时刻的历史费率。

### 最小候选补丁和回归

优先让明确的 zero_fees 识别为 FeeFree，避免类别返佣补全触发 CLOB 回退。
保留已有非零 schedule、未知分类和真正缺失 schedule 的处理，避免把所有零值都一概当成
同一种情况。应检查费率缓存是否会继续覆盖更新后的权威 metadata。

建议用 Gamma HTTP mock 构造上述零费率市场，并让 CLOB mock 始终返回 1000：
期望最终费率与返佣仍为 0，而且不发生该回退请求。
再覆盖 feesEnabled=false、正常收费市场、未知 feeType、缺失 schedule，以及 metadata
从收费变成明确零费率的更新。已有测试不能为了通过而简单删除或改成更弱断言。

## 手续费舍入：相关证据，暂不另立已确认的通用 bug

正确处理分档后，五笔全球批次的费用曲线结果整体向下截五位，可在 10^-6 现金精度内
吻合 activity 含费金额减 maker 名义金额。Qingdao 的 0.015379 对应 0.01537；
Karachi 的约 0.031247964 对应 0.03124。
本地 compute_commission 使用 round_dp(5)，会分别得到 0.01538、0.03125。

应先汇总再处理精度；Busan 若各腿分别截断后相加，会得到 0.01933，而观察值为 0.01934。
但官方费用页尚未提供这里需要的完整舍入合同；本轮链上收据读取被网络拒绝，
不能声称已用 OrderFilled/FeeCharged 独立核实实际链上扣费。
因此此证据应随问题一提供给维护者，不宜直接据此改所有市场的舍入政策。

## 提交上游前建议的组织方式

1. 先把“明确 zero_fees 被回退”做成最小独立复现，核查上游当前 develop 是否存在相同路径。
2. 多档 taker 记价另开 issue/PR，重点讨论完整 maker 集合、精度、TradeId、去重和费用语义。
3. 报告均价精度作为独立兼容性讨论，先明确合同再设计实现。

不要把三个问题合成“放宽 Polymarket 恢复容差”的大补丁。
前两项修复若需要改原生 adapter，需跑相关 Rust/adapter 回归并重新编译 PyO3 扩展。
现网运行库替换、服务重启和历史持仓修复与源码 PR 是不同步骤，应分别安排。
本项目策略继续复用 Nautilus 原生账户/订单/持仓管理，没有必要在策略层增加影子账本。

本文不是已完成的 PR 验收声明。全仓检查未执行；用户要求当前只验证相关实盘范围。
向官方上游提交时，应重新遵循其当时的贡献要求，并由人工核实描述、测试和披露义务。

## 证据目录与复现入口（仅供本机评审）

原始响应包含公开钱包标识及账户持仓信息，不能直接整包上传。
公开复现优先使用本文的去身份经济值示例；完整 native fixture 还需补充有效身份和 instrument
字段，并在分享前审查。不要上传交易凭据、Redis 连接配置或未经筛选的日志。

| 内容 | 本机完整路径 |
| --- | --- |
| 本地原生成交、22 个持仓回放及逐标的比较 | `/data/liuchang/nautilus_trader/polymarket/operations/tmp/position-average-audit-20261021-144952/` |
| 原生回放与 maker 明细离线验证 | `/data/liuchang/nautilus_trader/polymarket/operations/tmp/position-average-audit-20261021-144952/verify_offline.py` |
| 截断模型、Gamma/CLOB 费用响应及手续费比较 | `/data/liuchang/nautilus_trader/polymarket/operations/tmp/trade-root-cause-20261021-150753/` |
| 20 个持仓的定点模型离线复现 | `/data/liuchang/nautilus_trader/polymarket/operations/tmp/trade-root-cause-20261021-150753/reproduce_root_causes.py` |
| 最初伦敦缓存与公开 API 原始证据 | `/data/liuchang/nautilus_trader/polymarket/research/2026-10-20-global-weather-capacity/tmp/mr-closeout-20261020/followup/` |

上述离线脚本只读取已保存文件，不联网、连接 Redis 或启动交易节点。
它们是诊断复现，不代替尚未编写并运行的上游原生 parser/执行管理器回归。

## 公开参考资料

- [Polymarket 用户成交事件字段](https://docs.polymarket.com/trading/realtime-order-updates)。
- [Polymarket 费用曲线](https://docs.polymarket.com/trading/fees)。
- [Polymarket Data API v2 schema](https://data-api.polymarket.com/v2/docs)。
- [CTF Exchange V2 显式费用结算](https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Trading.sol)。
- [历史 PnL subgraph 整数均价更新（固定提交）](https://github.com/Polymarket/polymarket-subgraph/blob/7a92ba026a9466c07381e0d245a323ba23ee8701/pnl-subgraph/src/utils/updateUserPositionWithBuy.ts)。

公开资料用于核对字段和机制，不证明本地缺陷在官方 Nautilus 当前版本中仍然存在。
