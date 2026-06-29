# 抖音短视频切片优化系统架构设计

设计日期：2026-06-23
阶段：MVP 架构
目标：把论文调研中的推荐系统思想转成一个创作者侧可落地的短视频切片优化系统。

相关文档：

- [research.md](./research.md)：论文与公开资料调研。
- [algorithm-study.md](./algorithm-study.md)：Douyin/字节论文算法拆解。
- [paper-architecture-review.md](./paper-architecture-review.md)：主线论文复核后的架构更新建议。
- [music-variety-strategy.md](./music-variety-strategy.md)：音乐综艺短视频切片专项策略。
- [platform-v0.3-plan.md](./platform-v0.3-plan.md)：多 Agent 进展汇总和 V0.3 平台迭代计划。

## 1. 系统定位

本系统第一阶段以音乐综艺短视频切片为核心对象。歌曲、舞台和歌手是核心素材，但成品不是纯歌曲片段，而是有开头钩子、节目上下文、情绪推进和互动点的短视频内容。

- 从音乐综艺长视频中自动发现高潜短视频片段。
- 为候选切片生成多维评分和解释。
- 输出标题、封面帧、字幕和风险建议。
- 记录发布后的表现数据。
- 用账号历史数据持续校准评分。

第一版不做自动发布、不做刷量、不做模拟互动、不做绕过平台规则的能力。

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
Source Video
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
    research.md
    algorithm-study.md
    architecture.md
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

## 5. 核心数据流

### 5.1 上传长视频

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

### 5.2 特征抽取

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

### 5.3 候选片段生成

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

### 5.4 多模态缓存

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

### 5.5 评分和排序

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

### 5.6 生成切片版本

每个候选片段可以生成多个版本：

- 不同标题。
- 不同封面帧。
- 不同字幕风格。
- 不同开头裁切点。
- 不同结尾停顿点。

MVP 只生成建议，不自动批量发布。

### 5.7 表现数据回流

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
status
created_at
```

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
experiment_id
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
local_account_id
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
platform_account_id
platform
platform_video_id
candidate_segment_id
slice_variant_id
publishing_experiment_id
match_status
match_confidence
matched_by
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

### 7.2 Feature Extractor

职责：

- 调用 ASR。
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
```

## 9. CLI 草案

```bash
dso ingest ./input.mp4 --account main --title "直播回放 2026-06-23"
dso extract-features <video_id>
dso generate-segments <video_id>
dso score <video_id>
dso suggest <video_id> --top-k 10
dso export <segment_id> --variant 1
dso import-metrics ./metrics.csv
dso insights --account main
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
