# Polymarket 通用 Event Alpha 研究 Idea

日期：2026-07-15

## 目的

本文是后续 boss report 讨论素材，不作为已验证 alpha 或交易结论。核心目的不是证明天气市场本身存在可交易机会，而是把天气 Event 作为标准试验田，沉淀可推广到政治、宏观、体育等 Polymarket Event 的通用研究与交易能力。

## 核心观点

天气不是最终业务边界。天气 Event 的价值在于它具备几个适合作为研究试验田的性质：

- 生命周期短：Event 从活跃、定价、临近结算到结束的过程清晰，便于观察 lifecycle 中的信号变化。
- 多结果结构明确：一个 Event 内存在多个互斥或近似互斥 outcome，可自然形成概率分布。
- 重复样本多：城市、日期、温度区间等维度提供可重复样本，便于做配对比较。
- 结算规则清晰：最终状态可验证，适合检查概率收敛、结构一致性和执行边界。

因此，天气研究的目标应从“天气 alpha”上升为“通用 Event alpha 框架”：先在天气上验证 Event 表示、市场结构、生命周期、relative value、执行约束，再推广到政治、宏观、体育等更高价值但更复杂的 Event。

## 通用 Event 表示

通用 Event 研究对象不应只停留在单 token 或单 market，而应表达整个 Event 的状态、结构和生命周期。

| 组件 | 含义 |
| --- | --- |
| Outcome probability vector | Event 内各 outcome 的概率向量，是 Event-level 研究的核心状态。 |
| Event graph / ontology | Event、market、outcome、token、结算条件之间的关系图，定义哪些对象可比较、可加总、可替代。 |
| Lifecycle | Event 从创建、活跃、临近结算到结束的阶段划分。 |
| Dynamic active set | 当前真正有交易、报价或价格发现意义的 outcome 子集，而不是静态使用全部 outcome。 |
| Entropy / concentration | 概率分布的不确定性与集中度，用于衡量 Event 是否已接近单一结果。 |
| Probability mass flow | 概率质量在 outcomes 之间如何迁移，用于观察预期变化和内部重定价。 |
| Sum / simplex consistency | outcome 概率和是否满足结构约束，例如总和接近 1 或符合 Event 定义的 simplex。 |
| Leader-lag | Event 内或跨 Event 的价格发现先后关系。 |
| Execution boundary | 哪些信号只适合观察，哪些信号在 spread、深度、latency、fees、inventory 后仍可执行。 |

对于 ordered outcome，可以额外构造 mean、variance、skew、tail mass 等有序分布特征。天气 implied temperature 属于 weather domain 的插件特征，只应放在诊断层或 domain alpha 层，不应成为通用 Event 框架的核心定义。

## 通用架构

建议将研究与交易链路拆成以下层次：

```text
Event discovery
  -> event/outcome 关系
  -> lifecycle / active set
  -> 通用 alpha
  -> 可选 domain alpha
  -> order intent / execution
  -> Nautilus portfolio / PnL
```

各层职责：

- Event discovery：发现可研究的 Event，收集 metadata、结算规则、market/outcome 列表。
- Event/outcome 关系：建立 Event graph，识别互斥、互补、ordered、bucket、duplicate、related-market 等关系。
- Lifecycle / active set：定义当前 Event 阶段和可交易 outcome 子集，避免把 inactive 或 stale outcome 纳入核心判断。
- 通用 alpha：只依赖通用市场结构、概率分布、流动性、价格发现和生命周期特征。
- 可选 domain alpha：接入天气预报、民调、宏观数据、伤病信息等领域信息，但作为插件而非主框架。
- Order intent / execution：把研究信号转成订单意图，明确 taker / maker、size、price、inventory、取消条件和执行边界。
- Nautilus portfolio / PnL：统一进入组合、风险、成交、持仓、PnL 与回测框架。

## Endogenous Market Alpha 定义

Endogenous market alpha 指只依赖市场内部产生的数据来识别可预测行为，不使用外部基本面信息。

可使用的数据包括：

- prices
- orderbook / L2
- trades
- cancels
- activity
- spread
- depth
- volume
- related-market responses
- Event 内 outcome 概率结构
- 跨 Event 的市场反应

它试图回答的问题是：市场自身的报价、成交、撤单、流动性变化、相关市场响应中，是否存在可预测的短期或结构性行为。

与之对照，exogenous / domain fundamental alpha 依赖外部领域信息，例如：

- 天气预报、气象模型、观测站数据
- 民调、新闻、政策日程
- 经济数据、央行信息、宏观指标
- 体育伤病、阵容、赛程、赔率信息

两类 alpha 的边界很重要。Endogenous alpha 更可跨 domain 复用，但更容易受到执行成本、adverse selection 和 crowding 影响；domain fundamental alpha 可能更强，但依赖领域数据、解释链和专门知识。

