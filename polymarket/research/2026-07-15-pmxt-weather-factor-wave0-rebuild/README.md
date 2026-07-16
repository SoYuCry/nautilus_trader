# 7 月 14 日单因子实验重跑（events-rebuild）

本目录使用 7 月 14 日的原始脚本和实验定义，仅替换数据源后重跑。

## 输入与口径

- 数据源：`C:\Projects\PolyReaper\data\curated\polymarket\events-rebuild\`
- 候选 Event：227 个完整生命周期 Event。
- 选择规则：沿用原脚本，按 `rows_written` 升序选择前 10 个 Event。
- 因子：`depth_imbalance_1`。
- horizon：30 秒、120 秒、600 秒。
- 每个二元 Market 只使用 YES token；先在 Event 内对 token 等权，再对 Event 等权。
- 未混用旧的 `events` 目录。

实际输入快照见：

- `inputs/event_inventory.parquet`
- `inputs/selected_events.json`

## 结果

| 数据 | horizon | Median Event IC | Positive Event Share | Median Hit Rate | Median Top-Bottom |
| --- | ---: | ---: | ---: | ---: | ---: |
| 旧数据 | 30s | 0.0868 | 90% | 0.573 | 0.00075 |
| rebuild | 30s | 0.0619 | 100% | 0.584 | 0.00043 |
| 旧数据 | 120s | 0.1115 | 90% | 0.570 | 0.00121 |
| rebuild | 120s | 0.0651 | 100% | 0.568 | 0.00051 |
| 旧数据 | 600s | 0.1326 | 100% | 0.597 | 0.00205 |
| rebuild | 600s | 0.0901 | 100% | 0.566 | 0.00178 |

方向一致性比旧样本更整齐，但 IC 和 top-bottom 整体下降。由于新旧选择出的 Event 不同，这不是同一样本的配对比较，只能说明结论在新候选数据上仍保持正向、但信号强度较弱。

## 运行异常

- 10 个 Event 全部完成。
- 110 个 YES token 全部生成指标，失败数为 0。
- 原 7 个失败 token 均属于 source-time 相隔 1ms 的重复 `0.01 -> 0.001` 通知；当前实现会显式发出 warning，并跳过第二次幂等通知。
- 超过 10ms 的重复通知仍严格失败；该 10 Event 样本没有出现长间隔异常。

因此，本次结果可以用于确认单因子方向是否复现；tick-size 抖动不再造成样本缺失，但 warning 仍保留在运行日志中用于数据质量审计。

详细输出：`outputs/depth_imbalance_1_rebuild_10/`。
