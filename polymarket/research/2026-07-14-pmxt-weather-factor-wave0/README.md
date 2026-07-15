# PMXT Weather Factor Wave 0 预注册

本文档是 G001 的 Wave 0 预注册记录，并包含 G002 因子定义补全 amendment。它在读取任何新的 441-event factor 结果之前固定研究范围、因子、标签、聚合、分层、候选门槛和失败隔离规则。本文档只定义研究协议，不报告结果，不提出 alpha、PnL、策略、成交质量、生产可用性或资金配置声明。

## G002 因子定义补全 amendment

- Amendment 时间：2026-07-14T16:01:45+08:00。
- Amendment definition version：`pmxt_weather_factor_wave0_factor_definitions.g002.v2`，对齐 committed implementation `b6e1daa29c`。
- Amendment 原因：在读取任何新的 441-event factor 结果之前，把 G001 已注册的因子名称补全为可执行、可复核的 factor definition，消除输入、replay、mutation、tick、BBO 与排名观测口径的歧义。
- Amendment 边界：只补定义，不改 factor registry、horizon、candidate gate、claim scope 或任何结果解释；不是事后调参，不读取、不引用、不回应 441-event 新结果。`b6e1daa29c` 对齐只记录实现语义，不报告性能结果、alpha、PnL 或候选结论。

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

If later reports show flow factors, they are quarantine diagnostics only and must be marked `ranking_eligible=false`, `research_only=true`, and `not_for_pnl=true`.

## Canonical input and replay validation

- Canonical input is fixed to `PolymarketL2DatasetV1`. The factor layer accepts only this typed dataset. Any temporary parsing layer must first convert to `PolymarketL2DatasetV1`, then the factor layer performs strict canonical update validation.
- Identifier mapping is fixed: `event_id = dataset.metadata.dataset_id`, `market = condition_id`, and `token_id = asset_id`.
- `steps` are consumed in the verified original order stored in `PolymarketL2DatasetV1.steps`; there is no sorting, fallback ordering, shard-position ordering, or reconstruction from arbitrary `dict` enumeration order.
- `replay_contract.py` verifies replay clock/order only: non-decreasing replay clock, strictly increasing `sequence`, and provable `timestamp_received` / `source_row_index` tie-breaks inside tied-clock groups. Field presence, field normalization, token/market boundaries, canonical update validity, book validity, and censor semantics are strict factor-layer validation responsibilities. Do not claim `replay_contract.py` verifies those checks.
- Runtime canonical update validation is strict: level entries must be `LevelV1`, and level price/size plus tick-size fields must be finite `Decimal` values. The factor layer does not coerce strings, floats, ints, or dict-shaped levels into canonical runtime values.
- Ranking anchors must come from typed steps that pass clock/order checks and factor-layer canonical validation. Failed steps, events, or tokens must be recorded as failure, skip, censor, or diagnostic; they cannot silently enter ranking.
- `source_quality.cohort_by_token` trusts only exact string cohorts `clean` and `degraded`. Missing, non-dict, non-string, or any other cohort value becomes `unknown`; `unknown` is never eligible for clean candidate gates.
- `source_quality.knownArchiveGaps` is either absent or a list of exact objects with only `market`, `asset_id`, `start`, `end`, and `provenance`. `market`, `asset_id`, and `provenance` must be nonempty strings; `start` and `end` must be timezone-aware timestamps; `end > start`. Malformed gap declarations fail closed before replay mutation. Each declared gap `start` is a token-scoped hard-break label barrier with declared provenance. Unknown archive gaps are not inferred.
- `MarketMetadataV1.resolution_time` is a label close barrier. If `token_id` is set, the close applies only to that token; if `token_id` is absent, the close is market-wide for the `condition_id`.

## Replay, O1, and BBO boundaries

- The canonical replay chain is `PMXTEventV1Adapter` + the `replay_contract.py` clock/order check + factor-layer canonical update validation.
- O1 is selected-token deterministic replay order: consume the verified original `PolymarketL2DatasetV1.steps` order directly, with no sorting or fallback. It is not true WebSocket, exchange, L3 queue, or cross-token event-wide execution order.
- O2 event-wide order is diagnostic/location context only; it cannot replace O1 or be treated as execution truth.
- O3 Nautilus callback evidence is boundary evidence only; Wave 0 offline factors do not treat O3 as ranking truth.
- BBO, mid, spread, depth, and all primary book-state factors must be derived from the reconstructed local L2 book. PMXT row `best_bid` / `best_ask` are audit-only for reconstructed BBO and cannot be factor inputs, label anchors, or ranking anchors.
- Invalid, crossed, locked, empty-side, hard-break, and event/token boundary states must be explicit censors or diagnostic flags; they cannot be silently filled. Book validity controls ranking/label availability only; it does not decide whether an actual mutation occurred.

