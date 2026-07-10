# PMXT L2 因子研究报告

生成时间: 2026-07-10T04:52:00.463390+00:00

## 0. 信任边界

这份报告是 **PMXT L2 因子研究**，不是 Nautilus 回测、成交、PnL 或可交易收益报告。

- 不计算手续费、返佣、订单状态、排队、部分成交、现金、仓位或 PnL。
- 不把 `future_bid - current_ask` 之类的量解释成可成交利润。
- fee/fill/PnL 必须放到 Nautilus 原生策略回测入口里处理。
- 本报告只回答：按 PMXT `replay_timestamp` 重建 L2 后，盘口因子和未来 mid-return 标签是否可用于研究。

Machine-readable boundary:

- data_tier: `TIER1_EXPLORATORY`
- run_grade: `TIER1_CAUTION_ORDERING_AMBIGUOUS`
- replay_clock: `timestamp`
- ordering_key: `timestamp,timestamp_received,_original_row_index`
- causality: `pmxt_source_time_ordered_not_exchange_sequence`
- execution_claims_allowed: `false`
- source_time_policy: `primary_sort_key`
- not_for_pnl: `true`
- diagnostic_non_causal: `true`
- ordering_ambiguous: `true`
- source_quality.orderingStatus: `ambiguous`
- source_quality.orderingAmbiguousRows: `9433`
- claim_boundary: Exploratory PMXT timestamp-ordered L2 factors and future mid-return labels only; stable fallback is reproducible but not proof of true exchange/message order; no fees, fills, queue position, cash, positions, PnL, executable edge, or tradeable claims.

## 1. 输入

- event_dir: `C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026`
- condition_id: `0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41`
- asset_id/token_id: `93683348120137447419301462197228170067558545967622008841151707439369216600718`
- horizons_seconds: `[60, 300, 900]`

## 2. 运行摘要

- loaded replay steps: 334501
- raw factor rows: 334501
- valid-book analysis rows: 333419
- replay_timestamp span: 2026-06-07T04:34:30.072000+00:00 to 2026-06-09T11:59:14.959000+00:00
- timestamp_received audit span: 2026-06-07T04:34:30.268000+00:00 to 2026-06-09T11:59:15.163000+00:00
- replay-order hard check ok: False
- data-health warnings/errors: 177530/1389
- valid/locked/crossed/missing book rows: 333419/697/65/320
- factor panel: `factor_panel.parquet` (parquet)

## 3. 输出文件

- `factor_panel.parquet`: raw panel，保留全部 row，并带 `book_validity` 标记。
- `factor_summary.csv`: 只基于 valid current book 的摘要。
- `quantile_returns.csv`: 因子分位数 vs future mid-return；current/future book 都要求 valid。
- `book_validity_summary.csv`: crossed/locked/missing book 统计。
- `spread_summary.csv`: raw panel 的 spread 分布诊断。
- `label_slippage_summary.csv`: forward as-of 标签匹配的时间滑移统计。
- `input_hashes.json`: 输入文件大小和 sha256，用于复现实验。
- `data_health_summary.json`: receive/source time 诊断摘要。
- `run_metadata.json`: machine-readable trust tier/run grade/clock policy/claim boundary.
- `run_summary.json` / `report.md`

## 4. 数据健康解释

`replay-order hard check ok` 来自通用 receive-time data_health；PMXT timestamp-ordered 研究会把 receive-time inversion 视为诊断，不把它当作 PMXT 研究硬失败。
PMXT v2 的当前口径按 `timestamp, timestamp_received, stable fallback` 排序；fallback 只保证可复现，不代表真实 WebSocket/message 顺序。

- receive_time_inversion_count: 1389
- sequence_inversion_count: 0
- source_time_inversion_count: 0
- source_delay_over_threshold_count: 177530
- max_source_delay_ms: 330692.0
- missing_source_timestamp_step_count: 0

## 5. 盘口有效性

| book_validity | count | share |
| --- | --- | --- |
| valid | 333419 | 0.9967653310453481 |
| locked | 697 | 0.0020837007961112225 |
| crossed | 65 | 0.00019431929949387296 |
| missing | 320 | 0.0009566488590467592 |

Spread 分布诊断:

| metric | value |
| --- | --- |
| spread_count | 334181.0 |
| spread_min | -0.020000000000000018 |
| spread_p05 | 0.009999999999999953 |
| spread_p50 | 0.020000000000000018 |
| spread_p95 | 0.07 |
| spread_max | 0.873 |

说明：正式因子统计默认只使用 `book_validity == valid` 的 current row。
crossed/locked/missing row 不删除；它们留在 panel 中用于诊断数据和 replay 质量。

