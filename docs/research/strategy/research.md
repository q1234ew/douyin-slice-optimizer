# 抖音短视频切片流量优化系统研究调研

调研日期：2026-06-23
目标：为“短视频切片在抖音平台获得流量提升的概率”构建一套可测量、可迭代、合规的算法与产品系统。

扩展阅读：

- 更细的论文算法拆解和工程化实现映射见 [algorithm-study.md](algorithm-study.md)。
- MVP 系统架构和模块拆分见 [architecture.md](../../architecture.md)。
- 主线论文复核后的架构修订建议见 [paper-architecture-review.md](paper-architecture-review.md)。
- 音乐综艺短视频切片的专项策略见 [music-variety-strategy.md](music-variety-strategy.md)。

## 1. 核心结论

短视频切片流量优化不应被理解为“破解推荐算法”，而应被设计为一个闭环系统：

```text
长视频素材理解
  -> 高潜片段识别
  -> 多版本切片生成
  -> 发布前流量评分
  -> 小规模发布实验
  -> 数据回流
  -> 排序模型和切片策略迭代
```

从公开资料和推荐系统研究看，短视频分发通常会强依赖用户交互、视频内容信息、观看完成度、观看时长、互动行为、负反馈和安全/质量约束。对切片系统而言，真正可控的变量主要包括：

- 前 3 秒钩子强度
- 信息密度和节奏
- 情绪峰值、冲突点、反转点、金句
- 视频长度与完播概率
- 标题、字幕、封面、话题标签
- 账号垂类与受众匹配度
- 内容原创性、质量和合规风险
- 多版本实验和数据回流速度

## 2. 平台公开信息

### 2.1 TikTok 推荐公开说明

TikTok 官方说明中提到，For You 推荐会综合用户互动、视频信息、设备和账号设置等因素。用户互动包括点赞、分享、评论、关注、观看完整视频、跳过等；视频信息包括字幕、声音、话题标签等。官方也强调完整观看一个较长视频通常是更强的兴趣信号，而设备/账号设置相对权重较低。

参考：