## Factor definition semantics

### L2, tick, mutation, and ranking observation

- Reconstructed local L2 is the only factor source. For each token, replay `PolymarketL2DatasetV1.steps` in O1 order and maintain visible bid/ask price levels and sizes. Row-level `best_bid` / `best_ask` are used only to audit reconstructed BBO consistency; they do not feed factors, label anchors, or ranking.
- Tick defaults to `0.01`. Only an explicit, exact token-level `0.01 -> 0.001` tick-size change accepted by canonical validation can switch that token to `0.001` after the effective point. Do not infer tick from observed decimals, performance, or other tokens.
- `actual_mutation` compares the pre-step canonical visible L2 state with the final post-step canonical visible L2 state after applying all canonical L2 updates in that step atomically. A change that appears and is reverted within the same step is not a mutation.
- Any true visible L2 state change counts as `actual_mutation`, even if the final book is missing, locked, crossed, or otherwise invalid; validity only controls ranking and label availability. Trade-only, flow-only, tick-only, no-op, parse failure, and normalized-no-change steps are not L2 mutations.
- `book_staleness_seconds(t) = max(0, t - last_actual_mutation_time)`, where `last_actual_mutation_time` is the most recent actual mutation for the same token at or before `t`. Before the first actual mutation, staleness is invalid/diagnostic and cannot enter ranking.
- `book_update_intensity(t) = count(actual_mutation_time in (t-30s, t]) / 30`, in mutations/sec. The window is exactly the bounded left-open, right-closed rolling window `(t-30s, t]`. It counts same-token actual mutations only; invalid-but-mutated books still count for staleness/intensity, while trade-only, flow-only, tick-only, no-op, and parse failures do not.
- `ranking_observation = actual_mutation AND valid_book`. Invalid, crossed/locked/empty, flow-only, trade-only, tick-only, noop, hard-break, parse failure, and contract/canonical validation failure states do not participate in ranking; they may only enter failure/censor/diagnostic statistics.
- Implementation performance semantics are fixed but are not results: ordinary deltas, flow-only updates, tick-only updates, and no-ops use incremental top-5/cached book-factor state and must avoid full-book rescans; snapshot `book` updates may rebuild the local book/heaps. This is an implementation invariant only, not an alpha, latency, throughput, or production claim.

### 公式符号

对某 token 在 anchor `t` 的本地重建有效 L2：

- bid 侧按价格从高到低为 `(b_i, B_i)`，ask 侧按价格从低到高为 `(a_i, A_i)`，`i=1..5`。缺失档数量按 0 处理，但 best bid/ask 任一侧缺失则该 anchor invalid。
- `m = (b_1 + a_1) / 2`，`s = a_1 - b_1`。要求 `0 <= b_1 < a_1 <= 1`；否则 invalid。
- `D^b_K = sum_{i=1..K} B_i`，`D^a_K = sum_{i=1..K} A_i`，分母为 0 的公式 invalid，不做静默填补。
- 对 side `x in {bid, ask}`，令 `Q^x = sum_{i=1..5} q^x_i`，`p^x_i = q^x_i / Q^x`。若某侧 `Q^x = 0`，该 anchor invalid。

### 11 个 primary 因子公式

| 因子 | 公式 | 定义边界 |
| --- | --- | --- |
| `depth_imbalance_1` | `(D^b_1 - D^a_1) / (D^b_1 + D^a_1)` | 只看 best level depth。 |
| `depth_imbalance_3` | `(D^b_3 - D^a_3) / (D^b_3 + D^a_3)` | 只看前 3 档累计 depth。 |
| `depth_imbalance_5` | `(D^b_5 - D^a_5) / (D^b_5 + D^a_5)` | 只看前 5 档累计 depth。 |
| `microprice_minus_mid` | `(a_1 * B_1 + b_1 * A_1) / (B_1 + A_1) - m` | 只用 top level，本地 BBO 派生。 |
| `spread` | `s = a_1 - b_1` | probability price 单位；tick-normalized spread 可做诊断，不替代本因子。 |
| `top_level_depth` | `B_1 + A_1` | best bid 与 best ask 数量总和，不含更深档。 |
| `depth_slope` | Top 5 sizes padded with zero; `x=(0,.25,.5,.75,1)`; per-side `p_i=q_i/Q`; `g_x=2*sum(x_i*p_i)-1`; `factor=(g_bid+g_ask)/2`. | Two-side average top-5 depth shape; formula is fixed exactly as written. |
| `depth_concentration` | `(bid1_depth/sum_bid_top5 + ask1_depth/sum_ask_top5)/2` | Two-side average top-level share; formula is fixed exactly as written. |
| `bid_ask_liquidity_asymmetry` | `(c_bid-c_ask)/(c_bid+c_ask)`, where `c_side=top1_depth/sum_side_top5`. | Bid-vs-ask top-level concentration asymmetry; formula is fixed exactly as written. |
| `distance_to_zero_one` | `min(m, 1 - m)` | mid 离 0/1 payout 边界的距离。 |
| `tick_size_regime` | categorical `{0.01, 0.001}`；默认 `0.01`，仅显式 token 级切换后为 `0.001` | 只编码已验证 tick 制度，不从表现或小数位反推。 |

