# PMXT Wave -1 内部研究收尾（同步至 ec8714066a；独立复核通过）

本文件是中文内部研究收尾，依据当前 G001-G004 证据链和 `463748282c` 之后重新生成的 G004 规范输出，以及强制 ai-slop-cleaner 后的 `ec8714066a` 当前事实同步。它只记录系统证据、边界、阻塞项和验证状态；不提出 alpha、PnL、策略有效性、当前面板确认性、生产使用或材料化模式选择结论。

- 当前实现 HEAD：`ec8714066a`（保留 `982453068c` / `32882f23ad` / `463748282c` 事实）。
- `ec8714066a` 删除 `benchmark_materialization.py` 中冗余 `except IntentionalInterruption: raise` 和 stale C901 suppression；行为不变。

## 1. 范围盘点

G001 固定的当前面板如下：

| 项目 | 数量 / 状态 |
| --- | ---: |
| 事件 | 441 |
| 城市 | 49 |
| 日期 | 9 |
| markets | 4,851 |
| tokens | 9,702 |
| rows_written_total | 747,185,591 |
| event-local missing-hour intersections | 278 |

边界：当前 9 个日期不是确认性 cohort；`performance_claims_allowed=false`；`not_for_pnl=true`；outcome、settlement、factor、strategy、fills、fees、positions、cash、PnL 字段均不得作为证据输入。

## 2. 排序与 Nautilus 证据

- **O1**：selected-token replay order，排序键为 `timestamp`, `timestamp_received`, `physical_original_row_ordinal`。physical ordinal 只是确定性并列排序兜底，不是实际消息到达顺序声明。
- **G007 physical ordinal 修复**：`982453068c` 将 parquet predicate pushdown 后的 physical ordinal 恢复改为位置化恢复，并覆盖持久化非 RangeIndex 输入。
- **O2**：event-wide diagnostic/materialization order，仅用于诊断/定位，不是执行真实顺序。
- **O3**：Nautilus native object / strategy callback observable order。G002 提供 compiled-engine callback 证据；G004 只外部引用，不重跑、不基准 O3。

## 3. 质量边界

- 当前面板状态：**非确认性**。确认性结论需要未来未见 sealed cohort。
- 当前面板只支持系统/覆盖证据，不支持 alpha、PnL、策略有效性、backtest performance、交易建议或材料化选择。
- manifest 缺失/损坏小时只有在与半开 `[event_start,event_end)` 区间相交时才是硬断裂。
- 自然陈旧与硬断裂分离；没有外部 missing/corrupt 证据时，更新间隔本身不构成硬断裂。
- 真实 ambiguity-heavy 全量扫描仍延后；完整 747M timestamp 分布与 label-match 分布仍延后。

## 4. G004 重新生成与 M1/M2/M3 基准

当前规范输出来自 `463748282c` 之后的 `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/`。`benchmark.stdout.log` 记录 `status=pass`、`events=3`、`materialization_decision=deferred`、`performance_claims_allowed=false`、`not_for_pnl=true`；该 stdout 不记录总运行秒数，所以本收尾不沿用旧 runtime。

模式定义：

- **M1 direct streaming**：一次 source-content byte hash pass 加一次权威 materialization pass；selected-token O1 sort buffer、rolling digests/checkpoints；不保留持久派生 primitive trace。
- **M2 sparse anchor-state materialization**：冷路径一次 hash pass 加一次 materialization pass；暖路径一次 hash pass 加 cache 查找/读取/校验；cache identity 使用实际 source byte SHA-256；不保留完整 primitive 列表。
- **M3 full primitive traces**：冷路径一次 hash pass 加一次 materialization pass；暖路径一次 hash pass 加 cache 查找/读取/校验；保留完整 selected-token primitive trace；不包含 orders、fills、strategy state、outcomes 或 PnL。

source pass 语义：冷路径 M1/M2/M3 均为 2 次 pass（一遍 content-hash byte pass + 一遍 materialization pass）；application-warm M1 仍为 2；cache-warm M2/M3 为 1。`source_rows_scanned` 只统计 materialization pass 解析行数，不包含 hash byte pass。content hash 成本已经包含在 measured elapsed/RSS/logical I/O 中。

