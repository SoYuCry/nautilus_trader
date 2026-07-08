
# 20260706 - PolyMarket 回测架构与边界_v1

0. TLDR

1. 已和张琦对齐数据需求，第一版 event bundle 预计本周出。
2. 已用自抓 Polymarket 行情跑通 adapter，把 raw / capture 数据统一成 Nautilus 可回测的数据结构。
3. 已把 Nautilus 回测侧适配到 Polymarket 特有的 tick size change、fee、settlement。
4. report 已补到可复盘 / 可验收 / 可定位问题的程度。

主链路是：

Polymarket raw/captured data
  -> PolymarketL2DatasetV1
  -> Data Health Gate
  -> Nautilus native data
  -> BacktestEngine
  -> fills / positions / account / summary reports

关键点：成交、持仓、现金、PnL 交给 Nautilus BacktestEngine，我们不自己写撮合。

1. 与张琦对需求
数据按 Event 划分，存 “BTC 15min” 与 “上海最高气温预测” 两个事件：

- 语义单一，不把多个业务问题混在一个 bundle 里；
- 需要自动市场发现，从 event 找到对应 markets / outcomes / token ids；
- Event 会成为一段回测的完整上下文；
- event bundle 里要能带上 market / fee / settlement metadata；

预计本周出第一版数据。


2. 回测框架

v1 版本做的是 Polymarket 数据合同、数据健康检查、Nautilus 原生数据转换，以及 Polymarket 特有的 tick / fee / settlement 接入。

2.1 目录结构


polymarket/
  README.md                         ---- Polymarket v1 的总说明和使用入口
  DATA_CONTRACT_V1.md                ---- v1 数据合同说明，定义 IT / adapter 应交付什么数据
  backtest_v1.py                     ---- 回测 runner 主入口，负责加载数据、接 Nautilus engine、输出报告
  data_health.py                     ---- 回测前数据健康检查，校验 receive time、内部 replay 行号等
  strategy.py                        ---- PolymarketStrategyBase，给 Nautilus Strategy 补动态 tick 下单 helper
  
  _core/                             ---- 核心纯逻辑和 Nautilus 桥接层
    models.py                        ---- PolymarketL2DatasetV1 等数据合同模型
    nautilus_native.py               ---- 把 Polymarket 数据转成 Nautilus 原生对象
    fees.py                          ---- fee source / fee report / explicit fee 校验
    tick_size.py                     ---- 根据 timeline 解析当前 effective tick size
    price_rounding.py                ---- 把策略价格投影到合法 tick 上
    reports.py                       ---- 生成研究友好的 account / fills / positions 报告，并保留 Nautilus 原始审计表
  
  adapters/                          ---- 数据源适配层，把 raw/event bundle 转成 v1 数据合同
    base.py                          ---- adapter protocol / 基础接口
    live_ws_v1.py                    ---- 读取 live WebSocket 抓包的 adapter
    live_event_bundle_v1.py          ---- 面向 IT event bundle 的 adapter（等待使用）
    utils.py                         ---- adapter 共用工具，如字符串时间 -> datetime
  
  _tools/                            
    normalize_live_ws_v1.py          ---- 把旧 capture wrapper 规范化成 NDJSON（以后会删）
    inspect_nautilus_conversion.py   ---- 检查 Polymarket -> Nautilus 转换结果
  
  research/                          ---- 研究 / smoke test 实验目录（这里只列 v1 主线相关）
    2026-07-03-live-ws-data-health/  ---- live WS 数据健康检查研究记录
    2026-07-03-live-ws-mock-debug/   ---- mock live WS 调试实验
    2026-07-03-live-ws-t02-simple-strategy/ ---- live WS 抓包上的简单策略 round-trip 实验
    2026-07-04-it-data-acceptance/   ---- IT event bundle / 数据验收口径

  ideas/                             ---- 策略和后续方向的草稿，不进入 v1 runtime

  boss_reports/                      ---- boss-facing 汇报材料
    README.md                        ---- boss report 目录说明
    2026-07-06-polymarket-nautilus-native-backtest-v1-closure-report.md ---- 当前 v1 收尾报告

  tests/                             ---- 各种单测
    fixtures/                        ---- 测试用最小数据样例