`book_staleness_seconds`、`book_update_intensity` 和 `valid_observation_counts` 仍是 diagnostic-only：可以用于分层、覆盖率、失败解释和稳健性诊断，不能单独晋级，也不能替代 elapsed wall-clock labels。

## 标签

主标签固定为 elapsed wall-clock：

- `mid_move_30s`
- `mid_move_120s`
- `mid_move_600s`
- `next_nonzero_mid_move`

Labels are generated after sealing the causal feature anchor. Fixed wall-clock labels choose the first actual mutation at or after the target timestamp; if duplicate actual mutations share that timestamp, the final state for that timestamp is used. Fixed horizon labels cannot cross the first applicable hard-break or close barrier between anchor and matched mutation. An invalid target censors the value. `next_nonzero_mid_move` may skip valid equal-mid actual mutations, but it cannot cross the first applicable hard-break or close barrier and the first invalid actual mutation is a censor barrier; missing future mids also censor.

Required audit columns are `label_censor_reason_<h>s` for each fixed horizon, `next_nonzero_mid_move_censor_reason`, and `hard_break_provenance`. Hard-break censor reasons carry declared `knownArchiveGaps.provenance`; this provenance records declared gaps only and does not imply unknown-gap inference.

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
`unknown` source-quality cohort 必须作为 failure/censor/coverage 诊断或单独 cohort 处理，不能并入 clean，也不能满足 clean gates。

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


## G003 materialization closeout

- Wave0 execution selected `M1_direct_replay` (`selected_mode: M1`). M1 is the oracle; this closeout does not report PnL, strategy results, execution quality, production readiness, or capital-allocation claims.
- M2 is disposable `safe_json`. M2 is only `eligible_not_selected` after exact factor/label/summary parity on synthetic fixtures and the frozen real-event parity set; M2 is not selected for Wave0 execution.
- M3 is primitive audit-only. M3 cannot be used as the execution mode, ranking input, label input, or candidate-gate input.
- The 3 frozen real parity events must be force-run once with `parity --force`: `highest-temperature-in-hong-kong-on-june-10-2026`, `highest-temperature-in-chicago-on-june-9-2026`, and `highest-temperature-in-amsterdam-on-june-6-2026`. After that forced run, hash-validated reuse (`reuse_hash_validated_committed_rows`) is allowed; mismatched hashes must fail closed rather than silently reusing rows.
- Dry-run manifest totals are fixed at 441 events / 4,851 markets / 9,702 tokens / 747,185,591 source rows / 6,008,295,014 source bytes, with 18 shards, worker cap 1, and 8GiB cache budget. Resource evidence cites the tracked Wave-1 benchmark representative peak RSS by path/hash/value; G003 does not enforce a runtime per-event memory cap, and source bytes are not treated as a memory cap.
- Failure isolation is event-level. Failed, skipped, censored, degraded, or partial events must be explicit and cannot silently pass candidate gates or be filled as zero.
- CLI closeout surface is `dry-run`, `validate-manifest`, `parity --force`, and hash-validated `parity` reuse.
- Git tracks compact artifacts only, including `outputs/compact_canonical_inventory.json`, `outputs/g003_dry_run_manifest.json`, `outputs/g003_materialization_parity.json`, and `outputs/g003_materialization_decision.json`. Cache, panel, partial, shard, trace, parquet, and pickle derivatives are not tracked.
- G003 did not change factors, labels, aggregation, candidate gate, claim scope, or the M1/M2/M3 materialization decision boundaries.

## 本目录文件与测试文件

- `README.md`：中文预注册说明。
- `protocol.json`：机器可读预注册协议。
- `experiment.yml`：可运行协议配置；不包含结果或候选名单，也不伪造任何运行结果。
- `factor_protocol.py`：预注册协议的本地解析/校验辅助文件；不改变本 README 中固定的因子名称、标签、聚合或候选门槛。

相关测试文件位于 `polymarket/tests/`：

- `polymarket/tests/test_pmxt_weather_factor_wave0_protocol.py`
