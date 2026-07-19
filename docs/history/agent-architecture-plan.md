# 架构诊断与成熟产品迭代计划

生成日期：2026-06-25
角色：架构及规划师 Agent
范围：`README.md`、`pyproject.toml`、`docs/architecture.md`、`docs/history/platform-v0.3-plan.md`、本地研究文档、`src/dso` 模块结构。

## 1. 当前产品判断

Douyin Slice Optimizer 当前已经不是纯算法 demo，而是一个本地优先的音乐综艺短视频切片工作台雏形。它的核心价值不是承诺爆款，也不是复刻平台推荐系统，而是帮助运营把授权长视频更快变成可审核、可导出、可复盘的短视频候选。

当前产品目标可以表述为：

```text
授权长视频
  -> 本地 ASR / 音频特征 / 人工校正
  -> 音乐综艺候选切片
  -> 规则评分 / 质量哨兵 / 推荐链路模拟
  -> 人工审核 / 9:16 导出 / 手动发布
  -> CSV 指标回流
  -> reward_proxy / training_samples / account_baselines
  -> 下一轮切片策略校准
```

现有代码已经覆盖主链路：

| 层 | 已有模块 | 当前状态 |
| --- | --- | --- |
| CLI/API | `cli.py`, `api/main.py`, `api/dashboard.py` | 命令与 Web 工作台可用，但 Dashboard 是单文件大模块 |
| 数据层 | `db/session.py` | SQLite schema 已覆盖候选、评分、授权、导出、指标快照、训练样本、账号基线 |
| 媒体层 | `media/ingest.py`, `media/ffmpeg.py` | 本地导入、探测、音频提取、抽帧、9:16 导出 |
| ASR/音频 | `features/asr.py`, `features/whisper_cpp.py`, `features/audio.py` | whisper.cpp/faster-whisper/sidecar/fallback，已有缓存、热词、VAD 元数据、广告口播标记 |
| 候选生成 | `segments/generator.py` | 已强领域化，支持节目上下文、音乐爆点、现场反应、广告口播降权 |
| 评分/风险 | `scoring/scorer.py`, `scoring/rights.py` | 音乐综艺规则评分、标题/封面建议、trusted sample/strict rights |
| 质量门 | `quality/insights.py` | 只读质量哨兵已成型，但尚未成为导出/审核 contract |
| 反馈闭环 | `feedback/importer.py`, `feedback/reward.py` | CSV -> metrics -> snapshots -> training_samples -> baselines 已落地 |
| 模拟 | `simulation/recommender.py` | 六阶段推荐链路模拟，更多是解释器/先验，不是可学习模型 |
| 人工校正 | `corrections/editor.py` | 可修表演区间、候选片段并触发重评分 |
| 测试 | `tests/test_core.py` | 覆盖核心纯函数和主流程，适合继续小步演进 |

## 2. 用户闭环与系统边界

### 2.1 用户闭环

目标用户是音乐综艺内容运营、剪辑/审核协作人员或个人创作者。成熟产品的主要任务闭环应是：

1. 导入已授权节目视频，确认账号、节目、素材来源和授权模式。
2. 运行环境诊断与 ASR 提取，看到 ASR 后端、模型、VAD、缓存命中和质量风险。
3. 生成候选切片，按“节目上下文 -> 音乐爆点 -> 现场反应”的闭环结构扫读。
4. 查看质量哨兵、推荐模拟、分项评分和风险说明，选择人工复核队列。
5. 手动修正候选边界、表演区间、歌曲信息、标题、封面时间和风险状态。
6. 导出少量 9:16 预览，人工发布到平台。
7. 导入 6h/24h/72h/7d/30d 数据，形成 `metric_snapshots` 和 `training_samples`。
8. 用账号基线、切片类型、时长、发布时间和结构表现，校准下一轮候选生成与排序。

### 2.2 系统边界

当前系统边界应继续保持保守：

- 本地优先：SQLite、本地文件系统、FFmpeg、本地 ASR。
- 创作者侧：只能使用本账号、自有/授权账号、许可数据和人工标注数据。
- 手动发布：系统只导出和解释，不自动发布、不自动互动。
- 小团队工作台：当前没有用户、权限、审计和队列调度，不适合直接多人生产环境。
- 指标回流：目前依赖 CSV，不假设平台 API 或用户级数据可得。

