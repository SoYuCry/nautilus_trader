# PMXT 天气 maker markout / fill 诊断报告

生成时间: 2026-07-10T03:07:30.091893+00:00

## 0. 结论先行

这份报告不是 Nautilus 回测，也不是 PnL。它只回答一个更窄的问题：

> `depth_imbalance_1` 如果被当作 maker / quote-skew 信号，在粗糙 fill proxy 下，成交后的 markout 是否还能为正？

乐观 300s fill / 300s markout：median_mean_markout=`0.00120776`，median_fill_rate=`0.0246`，positive_token_share=`0.5`。 半队列假设：median_mean_markout=`-0.0019`，median_fill_rate=`0.00845`，positive_token_share=`0.159`。 全队列假设：median_mean_markout=`-0.00323`，median_fill_rate=`0.00423`，positive_token_share=`0.0909`。

核心读法：如果假设自己几乎排在队首，信号还有一点弱正 markout；但只要引入半队列/全队列假设，strategy-tail 的 median markout 很快转负或接近 0。这说明上一轮 IC 里的盘口预测性，至少在这两个 PMXT 天气 event 上，并不能直接升级成 maker 可捕获收益。

本报告比上一轮 IC 分析更保守：上一轮证明的是“盘口状态后 mid-price 倾向怎么走”；这一轮检查的是“如果我被动挂单并被成交，成交后的 mid-price markout 怎么样”。

## 1. 信任边界

- 数据源：curated PMXT event parquet。
- 回放时钟：`timestamp_received`。
- probe 抽样：每 `60` 秒取最后一个 valid book state，避免每行都提交一个虚拟订单导致严重重复计数。
- fill proxy：未来 trade touch/cross 当前 best bid/ask。
- queue proxy：需要成交量 >= displayed top size × queue fraction + order size。
- markout：成交后未来 mid-price 相对 fill price 的变化。
- 不包含 fee、rebate、真实 queue priority、撤单、库存、现金、仓位、Nautilus order state 或可执行 PnL。

## 2. Fill 模型

| queue_model | 含义 |
| --- | --- |
| optimistic | 队列前面没有别人，只要求成交量覆盖自己的 order_size。 |
| half_queue | 假设自己排在当前 displayed top size 的一半之后。 |
| full_queue | 假设自己排在当前 displayed top size 全部之后。 |

买单 fill 条件：未来 `SELL` trade price <= 当前 bid。卖单 fill 条件：未来 `BUY` trade price >= 当前 ask。

## 3. 使用的 event/token

- event_count: 2
- token_count: 22
- probe_count: 56584

| event_slug | market_index | market_label | panel_rows | probe_rows | trade_rows | first_timestamp_received | last_timestamp_received |
| --- | --- | --- | --- | --- | --- | --- | --- |
| highest-temperature-in-shanghai-on-june-9-2026 | 0 | 18°C or below | 21409 | 283 | 1 | 2026-06-07T04:34:18.488000+00:00 | 2026-06-09T11:59:09.781000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 1 | 19°C | 25726 | 420 | 3 | 2026-06-07T04:34:12.940000+00:00 | 2026-06-09T11:59:09.781000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 2 | 20°C | 62995 | 1697 | 6 | 2026-06-07T04:34:22.601000+00:00 | 2026-06-09T11:59:09.781000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 3 | 21°C | 89673 | 2527 | 7 | 2026-06-07T04:34:33.563000+00:00 | 2026-06-09T11:59:55.580000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 4 | 22°C | 144190 | 2709 | 92 | 2026-06-07T04:34:30.268000+00:00 | 2026-06-09T11:59:55.580000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 5 | 23°C | 295717 | 2905 | 477 | 2026-06-07T04:34:30.268000+00:00 | 2026-06-09T11:59:55.580000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 6 | 24°C | 346962 | 2922 | 392 | 2026-06-07T04:34:30.268000+00:00 | 2026-06-09T11:59:09.781000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 7 | 25°C | 334501 | 3204 | 617 | 2026-06-07T04:34:30.268000+00:00 | 2026-06-09T11:59:15.163000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 8 | 26°C | 333019 | 3143 | 368 | 2026-06-07T04:34:30.268000+00:00 | 2026-06-09T11:54:48.150000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 9 | 27°C | 154807 | 3066 | 233 | 2026-06-07T04:34:18.488000+00:00 | 2026-06-09T11:59:51.767000+00:00 |
| highest-temperature-in-shanghai-on-june-9-2026 | 10 | 28°C or higher | 153872 | 2996 | 165 | 2026-06-07T04:34:05.018000+00:00 | 2026-06-09T11:59:53.871000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 0 | 23°C or below | 116087 | 2431 | 6 | 2026-06-08T04:27:11.576000+00:00 | 2026-06-10T11:59:57.686000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 1 | 24°C | 79060 | 1871 | 1 | 2026-06-08T04:26:58.749000+00:00 | 2026-06-10T11:52:35.447000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 2 | 25°C | 131907 | 2696 | 10 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:57.686000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 3 | 26°C | 225981 | 2792 | 143 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:57.686000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 4 | 27°C | 397922 | 2920 | 440 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:59.045000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 5 | 28°C | 380116 | 3107 | 430 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:59.045000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 6 | 29°C | 374898 | 3083 | 255 | 2026-06-08T04:26:36.213000+00:00 | 2026-06-10T11:58:03.597000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 7 | 30°C | 339992 | 2957 | 180 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:57.957000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 8 | 31°C | 261085 | 3019 | 119 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:57.686000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 9 | 32°C | 170922 | 3000 | 29 | 2026-06-08T04:26:59.159000+00:00 | 2026-06-10T11:59:57.957000+00:00 |
| highest-temperature-in-shanghai-on-june-10-2026 | 10 | 33°C or higher | 141120 | 2836 | 16 | 2026-06-08T04:27:13.259000+00:00 | 2026-06-10T11:59:57.957000+00:00 |

