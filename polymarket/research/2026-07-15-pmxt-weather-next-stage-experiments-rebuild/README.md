# PMXT 天气实验重跑（events-rebuild）

本目录重跑 2026-07-15 的两套实验需求：

- `EXPERIMENT_REQUIREMENTS.md`：6-Event pilot、E1/E2/E3、valid_obs、parity 与管理报告。
- `EVENT_LEVEL_EXPERIMENT_REQUIREMENTS.md`：36-Event token baseline，以及 lifecycle、active set、Event probability distribution 和缓存补充分析。

## 数据源

仅使用：

`C:\Projects\PolyReaper\data\curated\polymarket\events-rebuild\`

没有混用旧 `events` 目录。输入 inventory 固化在 `inputs/event_inventory.parquet`。

新数据只有 2026-06-06 至 2026-06-10 共 5 个日期，无法原样复刻旧实验的 9 日期样本。36 Event 因此在现有 5 个日期间确定性均衡抽取；样本为 36 个城市、29 clean / 7 degraded。`degraded` 在本次重跑中表示 `postCloseValidation.rowsObserved > 0`。

## 主要入口与交付物

- Pilot 报告：`report/pilot_report.md`
- Pilot scorecard：`report/boss_scorecard.md`
- 36 Event 样本：`protocol/batch_36_sample_plan.csv`
- Event-level 报告：`event_level/reports/event_level_summary.md`
- 缓存补充报告：`event_level/reports/cached_supplement_report.md`
- Event-level scorecard：`event_level/reports/boss_scorecard.md`
- 汇总指标：`compact/batch_36_event/`
- 图表：`charts/` 与 `event_level/`

## 关键边界

- 这是因子与市场结构研究，不是 PnL 回测。
- crossing markout 不是成交收益。
- direct→Nautilus parity 已使用 base Python 3.13 的编译扩展运行：8 条 YES/NO 检查中 6 条通过；Beijing YES/NO 两条因 tick 仍为 `0.01` 时出现 `0.001` 价格而失败，正式 Nautilus 回测仍被阻塞。
- 36 Event 中没有整个 Event 失败。原 24 个异常 token 中，23 个 source-time 间隔为 0-8ms 的重复 `0.01 -> 0.001` 通知已告警并按幂等抖动跳过；Wuhan 的 1 个约 325s 长间隔重复仍严格报错，因此最终为 1 个失败 token、涉及 1 个 Event。
- 新旧样本日期和 Event 组成不同，结果不能当作严格配对比较。
