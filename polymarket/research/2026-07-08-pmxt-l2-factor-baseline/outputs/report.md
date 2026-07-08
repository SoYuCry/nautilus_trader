# PMXT L2 因子研究报告

生成时间: 2026-07-08T09:43:21.521872+00:00

## 0. 信任边界

这份报告是 **PMXT L2 因子研究**，不是 Nautilus 回测、成交、PnL 或可交易收益报告。

- 不计算手续费、返佣、订单状态、排队、部分成交、现金、仓位或 PnL。
- 不把 `future_bid - current_ask` 之类的量解释成可成交利润。
- fee/fill/PnL 必须放到 Nautilus 原生策略回测入口里处理。
- 本报告只回答：按 receive-time replay 重建 L2 后，盘口因子和未来 mid-return 标签是否可用于研究。

## 1. 输入

- event_dir: `C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026`
- condition_id: `0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41`
- asset_id/token_id: `93683348120137447419301462197228170067558545967622008841151707439369216600718`
- horizons_seconds: `[60, 300, 900]`

## 2. 运行摘要

- loaded replay steps: 334501
- raw factor rows: 334501
- valid-book analysis rows: 329121
- receive-time span: 2026-06-07T04:34:30.268000+00:00 to 2026-06-09T11:59:15.163000+00:00
- replay-order hard check ok: True
- data-health warnings/errors: 266587/0
- valid/locked/crossed/missing book rows: 329121/2962/2100/318
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
- `run_summary.json` / `report.md`

## 4. 数据健康解释

`replay-order hard check ok` 只表示 receive-time replay 顺序没有硬错误；它不表示 source timestamp 完美。
PMXT source timestamp 的乱序和延迟只作为 warning 暴露，因子研究仍然按 `timestamp_received` 回放。

- receive_time_inversion_count: 0
- sequence_inversion_count: 0
- source_time_inversion_count: 89057
- source_delay_over_threshold_count: 177530
- max_source_delay_ms: 330692.0
- missing_source_timestamp_step_count: 0

## 5. 盘口有效性

| book_validity | count | share |
| --- | --- | --- |
| valid | 329121 | 0.9839163410572763 |
| locked | 2962 | 0.008854981001551564 |
| crossed | 2100 | 0.006278008137494357 |
| missing | 318 | 0.000950669803677717 |

Spread 分布诊断:

| metric | value |
| --- | --- |
| spread_count | 334183.0 |
| spread_min | -0.10999999999999999 |
| spread_p05 | 0.009000000000000008 |
| spread_p50 | 0.020000000000000018 |
| spread_p95 | 0.07 |
| spread_max | 0.868 |

说明：正式因子统计默认只使用 `book_validity == valid` 的 current row。
crossed/locked/missing row 不删除；它们留在 panel 中用于诊断数据和 replay 质量。

## 6. 标签滑移

| horizon_seconds | rows | matched_rows | missing_future_rows | mean_slippage_seconds | p95_slippage_seconds | max_slippage_seconds | future_valid_rows | future_invalid_rows |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 60.0 | 334501.0 | 334498.0 | 3.0 | 1.9430056263415627 | 8.588149999999965 | 173.563 | 328915.0 | 5586.0 |
| 300.0 | 334501.0 | 334492.0 | 9.0 | 2.3159137647537156 | 10.295 | 147.909 | 329297.0 | 5204.0 |
| 900.0 | 334501.0 | 334471.0 | 30.0 | 2.5583437338364163 | 11.4575 | 174.518 | 330361.0 | 4140.0 |

标签构造：对每个 row 的 `timestamp_received + horizon` 做 forward as-of，使用目标时间之后第一条 receive-time replay 状态。
如果同一 receive timestamp 有多条 replay row，标签用该 timestamp 的最后一个重建状态。

## 7. 因子构造说明