### 2.3 风险边界

成熟产品必须把这些边界做成显式 UI/数据 contract：

- 版权/授权风险：音乐综艺同时涉及节目视听作品、歌曲词曲、表演者、录音录像、艺人肖像和平台授权范围。默认 `trusted_sample` 适合样本验证，生产模式必须能切到 `strict`。
- ASR 风险：whisper.cpp 速度路径可用，但音乐/静音/英文歌词场景会出现重复幻觉、广告口播混入和专名误识别；ASR 结果不能直接作为发布依据。
- 低原创风险：纯歌曲高潮截取、长段连续副歌和缺节目上下文的片段，应进入人工复核或降权。
- 数据合规风险：公开视频页面可见数据不等同于可批量抓取、商用训练。公开资料只能做人工研究和趋势先验。
- 指标偏差风险：播放量受曝光强烈影响；主标签应优先看 5 秒留存、平均观看比例、完播、复播、收藏/评论/分享/关注、负反馈和授权风险。
- 产品承诺风险：系统只能提升“可控质量和实验效率”，不能承诺流量、绕过平台规则或操控推荐。

## 3. 从 MVP 到可用工作台的架构缺口

### P0：必须先补，直接影响产品可用性

1. **质量门禁从报告升级为 contract**
   - 现状：`quality_insights()` 已能输出 health、issues、actions、watchlist，但导出只检查 rights。
   - 缺口：缺少统一的 `allow / review / block` 发布前决策，以及决策原因、版本和可测试阈值。
   - 建议：先做只读 `quality_gate`，不改变导出行为；稳定后再接入 export warning/block。

2. **Pipeline run / artifact manifest**
   - 现状：ASR 有缓存 key，其他步骤主要靠 DB 状态和文件路径。
   - 缺口：没有一次运行的步骤状态、输入版本、输出 artifact、错误、耗时、可重跑范围。
   - 建议：先用轻量 JSON manifest 或 DB 表记录 `video_id + step + artifact + version + status`。

3. **版本化特征 contract**
   - 现状：`POSTPROCESS_VERSION` 和 `training_samples.feature_version = v1.rules` 已有雏形。
   - 缺口：候选生成规则版本、评分规则版本、广告词表版本、质量门阈值版本没有随结果持久化。
   - 建议：把 `segmenter_version`、`scorer_version`、`quality_gate_version` 写入输出或解释，便于回测。

4. **生产/样本模式边界**
   - 现状：`DSO_RIGHTS_MODE=trusted_sample` 默认非常适合本地样本，但容易被误当生产规则。
   - 缺口：UI/API/导出结果没有足够强的“样本模式”提示。
   - 建议：在 doctor、质量哨兵、导出结果中显示 rights mode，并把 strict 模式路径写入工作台。

5. **反馈导入的可追责性**
   - 现状：CSV 中无法解析到候选的行仍可能被导入到 metrics，但不会形成训练样本。
   - 缺口：运营会以为数据已进入闭环；训练资产会 silently missing。
   - 建议：导入结果显式返回 `linked / unlinked / skipped`，未链接样本不进入 training。

6. **人工复核工作流**
   - 现状：候选和表演区间可 patch，重评分可触发。
   - 缺口：没有审核状态、审核人、复核原因、修改前后 diff、是否允许导出。
   - 建议：先在现有 `candidate_segments.status` 上收敛状态枚举：`candidate / review / approved / blocked / exported`。

### P1：让工作台从“能跑”变成“好用”

1. **Dashboard 拆分与状态合并**
   - 现状：`api/dashboard.py` 约 2200 行，UI 有视频、候选、质量、模拟、反馈、运行状态。
   - 缺口：质量、模拟、导出、反馈不是统一审核面板，运营需要来回切。
   - 建议：优先做候选详情的“质量旗标 + 模拟瓶颈 + 导出状态 + 反馈样本”合并视图。

