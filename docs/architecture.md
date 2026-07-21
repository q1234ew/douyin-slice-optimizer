# 抖音短视频切片优化系统架构设计

设计日期：2026-06-23
阶段：MVP 架构
目标：把论文调研中的推荐系统思想转成一个创作者侧可落地的短视频切片优化系统。

相关文档：

- [product-goals.md](product-goals.md)：产品北极星目标、成功指标与持续迭代原则。
- [research.md](research/strategy/research.md)：论文与公开资料调研。
- [algorithm-study.md](research/strategy/algorithm-study.md)：Douyin/字节论文算法拆解。
- [paper-architecture-review.md](research/strategy/paper-architecture-review.md)：主线论文复核后的架构更新建议。
- [music-variety-strategy.md](research/strategy/music-variety-strategy.md)：音乐综艺短视频切片专项策略。
- [model-scheduling-architecture.md](architecture/model-scheduling-architecture.md)：本地模型持久队列、GPU lease、优先级、模型亲和和恢复设计。
- [platform-v0.3-plan.md](history/platform-v0.3-plan.md)：多 Agent 进展汇总和 V0.3 平台迭代计划。

## 1. 系统定位

本系统第一阶段以音乐综艺短视频切片为核心对象。歌曲、舞台和歌手是核心素材，但成品不是纯歌曲片段，而是有开头钩子、节目上下文、情绪推进和互动点的短视频内容。系统有两个输入入口：已经切好的短视频直接进入候选排名，完整节目先自动生成和分类候选；两类候选标准化后共用同一排序、审核和反馈链路。

- 对已经切好的单条或批量短视频做内容理解、筛选、推荐排名和解释，保留原始片段边界。
- 从音乐综艺长视频中自动发现高潜短视频片段。
- 为候选切片生成多维评分和解释。
- 输出标题、封面帧、字幕和风险建议。
- 记录发布后的表现数据。
- 用账号历史数据持续校准评分。

第一版不做自动发布、不做刷量、不做模拟互动、不做绕过平台规则的能力。

统一候选主链路：

```text
已切短片 ----------------------------> 标准化候选
完整节目 -> 多模态理解 -> 切片/分类 -> 标准化候选
                                         -> 统一排序
                                         -> 人工审核与导出
                                         -> 平台表现回流与校准
```

## 1.1 主线论文复核后的结论

2026-06-24 复核 Douyin/字节主线论文后，当前 MVP 架构方向成立，但需要从“规则评分工具”升级为“可学习的创作者侧推荐系统镜像”。不需要重写现有 MVP，应该在架构上补齐以下能力：

- **合规数据闭环层**：公开样本只做小规模人工研究和趋势先验；训练主数据来自自有账号、授权账号、开放平台接口、合法第三方数据和人工标注。
- **事件日志与标签窗口**：发布后按 6h/24h/72h/7d/30d 形成指标快照，支持 reward label、排序训练和校准。
- **多模态 memory bank**：缓存 ASR、歌词、标题、封面、关键帧、音频和 OCR 表征，避免每次评分重算。
- **候选召回 + 二阶段排序**：先用规则、音乐结构、剧情结构和向量相似召回候选，再用规则分/LightGBM/LambdaRank/后续 reranker 排序。
- **账号历史序列匹配**：把当前候选当作 target，把账号历史切片当作 history，做轻量 STCA 风格的 target-to-history 匹配特征。
- **时间与主题联合建模**：发布时间不只按小时，而要按“账号 x 主题簇 x 切片结构 x 小时”做平滑统计。
- **冷启动受众簇**：不能拿到平台用户级数据时，用受众簇、主题簇和历史相似样本近似 Next-User Retrieval。
- **多任务目标函数**：将 5 秒留存、平均观看比例、完播、复播、分享、收藏、评论质量、关注和负反馈拆成多任务/多标签，而不是只优化播放量。

## 2. 总体架构

```text
Authorized Local File -----------------------------------------+
Authorized Tencent-family / YouTube single-video URL           |
  -> Optional restricted videodl Acquisition Adapter -> Local File +
                                                        -> Media Ingest
  -> ASR / Frame / OCR / Audio Feature Extraction
  -> Multimodal Memory Bank
  -> Performance / Song Segment Detection
  -> Narrative / Reaction Context Detection
  -> Candidate Short-video Slice Generation
  -> Candidate Retrieval
       -> Narrative/Music Rule Recall
       -> Similar Historical Clip Recall
       -> Topic/Long-tail Recall
       -> Cold-start Audience Recall
  -> Ranking and Scoring
       -> Short-video Hook Scorer
       -> Narrative Context Scorer
       -> Comment Trigger Scorer
       -> Musical Moment Scorer
       -> Chorus / Climax Scorer
       -> Lyric Resonance Scorer
       -> Performer / Stage Scorer
       -> History Similarity Scorer
       -> Mini-Trinity Topic Scorer
       -> Mini-InterestClock Time Scorer
       -> Reward Proxy / Learned Ranker
       -> Rights / Risk Scorer
  -> Slice Variant Suggestions
  -> Export / Manual Publish
  -> Performance Import / Metric Snapshots
  -> Training Sample Builder
  -> Feedback Update / Model Calibration
```

### 2.1 Qwen3-ASR 音乐综艺主路由与 Shadow 对照（2026-07-18）

音乐综艺 `input_mode=program/precut` 的 auto 路由优先使用 Qwen3-ASR 写主转写；服务不可用或失败时自动回退 Whisper。`qwen3_asr_shadow.v1` 继续提供独立对照 artifact：

```text
source_video
  -> Qwen3-ASR 主 transcript + 数据库 transcript_path
       -> 失败时 Whisper.cpp / faster-whisper fallback
  -> 可选 Qwen3-ASR Shadow
       -> transcript/shadow/qwen3_asr/transcript.json
       -> status.json / qwen3_asr_last_run.json
       -> comparison only / auto_promote=false
```

- auto 主路由记录 `requested_backend / backend_preference / selected_backend / fallback_used`，显式指定其他后端时尊重任务级覆盖。
- Qwen 主转写缓存会在服务随后卸载时继续复用；只有配置变化或 `--force-asr` 才重跑，避免回退 Whisper 反向覆盖已有 Qwen 结果。
- Shadow 路径不调用 `_mark_transcribed`，不修改 `source_videos.transcript_path/status`。
- 音频 hash、Qwen 配置和后处理版本共同组成缓存键；相同配置重复运行直接复用。
- 主路由服务未加载时自动回退 Whisper；Shadow 路由返回 `waiting_model_switch`，不写 placeholder。
- CLI 为 `dso qwen3-asr-shadow <video_id>`；API 为 `GET/POST /videos/{video_id}/asr/shadow`。
- 16GB GPU 仍需在 Qwen3-ASR、Omni 与 Embedding 间串行切换；`model_scheduler.v1` 的持久队列、GPU lease/fencing、逐窗口 Omni、逐块 ASR、Embedding Adapter 和受控 Resource Agent 已落地。局域网 canary 已在 `192.168.31.143:8010` 部署 Agent，本机 launchd Worker/8127 Web 显式启用；其他环境仍默认关闭。

### 2.2 Hybrid Slice Shadow Pipeline V1（2026-07-18）

候选生成采用确定性召回，Omni 只生成显式研究复排证据；大模型不直接扫描整条长视频，也不改写最终边界或默认生产排序：