单位说明：规范 benchmark 产物保留 raw bytes；下表所有容量换算均为 MiB = bytes/(1024**2)，不使用 MB 标签。RSS 是 Windows PSAPI 采样的进程 working set；I/O 是进程 logical transfer bytes；OS-cold 状态未知。

| 角色 / 事件 | 模式 | selected rows | source rows scanned | retained primitives | 冷路径源 pass | 暖路径源 pass | 冷秒 | 暖秒 | 冷峰值 RSS MiB | 暖峰值 RSS MiB | 冷逻辑读 MiB | 冷逻辑写 MiB | 暖逻辑读 MiB | 暖逻辑写 MiB | artifact MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 最大 / Hong Kong Jun10 | M1 | 74,654 | 5,323,117 | 0 | 2 | 2 | 5.186 | 5.074 | 375.664 | 393.629 | 79.802 | 0.000 | 79.802 | 0.000 | 0.000 |
| 最大 / Hong Kong Jun10 | M2 | 74,654 | 5,323,117 | 0 | 2 | 1 | 5.105 | 0.060 | 458.230 | 437.242 | 80.210 | 0.409 | 41.307 | 0.000 | 0.409 |
| 最大 / Hong Kong Jun10 | M3 | 74,654 | 5,323,117 | 74,654 | 2 | 1 | 6.824 | 0.827 | 528.973 | 548.008 | 125.728 | 45.927 | 132.343 | 0.000 | 45.927 |
| 中位 / Chicago Jun9 | M1 | 28,386 | 1,495,960 | 0 | 2 | 2 | 1.772 | 1.805 | 462.711 | 485.125 | 23.578 | 0.000 | 23.578 | 0.000 | 0.000 |
| 中位 / Chicago Jun9 | M2 | 28,386 | 1,495,960 | 0 | 2 | 1 | 1.849 | 0.031 | 481.992 | 474.766 | 23.706 | 0.130 | 12.177 | 0.000 | 0.130 |
| 中位 / Chicago Jun9 | M3 | 28,386 | 1,495,960 | 28,386 | 2 | 1 | 2.474 | 0.158 | 481.723 | 491.488 | 40.871 | 17.295 | 46.508 | 0.000 | 17.295 |
| 降级样本 / Amsterdam Jun6 | M1 | 912 | 96,822 | 0 | 2 | 2 | 0.089 | 0.081 | 475.758 | 474.965 | 2.056 | 0.000 | 2.056 | 0.000 | 0.000 |
| 降级样本 / Amsterdam Jun6 | M2 | 912 | 96,822 | 0 | 2 | 1 | 0.090 | 0.025 | 473.168 | 472.160 | 2.066 | 0.011 | 1.040 | 0.000 | 0.011 |
| 降级样本 / Amsterdam Jun6 | M3 | 912 | 96,822 | 912 | 2 | 1 | 0.114 | 0.013 | 472.250 | 474.055 | 2.615 | 0.560 | 2.139 | 0.000 | 0.560 |

三个代表事件均为 independent parity pass。

## 5. Cache identity 与 resume 行为

- `32882f23ad` 将 M2/M3 cache identity 绑定到实际 source byte SHA-256。
- same-size rewrite 即使恢复 mtime 也会使 cache 失效；相关回归已覆盖。
- `463748282c` 将 source-content hash pass 的成本纳入 elapsed/RSS/logical I/O 测量；cache-warm M2/M3 仍需 1 次 hash byte pass。
- bitflip、truncate、manifest corrupt、manifest missing 均被检测；只允许复用 committed matching payload；partial reuse 被阻止；失败隔离到 per-event-token cache identity。
- 没有跟踪 disposable cache directory。

## 6. Jun4 与歧义状态

Jun4 仍是可行性投影，不是完整日期 benchmark：

| 项目 | 当前值 |
| --- | ---: |
| expected_task_count | 49 |
| manifest_available_event_count | 49 |
| manifest_feasibility_only_count | 49 |
| full_49_event_m3_attempted | false |
| manifest_projected_rows_total | 47,168,018 |
| manifest_projected_source_file_bytes_upper_bound_total | 379,865,225 bytes / 362.268 MiB |
| manifest_projected_m3_artifact_bytes | 453,476,010 bytes / 432.468 MiB |
| representative_m3_artifact_to_source_file_upper_bound_ratio | 1.193781320984 |