2. **Golden sample 回归**
   - 现状：单元测试覆盖很多合成案例，但真实样本质量没有固定验收线。
   - 缺口：ASR/候选/评分每次调规则可能改变 Top 队列，没有 NDCG/Top 命中/闭环率守护。
   - 建议：固定一个脱敏样本或合成 transcript fixture，记录 Top10 闭环率、广告口播数、音频-only 数、health score。

3. **实验和版本管理**
   - 现状：`slice_variants` 和 `publishing_experiments` 有表，但版本差异、假设、发布时间更多停留在数据结构。
   - 缺口：没有“同一候选多个版本只改一个变量”的产品引导。
   - 建议：先支持手动创建 2-3 个 variant，并记录 `hypothesis`、`changed_variable`、`publish_window`。

4. **可观测性与错误恢复**
   - 现状：同步命令直接跑 FFmpeg/ASR，失败靠异常。
   - 缺口：长视频工作流需要知道当前卡在 ASR、候选、评分还是导出。
   - 建议：先不引入队列系统，使用 step status 和错误摘要支撑 UI 进度。

### P2：为可学习系统打地基

1. **Memory bank 最小实现**
   - 现状：`clip_embeddings` 表存在，但没有 embedding 生成、检索和失效策略。
   - 路线：先文本 embedding + cover/keyframe embedding + audio summary features，文件系统存向量，SQLite 记录模型和版本。

2. **历史相似与时间适配**
   - 现状：`history_match_score` 固定约 50，模拟器只按同账号同类型同长度查训练样本均值。
   - 路线：先做轻量 target-to-history：同账号历史切片按文本/类型/结构/时长相似，乘 recency 和 normalized_reward。

3. **Mini-InterestClock**
   - 现状：`account_baselines` 有 publish_hour，但没有平滑和推荐决策。
   - 路线：按 `account x slice_type x duration_bucket x hour` 做高斯平滑，输出小时候选和不确定性。

4. **轻量 ranker / backtest**
   - 现状：规则分可解释，但不能从 100+ 自有样本中学习。
   - 路线：100-300 条自有/授权发布样本后，训练 LightGBM/Logistic baseline；指标使用 NDCG@10、Top10 命中率、校准误差和人工排序一致率。

5. **抖音只读数据回流**
   - 现状：反馈主路径是 CSV，适合 MVP 验证，但无法自动获得账号下已发布视频的窗口指标，也容易丢失平台视频 ID 映射。
   - 路线：先在 V0.4 做平台视频映射、数据来源抽象和 fake client 字段映射；V0.5 再接真实 OAuth/OpenAPI，只做授权账号的视频列表和表现数据同步，不做自动发布。

## 4. 已核验的最新技术路线

本次可联网；我用公开主源核验了本地研究文档中的主线方向，但没有逐篇阅读全文，也没有把所有 B 级论文全部复核。可确认的路线如下：

1. **平台推荐公开信号仍支持当前闭环设计**
   - TikTok 官方帮助文档将推荐因素归为 user interactions、content information、user information，并明确互动、完整观看/跳过、时长等信号通常更重；也强调多样性和安全约束。
   - 对本项目的落地：不要优化单一播放量，继续把 5 秒留存、观看比例、完播、复播、互动、关注、负反馈和风险作为多目标代理。

2. **Douyin/Byte 主线论文仍指向“内容理解 + 历史序列 + 实时反馈 + 多目标约束”**
   - `Make It Long, Keep It Fast` 已更新到 2026-05 的 arXiv v3，核心是 STCA、RLB 和 10K 长历史。
   - `Monolith` 仍是实时反馈与在线训练架构的关键参考。
   - `Streaming VQ Retriever` 强调候选索引实时更新和 index balancing。
   - `Next-User Retrieval` 将冷启动 item 转成 next-user/lookalike 建模。
   - `Interest Clock` 已部署于 Douyin Music App，支持按小时平滑建模兴趣。
   - `LEMUR` 强调端到端多模态与 memory bank，已在 Douyin Search/广告场景验证。
   - `RankMixer` 代表后续排序模型从手工 feature crossing 走向 token mixing/统一特征交互。