## Alpha Taxonomy

这些方向可以叠加，不是互斥分类。一个策略可能同时使用 market structure、relative value、lifecycle gate 和 passive execution，只是每一层的经济来源不同。

| 方向 | 经济来源 | 例子 | 优点 | 缺点 |
| --- | --- | --- | --- | --- |
| Exogenous fundamental | 外部信息比市场更快或更准地反映最终结算概率。 | 天气预报更新领先温度市场；民调变化领先选举市场；伤病信息影响体育胜负概率。 | alpha 来源直观，可能强度高。 | domain 依赖重，数据接入和质量控制复杂，泛化难。 |
| Informed-flow / order-book | 知情交易者或更快交易者的行为先体现在成交、挂单、撤单和盘口变化中。 | 大额主动买入后价格继续移动；撤单导致一侧深度消失；相关 outcome 同步重定价。 | 不依赖外部数据，跨市场可复用。 | 容易把噪声、库存调整或短期流动性压力误判为信息。 |
| Behavioral / attention | 参与者注意力、叙事、热点或可见事件导致短期过度反应或反应不足。 | 新闻后交易量激增但价格过冲；热门 Event 的长尾 outcome 被非理性追买。 | 可解释部分散户市场行为。 | 稳定性弱，容易随参与者结构变化衰减。 |
| Liquidity provision / market making | 提供流动性、赚取 spread、管理库存和 adverse selection。 | 在宽 spread outcome 上被动挂单；对冲 Event 内概率暴露；在 active set 内动态调整 quote。 | 与预测最终结果的相关性较低，可与其他 alpha 叠加。 | 对执行、排队、撤单、库存和风控高度敏感。 |
| Event 内结构性 relative value | 同一 Event 内 outcome 概率之间存在结构约束，错误定价可通过相对价值表达。 | outcome 概率和偏离 1；相邻温度 bucket 概率不平滑；leader outcome 与 tail outcomes 质量迁移不一致。 | 直接利用 Event 结构，适合多结果市场。 | 需要精确 ontology 和交易可达性；套利可能被费用、深度和结算规则吃掉。 |
| 跨 Event 信息传播 / leader-lag | 一个 Event 或 market 先反映信息，相关 Event 滞后调整。 | 同城市相邻日期天气市场联动；总统提名市场领先大选市场；宏观数据 Event 领先利率路径 Event。 | 可推广到政治、宏观、体育的相关市场网络。 | 需要定义关系图，避免伪相关和共同冲击误判。 |
| Lifecycle / settlement convergence | 临近结算时不确定性下降，概率、流动性和参与者行为发生系统性变化。 | 到期前 active set 收缩；最终 outcome 概率快速集中；错误的 stale quote 被清除。 | 生命周期清晰，易与执行约束结合。 | 临近结算 spread、latency 和 adverse selection 风险更高。 |

## 盘口因子的边界

“盘口因子 = 跟单信息优势交易者”只解释其中一部分现象。盘口是多种力量混合后的结果，不能默认所有 imbalance 或 order-book pressure 都来自知情交易。

盘口可能混合以下来源：

- 知情交易：更快或更准的信息通过成交和挂单体现。
- 噪声交易：非信息驱动的买卖、追涨杀跌、小额随机行为。
- 做市库存：maker 为管理库存而调整 quote，不一定表达方向观点。
- 撤单 / 流动性变化：深度消失可能是风险控制、latency、库存或重新报价。
- 机械约束：tick size、最小订单、余额、组合限制、API 行为等造成的价格形态。
- Stale quote：旧报价未及时更新，可能短暂产生虚假的 imbalance 或 relative value。

因此，imbalance 可能捕获知情流，也可能只是短期流动性压力或 adverse selection。必须用更完整的证据区分：

- trade：主动成交是否跟随后续价格变化。
- cancel：撤单后是否出现同方向价格调整，还是只是流动性回撤。
- 后续价格：信号后 mid、BBO、成交价是否持续移动。
- 跨 market 一致性：Event 内其他 outcome 或相关 Event 是否同步响应。
- 结构约束：probability vector 是否朝更一致的 simplex 状态移动。
- 执行结果：扣除 spread、fees、slippage、queue 后是否仍有正贡献。

## 对当前项目的建议

当前阶段不宜把研究主线押在纯 L2 taker 上。纯 L2 taker 风险高，原因包括：

- taker 直接支付 spread，短周期信号很容易被执行成本吞掉。
- L2 imbalance 可能是 adverse selection，而不是可跟随的信息。
- Polymarket 深度、更新频率、撤单行为和 active set 变化会放大回测与实盘差异。
- 单 token 信号难以表达多 outcome Event 的结构约束。