## 4. Strategy-tail 汇总

strategy-tail 的定义：

- Q5 + buy_bid：因子最高分位时挂买一档；
- Q1 + sell_ask：因子最低分位时挂卖一档；
- combined：把上面两个方向合并看。

| queue_model | queue_ahead_fraction | fill_window_seconds | markout_seconds | quote_side | token_metric_count | total_probes | total_fills | median_fill_rate | median_mean_markout | positive_markout_token_share | median_hit_rate | median_avg_win | median_avg_loss_abs | median_win_loss_ratio | adverse_selection_token_share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| optimistic | 0 | 60 | 60 | buy_bid | 22 | 11321 | 133 | 0.00618633 | 0.00310268 | 0.363636 | 0.8375 | 0.00604464 | 0.0108214 | 0.87851 | 0.136364 |
| optimistic | 0 | 60 | 60 | sell_ask | 22 | 11327 | 285 | 0.017156 | 0.00369712 | 0.5 | 0.771712 | 0.00938959 | 0.0151333 | 0.636713 | 0.454545 |
| optimistic | 0 | 60 | 60 | strategy_tail_combined | 44 | 22648 | 418 | 0.00699662 | 0.00334375 | 0.431818 | 0.78293 | 0.00827446 | 0.0148 | 0.678947 | 0.295455 |
| optimistic | 0 | 60 | 300 | buy_bid | 22 | 11321 | 133 | 0.00618633 | 0.00434598 | 0.363636 | 0.74375 | 0.0147656 | 0.0161538 | 1.02586 | 0.136364 |
| optimistic | 0 | 60 | 300 | sell_ask | 22 | 11327 | 285 | 0.017156 | 0.00167372 | 0.454545 | 0.651515 | 0.010754 | 0.012775 | 0.735309 | 0.409091 |
| optimistic | 0 | 60 | 300 | strategy_tail_combined | 44 | 22648 | 418 | 0.00699662 | 0.00202604 | 0.409091 | 0.666667 | 0.0121736 | 0.0145 | 0.826471 | 0.272727 |
| optimistic | 0 | 60 | 900 | buy_bid | 22 | 11321 | 133 | 0.00618633 | 0.00460714 | 0.272727 | 0.666667 | 0.017 | 0.0124167 | 1.98147 | 0.0454545 |
| optimistic | 0 | 60 | 900 | sell_ask | 22 | 11327 | 285 | 0.017156 | 0.00314015 | 0.363636 | 0.624633 | 0.01325 | 0.0231312 | 1.13534 | 0.227273 |
| optimistic | 0 | 60 | 900 | strategy_tail_combined | 44 | 22648 | 418 | 0.00699662 | 0.00336364 | 0.318182 | 0.636364 | 0.014 | 0.0200573 | 1.1808 | 0.136364 |
| optimistic | 0 | 300 | 60 | buy_bid | 22 | 11321 | 458 | 0.0174393 | 0.0035 | 0.636364 | 0.833333 | 0.0051 | 0.0103426 | 0.772386 | 0.318182 |
| optimistic | 0 | 300 | 60 | sell_ask | 22 | 11327 | 950 | 0.0459549 | 0.00105714 | 0.545455 | 0.714286 | 0.00682558 | 0.012475 | 0.528113 | 0.590909 |
| optimistic | 0 | 300 | 60 | strategy_tail_combined | 44 | 22648 | 1408 | 0.0246039 | 0.00279349 | 0.590909 | 0.755836 | 0.00567187 | 0.0103426 | 0.580547 | 0.454545 |
| optimistic | 0 | 300 | 300 | buy_bid | 22 | 11321 | 458 | 0.0174393 | 0.00153509 | 0.545455 | 0.736842 | 0.00777778 | 0.0115 | 0.920897 | 0.318182 |
| optimistic | 0 | 300 | 300 | sell_ask | 22 | 11327 | 950 | 0.0459549 | 0.000542857 | 0.454545 | 0.631579 | 0.00871429 | 0.0116508 | 0.583728 | 0.545455 |
| optimistic | 0 | 300 | 300 | strategy_tail_combined | 44 | 22648 | 1408 | 0.0246039 | 0.00120776 | 0.5 | 0.644949 | 0.00824603 | 0.0116508 | 0.705894 | 0.431818 |
| optimistic | 0 | 300 | 900 | buy_bid | 22 | 11321 | 458 | 0.0174393 | 0.00299621 | 0.454545 | 0.683333 | 0.0096199 | 0.0117083 | 1.44452 | 0.136364 |
| optimistic | 0 | 300 | 900 | sell_ask | 22 | 11327 | 950 | 0.0459549 | 0.000541667 | 0.454545 | 0.590909 | 0.01135 | 0.0145192 | 0.72539 | 0.454545 |
| optimistic | 0 | 300 | 900 | strategy_tail_combined | 44 | 22648 | 1408 | 0.0246039 | 0.0011 | 0.454545 | 0.612903 | 0.0106026 | 0.0118315 | 0.992479 | 0.295455 |
| half_queue | 0.5 | 60 | 60 | buy_bid | 22 | 11321 | 32 | 0.00166115 | 0.00590909 | 0.0454545 | 0.545455 | 0.015 | 0.00833333 | 1.8 | 0 |
| half_queue | 0.5 | 60 | 60 | sell_ask | 22 | 11327 | 116 | 0.00501789 | 0.0009 | 0.227273 | 0.625 | 0.0076 | 0.015625 | 0.4864 | 0.318182 |
| half_queue | 0.5 | 60 | 60 | strategy_tail_combined | 44 | 22648 | 148 | 0.00181696 | 0.00129375 | 0.136364 | 0.598214 | 0.0088 | 0.0154792 | 0.52445 | 0.159091 |
| half_queue | 0.5 | 60 | 300 | buy_bid | 22 | 11321 | 32 | 0.00166115 | 0.00136364 | 0.0454545 | 0.545455 | 0.0166667 | 0.017 | 0.980392 | 0.0454545 |
| half_queue | 0.5 | 60 | 300 | sell_ask | 22 | 11327 | 116 | 0.00501789 | 0.000833333 | 0.227273 | 0.5 | 0.0139 | 0.0158333 | 0.947727 | 0.227273 |
| half_queue | 0.5 | 60 | 300 | strategy_tail_combined | 44 | 22648 | 148 | 0.00181696 | 0.00109848 | 0.136364 | 0.522727 | 0.0151687 | 0.0164167 | 0.96406 | 0.136364 |
| half_queue | 0.5 | 60 | 900 | buy_bid | 22 | 11321 | 32 | 0.00166115 | -0.00590909 | 0 | 0.454545 | 0.017 | 0.025 | 0.68 | 0.0454545 |
| half_queue | 0.5 | 60 | 900 | sell_ask | 22 | 11327 | 116 | 0.00501789 | -0.0035625 | 0.181818 | 0.5 | 0.0163 | 0.0280909 | 0.627358 | 0.227273 |
| half_queue | 0.5 | 60 | 900 | strategy_tail_combined | 44 | 22648 | 148 | 0.00181696 | -0.0047358 | 0.0909091 | 0.477273 | 0.01665 | 0.0271288 | 0.653679 | 0.136364 |
| half_queue | 0.5 | 300 | 60 | buy_bid | 22 | 11321 | 119 | 0.00675747 | -0.00234615 | 0.0909091 | 0.449519 | 0.00442857 | 0.0154375 | 0.326342 | 0.272727 |
| half_queue | 0.5 | 300 | 60 | sell_ask | 22 | 11327 | 471 | 0.0268113 | -0.000166667 | 0.318182 | 0.545455 | 0.00625 | 0.0110469 | 0.484987 | 0.590909 |
| half_queue | 0.5 | 300 | 60 | strategy_tail_combined | 44 | 22648 | 590 | 0.00844749 | -0.00103125 | 0.204545 | 0.535714 | 0.00491667 | 0.015 | 0.376606 | 0.431818 |
| half_queue | 0.5 | 300 | 300 | buy_bid | 22 | 11321 | 119 | 0.00675747 | -0.002175 | 0.136364 | 0.354167 | 0.0138462 | 0.0128 | 1.21184 | 0.136364 |
| half_queue | 0.5 | 300 | 300 | sell_ask | 22 | 11327 | 471 | 0.0268113 | -0.00137179 | 0.181818 | 0.421053 | 0.00746875 | 0.0137026 | 0.728008 | 0.545455 |
| half_queue | 0.5 | 300 | 300 | strategy_tail_combined | 44 | 22648 | 590 | 0.00844749 | -0.0019 | 0.159091 | 0.4 | 0.0075613 | 0.0128 | 0.728008 | 0.340909 |
| half_queue | 0.5 | 300 | 900 | buy_bid | 22 | 11321 | 119 | 0.00675747 | 0.0005 | 0.181818 | 0.3125 | 0.0211944 | 0.01575 | 1.65926 | 0.0909091 |
| half_queue | 0.5 | 300 | 900 | sell_ask | 22 | 11327 | 471 | 0.0268113 | 6.66667e-05 | 0.363636 | 0.460526 | 0.00970455 | 0.0127821 | 0.766378 | 0.409091 |
| half_queue | 0.5 | 300 | 900 | strategy_tail_combined | 44 | 22648 | 590 | 0.00844749 | 0.000283333 | 0.272727 | 0.441558 | 0.0128333 | 0.015 | 0.861328 | 0.25 |
| full_queue | 1 | 60 | 60 | buy_bid | 22 | 11321 | 16 | 0 |  | 0 |  |  |  |  | 0 |
| full_queue | 1 | 60 | 60 | sell_ask | 22 | 11327 | 64 | 0.00260889 | -0.001 | 0.136364 | 0.571429 | 0.00675 | 0.015625 | 0.866667 | 0.227273 |
| full_queue | 1 | 60 | 60 | strategy_tail_combined | 44 | 22648 | 80 | 0.00157494 | -0.001 | 0.0681818 | 0.571429 | 0.00675 | 0.015625 | 0.866667 | 0.113636 |
| full_queue | 1 | 60 | 300 | buy_bid | 22 | 11321 | 16 | 0 |  | 0 |  |  |  |  | 0 |
| full_queue | 1 | 60 | 300 | sell_ask | 22 | 11327 | 64 | 0.00260889 | -0.006 | 0.0909091 | 0.375 | 0.00833333 | 0.015 | 0.925926 | 0.181818 |
| full_queue | 1 | 60 | 300 | strategy_tail_combined | 44 | 22648 | 80 | 0.00157494 | -0.006 | 0.0454545 | 0.375 | 0.00833333 | 0.015 | 0.925926 | 0.0909091 |
| full_queue | 1 | 60 | 900 | buy_bid | 22 | 11321 | 16 | 0 |  | 0 |  |  |  |  | 0 |
| full_queue | 1 | 60 | 900 | sell_ask | 22 | 11327 | 64 | 0.00260889 | -0.004 | 0.136364 | 0.333333 | 0.014875 | 0.0348571 | 0.533333 | 0.181818 |
| full_queue | 1 | 60 | 900 | strategy_tail_combined | 44 | 22648 | 80 | 0.00157494 | -0.004 | 0.0681818 | 0.333333 | 0.014875 | 0.0348571 | 0.533333 | 0.0909091 |
| full_queue | 1 | 300 | 60 | buy_bid | 22 | 11321 | 67 | 0.00164062 | -0.00522222 | 0.0454545 | 0.266667 | 0.00416667 | 0.0147917 | 0.277778 | 0.136364 |
| full_queue | 1 | 300 | 60 | sell_ask | 22 | 11327 | 285 | 0.0104278 | -0.00294231 | 0.181818 | 0.506366 | 0.00692857 | 0.02 | 0.446041 | 0.454545 |
| full_queue | 1 | 300 | 60 | strategy_tail_combined | 44 | 22648 | 352 | 0.00422976 | -0.00325 | 0.113636 | 0.403175 | 0.00686134 | 0.01875 | 0.369524 | 0.295455 |
| full_queue | 1 | 300 | 300 | buy_bid | 22 | 11321 | 67 | 0.00164062 | -0.0023 | 0.0909091 | 0.211111 | 0.0075 | 0.01395 | 0.87931 | 0.136364 |
| full_queue | 1 | 300 | 300 | sell_ask | 22 | 11327 | 285 | 0.0104278 | -0.00457231 | 0.0909091 | 0.398148 | 0.00877273 | 0.0165 | 0.584416 | 0.409091 |
| full_queue | 1 | 300 | 300 | strategy_tail_combined | 44 | 22648 | 352 | 0.00422976 | -0.00323 | 0.0909091 | 0.374444 | 0.00813636 | 0.0154167 | 0.633191 | 0.272727 |
| full_queue | 1 | 300 | 900 | buy_bid | 22 | 11321 | 67 | 0.00164062 | -0.0025 | 0.0909091 | 0.166667 | 0.0193125 | 0.0156 | 1.13863 | 0.0454545 |
| full_queue | 1 | 300 | 900 | sell_ask | 22 | 11327 | 285 | 0.0104278 | -0.00256 | 0.136364 | 0.406829 | 0.0158061 | 0.0182656 | 0.770375 | 0.318182 |
| full_queue | 1 | 300 | 900 | strategy_tail_combined | 44 | 22648 | 352 | 0.00422976 | -0.0025 | 0.113636 | 0.40625 | 0.0182917 | 0.0156 | 0.850576 | 0.181818 |