```text
ASR + 1s 音频能量 + 静音/起音 + 低帧率切镜
  -> 30+ 候选召回
  -> 句首/句尾/静音/切镜边界吸附
  -> 规则分 + 历史先验预排
  -> Top 3 候选
  -> Qwen2.5-Omni 逐条分析 hook / middle / payoff（每窗默认 6 秒）
  -> 置信度折算后的研究混合分（默认上限 15%）
  -> 显式 research scope 对照，不改变 production scope
```

关键约束：

- Omni 不写 `manual_verified`，不自动批准候选，不自动发布。
- Omni 的边界建议只作为证据；实际边界由确定性时间轴信号吸附。
- 模型未加载、服务不可用或单条媒体失败时，保持 `current_rules/final_score` 默认排序。
- 转码窗口和模型结果按源文件、候选时间范围、模型与版本缓存，重复运行不重复推理。
- 16GB 显存默认 `candidate_limit=3`、`max_clip_seconds=6`、`batch_size=1`；前端先返回规则候选，再增量刷新 Omni 结果。
- 模型服务使用 text-only thinker 路径，明确传递 `thinker_max_new_tokens=128`；推理由单线程执行器串行化，`/health` 独立返回 `ready/busy`，150 秒超时后由 systemd watchdog 重启服务，避免孤儿推理拖死整个 API。生产端口锁定 Omni 模型，Embedding 任务必须使用独立服务或先显式解除维护锁，避免共享 `/load` 竞争显存。
- 跨 ASR、Omni、Embedding 和研究任务的统一调度 contract 为 `model_scheduler.v1`；详细状态机、SQLite、lease/fencing、API 和迁移方案见 [本地模型资源调度架构设计](architecture/model-scheduling-architecture.md)。Phase 0–2 和 Phase 3 batch-1 基线已实现独立队列、单 GPU lease、逐 item staged commit、亲和/公平调度、准备池、逐窗口 Omni、逐块 ASR、Embedding、状态/取消 API 和安全 Resource Agent；局域网 canary 已显式启用并保持 `validate`，长节目/批量真实混合 workload、GPU 空闲率和 batch 2/4 门禁仍未通过。

主要入口：

- API：`POST /videos/{video_id}/hybrid-slice`
- 增量复排：`POST /videos/{video_id}/omni-rerank`
- CLI：`dso hybrid-slice <video_id> --load-model`
- 前端：节目管理中的“智能切片”按钮；候选卡与右侧详情显示 Omni 参与/回退状态。

### 2.3 生产排序与研究排序 contract

- `production_ranking_policy.v1` 的默认 scope 为 `production`，唯一默认策略为 `current_rules`，分数字段为 `final_score`。
- `ranker_score`、`hybrid_score`、Omni、embedding 和后续实验模型均属于显式 `research` scope；它们可参与回测、解释和诊断，不改变默认审核、导出或发布顺序。
- Promotion gate 同时要求绝对指标、至少 10 个 ready 账号胜出、相对 `current_rules` 与 `semantic_baseline_v2` 中最强基线 lift 至少 `+0.03`，且 NDCG、高互动命中和低互动避让均不回退。
- Gate 通过只产生 `eligible_for_promotion`，不会自动改写生产策略；采用新策略必须冻结新 benchmark，并显式更新 production policy。
- `GET /ranking/policy` 返回当前 contract；`GET /videos/{video_id}/suggestions` 默认生产排序，只有显式 `ranking_scope=research` 才返回研究顺序。

### 2.3.1 研究账号与发布账号证据隔离

历史采集账号、平台连接账号和候选所属本地账号不得再依赖同一个 `account_id` 字符串猜测用途：

```text
研究账号（tianci 等） -> historical_capture_samples -> cross-account research prior
目标发布账号          -> target_outcome mappings   -> verified platform outcomes
未分配 main 槽位      -> unclassified mappings     -> audit only
```

- `platform_accounts.account_role` 只接受 `unassigned / publishing_target / research_source`；目标发布账号必须显式指定，不能由历史样本、当前筛选项或 `main` 默认值推断。
- `platform_video_mappings.evidence_scope` 只接受 `unclassified / target_outcome / research_proxy`。同一平台 item 已归属某个本地账号后禁止静默跨账号重分配。
- `performance_metrics.metric_semantics` 区分 `explicit_platform_outcome / engagement_proxy / ambiguous_visible_count / legacy_unverified / mock`。
- `可见计数 / 计数数值 / visible_count_number / best_visible_count_number` 不属于播放量别名，不写入 `views`。
- 目标账号个性化 readiness 只统计 `publishing_target + target_outcome + explicit_platform_outcome + 已链接候选 + 非 mock` 的去重作品；不足 30 条或缺少流量及观看/转化维度时保持 `cold_start`。
- 离线 research promotion gate 即使通过，也不代表目标账号生产校准通过；平台结果缺失时生产默认仍为 `current_rules/final_score`。

### 2.3.2 互动热度标签 V3 冻结 contract

`interaction_heat_labels.v3` 是可学习排序的研究标签层，不是生产分。它从点赞、评论、收藏和转发四项抓取时点客观计数分别生成 `like_heat / discussion_heat / favorite_heat / share_heat`，并以四维等权平均生成只用于宽口径诊断的 `broad_heat`；没有曝光分母时不得把这些字段解释为播放流量或转化率。

- 原始计数先做 `log1p`，再按账号、发布时间年龄桶和时长桶映射到训练分区拟合的经验分位数；回退层级和样本量随标签保存，缺失指标保持 `null`，不以 0 填充。
- `account_time` 和 `account_holdout` 各自只读取本 protocol 的 train 行拟合 normalizer。账号留出没有账号内统计时回退到训练账号的全局年龄/时长分布，禁止读取 holdout 分位点。
- source group 联合平台 item ID、规范/近重复标题、节目+歌曲键和可用媒体 SHA-256；同组不得跨有效 split。无法安全保留在原 fold 的样本显式写为 `excluded_leakage`，不得静默混入训练。
- 冻结目录使用 `interaction_heat_artifact.v1`，必须精确包含 `manifest.json / labels.jsonl / splits.jsonl / normalizers.json / report.json`，多一个或少一个目录项都失败。manifest 只能列出固定四个 payload basename；manifest/payload 使用 no-follow 普通文件读取，内部或外部 symlink、FIFO/device 和路径片段逃逸均 fail closed。验签必须提供另处保存的 `expected_manifest_sha256`，自签 manifest 只能证明内部一致，不能证明未被整体重写。manifest 同时固定输入、代码、网络请求数、模型费用及生产影响；同一 artifact ID 拒绝覆盖。
- `research_labels.visible_engagement_v2` 保持原状。V3 只能进入显式研究训练/评测，Pairwise/LambdaRank 通过冻结 benchmark、账号宏平均和强基线门禁前不得写生产排序。
- 阶段 2 的 `interaction_heat_pairwise_logistic.v1` 只从白名单结构元数据、时间/时长桶和标签置信度生成稳定 signed-hash 特征，禁止互动计数及其直接派生量。`account_time` 与 `account_holdout` 分别只用各自 train 行构造同账号有序对并训练独立模型；输出 artifact 固定保存模型、无标签预测、账号宏报告和来源/成本/生产影响 manifest，同一实验 ID 不覆盖。当前本地 r3 在账号内时间 test 回退、整账号留出仅有弱增益，状态为 `research_only`，没有接入统一生产 ranker。
- `interaction_heat_target_encoding.v1` 从同一 V3 artifact 和 SQLite 白名单元数据构造层级平滑类别均值。训练预测必须使用 OOF，validation/test 只能查询本 protocol 的 train 统计；`account_time` 可按账号+特征值回退到全局特征值和全局均值，`account_holdout` 禁止账号历史。runner 先读无标签 splits，再按 `evaluation_scope` 过滤 sample ID；纯 test 行不进入 validation-only labels 映射，跨协议样本也必须先检查本 protocol split 再解析 target。artifact 固定包含 manifest、模型、无标签预测和账号宏报告，同一 ID 不覆盖。正式 r2 只评估 validation，当前为 `research_only`，未接入统一生产 ranker。
- `interaction_heat_holdout_readiness.v1` 是后续非线性 ranker 的数据解锁门禁，不生成或重写标签。runner 必须先以外部 pinned SHA 验证冻结 V3，只解析 `splits.jsonl` 获得冻结 sample、账号和最大 `published_at`；当前数据库行继续通过 V3 的显式 metric provenance loader 过滤。只有发布时间严格晚于 cutoff 且不在冻结 sample ID 集合中的行可成为前向候选；整账号候选必须来自冻结账号集合外，可以是较早发布但在冻结后新收集的作品。输出仅为内存 JSON 状态、阈值、未满足原因、数据库 SHA 和零副作用声明；`not_ready` 时不得安装/运行下一非线性模型或重复使用旧 test 作为新 holdout。

