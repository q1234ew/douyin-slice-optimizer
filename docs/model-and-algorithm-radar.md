# 模型与算法雷达

更新时间：2026-07-20

目标：持续记录可能改善切片召回、短片排序、流量质量、转发、关注转化或推理成本的新模型与算法，并主动向用户报告。

准入与汇报规则见 [product-goals.md](product-goals.md)。本文件是动态登记表，不代表所有条目都应实现或进入生产。

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
| 云端多模态检索与重排 | 百炼 `qwen3-vl-embedding` + `qwen3-vl-rerank` | `research_only` | 150/240 条已有 Text/Fusion 2560 维云向量。D12-A 40 对同集搜索曾观察到 85/15 融合较 v2.4 `+11.54pp`；D12-B 独立 20 对增量为 0。D12-C0 零网络归因进一步确认 Cloud 新增纠错 `0`、误导 `3`、共同错误 `5` | 留出集 Text/Rerank/Cloud/Fusion 平衡命中为 `60.61%/66.16%/60.61%/45.96%`，均弱于 v2.4 `76.26%`。40/40 条 Fusion 只有封面加单帧；同账号参考覆盖 `50%`、Text Top1 同账号率 `12.5%`。质量而非成本成为主阻塞 | 保持 v2.4；先补真实 hook/middle/payoff 帧和账号/节目分层参考池，再冻结未见集。停止在同一 60 对调权、补全量或扩 Judge；详见 [D12-C0](research/evaluations/bailian-holdout-failure-attribution-20260719.md) |
| 云端多模态判别层 | 百炼 `qwen3.7-plus-2026-05-26` 主裁判 + `qwen3.6-flash-2026-04-16` 成本 Challenger | `research_only` | `pairwise-input.v2` 已实现真实 choice disagreement 准入和盲于策略选择/分差的代表帧裁判；首批 2 个真实分歧上两模型互相反选。D12-B 不调用 Judge，并证明固定云融合在独立 20 对上无增益 | 4 次历史盲裁 usage 估算 `0.0115072 CNY`；Plus 延迟 `5.446–5.794s`，Flash `1.946–2.001s`。检索/重排前级没有独立增益时，继续增加 Judge 只会增加成本和不稳定性 | 暂停 Judge。只有新的未见账号/节目集先证明检索/重排跨账号增益，才允许 Judge 评估真实分歧 |
| 完整短片时序视觉诊断 | 百炼 `qwen3.7-plus-2026-05-26` `complete_short_clip` profile | `research_only` | 已在 `pair-048` 一条 21.767 秒高表现音乐综艺样本跑通完整时域 H.264 无音轨输入，输出 5 段时间轴、人物/造型/字幕手势和镜头短板；技术可行但不是盲对照 | 1 次请求、0 重试，`5810/836` 输入/输出 Token、`17.048s`、usage 估算 `0.018308 CNY`。Qwen 视觉模型不理解音轨，输出仍出现节奏推断；单样本不能证明排序或流量增益 | 保持 `research_only`；若继续，用 10 条冻结诊断集做代表帧 vs 完整视频盲事实核验，比较严重错判、弃权、P95 和每条费用；未通过前不扩全量，详见 [单样本报告](research/evaluations/qwen37-complete-short-clip-shadow-20260719.md) |
| 完整短片全模态传播特征 | 百炼 `qwen3.5-omni-plus-2026-03-15` + `bailian_propagation_feature_batch.v1` | `research_only` | 30 对/60 条、8 账号的完整音视频事实特征账号隔离验证达到 60/60 schema、音频、视觉和时间线覆盖。固定 85/15 融合成对命中由 v2.4 的 `66.67%` 提至 `73.33%`，无新增误导 | 61 次请求、usage 估算 `6.654878 CNY`，延迟中位数/P95 `23.296/29.061s`。提升只来自同一账号 2 对；4 个 ready 账号仅 1 个改善，Top15 命中不变，纯 Omni `63.33%` 弱于 v2.4，符号检验 `p=0.5`、账号簇区间含 0。分享率、关注转化和观看质量仍为 0 覆盖 | `keep_v2_4`。Omni 仅保留为低分差分歧诊断，不在同一 60 条调权。新验证必须使用未见 pair 并让至少 3 个账号具备足够样本；优先接入真实多目标指标。详见 [账号隔离验证](research/evaluations/qwen35-omni-propagation-account-holdout-20260720.md) |
| 高质量中文 ASR | Qwen3-ASR-1.7B + ForcedAligner-0.6B | `adopted` | 已按用户决策成为音乐综艺 auto 路由生产主后端，Whisper.cpp/faster-whisper 自动回退；独立 `qwen3_asr_shadow.v1` 保留双模型对照。两个完整节目、22 个冻结局部字幕锚点合计命中 `15/22`，高于 Whisper small `7/22`、base `4/22`；第二节目 RTF `0.054153`、4 个歌词锚点全命中；局域网 canary 已通过 Scheduler 完成 ASR 主路由和 Omni 后 ASR 回切 | 采用不等于通用质量已证实：局部字幕锚点不是完整逐字稿；第一节目存在单字语气词假恢复；约 8.9GB 显存且与 Omni 串行；当前 canary 仅 19.1 秒，batch 2/4 未验证 | 继续增加 VAD/低信息恢复门控和 3–5 个节目完整人工逐字稿；在长节目真实混合 workload 复验输出等价与 RTF；若严重漏段或失败率越门槛，可通过 `DSO_QWEN3_ASR_PRIMARY=0` 或显式 Whisper 后端即时回滚 |
| 演唱段 ASR 路由 | Whisper small 定向关闭 Silero VAD | `validate` | 第二节目 12 个冻结小窗中，无提示词 small 关闭 VAD 后精确命中由 `3/12` 提升至 `4/12`、近似命中由 `6/12` 提升至 `8/12`，找回部分被 VAD 抑制的歌词 | 全片关闭 VAD 可能增加音乐幻觉和计算量；当前只是 12 个窗口消融，仍弱于 Qwen `8/12` 精确命中 | 基于音乐能量、切镜和字幕 OCR 只路由演唱高价值窗口，冻结比较歌词召回、幻觉率、RTF 与候选 Recall@K；未通过前不改默认 Whisper 配置 |
| 本地多模态复排 | Qwen2.5-Omni-7B GPTQ-Int4 | `shadow` | 可为 Top 候选补充 hook/middle/payoff 语义证据 | 只适合短窗口、batch=1；与其他 GPU 模型存在显存互斥 | 在冻结 Gold 上证明对 Top-K 排序有稳定增益且严重错判不升高 |
| 文本/视觉历史证据检索 | Qwen3-VL-Embedding-2B | `research_only` | 已冻结 `dso-multimodal-vector-value-20260719-r1`：60 组短片盲审 + 120 条不重叠高/低参考池，并与 current/v2.4/text/visual/fusion 并排对照；T0 低覆盖历史代理中 text 仅比 v2.4 `+1.66pp`，尚无人工证据 | 局域网不可达后 459 个缺失 item 的任务已取消，21 个缓存保留；历史互动不是曝光/播放，人工盲审另有运营成本 | 保留为本地历史基线，不再约束云端模型选择或向量维度；只有云端方案失败或需要复现旧结果时才恢复该任务 |
| 单卡资源感知调度 | `model_scheduler.v1`：持久队列、GPU lease/fencing、亲和/公平调度、逐 item staged result | `validate` | Phase 0–3 batch-1 基线与局域网 canary 已部署：Agent 401/白名单/stale fencing 通过，ASR→Omni 3 窗→ASR 的 5 个 attempt 全部成功，Embedding 返回 2048 维真实向量；launchd Worker/8127 Web 常驻，token 存 Keychain；合成 workload 仍为 6 job/44 item、模拟切模 `36 -> 3` | 局域网显式启用，其他环境默认关闭；19.1 秒 canary 不代表长节目/批量 GPU 空闲、OOM 或质量；batch 2/4 和冻结真实三模型混合 workload 未完成；SQLite 面向单应用主机 | 扩展为长节目、10 个 Omni 任务与批量 Embedding 的冻结混合 workload，验证输出等价、显存峰值和 GPU 空闲；真实切模至少减少 60%、GPU 空闲间隙至少减少 30% 且质量不变后才能晋级；详见 [调度设计](architecture/model-scheduling-architecture.md) |
| G3 公网模型治理底座 | `public_model_provider/runner/ledger.v2` + Fake Provider + Shadow Evaluator | `research_only` | 已验证许可/保留政策匹配、缓存前置、请求/批次/单日预算 reserve/settle/release、未知账单保守占用、逐网络尝试台账和本地回退；ECS 当前仅在已授权 Shadow 环境显式开启，三层预算为 `2/50/50 CNY`，配置文件保持 `0600 root:root` | 治理通过不代表模型质量；赠送 Token 不会自动冲抵本地公开价估算，成功调用仍需与控制台最终账单校准；50 元是硬上限而非消费目标，不写生产排序、人工 Gold 或发布状态 | 保持显式批次调用和缓存优先；每轮先做离线消融/预检，再在质量假设明确时使用赠送额度，定期核对控制台账单与本地台账 |
| 云端缓存信号归因 | `bailian_cached_signal_ablation.v1`：Text/Fusion cosine + cached Rerank + v2.4 低权重融合 | `research_only` | 110/240 Text/Fusion 缓存、40 个可比 pair 上，最佳纯云为 `75.64%`，较同子集 v2.4 `+5.84pp`；最佳观察融合为 `81.34%`，较 v2.4 `+11.54pp`，网络请求和费用均为 0 | 同一 40 对同时用于配置搜索与评估；融合增量 bootstrap 95% 区间为 `0–23.08pp`；样本量充足账号仅 2 个改善。后续 D12-B 独立 20 对增量为 0，已证实同集观察值没有泛化 | 归因阶段已完成并由 D12-B 复验。保持 `keep_v2_4`，不在同一 60 对继续搜索权重或恢复 Judge |
| 三窗口证据与分层参考 | `bailian_evidence_quality_reconstruction.v1` | `research_only` | D12-B 40 条留出样本已生成 120 张 hook/middle/payoff 真实帧，三时点覆盖 100%，0 请求、0 元。30 条缓存参考下，同账号 high/low 双侧覆盖 20%，账号/节目/素材双侧语境覆盖 70% | 当前冻结 manifest 的同账号双侧覆盖上限只有 65%，4 个账号缺低互动侧。分层 Text 平衡命中 `51.52%`，较全局 Text `57.07%` 低 `5.55pp`；账号/类别相似不能直接视为传播结果证据 | 保持 v2.4；先冻结包含双侧对照的新参考池并补 9 条可恢复参考缓存，再显式构建 `fusion_d12c1`。未通过参考门禁前不花费云调用、不冻结新留出结论；详见 [D12-C1](research/evaluations/bailian-evidence-quality-reconstruction-20260719.md) |
| 公网文本/代表帧增强 | `AliyunBailianProvider`：Chat、Embedding、Rerank、Pairwise Judge | `validate` | 已实现北京业务空间 Host、固定白名单、按能力独立 URL/参数/schema、最多三 JPEG、错误重试、实际 usage/Provider request ID 和 MockTransport contract；真实 Chat、Embedding、Rerank 与小批 Judge 已写入 usage 台账 | D12-B 独立留出未证明云向量增益，厂商公开页未给固定保留天数；标准 profile 继续禁止视频，完整短片只能走另一个显式授权、严格有界的研究 profile | 保持 `validate`；先修复证据目标和多帧/参考池质量，再冻结未见集；任何新增能力都不自动改生产排序 |
| 公网独立 Provider 对照 | Kimi K2.6 | `watch` | 256K 上下文、图片/视频、JSON Mode 和自动缓存，可在百炼基线后提供厂商独立性对照 | 缓存未命中输入/输出为 6.5/27 元每百万 Token；协议同样含输入输出用于服务优化的授权，固定保留期未知 | 先完成百炼冻结基线并确认企业数据条款；获准后只用同一脱敏冻结集做对照，不上传完整业务媒体 |
| 公网长上下文/视觉 Challenger | Kimi K3 | `watch` | 1M 上下文、图片/视频、自动缓存和 strict JSON Schema；适合本地与百炼均高分歧的少量疑难样本 | 始终 `reasoning_effort=max`，缓存未命中输入/输出为 20/100 元每百万 Token；只列滚动 `kimi-k3`，无 dated snapshot；协议含输入输出用于服务优化的授权，固定保留期未知 | 先按 [K3 专项规格](research/providers/kimi-k3-provider-research-20260718.md) 明确企业数据条款和版本漂移策略，再做无密钥 mock；若获准，3 条合成 Smoke 后只验证 10 条困难样本，不上传完整媒体、不做全量默认 |
| 其他大陆 Provider | 火山方舟 Doubao、腾讯 TokenHub/混元 | `watch` | 方舟具备图片/视频/音频和 Responses API；混元有低价文本、视觉与视频模型 | 方舟数据授权范围需确认；腾讯旧混元平台正在迁移并计划停服，API Host/模型/价格仍在变化 | 方舟先确认账号数据授权和退出机制；腾讯待 TokenHub 稳定后重新核对，不作为首接 |
| 可学习排序 | Pairwise Logistic / Target Encoding / LightGBM LambdaRank | Pairwise `rejected`；Target Encoding `research_only`；路线 `validate`、数据门禁 `not_ready` | 阶段 1 已冻结 10,853 条 `interaction_heat_labels.v3` r3。Pairwise r3 的账号内时间 test NDCG@10 `0.434468`、整账号 `0.424485`。正式纯 Python Target Encoding r2 只看 train/validation：账号内 validation `0.669155` 对随机 `0.583703`，Top-10 heat lift `+0.053093`；整账号 validation `0.304649` 对随机 `0.242408`，lift `+0.048263`。r2 使用 train-only 平滑、OOF、协议隔离和显式 fallback，标题关闭；模型/预测独立复跑一致，网络和费用为 0。新增 `interaction_heat_holdout_readiness.v1` 对 pinned r3 和真实数据库检查后，新前向候选与新账号均为 0 | 没有曝光分母；媒体 SHA 仅覆盖 630/10,853；整账号 validation 只有 3 个账号、严重误选率 `33.3333%`，且低于 Pairwise 整账号 validation `0.4238`；整账号 train OOF `0.437350` 还略低于随机 `0.448042`。当前 test 已查看，且目前没有晚于冻结 cutoff 的 provenance 合格数据，无法形成新盲 holdout | Pairwise 保留为负向基线，Target Encoding 保留为正式研究基线。readiness 默认要求前向至少 1,000 条/5 账号/7 天及 3 个新账号各 100 条；门禁通过前不安装 LightGBM、不运行 LambdaRank、不重用旧 test。通过后再冻结新 holdout，做标题消融与本地 LambdaRank/小型 GBDT，并报告账号宏 NDCG@30、Top-K heat lift、严重错判及账号改善/回退 |
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