## 5. Event-level 预览

| event_slug | queue_model | fill_window_seconds | markout_seconds | quote_side | factor_quantile | token_metric_count | total_probes | total_fills | median_fill_rate | median_mean_markout | positive_markout_token_share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | buy_bid | 1 | 11 | 6148 | 36 | 0.00178891 | -0.00739375 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | buy_bid | 2 | 11 | 6140 | 33 | 0.00176367 | -0.00121429 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | buy_bid | 3 | 11 | 6140 | 32 | 0.00358423 | -0.00676389 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | buy_bid | 4 | 11 | 6140 | 9 | 0.00165563 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | buy_bid | 5 | 11 | 6144 | 10 | 0 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | sell_ask | 1 | 11 | 6148 | 35 | 0.00324149 | 0.00145833 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | sell_ask | 2 | 11 | 6140 | 43 | 0.00371058 | -0.00133333 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | sell_ask | 3 | 11 | 6140 | 57 | 0.00648298 | -0.00309091 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | sell_ask | 4 | 11 | 6140 | 52 | 0.00496689 | -0.009875 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 60 | sell_ask | 5 | 11 | 6144 | 62 | 0.00496689 | -0.00116667 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | buy_bid | 1 | 11 | 6148 | 36 | 0.00178891 | -0.00526071 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | buy_bid | 2 | 11 | 6140 | 33 | 0.00176367 | -0.0057 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | buy_bid | 3 | 11 | 6140 | 32 | 0.00358423 | -0.00926389 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | buy_bid | 4 | 11 | 6140 | 9 | 0.00165563 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | buy_bid | 5 | 11 | 6144 | 10 | 0 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | sell_ask | 1 | 11 | 6148 | 35 | 0.00324149 | -0.000142857 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | sell_ask | 2 | 11 | 6140 | 43 | 0.00371058 | -0.00335 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | sell_ask | 3 | 11 | 6140 | 57 | 0.00648298 | -0.00174641 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | sell_ask | 4 | 11 | 6140 | 52 | 0.00496689 | -0.00705609 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 300 | sell_ask | 5 | 11 | 6144 | 62 | 0.00496689 | -0.00641667 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | buy_bid | 1 | 11 | 6148 | 36 | 0.00178891 | -0.0127692 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | buy_bid | 2 | 11 | 6140 | 33 | 0.00176367 | -0.00235714 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | buy_bid | 3 | 11 | 6140 | 32 | 0.00358423 | -0.007 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | buy_bid | 4 | 11 | 6140 | 9 | 0.00165563 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | buy_bid | 5 | 11 | 6144 | 10 | 0 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | sell_ask | 1 | 11 | 6148 | 35 | 0.00324149 | 0.004125 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | sell_ask | 2 | 11 | 6140 | 43 | 0.00371058 | 0.00766667 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | sell_ask | 3 | 11 | 6140 | 57 | 0.00648298 | 0.00286364 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | sell_ask | 4 | 11 | 6140 | 52 | 0.00496689 | -0.00725801 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 60 | 900 | sell_ask | 5 | 11 | 6144 | 62 | 0.00496689 | -0.0007 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | buy_bid | 1 | 11 | 6148 | 149 | 0.0143113 | -0.000585598 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | buy_bid | 2 | 11 | 6140 | 148 | 0.0133333 | -0.00225 | 0.363636 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | buy_bid | 3 | 11 | 6140 | 88 | 0.00896057 | -0.00345526 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | buy_bid | 4 | 11 | 6140 | 48 | 0.00165563 | -0.004 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | buy_bid | 5 | 11 | 6144 | 53 | 0.00331126 | -0.00694444 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | sell_ask | 1 | 11 | 6148 | 149 | 0.0125224 | -0.00294231 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | sell_ask | 2 | 11 | 6140 | 210 | 0.0227273 | -0.00157549 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | sell_ask | 3 | 11 | 6140 | 238 | 0.0307942 | -0.00292568 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | sell_ask | 4 | 11 | 6140 | 207 | 0.0248344 | -0.00583198 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 60 | sell_ask | 5 | 11 | 6144 | 275 | 0.0149007 | -0.00240404 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | buy_bid | 1 | 11 | 6148 | 149 | 0.0143113 | -0.00168614 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | buy_bid | 2 | 11 | 6140 | 148 | 0.0133333 | -0.00491667 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | buy_bid | 3 | 11 | 6140 | 88 | 0.00896057 | -0.00722105 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | buy_bid | 4 | 11 | 6140 | 48 | 0.00165563 | -0.0110357 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | buy_bid | 5 | 11 | 6144 | 53 | 0.00331126 | -0.0025 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | sell_ask | 1 | 11 | 6148 | 149 | 0.0125224 | -0.00457231 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | sell_ask | 2 | 11 | 6140 | 210 | 0.0227273 | -0.0028401 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | sell_ask | 3 | 11 | 6140 | 238 | 0.0307942 | -0.00344595 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | sell_ask | 4 | 11 | 6140 | 207 | 0.0248344 | -0.00871532 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 300 | sell_ask | 5 | 11 | 6144 | 275 | 0.0149007 | -0.00165152 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | buy_bid | 1 | 11 | 6148 | 149 | 0.0143113 | -0.0036019 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | buy_bid | 2 | 11 | 6140 | 148 | 0.0133333 | -0.00114189 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | buy_bid | 3 | 11 | 6140 | 88 | 0.00896057 | -0.00274412 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | buy_bid | 4 | 11 | 6140 | 48 | 0.00165563 | -0.0045869 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | buy_bid | 5 | 11 | 6144 | 53 | 0.00331126 | -0.00770833 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | sell_ask | 1 | 11 | 6148 | 149 | 0.0125224 | -0.00206714 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | sell_ask | 2 | 11 | 6140 | 210 | 0.0227273 | -0.00101299 | 0.363636 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | sell_ask | 3 | 11 | 6140 | 238 | 0.0307942 | 0.00148986 | 0.454545 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | sell_ask | 4 | 11 | 6140 | 207 | 0.0248344 | -0.00170217 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | full_queue | 300 | 900 | sell_ask | 5 | 11 | 6144 | 275 | 0.0149007 | 0.000189394 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | buy_bid | 1 | 11 | 6148 | 55 | 0.00357782 | -0.003 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | buy_bid | 2 | 11 | 6140 | 50 | 0.00644122 | 9.09091e-05 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | buy_bid | 3 | 11 | 6140 | 43 | 0.00358423 | -0.00561389 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | buy_bid | 4 | 11 | 6140 | 18 | 0.00331126 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | buy_bid | 5 | 11 | 6144 | 24 | 0.00176367 | 0.00590909 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | sell_ask | 1 | 11 | 6148 | 57 | 0.00715564 | 0.0016875 | 0.363636 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | sell_ask | 2 | 11 | 6140 | 71 | 0.00811688 | 3.02198e-05 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | sell_ask | 3 | 11 | 6140 | 94 | 0.0113452 | 0.000254464 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | sell_ask | 4 | 11 | 6140 | 81 | 0.00662252 | -0.00215789 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 60 | sell_ask | 5 | 11 | 6144 | 106 | 0.00496689 | -0.0015625 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | buy_bid | 1 | 11 | 6148 | 55 | 0.00357782 | -0.00433333 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | buy_bid | 2 | 11 | 6140 | 50 | 0.00644122 | -0.0057 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | buy_bid | 3 | 11 | 6140 | 43 | 0.00358423 | -0.00633333 | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | buy_bid | 4 | 11 | 6140 | 18 | 0.00331126 |  | 0 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | buy_bid | 5 | 11 | 6144 | 24 | 0.00176367 | 0.00136364 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | sell_ask | 1 | 11 | 6148 | 57 | 0.00715564 | 0.00166667 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | sell_ask | 2 | 11 | 6140 | 71 | 0.00811688 | -0.00172527 | 0.181818 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | sell_ask | 3 | 11 | 6140 | 94 | 0.0113452 | -2.62605e-06 | 0.272727 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | sell_ask | 4 | 11 | 6140 | 81 | 0.00662252 | -0.00417647 | 0.0909091 |
| highest-temperature-in-shanghai-on-june-10-2026 | half_queue | 60 | 300 | sell_ask | 5 | 11 | 6144 | 106 | 0.00496689 | -0.00932258 | 0.0909091 |