原因：49 个冻结 Jun4 task ID 全部只是 manifest-feasibility-only；没有声称完整日期 M1/M2/M3 执行或完成。

真实 ambiguity-heavy 扫描仍延后。G003 真实 16-token O1 ambiguity 扫描超过 180 秒上限后明确延后。G002 bounded real smoke 只证明 ordering plumbing，不能替代完整 ambiguity-heavy 扫描。

## 7. Gate 状态

- `materialization_decision=deferred`，原因是歧义角色仍为外部/资源延后且 49 个 Jun4 任务仍为 manifest-feasibility-only。
- 材料化选择 gate：关闭。
- Factor screening gate：关闭，直到获得显式 protocol approval；不声称 factor promotion。
- Performance/alpha/PnL/strategy gate：关闭，因为 `performance_claims_allowed=false` 且 `not_for_pnl=true`。
- Confirmation gate：关闭，因为当前面板非确认性。
- Production-use gate：关闭；本收尾只用于研究证据。

## 8. Ready / Not ready

Ready：

- 441-event universe inventory、contract/schema identity、row/event-type totals 与半开硬断裂规则。
- O1 排序契约、O2 诊断边界、G002 compiled-engine O3 callback 证据。
- Outcome-blind 质量/protocol 产物与不变性证据。
- 3 个冻结代表事件上的 M1/M2/M3 系统基准、cache/resume 检查和 independent parity pass。
- Jun4 manifest 可行性状态；仅限容量投影。

Not ready：

- 材料化模式选择或默认模式 rollout。
- Factor screening、alpha/PnL/strategy 比较、策略上线或资金分配。
- 当前面板确认性结论。
- 完整 49-event Jun4 benchmark、真实 ambiguity-heavy 扫描、完整 747M timestamp/label-match 分布。
- 生产或 PnL 用途。

## 9. 提交

- G001 `1a80c6131381b50e023872fe83d80b2e6cd5a50a` — `research(polymarket): inventory 441-event PMXT panel`
- G002 `6a26ddf3e6bc65dd3a803f2979d3cde2cc2e82cc` — `test(polymarket): verify PMXT ordering through Nautilus callbacks`
- G003 `34dbe150662c601f3389585d9b613157a4a6910a` — `research(polymarket): calibrate outcome-blind Wave -1 rules`
- G004 `1f5a9825bbca5da309ff863547557d0afd89b7a6` — `research(polymarket): benchmark Wave -1 materialization modes`
- G005 prior cleaner guarded import `40555d5248` — `test(polymarket): guard compiled runtime parity imports`
- G006 EOF blocker resolution `0fbd3d5f82` — `chore(polymarket): normalize Wave -1 file endings`
- G007 physical ordinal recovery `982453068c` — `fix(polymarket): recover physical PMXT row ordinals`
- G007 cache source bytes `32882f23ad` — `fix(polymarket): key materialization cache by source bytes`
- G007 source pass measurement `463748282c` — `fix(polymarket): measure cache identity source pass`
- G007 cleanup `ec8714066a` — `refactor(polymarket): simplify materialization measurement`（删除 `benchmark_materialization.py` 中冗余 `except IntentionalInterruption: raise` 和 stale C901 suppression；行为不变）

## 10. 验证与复核状态

- 当前实现 HEAD：`ec8714066a`（保留 `982453068c` / `32882f23ad` / `463748282c` 事实）。
- 强制 ai-slop-cleaner：PASS；pre/post targeted 均为 52 passed, 3 skipped；fallback review 无 masking fallback，grounded fallbacks 保留；ruff、py_compile、diff check 通过。
- post-cleaner 全量：`python -m pytest -q -p no:cacheprovider -o addopts='' polymarket/tests` — 125 passed, 6 skipped in 28.15s。
- clean temp + installed compiled Nautilus：导入路径 `C:/Users/xhth/miniconda3/Lib/site-packages/nautilus_trader/__init__.py`；两个 test files 共 22 passed in 2.56s。
- changed-Python ruff 与 py_compile：通过。
- full-range/current diff-check：通过。
- closeout canonical/MiB/static-hash/Han/gate audit：通过。
- 架构范围：无 generic core changes；无 tracked disposable cache。
- `quality_gate.json` 已创建；`quality_gate_json_created=true`。
- `independent_review.status=passed`；approval=APPROVE；architect=CLEAR。
- code-reviewer agent `019f5d45-0b3b-7190-97d7-e4a98cf98e58`：38 files；CRITICAL/HIGH/MEDIUM/LOW 全 0；prior blockers 全 PASS；recommendation=APPROVE。
- architect agent `019f5d45-0f29-7cd1-99a5-4919bec25949`：Architectural Status=CLEAR；I1-I10 全 PASS；deferred ambiguity scan、Jun4 full benchmark、完整 747M 分布均为未来范围且 gate 关闭，不阻塞本 Wave -1 closeout。
- `.omx/ultragoal/goals.json` 与 `.omx/ultragoal/ledger.jsonl` 是动态文件，不固定 SHA-256。

