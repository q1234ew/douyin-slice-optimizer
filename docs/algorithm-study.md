# 最新 Douyin/字节推荐系统论文算法学习笔记

学习日期：2026-06-23
目标：理解与抖音短视频切片流量优化最相关的最新推荐系统实现思路，并沉淀为后续可实现的系统架构。

落地架构见 [architecture.md](./architecture.md)。

## 1. 总体判断

从 2024-2026 年 Douyin/字节相关论文看，当前工业级短视频推荐系统更像一条多模块流水线，而不是单一模型：

```text
内容入库/特征抽取
  -> 多路召回
       -> 实时索引召回
       -> 冷启动 next-user/lookalike 召回
       -> 长期/长尾/多兴趣补充召回
       -> 多模态语义召回
  -> 粗排/精排
       -> 长序列用户行为建模
       -> 多目标预测
       -> 时间感知特征
       -> 多模态内容理解
  -> 重排/约束
       -> 多样性
       -> 风险和质量
       -> 创作者生态
  -> 曝光
  -> 用户反馈回流
  -> 在线训练/实时索引更新
```

对我们的短视频切片系统，最重要的不是复刻 Douyin 的平台级推荐，而是仿照它的思想做一个创作者侧的“小型可学习系统”：

- 用内容多模态特征理解切片。
- 用账号历史表现建模长期受众兴趣。
- 用冷启动思路判断新切片可能适合哪类人。
- 用时间感知特征选择发布时间。
- 用实时反馈快速修正评分。
- 用多目标指标代替单一播放量。

## 2. 最新系统架构理解

### 2.1 召回层：先从海量内容中找候选

相关论文：

