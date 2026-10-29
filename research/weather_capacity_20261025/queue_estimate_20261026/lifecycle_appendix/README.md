# 生命周期详细数据附录

此目录保存主文档引用的数据副本及提取结果，不依赖临时截图路径。归档脚本在上一级 `archive_lifecycle.py`，来源记录和数据文件 SHA-256 校验值在 `manifest.json`。

| 文件 | 内容 |
|---|---|
| `orders_screenshot.png` | 用户提供的订单截图原图 |
| `orders_and_gamma.csv` / `.json` | 12 笔订单的截图转录、condition ID、确认时间、终止回报、同价盘口数量及 Gamma 状态时间 |
| `gamma_conditions.csv` / `.json` | 北京、上海全部 22 个温度 condition 的关键字段 |
| `highest-temperature-in-*.json` | 两个城市 10/26 市场的完整已保存 Gamma 响应副本 |
| `market_resolved_raw.csv` / `.json` | 从小时 parquet 提取的 22 条公开 market_resolved 记录，保留源时间、接收时间、condition、赢家 |
| `resolution-state-beijing16.json` | 北京 16°C 的已保存 resolution API 响应，包含交易哈希 |
| `refined.csv` / `.json` | 挂单确认前重放后的同价深度、最近更新距离、剪枝数量及重放状态 |
| `estimates.csv` / `.json` | 初版快照估计，保留用于追溯；最终盘口值以 refined 为准 |
| `halfhour_trades.csv` / `.json` | 原分析的逐笔 NO 成交明细，保留原始日期范围，使用时筛选 10/26 |
| `halfhour_all_windows.csv` / `.json` | 原分析全部半小时窗口，包括没有成交的窗口 |
| `halfhour_windows_with_trades.csv` / `.json` | 有成交的半小时窗口 |
| `halfhour_REPORT.md` | 原半小时分析报告副本 |
| `live_awc_events.json` | 已整理的实录 AWC 事件副本 |

## 字段及口径

- `ack_beijing` 和 `terminal_beijing` 为北京时间，显式带 `+08:00`。原始市场消息时间保留其时区表示，不要把 UTC 直接当北京时间。
- `terminal_beijing` 为空表示截图没有找到终止回报，不表示市场仍可交易。
- `same_price_depth_shares` / `replayed_bid999_shares` 是 NO 0.999 买档的份数，不是订单笔数，也不是已证实排在自己前方的份数。
- 截图的订单信息是人工转录，不包含原始订单 ID、交易所原始终止原因和累计成交量。
- 成交量单位为份；成交额为价格乘份数的计价金额，未扣费用。公开成交不是本账户成交。
- `market_resolved_raw` 保留原始源消息与本地接收时间；它不是链上交易回执。
- Gamma 的 `closedTime` 和 resolution API 的更新时间不自动等于链上区块时间或 CLOB 最后接单时间。

## 本次归档新增确认

12 笔订单对应的 Gamma condition 均有 `customLiveness: 900`，即 **15 分钟的索引配置值**。因此已有数据支持“Gamma 配置为 15 分钟”，比完全未知更进一步；仍没有核验每个链上请求的生效参数、提案时间及争议规则。

原始小时 parquet 没有整份重复复制；相关的 22 条 resolved 记录已经独立提取保存。盘口明细保留的是现有重放结果，完整订单簿事件流仍在原数据路径。没有将缺失的链上回执或逐订单队列伪装为已归档数据。
