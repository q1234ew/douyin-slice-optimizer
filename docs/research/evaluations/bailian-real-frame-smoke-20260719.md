# 百炼真实研究帧 Smoke 与盲裁修正

- 日期：2026-07-19
- 目标：G3 本地/公网模型协同
- 状态：`research_only`

## 1. 冻结输入

- Benchmark：`dso-multimodal-vector-value-20260719-r1`
- Manifest SHA-256：`f32a6699ffc2cc7554ea6e2fa9a0550afc7e92a91cd2c46585beb130544a1510`
- v2.4 基线侧车：`benchmarks/dso-multimodal-vector-value-20260719-r1.baseline.json`
- 侧车 SHA-256：`a2dd4087a688515fb6dadf5a43c0c91668d7f0b6e9ef092e7083996ffb5a54ec`
- 侧车覆盖：60/60 个 pair 有冻结 `research_ranker_v2_4` 选择。

真实输入仅包含 manifest 内结构化摘要和本地代表 JPEG 帧，不包含完整视频。用户已明确授权该研究批次；ECS 记录授权依据、脱敏策略、上传等级和阿里云政策引用。阿里云公开政策没有固定天数，因此使用 `provider_minimum_necessary` 语义并保持 `retention_days_known=false`，没有伪填 `0`。

## 2. 执行结果

### 2.1 两条真实 Fusion 探针

批次 `ecs-bailian-real-fusion-probe-20260719`：2 条样本生成 2 个 Text 和 2 个 Fusion 向量，4/4 成功，维度均为 2560；4 次真实网络请求，usage 估算 `0.0040668 CNY`。

### 2.2 十条完整 Smoke

批次 `ecs-bailian-real-smoke-20260719`：

- 10 条评测样本和 20 条参考样本生成 60 个 Text/Fusion 向量，60/60 成功。
- Rerank 10/10 成功。
- Plus/Flash Judge 各 5/5 成功。
- 该批 usage 估算合计 `0.1039723 CNY`，无 schema 或网络错误。

该批随后发现 ECS 缺少冻结 v2.4 pair 报告，5 个 `v2_4_choice` 均为 `unknown`。因此它只证明真实 API、代表帧、缓存、usage 和 schema 可运行，不能作为分歧质量证据。

### 2.3 基线恢复与盲裁 v2

工程修正：

1. 增加与 manifest SHA 绑定的 v2.4 冻结侧车。
2. 基线缺失、不兼容或不完整时不生成分歧队列，Judge 返回 `not_ready/baseline_missing`，网络请求为 0。
3. 只把 `v2.4_choice != cloud_choice` 的真实选择分歧送入 Judge。
4. `dso-bailian-pairwise-input.v2` 不向 Judge 暴露 v2.4/cloud 选择或分差，只提供候选摘要、左右各最多一张代表帧和中性评测口径。

最终批次 `ecs-bailian-blind-judge-v2-20260719`：

- 60 个向量全部复用，Embedding 网络请求 0。
- 10 个 Rerank 全部命中缓存，网络请求 0。
- 5 个可比 pair 中只有 2 个真实选择分歧。
- Plus 与 Flash 各完成 2 次盲裁，共 4 次网络请求，usage 估算 `0.0115072 CNY`。
- Plus 单次延迟 `5.446–5.794s`，Flash 为 `1.946–2.001s`。
- 两个模型在 2/2 pair 上均互相反选，一致率为 `0.0`；没有人工盲审 Gold，不能判断谁正确。

### 2.4 客观互动代理门禁

冻结侧车的 `proxy_choice` 来自账号内归一化的抓取时点可见互动结果，只在云端排序完成后用于评分，不进入 Embedding、Rerank 或 Judge 输入。当前数据没有播放量、曝光或关注转化，因此该指标是客观互动代理，不是流量 Gold。

实验扩展依次暴露三处评测问题：

1. 首次 `rerank --limit 10` 使用散列样本而未保证完整 pair，得到 0 个可比 pair，并产生 2 次未命中缓存的 Rerank 请求，usage 估算 `0.0037415 CNY`。现已改为按完整 pair 选择。
2. 首批 20 条参考样本全部为 `high`，没有 `low`，导致云分被压缩且风险项失效。未平衡的 20 对结果无效；相关 60 次 Embedding 与 30 次 Rerank usage 估算 `0.1117166 CNY`。现强制高/低各半，缺任一侧直接拒绝评测；补平衡参考和重排 usage 估算 `0.0901307 CNY`。
3. 审核使用的 `abs(delta)<0.5 -> tie` 被误用于只有左右高低侧的客观标签，40 对中弃权 16 对。客观门禁现按分差正负二选一，审核 `tie` 独立保留；由于结果侧为左 27、右 13，主指标改为两侧 macro-average，原始准确率和多数类基线仍报告。

正式批次 `ecs-outcome-proxy-balanced-gate40-20260719` 补齐到 40 对，新增 76 次 Embedding 和 40 次 Rerank，usage 估算 `0.1431662 CNY`。最终 `ecs-outcome-proxy-score-sign40-20260719` 全部命中缓存，网络请求和费用为 0：

- 云端原始命中 `25/40=62.5%`，v2.4 为 `28/40=70%`，多数类基线为 `67.5%`。
- 云端结果侧平衡准确率 `66.24%`，v2.4 为 `69.80%`，差 `-3.56pp`。
- 云分与归一化互动的 Pearson 相关仅 `0.0579`；高/低样本云分均值约 `50.441/50.324`，当前检索/重排分缺少稳定性能区分度。
- 门禁要求相对 v2.4 `+5pp`，结果为 `ready/passed=false`，不晋级。

## 3. 结论

- **工程门禁通过**：真实结构化摘要、代表帧、Embedding、Rerank、双 Judge、缓存、预算与台账链路可运行。
- **质量门禁未通过**：40 对平衡准确率云端 `66.24%`、v2.4 `69.80%`，未达到相对 `+5pp`；双 Judge 在首批 2 个真实分歧上互相反选。云端信号并非完全随机，但没有证明替换或提升 v2.4 的价值。
- **不扩大到 60 对或 240 条全量**：固定现有 110/240 云向量与 40 对结果，只做离线 embedding/rerank/text/fusion 消融和保守融合。人工偏好只作次级诊断。
- 结果继续为 `research_only`，没有修改生产排序、人工 Gold、审核状态、导出或发布。

复现入口：

```bash
dso bailian-vector-status --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-run --stage rerank --limit 80 --top-n 10 \
  --batch-id ecs-outcome-proxy-score-sign40-20260719
```