3. **音乐方向新增信号值得纳入中期路线**
   - `MuChator` 是 2026-05 的 Douyin Music 论文，强调音乐知识、情境意图和个性化偏好对齐。
   - 对本项目的落地：音乐综艺切片不能只看“副歌/高音”，还应把歌曲知识、歌词情绪、节目语境、歌手故事和受众意图作为 feature group。

4. **ASR 路线与仓库当前方向一致**
   - whisper.cpp 官方 README 显示其支持 Apple Silicon Metal、Core ML、离线本地运行，并支持 `--vad` 与 Silero VAD。
   - 对本项目的落地：`fast=base + VAD` 做批量召回，`quality=small` 做发布前复核，是合理主线；`verify/premium=large-v3-turbo-q5_0` 只作为高价值候选二次转写，不直接替换 `quality` 默认。
   - 2026-06-26 sample 实测：`small` 在英文歌名/英文介绍和部分压力口播上更稳，`large-v3-turbo-q5_0` 在中文长口播、人名和节目叙事密集段更顺；因此后续优化重点是“按片段类型路由模型 + 保留人工复核”，而不是单模型升级。
   - 质量哨兵必须继续拦住重复幻觉、广告口播和弱上下文候选，并避免把自然英文歌手/英文采访误判为重复噪声。

5. **版权风险必须产品化**
   - 国家版权局公布的《中华人民共和国著作权法》明确列出音乐作品、视听作品和信息网络传播权，且视听作品中的音乐等可单独使用作品，作者可单独行使权利。
   - 对本项目的落地：`rights_clearance` 不是附属字段，应是导出/训练资格 gate 的输入。

## 5. 成熟产品迭代计划

### 阶段一：V0.3.1 发布前质量门与数据 contract

目标：让工作台能稳定回答“这个节目现在能不能导出预览，为什么”。

交付：

- `quality_gate` 只读决策：`allow / review / block`、原因列表、版本号、依赖的 health/rights/ASR/queue 指标。
- `rights_mode` 在 doctor、质量哨兵和导出结果中显式呈现。
- CSV 导入报告拆成 `imported_metrics`、`linked_snapshots`、`created_training_samples`、`unlinked_rows`。
- `segmenter_version`、`scorer_version`、`quality_gate_version` 进入评分解释或训练样本 feature snapshot。
- Golden sample 测试：固定质量哨兵、广告口播、ASR 重复、闭环候选比例。

不做：

- 不引入 Celery/RQ。
- 不训练模型。
- 不改变默认导出行为，先只提示 review/block。

执行状态（2026-06-25）：已完成。

- `quality_gate` 已作为只读 contract 输出 `allow / review / block`、原因、动作、版本与依赖信号。
- `rights_mode` 已在 doctor、质量哨兵和导出结果中显式呈现。
- CSV 导入报告已返回 `linked_rows / unlinked_rows`、训练资格和行级问题。
- `segmenter_version / scorer_version / quality_gate_version` 已进入分段结果、评分解释、质量报告和导出结果。
- 已补充 Golden sample 回归，覆盖质量哨兵、广告口播、ASR 重复、闭环候选和推荐模拟联动。

### 阶段二：V0.4 可用运营工作台

目标：让运营在一个候选详情里完成“判断、修正、导出、回流”的闭环。

交付：

- ASR 模型组合 v1：在 doctor、质量报告和候选详情中显式展示 `fast=base`、`quality=small`、`verify/premium=large-v3-turbo-q5_0` 的用途、模型路径、VAD 状态和最近 benchmark。
- Top 候选二次转写：对人工选中的高价值候选支持 `verify` 模型重转写，并与 `quality` transcript 并排展示差异；默认不覆盖全片 transcript。
- ASR 对比 artifact：保存 `small` vs `large-v3-turbo-q5_0` 的片段级对照报告，作为是否升级模型的依据。
- 候选详情合并质量旗标、推荐模拟瓶颈、分项评分、导出状态和反馈样本。
- 人工复核状态：`candidate / review / approved / blocked / exported`。
- 变更可追踪：候选边界、标题、封面时间、表演区间修改后记录原因和前后值。
- 轻量 artifact manifest：每个 video 的 transcript/audio/candidates/scores/exports/quality report 路径和生成版本。
- Variant 实验最小闭环：同一候选 2-3 个版本，记录假设和唯一变量。
- 平台视频映射状态壳：允许运营记录平台侧 `item_id/video_id`，并把 `candidate_segment_id`、`slice_variant_id`、`publishing_experiment_id` 与平台视频绑定。
- 数据来源抽象：反馈导入保留 CSV 主路径，同时在 contract 中区分 `sample_source=csv/api/mock`，为后续只读 SDK 同步预留字段。
- Fake client 字段映射：用模拟响应验证播放、点赞、评论、分享、平均播放时长等字段如何进入 `performance_metrics`，不发真实平台请求。