- `book` snapshot 重置 selected token book。
- `price_change` 按 side/price/size 更新或删除价位。
- `trade` 只贡献 signed 30-second trade pressure，不直接改 book。
- `tick_size_change` 只记录当前 tick-size regime，不参与订单撮合。
- 因子只用当前/过去 replay state；future label 单独生成。

## 8. Selected summary metrics

| metric | value |
| --- | --- |
| dataset_id | shanghai-june-9-2026-25c-yes-pmxt-l2 |
| adapter | pmxt_event_v1:v1 |
| steps | 334501 |
| raw_factor_rows | 334501 |
| analysis_rows_valid_current_book | 329121 |
| replay_order_ok | True |
| health_issue_count | 266587 |
| book_valid_rows | 329121 |
| book_locked_rows | 2962 |
| book_crossed_rows | 2100 |
| book_missing_rows | 318 |
| first_timestamp_received | 2026-06-07T04:34:30.268000+00:00 |
| last_timestamp_received | 2026-06-09T11:59:15.163000+00:00 |
| bid1.count | 329121 |
| bid1.mean | 0.4125060509660581 |
| bid1.p05 | 0.14 |
| bid1.p50 | 0.35 |
| bid1.p95 | 0.975 |
| ask1.count | 329121 |
| ask1.mean | 0.44351796147921285 |
| ask1.p05 | 0.17 |
| ask1.p50 | 0.38 |
| ask1.p95 | 0.986 |
| bid_size1.count | 329121 |
| bid_size1.mean | 57.30105040395478 |
| bid_size1.p05 | 5.0 |
| bid_size1.p50 | 26.98 |
| bid_size1.p95 | 178.41 |
| ask_size1.count | 329121 |
| ask_size1.mean | 64.03577191974989 |

## 9. Quantile return preview

