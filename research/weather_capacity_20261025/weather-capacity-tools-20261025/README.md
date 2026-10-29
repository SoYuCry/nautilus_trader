# 天气市场容量/利润离线工具包

代码包，不含行情、AWC、业务配置、凭据、实盘入口或虚拟环境。
复用已在上海 2026-10-16～23 验证的原生转换器、seller 原样 BBO 裁剪和容量统计。
支持任意数量 Event，默认 32 worker，每 worker Arrow CPU/IO/BLAS 1 线程。
不会连接账户、Redis 或下单。不会下载天气或行情。

## 1. 环境

原验证环境：Linux x86_64，CPython 3.14，Nautilus 2.0.0rc6，16-byte precision，
PyArrow 25.0.1、NumPy 2.5.3。**需要公司同一版本的 Nautilus v2 wheel/环境**，
不包含原生二进制，不要随意安装公共 PyPI 的其他版本或旧 v1。

```bash
tar -xzf weather-capacity-tools-20261025.tar.gz
cd weather-capacity-tools-20261025
mkdir -p tmp python
export TMPDIR="$PWD/tmp"
uv venv python/.venv --python 3.14
uv pip install --python python/.venv/bin/python /ABS/nautilus_trader-2.0.0rc6-MATCHING.whl
uv pip install --python python/.venv/bin/python -r requirements.txt
bash run.sh doctor.py
bash run.sh -m unittest discover -s . -p 'test_*.py' -v
```

也可复用另一台机器已有的匹配环境：
`export CAPACITY_PYTHON=/ABS/existing/python/.venv/bin/python`，然后 `bash run.sh doctor.py`。
本包没有系统 /tmp 写入；所有新产物放解压目录的 `tmp/`。

## 2. 两个输入入口

### 已有本套裁剪后的 Catalog

直接跳到第 4 步。需要同版本 native data/、本套 manifest.json（含 captured_window、
analysis_window、audit.max_adjustment_ns）。不能把其他旧 Catalog 不加判断地冒充该合同。
已剪裁的 Catalog 不要再剪裁。

### 原始 collector WS JSONL

支持逐行 envelope：capture_seq、timestamp_received（带时区，保留纳秒）、可选
received_monotonic_ns、frame（对象或数组）；book / price_change / last_trade_price /
tick_size_change / market_resolved。price_changes 中需保留源 best_bid/best_ask。
当前原始输入 reader **不是 PMXT/Pendulumflow Parquet reader**；若另一台机器格式不同，
先做薄输入映射，不需要更改 BBO 裁剪或利润算法，不伪造 WS 原帧。

## 3. 编译 Event（可选）

复制 `examples/compile-template.json`，填实际路径、完整 condition / YES / NO 映射、
记录中的 scheduled_expiration 和 metadata 证据。source_paths 列出所消费的全部原文件，
不得省略 metadata、HTTP body 和 request log。文件须停止传输/写入后冻结。
每个 Event 单独模板。例子只是占位，不可当真实数据运行。

```bash
mkdir -p tmp/contracts tmp/catalogs
bash run.sh freeze_inputs.py /ABS/event-template.json \
  --output "$PWD/tmp/contracts/EVENT.json" --completion-note '传输完成；本批文件已冻结'
bash run.sh compile_temporary.py --input-contract "$PWD/tmp/contracts/EVENT.json" \
  --output "$PWD/tmp/catalogs/EVENT"
bash run.sh validate_samples.py "$PWD/tmp/catalogs/EVENT"
```

完整保存采集窗口，analysis_window 只记录统计窗口，不按午夜裁掉母数据。
未知结算不写 Close，未知 tick/fee 保留 null/unknown，单边盘口合法。
HTTP 结算可用 `http_resolutions` 条目：condition_id、winning_asset_id、actual_close_time、
evidence={path,request_log_path,received_at}；仅在有真实 closed/resolved/terminal payout
证据并验证 YES/NO 映射时生成。Close 的 source time 是 actual_close_time，availability
是第一次收到该证据的时间，不允许提前。参考转换见 reference_shanghai/prepare_frozen.py。

## 4. 准备统计输入并行运行

参照 `examples/capacity-template.json`，每 Event 写一个 JSON 到 `tmp/capacity-inputs/`，
文件名必须为 `highest-temperature-in-....json`。填实际 station、当地 timezone、date、
no_token_id、桶 upper_bound（最高无上界桶 null）、fee_rate、minimum_quantity、limits。
fee_rate 是 `rate*p*(1-p)` 的 rate；未知为 null，明确 0 与未知不能混淆。
站点必须来自该 Event 规则，不能照抄上海 ZSPD。

`weather[]` 来自当时保存的 AWC 轮询：kind=metar_poll/metar_error、received_ns 为
**本机实际收到时间**、rows 为完整 AWC rows，保留原路径/行号。不用现在拉取的数据回填。
同接收时刻 poll→error 要同时保留，错误会原子撤销信号；不得把上游 receiptTime 当成本机
可见时间。消费者按 Catalog analysis_window 截断，23 日不延伸到盘口截止后。

metar_rule 必须明确：decoded-temperature 或 awc-routine-rounded-celsius-v1。
后者按 routine METAR 原文、T remark 和已实现的摄氏取整规则；不能静默套到所有城市。
**本工具 upper_bound 以摄氏度解释；华氏市场必须先按该市场的取整/结算规则人工确认
阈值转换。temperature_unit=F 会明确拒绝，避免全球混用单位算错。**

```bash
bash run.sh capacity_run.py --inputs "$PWD/tmp/capacity-inputs" \
  --output "$PWD/tmp/capacity-results" --workers 32
```

不需要恰好八个 Event，不限制上海 slug。最多 32 个独立 Event 同时处理，少于 32 个
则按实际数量启动。每个 Event 独立写结果，一个协调进程写 status.json/summary.json。
输出目录必须是新的；失败列在 summary.failures，不自动覆盖历史结果。

## 5. 结果怎么解释

- peak_simultaneous_capacity：同一时刻各合格桶全部 NO 卖盘的最大投入及对应条件利润。
- mean_capacity_during_valid_weather：实际盘口覆盖与有效天气交集内的时间加权均值。
- first_opportunity_sweep_per_condition：每 condition 第一次可扫深度，只计算一次。
- bounded_first_taker_estimate：按当前每 condition/order 5 份、每 Event 100 pUSD
  （可在 limits 配置）的首次最佳卖价立即成交情景；不是完整订单撮合回测。
- settlement_profit 仅当已有实际 payout 证据才有值；否则 null。
- 无天气是 coverage gap，不是零容量。各时间快照不能累加，镜像 YES bids 不重复统计。
- 费用沿输入保存配置/注明情景；返佣、maker、竞争、延迟、失败订单与已有持仓不模拟。

全球数据与本机上海日志布局可能不同。**reference_shanghai/ 下的提取脚本保留了原研究
固定路径和日期，仅供其他机器的 coding agent 参考输入映射，不能直接当全球入口运行。**
最快接续方式：让另一台机器的 agent 读本说明、examples 和 reference_shanghai/INPUTS.md，
把其已存在的全市场数据映射成上述合同，先一 Event 再 --workers 32；不必重新设计框架。

## 校验与来源

BUILD.json 记录代码来源提交及每个输入源码的 SHA；SHA256SUMS.json 记录包内文件。
原样 bbo_prune.py 的 RULE=carried_bbo_delete_only/v1：删除矛盾旧档位，不补数量，
不把全深度缩为 top-of-book，缺失 BBO 遵循原算法。
打包时会运行 portable doctor 和包含原生往返的合成测试；跨城市真实数据未在本机验证。