- [Real-time Indexing for Large-scale Recommendation by Streaming Vector Quantization Retriever](https://arxiv.org/abs/2501.08695)
- [Next-User Retrieval](https://arxiv.org/abs/2506.15267)
- [Trinity](https://arxiv.org/abs/2402.02842)

召回层解决的问题不是“精确排序”，而是在严格延迟下快速缩小候选池。对平台而言，是从亿级视频中找几万/几千候选；对我们的系统而言，是从长视频和历史切片库中找几十个候选切片。

最新趋势：

- 索引要实时更新，不能等几个小时重建。
- 召回不只看热门，还要覆盖长尾、多兴趣、长期兴趣。
- 新内容冷启动不能只依赖 item ID，要用内容特征和早期互动人群推断潜在受众。

### 2.2 排序层：候选视频和用户历史做深度匹配

相关论文：

- [Make It Long, Keep It Fast](https://arxiv.org/abs/2511.06077)
- [YouTube Multitask Ranking](https://research.google/pubs/recommending-what-video-to-watch-next-a-multitask-ranking-system/)
- [MMoE](https://dl.acm.org/doi/10.1145/3219819.3220007)

排序层的关键是判断“这个用户在这个上下文里对这个候选视频会不会产生目标行为”。Douyin 最新论文强调 10k 级用户历史，说明短视频排序越来越依赖长期行为序列，而不是只看最近几个点击。

对切片系统的启发：

- 切片评分不能只看切片本身，还要看账号历史表现。
- 候选切片应该和历史高表现切片做相似度、差异度、受众匹配。
- 目标不能只设为播放量，要同时预测完播、跳出、点赞、评论、收藏、关注等。

### 2.3 内容理解层：从 ID 推荐走向多模态推荐

相关论文：

- [LEMUR](https://arxiv.org/abs/2511.10962)
- [MicroLens](https://arxiv.org/abs/2309.15379)
- [Highlight-CLIP](https://openaccess.thecvf.com/content/CVPR2024W/ELVM/html/Han_Unleash_the_Potential_of_CLIP_for_Video_Highlight_Detection_CVPRW_2024_paper.html)

传统推荐强依赖 item ID 和历史交互，冷启动、新内容、跨主题泛化都比较难。LEMUR 的方向是把文本、OCR、ASR、标题、封面等原始内容特征端到端并入排序目标。

对切片系统的启发：

- 不应只分析标题和标签。
- 至少要抽取 ASR 文本、画面帧、封面、OCR、音频节奏。
- 多模态 embedding 要缓存成 memory bank，避免每次评分都重算。

### 2.4 时间感知层：发布时间和兴趣不是玄学

相关论文：

- [Interest Clock](https://arxiv.org/abs/2404.19357)

Interest Clock 的核心观点是：用户一天中不同时段的兴趣会变化，简单 hour embedding 在实时流训练中容易过拟合当前时段并导致不稳定。因此它把用户过去 30 天每小时的偏好统计成 24 小时兴趣钟，并用高斯平滑聚合当前时段附近的兴趣。

对切片系统的启发：

- 账号应记录“不同内容类型在不同小时的表现”。
- 发布时间策略应该是账号级、内容类型级的，不是通用最佳时间。
- 早期可以用高斯平滑的小时表现表，不需要复杂模型。

### 2.5 在线学习层：实时反馈比离线经验更重要

相关论文：

- [Monolith](https://arxiv.org/abs/2209.07663)

Monolith 的重点不是某个模型结构，而是实时训练系统：用户行为日志和特征通过流式 join 形成训练样本，模型参数周期性同步到 serving；稀疏 embedding 使用 collisionless hash table，并通过频率过滤和过期机制控制内存。

对切片系统的启发：

- 发布后 5 秒留存、完播、点赞、评论、收藏、转发、关注、负反馈都要尽快回流。
- 早期不需要真正在线训练，但要模拟“快回流”：每次导入数据后更新评分权重和模板表现。
- 对历史内容要设置过期权重，近期表现更重要。

## 3. 核心论文算法拆解

### 3.1 Make It Long, Keep It Fast

论文目标：在 Douyin 排序系统中使用 10k 级用户历史，同时满足线上延迟和成本约束。

关键算法：

1. **STCA：Stacked Target-to-History Cross Attention**

   传统 Transformer 对历史序列做 self-attention，复杂度接近 `O(L^2)`。STCA 认为排序任务最关键的是“候选视频 target 和用户历史 history 的关系”，不是历史中每两个视频之间的关系。因此只让 target 作为 query 去 attend 全历史：

   ```text
   query = target_embedding
   keys, values = history_embeddings
   attention = softmax(query * keys)
   summary = attention * values
   score = ranker(target, summary, context)
   ```

   这样每层复杂度从 `O(L^2)` 变为 `O(L)`，可以承载 10k 级历史。

2. **RLB：Request Level Batching**

   同一个用户的一次请求里通常有多个候选视频。如果每个候选都重复编码一遍用户历史，会浪费巨大。RLB 将同用户同请求的多个 target 聚合起来，共享一次用户历史编码。

   ```text
   user_history_encoding = encode(history)
   for target in request_candidates:
       score[target] = rank(target, user_history_encoding)
   ```

3. **Train Sparsely, Infer Densely**

   训练时不总是用完整 10k 历史，而是用较短窗口随机训练；推理时使用更长历史。论文中训练平均长度约 2k，推理可到 10k。

对我们可仿的版本：

- 不需要一开始训练深度模型。
- 可以把账号历史高表现切片视为 `history`，当前候选切片视为 `target`。
- 用 embedding 相似度、主题相似度、标签差异、表现加权来做轻量 target-to-history attention。

轻量实现：

```text
candidate_embedding = embed(candidate_clip)
history_embeddings = embed(past_clips)
weights = softmax(sim(candidate_embedding, history_embeddings) * recency_weight * performance_weight)
history_match = sum(weights * past_clip_performance)
```

输出特征：

- `similar_high_perf_score`
- `similar_low_perf_risk`
- `topic_novelty_score`
- `audience_fit_score`

### 3.2 Monolith

论文目标：构建能实时学习用户反馈的大规模推荐系统。

关键算法/系统点：

1. **Collisionless Embedding Table**

   推荐系统里用户 ID、视频 ID、作者 ID 极多，哈希碰撞会损害模型。Monolith 使用无碰撞哈希表存储 sparse embedding。

2. **频率过滤和过期机制**

   长尾 ID 如果出现次数太少，embedding 学不稳；很久不用的 ID 也会占内存。系统会过滤低频 ID，并让 stale ID 过期。

3. **在线训练闭环**

   ```text
   用户请求 -> 模型打分 -> 曝光 -> 用户行为
     -> 行为日志 + 特征 join
     -> 训练样本
     -> 在线训练
     -> 参数同步到 serving
   ```

4. **增量参数同步**

   稀疏参数巨大，但短时间只有一小部分 ID 被更新。Monolith 同步 touched keys，而不是全量模型。

对我们可仿的版本：

- 不需要 PS/在线训练集群。
- 建一个事件表，发布数据导入后立即重算评分基线。
- 历史样本加入 recency decay，过旧切片降低影响。
- 对样本少的主题标注“不确定”，不要过拟合。

### 3.3 Streaming VQ Retriever

论文目标：让推荐召回索引实时更新，替代需要小时级重建的 HNSW/DR 类索引。

关键算法：

1. **两阶段结构**

   - indexing step：用 two-tower 生成 user/item embedding，item 通过 VQ 分配到最近 cluster。
   - ranking step：先选择 cluster，再对 cluster 内 item 排序。

2. **实时 item-index assignment**

   item embedding 生成后立刻查最近 cluster，写入参数服务器：

   ```text
   cluster_id = argmin distance(item_embedding, cluster_embeddings)
   item_index[item_id] = cluster_id
   ```

3. **candidate stream**

   只靠曝光流会让新视频和低曝光视频更新不足。论文额外引入 candidate stream，让候选内容以较均匀概率前向更新索引，但不参与有监督 loss。

4. **index reparability**

   论文强调 “item first”：items decide indexes。item embedding 应能随分布漂移更新，再反过来更新 cluster。

5. **index balancing**

   通过 EMA、热度项、低曝光 cluster boost 等机制避免热门视频挤在少数 cluster。

对我们可仿的版本：

- 用普通向量库或 SQLite/pgvector/FAISS 建候选切片索引。
- 每生成一个新候选切片，立即写入向量索引，不等批处理。
- 聚类时不要让热门主题吞掉所有候选，要保留长尾主题 cluster。
- 召回时混合：
  - 高相似历史爆款
  - 新颖长尾主题
  - 同垂类但不同表达方式
  - 冷启动探索候选

### 3.4 Next-User Retrieval

论文目标：解决新视频冷启动，生成“下一个可能互动的用户”表示，再用 ANN/HNSW 检索适合该用户的新 item。

关键算法：

1. **将 item 冷启动转成 next-user generation**

   对一个新视频，历史互动用户很少。论文用最近发生正向互动的用户序列来预测下一个可能互动用户。

2. **输入结构**

   ```text
   prefix prompts = item features, category, ID-like features
   sequential UID embeddings = 最近点赞/评论等正反馈用户序列
   [CLS] token = 生成模式切换和域适配
   ```

3. **Causal Attention**

   用户互动序列有时间方向，模型用 causal attention 生成后续用户。

4. **三种 loss**

   - contrastive loss：生成 next-user embedding，与真实互动用户更相似。
   - cross-entropy loss：利用曝光但未互动样本。
   - auxiliary loss：增强序列 UID 表征，防止表示坍塌。

对我们可仿的版本：

- 我们没有用户级数据，不能真正生成 next user。
- 可以把“用户”抽象为受众簇，例如：
  - 职场成长型
  - 情绪共鸣型
  - 技术干货型
  - 八卦娱乐型
  - 强观点争议型
- 新切片冷启动时，先预测它最像哪几个受众簇，再用历史表现估计冷启动潜力。

轻量实现：

```text
audience_vector = predict_audience(candidate_text, visual_summary, topic_tags)
similar_segments = retrieve_history_by_audience(audience_vector)
cold_start_score = weighted_performance(similar_segments)
```

### 3.5 Trinity

论文目标：统一解决多兴趣、长尾兴趣、长期兴趣，缓解在线学习中的 “interest amnesia”。

核心观点：

- 只看实时样本会忘记低频但真实的长期兴趣。
- 长期行为统计可以揭示多兴趣和长尾兴趣。
- 用统计直方图比在在线模型里硬塞更多 heads 更稳定、更可解释。

关键算法：

1. 建立实时聚类系统，把 item 投射到可枚举 cluster。
2. 将用户长期行为映射成 cluster histogram。
3. 从 histogram 中识别：
   - Trinity-M：多兴趣主题。
   - Trinity-LT：长尾兴趣主题。
   - Trinity-L：长期但近期被遗忘的兴趣。
4. 这些 retriever 作为主召回的补充，提供多样性和长期兴趣恢复。

对我们可仿的版本：

- 给账号历史切片建立主题 cluster。
- 统计每个 cluster 的长期表现、近期表现、发布频率。
- 找出三类机会：
  - 多兴趣：账号有多个稳定高表现方向。
  - 长尾兴趣：发布少但互动/收藏高。
  - 长期兴趣：以前表现好、最近没发。

可落地指标：

```text
cluster_long_term_score
cluster_recent_score
cluster_publish_gap_days
cluster_under_delivery_score
cluster_tail_potential
```

### 3.6 Interest Clock

论文目标：在实时流式训练中建模用户一天内的动态兴趣。

关键算法：

1. 将一天分成 24 个小时桶。
2. 统计用户过去一段时间在每小时的偏好特征。
3. 对当前时刻附近小时做高斯平滑聚合：

```text
interest_clock(now_hour) =
  sum(hour_embedding[h] * gaussian_weight(now_hour, h))
```

为什么要高斯平滑：

- 用户兴趣不会在整点突然变化。
- 直接 hour embedding 在 streaming training 中容易过拟合当前小时。

对我们可仿的版本：

- 对账号记录每个小时的切片表现。
- 按内容类型和小时统计表现。
- 发布建议不是“晚上 8 点最好”，而是：
  - 这个账号的这类内容，在哪些时间段更容易获得完播/互动。

### 3.7 LEMUR

论文目标：把多模态编码器和推荐排序目标端到端联合训练，避免“先预训练多模态模型，再冻结给排序模型”的目标错位。

关键算法：

1. **Raw Features Modeling**

   将 query 和 document/video 的原始文本特征送进 transformer。视频侧特征包括标题、OCR、ASR、封面 OCR 等。

2. **SQDC：Session-masked Query-Document Contrastive Loss**

   用真实点击信号对 query 和 doc 表示做对比学习；同一 session/query 内样本相关性高，所以用 session mask 稳定训练。

3. **Memory Bank**

   历史视频的多模态表示如果每次都重新编码，成本极高。LEMUR 将 document 表示存入 memory bank，用 doc ID 直接取历史多模态表示。

4. **Multimodal Sequential Modeling**

   用户历史文档的多模态表示来自 memory bank，然后通过轻量 decoder、similarity module 和 RankMixer 汇合。

5. **Efficiency Optimization**

   训练时只对一部分样本跑 transformer 前后向，其余样本复用 memory bank 表示；推理时直接查 memory bank。

对我们可仿的版本：

- 每个候选切片生成后，缓存：
  - ASR embedding
  - 标题 embedding
  - 封面/关键帧 embedding
  - OCR embedding
  - 音频节奏特征
- 评分时优先读取缓存，避免重复计算。
- 历史切片和当前候选切片做多模态相似度：

```text
text_sim
visual_sim
audio_sim
cover_sim
topic_sim
overall_multimodal_sim
```

## 4. 面向本项目的拟实现算法架构

### 4.1 MVP 阶段：规则 + 向量 + LLM 评分

适合无训练数据或数据少于 100 条。

```text
Video Ingest
  -> ASR/OCR/Frame/Audio Features
  -> Candidate Segment Generator
  -> Multimodal Embedding Cache
  -> Rule + LLM Scorer
  -> Export Slice Suggestions
  -> Manual Performance Import
```

核心模块：

- `segmenter`：候选片段切分。
- `feature_extractor`：文本、视觉、音频特征。
- `embedding_store`：切片向量缓存。
- `slice_scorer`：规则 + LLM 打分。
- `metrics_importer`：导入发布表现。
- `insight_engine`：按主题、时间、长度、开头类型总结规律。

### 4.2 数据积累阶段：轻量排序模型

适合 100-1000 条发布数据。

使用模型：

- LightGBM / XGBoost / CatBoost。
- 目标是组合 reward，不是单一播放量。

特征：

- 内容特征：主题、情绪、语速、信息密度、长度、字幕密度。
- 多模态特征：文本/视觉/音频 embedding 聚合。
- 历史匹配：与历史高表现/低表现切片相似度。
- 时间特征：发布时间小时、星期、账号 hour clock。
- 冷启动受众：预测受众簇。
- 风险特征：标题党、低质、敏感、搬运风险。

标签：

```text
reward =
  0.30 * normalized_watch_score
  + 0.20 * completion_rate
  + 0.15 * five_second_retention
  + 0.10 * like_rate
  + 0.10 * comment_rate
  + 0.10 * favorite_rate
  + 0.05 * follow_rate
  - 0.20 * negative_feedback_rate
```

### 4.3 进阶阶段：小型 Douyin-like 模块

适合 1000+ 条发布数据。

可以逐步加入：

1. **Mini-STCA**

   当前候选切片作为 target，账号历史切片作为 history，做 target-to-history attention。

2. **Mini-Trinity**

   对账号历史内容聚类，识别多兴趣、长尾兴趣、长期兴趣。

3. **Mini-InterestClock**

   为账号建立小时级内容表现矩阵，提供发布时间推荐。

4. **Mini-NextAudience**

   将真实用户不可得的问题转换为受众簇预测。

5. **Memory Bank**

   缓存所有切片多模态表示，支持快速召回和评分。

## 5. 建议系统数据表扩展

在 `docs/research.md` 的基础上，建议新增这些表/字段。

### 5.1 多模态表示缓存

```text
clip_embeddings
- id
- slice_variant_id
- text_embedding
- visual_embedding
- audio_embedding
- cover_embedding
- ocr_embedding
- multimodal_embedding
- model_name
- updated_at
```

### 5.2 主题聚类

```text
topic_clusters
- id
- account_id
- cluster_name
- centroid_embedding
- long_term_score
- recent_score
- tail_potential
- publish_gap_days
- updated_at
```

### 5.3 切片与主题关系

```text
clip_cluster_assignments
- id
- slice_variant_id
- cluster_id
- confidence
- is_primary
- assigned_at
```

### 5.4 账号兴趣时钟

```text
account_interest_clock
- id
- account_id
- hour
- topic_cluster_id
- avg_watch_ratio
- completion_rate
- engagement_rate
- sample_count
- smoothed_score
- updated_at
```

### 5.5 受众簇预测

```text
audience_segments
- id
- account_id
- name
- description
- centroid_embedding
- historical_reward
- sample_count
```

```text
clip_audience_predictions
- id
- slice_variant_id
- audience_segment_id
- confidence
- cold_start_score
- reason
```

## 6. 推荐实现顺序

第一阶段先做这些，贴近最新系统但保持可落地：

1. **多模态特征缓存**
   - ASR 文本 embedding。
   - 关键帧/封面 embedding。
   - OCR 和音频基础特征。

2. **历史相似切片召回**
   - 当前候选切片召回相似历史切片。
   - 区分相似高表现和相似低表现。

3. **Mini-InterestClock**
   - 按小时和主题统计账号表现。
   - 输出发布时间建议。

4. **Mini-Trinity**
   - 主题聚类。
   - 找出长期没发但历史表现好的主题。
   - 找出长尾高收藏/高评论主题。

5. **发布反馈闭环**
   - 每次导入表现数据后更新主题分、时间分、历史相似分。

第二阶段再做：

1. LightGBM/XGBoost 预测 reward。
2. Mini-STCA 历史匹配特征。
3. Mini-NextAudience 冷启动受众预测。
4. 多版本切片实验策略。

## 7. 当前最贴近 Douyin 系统的工程蓝图

```text
            ┌────────────────────────┐
            │       Source Video      │
            └───────────┬────────────┘
                        │
                        ▼
      ┌──────────────────────────────────┐
      │ ASR / OCR / Frame / Audio Extract │
      └───────────┬──────────────────────┘
                  │
                  ▼
      ┌──────────────────────────────────┐
      │ Candidate Segment Generator       │
      └───────────┬──────────────────────┘
                  │
                  ▼
      ┌──────────────────────────────────┐
      │ Multimodal Memory Bank            │
      └───────┬───────────────┬──────────┘
              │               │
              ▼               ▼
   ┌────────────────┐   ┌────────────────────┐
   │ Similar History │   │ Topic Cluster/Clock │
   │ Retrieval       │   │ Mini-Trinity        │
   └───────┬────────┘   └─────────┬──────────┘
           │                      │
           ▼                      ▼
      ┌──────────────────────────────────┐
      │ Slice Ranker / Scorer             │
      │ - hook/retention/engagement       │
      │ - history match                   │
      │ - cold-start audience             │
      │ - time fit                        │
      │ - risk score                      │
      └───────────┬──────────────────────┘
                  │
                  ▼
      ┌──────────────────────────────────┐
      │ Export Suggestions + Experiments  │
      └───────────┬──────────────────────┘
                  │
                  ▼
      ┌──────────────────────────────────┐
      │ Performance Import / Feedback     │
      └───────────┬──────────────────────┘
                  │
                  ▼
      ┌──────────────────────────────────┐
      │ Update Weights / Clusters / Clock │
      └──────────────────────────────────┘
```

## 8. 关键原则

1. **不要只做内容评分，要做“内容 x 账号历史 x 时间 x 受众”的评分。**
2. **不要只追热点，要保留长期兴趣和长尾兴趣。**
3. **不要用播放量做唯一标签，要用归一化留存和互动组合指标。**
4. **不要每次重算多模态特征，要建立 memory bank。**
5. **不要把冷启动当成玄学，要预测切片适合的受众簇。**
6. **不要把发布时间当成固定经验，要做账号级 Interest Clock。**
7. **不要等数据很多才开始学习，先用规则，后用轻量模型，再做深度模块。**
