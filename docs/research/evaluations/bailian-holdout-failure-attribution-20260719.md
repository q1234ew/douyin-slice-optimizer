# V1 Beta-D-12C0：独立留出零成本失败归因

日期：2026-07-19
目标：G3 本地/公网模型协同
状态：`research_only / keep_v2_4_and_redesign_evidence`

## 1. 范围与完整性

本轮只读取 D12-B 已冻结的 manifest、Text/Fusion 向量、Rerank、盲预测和揭盲评估，不构造 Provider Runtime，不发送新请求，不重新选择权重。输入仍为 `dso-multimodal-vector-value-20260719-r1` 的 `pair-041..060`：20 对、40 条留出样本和 20 条高低互动平衡参考。

- Manifest SHA-256：`f32a6699ffc2cc7554ea6e2fa9a0550afc7e92a91cd2c46585beb130544a1510`
- D12-B 配置 SHA-256：`7b83dbf0306eda51b782118de43b96339721f8451d07ebb13b21495aa52d8b00`
- D12-B 预测 SHA-256：`310d49bc60b95b54f6a2a2ebdd5141db9ef3823e17fa6cdcc6c501f98c5a65e3`
- D12-B 评估 SHA-256：`f2311ed560de9f25d685bb70c01d0a5d322302d5b2d33f0b8b3b408318e8c680`
- 归因报告 SHA-256：`1cc88ddcd220916bac673938cd54c228b5f40952f43ad3eff0242b8b6bb0624f`
- 网络请求：`0`
- 费用：`0 CNY`

## 2. 组件结果

| 组件 | 平衡命中 | 原始命中 |
| --- | ---: | ---: |
| v2.4 | 76.26% | 75.00% |
| Text embedding | 60.61% | 60.00% |
| Fusion embedding | 45.96% | 45.00% |
| Rerank | 66.16% | 65.00% |
| Text/Rerank 50/50 Cloud | 60.61% | 60.00% |
| v2.4/Cloud 85/15 Final | 76.26% | 75.00% |

20 对中有 12 对 v2.4 与 Cloud 同时正确、5 对同时错误、3 对 v2.4 正确但 Cloud 错误。Cloud 没有纠正任何一对 v2.4 错误；15% 权重使这 3 个错误信号没有改变最终选择，因此 Final 与 v2.4 完全相同。

后验权重网格只用于解释决策弹性，不是调参结果：Cloud 权重 25% 仍不改变选择；50% 时开始改变 1 对且原始命中从 75% 降至 70%；100% 时改变 3 对并降至 60%。三对冲突的中位翻转权重为 81.09%，提高云权重没有客观依据。

## 3. 失败样本

| 类型 | Pair | 账号 | 内容分类 |
| --- | --- | --- | --- |
| v2.4 与 Cloud 同错 | 043 | yuhuan | performance_clip |
| v2.4 与 Cloud 同错 | 045 | sixuweilive | music_variety |
| v2.4 与 Cloud 同错 | 053 | weibabibibi | performance_clip |
| v2.4 与 Cloud 同错 | 057 | hukan_music | performance_clip |
| v2.4 与 Cloud 同错 | 059 | yuhuan | music_variety |
| Cloud 错、v2.4 对且被抑制 | 048 | raoxianyin | music_variety |
| Cloud 错、v2.4 对且被抑制 | 051 | yuhuan | performance_clip |
| Cloud 错、v2.4 对且被抑制 | 052 | tianci | performance_clip |

## 4. 根因

1. **云证据没有对齐结果目标。** Text、Rerank 和 50/50 Cloud 都弱于 v2.4，Cloud 在独立集上只有风险信号，没有新增纠错价值。
2. **视觉输入缺少时间覆盖。** 40/40 条 Fusion 输入都只有两张图，即封面和一个视频帧；40/40 都只有不超过一张真实时间帧。Fusion 与 Text 向量平均 cosine 为 `0.918596`，但 Fusion 命中降至 `45.96%`，现有视觉 payload 不是 hook/middle/payoff 证据。
3. **全局参考池缺少账号语境。** 20 条参考高低各 10 条、覆盖 13 个账号，但只有 50% 留出样本能找到同账号参考，Text Top1 同账号率仅 `12.5%`。账号内相对互动标签被跨账号检索稀释。
4. **Rerank 更容易匹配内容相似，而非结果相似。** Rerank Top1 的内容分类一致率为 `65%`，表现标签一致率为 `60%`；Text/Fusion 的表现标签 Top1 一致率也只有 `57.14%/42.86%`。参考池标签虽平衡，但检索目标仍不足以表达传播结果。
5. **不是标题近重复泄漏。** Text/Fusion Top match 的平均最大标题重叠只有 `2.34%`，精确标题重复为 0；失败不能归因于标题复制或同源泄漏。

## 5. 决策

```text
decision=keep_v2_4_and_redesign_evidence_before_new_holdout
raise_cloud_weight=false
expand_judge=false
production_weight_changed=false
writes_manual_gold=false
network_request_count=0
effective_cost_cny=0
```

后续顺序固定为：先为新样本生成真实 hook/middle/payoff 三帧，再建立按账号或节目分层的高低互动参考池，完成缓存-only 检索纯度检查后，才冻结新的未见账号/节目 benchmark。不得继续在当前 60 对上选择权重，也不得用该后验诊断声称新的泛化收益。

## 6. 复现

```bash
dso bailian-vector-attribution \
  --benchmark-id dso-multimodal-vector-value-20260719-r1
```

原始报告位于：

```text
outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/holdout-failure-attribution-latest.json
```