| factor | horizon_seconds | quantile | count | mean_future_mid_return | median_future_mid_return |
| --- | --- | --- | --- | --- | --- |
| microprice_minus_mid | 60 | 1 | 65066 | 0.0051591614668183095 | 0.0 |
| microprice_minus_mid | 60 | 2 | 65230 | 0.0010636823547447497 | 0.0 |
| microprice_minus_mid | 60 | 3 | 65131 | 0.002501681227065452 | 0.0 |
| microprice_minus_mid | 60 | 4 | 65132 | 0.004422042928207332 | 0.0 |
| microprice_minus_mid | 60 | 5 | 64301 | 0.008135231178364257 | 0.0 |
| microprice_minus_mid | 300 | 1 | 65344 | 0.01319463148873653 | 0.0 |
| microprice_minus_mid | 300 | 2 | 65300 | 0.005369157733537519 | 0.0 |
| microprice_minus_mid | 300 | 3 | 64832 | 0.012302929109081933 | 0.0 |
| microprice_minus_mid | 300 | 4 | 64871 | 0.013198108553899279 | 0.0 |
| microprice_minus_mid | 300 | 5 | 64450 | 0.026150853374709078 | 0.0 |
| microprice_minus_mid | 900 | 1 | 64235 | 0.02949174904646999 | 0.0 |
| microprice_minus_mid | 900 | 2 | 64647 | 0.020386831562176126 | 0.0 |
| microprice_minus_mid | 900 | 3 | 65339 | 0.024236719264145456 | 0.0 |
| microprice_minus_mid | 900 | 4 | 65531 | 0.030353153469350386 | 0.0 |
| microprice_minus_mid | 900 | 5 | 65536 | 0.0698736343383789 | 0.0050000000000000044 |
| depth_imbalance_1 | 60 | 1 | 65472 | 0.001386653836754644 | 0.0 |
| depth_imbalance_1 | 60 | 2 | 64817 | 0.0034625638335621816 | 0.0 |
| depth_imbalance_1 | 60 | 3 | 64886 | 0.007486414634898128 | 0.0 |
| depth_imbalance_1 | 60 | 4 | 66893 | 0.0059145725262733035 | 0.0 |
| depth_imbalance_1 | 60 | 5 | 62792 | 0.0029064212001528845 | 0.0 |
| depth_imbalance_1 | 300 | 1 | 65787 | 0.005569253803943031 | 0.0 |
| depth_imbalance_1 | 300 | 2 | 64883 | 0.01145038762079435 | 0.0 |
| depth_imbalance_1 | 300 | 3 | 64615 | 0.021528414454847947 | 0.0 |
| depth_imbalance_1 | 300 | 4 | 66588 | 0.022940124346729138 | 0.0 |
| depth_imbalance_1 | 300 | 5 | 62924 | 0.008329111308880553 | 0.0 |
| depth_imbalance_1 | 900 | 1 | 65132 | 0.014104572253270284 | 0.0 |
| depth_imbalance_1 | 900 | 2 | 63900 | 0.03049807511737089 | 0.0 |
| depth_imbalance_1 | 900 | 3 | 65264 | 0.04423523688404021 | 0.0014999999999999458 |
| depth_imbalance_1 | 900 | 4 | 67420 | 0.054768191931177684 | 0.0 |
| depth_imbalance_1 | 900 | 5 | 63572 | 0.030160101931668026 | 0.004999999999999977 |
| depth_imbalance_3 | 60 | 1 | 65157 | 0.002295977408413525 | 0.0 |
| depth_imbalance_3 | 60 | 2 | 65075 | 0.0063876680752977325 | 0.0 |
| depth_imbalance_3 | 60 | 3 | 65189 | 0.00553427725536517 | 0.0 |
| depth_imbalance_3 | 60 | 4 | 64635 | 0.004224181944766768 | 0.0 |
| depth_imbalance_3 | 60 | 5 | 64804 | 0.0027783238688969796 | 0.0 |
| depth_imbalance_3 | 300 | 1 | 65440 | 0.0038182839242053815 | 0.0 |
| depth_imbalance_3 | 300 | 2 | 64605 | 0.014779390140082036 | 0.0 |
| depth_imbalance_3 | 300 | 3 | 64704 | 0.01952999041790306 | 0.0 |
| depth_imbalance_3 | 300 | 4 | 64735 | 0.018241445894801884 | 0.0 |
| depth_imbalance_3 | 300 | 5 | 65313 | 0.013822623367476609 | 0.0014999999999999458 |

## 10. Data-health issue preview

Issue counts by code:

| code | count |
| --- | --- |
| source_delay_over_threshold | 177530 |
| source_time_inversion | 89057 |

- warning: source_time_inversion at sequence 2: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 7: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 11: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 12: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 13: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 19: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 20: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 21: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 22: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- warning: source_time_inversion at sequence 24: source timestamp moved backwards within event_type+asset_id; this is diagnostic only because replay uses receive time
- ... 266577 additional issues summarized in data_health_summary.json

## 11. Input hashes

| source_file | resolved_path | exists | size_bytes | sha256 |
| --- | --- | --- | --- | --- |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/orderbook.parquet | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\orderbook.parquet | True | 29163845 | a07ff5185b0cb2ac9e08ed3814cfbd04bbfb31d7ac6bfcd5c801696cddefffa7 |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/event_index.json | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\event_index.json | True | 6265 | 2fb0e3ce6dbb24cdae7d90f24557b8e3511100b018a0a29a5484d0b585320725 |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/gamma_event.raw.json | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\gamma_event.raw.json | True | 57210 | 39a7c7e32ffd9f8e5050e252aa37723b96919c1916e612a6da1cb0bb9c5bf954 |
| C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026/manifest.json | C:\Projects\PolyReaper\data\curated\polymarket\events\highest-temperature-in-shanghai-on-june-9-2026\manifest.json | True | 13038 | d6befe6f0ffe24f3607607b7385df0992ab287f411a0715709c81561fbeea620 |
