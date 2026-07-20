# PMXT Weather Event-level Scorecard

日期：2026-07-15

## 结构发现

- Lifecycle 主窗口：1-6h；<1h 作为临近结算 regime，不作统一窗口。
- Active set：暂不采用；all IC=0.057/crossing=-0.0074，dynamic IC=0.079/crossing=-0.0110。
- Distribution：pressure/mean IC median=0.073，但 sum deviation P95=0.135、max=3.935，只保留候选结构信号。
- 单 token baseline 角色：局部盘口特征与配对基准，不作 Alpha 声明。

## 下一步

- 是否扩 100：否，本轮停止于 36 Event。
- 是否进入 Nautilus：否，native parity 后再评估。
- Stop condition：所有 crossing 为负；失败 token=6；skipped token=24 且偏向早期 clean；概率和偏差 P95=0.135。