## 11. Source artifact hashes

| Goal | Artifact | sha256 / policy |
| --- | --- | --- |
| G001 | `polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/inventory_summary.json` | `527ce72b8a1920e1cfc65eec05df3b484782cdae1564eea1daae42b503367efe` |
| G001 | `polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/protocol-v1.json` | `37a5b8cc2e5e7a73bf1d08c45b2591ce5d7247f4df2958a9974353c9b00f0bf9` |
| G002 | `polymarket/research/2026-07-13-pmxt-wave-minus1-ordering-parity/outputs/summary.json` | `6faa50f9cb06884f71991ba2225c29f6ba7cc855aa8ae67e131cd7c227c5edc9` |
| G002 | `polymarket/research/2026-07-13-pmxt-wave-minus1-ordering-parity/outputs/real_curated_smoke_parity.json` | `f771b45b99f1afb7f650676026bf3207f037d886207c3c0c15cb80251dfcdc6a` |
| G003 | `polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/quality_calibration.json` | `4828303e0c678c2eb3f42ea6e8ae2c4d0bd4fc2068be0772b762f5a36ff47ec3` |
| G003 | `polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/representative_sample.json` | `6804354043452ecd103f04b9fc4dad46d121d18120e71e8d22bf2a60de966219` |
| G003 | `polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs/outcome_blind_invariance.json` | `bb7909cacdcfad4fa86140c83114a66c6e2e6fd29e9d2c1376a325198e99bbf3` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/benchmark_manifest.json` | `9ec128d64005cc30ebd92649f7abcd825260296d39a7918dd6ee3f8c71edc3bb` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/benchmark_report.json` | `7a0e84bfe978176a402bd5c70c6e44f257cc39a52a9c780d23f20cc994938fd0` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/m1_m2_m3_results.json` | `4a55e64808379d8619a66d0d40b5e7358be01e1707ee945bacbea1569a755c51` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/materialization_decision.json` | `2bf9c115f469971121d90029171c2866ef8acbfa920be00f69403d41ac450e3c` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/complete_date_feasibility.json` | `e01f215acc02ce087b1231c3f226b10c70f43f104bf8dc25deae5d96ffb8d617` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/parity_oracles.json` | `c9a08b6870627702736ef042efcfaefb23c64c9dd7bfd664804deb05a0b65b13` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/cache_resume_tests.json` | `7bfd5f246fa88653649f00622731f3e34c2cae30ca76f6769f66410c1f8bce6f` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/outcome_blind_invariance_g004.json` | `e9d0c579c1de84605993a04dde05656902e35f7a7aa8ec93250f07244db17be3` |
| G004 | `polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/outputs/representative_sample_g004.json` | `5c64f77dbaaf3cc1d15318e8b79678d424baa4066f80cd19e5a6fc96df413b9b` |
| PLAN | `.omx/ultragoal/brief.md` | `cb33524aa993eabe606877d669fe7157d04c60a113e7043a7cecaf4f506d0633` |
| PLAN | `.omx/ultragoal/goals.json` | 动态 / 不固定 SHA-256 |
| PLAN | `.omx/ultragoal/ledger.jsonl` | 动态 / 不固定 SHA-256 |

## 12. 收尾结论

Wave -1 当前具备可复现的内部系统证据链：inventory、ordering plumbing、outcome-blind 质量边界、代表性材料化基准和 Jun4 可行性投影。它不支持 alpha、PnL、strategy、当前面板确认、生产使用或材料化模式选择。若要打开 factor screening 或材料化选择，必须先取得显式 protocol approval，并解决 ambiguity/full-date 延后项。