更可推广的主线应是：

- 通用市场结构：先把 Event graph、probability vector、active set、lifecycle、simplex consistency 做稳。
- Relative value：优先研究 Event 内概率结构与跨 Event 关系，而不是只看单 token 方向。
- Passive execution：将 maker / passive order intent 纳入核心路径，降低 spread 成本并观察真实可成交性。
- Domain signals 插件化：天气预报、implied temperature、民调、宏观数据等作为 domain alpha 插件接入，不污染通用框架。
- Weather Event-level 实验指标保持通用：核心指标应继续围绕 lifecycle、active set、entropy、concentration、probability mass flow、leader-lag、execution boundary。Weather implied temperature 放在诊断层，用于解释 weather ordered outcome，不作为通用 Event 表示的必要条件。

## Boss Report 讨论提纲

后续可摘入 boss report 的讨论结构：

1. 已经完成什么
   - 已建立天气 Event 的 replay / anchor / token-level baseline。
   - 已观察到天气 Event 具有短生命周期、多 outcome、active set 收缩、Event-level probability distribution 等结构。
   - 当前成果是研究框架与结构识别，不宣称已发现可交易 alpha。

2. Weather 为何作为试验田
   - 生命周期短，便于观察从定价到结算的全过程。
   - 重复样本多，可做配对比较。
   - 多结果结构明确，适合验证 probability vector 和 relative value。
   - 结算清晰，适合检查收敛和执行边界。

3. 识别出的三项结构
   - Lifecycle：不同到期阶段的 activity、spread、active set、价格发现行为不同。
   - Dynamic active set：Event 内通常只有少数 outcome 真正活跃，静态全量 outcome 会稀释信号。
   - Event probability distribution：单 token 不是唯一研究对象，Event-level 概率分布更接近真实交易问题。

4. 通用化路径
   - 从 weather Event 表示推广到 generic Event graph / ontology。
   - 从 token-level factor 推广到 probability vector、mass flow、relative value、leader-lag。
   - 从单 market 回测推广到 order intent、execution boundary、Nautilus portfolio / PnL。

5. 不同 alpha 方向
   - Exogenous fundamental。
   - Endogenous market alpha。
   - Liquidity provision / market making。
   - Event 内 relative value。
   - 跨 Event leader-lag。
   - Lifecycle / settlement convergence。

6. 下一阶段决策
   - 是否把下一阶段主线切到 Event-level unified representation。
   - 是否优先做 active set、simplex consistency、probability mass flow。
   - 是否把 passive execution 和 maker diagnostics 提升为核心实验。
   - 是否把 weather implied temperature 降级为诊断层插件。

7. 风险
   - 盘口信号被误读为知情流。
   - Taker 回测收益被 spread 和 slippage 吞掉。
   - Event ontology 错误导致 relative value 失效。
   - Weather 结构对政治、宏观、体育迁移时出现 domain gap。
   - 数据覆盖、stale quote、撤单与成交重建误差影响结论。

## 决策矩阵

| 方向 | 可推广性 | Alpha 强度潜力 | 数据要求 | 执行敏感度 | 当前成熟度 |
| --- | --- | --- | --- | --- | --- |
| 纯 L2 taker / imbalance | 中 | 中 | L2、trades、BBO、后续价格 | 高 | 已有初步 baseline，但风险高 |
| 通用 Event 表示 | 高 | 中 | Event metadata、outcome 关系、prices、orderbook、trades | 中 | 应作为下一阶段基础设施 |
| Event 内 relative value | 高 | 中高 | Event graph、probability vector、active set、费用与深度 | 中高 | 适合优先推进 |
| Passive execution / market making | 高 | 中 | L2、queue、fills、cancels、inventory、spread | 高 | 需要纳入 Nautilus 执行链路 |
| Cross-Event leader-lag | 高 | 中高 | 相关 Event ontology、时间同步 prices/trades/orderbook | 中 | 需要先建设关系图 |
| Lifecycle / settlement convergence | 高 | 中 | Event time、expiry、settlement、activity、prices | 中高 | Weather 上最适合先验证 |
| Domain fundamental 插件 | 中 | 高 | 天气预报、民调、宏观、体育等外部数据 | 中 | 应插件化，不应成为主框架 |
| Weather implied temperature | 低到中 | 中 | Ordered weather outcomes、价格向量 | 中 | 适合诊断层，不适合作为通用核心 |

## 结论

建议把天气研究定位为通用 Polymarket Event alpha 能力的试验田，而不是天气专项交易项目。下一阶段应优先建立 Event-level 表示、dynamic active set、probability mass flow、relative value 和 execution boundary，再把 weather implied temperature 等 domain 特征作为插件接入。

当前可以讨论研究方向和架构取舍，但不应宣称已经发现可交易 alpha。