## 6. 标签滑移

| horizon_seconds | rows | matched_rows | missing_future_rows | mean_slippage_seconds | p95_slippage_seconds | max_slippage_seconds | future_valid_rows | future_invalid_rows |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 60.0 | 334501.0 | 334498.0 | 3.0 | 5.595066427900915 | 9.62129999999993 | 332.053 | 333254.0 | 1247.0 |
| 300.0 | 334501.0 | 334492.0 | 9.0 | 12.240453466749578 | 95.39835000000004 | 332.151 | 333405.0 | 1096.0 |
| 900.0 | 334501.0 | 334471.0 | 30.0 | 10.427521417402405 | 59.1305 | 308.722 | 333720.0 | 781.0 |

标签构造：对每个 row 的 `replay_timestamp + horizon` 做 forward as-of；PMXT 中 `replay_timestamp` 优先使用 source `timestamp`，缺失时才退回 `timestamp_received`。
如果同一 replay timestamp 有多条 replay row，标签用该 timestamp 的最后一个重建状态；若 tied group 内容不同，需要结合 ordering_ambiguous 和 sensitivity test 降级解读。

## 7. 因子构造说明

- `book` snapshot 重置 selected token book。
- `price_change` 按 side/price/size 更新或删除价位。
- `trade` 只贡献 signed 30-second trade pressure，不直接改 book。
- `tick_size_change` 只记录当前 tick-size regime，不参与订单撮合。
- 因子只用当前/过去 replay state；future label 单独生成。

## 8. Selected summary metrics

| metric | value |
| --- | --- |
| data_tier | TIER1_EXPLORATORY |
| run_grade | TIER1_CAUTION_ORDERING_AMBIGUOUS |
| replay_clock | timestamp |
| ordering_key | timestamp,timestamp_received,_original_row_index |
| causality | pmxt_source_time_ordered_not_exchange_sequence |
| execution_claims_allowed | False |
| source_time_policy | primary_sort_key |
| not_for_pnl | True |
| ordering_ambiguous | True |
| source_quality.orderingStatus | ambiguous |
| source_quality.orderingAmbiguousRows | 9433 |
| dataset_id | shanghai-june-9-2026-25c-yes-pmxt-l2 |
| adapter | pmxt_event_v1:v1 |
| steps | 334501 |
| raw_factor_rows | 334501 |
| analysis_rows_valid_current_book | 333419 |
| replay_order_ok | False |
| health_issue_count | 178919 |
| book_valid_rows | 333419 |
| book_locked_rows | 697 |
| book_crossed_rows | 65 |
| book_missing_rows | 320 |
| first_timestamp_received | 2026-06-07T04:34:30.268000+00:00 |
| last_timestamp_received | 2026-06-09T11:59:15.163000+00:00 |
| first_replay_timestamp | 2026-06-07T04:34:30.072000+00:00 |
| last_replay_timestamp | 2026-06-09T11:59:14.959000+00:00 |
| bid1.count | 333419 |
| bid1.mean | 0.4107261043911714 |
| bid1.p05 | 0.14 |
| bid1.p50 | 0.35 |

## 9. Quantile return preview

