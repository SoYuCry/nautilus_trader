# Boss scorecard：PMXT 天气因子 pilot

- 阶段：6-event pilot；城市 6；clean 3 / degraded 3。
- Runtime：7.7 分钟；event failure：0/6。
- 因子统计路径：**U0**；扩张：**NO-GO**。
- E2：价格分层样本不足，只能诊断；不得当作正式结论。
- valid_obs：diagnostic-only；已达到 amendment 候选门槛，但尚未采用。
- direct→Nautilus parity：FAIL，8 条检查中 6 条通过；Beijing YES/NO 两条在有效 tick 仍为 `0.01` 时出现 `0.001` 价格。

## 主指标

| factor | horizon | coverage | zero | median IC | positive event | activity IC diff | gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| depth_imbalance_1 | 30s | 0.995 | 0.763 | 0.053 | 1.000 | 0.031 | PASS |
| depth_imbalance_1 | 120s | 0.988 | 0.671 | 0.076 | 1.000 | 0.039 | PASS |
| depth_imbalance_1 | 600s | 0.972 | 0.529 | 0.079 | 1.000 | 0.073 | FAIL |
| microprice_minus_mid | 30s | 0.995 | 0.763 | 0.051 | 1.000 | 0.039 | PASS |
| microprice_minus_mid | 120s | 0.988 | 0.671 | 0.062 | 0.833 | 0.053 | FAIL |
| microprice_minus_mid | 600s | 0.972 | 0.529 | 0.076 | 0.667 | 0.091 | FAIL |

## 下一步

pilot 基础设施通过且无 stop condition 时，按预注册样本设计扩至 36 Event；否则先修复失败项。
