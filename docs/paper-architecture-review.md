# 主线论文复核与架构更新建议

复核日期：2026-06-24
目标：阅读 Douyin/字节推荐系统主线论文后，判断当前音乐综艺短视频切片优化系统是否需要更新架构。

结论：需要更新，但不是推倒重来。现有 MVP 的 ingest、extract、generate、score、suggest、export 链路是正确的第一步；下一版架构应补齐“合规数据闭环、训练样本层、多模态 memory bank、可学习排序、时间/主题/账号历史建模、冷启动受众簇”。

## 1. 复核论文范围

本次优先读与抖音当前推荐系统实现最接近的论文：

| 论文 | 关键点 | 对本系统影响 |
| --- | --- | --- |
| [Make It Long, Keep It Fast](https://arxiv.org/abs/2511.06077) | Douyin 10K 长序列排序，STCA、RLB、Train Sparsely Infer Densely | 增加账号历史序列匹配，不只看单条切片 |
| [Monolith](https://arxiv.org/abs/2209.07663) | 在线训练、collisionless embedding、频率过滤、过期机制 | 增加事件日志、指标快照、快速校准 |
| [Streaming VQ Retriever](https://arxiv.org/abs/2501.08695) | 实时索引、item-first、index balancing | 候选切片生成后立即入库和向量索引 |
| [Next-User Retrieval](https://arxiv.org/abs/2506.15267) | 新内容冷启动，用互动用户序列生成 next-user | 用受众簇近似冷启动人群 |
| [Trinity](https://arxiv.org/abs/2402.02842) | 多兴趣、长尾兴趣、长期兴趣统一召回 | 增加主题簇、长尾/长期机会发现 |
| [Interest Clock](https://arxiv.org/abs/2404.19357) / [LIC](https://arxiv.org/abs/2501.15817) | 小时级与长期行为结合的时间兴趣 | 发布时间建议变成账号 x 主题 x 小时 |
| [LEMUR](https://arxiv.org/abs/2511.10962) | 端到端多模态推荐，memory bank | 多模态特征缓存必须成为核心资产 |
| [RankMixer](https://arxiv.org/abs/2507.15551), [MixFormer](https://arxiv.org/abs/2602.14110), [HyFormer](https://arxiv.org/html/2601.12681), [MDL](https://arxiv.org/html/2602.07520) | 大规模排序模型、token 化、多任务/多场景、序列与非序列特征融合 | 下一阶段把规则分拆为 feature tokens 和 task labels |
| [Delving Deep into Engagement Prediction of Short Videos](https://arxiv.org/abs/2410.00289) | 短视频内容特征可预测参与度，NAWP/ECR 比裸播放量更稳 | 引入归一化观看比例和持续参与指标 |
| [MuChator](https://arxiv.org/abs/2605.27103) | Douyin Music 对音乐知识、意图、偏好对齐 | 音乐综艺应增加歌曲/歌词/场景意图层 |

## 2. 架构判断

### 2.1 不需要改变的部分

以下 MVP 能力继续保留：

- 本地优先：FastAPI + Typer CLI + SQLite + 本地文件存储。
- 视频导入、ASR、音频峰值、候选生成、规则评分、授权检查、9:16 导出。
- 人工校正歌曲、表演区间、授权、候选片段。
- 不自动发布、不刷量、不绕过平台规则。

这些是“出片闭环”的必要地基。

### 2.2 必须补齐的部分

论文共同指向一个事实：现代推荐不是单点评分，而是“内容理解 + 历史序列 + 实时反馈 + 多目标排序 + 约束重排”的闭环。当前架构需要补四层：

1. **合规数据与训练样本层**
   - 自有/授权账号数据是训练主数据。
   - 公开视频只做小规模人工研究、趋势先验和弱监督。
   - 所有训练样本记录来源、授权、窗口、特征版本、人工审核状态。

2. **多模态 memory bank**
   - 每个候选切片缓存文本、视觉、音频、封面、OCR、歌词、节目上下文表征。
   - LEMUR 的启发是：多模态表示不能只做离线预训练特征，应尽量贴近排序目标；MVP 先用缓存 + 校准实现。

3. **二阶段候选排序**
   - 第一阶段召回：音乐高能点、叙事结构、反应镜头、历史相似、主题簇、长尾机会。
   - 第二阶段排序：规则分 + reward proxy + 历史匹配 + 时间适配 + 权利风险；数据积累后升级 LightGBM/LambdaRank。

4. **反馈校准与回测层**
   - Monolith 的启发不是马上做在线训练，而是每次导入数据后快速重算基线。
   - 增加 6h/24h/72h/7d/30d 指标快照，避免只看最终播放量。

## 3. 修订后的目标函数

抖音公开算法说明和主线论文都强调多目标行为预测。系统应以“用户长期价值代理指标”来排序候选，而不是以播放量最大化排序。

建议第一版训练/校准 reward：

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

注意：

- `views` 只做归一化参考，不直接当主标签。
- 必须按账号基线、发布时间、切片长度、内容类型、曝光量做归一化。
- 低曝光样本要加 `uncertainty`，避免模型误判。
- 未授权或政策风险高的切片直接进入 export gate，不参与可发布排序。

## 4. 修订后的系统蓝图

```text
Source Video
  -> Ingest / Rights Metadata
  -> ASR + Lyrics + Audio + Frame + OCR
  -> Multimodal Memory Bank
  -> Candidate Generator
       -> music moment windows
       -> narrative windows
       -> reaction windows
       -> comment-trigger windows
  -> Candidate Retrieval
       -> similar high-performing clips
       -> similar low-performing risk clips
       -> topic clusters / long-tail clusters
       -> audience segment cold-start match
       -> time-clock fit
  -> Ranker
       -> rule score
       -> reward proxy score
       -> historical target-to-history score
       -> learned ranker score
       -> rights and policy gate
  -> Suggestion / Variant Generator
  -> Export / Manual Publish
  -> Metric Snapshots
  -> Training Sample Builder
  -> Weekly Calibration / Backtest
```

## 5. 关键模块更新

### 5.1 Mini-STCA：账号历史匹配

Make It Long 的 STCA 用 target-to-history cross attention 处理 10K 用户历史。我们没有用户级历史，但可以把“账号历史已发布切片”作为 history，把当前候选作为 target：

```text
candidate_embedding = multimodal_embed(candidate)
history_embeddings = multimodal_embed(published_clips)
attention_weight =
  softmax(sim(candidate, history) * recency_weight * performance_weight)
history_match_score = sum(attention_weight * normalized_reward(history))
```

新增特征：

- `similar_high_perf_score`
- `similar_low_perf_risk`
- `topic_novelty_score`
- `audience_fit_score`
- `history_uncertainty`

### 5.2 Mini-Trinity：主题簇机会

Trinity 的关键不是追热点，而是防止长期/长尾兴趣被实时热点淹没。音乐综艺切片尤其需要这个能力，因为“经典老歌”“情绪歌词”“导师评价”“歌手故事”可能不是实时热点，但有长期消费价值。

新增指标：

- `cluster_long_term_score`
- `cluster_recent_score`
- `cluster_tail_potential`
- `cluster_under_delivery_score`
- `cluster_publish_gap_days`

候选召回应混合：

- 近期高表现主题。
- 长期稳定主题。
- 长尾高收藏/高评论主题。
- 节目当期热点主题。

### 5.3 Mini-InterestClock：发布时间与场景匹配

Interest Clock 使用小时级偏好并做高斯平滑。我们的版本按账号统计：

```text
time_fit_score(account, clip_type, topic, hour) =
  gaussian_smooth(
    reward(account, clip_type, topic, hour - 2 ... hour + 2)
  )
```

音乐综艺场景建议将小时偏好拆为：

- 歌曲共鸣型。
- 舞台高能型。
- 导师评价型。
- 歌手故事型。
- 争议讨论型。
- 怀旧/经典型。

### 5.4 Mini-NextAudience：冷启动受众簇

Next-User Retrieval 的用户级建模在创作者侧不可得。可替代为受众簇预测：

```text
audience_segments:
  - 情绪共鸣型
  - 音乐技术型
  - 综艺剧情型
  - 怀旧金曲型
  - 歌手粉丝型
  - 争议讨论型
```

每个候选切片预测 top audience segments，并从历史相同受众簇样本估计冷启动表现。

### 5.5 多模态 Memory Bank

LEMUR 说明工业系统需要把原始多模态信息和推荐目标对齐。MVP 先做可缓存、可回测的 memory bank：

```text
clip_embeddings:
  text_embedding
  lyrics_embedding
  visual_embedding
  cover_embedding
  ocr_embedding
  audio_embedding
  multimodal_embedding
  model_name
  feature_version
```

优先级：

1. ASR/歌词文本 embedding。
2. 封面/关键帧 embedding。
3. 音频节奏、RMS、onset、掌声/欢呼 proxy。
4. OCR。
5. 后续才做端到端多模态训练。

### 5.6 Feature Tokens 和 Task Tokens

RankMixer、MixFormer、HyFormer、MDL 的共同趋势是把特征分组为 token，让序列、内容、场景、任务在同一个 backbone 中交互。我们暂不实现大模型，但数据层要按 token 思路设计：

```text
feature_groups:
  content_token: ASR, lyrics, title, hook text
  visual_token: cover, keyframes, closeup, stage
  audio_token: energy, chorus, applause, vocal intensity
  account_token: account baseline, fan active hours
  history_token: similar clips, cluster stats
  scenario_token: publish hour, platform, episode context
  rights_token: license scope, max duration, risk

task_labels:
  five_second_retention
  avg_watch_ratio
  completion_rate
  rewatch_rate
  like_rate
  comment_quality_rate
  favorite_rate
  share_rate
  follow_rate
  negative_feedback_rate
```

这会让后续从规则模型平滑过渡到 LightGBM、LambdaRank 或小型 Transformer reranker。

## 6. 数据表更新

应在现有表基础上增加：

```text
metric_snapshots
- experiment_id
- window_name
- hours_since_publish
- views
- impressions
- avg_watch_ratio
- five_second_retention
- completion_rate
- rewatch_rate
- comment_quality_score
- negative_feedback
```

```text
training_samples
- candidate_segment_id
- slice_variant_id
- experiment_id
- sample_source
- feature_version
- label_window
- reward_proxy
- normalized_reward
- account_baseline_snapshot
- rights_policy_status
- train_split
```

```text
account_baselines
- account_id
- content_type
- duration_bucket
- publish_hour
- metric_name
- median_value
- p75_value
- p90_value
- sample_count
```

```text
clip_cluster_assignments
- candidate_segment_id
- topic_cluster_id
- confidence
- is_primary
```

```text
audience_segments
- account_id
- name
- description
- centroid_vector_path
- historical_reward
- sample_count
```

```text
clip_audience_predictions
- candidate_segment_id
- audience_segment_id
- confidence
- cold_start_score
- reason
```

## 7. 研发优先级

### 立即更新

1. 把公开数据调研结论写进数据治理边界：不做未授权批量爬取训练。
2. 增加 `metric_snapshots` 和 `training_samples` 概念。
3. 将 CSV 导入从单次表现升级为多窗口指标快照。
4. 在评分解释中显示 reward proxy 各项贡献和不确定性。
5. 增加账号历史相似切片召回。

### 第二阶段

1. 增加主题簇和 audience segment。
2. 增加 Mini-InterestClock 发布时间建议。
3. 用 100-300 条自有样本训练 LightGBM/Logistic baseline。
4. 评估 NDCG@10、Top-10 命中率、校准误差、人工排序一致率。

### 第三阶段

1. 做 LambdaRank / RankNet。
2. 做 Mini-STCA 历史匹配模块。
3. 做多任务标签预测。
4. 做版本实验与 bandit 策略。

## 8. 是否需要改 MVP 范围

4 周 MVP 不应膨胀成训练平台。建议保持原验收口径，但补两条：

- 所有导入表现数据必须生成可训练样本，哪怕第一版不训练。
- Top 10 候选排序必须输出“规则分 + 历史相似分 + 时间适配分 + 权利风险 + 不确定性”。

这能让 MVP 既能出片，也不会把后续学习系统的地基漏掉。
