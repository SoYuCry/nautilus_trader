# 上海 Depth 因子与 Nautilus PnL 一致性实验

本实验只验证一个问题：同一份上海天气 Event 数据，在 direct research replay 与 Nautilus `BacktestEngine` 中，是否产生一致的：

1. `depth_imbalance_1`；
2. 入场/退出决策；
3. 无手续费成交价格；
4. 已完成 round trip 的 gross PnL；
5. 缺 ask、缺 exit bid 等边界状态。

策略口径：`depth_imbalance_1 >= 0.10` 时以 market BUY 1 份，持有 120 秒后在首个有足够 bid 的 `OrderBookDeltas` callback 以 market SELL 退出。每个 token 同时最多一笔持仓。

现有 `future_mid_return_120s` 仍是预测研究 label，不应等同于成交 PnL。本实验新增的 executable oracle 是：

```text
gross_pnl = exit_bid - entry_ask
```

缺 entry ask 时不交易；到达 120 秒后仍缺 bid 时继续等待，不使用未来信息提前删除交易。

运行：

```powershell
python C:\Projects\nautilus_trader\polymarket\research\2026-07-20-shanghai-depth-factor-pnl-parity\depth_pnl_parity.py --all
```