不做：

- 不做自动发布。
- 不做默认真实抖音 OpenAPI 拉取；OAuth 仅作为用户主动扫码授权契约和后续只读 client 预留。
- 不做用户权限系统。
- 不做大规模迁移到 Postgres，除非多人协作成为明确需求。

执行状态（2026-06-26）：V0.4 主链路已推进到 90%+。

- ASR 模型组合 v1 已形成 `fast / quality / verify` 三档契约，并在 doctor、质量报告和候选详情可见；`verify` 默认只用于人工选中的 Top 候选。
- Top 候选二次转写已支持手动触发，产出候选级 ASR 对比 artifact，不覆盖全片 transcript。
- 候选详情已合并质量 Gate、推荐模拟联动、分项评分、导出状态、反馈样本、运行清单、人工复核、ASR verify 和 Variant 实验入口。
- 人工复核状态已可通过 API/工作台标记，并记录复核事件。
- 候选边界、封面时间、字幕/结构、表演区间和 variant 修改已记录 before/after diff、原因、操作人和时间。
- 轻量 artifact manifest 已覆盖 transcript、audio、candidates、scores、quality、exports、asr_verify 等步骤。
- Variant 实验最小闭环已支持同一候选多版本、假设、唯一变量、发布时间窗口和发布实验记录；CSV 回流可用 `slice_variant_id / experiment_id` 关联。
- 平台视频映射状态壳已支持本地 `platform_item_id` 与 candidate/variant/experiment 绑定，不接真实平台 API。
- 反馈导入已区分 `sample_source=csv/api/mock`，并支持平台 item 映射进入训练样本。
- Fake client 字段映射已提供 mock contract，可把 `play_count / show_count / avg_play_duration / like_count` 等模拟字段映射到 `performance_metrics`。
- 抖音数据回流已从“映射状态壳”升级为本地只读同步适配层：支持 `mock / csv / json / api` 四类输入统一进入 `platform_video_mappings -> performance_metrics -> metric_snapshots -> training_samples -> account_baselines`，并记录 `platform_accounts` 与 `platform_sync_runs`。
- 工作台已新增“抖音数据回流”面板，支持 Mock 同步和本地 CSV/JSON 文件同步；候选详情可绑定平台侧 `item_id / aweme_id`。
- 用户抖音账号扫码登录已形成 OAuth 契约：工作台可生成官方授权 URL，用户扫码后通过回调 `code/state` 完成授权；真实 token 不写入 SQLite，只写入本地 `data/auth/douyin_tokens.json` 并尽量设置 0600 文件权限。

剩余 10%：

- `verify` profile 的真实模型效果仍需要 ASR 评估集和真实 benchmark 支撑，当前先完成可调用和可追责契约。
- Artifact manifest 目前是轻量运行清单，不是完整队列调度系统；错误恢复仍以同步命令/API 返回为主。
- Variant/平台映射已有数据闭环，但尚未做专门的 A/B 可视化报表、发布日历和真实只读 OpenAPI 同步。
- 真实抖音开放平台接入还需要开放平台应用、`client_key / client_secret`、HTTPS `redirect_uri`、用户授权、`user_info / posting.behavior / video list or data read` 等 scope 审批；当前默认同步 client 不主动发起平台数据请求，token 仅为后续真实只读 client 预留，尚未完成 token refresh、授权账号视频列表和真实指标拉取。

### 阶段三：V0.5 可学习排序地基

目标：从规则工作台升级为可回测、可校准的小型推荐镜像。

交付：

