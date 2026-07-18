# 模型与算法雷达

更新时间：2026-07-18

目标：持续记录可能改善切片召回、短片排序、流量质量、转发、关注转化或推理成本的新模型与算法，并主动向用户报告。

准入与汇报规则见 [product-goals.md](./product-goals.md)。本文件是动态登记表，不代表所有条目都应实现或进入生产。

## 状态定义

| 状态 | 含义 |
| --- | --- |
| `watch` | 值得关注，但证据、资源或适用场景不足 |
| `validate` | 已有明确假设，准备在冻结数据集验证 |
| `research_only` | 已接入研究链路，但不得影响生产排序或人工 Gold |
| `shadow` | 与生产基线并行运行并记录差异，不直接控制结果 |
| `candidate` | 已过离线门禁，等待小范围上线或人工批准 |
| `adopted` | 已进入明确的默认或按需生产链路 |
| `rejected` | 当前证据不支持采用，保留原因避免重复试错 |

## 当前基线条目

| 方向 | 模型/算法 | 当前状态 | 已知价值 | 成本与约束 | 下一门禁 |
| --- | --- | --- | --- | --- | --- |
| 高质量中文 ASR | Qwen3-ASR-1.7B + ForcedAligner-0.6B | `shadow` | 冻结节目锚点 Recall `7/10`，高于 Whisper small `5/10`、base `3/10`；7 个命中锚点的字符时间戳中位误差 `0.12s`；60 秒低能量切界、空 context 和异常块 30 秒恢复已完成 7506.73 秒复测，音频覆盖 100%、RTF `0.050345`、历史两个静默漏段重新命中 | 精确锚点较固定 60 秒基线没有提升；2 个恢复块中 1 个为单字语气词假恢复；英文空格、专名和代词仍有错误；约 8.9GB 显存且与 Omni 串行 | 先增加 VAD/低信息恢复门控，再扩到 3–5 个节目人工逐字稿集，并用下游人工切片 Gold 验证 Recall@K；完整复测见 [qwen3-asr-recovery-full-program-retest-20260718.md](./qwen3-asr-recovery-full-program-retest-20260718.md) |
| 本地多模态复排 | Qwen2.5-Omni-7B GPTQ-Int4 | `shadow` | 可为 Top 候选补充 hook/middle/payoff 语义证据 | 只适合短窗口、batch=1；与其他 GPU 模型存在显存互斥 | 在冻结 Gold 上证明对 Top-K 排序有稳定增益且严重错判不升高 |
| 视觉窗口表示 | Qwen3-VL-Embedding-2B | `research_only` | 为无音轨、舞台、后台和彩排素材提供视觉相似与原型证据 | 真实向量覆盖率和服务切模需要门控 | 完成覆盖率门禁和 leave-one-sample-out 累计评测 |
| G3 公网模型治理底座 | `public_model_provider.v1` + Fake Provider + Shadow Evaluator | `research_only` | 已验证默认关闭、显式数据许可、请求/批次/日预算、确定性缓存、独立台账和本地回退；重复 Smoke 可命中缓存，网络请求和费用均为 0 | Fake 只验证 contract 与故障边界，不代表任何真实模型质量；不写生产排序、人工 Gold 或发布状态 | 用首个真实 Adapter 在冻结集验证 schema、失败降级、质量、P50/P95、费用、缓存率和隐私许可，再决定是否进入 `shadow` |
| 公网文本/代表帧增强 | 阿里云百炼：Qwen3.5-Flash、Qwen3.6-Flash、Qwen3-VL-Flash | `validate` | OpenAI 兼容、中国内地地域、固定模型快照、低按量价格、子业务空间模型/IP 白名单和 QPM/TPM；官方称调用数据不用于训练 | 尚未实现真实 Adapter、未配置密钥、未产生真实质量与账单证据；首版禁止完整节目/音频上传 | 按 [真实 Provider 调研](./public-model-provider-research-20260718.md) 实现 Adapter，先做 3 条脱敏连通性 Smoke，再跑冻结 100 条文本 Shadow |
| 公网长上下文/视频对照 | Kimi K2.6 / K3 | `watch` | K2.6 支持 256K 与图片/视频，K3 支持 1M、视觉和 JSON Schema，可作为百炼之外的独立对照 | K2.6 缓存未命中输入/输出为 6.5/27 元每百万 Token，K3 为 20/100；开放平台协议含输入输出用于服务优化的授权表述 | 先完成百炼冻结基线；企业数据条款未确认前只允许脱敏摘要，不上传完整业务媒体 |
| 其他大陆 Provider | 火山方舟 Doubao、腾讯 TokenHub/混元 | `watch` | 方舟具备图片/视频/音频和 Responses API；混元有低价文本、视觉与视频模型 | 方舟数据授权范围需确认；腾讯旧混元平台正在迁移并计划停服，API Host/模型/价格仍在变化 | 方舟先确认账号数据授权和退出机制；腾讯待 TokenHub 稳定后重新核对，不作为首接 |
| 可学习排序 | LightGBM / LambdaRank / 小型多任务 reranker | `watch` | 可能替代部分手工权重，提高账号级和多目标排序 | 受弱标签、曝光偏差、样本泄漏和标签漂移影响 | 先稳定时间切分、曝光归一化标签和强规则基线 |
| 生产排序基线 | `production_ranking_policy.v1`：`current_rules/final_score` | `adopted` | 为 G1/G2 提供同一默认排序，研究分只有显式 scope 才可见；冻结参考 lift 为 `1.4905` | 当前只有互动热度代理，尚无真实 G1 发布回流与 G2 Recall Gold | 保持默认；研究策略需同时满足绝对门槛、强基线 `+0.03`、无关键指标回退和账号级门槛，再冻结新 manifest 后显式晋级 |

## 新条目模板

```markdown
### YYYY-MM-DD：方案名称

- 对齐目标：G1 / G2 / G3
- 状态：watch / validate / research_only / shadow / candidate / adopted / rejected
- 当前问题与基线：
- 新模型或算法：
- 证据来源：论文、官方文档、本地实验或冻结 benchmark
- 预期改善指标：
- 计算/API/人工成本：
- 数据、合规与失败风险：
- 最小验证方案：
- 建议：立即验证 / 进入观察 / 暂不采用
- 最近一次向用户汇报：
```

## 维护规则

- 新发现先登记再实现；仅有营销描述或排行榜分数的方案保持 `watch`。
- 每次状态变化记录证据、benchmark ID、模型/提示词版本和日期。
- 进入 `candidate` 前必须有冻结基线和成本报告；进入 `adopted` 前必须有降级与回滚方案。
- 被拒绝的方案保留失败原因，除非模型版本、数据或资源条件发生实质变化，否则不重复投入。
