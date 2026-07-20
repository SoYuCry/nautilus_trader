# Nautilus 因子回调回放 Smoke

## 目的

验证研究侧已经确认的 PMXT replay 顺序进入 Nautilus `BacktestEngine` 后，策略回调实际看到的盘口状态和一档深度不平衡是否保持一致。

本实验只回答：

1. `OrderBookDeltas` / `TradeTick` 回调是否按 native `ts_init` 顺序到达；
2. 同 source timestamp 的稳定 fallback 顺序是否进入策略回调；
3. 策略在回调中从 Nautilus `OrderBook` 读取的 `bid1` / `ask1` 是否等于 direct research replay；
4. 策略在回调中重算的 `depth_imbalance_1` 是否等于 direct research replay；
5. no-op update 是否不会被误当成新的 ranking observation / signal trigger。

## 不回答

- 不证明 PMXT tied rows 的 fallback 就是真实交易所顺序；
- 不验证完整 L2 每一档语义等价；
- 不验证成交、queue、fee、PnL 或 settlement；
- 不对缺失 `tick_size_change` 的 Beijing 数据做推断或修复。

## 验收标准

- 合成 case：callback 顺序、同时间稳定顺序、BBO、`depth_imbalance_1`、signal 序列全部一致；
- 真实数据：Karachi、Busan、Qingdao 的 YES/NO 共 6 token 全部通过；
- Beijing YES/NO 保持已知数据失败，不计入引擎正确性通过率；
- 重复运行输出摘要签名一致。

## 运行

从仓库外或任意不遮蔽已安装 Nautilus wheel 的目录运行：

```powershell
python C:\Projects\nautilus_trader\polymarket\research\2026-07-17-nautilus-factor-callback-replay-smoke\factor_callback_smoke.py --all
```

输出写入本目录 `outputs/`。