### 2.4 授权远程媒体获取（2026-07-18）

远程下载只作为 Media Ingest 前的可选获取层，不生成第二套节目、候选或排序 contract：

```text
授权腾讯 URL  -> videodl TencentVideoClient -------------------+
授权 YouTube 单视频 URL -> 受限 YouTubeVideoClient -> 720p 视频 + 音频 |
                                                             -> 本地任务目录 + manifest
                                                             -> ingest_video
                                                             -> G2 标准节目链路
```

约束如下：

- Provider 固定为 `videofetch==0.9.1`；按白名单只加载 `TencentVideoClient` 或受限 `YouTubeVideoClient`，下载 contract 为 `video_download.v1`。
- 只接受 HTTPS 的 `v.qq.com`、`wetv.vip`、`iflix.com`、`youtube.com`、`youtu.be`。YouTube 的 watch/短链/shorts/live/embed URL 统一规范成单个 watch URL，删除播放列表参数并拒绝 playlist/channel URL。
- YouTube 不调用上游 `ytdown`、`downr` 或通用解析器；只使用 `videodl` 内置 YouTube 直连工具，最高选取 720p、优先 H.264 MP4，并将默认 AAC/MP4 音轨交给 FFmpeg 合并。目标缺少兼容独立音轨时才回退渐进式 MP4。
- 不传 Cookie、账号凭据或代理；上游选中格式标记 `has_drm=true` 时硬拒绝，不提供解密或绕过路径。
- 默认单链接最多 1 个媒体，显式上限 20；线程上限 8；结果只能落在当前工程的 `data/tmp/video_downloads/<job_id>/` 内，或用户显式指定的 `--output-dir` 任务子目录内。
- 每次调用必须确认 PolyForm Noncommercial 1.0.0 非商业许可；商业使用需另行获得上游授权。
- 下载是长任务，当前只提供 CLI/service 入口，不新增阻塞 FastAPI 请求；`--dry-run` 只解析并写 manifest，`--no-ingest` 不修改业务数据库。

对应论文思想：

| 系统模块 | 参考论文思想 | MVP 实现方式 |
| --- | --- | --- |
| Memory Bank | LEMUR | 缓存 ASR、视觉、音频、OCR embedding |
| History Similarity | Make It Long / STCA | 候选切片与历史切片做 target-to-history 匹配 |
| Real-time Update | Monolith | 每次导入表现数据后立即更新统计和评分基线 |
| Topic Cluster | Trinity | 账号历史主题聚类，识别多兴趣、长尾和长期兴趣 |
| Time Fit | Interest Clock | 按小时和主题统计表现，推荐发布时间 |
| Cold-start Audience | Next-User Retrieval | 用受众簇近似“下一批可能互动的人群” |
| Vector Retrieval | Streaming VQ Retriever | 用向量索引召回相似历史切片和主题簇 |
| Reward Proxy | RankMixer / MixFormer / MDL | 将多目标行为标签 token 化或结构化，支持后续学习排序 |
| Legal Training Loop | Monolith + 平台规则 | 只使用授权/自有/许可数据训练，公开数据只做弱监督和趋势先验 |

## 3. 技术选型

MVP 优先本地可运行，降低部署成本。

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 后端语言 | Python | 视频处理、ML、数据处理生态最好 |
| API | FastAPI | 后续接前端方便，CLI 也能复用 service |
| 数据库 | SQLite | MVP 足够，后续迁移 Postgres |
| 向量检索 | sqlite-vss / FAISS / 本地 numpy | 第一版可先用 numpy cosine，相似度规模小 |
| 视频处理 | FFmpeg | 切片、抽帧、转码、音频提取 |
| ASR | Whisper 或兼容模型 | 生成带时间戳字幕 |
| OCR | 可选 PaddleOCR / EasyOCR | MVP 可先延后 |
| 图像 embedding | CLIP / SigLIP | 提取封面和关键帧语义 |
| 文本 embedding | 本地 embedding 或 API | 用于主题聚类和相似切片召回 |
| 传统模型 | LightGBM / XGBoost | 数据积累后训练 reward 预测 |
| 存储 | 本地文件系统 | 视频、音频、字幕、导出切片 |

## 4. 建议目录结构

```text
douyin-slice-optimizer/
  docs/
    README.md
    product-goals.md
    development-requirements.md
    current-state.md
    architecture.md
    model-and-algorithm-radar.md
    user-manual.md
    architecture/
    guides/
    design/
      frontend/
    research/
      providers/
      evaluations/
      strategy/
    history/
  src/
    dso/
      __init__.py
      config.py
      db/
        models.py
        session.py
        migrations/
      media/
        ingest.py
        video_download.py
        ffmpeg.py
        storage.py
      features/
        asr.py
        frames.py
        audio.py
        ocr.py
        embeddings.py
      segments/
        generator.py
        heuristics.py
      scoring/
        hook.py
        retention.py
        engagement.py
        history.py
        trinity.py
        interest_clock.py
        risk.py
        final_score.py
      variants/
        title.py
        cover.py
        subtitle.py
        exporter.py
      feedback/
        importer.py
        metrics.py
        updater.py
      api/
        main.py
        routes/
      cli.py
  data/
    media/
    exports/
    cache/
    db/
  tests/
```

第一阶段可以先不建前端，提供 CLI 和 API；等核心链路跑通后再做管理台。

## 4.1 修订后的工程边界

论文给我们的启发不是“复刻抖音推荐系统”，而是把创作者侧系统做成可学习闭环：

- **MVP 保持本地优先**：继续使用 SQLite、FFmpeg、ASR、规则评分和人工校正。
- **从第一天记录训练资产**：即使暂不训练模型，也要记录样本来源、指标窗口、特征版本、授权状态和人工审核标签。
- **先做可解释排序，后做大模型排序**：100-1000 条发布样本前，使用规则和 LightGBM/LambdaRank；1000+ 样本后，再考虑 Mini-STCA、多任务模型或小型 reranker。
- **训练数据默认保守合规**：公开视频页面可见数据不等同于可批量抓取和商用训练；批量训练应依赖自有账号、授权账号、开放平台或合同许可数据。