| factor | horizon_seconds | quantile | count | mean_future_mid_return | median_future_mid_return |
| --- | --- | --- | --- | --- | --- |
| microprice_minus_mid | 60 | 1 | 66731 | 0.0047182643748782446 | 0.0 |
| microprice_minus_mid | 60 | 2 | 66522 | 0.0011373756050629864 | 0.0 |
| microprice_minus_mid | 60 | 3 | 66407 | 0.0029511572575180315 | 0.0 |
| microprice_minus_mid | 60 | 4 | 67297 | 0.004613771787746853 | 0.0 |
| microprice_minus_mid | 60 | 5 | 65708 | 0.008722035368600476 | 0.0 |
| microprice_minus_mid | 300 | 1 | 66745 | 0.0157396958573676 | 0.0 |
| microprice_minus_mid | 300 | 2 | 66455 | 0.005340079753216464 | 0.0 |
| microprice_minus_mid | 300 | 3 | 66409 | 0.01495858995015736 | 0.0 |
| microprice_minus_mid | 300 | 4 | 67377 | 0.014757810528815469 | 0.0 |
| microprice_minus_mid | 300 | 5 | 65644 | 0.028210171531289994 | 0.004999999999999949 |
| microprice_minus_mid | 900 | 1 | 66672 | 0.033835313174946 | 0.0050000000000000044 |
| microprice_minus_mid | 900 | 2 | 66396 | 0.02122231760949455 | 0.0 |
| microprice_minus_mid | 900 | 3 | 66601 | 0.032772263179231535 | 0.0 |
| microprice_minus_mid | 900 | 4 | 67478 | 0.032895877174782893 | 0.0 |
| microprice_minus_mid | 900 | 5 | 65798 | 0.07292376667983831 | 0.010000000000000009 |
| depth_imbalance_1 | 60 | 1 | 66904 | 0.0005549145043644641 | 0.0 |
| depth_imbalance_1 | 60 | 2 | 66324 | 0.004212336409142994 | 0.0 |
| depth_imbalance_1 | 60 | 3 | 66473 | 0.007608239435560301 | 0.0 |
| depth_imbalance_1 | 60 | 4 | 66838 | 0.006220772614381041 | 0.0 |
| depth_imbalance_1 | 60 | 5 | 66126 | 0.0035093836010041428 | 0.0 |
| depth_imbalance_1 | 300 | 1 | 66991 | 0.0049035094266394 | 0.0 |
| depth_imbalance_1 | 300 | 2 | 66206 | 0.014872383167688729 | 0.0 |
| depth_imbalance_1 | 300 | 3 | 66594 | 0.022659924317506082 | 0.0 |
| depth_imbalance_1 | 300 | 4 | 66747 | 0.027311946604341764 | 0.0 |
| depth_imbalance_1 | 300 | 5 | 66092 | 0.009075742903831022 | 0.0 |
| depth_imbalance_1 | 900 | 1 | 66871 | 0.015149534177745214 | 0.0 |
| depth_imbalance_1 | 900 | 2 | 66176 | 0.03662828669003869 | 0.0 |
| depth_imbalance_1 | 900 | 3 | 66686 | 0.0488654590168851 | 0.0030000000000001137 |
| depth_imbalance_1 | 900 | 4 | 67065 | 0.06266191008722881 | 0.0 |
| depth_imbalance_1 | 900 | 5 | 66147 | 0.02974519630519902 | 0.0050000000000000044 |
| depth_imbalance_3 | 60 | 1 | 66657 | 0.0015649444169404576 | 0.0 |
| depth_imbalance_3 | 60 | 2 | 66582 | 0.006528543750563215 | 0.0 |
| depth_imbalance_3 | 60 | 3 | 66565 | 0.006103365131826036 | 0.0 |
| depth_imbalance_3 | 60 | 4 | 66815 | 0.00398873007558183 | 0.0 |
| depth_imbalance_3 | 60 | 5 | 66046 | 0.003911175544317595 | 0.0 |
| depth_imbalance_3 | 300 | 1 | 66673 | 0.003846962038606336 | 0.0 |
| depth_imbalance_3 | 300 | 2 | 66533 | 0.017167578494882242 | 0.0 |
| depth_imbalance_3 | 300 | 3 | 66346 | 0.02133745063756669 | 0.0 |
| depth_imbalance_3 | 300 | 4 | 67082 | 0.020028323544318893 | 0.0 |
| depth_imbalance_3 | 300 | 5 | 65996 | 0.016471877083459596 | 0.0050000000000000044 |

## 10. Data-health issue preview

Issue counts by code:

| code | count |
| --- | --- |
| source_delay_over_threshold | 177530 |
| receive_time_inversion | 1389 |

- warning: source_delay_over_threshold at sequence 42: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 43: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 44: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 45: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 73: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 94: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 95: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 96: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 97: source timestamp is much earlier than receive time; reported for late-delivery severity
- warning: source_delay_over_threshold at sequence 98: source timestamp is much earlier than receive time; reported for late-delivery severity
- ... 178909 additional issues summarized in data_health_summary.json

## 11. Input hashes

| source_file | resolved_path | exists | size_bytes | sha256 |
| --- | --- | --- | --- | --- |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/orderbook.parquet | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\orderbook.parquet | True | 29163845 | a07ff5185b0cb2ac9e08ed3814cfbd04bbfb31d7ac6bfcd5c801696cddefffa7 |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/event_index.json | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\event_index.json | True | 6265 | 2fb0e3ce6dbb24cdae7d90f24557b8e3511100b018a0a29a5484d0b585320725 |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/gamma_event.raw.json | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\gamma_event.raw.json | True | 57210 | 39a7c7e32ffd9f8e5050e252aa37723b96919c1916e612a6da1cb0bb9c5bf954 |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/manifest.json | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\manifest.json | True | 13038 | d6befe6f0ffe24f3607607b7385df0992ab287f411a0715709c81561fbeea620 |