- [How TikTok recommends videos #ForYou](https://newsroom.tiktok.com/en-us/how-tiktok-recommends-videos-for-you)
- [How TikTok recommends content](https://support.tiktok.com/en/using-tiktok/exploring-videos/how-tiktok-recommends-content)

### 2.2 抖音规则和安全边界

抖音有规则中心和安全与信任中心，平台强调内容合规、治理政策、算法透明和违规处理。系统设计中应避免任何刷量、虚假互动、搬运、诱导互动、违规蹭热点、标题党和侵犯版权的策略。

参考：

- [抖音规则中心](https://www.douyin.com/rule/)
- [抖音社区自律公约](https://www.douyin.com/rule/policy)
- [抖音安全与信任中心](https://trust.douyin.com/)

## 3. 高相关度论文清单

### 3.1 S 级：Douyin/字节生产系统论文

| 论文 | 方向 | 与本系统的关系 |
| --- | --- | --- |
| [Make It Long, Keep It Fast: End-to-End 10k-Sequence Modeling at Billion Scale on Douyin](https://arxiv.org/abs/2511.06077) | Douyin 长序列排序 | 直接研究 Douyin 推荐中的 10k 长度用户行为序列建模，涉及排序模型、完播/点击等目标，是当前最贴近抖音推荐排序的论文 |
| [Monolith: Real Time Recommendation System With Collisionless Embedding Table](https://arxiv.org/abs/2209.07663) | 字节系实时推荐系统 | 字节跳动推荐系统论文，强调短视频/广告场景中的实时反馈、稀疏特征、在线训练 |
| [Real-time Indexing for Large-scale Recommendation by Streaming Vector Quantization Retriever](https://arxiv.org/abs/2501.08695) | 实时召回索引 | 字节论文，已部署并替换 Douyin/Douyin Lite 主要召回器，适合理解召回层如何从海量视频里先挑候选 |
| [Next-User Retrieval: Enhancing Cold-Start Recommendations via Generative Next-User Modeling](https://arxiv.org/abs/2506.15267) | 新视频冷启动召回 | 面向 item cold-start，关注新视频如何找到下一批可能互动的人群，对创作者新切片起量尤其关键 |
| [Trinity: Syncretizing Multi-/Long-tail/Long-term Interests All in One](https://arxiv.org/abs/2402.02842) | 多兴趣/长尾/长期兴趣召回 | 字节/抖音召回相关论文，用长期兴趣缓解兴趣遗忘，解释为什么垂类稳定和内容标签清晰有价值 |
| [Interest Clock: Time Perception in Real-Time Streaming Recommendation System](https://arxiv.org/abs/2404.19357) | 时间感知推荐 | 抖音集团 SIGIR 2024 论文，建模用户不同时段兴趣变化，对发布时间和内容场景匹配有参考意义 |
| [LEMUR: Large scale End-to-end MUltimodal Recommendation](https://arxiv.org/abs/2511.10962) | 端到端多模态推荐 | 字节多模态推荐论文，涉及 Douyin Search/广告推荐，说明系统越来越重视视频、文本、语音、图像等多模态理解 |

### 3.1.1 S 级补充：2025-2026 排序架构与音乐方向

| 论文 | 方向 | 与本系统的关系 |
| --- | --- | --- |
| [RankMixer: Scaling Up Ranking Models in Industrial Recommenders](https://arxiv.org/abs/2507.15551) | 大规模排序模型 | 用统一、高并行 feature interaction 替代手工特征交叉，对后续可学习 reranker 有参考价值 |
| [MixFormer: Co-Scaling Up Dense and Sequence in Industrial Recommenders](https://arxiv.org/abs/2602.14110) | 序列和稠密特征联合建模 | 提醒我们不要把账号历史序列、内容特征、时间场景割裂成完全独立打分器 |
| [HyFormer: Revisiting the Roles of Sequence Modeling and Feature Interaction in CTR Prediction](https://arxiv.org/html/2601.12681) | 长序列与异构特征统一架构 | 对 Mini-STCA 后续升级有启发：候选内容、历史序列、非序列特征应深层交互 |
| [MDL: A Unified Multi-Distribution Learner in Large-scale Industrial Recommendation through Tokenization](https://arxiv.org/html/2602.07520) | 多场景/多任务 token 化 | 适合指导我们将发布场景、切片类型、目标行为拆成 scenario/task tokens |
| [Compute Only Once: UG-Separation for Efficient Large Recommendation Models](https://arxiv.org/abs/2602.10455) | 用户侧计算复用 | 对批量给同一期节目多个候选打分有启发：账号历史和节目上下文应复用计算 |
| [MSN: A Memory-based Sparse Activation Scaling Framework](https://arxiv.org/abs/2602.07526) | 个性化记忆与稀疏激活 | 可作为后续账号级个性化 memory bank 的远期参考 |
| [Long-Term Interest Clock](https://arxiv.org/abs/2501.15817) | 长期时间兴趣 | 比 Interest Clock 更细，适合后续把发布时间与长期音乐偏好结合 |
| [MuChator: Enabling Active Music Discovery via Conversational Music LLMs in Douyin Music](https://arxiv.org/abs/2605.27103) | 音乐知识、音乐意图、偏好对齐 | 对音乐综艺切片尤其相关：歌曲切片要理解歌词、场景意图和个性化偏好，不只是识别副歌 |

### 3.2 S 级：通用视频推荐与短视频数据集

| 论文 | 方向 | 与本系统的关系 |
| --- | --- | --- |
| [Deep Neural Networks for YouTube Recommendations](https://research.google.com/pubs/archive/45530.pdf) | 工业级视频推荐 | 经典“召回 + 排序”两阶段架构，可映射为“候选切片召回 + 切片排序” |
| [Recommending What Video to Watch Next: A Multitask Ranking System](https://research.google/pubs/recommending-what-video-to-watch-next-a-multitask-ranking-system/) | 多目标视频排序 | 同时建模多种目标，适合预测完播、点赞、评论、收藏、关注等指标 |
| [MicroLens: A Content-Driven Micro-Video Recommendation Dataset at Scale](https://arxiv.org/abs/2309.15379) | 短视频多模态推荐数据集 | 包含标题、封面、音频、视频等原始模态，最贴近短视频内容理解 |
| [KuaiRand: An Unbiased Sequential Recommendation Dataset with Randomly Exposed Videos](https://arxiv.org/abs/2208.08696) | 短视频随机曝光与去偏 | 快手短视频推荐数据集，适合研究曝光偏差、随机探索、序列推荐 |
| [KuaiRec: A Fully-observed Dataset and Insights for Evaluating Recommender Systems](https://arxiv.org/abs/2202.10842) | 短视频推荐评估 | 用全观测数据研究离线评估偏差，提醒我们不要过度相信普通历史日志 |

### 3.3 A 级：切片生成和流量预测相关

| 论文 | 方向 | 与本系统的关系 |
| --- | --- | --- |
| [Counteracting Duration Bias in Video Recommendation via Counterfactual Watch Time](https://arxiv.org/html/2406.07932v1) | 观看时长偏差 | 避免把“长视频天然观看时长更高”误判为内容更好 |
| [Deconfounding Duration Bias in Watch-time Prediction for Video Recommendation](https://dl.acm.org/doi/10.1145/3534678.3539092) | 时长去偏 | 指导完播率、平均观看比例、归一化观看时长等指标设计 |
| [Unleash the Potential of CLIP for Video Highlight Detection](https://openaccess.thecvf.com/content/CVPR2024W/ELVM/html/Han_Unleash_the_Potential_of_CLIP_for_Video_Highlight_Detection_CVPRW_2024_paper.html) | 高光片段检测 | 可用于长视频中识别高潜力片段 |
| [Mr. HiSum: A Large-scale Dataset for Video Highlight Detection and Summarization](https://proceedings.neurips.cc/paper_files/paper/2023/file/7f880e3a325b06e3601af1384a653038-Paper-Datasets_and_Benchmarks.pdf) | 高光检测和摘要数据集 | 适合评估高能片段识别能力 |
| [Agent-based Video Trimming](https://arxiv.org/html/2412.09513v1) | 自动剪辑 | 从“选片段”扩展到“删除废片、组织片段、构建短视频叙事” |
| [Delving Deep into Engagement Prediction of Short Videos](https://arxiv.org/html/2410.00289v1) | 短视频互动预测 | 适合做发布前的互动和留存评分 |
| [Large Language Models Are Natural Video Popularity Predictors](https://aclanthology.org/2025.findings-acl.597.pdf) | LLM/VLM 流行度预测 | 说明将多模态内容转为文本解释后，LLM 可参与流行度预测 |

### 3.4 B 级：可借鉴的模型组件

| 论文 | 方向 | 与本系统的关系 |
| --- | --- | --- |
| [MMoE: Modeling Task Relationships in Multi-task Learning](https://dl.acm.org/doi/10.1145/3219819.3220007) | 多任务学习 | 可同时预测完播率、点赞率、评论率、转粉率等目标 |
| [ESMM: Entire Space Multi-Task Model](https://arxiv.org/abs/1804.07931) | 转化链路建模 | 对“曝光 -> 观看 -> 互动 -> 关注/转化”的链路建模有启发 |
| [DeepFM: A Factorization-Machine based Neural Network for CTR Prediction](https://arxiv.org/abs/1703.04247) | CTR/排序模型 | 适合作为结构化特征排序模型参考 |
| [DIN: Deep Interest Network for Click-Through Rate Prediction](https://arxiv.org/abs/1706.06978) | 用户兴趣建模 | 可借鉴做账号受众兴趣匹配 |
| [DIEN: Deep Interest Evolution Network for Click-Through Rate Prediction](https://arxiv.org/abs/1809.03672) | 兴趣演化建模 | 适合处理热点变化、受众偏好漂移 |

## 4. 论文到系统的映射

### 4.1 候选切片召回

对应研究：

- YouTube 两阶段推荐
- Streaming VQ Retriever
- Next-User Retrieval
- Trinity
- Highlight Detection
- Video Summarization
- Video Trimming

系统实现：

- 对原视频做 ASR，生成逐字或逐句时间戳。
- 按语义段落、镜头变化、音频能量和字幕断句切分候选窗口。
- 提取候选片段特征：文本、画面、音频、说话速度、情绪、关键词、冲突点。
- 召回策略先规则化，不必一开始训练模型。

候选片段召回特征示例：

- 金句密度
- 情绪波动
- 冲突词/反转词
- 画面变化率
- 音量峰值
- 语速变化
- 主题完整度
- 是否包含明确问题、结论、反转或利益点

### 4.2 切片排序与流量评分

对应研究：

- Make It Long, Keep It Fast
- YouTube 多目标排序
- MMoE
- DeepFM
- LEMUR
- Interest Clock
- 短视频互动预测

建议第一版评分项：

```text
final_score =
  0.20 * hook_score
  + 0.20 * retention_score
  + 0.15 * engagement_score
  + 0.15 * topic_match_score
  + 0.10 * novelty_score
  + 0.10 * production_quality_score
  - 0.10 * risk_score
```

评分项定义：

- `hook_score`：前 3 秒是否有问题、冲突、反常识、结果预告、强情绪。
- `retention_score`：中段是否持续提供信息增量，是否有悬念和节奏变化。
- `engagement_score`：是否天然引发评论、收藏、转发或二次讨论。
- `topic_match_score`：是否匹配账号垂类、历史高表现主题和目标人群。
- `novelty_score`：是否避免与账号近期内容高度重复。
- `production_quality_score`：画质、收音、字幕可读性、裁切稳定性。
- `risk_score`：搬运、低质、诱导互动、敏感内容、标题党、版权风险。

### 4.3 观看时长与完播率去偏

对应研究：

- Duration Bias
- Counterfactual Watch Time

系统启发：

不能只用原始观看时长作为优化目标。短视频切片长度不同，天然会导致原始观看时长和完播率不可直接比较。

建议记录和优化这些指标：

- `avg_watch_seconds`：平均观看秒数。
- `avg_watch_ratio`：平均观看比例。
- `completion_rate`：完播率。
- `five_second_retention`：5 秒留存率。
- `rewatch_rate`：复看率。
- `normalized_watch_score`：按视频长度、账号基线、内容类型归一化后的观看得分。

### 4.4 发布实验与数据回流

对应研究：

- KuaiRand
- Counterfactual Evaluation
- Multi-armed Bandit
- Monolith

系统启发：

短视频效果具有强时效性和强曝光偏差。建议把“发布”当作实验，而不是单次动作。

第一阶段可用人工 A/B：

- 同一素材切 3-5 个版本。
- 只变一个变量：开头、标题、封面、长度、字幕风格或话题。
- 记录发布时间、账号状态、版本差异和表现数据。

第二阶段可用轻量 Bandit：

- 用 Thompson Sampling 或 UCB 在多个候选版本之间分配发布机会。
- 早期探索更多版本，后期集中投入高胜率模板。
- 奖励函数不要只看播放量，应看留存和互动的组合指标。

### 4.5 Douyin/字节论文对切片系统的直接启发

| 论文 | 可落地启发 |
| --- | --- |
| Make It Long, Keep It Fast | 切片评分不应只看单条素材，还应利用账号长期历史表现，建立“账号受众长期兴趣画像” |
| Monolith | 新发布切片的实时反馈很重要，系统要尽快回收 5 秒留存、完播、互动和负反馈 |
| Streaming VQ Retriever | 候选切片库需要高效召回，可以按主题、文本向量、视觉向量、账号垂类建立索引 |
| Next-User Retrieval | 新切片冷启动时，应主动推断“可能互动的人群画像”，而不是只依赖粉丝画像 |
| Trinity | 账号内容应兼顾主垂类、长尾主题和长期兴趣，不要只追短期热点 |
| Interest Clock | 发布时间不是玄学，应记录发布时间与内容类型的交互效果，形成账号级时间偏好 |
| LEMUR | 切片理解要从标题标签升级到多模态：字幕、画面、语音、封面、音频节奏都应进入评分 |

## 5. 推荐数据结构

### 5.1 视频素材表

```text
source_videos
- id
- title
- duration_seconds
- file_path
- transcript
- created_at
- source_type
- topic_tags
```

### 5.2 候选片段表

```text
candidate_segments
- id
- source_video_id
- start_time
- end_time
- transcript
- visual_summary
- audio_features
- text_features
- hook_score
- retention_score
- engagement_score
- risk_score
- final_score
```

### 5.3 切片版本表

```text
slice_variants
- id
- candidate_segment_id
- title
- caption_style
- cover_frame_time
- duration_seconds
- export_path
- version_notes
- predicted_score
- status
```

### 5.4 发布实验表

```text
publishing_experiments
- id
- slice_variant_id
- platform
- published_at
- title_used
- hashtags_used
- cover_used
- experiment_group
- hypothesis
```

### 5.5 表现数据表

```text
performance_metrics
- id
- experiment_id
- collected_at
- impressions
- views
- avg_watch_seconds
- avg_watch_ratio
- five_second_retention
- completion_rate
- likes
- comments
- favorites
- shares
- follows
- negative_feedback
```

## 6. MVP 算法路线

### 阶段 1：无训练数据时

目标：自动推荐候选切片，生成评分和理由。

实现：

- FFmpeg：抽帧、转码、切片。
- ASR：生成带时间戳字幕。
- 规则特征：时长、语速、关键词、情绪词、镜头变化、音频能量。
- LLM：判断钩子、冲突、反转、标题、风险。
- 排序：规则加权分。

输出：

- 每条长视频推荐 10-20 个候选片段。
- 每个片段给出评分、推荐理由、风险提示、标题建议。

### 阶段 2：积累 100-300 条发布数据后

目标：从人工规则升级到数据驱动排序。

实现：

- 用 LightGBM/XGBoost 训练切片表现预测模型。
- 标签可以使用组合指标：

```text
reward =
  0.35 * normalized_watch_score
  + 0.20 * completion_rate
  + 0.15 * like_rate
  + 0.15 * comment_rate
  + 0.10 * favorite_rate
  + 0.05 * follow_rate
  - 0.20 * negative_feedback_rate
```

注意：

- 必须按视频长度、发布时间、账号阶段做归一化。
- 不要让播放量单独主导模型，否则会强化曝光偏差。
- 对低曝光样本要标记不确定性，避免误判。

### 阶段 3：积累 1000+ 条发布数据后

目标：做多任务预测和个性化模板推荐。

实现：

- 多任务模型预测完播、点赞、评论、收藏、关注、负反馈。
- 引入账号受众画像、内容主题、近期热点、历史表现模板。
- 使用 Bandit 策略指导多版本实验。

## 7. 风险与约束

### 7.1 算法风险

- 历史数据存在曝光偏差，不能直接等同于真实内容质量。
- 平台推荐机制会持续变化，模型需要定期回测和更新。
- 小样本下模型容易过拟合账号偶然爆款。
- 完播率和观看时长会受视频长度强烈影响。

### 7.2 内容风险

- 标题党可能短期提升点击，但损害后续留存和账号质量。
- 搬运或二创授权不清可能导致限流、投诉或处罚。
- 过度使用争议、情绪和对立话术会带来合规风险。
- 诱导点赞、刷互动、虚假数据属于高风险行为。

### 7.3 产品边界

系统应定位为：

- 辅助识别高潜片段。
- 辅助生成多个内容版本。
- 辅助分析数据和迭代策略。
- 辅助识别风险。

系统不应承诺：

- 保证爆款。
- 绕过平台规则。
- 操控平台推荐。
- 使用虚假互动或自动化刷量。

## 8. 建议阅读顺序

第一批：

1. [Make It Long, Keep It Fast](https://arxiv.org/abs/2511.06077)
2. [Monolith](https://arxiv.org/abs/2209.07663)
3. [Real-time Indexing by Streaming VQ Retriever](https://arxiv.org/abs/2501.08695)
4. [Next-User Retrieval](https://arxiv.org/abs/2506.15267)
5. [Trinity](https://arxiv.org/abs/2402.02842)
6. [Interest Clock](https://arxiv.org/abs/2404.19357)
7. [LEMUR](https://arxiv.org/abs/2511.10962)

第二批：

1. [Deep Neural Networks for YouTube Recommendations](https://research.google.com/pubs/archive/45530.pdf)
2. [Recommending What Video to Watch Next](https://research.google/pubs/recommending-what-video-to-watch-next-a-multitask-ranking-system/)
3. [MicroLens](https://arxiv.org/abs/2309.15379)
4. [KuaiRand](https://arxiv.org/abs/2208.08696)
5. [Counteracting Duration Bias](https://arxiv.org/html/2406.07932v1)
6. [Delving Deep into Engagement Prediction of Short Videos](https://arxiv.org/html/2410.00289v1)

第三批：

1. [Highlight-CLIP](https://openaccess.thecvf.com/content/CVPR2024W/ELVM/html/Han_Unleash_the_Potential_of_CLIP_for_Video_Highlight_Detection_CVPRW_2024_paper.html)
2. [Mr. HiSum](https://proceedings.neurips.cc/paper_files/paper/2023/file/7f880e3a325b06e3601af1384a653038-Paper-Datasets_and_Benchmarks.pdf)
3. [MMoE](https://dl.acm.org/doi/10.1145/3219819.3220007)
4. [DIN](https://arxiv.org/abs/1706.06978)
5. [DIEN](https://arxiv.org/abs/1809.03672)

## 9. 下一步落地建议

优先实现一个不依赖训练数据的 MVP：

1. 上传长视频。
2. 自动转写并切分候选片段。
3. 提取文本、音频、视觉基础特征。
4. 使用规则和 LLM 生成切片评分。
5. 输出推荐片段、标题、封面帧、字幕建议和风险提示。
6. 手动导入发布表现数据。
7. 用表现数据校准评分权重。

最小可用目标：

- 输入 1 条长视频。
- 输出 10 个候选切片。
- 每个候选切片包含：时间段、推荐理由、风险提示、标题建议、评分细分。
- 支持后续录入播放、完播、点赞、评论、收藏、转发、关注等数据。