## 4.2 G3 公网模型 Provider V2

公网模型以独立的 `src/dso/providers/` 边界接入，不允许厂商 SDK 或返回结构直接进入候选、排序和审核业务模块。V2 注册零网络 `FakeProvider` 和真实协议但默认关闭的 `AliyunBailianProvider`。百炼 Adapter 只接受北京业务空间专属 HTTPS Host、固定模型白名单和按能力冻结的 JSON 请求；Chat、Multimodal Embedding、Multimodal Rerank、Pairwise Judge 分别使用独立 URL、参数白名单、提示词/接口版本和响应 schema。标准 profile 的媒体输入只接受本地 JPEG Data URI，单请求最多三张。完整短片研究使用互相隔离的 profile：`complete_short_clip` 只允许固定 `qwen3.7-plus-2026-05-26` 和本地无音轨 MP4；`qwen35_omni_complete_short_clip` 只允许固定 `qwen3.5-omni-plus-2026-03-15` 和 H.264/AAC MP4；`qwen35_omni_propagation_features` 复用同一固定 Omni 快照，但只抽取 `content_form/hook/audio/visual/narrative/timeline` 等事实字段，禁止要求或返回平台传播分。Omni 响应结束后才由 `propagation_feature_outcome_dataset.v1` 在本地连接可见互动代理、分享率、关注转化率和观看质量；缺失分母或指标必须写 `unavailable_*`，不能以零或其他互动计数替代。`bailian_complete_clip_batch.v1` 与 `bailian_propagation_feature_batch.v1` 只为已冻结短片清单生成源 SHA 绑定的全时长 H.264/AAC 代理，禁止抽帧和标签入模，先完成全批零网络预检与显式可配置硬预算检查，再串行执行。所有完整短片 profile 都限制 2–60 秒、3.5 MB、`fps<=2`、仅文本输出、流式 usage、模态分项计费、逐批 `full_media` 许可和 0 次重试；不接受任意公网 URL、完整节目、工具调用或调用方自定义 messages/parameters，也不会因只设置 API Key 自动联网。

`propagation_feature_validation_manifest.v1` 冻结完整短片传播特征的账号隔离对照集。高低互动样本必须同账号、同发布时间年龄桶、同时长桶、同内容类别，且满足互动差距、时长差、平台 item、稳定标题和媒体 SHA 防重复；每条媒体还需通过真实视频流、音轨、时长和源 SHA 校验。`propagation_feature_account_holdout.v1` 使用留一账号评测：每个 fold 的 Omni 类别对数优势与 v2.4 历史证据都只能读取其他账号，固定比较 v2.4、Omni 事实特征和 85/15 固定融合，不在评测集搜索权重。门禁同时检查总体成对命中、样本充足账号宏平均、账号胜负和回退，避免大账号支配结论。完整视频缓存身份包含输出 Token 上限；个别截断样本只能用独立受限恢复 manifest 处理，再由成功结果优先的 merge contract 合并，禁止手工覆盖主报告。通过门禁也只允许建立下一版代理 holdout；缺少真实曝光、观看、分享率或关注转化时，禁止晋级生产排序。

```text
本地基线
  -> ProviderRequest（内容 hash、模型/API/提示词版本、输入规模）
  -> 默认关闭 / 数据许可 / 上传级别 / 密钥引用门禁
  -> 确定性缓存
  -> 跨进程预算执行锁 + 台账实时刷新
  -> 单请求 / 单批次 / 单日最坏费用预留
  -> Provider Adapter（Fake 或显式启用的 Aliyun Bailian）
  -> 结果 schema 与敏感字段校验
  -> 按实际 usage 结算/释放；未知账单保守占用预留
  -> 独立 SQLite 调用与逐网络尝试台账
  -> Shadow 评测（质量、严重错判、P50/P95、失败率、缓存、费用）
  -> 最终结果仍保留本地基线
```

核心 contract：

- `public_model_provider.v2`：厂商无关请求、结果、实际 usage、Provider request ID、缓存 Token、价表版本、账单状态、逐尝试指标、许可审计快照和决策证据。
- `public_model_runner.v2`：按“策略/数据许可 -> 本地缓存 -> POSIX 跨进程预算锁 -> 台账费用刷新 -> 预算预留 -> Provider -> usage 结算 -> 台账 -> 本地回退”的 fail-closed 顺序执行；共享同一台账的付费调用串行进入预算临界区，缓存命中不占付费预算，实际费用超过预留时只保留本地结果。不支持跨进程锁的平台不得静默降级为进程内锁。
- `public_model_ledger.v2`：固定字段独立存储 preflight reservation、usage estimate、账单校准位、未知账单、实际 token/字节、逐网络尝试、重试、限流、缓存、许可和保留政策引用；不存 API key、Authorization、提示词正文、字幕正文、Base64 或原始媒体。
- `public_model_shadow_evaluation.v1`：只生成 `research_only` 对比报告，不改变生产权重、人工 Gold、导出和发布。

真实 Adapter 的准入顺序固定为：核对厂商官方接口与价格、明确数据许可和保留政策、配置环境变量密钥、配置同币种预算、在冻结集 Shadow 对比，最后才讨论低权重融合。固定保留天数未知时只允许记录带政策引用的 `provider_minimum_necessary`，状态同时暴露 `retention_days_known=false`；不得用 `0` 伪装零保留。任何门禁缺失均返回本地基线。

### 4.2.1 Provider 管理配置 contract

`provider_admin_config.v1` 为 Web 管理面板提供只配置、不调用的连接管理边界：

- `GET /providers/config` 返回 Provider、固定模型、兼容地址、三层预算、治理门禁以及 `api_key_configured` 布尔值；响应固定 `Cache-Control: no-store`，绝不返回 API Key。
- `POST /providers/config` 仅接受同源 JSON，并且只允许 HTTPS 反向代理或直接 loopback/SSH 本地端口转发；公网 HTTP 请求返回 403。
- 保存目标是 root 服务的 `/etc/dso/bailian.env`，本地非 root 开发为 `data/auth/bailian.env`；采用同目录临时文件、`fsync`、原子替换和 `0600` 权限，不经过 shell、不写数据库。
- 每次保存强制写入 `DSO_PUBLIC_MODEL_API_ENABLED=0`，因此连接信息写入不构造 Runner、不调用网络、不产生成本。数据许可和保留策略仍是独立 fail-closed 门禁。
- API Key 只允许百炼业务空间 `sk-` Key；拒绝空白/控制字符、超长值和 `sk-sp-` Coding Plan Token。前端使用 password input，保存成功立即清空，不写 localStorage/sessionStorage。

该 contract 只改变运维配置面，不改变候选、排序、人工 Gold、导出或发布 contract。验证入口为研究中心“模型与环境 → 公网模型 API”和 `pytest -q tests/test_provider_admin_config.py`。

### 4.2.2 百炼向量研究链路

`bailian_multimodal_vector_chain.v1` 复用 `public_model_runner.v2`，在冻结的 `dso-multimodal-vector-value-20260719-r1` 上按以下顺序运行：

