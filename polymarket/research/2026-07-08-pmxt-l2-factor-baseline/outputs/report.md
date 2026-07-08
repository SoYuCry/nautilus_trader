# PMXT L2 factor baseline report

Generated: 2026-07-08T07:53:05.746305+00:00

## Scope

Research-only PMXT L2 factor panel; this is not a Nautilus strategy or backtest.
The script loads canonical replay steps through `PMXTEventV1Adapter`, validates them with `analyze_dataset_health`, and reconstructs selected-token book state in receive-time order.

## Input

- event_dir: `C:/Projects/PolyReaper/data/curated/polymarket/events/highest-temperature-in-shanghai-on-june-9-2026`
- condition_id: `0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41`
- asset_id/token_id: `93683348120137447419301462197228170067558545967622008841151707439369216600718`
- horizons_seconds: `[60, 300, 900]`
- taker_fee used for configured edge labels: `0.05`
- taker_fee source: `gamma_event.raw.market.feeSchedule.rate`

## Run summary

- loaded replay steps: 334501
- factor rows: 334501
- receive-time span: 2026-06-07T04:34:30.268000+00:00 to 2026-06-09T11:59:15.163000+00:00
- data health ok: True
- data health warnings/errors: 266587/0
- factor panel: `factor_panel.parquet` (parquet)

## Output files

- `factor_panel.parquet`
- `factor_summary.csv`
- `quantile_returns.csv`
- `fee_sensitivity.csv`
- `data_health_summary.json`
- `run_summary.json`
- `report.md`

## Factor construction notes

- `book` snapshots reset the selected token book.
- `price_change` updates or removes levels by side/price/size.
- `trade` contributes signed 30-second trade pressure but does not mutate the book.
- `tick_size_change` records the latest tick-size regime.
- Labels use forward as-of rows at each target receive timestamp; when multiple rows share the same receive timestamp, the label uses the last replay state at that timestamp.
- Factors use only current/past replay state.

## Caveats

- The PMXT sample is receive-time ordered by the adapter; source-time inversions remain data-health diagnostics.
- Trade aggressor-side semantics are inherited from the adapter-normalized PMXT side field.
- Fee sensitivity is descriptive; it does not simulate queue position, fill probability, or market impact.
- Missing future rows near the end of the sample produce null labels for affected horizons.

## Selected summary metrics

| metric | value |
| --- | --- |
| dataset_id | shanghai-june-9-2026-25c-yes-pmxt-l2 |
| adapter | pmxt_event_v1:v1 |
| steps | 334501 |
| factor_rows | 334501 |
| health_ok | True |
| health_issue_count | 266587 |
| first_timestamp_received | 2026-06-07T04:34:30.268000+00:00 |
| last_timestamp_received | 2026-06-09T11:59:15.163000+00:00 |
| taker_fee | 0.05 |
| taker_fee_source | gamma_event.raw.market.feeSchedule.rate |
| bid1.count | 334488 |
| bid1.mean | 0.41202617134246966 |
| bid1.p05 | 0.14 |
| bid1.p50 | 0.35 |
| bid1.p95 | 0.975 |
| ask1.count | 334196 |
| ask1.mean | 0.44186517791954427 |
| ask1.p05 | 0.17 |
| ask1.p50 | 0.38 |
| ask1.p95 | 0.985 |
| bid_size1.count | 334488 |
| bid_size1.mean | 63.70602296644424 |
| bid_size1.p05 | 5.0 |
| bid_size1.p50 | 26.4 |

## Quantile return preview