2.2 运行逻辑

回测时钟使用 timestamp_received。source timestamp 只做诊断，不参与 replay 排序。

加载 adapter
  -> 跑 Data Health Gate
  -> 构建 BinaryOption instrument
  -> 转换 Nautilus native data
  -> 挂 venue / instrument / data
  -> 加载 Nautilus Strategy
  -> 注入 Polymarket tick timeline / price guard
  -> 跑 BacktestEngine
  -> 输出报告

配置方式：

现在 runner 是 YAML 驱动。每个实验提交一个 experiment.yml，里面写清楚：

- adapter 输入数据路径；
- selection / instrument / fee；
- strategy 路径、class 和参数；
- engine 参数，如 trade_execution、liquidity_consumption、queue_position；
- portfolio 初始资金；
- runtime.run_id；
- report.output_dir。

启动方式类似：

python -P -m polymarket.backtest_v1 --config polymarket/research/<experiment>/experiment.yml

runner 会把原始 YAML 复制到 run 目录里的 original_config.yml，同时生成 resolved_config.json。

resolved_config.json 记录的是“本次实际生效配置”，包括：

- config_path；
- source_files_resolved，也就是这次实际用了哪些数据文件；
- strategy.source_path_resolved，也就是这次实际加载了哪个策略文件；
- instrument / fee / tick / settlement 的最终解析结果；
- data_health 摘要；
- report 输出目录。

所以每次跑完都能反查：用的是哪份数据、哪个策略、哪些参数、哪个 run_id、输出落在哪里。report.output_dir 也被限制在当前实验目录的 runs/ 下，避免报告乱写到项目其他位置。

Data Health Gate 的规则：

- timestamp_received 不能倒退；
- 内部 replay 行号必须递增；
- source timestamp 倒序只报 warning；
- future source timestamp 只报 warning；
- 不靠 sort 去“修”数据。


输出包括：

- original_config.yml ---- 本次提交给 runner 的原始 YAML；用于保留实验入口配置。
- resolved_config.json ---- 实际生效配置快照；用于复现实验，记录数据路径、strategy、instrument、fee、tick、settlement 等解析结果。
- data_health.json ---- 数据健康检查结果；看 receive time、source time、adapter 输出顺序是否过关。
- account.csv ---- 研究友好的账户状态时间线；一行是一次 account state 里的一个币种余额，字段比 Nautilus 原始表更短。
- fills.csv ---- 研究友好的成交订单汇总；一行是一个有成交的 order，能看成交均价、成交数量、方向、手续费、gross / net cashflow 和状态。
- positions.csv ---- 研究友好的持仓明细；一行是一个 position / snapshot，能看仓位如何打开、关闭，以及 realized / unrealized PnL。
- raw_nautilus/account.csv ---- Nautilus 原始 account report；用于审计和 debug，不做字段裁剪。
- raw_nautilus/fills.csv ---- Nautilus 原始 fills report；保留完整 order-level 字段，方便和 engine 原始输出对账。
- raw_nautilus/positions.csv ---- Nautilus 原始 positions report；保留完整 position 字段，方便和 engine 原始输出对账。
- summary.json ---- 机器可读摘要；用于跑批对比和程序化读取指标，也记录 curated report 和 raw_nautilus report 的路径。
- run_report.md ---- 给人看的单次回测报告；快速复盘输入、输出、fee、settlement 和风险提示。
这些输出后续用于策略复盘、数据验收和问题定位。



2.3 Adapter

adapter 的职责是把不同输入源统一成 PolymarketL2DatasetV1。

PolymarketL2DatasetV1 的核心结构：