```text
240 条冻结样本
  -> qwen3-vl-embedding：text + text/image fusion，2560 维
  -> cosine 全量初召回
  -> qwen3-vl-rerank：只重排 Top-N 结构化候选
  -> 与冻结可见互动代理做客观成对结果对照
  -> 与 frozen v2.4 成对结果生成真实选择 disagreement queue
  -> qwen3.7-plus 主 Judge + qwen3.6-flash 成本 Challenger（盲于两侧策略选择/分差）
  -> research_only 质量/费用/延迟报告
```

- 云端向量继续写通用 `embedding_records`，但使用独立 `model_name=aliyun:qwen3-vl-embedding`、`model_version`、`source_hash` 和 `data/cache/bailian_embeddings/`，不会覆盖本地 2048 维 Qwen 记录。
- 向量输入只含标题和现有语义字段；不含互动标签、`reward_proxy`、Gold 身份或历史策略分。Fusion 最多上传三张 manifest 内代表帧，不上传原视频。
- `preflight` 阶段在不解析 API Key、不构造网络客户端的情况下，使用真实本地帧完成 JPEG/尺寸/Base64/schema/请求字节和最坏预算校验。长边超过 1280px 或单图超过 1MB 时，FFmpeg 只在 `data/cache/bailian_embeddings/normalized_frames/` 生成源 SHA 绑定的派生 JPEG，原始素材保持不变。
- Rerank 当前以融合向量召回 Top-N，再用结构化文本重排；Provider Adapter 具备受限图片输入 contract，但冻结首轮不批量上传 Top-N 图片。
- v2.4 pair 选择来自与 manifest SHA-256 绑定的冻结侧车；缺失、SHA 不匹配或覆盖不完整时，分歧比较 fail closed，Judge 返回 `not_ready` 且不发起网络请求。
- 同一侧车的 `proxy_choice` 只在云端排序完成后进入 `outcome_proxy_comparison`，不会进入 Embedding、Rerank 或 Judge 输入。该指标是账号内校正的抓取时点可见互动代理，不是播放量、曝光或关注转化。为避免冻结 pair 左右朝向不均衡，主指标按真实结果侧做 macro-average；原始准确率、结果侧分布和多数类基线同时报告。客观门禁至少需要 40 个完整 pair，且云端平衡准确率需较 v2.4 高 `5pp`；通过也只生成正向研究信号，不自动晋级生产。

`bailian_cached_signal_ablation.v1` 在上述链路之后提供 D12-A 零网络归因层：

- 只读取冻结 manifest、SHA 匹配的 v2.4 侧车、现有 `embedding_records` 向量文件和 `rerank-latest.json`；函数不构造 Provider Runtime，报告固定记录 `network_request_count=0` 和 `effective_cost_cny=0`。
- 消融矩阵比较 Text/Fusion cosine、高低互动平衡参考池、每侧 Top-3/5/10、现有缓存 Rerank、Embedding/Rerank 融合，以及经 median absolute pair delta 归一化后的 v2.4 低权重融合。
- 每个配置只和它实际覆盖的同一 pair 子集上的 v2.4 比较，输出结果侧平衡命中、原始命中、低互动避让、分层 bootstrap 95% 区间、账号/类别/互动差距切片和诊断 pair，禁止拿全量基线与不完整云子集直接相减。
- 扩到新 60-pair 冻结集前，至少要求 40 个可比 pair、相对 v2.4 `+2pp`、低互动避让不下降、3 个样本量充足账号和 2 个素材类别改善。即使满足也只得到 `eligible_for_60_pair_expansion`；同一 40 对上的权重搜索明确带选择偏差，不能直接晋级生产。
- 参考池必须同时具备高/低互动云向量，Rerank 只使用两侧等量样本，多余一侧明确排除；缺任一侧直接拒绝评测。审核分歧仍允许 `tie`，但客观结果只有左右高低侧时按云分差正负二选一，避免把审核弃权阈值误当结果分类阈值。
- `rerank --limit N` 按完整 A/B pair 选择评测样本，不允许用散列样本凑限额；报告同时保留 pair 数、云端/v2.4 正确数、准确率差、缺失语义和是否满足样本量。
- Judge 最多处理 20–40 条真实选择分歧 pair，左右各最多一张代表帧；`pairwise-input.v2` 不传入 v2.4/cloud 选择或分差，避免裁判被待比较策略锚定。两种 Judge 读取相同盲裁输入并各自记录缓存、usage、费用和失败降级。
- Web 的离线预检最多 40 条且不要求公网门禁；真实调用仅允许最多 10 条 Smoke 或 40 条有界批次，避免长任务阻塞健康检查。全量执行通过可续跑 CLI。任何阶段缺门禁、超预算、schema 失败或网络不可用都保留本地 v2.4，不写人工 Gold、生产权重、审核状态、导出或发布。

`bailian_independent_holdout_validation.v1` 提供 D12-B 不可变独立复验：

- 固定 `pair-001..040` 为校准集、`pair-041..060` 为留出集，禁止样本交叉；固定 Text ref20/K3、Embedding/Rerank 50/50 和 v2.4/cloud 85/15，不在留出集搜索配置。
- 校准集冻结 median absolute pair delta 尺度。配置 artifact 只保留 v2.4 预测和信号分差；盲预测 artifact 拒绝结果代理、互动标签、归一化奖励和 anchor/control 身份字段，并以 SHA-256 防覆盖。
- Rerank 支持 `include_outcomes=false` 与独立 report stage；盲预测阶段不会生成 `outcome_proxy_comparison`。预测 SHA 冻结后，评估阶段才关联可见互动代理。
- 单实验硬上限为 `10.00 CNY`。零网络 preflight 汇总 Embedding/Rerank 最坏预留，真实 Runtime 另把共享 50 元批次额度收紧到 10 元；失败可复用逐条缓存恢复。
- 结果继续为 `research_only`。独立增量、低互动避让和账号门禁不通过时保留 v2.4，不调用 Judge、不写生产权重、Gold、审核、导出或发布。

`bailian_holdout_failure_attribution.v1` 提供 D12-C0 零网络失败归因：

- 先校验 manifest、配置、盲预测和揭盲评估 SHA，再读取既有 Text/Fusion 向量与 `holdout-rerank`；不构造 Provider Runtime，固定写入 `network_request_count=0` 和 `effective_cost_cny=0`。
- 逐 pair 区分 Cloud 纠错、Cloud 误导、共同错误和共同正确，报告 15% 固定融合的决策弹性；后验权重网格只解释翻转阈值，明确禁止作为调参或晋级结果。
- 检查高低参考池标签/账号分布、Top1 表现标签与内容分类一致、同账号参考覆盖、标题近重复、Text/Fusion 差异和视觉源时间覆盖。
- 报告只写独立研究 artifact，不改生产排序、人工 Gold、审核、导出或发布。同一 60 对不得因归因结果重新搜索权重。

`bailian_evidence_quality_reconstruction.v1` 提供 D12-C1 证据质量重构：

