# 上海单 Token Depth 因子与 Nautilus 成交/PnL 一致性报告

日期：2026-07-20

## 结论

这次验证通过。

在 `highest-temperature-in-shanghai-on-june-9-2026` Event 的第一个 YES token（`18°C or below`）上，direct research replay 与 Nautilus `BacktestEngine` 的结果完全一致：

- 21,864 个 callback 上的盘口 BBO 与 `depth_imbalance_1` 逐条比对，**0 mismatch**；
- 入场与退出的成交动作序列，**0 mismatch**；
- 8 笔已完成 round trip 的逐笔 gross PnL，**0 mismatch**；
- 整个 token 的合计 gross PnL：direct = **-0.071 pUSD**，Nautilus = **-0.071 pUSD**；
- 手续费强制为 0，报表中 commission 全部为 0；
- 无订单被拒。

这比 2026-07-17 的因子回调验证多证明了一层：不只是"策略在 Nautilus callback 里能看到相同的盘口和相同的因子值"，而是"在同一份因子信号上，Nautilus 原生撮合产生的成交价与 PnL 等于 direct oracle 的结果"。

## 策略口径

- 因子：`depth_imbalance_1`。
- 入场：因子 `>= 0.10` 时，以当时 best ask 市价 market BUY 1 份。
- 退出：持仓满 120 秒后，在第一个存在可用 best bid 的 callback 上 market SELL。
- 手续费强制为 0，只比较无费用口径下的 PnL。
- direct 侧的 PnL oracle：`exit_bid - entry_ask`。

必须区分：现有 label `future_mid_return_120s` 是预测研究口径的 mid 收益，不是成交 PnL。本实验的入场吃 ask、退出吃 bid，天然承担 spread，所以它是一个可执行口径的 executable oracle，用来校验 Nautilus 的成交与 PnL，而不是用来宣称信号有收益。

## 真实 Token 结果

| 项目 | 数值 |
| --- | ---: |
| Outcome | `18°C or below` YES |
| Native callbacks | 21,864 |
| Direct callback rows | 21,864 |
| Factor/BBO mismatch | 0 |
| Native fills | 17 |
| 完成 round trips | 8 |
| Direct / native action mismatch | 0 |
| Direct / native round-trip PnL mismatch | 0 |
| Direct gross PnL | -0.071 pUSD |
| Nautilus gross PnL | -0.071 pUSD |
| Commission | 0 |
| Order rejection | 0 |
| 期末未平仓 | LONG 1 @ 0.004 pUSD |

`positions` 报表 9 行 = 8 笔已平仓 round trip + 1 笔期末未平仓（sequence 7536，`ts_init` 1780824818465000001，direct 与 native 的价格、方向、数量、时间戳全部一致）。

入场是 BUY 吃 ask、退出是 SELL 吃 bid，因此这组交易整体为负。这正说明 mid 口径的预测标签不能直接当成成交 PnL；本实验要证明的是 direct oracle 与 Nautilus 在同一可执行口径下给出同一个数，而不是这个信号能赚钱。

持仓满 120 秒后仍然缺 bid 的 callback 共 14,304 次，direct 与 native 一致，**这不是交易次数**。这 14,304 次说明该 outcome 在大量时间里没有可成交的 bid，退出只能继续等待，不允许用未来信息提前平掉这笔交易。

## 合成边界 case

为了覆盖真实数据里没有出现的边界状态，另外构造了三个合成 case，全部 pass：

1. `normal_round_trip`：正常 round trip，direct 与 Nautilus 同为 `-0.10`。
2. `missing_entry_ask_at_0999`：tick size 精度为 `0.001`，盘口只有 bid `0.999` 而没有 ask，不入场，0 fills。
3. `missing_exit_bid_then_recovers`：退出时刻缺 bid，等到 bid 恢复后才成交，direct 与 Nautilus 同为 `-0.11`。

其中 `0.999` 这个 case 顺带确认了极端价格下的 BBO 解析和 tick 合法性判断没有出错。

## 适用边界

本报告的一致性结论**只适用于**以下限定条件：

- 只覆盖 1 个真实 YES token；
- fee 强制为 0；
- market fill 由 Nautilus 原生 L2 撮合产生，未验证 queue、排队优先级、部分成交与滑点模型；
- 未验证结算与 settlement 口径；
- 不构成任何可交易结论或收益预期。

完整结果：

- `outputs/depth_pnl_parity.json`（真实 token，`summary_signature` = `1025526513db4a142e7f4e7cee863a75b2490ecce03cd057b096359820f4d33e`）；
- `outputs/synthetic/depth_pnl_parity.json`（三个合成边界 case，`summary_signature` = `f3a81b776c151f1221c0ae6a590289bf5bceb350ef2bc08b83411602fedddd9b`）。

---

注：本文于 2026-09-14 重写。原文在 `42b7fb839e` 提交时中文字符已被写坏为字面 `?`（经 cp936 控制台重定向导致），无法逐字节还原。重写内容全部取自未损坏的 `outputs/*.json`、`README.md` 与 `depth_pnl_parity.py`；所有数值、签名与 mismatch 计数与产物一致，措辞不保证与原文完全相同。