metadata:                                      ---- 整个 dataset 的来源、市场和审计信息，不参与逐笔 replay
  dataset_id                                  ---- 本次数据集 id，用于追踪一次回测输入
  adapter_name / adapter_version              ---- 哪个 adapter 生成，方便定位解析版本
  source_type / source_files                  ---- 数据来源类型和原始文件列表
  market_metadata:                            ---- 市场级 metadata，给 instrument / fee / settlement 使用
    condition_id                              ---- Polymarket 条件 id，用来识别同一个市场条件
    token_id / outcome                        ---- 当前回测的 token / outcome
    maker_fee / taker_fee / fee_source        ---- fee 参数和来源，供 PolymarketFeeModel 使用
    minimum_tick_size / tick_size_source      ---- 初始 tick size 及来源
    resolution_status / resolution_time / token_payout / winner ---- 结算状态、时间、赔付和输赢结果

steps:                                        ---- replay 时间线，BacktestEngine 按这个顺序吃数据
  sequence                                    ---- 内部 replay 行号，用于审计 adapter 输出顺序
  timestamp_received                          ---- 回测时钟，决定 replay 顺序
  timestamp                                   ---- source timestamp，只用于诊断，不参与排序
  updates:                                    ---- 一个 replay step 内的原子更新集合
    event_type = book | price_change | trade | tick_size_change ---- 更新类型
    market                                    ---- Polymarket market id
    asset_id                                  ---- token / asset id
    side / price / size                       ---- 单档盘口或成交更新字段
    bids / asks                               ---- book snapshot / 多档盘口
    best_bid / best_ask                       ---- 最优买卖价，主要用于诊断和补充信息
    old_tick_size / new_tick_size             ---- tick size change 事件的前后 tick

adapter 支持的输入语义：

- book
- price_change
- trade
- tick_size_change
- market / fee / settlement metadata
成交、持仓、现金、PnL 全部交给 Nautilus BacktestEngine。adapter 不负责撮合，也不生成策略 fill。


2.4 Polymarket 兼容

Fee

Polymarket fee 不是普通 fixed commission。v1 已经能把 fee metadata 接进 BinaryOption，并交给 Nautilus 的 PolymarketFeeModel。

当前 report 会显式写出：

- fee model 是否开启；
- maker / taker fee；
- fills report 里实际汇总到的 fee total。
这解决的是“fee 不能静默消失”。如果正式回测要求 fee metadata，但数据侧没给，fees.require_explicit=true 会 fail fast。

tick_size_change

Polymarket 有动态 effective tick。Nautilus instrument 仍使用静态 0.001 price precision；策略真正下单前，需要把模型价格投影到当前有效 tick。

v1 的处理方式：

- 默认初始 tick 可以是 0.01；
- 收到 tick_size_change 后，按 timeline 切到新的 effective tick，例如 0.001；
- 策略基类负责下单前价格投影；
- runner 额外安装 submit-time guard，兜底挡住非法 tick price。

Settlement

v1 支持三类 settlement path：

- official metadata：有数据侧明确结算信息时走这个；
- inferred：根据市场收敛情况估算，用于 research convenience；
- open：未结算市场不强行结算。
正式 PnL 口径应优先走 official settlement source。inferred settlement 只能作为研究便利项，后续建议继续收紧成 opt-in。


策略基类

PolymarketStrategyBase 已完成。

它是 Nautilus Strategy 的轻量子类，不替代 Nautilus 策略系统，只解决 Polymarket 特有的价格合法性问题：

- current_effective_tick_size(...)      // 获取当前 tick size
- round_price_to_current_tick(...)  --> Decimal    // 仅 round
- make_polymarket_price(...) --> Nautilus Price     // 检查精度并 round，喂给 engine
- polymarket_price_rounding_events     // 审计日志
price projection 已拆成 pure helper：

polymarket/_core/tick_size.py
polymarket/_core/price_rounding.py
polymarket/strategy.py