- 从冻结 manifest 的原视频路径确定性生成 15 秒 hook / middle / payoff 三窗口，每窗口保留一张真实时间帧；短视频允许窗口重叠，但三个代表时点必须不同。缓存记录源视频 SHA、帧 SHA、窗口时间和 `dso-bailian-vector-input.d12c1` source hash，不覆盖旧 Fusion 向量。
- 高、低互动参考分开检索；账号、节目或素材形态层级只有在 high/low 两侧同时存在时才可共同使用，否则双侧一起降级，禁止一侧账号证据、一侧全局证据。
- `fusion_d12c1` 是预留的新模态标识。证据包构建和缓存对照不会自动创建公网 Runtime；只有显式后续批次才能生成新向量。
- Gate 同时要求三时点覆盖、双侧参考、语境覆盖、新 Fusion 覆盖和分层策略不劣于全局。任一条件未通过时保持 `research_only` 和 v2.4。
- 当前冻结参考池即使全部补齐，同账号 high/low 覆盖上限也只有 65%；若目标门槛为 80%，必须冻结新参考池版本，不能原地改写当前 manifest。

## 5. 核心数据流

### 5.1 双入口标准化候选

G1 已切短片使用 `precut_batch.v1`：批量任务先按 `account_id + SHA-256` 去重，每个源文件只创建一个 `standard_candidate.v1` 候选。候选固定为 `start_time=0`、`end_time=source.duration_seconds`、`candidate_origin=precut`、`boundary_locked=1`，数据库触发器禁止修改开始、结束、时长或解锁。特征提取失败时保留条目级降级说明，仍可用标题和已有确定性特征进入共享 scorer；不会伪造 ASR、音频或多模态结果。

G2 完整节目继续由 Segment Generator 召回多个候选，写入同一个 `candidate_segments` contract，标记 `candidate_origin=generated`、`boundary_locked=0`。两类候选从这里开始共同使用 `score_segment`、历史证据 ranker、解释、质量 Gate、审核、导出、平台映射和指标回流。

```text
多条已切短片 -> 批次/内容哈希去重 -> 每文件一个锁边候选 -+
完整节目 -> 特征抽取 -> 候选召回/边界吸附 -> 多个候选 -----+-> standard_candidate.v1
                                                               -> 统一 scorer/ranker/review/feedback
```

### 5.2 上传长视频

输入：

- 本地视频文件。
- 素材标题。
- 账号/垂类。
- 可选备注：内容主题、目标人群、视频来源。

输出：

- `source_video` 记录。
- 标准化视频文件。
- 音频文件。
- 抽帧目录。

### 5.3 特征抽取

生成：

- ASR 文本和句级时间戳。
- 关键帧。
- 音频能量、静音段、语速。
- 可选 OCR 文本。
- 文本/视觉/音频 embedding。

MVP 优先级：

1. ASR + 句级时间戳。
2. 音频能量和静音段。
3. 每 2-3 秒抽关键帧。
4. 文本 embedding。
5. 图像 embedding。
6. OCR。

### 5.4 候选片段生成

候选片段来源：

- ASR 语义段落。
- 句子窗口滑动。
- 音频峰值附近。
- 明显停顿/转折处。
- LLM 识别的金句、冲突、反转、结论。

默认候选长度：

- 短切片：15-25 秒。
- 中切片：25-45 秒。
- 长切片：45-90 秒。

过滤规则：

- 低于 8 秒默认丢弃。
- 超过 120 秒默认不作为短视频切片。
- 没有完整语义闭环的片段降权。
- 开头 3 秒无信息增量的片段降权。

### 5.5 多模态缓存

每个候选片段缓存：

```text
text_embedding
visual_embedding
audio_embedding
cover_embedding
ocr_embedding
multimodal_embedding
```

第一版可以只存：

```text
text_embedding
cover_embedding
multimodal_embedding = weighted_average(text, cover)
```

Qwen3-VL 通用历史向量继续写入 `embedding_records`，与旧 `clip_embeddings` 分离。批量构建可显式传入不超过 500 个 `entity_ids`；Scheduler 在入队前按 `entity_type + entity_id + modality + model + source_hash` 复用已存在的 2048 维缓存，避免冻结实验退化为全库重算。

百炼 `qwen3-vl-embedding` 使用同一表但独立模型名、2560 维与 `text/fusion` modality；缓存键同时绑定 frozen manifest SHA-256、输入 contract、模型、维度、样本 ID、语义摘要和代表帧 SHA-256。模型、维度、提示词或媒体变化后不会误用旧向量。

`multimodal_vector_value.v1` 使用独立冻结 contract：60 组盲审样本与高/低互动参考池在 `sample_id / platform_item_id / stable_title_key` 上互斥；评测样本只作为查询向量，互动标签只来自参考池。盲审结果独立写入 `multimodal_vector_reviews`，不写 `material_gold_annotations`、历史语义字段或候选分数。媒体读取 API 只能通过 manifest 中的 `benchmark_id + task_id + side` 解析 `data/douyin_media_assets` 内文件，不接受任意本地路径。

### 5.6 评分和排序

音乐综艺第一版使用短视频切片专用评分：

```text
music_variety_slice_score =
  0.13 * short_video_hook_score
  + 0.12 * musical_moment_score
  + 0.11 * narrative_context_score
  + 0.10 * chorus_climax_score
  + 0.10 * lyric_resonance_score
  + 0.09 * performer_stage_score
  + 0.09 * audience_reaction_score
  + 0.08 * comment_trigger_score
  + 0.07 * song_recognition_score
  + 0.06 * novelty_arrangement_score
  + 0.05 * production_quality_score
  - 0.20 * rights_risk_score
  - 0.10 * low_originality_score
```

MVP 中没有足够历史数据时：

- `history_match_score` 默认使用同账号人工标签或全局规则。
- `short_video_hook_score` 先用标题、前 3 秒画面/字幕、节目上下文完整度判断。
- `narrative_context_score` 先由 ASR 对话、歌词和反应镜头共同判断。
- `comment_trigger_score` 先由标题、节目观点、歌词共鸣和改编差异判断。
- `song_recognition_score` 先由人工填写歌曲热度、歌手热度和节目曝光。
- `chorus_climax_score` 先用音频能量、歌词重复和人工校正结合。
- `lyric_resonance_score` 先由 LLM 判断歌词情绪和传播语境。
- `audience_reaction_score` 先用画面切换、掌声/欢呼峰值和人工校正。
- `rights_risk_score` 必须优先依赖授权元数据，缺失时提高风险分。

### 5.7 生成切片版本

每个候选片段可以生成多个版本：

- 不同标题。
- 不同封面帧。
- 不同字幕风格。
- 不同开头裁切点。
- 不同结尾停顿点。

MVP 只生成建议，不自动批量发布。

### 5.8 表现数据回流

MVP 先手动导入 CSV 或表单录入：

```text
published_at
title_used
duration_seconds
views
impressions
avg_watch_seconds
avg_watch_ratio
five_second_retention
completion_rate
likes
comments
favorites
shares
follows
negative_feedback
```

导入后更新：

- 账号整体基线。
- 主题 cluster 分数。
- 小时表现分数。
- 标题模板表现。
- 开头类型表现。
- 长度区间表现。
- reward label 和模型校准样本。
- 训练/验证/回测数据切分。

推荐的发布后 reward proxy：

```text
reward_proxy =
  0.22 * five_second_retention
  + 0.18 * avg_watch_ratio
  + 0.15 * completion_rate
  + 0.10 * rewatch_rate
  + 0.10 * comment_quality_rate
  + 0.08 * favorite_rate
  + 0.07 * share_rate
  + 0.05 * follow_rate
  + 0.05 * related_content_continue_watch
  - 0.18 * negative_feedback_rate
  - 0.25 * rights_or_policy_risk
```

