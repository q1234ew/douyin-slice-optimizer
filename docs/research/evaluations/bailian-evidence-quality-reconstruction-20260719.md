# V1 Beta-D-12C1：证据质量重构

- 日期：2026-07-19
- 目标：G3 本地/公网模型协同
- 状态：`research_only / expand_stratified_high_low_reference_coverage`

## 1. 实验范围

本轮继续使用冻结 benchmark `dso-multimodal-vector-value-20260719-r1` 和 D12-B `pair-041..060`，先校验配置、盲预测与揭盲评估 SHA，再在本地原视频上生成 15 秒 hook / middle / payoff 三窗口代表帧。帧缓存同步到 ECS 后，只读取已有 Text 向量做分层高低互动参考对照，不构造 Provider Runtime，不发送公网请求。

- D12-C1 report SHA-256：`d41dbffe9d2b2fe6f9fd0c579869038b448bca9c33ddd0217a0a3fc82c6f7339`
- 网络请求：`0`
- 费用：`0 CNY`
- 生产排序、人工 Gold、审核、导出和发布：均未修改

## 2. 三窗口证据包

40/40 条留出样本成功生成证据包，共 120 张真实时间帧，hook / middle / payoff 三时点覆盖率为 100%。派生缓存约 8.5 MB；短视频即使三个窗口覆盖同一完整片段，也分别在前、中、后位置抽取不同帧。每个证据包保存源视频 SHA、窗口时间、帧 SHA 和新的 `dso-bailian-vector-input.d12c1` source hash，不覆盖旧 Fusion 输入。

当前状态为 `ready_for_embedding_rebuild`，但本轮没有生成 `fusion_d12c1` 云向量。

## 3. 参考池覆盖

ECS 当前只有 30 条参考具备 Text/Fusion 缓存，其中 high 20、low 10。40 条留出查询覆盖 10 个账号：

- 任意同账号参考覆盖：100%
- 同账号 high/low 两侧同时齐备：20%
- 同账号、同节目或同素材形态的双侧语境覆盖：70%
- 将冻结 manifest 剩余参考全部补齐后的同账号双侧覆盖上限：65%
- 当前 manifest 无法形成同账号 high/low 对照的账号：`dk_voice_teacher`、`hukan_music`、`jason_teacher`、`sixuweilive`

现有 manifest 中另有 9 条未缓存参考可以把可恢复账号补到 65%，但无法达到 80% 同账号门槛。要跨过该门槛必须新建参考池版本，不能原地修改当前冻结 manifest。

## 4. 缓存对照

分层策略要求 high/low 两侧共享同一语境层级；只有两侧同时存在同账号参考时才使用账号层，否则整组降级到节目、素材形态或全局，避免一侧账号、一侧全局造成方向失衡。

| 策略 | 平衡命中 | 原始命中 |
| --- | ---: | ---: |
| 全局 Text cosine | 57.07% | 55.00% |
| 分层双侧 Text cosine | 51.52% | 50.00% |

分层策略较全局低 `5.55pp`。该结果说明当前语境覆盖和参考标签方向仍不足，不能把账号、节目或素材形态相似直接当成传播结果证据。该对照是当前留出集上的诊断，不用于搜索权重或声明泛化收益。

## 5. 决策

```text
decision=expand_stratified_high_low_reference_coverage
three_window_evidence_ready=true
context_reference_coverage_at_least_80pct=false
d12c1_fusion_vector_coverage_at_least_80pct=false
cached_stratified_not_worse_than_global=false
production_weight_changed=false
network_request_count=0
effective_cost_cny=0
```

下一步先为 4 个单侧账号补低互动对照，并为可恢复账号补 9 条缺失参考缓存；随后冻结新的参考池版本。只有参考覆盖通过后，才显式构建 `fusion_d12c1` 并在新的未见账号/节目留出集验证，不继续在当前 20 对上调权。

## 6. 复现

```bash
dso bailian-evidence-quality \
  --benchmark-id dso-multimodal-vector-value-20260719-r1 \
  --scope holdout \
  --limit 40
```

完整 JSON 报告：

```text
outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/evidence-quality-reconstruction-latest.json
```