- ASR 评估集 v1：沉淀中文长口播、英文歌手/英文歌名、赛制口播、人名密集、广告口播五类标注片段，记录关键词召回、错词率、分段质量、Top30 影响和耗时。
- 模型路由策略 v1：基于片段类型和质量风险决定是否触发 `verify/premium`，例如中文长叙事/人名密集用 `large-v3-turbo-q5_0` 复核，英文歌名/英文介绍优先保留 `small` 结果。
- 高规格候选验证：仅在评估集显示 `large-v3-q5_0` 或其他模型显著提升关键词召回、且不伤英文歌手场景时，才考虑新增更高质量 profile。
- Memory bank v1：文本 embedding、封面/关键帧 embedding 占位或可选实现、音频 summary features，全部带模型和版本。
- Mini-STCA v1：当前候选与账号历史高/低表现样本做相似匹配，输出 `similar_high_perf_score`、`similar_low_perf_risk`、`history_uncertainty`。
- Mini-InterestClock v1：账号 x 切片类型 x 时长 x 小时的平滑时间建议。
- 抖音开放平台只读接入 v1：OAuth 授权、token refresh、授权账号视频列表、平台视频与本地 variant/experiment 匹配、按 6h/24h/72h/7d/30d 同步表现数据。
- 同步审计 v1：记录每次平台数据拉取的窗口、状态、错误、权限/限流信息、原始响应摘要和入库条数；权限不足、账号解绑、视频删除必须显式显示。
- LightGBM/Logistic baseline：仅在 100+ 自有/授权样本后启用；输出离线回测报告，不直接替代规则排序。
- 评估面板：NDCG@10、Top10 人工一致率、闭环率、广告风险率、校准误差、低曝光不确定样本占比。

执行状态（2026-06-26）：V0.5 已推进到可学习排序地基约 70%。

- Memory bank v1 已落地文本 hashing embedding、版本号、向量缓存、content hash 失效重建和 API/CLI 入口；空账号返回 `empty`，避免无候选时误报 ready。封面/关键帧 embedding 与音频 summary features 仍未接入。
- Mini-STCA v1 已落地候选历史相似样本匹配，输出 `similar_high_perf_score`、`similar_low_perf_risk`、`history_uncertainty`，并在样本不足时返回 `low_confidence / insufficient_history`。该模块只使用非 mock 训练样本，mock 回流不参与生产学习判断。
- Mini-InterestClock v1 已落地账号 x 切片类型 x 时长桶 x 小时的平滑建议，优先用 `published_at` 推导发布时间，缺失时退回 `collected_at - hours_since_publish`。同样排除 mock 样本。
- Backtest v1 已落地规则排序离线回测，指标包含 `NDCG@K`、`topk_hit_rate`、`calibration_mae`、`closed_loop_rate`、`low_exposure_uncertain_rate`，并排除 mock 样本；列表接口已扁平化 `metrics / top_rows`，便于前端使用。
- 工作台“数据反馈与模型学习”页已新增 V0.5 学习评估面板，支持构建记忆库、刷新时间建议、生成离线回测，并展示记忆候选、最佳小时和 NDCG。学习评估接口失败时不会拖垮已有训练样本、账号基线和抖音回流展示。
- API 已提供 `/learning/memory/build`、`/segments/{segment_id}/history`、`/accounts/{account_id}/interest-clock`、`/accounts/{account_id}/interest-clock/rebuild`、`/learning/backtest`；CLI 已提供对应 `memory-build / history / interest-clock / backtest / backtest-reports` 命令。

剩余 30%：

- ASR 评估集 v1 和模型路由策略 v1 尚未完成，`verify/premium` 何时触发仍需要真实评估集校准。
- 高规格候选验证还缺 `large-v3-q5_0` 或其他模型的收益/耗时 benchmark。
- 抖音开放平台只读接入仍停留在 OAuth 契约和本地同步适配层；真实 token refresh、授权账号视频列表、视频指标拉取、权限/限流错误审计还未产品化。
- InterestClock 与 variant/experiment 的 `publish_window` 尚未形成完整追溯报表。
- 历史相似信号尚未进入候选详情和推荐模拟解释，只在 API/CLI 层可调用。
- LightGBM/Logistic baseline 仍需等待 100+ 自有/授权样本，不应提前启用。