训练标签必须按账号基线、切片长度、发布时间窗口、曝光量和内容类型做归一化。播放量只能作为辅助观察，不作为唯一优化目标。

后续平台数据回流按阶段推进：

- V0.3.1：CSV 是唯一生产可用回流路径，导入报告必须标明 linked/unlinked/skipped 和训练资格。
- V0.4：补平台视频映射、数据来源字段和 fake client 字段映射，允许运营把本地 variant/experiment 绑定到平台侧 `item_id/video_id`。
- V0.5：接入抖音开放平台只读接口，经过授权后同步账号视频列表和表现数据；仍不做自动发布、自动互动或无人值守账号操作。
- API 没覆盖的指标继续允许 CSV 补齐，所有训练样本必须保留 `sample_source` 和来源可信度。

## 6. 数据模型

### 6.1 source_videos

```text
id
account_id
title
file_path
duration_seconds
status
transcript_path
input_mode                 # program | precut
content_hash               # precut 使用 SHA-256
import_batch_id
created_at
updated_at
```

### 6.2 candidate_segments

```text
id
source_video_id
performance_id
start_time
end_time
duration_seconds
transcript
summary
primary_topic
song_section_type
music_slice_type
emotion_type
candidate_origin           # generated | precut
boundary_locked
boundary_strategy
boundary_confidence
source_content_hash
import_batch_id
candidate_contract_version # standard_candidate.v1
status
created_at
```

### 6.2.1 precut_import_batches / precut_import_items

`precut_import_batches` 保存账号、批次状态、创建/复用/失败/处理计数和 `precut_batch.v1` 版本；`precut_import_items` 保存原文件名、顺序、内容哈希、源视频、锁边候选、处理状态、错误和降级说明。批次允许部分失败、后台继续处理和显式重试，不因单条坏文件回滚其他已成功条目。

### 6.3 songs

```text
id
title
original_artist
composer
lyricist
is_original_for_program
recognition_level
rights_status
created_at
```

### 6.4 performances

```text
id
source_video_id
song_id
performer_name
episode
start_time
end_time
stage_type
arrangement_notes
rights_status
created_at
```

### 6.5 music_segments

```text
id
performance_id
start_time
end_time
section_type
energy_level
vocal_intensity
chorus_probability
climax_probability
lyric_text
emotion_label
created_at
```

### 6.6 clip_embeddings

```text
id
candidate_segment_id
embedding_type
model_name
vector_path
vector_dim
created_at
```

### 6.7 slice_scores

```text
id
candidate_segment_id
short_video_hook_score
musical_moment_score
narrative_context_score
chorus_climax_score
lyric_resonance_score
performer_stage_score
audience_reaction_score
comment_trigger_score
song_recognition_score
novelty_arrangement_score
history_match_score
production_quality_score
rights_risk_score
low_originality_score
final_score
score_explanation
created_at
```

### 6.8 rights_clearance

```text
id
asset_type
asset_id
program_rights_status
song_rights_status
performance_rights_status
artist_portrait_status
platform_license_scope
allowed_clip_duration
allowed_publish_accounts
allowed_publish_platforms
expiration_date
notes
updated_at
```

### 6.9 slice_variants

```text
id
candidate_segment_id
title
cover_time
subtitle_style
export_path
variant_notes
predicted_score
created_at
```

### 6.10 publishing_experiments

```text
id
slice_variant_id
platform
published_at
title_used
hashtags_used
experiment_group
hypothesis
created_at
```

### 6.11 performance_metrics

```text
id
experiment_id
slice_variant_id
candidate_segment_id
window_name
collected_at
views
impressions
avg_watch_seconds
avg_watch_ratio
five_second_retention
completion_rate
likes
comments
favorites
shares
follows
negative_feedback
comment_quality_score
reward_proxy
normalized_reward
uncertainty
sample_source
platform_item_id
metric_semantics
created_at
```

### 6.12 topic_clusters

```text
id
account_id
name
description
centroid_vector_path
long_term_score
recent_score
tail_potential
publish_gap_days
updated_at
```

### 6.13 account_interest_clock

```text
id
account_id
hour
topic_cluster_id
avg_watch_ratio
completion_rate
engagement_rate
sample_count
smoothed_score
updated_at
```

### 6.14 metric_snapshots

```text
id
performance_metric_id
experiment_id
slice_variant_id
candidate_segment_id
window_name
collected_at
hours_since_publish
views
impressions
avg_watch_seconds
avg_watch_ratio
five_second_retention
completion_rate
rewatch_rate
likes
comments
favorites
shares
follows
negative_feedback
comment_quality_score
reward_proxy
normalized_reward
uncertainty
sample_source
platform_item_id
metric_semantics
created_at
```

### 6.15 training_samples

```text
id
candidate_segment_id
slice_variant_id
experiment_id
sample_source
feature_version
label_window
reward_proxy
normalized_reward
account_baseline_snapshot
rights_policy_status
train_split
created_at
```

### 6.16 account_baselines

```text
id
account_id
content_type
duration_bucket
publish_hour
metric_name
median_value
p75_value
p90_value
sample_count
updated_at
```

### 6.17 clip_cluster_assignments

```text
id
candidate_segment_id
topic_cluster_id
confidence
is_primary
assigned_at
```

### 6.18 audience_segments

```text
id
account_id
name
description
centroid_vector_path
historical_reward
sample_count
updated_at
```

### 6.19 clip_audience_predictions

```text
id
candidate_segment_id
audience_segment_id
confidence
cold_start_score
reason
created_at
```

### 6.20 platform_accounts

V0.4 先作为状态壳，V0.5 接真实 OAuth/OpenAPI。

```text
id
platform
account_id
account_role  # unassigned | publishing_target | research_source
platform_account_id
display_name
auth_status
scopes
token_expires_at
last_sync_at
created_at
updated_at
```

### 6.21 platform_video_mappings

```text
id
account_id
platform
platform_item_id
candidate_segment_id
slice_variant_id
experiment_id
platform_url
platform_title
published_at
evidence_scope  # unclassified | target_outcome | research_proxy
sync_status
last_synced_at
last_metrics_at
notes
created_at
updated_at
```

### 6.22 platform_sync_runs

```text
id
platform_account_id
sync_type
window_name
started_at
finished_at
status
error_code
error_message
request_summary
response_summary
imported_metrics
created_snapshots
created_training_samples
```

## 7. 模块设计

### 7.1 Media Ingest

职责：

- 校验视频文件。
- 读取时长、分辨率、帧率、音轨信息。
- 复制到项目媒体目录。
- 生成 source_video 记录。

关键接口：

```python
ingest_video(path: str, account_id: str, title: str) -> SourceVideo
```

可选的远程获取接口先产出本地文件，再复用上述入口：

```python
download_video_resource(url: str, ..., ingest: bool = True) -> dict
```

其返回值包含 provider/版本/许可、策略状态、候选、文件、入库结果与不可变任务目录中的 JSON manifest；不改变候选排名权重。

### 7.2 Feature Extractor

职责：

- 调用 ASR。
- 对长音频做可复现的切块、边界吸附和时间轴合并。
- 把逐块 `text_chars / elapsed / attempts / quality_status` 写入运行清单；请求成功但文本空或可疑时执行受限缩块重试，未恢复则输出 `degraded`，不能伪装为完整转写。
- 抽帧。
- 提取音频特征。
- 生成 embeddings。