策略基类负责“正确表达交易意图”，submit-time guard 负责“兜底挡住非法价格”。rounding audit 会落到 resolved_config.json。



2.5 测试护栏 / 回测准确性检查

设置了一组 tests，用来单测回测链路，是后续回测架构的通用单测。

这些测试覆盖：

- runner 必须使用 Nautilus 原生 BacktestEngine，不能退回自定义撮合；
- data health 必须先于 Nautilus 转换执行，不能靠事后 sort 修数据；
- replay clock 必须使用 timestamp_received；
- Polymarket book / price_change / trade 必须转成 Nautilus 原生 OrderBookDeltas / TradeTick；
- settlement metadata 必须转成 InstrumentClose / settlement path，不能伪装成普通 trade；
- tick size change 前后的价格合法性必须正确，非法 tick price 要 fail fast；
- 策略基类 rounding 后的价格可以正常提交，未 rounding 的非法价格会被 guard 拦住；
- fee metadata 缺失时不能静默当成成功，fees.require_explicit=true 会 fail fast；
- runner 输出必须包含 fills / positions / account / summary / resolved config / data health 等可审计产物；
- adapter 对 raw / normalized live WS、event bundle metadata、异常事件类型都有 contract 检查。

当前本地测试结果：

python -m pytest -q polymarket/tests
29 passed, 6 skipped

其中 skipped 是本机 Nautilus compiled runtime 没构建导致的 native runtime 用例跳过，不是源码测试失败。

这组测试证明的是：v1 的数据合同、转换、runner、报告和 Polymarket 特殊规则没有明显跑偏。它不证明 Polymarket 真实队列 / partial fill / latency 已经被校准；这一块仍属于 v2 fill model。



3. v2 方向

v2 不是重写 v1，而是把 research 级 runner 往 production / live-adjacent 推。

3.1 IT / 数据 feed

- 固化 event bundle schema；
- market / fee / settlement metadata 必须齐；
- metadata timeline 化，而不是单 snapshot。（需要跟 IT 沟通，是轮询还是订阅）


3.2 执行语义

v1 现在使用的是 Nautilus generic backtest matching：策略可以提交 Nautilus 支持的 MARKET / LIMIT 等订单，engine 会基于 replay 出来的 L2 book 生成 fills，允许部分成交。

Nautilus 的 Polymarket live adapter 已经有基础 order mapping：LIMIT / MARKET、GTC / GTD / FOK / IOC、cancel 等路径都在 adapter 里；但 v1 report 不把它包装成 production live 框架。

v2 考虑在回测引擎中对齐这些语义。比如 live 侧 market order / IOC / FOK 真实走 Polymarket API，回测侧则要明确用什么 worst-price / liquidity-consumption 近似。


3.3 Fill model

v1 只把数据喂给 Nautilus engine，不复原真实 L3 队列。

v2 要继续看：

- L2 queue model；
- liquidity consumption；
- taker delay；
- cancel-before-fill；
- partial fill 近似。

3.4 跨 market 回测

支持多个 market 同时回测，策略同时考虑多 token 盘口。



4. v1 的边界

v1 支持：

- 单 token / 指定 outcome 回测；
- L2 MBP order book replay；
- trade prints 转 TradeTick；
- tick size timeline；
- strategy-level dynamic tick price projection；
- rounding audit；
- official / inferred / open 三种 settlement 模式；
- fee metadata 接入；
- Nautilus 原生策略执行；
- Nautilus 原生 fills / positions / account 报告。

v1 不承诺：

- 不是 production Nautilus adapter package；
- 不是 live trading adapter；
- 不覆盖 PMXT runnable path；
- 不做 L3 queue 重建；
- 不做真实排队 / partial fill 校准；
- 缺 fee metadata 时不保证 fee 正确；
- research strategy 不是 live execution 模板。


5. 下一步计划
- 与 IT 数据张琦确认 Data Schema
- 开始刷策略
