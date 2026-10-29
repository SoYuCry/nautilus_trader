# Weather capacity research — October 2026

本目录保存 10/16–23 的每日天气策略容量估计，以及 10/25–26 北京、上海的 AWC 实录校验、盘口排队估计和市场结算生命周期分析。

## 报告入口

- [每日容量报告](results/REPORT.md)，汇总表 `results/daily_combined.csv`；城市、condition 明细位于同目录。
- [信号后成交量口径](results/post_signal_volume/README.md)。成交额是对应 NO token 的价格乘份数，不是整个 event 的成交额，也不等于自己的可成交量。
- 接收时间增加 60 / 120 秒的结果分别位于 `scenarios/receipt_plus_60s/results/` 和 `scenarios/receipt_plus_120s/results/`。
- [10/25–26 实录与历史 AWC 对照](live_validation_2526/REPORT.md)。10/25 实录覆盖不完整，不应直接比较全天总额。
- [半小时与温度桶明细](live_validation_2526/live/results/halfhour/REPORT.md)。
- [10/26 排队压力](queue_estimate_20261026/README.md)。同价深度不是精确的队列前方数量或订单笔数。
- [订单簿与结算生命周期](queue_estimate_20261026/condition_lifecycle_20261026.md)。
- [生命周期详细数据附录](queue_estimate_20261026/lifecycle_appendix/README.md)：截图、订单转录、Gamma 快照、resolved 广播、逐笔成交与来源校验值。

## 本地输入与重跑

原始市场数据位于 `C:\Projects\PolyReaper\data\external\pendulumflow\v3_1012-latest`。原工具包为 `C:\Projects\nautilus_trader\weather-capacity-tools-20261025.tar.gz`，实录包为 `C:\Projects\nautilus_trader\live-awc-20261025-26-compact.zip`。

脚本保留本次研究使用的路径和日期配置，属于研究快照，不是任意环境即装即用的产品。核心依赖包括 Python、pyarrow、requests，原工具依赖见 `weather-capacity-tools-20261025/requirements.txt`。

主要流程：`fetch_inputs.py` / `prepare.py` 准备输入；`extract_books.py` / `sample_capacity.py` 估计盘口容量；`post_signal_volume.py` 统计信号后的成交量；`validate_live_2526.py` / `halfhour_live_2526.py` 校验实录与分时统计；`queue_estimate.py` / `queue_refine.py` 重建挂单时盘口；`queue_estimate_20261026/archive_lifecycle.py` 保存生命周期附录。

在本目录执行已有检查：

```powershell
C:\Users\xhth\miniconda3\python.exe -m unittest test_capacity_estimate.py
```

`.gitignore` 排除可重新生成的输入缓存、parquet 中间分片、日志和未完成写入文件；这些文件仍保留在本地。提交包含分析脚本、结果和关键证据，完整重跑仍需要原始数据，不能只用仓库快照替代。