关键接口：

```python
extract_features(source_video_id: str) -> FeatureBundle
```

### 7.3 Segment Generator

职责：

- 基于 ASR 时间戳和音频/视觉信号生成候选片段。
- 给出初步片段摘要和标签。

关键接口：

```python
generate_segments(source_video_id: str) -> list[CandidateSegment]
```

### 7.4 Memory Bank

职责：

- 缓存候选片段和历史切片 embedding。
- 提供相似切片召回。

关键接口：

```python
upsert_embedding(segment_id: str, embedding_type: str, vector: list[float]) -> None
search_similar(segment_id: str, top_k: int = 20) -> list[SimilarClip]
```

### 7.5 Slice Scorer

职责：

- 计算所有子评分。
- 给出最终分和解释。

关键接口：

```python
score_segment(segment_id: str) -> SliceScore
```

子模块：

- `hook.py`：开头钩子。
- `retention.py`：留存潜力。
- `engagement.py`：互动潜力。
- `history.py`：历史匹配。
- `trinity.py`：主题长期/长尾分。
- `interest_clock.py`：发布时间匹配。
- `risk.py`：合规和低质风险。

### 7.6 Variant Generator

职责：

- 生成标题建议。
- 推荐封面帧。
- 推荐字幕样式。
- 导出切片。

关键接口：

```python
generate_variants(segment_id: str, count: int = 3) -> list[SliceVariant]
export_variant(variant_id: str) -> str
```

### 7.7 Feedback Updater

职责：

- 导入表现数据。
- 更新账号基线、主题分、时间分、模板分。
- V0.4 起维护平台视频映射和数据来源状态。
- V0.5 起通过授权 API 同步只读表现数据，并记录同步审计。

关键接口：

```python
import_metrics(csv_path: str) -> ImportResult
map_platform_video(variant_id: str, platform_video_id: str) -> PlatformVideoMapping
sync_platform_metrics(account_id: str, window_name: str) -> SyncRunResult
update_feedback_models(account_id: str) -> FeedbackUpdateResult
```

## 8. API 草案

MVP 可以先 CLI，API 为后续前端预留。

```text
POST /videos
GET  /videos
GET  /videos/{id}

POST /precut-batches
GET  /precut-batches
GET  /precut-batches/{id}
POST /precut-batches/{id}/process

POST /videos/{id}/features
POST /videos/{id}/segments
GET  /videos/{id}/segments

POST /segments/{id}/score
GET  /segments/{id}/score

POST /segments/{id}/variants
GET  /segments/{id}/variants
POST /variants/{id}/export

POST /metrics/import
POST /platform-videos/map
POST /accounts/{id}/platform-sync
GET  /accounts/{id}/insights
GET  /accounts/{id}/interest-clock
GET  /accounts/{id}/topic-clusters

GET  /providers/status
GET  /providers/config
POST /providers/config
POST /providers/fake-smoke

GET  /learning/multimodal-vector-experiment/cloud/status
POST /learning/multimodal-vector-experiment/cloud/run
POST /learning/multimodal-vector-experiment/cloud/ablation
POST /learning/multimodal-vector-experiment/cloud/holdout/{freeze|predict|evaluate}
POST /learning/multimodal-vector-experiment/cloud/holdout-attribution
POST /learning/multimodal-vector-experiment/cloud/evidence-quality/rebuild
```

## 9. CLI 草案

```bash
dso ingest ./input.mp4 --account main --title "直播回放 2026-06-23"
dso precut-import ./clips/*.mp4 --account main --batch-title "首轮候选"
dso extract-features <video_id>
dso generate-segments <video_id>
dso score <video_id>
dso suggest <video_id> --top-k 10
dso export <segment_id> --variant 1
dso import-metrics ./metrics.csv
dso insights --account main
dso provider-status
dso provider-smoke --repeat 2
dso bailian-vector-status --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-run --stage smoke --limit 10 --top-n 10 --judge-limit 5
dso bailian-vector-ablation --benchmark-id dso-multimodal-vector-value-20260719-r1
```

## 10. MVP 里程碑

### M0：文档和项目骨架

产物：

- 架构文档。
- Python 项目骨架。
- SQLite 数据模型草案。

验收：

- 能运行 CLI help。
- 能初始化本地数据目录和数据库。

### M1：视频导入和基础特征

产物：

- 视频导入。
- FFmpeg 元数据读取。
- 音频提取。
- ASR 转写。

验收：

- 输入一个视频，生成 transcript。
- 能看到句子级时间戳。

### M2：候选片段生成

产物：

- 基于 transcript 的候选片段生成。
- 基础片段摘要。

验收：

- 输入一个 30-120 分钟视频，输出 10-30 个候选片段。
- 每个片段有 start/end/transcript/summary。

### M3：评分和推荐

产物：

- hook/retention/engagement/risk 规则评分。
- LLM 辅助解释。
- top-k 推荐。

验收：

- 每个片段有分项评分、总分、推荐理由、风险提示。

### M4：导出切片

产物：

- 标题建议。
- 封面帧建议。
- FFmpeg 导出。

验收：

- 能导出可发布的竖屏切片文件。

### M5：数据回流和 Insight

产物：

- CSV 表现数据导入。
- 平台视频映射状态壳。
- 账号基线统计。
- 主题/发布时间/长度区间分析。

验收：

- 导入已发布数据后，系统能输出哪些主题、时间段、长度表现更好。
- 运营能看到哪些指标行已链接到本地候选，哪些只是未链接参考数据。

### M6：平台只读数据同步

产物：

- 抖音账号 OAuth 授权与 token refresh。
- 授权账号视频列表同步。
- 平台视频与本地 variant/experiment 的手动或半自动匹配。
- 6h/24h/72h/7d/30d 表现数据同步。
- 同步审计、限流/权限/解绑/删除状态提示。

验收：

- 不依赖 CSV 也能把已映射视频的表现数据写入 `metric_snapshots` 和 `training_samples`。
- 权限不足或 API 缺字段时，系统明确降级到 CSV 补齐路径。
- 不产生自动发布、自动互动或平台规避行为。

## 11. 第一版优先级

必须做：

- 视频导入。
- ASR。
- transcript-based 候选片段。
- 基础评分。
- top-k 推荐。
- FFmpeg 导出。
- 表现数据导入。

可以延后：

- OCR。
- 图像 embedding。
- 音频深度模型。
- 复杂向量数据库。
- 前端管理台。
- 自动发布：不进入当前路线，只有在独立合规评审、授权审核和人工确认链路成熟后再议。
- 深度排序模型。

## 12. 验收样例

输入：

```text
一个 60 分钟直播回放
账号：财经知识类
目标：生成 10 个可发布切片
```

输出：

```text
候选 1
- 时间：00:13:42 - 00:14:21
- 标题建议：普通人最容易忽略的现金流陷阱
- 分数：86
- 推荐理由：开头 3 秒有问题钩子，中段有具体案例，结尾有明确结论
- 风险提示：标题不要夸大收益承诺
- 建议发布时间：20:00-22:00，基于该账号知识类内容历史表现

候选 2
...
```

## 13. 关键约束

- 不承诺爆款，只提升可控质量和实验效率。
- 不使用虚假互动、刷量、自动化操控。
- 不绕过平台规则。
- 数据少时所有模型输出都要带不确定性。
- 版权和二创授权必须作为风险检查项。
