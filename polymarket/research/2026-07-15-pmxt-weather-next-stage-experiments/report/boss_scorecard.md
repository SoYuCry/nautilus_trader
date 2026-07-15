# Boss scorecard：PMXT 天气因子 pilot

- 阶段：6-event pilot；城市 6；clean 3 / degraded 3。
- Runtime：3.8 分钟；event failure：0/6。
- 因子统计路径：**U1 / R1（pilot 仅诊断，进入 36-event 前需确认 regime）**；扩张：**NO-GO**。
- E2：价格分层样本不足，只能诊断；不得当作正式结论。
- valid_obs：diagnostic-only，未触发 amendment。
- direct→Nautilus parity：FAIL：ValueError("price violates effective tick size: sequence=1, asset_id='90126329302086190846098290501947516698603202176436371691446950377026992913400', price=0.0170, effective_tick_size=0.01")；按预注册规则暂不扩张。

## 主指标

| factor | horizon | coverage | zero | median IC | positive event | activity IC diff | gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| depth_imbalance_1 | 30s | 0.926 | 0.892 | 0.049 | 0.833 | 0.142 | FAIL |
| depth_imbalance_1 | 120s | 0.904 | 0.826 | 0.075 | 0.667 | 0.180 | FAIL |
| depth_imbalance_1 | 600s | 0.816 | 0.709 | 0.153 | 0.833 | 0.119 | FAIL |
| microprice_minus_mid | 30s | 0.926 | 0.892 | 0.064 | 0.833 | 0.116 | FAIL |
| microprice_minus_mid | 120s | 0.904 | 0.826 | 0.093 | 0.833 | 0.150 | FAIL |
| microprice_minus_mid | 600s | 0.816 | 0.709 | 0.146 | 1.000 | 0.236 | FAIL |

## 下一步

pilot 基础设施通过且无 stop condition 时，按预注册样本设计扩至 36 Event；否则先修复失败项。