| factor | horizon_seconds | quantile | count | mean_future_mid_return | median_future_mid_return |
| --- | --- | --- | --- | --- | --- |
| microprice_edge | 60 | 1 | 66889 | 0.004668861845744443 | 0.0 |
| microprice_edge | 60 | 2 | 66779 | 0.001325394210754878 | 0.0 |
| microprice_edge | 60 | 3 | 66835 | 0.003186833246053713 | 0.0 |
| microprice_edge | 60 | 4 | 66849 | 0.00458130263728702 | 0.0 |
| microprice_edge | 60 | 5 | 66823 | 0.008331121021205276 | 0.0 |
| microprice_edge | 300 | 1 | 66889 | 0.013484399527575533 | 0.0 |
| microprice_edge | 300 | 2 | 66752 | 0.0057733026725790984 | 0.0 |
| microprice_edge | 300 | 3 | 66835 | 0.019231772275005606 | 0.0 |
| microprice_edge | 300 | 4 | 66849 | 0.01397831680354231 | 0.0 |
| microprice_edge | 300 | 5 | 66823 | 0.026030019604028554 | 0.0 |
| microprice_edge | 900 | 1 | 66889 | 0.029940782490394537 | 0.0040000000000000036 |
| microprice_edge | 900 | 2 | 66656 | 0.02333103546567451 | 0.0 |
| microprice_edge | 900 | 3 | 66835 | 0.03768780579037929 | 0.0 |
| microprice_edge | 900 | 4 | 66849 | 0.031986910798964825 | 0.0 |
| microprice_edge | 900 | 5 | 66823 | 0.06982205228738608 | 0.0050000000000000044 |
| depth_imbalance_1 | 60 | 1 | 66880 | 0.0011610421650717715 | 0.0 |
| depth_imbalance_1 | 60 | 2 | 66902 | 0.003462422648052375 | 0.0 |
| depth_imbalance_1 | 60 | 3 | 67070 | 0.007983211570001491 | 0.0 |
| depth_imbalance_1 | 60 | 4 | 68229 | 0.006173042254759709 | 0.0 |
| depth_imbalance_1 | 60 | 5 | 65094 | 0.003239223277106951 | 0.0 |
| depth_imbalance_1 | 300 | 1 | 66853 | 0.005897708405008005 | 0.0 |
| depth_imbalance_1 | 300 | 2 | 66902 | 0.016245702669576395 | 0.0 |
| depth_imbalance_1 | 300 | 3 | 67070 | 0.023886029521395555 | 0.0 |
| depth_imbalance_1 | 300 | 4 | 68229 | 0.023377596036875815 | 0.0 |
| depth_imbalance_1 | 300 | 5 | 65094 | 0.00873072787046425 | 0.0 |
| depth_imbalance_1 | 900 | 1 | 66757 | 0.01653169705049658 | 0.0 |
| depth_imbalance_1 | 900 | 2 | 66902 | 0.03819287913664763 | 0.0 |
| depth_imbalance_1 | 900 | 3 | 67070 | 0.052278738631280755 | 0.0014999999999999458 |
| depth_imbalance_1 | 900 | 4 | 68229 | 0.05529069017573174 | 0.0 |
| depth_imbalance_1 | 900 | 5 | 65094 | 0.02985204473530587 | 0.0050000000000000044 |

## Fee sensitivity

| horizon_seconds | taker_fee | long_count | long_positive_rate | long_mean_edge | short_count | short_positive_rate | short_mean_edge |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 60.0 | 0.0 | 334196.0 | 0.05935139858047373 | -0.02471156447114867 | 334175.0 | 0.02351462557043465 | -0.033519712725368446 |
| 60.0 | 0.001 | 334196.0 | 0.05935139858047373 | -0.02571156447114867 | 334175.0 | 0.02351462557043465 | -0.03451971272536845 |
| 60.0 | 0.002 | 334196.0 | 0.05900130462363404 | -0.02671156447114867 | 334175.0 | 0.023493678461883744 | -0.03551971272536845 |
| 60.0 | 0.005 | 334196.0 | 0.05698153179571269 | -0.029711564471148668 | 334175.0 | 0.023029849629685045 | -0.038519712725368443 |
| 60.0 | 0.05 | 334196.0 | 0.021104381859746973 | -0.07471156447114867 | 334175.0 | 0.0059609486047729485 | -0.08351971272536846 |
| 300.0 | 0.0 | 334196.0 | 0.13681193072328812 | -0.012158589570192347 | 334148.0 | 0.07656487544441386 | -0.04353124663322839 |
| 300.0 | 0.001 | 334196.0 | 0.13681193072328812 | -0.013158589570192345 | 334148.0 | 0.07656487544441386 | -0.0445312466332284 |
| 300.0 | 0.002 | 334196.0 | 0.13664436438497168 | -0.014158589570192344 | 334148.0 | 0.07626261417096616 | -0.04553124663322839 |
| 300.0 | 0.005 | 334196.0 | 0.13595913775149912 | -0.017158589570192343 | 334148.0 | 0.07625662879921472 | -0.04853124663322839 |
| 300.0 | 0.05 | 334196.0 | 0.06865731486911872 | -0.062158589570192345 | 334148.0 | 0.01569364473227432 | -0.09353124663322838 |
| 900.0 | 0.0 | 334196.0 | 0.22283031514440627 | 0.01136442387102179 | 334052.0 | 0.11338653862272939 | -0.06572040580508424 |
| 900.0 | 0.001 | 334196.0 | 0.22283031514440627 | 0.010364423871021796 | 334052.0 | 0.11338653862272939 | -0.06672040580508423 |
| 900.0 | 0.002 | 334196.0 | 0.22037367293444565 | 0.00936442387102179 | 334052.0 | 0.11250044903188725 | -0.06772040580508423 |
| 900.0 | 0.005 | 334196.0 | 0.21564590838908904 | 0.006364423871021796 | 334052.0 | 0.11242561038401207 | -0.07072040580508425 |
| 900.0 | 0.05 | 334196.0 | 0.11378951274102622 | -0.0386355761289782 | 334052.0 | 0.042634080921533174 | -0.11572040580508425 |

## Data-health issue preview

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