不做：

- 不做深度多任务模型。
- 不做用户级建模。
- 不做自动发布、自动互动或无人值守账号操作。
- 不使用未授权抓取数据训练。

## 6. 阶段一实现建议（已完成记录）

以下建议已在阶段一实现中完成或收敛到等价实现，保留为回溯记录：

1. 新增纯函数 `quality_gate_from_insights(report, rights_mode)`，先不接导出阻断。
   - 测试：missing transcript、ASR repetition、sponsor risk、strict rights、trusted sample 五类 fixture。
   - 回滚：删除函数和 endpoint 即可，不影响主链路。

2. 扩展 `/videos/{video_id}/quality` 返回 `gate` 字段。
   - 测试：现有 `quality_insights` 测试增加 `gate.status` 和 `gate.reasons`。
   - 回滚：移除字段，不破坏老客户端。

3. 改进 `import_metrics()` 的导入报告。
   - 行为：未解析到 `candidate_segment_id` 的 CSV 行标为 `unlinked_rows`，不创建 training sample，并在返回值说明。
   - 测试：一条有效行 + 一条无效行。
   - 回滚：只影响报告和校验，不动 schema。

4. 引入三个常量版本号：`SEGMENTER_VERSION`、`SCORER_VERSION`、`QUALITY_GATE_VERSION`。
   - 行为：先放进 explanation/report/feature_version 字符串，不新增列。
   - 测试：断言 training sample 或 report 能看到版本。
   - 回滚：常量和拼接删除即可。

5. 把 `candidate_segments.status` 状态文案收敛到最小枚举。
   - 行为：人工 patch 后仍可保持 `corrected`，但 UI 映射成 `review` 或 `approved` 时要明确。
   - 测试：patch 后重评分和状态不回退。
   - 回滚：只改显示/映射层。

6. 增加一个 golden quality fixture。
   - 内容：合成 transcript 包含导师评价、副歌高音、现场反应、广告口播、重复幻觉。
   - 指标：health 非 good、watchlist 非空、closed_loop_count >= 1、sponsor_risk_count >= 1。
   - 回滚：只删测试 fixture。

## 7. 架构结论

当前项目的 MVP 方向是正确的，且已经比文档里的早期骨架更成熟：质量哨兵、推荐模拟、指标快照、训练样本和账号基线都已经有代码支撑。下一步不应大改算法，也不应急着训练模型；更应该把现有能力收束成成熟工作台的 contract。

最短路径是：

```text
质量门禁 contract
  -> 审核/导出状态 contract
  -> 指标导入可信度 contract
  -> 版本化特征 contract
  -> memory bank 和历史校准
```

只要把这几层补齐，系统就会从“能跑的切片工具”稳稳变成“可运营、可复盘、可学习的本地工作台”。

## 8. 核验来源

- 本地文档：`README.md`、`pyproject.toml`、`docs/architecture.md`、`docs/history/platform-v0.3-plan.md`、`docs/research/strategy/research.md`、`docs/research/strategy/algorithm-study.md`、`docs/research/strategy/paper-architecture-review.md`、`docs/research/strategy/music-variety-strategy.md`
- 本地源码：`src/dso`、`tests/test_core.py`
- TikTok 官方推荐说明：https://support.tiktok.com/en/using-tiktok/exploring-videos/how-tiktok-recommends-content
- 国家版权局《中华人民共和国著作权法》：https://www.ncac.gov.cn/xxfb/flfg/flfg_532/202103/t20210309_50530.html
- whisper.cpp 官方仓库：https://github.com/ggml-org/whisper.cpp
- Make It Long, Keep It Fast：https://arxiv.org/abs/2511.06077
- Monolith：https://arxiv.org/abs/2209.07663
- Streaming VQ Retriever：https://arxiv.org/abs/2501.08695
- Next-User Retrieval：https://arxiv.org/abs/2506.15267
- Interest Clock：https://arxiv.org/abs/2404.19357
- LEMUR：https://arxiv.org/abs/2511.10962
- RankMixer：https://arxiv.org/abs/2507.15551
- MuChator：https://arxiv.org/abs/2605.27103
