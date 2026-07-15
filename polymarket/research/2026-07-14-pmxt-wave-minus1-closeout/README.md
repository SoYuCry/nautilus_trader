# PMXT Wave -1 内部研究收尾

本目录保存 Wave -1 的中文内部研究收尾。内容已同步到 `ec8714066a` 当前实现事实，并保留 `463748282c` 之后的 G004 规范输出，只记录系统证据、边界、阻塞项、验证与复核状态；不提出 alpha、PnL、策略、确认性、材料化选择或生产使用结论。

## 文件

- `report.md`：面向研究者阅读的中文收尾，包含 inventory、排序边界、质量边界、当前 M1/M2/M3 MiB 表、Jun4 可行性投影、gate、Ready/Not ready、验证状态和 source hash。
- `closeout.json`：机器可读摘要；键名、enum、路径、hash 保持英文稳定，人类叙述字段使用中文；二进制派生容量字段使用 `*_mib`。
- `README.md`：本导航文件。

## 当前决定

- `materialization_decision=deferred`
- `performance_claims_allowed=false`
- `not_for_pnl=true`
- 当前面板为 `not_confirmatory`
- factor gate 在显式 protocol approval 前保持关闭
- 材料化选择仍延后；Jun4 仅为 manifest feasibility / 容量投影
- `independent_review.status=passed`；approval=APPROVE；architect=CLEAR
- `quality_gate.json` 已创建；`quality_gate_json_created=true`

## 同步要点

- `982453068c`：physical ordinal 恢复改为位置化，并覆盖持久化非 RangeIndex parquet 输入。
- `32882f23ad`：M2/M3 cache identity 使用实际 source byte SHA-256；same-size/restored-mtime rewrite 会使 cache 失效。
- `463748282c`：source-content hash pass 成本已计入 measured elapsed/RSS/logical I/O；冷路径 M1/M2/M3 为 2 pass，application-warm M1 为 2 pass，cache-warm M2/M3 为 1 pass。
- `ec8714066a`：`benchmark_materialization.py` 删除冗余 `except IntentionalInterruption: raise` 和 stale C901 suppression；行为不变。
- 规范 artifact 保留 raw bytes；文档中的二进制换算统一标为 MiB=bytes/(1024**2)，不使用 MB。
- RSS 是 Windows PSAPI 采样进程 working set；I/O 是 logical transfer；OS-cold 未知。

## 最新验证

- 强制 ai-slop-cleaner：PASS；pre/post targeted 均为 52 passed, 3 skipped；fallback review 无 masking fallback，grounded fallbacks 保留；ruff、py_compile、diff check 通过。
- Post-cleaner full verification：`python -m pytest -q -p no:cacheprovider -o addopts='' polymarket/tests` — 125 passed, 6 skipped in 28.15s。
- Clean temp + installed compiled Nautilus：导入路径 `C:/Users/xhth/miniconda3/Lib/site-packages/nautilus_trader/__init__.py`；两个 test files 共 22 passed in 2.56s。
- changed-Python ruff、py_compile、full-range/current diff-check、closeout canonical/MiB/static-hash/Han/gate audit：通过。
- 无 generic core changes；无 tracked disposable cache。
- independent review 已通过：code-reviewer agent `019f5d45-0b3b-7190-97d7-e4a98cf98e58` 复核 38 files，CRITICAL/HIGH/MEDIUM/LOW 全 0，prior blockers 全 PASS，recommendation=APPROVE。
- architecture gate 已通过：architect agent `019f5d45-0f29-7cd1-99a5-4919bec25949` 给出 Architectural Status=CLEAR，I1-I10 全 PASS；deferred ambiguity scan、Jun4 full benchmark、完整 747M 分布是未来范围且 gate 关闭，不阻塞本 Wave -1 closeout。
- `quality_gate_json_created=true`。