## 6. Token probe 概览

| event_slug | event_title | event_start | event_end | market_index | market_label | condition_id | asset_id | token_side | probe_rows | trade_rows | median_spread | median_depth_imbalance_1 | source_time_inversion_count | source_delay_over_threshold_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 0 | 18°C or below | 0xc2e74183c15adfc561bdab466ce80efb6525b907ff36a1667452f14e7e607b73 | 82108548216968010072870699597305974161044963313995774427931182710031745067538 | YES | 283 | 1 | 0.002 | -0.96019 | 4660 | 7169 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 1 | 19°C | 0xe9270a0ad0e9215e2249b5573f8e1e40b39e76d19b45ded06aa4c0faeae9b9a8 | 74502948617169463266911772205376938956442075011044734712645525624423814463171 | YES | 420 | 3 | 0.007 | 0.254658 | 5720 | 8248 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 2 | 20°C | 0xe692a6f51125cf6914c090f8e01ecf1491030ed0ad775156b7dea9ed74099392 | 29363478216105628907851761749240280974313606434750483239415068662725185057677 | YES | 1697 | 6 | 0.003 | 0.215686 | 14386 | 31089 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 3 | 21°C | 0x4b71fc3b2d6d0a5a6d4a3c57a66f3b7c0a70049d1bd8fb47fdb985ca5e42a0e7 | 32958582433402594997678224016035388842620018324746531711463337433683768535571 | YES | 2527 | 7 | 0.004 | 0.561404 | 19359 | 47884 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 4 | 22°C | 0x5e9cdf0acfadf1d761629c3e824f608b4c8abda66747f716816cd21a6ef44b02 | 21885399021320574560651114684216824442350810668622956886293663194490254187234 | YES | 2709 | 92 | 0.016 | 0.0375713 | 31457 | 90049 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 5 | 23°C | 0xc6e48d3b9fb338a583851f46245eeb5d016d1c7db5325bc711b47592d44dea31 | 88280062208897936928474997251824304255619443393959943341817445415105690345219 | YES | 2905 | 477 | 0.02 | 0.141455 | 64988 | 172368 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 6 | 24°C | 0x09e1e10495f1dbb1948b63ad02a4af9f59a0401dde53ec713e5105d75eefe748 | 24582299336321328719263949980519370081553878495528020204195731783368989713817 | YES | 2922 | 392 | 0.01 | 0.000705384 | 95517 | 218452 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 7 | 25°C | 0x715ee73ffee65801adb9f51098cf61a14fb4262ae2b20eeb1d68528bdde59c41 | 93683348120137447419301462197228170067558545967622008841151707439369216600718 | YES | 3204 | 617 | 0.02 | 0.276293 | 89057 | 177530 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 8 | 26°C | 0x2488923e3cccb1742f4a230dfe8d79f6add52e0807b81809f1cd1e33028d03c1 | 84036100857155819931728194506958678774155362425129625655151905448772883585970 | YES | 3143 | 368 | 0.02 | 0.319201 | 79714 | 172674 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 9 | 27°C | 0x15a54d2d69dbe4ba0be84b4be5d634bcd81fb2659e4c211167ce7345c0f29ef7 | 8027430777184882285423173626503131257499585722975011528474150083679962657468 | YES | 3066 | 233 | 0.007 | 0.442308 | 33984 | 80865 |
| highest-temperature-in-shanghai-on-june-9-2026 | Highest temperature in Shanghai on June 9? | 2026-06-07T04:34:03Z | 2026-06-09T12:00:00Z | 10 | 28°C or higher | 0xbb205043a60774a3a047d9b19a1ff1b89f476d221df716552ffbce4dc9d47524 | 74894507304943217982308150117351772383465743335849048351488006458495739714670 | YES | 2996 | 165 | 0.003 | 0.328068 | 33044 | 81943 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 0 | 23°C or below | 0x209ea702bffd8aa70d4fca6c1287885fdb91716c30e0e8bd81567963400b0e50 | 12036673829484768976214971983373134806036380987353183612468179472146562556837 | YES | 2431 | 6 | 0.002 | 0.179104 | 19519 | 48035 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 1 | 24°C | 0xb3b7b4ad958cbe3e28c7d6ba4ebe7a7a879dac0223b694775f12050c38a6f384 | 68012748651234380826998744095104282306642841521116724279855731758194825122632 | YES | 1871 | 1 | 0.003 | 0.657381 | 16145 | 35069 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 2 | 25°C | 0x1637fef417faa8e8123e6932708157b1347d8325263f33ca81ba04629ba4ecb3 | 56788830322716258264964226087573396454736855011978630420131480404812475321234 | YES | 2696 | 10 | 0.003 | 0.578532 | 25323 | 56270 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 3 | 26°C | 0x23b087336e0307221d1d284112a21c185292a3ec68d31fa5676401b39c5e4e92 | 104200728392517260769227840787358244233838628411342747825408718627319711152999 | YES | 2792 | 143 | 0.02 | 0.111111 | 50226 | 100074 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 4 | 27°C | 0xdf893641b3da75d542e9816e3e50650ff9a57395826fd717e855bd5eec447ed5 | 70515615079914594072283762648675090779952114797105930464607795042963547323371 | YES | 2920 | 440 | 0.02 | 0.289476 | 91899 | 136266 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 5 | 28°C | 0x9fe8b4b759767560042e24c382fb7fc9be3da3235298edd0065ece2888659184 | 79468022192238370215968228824413698994937653291968216025900787429247470618594 | YES | 3107 | 430 | 0.02 | -0.0736903 | 95496 | 117045 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 6 | 29°C | 0xfd87fa8472dfb154071783a0aebe68d0b8277ce700a4265c707ec9639114d952 | 27278603221623619784217526778931912161201014588668933433614811866718916250456 | YES | 3083 | 255 | 0.02 | -0.0625488 | 102357 | 127024 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 7 | 30°C | 0x159328aa49ce5325050c0737bf19698a5a9b5c417d8a7d404e6da19d8ae9d5c2 | 42762311995392589756762413746744603645968981252144913788678454874717778235915 | YES | 2957 | 180 | 0.02 | 0.165387 | 93295 | 151495 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 8 | 31°C | 0xdb6d5221bcee90c9b194e42681793e2caff0509197eb53c0031fc9802f50474f | 83824793511464858660778918412947343764676476743062614862944889637630496459149 | YES | 3019 | 119 | 0.004 | 0.605035 | 67430 | 115444 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 9 | 32°C | 0x172043a1fcf0edff9fc73bd6ed63a50b2874be1f4323f7070b7c235d7fd2b636 | 107394249729422528580295423197418949033681781170606210461168594071997949686079 | YES | 3000 | 29 | 0.003 | 0.64385 | 33961 | 64649 |
| highest-temperature-in-shanghai-on-june-10-2026 | Highest temperature in Shanghai on June 10? | 2026-06-08T04:26:36Z | 2026-06-10T12:00:00Z | 10 | 33°C or higher | 0xf7c320623ae04b343a84ef1d29e41f1a0a9273cb5ede41198542839b04663b42 | 77126216875827511182932083508968979397438543022663466379727138106562657476235 | YES | 2836 | 16 | 0.002 | 0.352207 | 26451 | 58680 |

## 7. 图

![strategy_tail_markout_fill_60s.svg](strategy_tail_markout_fill_60s.svg)

![strategy_tail_markout_fill_300s.svg](strategy_tail_markout_fill_300s.svg)

![optimistic_300s_fill_rate_by_quantile.svg](optimistic_300s_fill_rate_by_quantile.svg)


## 8. 输出文件

- `event_token_inventory.csv`
- `token_probe_summary.csv`
- `maker_diagnostics.csv`
- `strategy_tail_summary.csv`
- `event_summary.csv`
- `fill_event_sample.csv`
- `run_summary.json`
