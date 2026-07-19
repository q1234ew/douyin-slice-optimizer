# V1 Beta-D-12B：百炼独立留出复验

日期：2026-07-19
目标：G3 本地/公网模型协同
状态：`research_only / inconclusive_keep_v2_4`

## 1. 目的与防泄漏

D12-B 用 D12-A 从未参与权重选择的剩余 20 对样本，检验 `85% v2.4 + 15% cloud` 是否具有独立泛化收益。实验继续使用冻结 benchmark `dso-multimodal-vector-value-20260719-r1`，manifest SHA-256 为 `f32a6699ffc2cc7554ea6e2fa9a0550afc7e92a91cd2c46585beb130544a1510`。

- 校准集：`pair-001` 至 `pair-040`。
- 独立留出：`pair-041` 至 `pair-060`，40 个唯一短片样本。
- 两个 split 的样本重叠为 0。
- 配置 SHA-256：`7b83dbf0306eda51b782118de43b96339721f8451d07ebb13b21495aa52d8b00`。
- 预测 SHA-256：`310d49bc60b95b54f6a2a2ebdd5141db9ef3823e17fa6cdcc6c501f98c5a65e3`。
- 评估 SHA-256：`f2311ed560de9f25d685bb70c01d0a5d322302d5b2d33f0b8b3b408318e8c680`。

流程固定为“冻结配置 -> 生成盲预测 -> 解锁结果”。预测 artifact 禁止出现 `proxy_choice / performance_label / normalized_reward / reward_proxy` 等结果字段；只有预测 SHA 写入后，评估阶段才读取账号内归一化的抓取时点可见互动代理。

## 2. 固定配置

本轮没有搜索权重：

```text
Text cosine：高/低互动平衡参考池 20，K=3/label
Cloud：50% Text embedding delta + 50% cached Rerank delta
Final：85% v2.4 delta + 15% Cloud delta
Normalization：只使用 D12-A 40 对冻结的 median absolute pair delta
```

冻结尺度为：Text `0.032951532441`、Rerank `0.6338`、v2.4 `3.9056`、Cloud `0.755933864669`。留出集没有重新计算尺度、选择配置或改变 tie 规则。

## 3. 请求与费用

ECS 先完成零网络 preflight：80 个 Text/Fusion embedding 请求和 40 个 Rerank 请求的最坏预留合计 `2.0543848 CNY`，低于用户确认的单实验 `10.00 CNY` 硬上限。

真实批次最终：

- 留出样本 Text/Fusion 覆盖均为 `40/40`。
- Rerank 完成为 `40/40`。
- 网络请求 `120` 次，无 schema、网络或预算错误。
- 本地 usage 估算费用为 `0.2243942 CNY`；赠送 Token 是否抵扣以百炼账单为准。
- 只发送结构化摘要与代表帧，没有上传完整视频，没有调用 Qwen3.7/Qwen3.6 Judge。

## 4. 独立结果

| 指标 | 固定融合 | v2.4 | 差值 |
| --- | ---: | ---: | ---: |
| 20 对平衡命中 | 76.26% | 76.26% | 0pp |
| 20 对原始命中 / 低互动避让 | 75.00% | 75.00% | 0pp |
| yuhuan（4 对） | 50.00% | 50.00% | 0pp |
| hukan_music（3 对） | 75.00% | 75.00% | 0pp |
| tianci（3 对） | 100.00% | 100.00% | 0pp |

20 对中最终选择相对 v2.4 改变 `0` 次。组件后验诊断显示：Text embedding 平衡命中 `60.61%`、Rerank `66.16%`、50/50 Cloud `60.61%`；Cloud 与 v2.4 有 3 对符号冲突，但 15% 权重不足以改变最终选择。这说明 D12-A 的同集增益没有在独立留出集上复现，不能提高云权重。

D12-A 40 对按冻结配置回放仍为 `81.34%`，较同子集 v2.4 高 `11.54pp`。将两部分合并后的 60 对次要指标为 `79.67%`，较 v2.4 `72.85%` 高 `6.82pp`；由于前 40 对参与过配置选择，该合并值不能代替独立留出结论。

## 5. Gate 与结论

通过项：低互动避让不下降、60 对合并后有 3 个样本量充足账号改善、费用低于 10 元、盲预测不可变。未通过项：独立增量未达到 `+5pp`，且 yuhuan/hukan_music 都没有改善。

```text
status=inconclusive
decision=inconclusive_keep_v2_4
production_promotion=false
production_weight_changed=false
writes_manual_gold=false
```

下一步不应在同一留出集继续调权，也不恢复 Judge 扩量。云路线若继续，只能先改造证据目标或建立新的未见账号/节目冻结集；D12 研究对照保持 `research_ranker_v2_4`，生产默认策略仍为 `current_rules/final_score`。

## 6. 复现

```bash
dso bailian-vector-holdout --stage freeze \
  --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-holdout --stage predict \
  --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-holdout --stage evaluate \
  --benchmark-id dso-multimodal-vector-value-20260719-r1
```

报告位于：

```text
outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/holdout-config-latest.json
outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/holdout-predictions-latest.json
outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/holdout-evaluation-latest.json
```
