# PMXT Weather Factor Wave 0 预注册

本文档是 G001 的 Wave 0 预注册记录。它在读取任何新的 441-event factor 结果之前固定研究范围、因子、标签、聚合、分层、候选门槛和失败隔离规则。本文档只定义研究协议，不报告结果，不提出 alpha、PnL、策略、成交质量、生产可用性或资金配置声明。

## 依据与范围

- 计划依据：`.omx/ultragoal/brief.md`。
- Wave -1 依据：`polymarket/research/2026-07-14-pmxt-wave-minus1-closeout/`。
- 当前面板：441 events、49 cities、9 dates、4,851 markets、9,702 tokens、747,185,591 source rows。
- 当前面板不是 confirmatory cohort；所有结论只能是探索性和内部复制/稳健性描述。
- 当前目录的文件清单为 `README.md`、`protocol.json`、`experiment.yml`、`factor_protocol.py`。大 panel、缓存、逐行派生特征和中间 parquet 不跟踪。

## 研究问题

Wave 0 只回答一个问题：在固定 441-event 天气 PMXT 面板上，纯订单簿状态因子是否出现值得后续确认的探索性一致性信号。即使候选通过门槛，也只能进入后续确认注册；不能称为可交易 alpha 或策略。

没有候选通过门槛也是有效研究结果。

## 允许与隔离的因子

允许进入 Wave 0 排名的因子必须是 prefix-causal、outcome-blind、纯 book-state：

- `depth_imbalance_1`
- `depth_imbalance_3`
- `depth_imbalance_5`
- `microprice_minus_mid`
- `spread`
- `top_level_depth`
- `depth_slope`
- `depth_concentration`
- `bid_ask_liquidity_asymmetry`
- `distance_to_zero_one`
- `tick_size_regime`
- `book_staleness_seconds`：仅用于诊断或分层，不单独晋级
- `book_update_intensity`：仅用于诊断或分层，不单独晋级

以下 flow 或 message-order-sensitive 因子必须隔离，不能参与 Wave 0 排名或候选门槛：

- OFI / order-flow imbalance
- trade pressure
- cancel pressure
- 依赖真实消息到达顺序、L3 queue、成交归因或撤单归因的任意因子

若后续报告展示这些 flow 因子，只能作为 quarantine diagnostics，且必须单独标记为 `ranking_eligible=false`。

## Replay、O1 与 BBO 边界

- 规范 replay 链为 `PMXTEventV1Adapter` + `replay_contract.py`。
- O1 是 selected-token deterministic replay order，排序键为 `timestamp`, `timestamp_received`, `physical_original_row_ordinal`。它不是真实 WebSocket、交易所、L3 queue 或跨 token event-wide 执行顺序。
- O2 event-wide order 只能用于诊断和定位，不能作为执行真相或 O1 替代。
- O3 Nautilus callback evidence 只可作为边界证据；Wave 0 离线因子不把 O3 当成排名真相。
- BBO、mid、spread、depth 因子必须从本地重建 L2 book 派生；PMXT row 中的 `best_bid` / `best_ask` 只能 audit，不得作为因子输入或标签锚点。
- invalid book、crossed book、empty side、硬断裂、event/token 边界必须显式 censor 或标记；不得静默填补。

## 标签

主标签固定为 elapsed wall-clock：

- `mid_move_30s`
- `mid_move_120s`
- `mid_move_600s`
- `next_nonzero_mid_move`

标签从封存后的 causal feature anchor 向后生成，遇到硬断裂、event close、token close、invalid future book 或无可用 future mid 时 censor。重复 timestamp 必须按 O1 顺序和固定匹配规则处理。

`valid_observation` horizon 只允许作为诊断字段，用于解释活跃度和 label availability，不能替代 elapsed time，不能单独触发候选晋级。

## 分层

主要分层固定如下：

- clean / primary development replication：2026-06-04、2026-06-05、2026-06-08、2026-06-09、2026-06-10，共 245 events。
- degraded robustness：2026-06-06、2026-06-07、2026-06-11、2026-06-12，共 196 events。
- lifecycle / time-to-event-end。
- activity / book update intensity。
- spread regime。
- tick-size regime。
- source-quality cohort 与 hard-break provenance。

clean 与 degraded 必须分开报告。degraded 可以降低、隔离或拒绝候选，但不能升级 claim scope。

## 聚合与统计口径

禁止 row-count domination。聚合顺序固定：

1. row / anchor 级计算因子与标签；
2. token 级汇总；
3. 同一 event 内 token 等权；
4. global headline 中 event 等权。

报告可以展示 row、anchor、token、market、event 数量作为覆盖率，但 headline metric 的有效口径必须是 event-equal，并明确当前面板非 confirmatory。

预注册 metric：

- event IC / Spearman；
- positive-event share；
- nonzero-move hit rate；
- conditional average win/loss；
- quantile monotonicity；
- block-bootstrap interval；
- horizon consistency；
- city/date stability；
- spread conditioning；
- clean/degraded sensitivity；
- concentration diagnostics。

## 候选门槛

候选进入 shortlist 必须同时满足：

1. 至少两个主 elapsed horizons 上方向一致；
2. clean positive-event share >= 0.60；
3. 至少一个主 elapsed horizon 的 block-bootstrap interval 排除 0；
4. 结果不能只依赖 widest-spread stratum；
5. clean 与 degraded 不得出现方向反转；
6. flow/quarantine 因子不得进入 shortlist；
7. coverage、censor、failure、hash 和版本信息完整。

通过上述门槛只表示“可后续注册确认的探索性候选”，不是 alpha、PnL、策略或生产声明。

## 确定性、哈希与失败隔离

- 所有输入、协议、代码版本、source byte hash、replay contract、factor schema、label schema、聚合 schema 和输出 summary 都必须记录 SHA-256 或等价内容地址。
- 重跑必须在允许的 run identity / timestamp 字段之外给出相同的 canonical summaries 和 hashes。
- 每个 event 是失败隔离单元；失败、跳过、censor、degraded 和 partial 状态必须显式记录，不能默认为 0 或 silent pass。
- M1 direct replay 是 oracle。M2 只能在 synthetic 与代表性 real event 上证明 factor/label/summary exact parity 后作为加速。M3 只用于 audit。
- 缓存和大 panel 派生产物是 disposable derivatives，不进入版本控制；可由 source hash、protocol hash 和 replay contract 重新生成。

## 本目录文件与测试文件

- `README.md`：中文预注册说明。
- `protocol.json`：机器可读预注册协议。
- `experiment.yml`：可运行协议配置；不包含结果或候选名单，也不伪造任何运行结果。
- `factor_protocol.py`：预注册协议的本地解析/校验辅助文件；不改变本 README 中固定的因子名称、标签、聚合或候选门槛。

相关测试文件位于 `polymarket/tests/`：

- `polymarket/tests/test_pmxt_weather_factor_wave0_protocol.py`
