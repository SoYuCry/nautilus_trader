# PMXT Weather Event-level Scorecard

日期：2026-07-15

## 结构发现

- Lifecycle 主窗口：6-24h（120s IC 最高）；<1h coverage 为零，只保留为数据诊断。
- Active set：暂不采用；all IC=0.053/crossing=-0.0064，dynamic IC=0.083/crossing=-0.0115。
- Distribution：pressure/mean IC median=0.074，但 sum deviation P95=0.097、max=4.370，只保留候选结构信号。
- 单 token baseline 角色：局部盘口特征与配对基准，不作 Alpha 声明。

## 下一步

- 是否扩 100：否，本轮停止于 36 Event。
- 是否进入 Nautilus：否，native parity 后再评估。
- Stop condition：所有 crossing 为负；失败 token=1；skipped token=1；概率和偏差 P95=0.097。