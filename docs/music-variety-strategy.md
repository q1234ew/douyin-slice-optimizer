# 音乐综艺短视频切片流量优化策略

制定日期：2026-06-23
目标对象：围绕音乐综艺节目中的歌曲、舞台、人物、反应和节目叙事生成的短视频切片
目标：提升音乐综艺短视频切片在抖音获得推荐流量的概率，同时控制版权、节目素材和艺人权益风险。

相关文档：

- [research.md](./research.md)：推荐系统和短视频论文调研。
- [algorithm-study.md](./algorithm-study.md)：Douyin/字节推荐系统算法拆解。
- [architecture.md](./architecture.md)：MVP 系统架构。

## 1. 目标变化

这里的目标不是“纯歌曲切片”，而是“短视频切片”。歌曲是内容核心和流量锚点，但成品需要具备短视频叙事：开头抓人、中段有情绪/信息推进、结尾有记忆点或互动点。

泛短视频切片通常重点看：

- 开头 3 秒钩子。
- 信息密度。
- 冲突和反转。
- 评论争议点。

音乐综艺短视频切片的核心变量不同，应重点看：

- 歌曲识别度。
- 副歌/高潮/高音/转调/和声/改编记忆点。
- 歌词情绪共鸣。
- 歌手舞台表现和镜头张力。
- 观众、导师、嘉宾反应。
- 歌手故事、赛制悬念、导师评价、节目关系和前后反差。
- 节目叙事上下文。
- 歌曲、表演、节目画面的版权和授权边界。

因此系统要从“文本驱动切片”升级为“音乐结构 + 舞台表现 + 人物叙事 + 现场反应 + 平台数据”的多模态短视频切片系统。

## 2. 合规和版权边界

音乐综艺短视频切片比普通综艺切片风险更高，因为同时涉及：

- 节目视听作品权利。
- 歌曲词曲著作权。
- 表演者权利。
- 录音录像制品或现场录制权利。
- 艺人肖像、姓名、舞台形象权益。
- 节目授权范围和平台分发范围。

中国《著作权法》明确提到，视听作品中的音乐等可以单独使用的作品，作者有权单独行使其著作权；表演者也享有许可他人录音录像、通过信息网络传播其表演等权利。短视频平台规则也要求用户尊重知识产权和平台规则。

参考：

- [中华人民共和国著作权法 - 国家版权局](https://www.ncac.gov.cn/xxfb/flfg/flfg_532/202103/t20210309_50530.html)
- [抖音社区自律公约](https://www.douyin.com/rule/policy)
- [抖音创作者中心](https://creator.douyin.com/)

系统中必须增加 `rights_risk_score`，并记录：

```text
program_rights_status
song_rights_status
performance_rights_status
artist_portrait_status
platform_license_scope
allowed_clip_duration
allowed_publish_accounts
allowed_publish_platforms
expiration_date
```

风险策略：

- 未确认节目和歌曲授权的切片，不进入可发布队列。
- 如果只拥有节目宣传授权，要确认是否覆盖歌曲完整/片段传播。
- 如果只使用抖音平台曲库音乐，要确认只能在平台范围内使用，不能默认跨平台复用。
- 对完整副歌、完整歌曲、长时间连续表演片段提高版权风险分。
- 对“节目画面 + 原声歌曲”的原样搬运提高低原创风险分。

## 3. 音乐综艺短视频切片类型

### 3.1 直入听觉爆点型

适合长度：12-25 秒
典型内容：

- 副歌第一句。
- 高音。
- 转调。
- 和声进入。
- RAP 爆发段。
- 乐队 drop。

优点：

- 听觉钩子强。
- 完播潜力高。
- 容易被反复播放。

风险：

- 如果只有原声搬运，短视频叙事弱，原创度和版权风险较高。

建议：

- 即使从高潮开始，也应加入标题、封面、字幕、反应镜头或上下文信息，让它成为短视频内容，而不是纯音乐截取。

### 3.2 铺垫到高潮型

适合长度：25-45 秒
典型内容：

- 主歌最后一句到副歌。
- 情绪递进到高音。
- 安静段到爆发段。
- 观众反应前后对比。

优点：

- 留存曲线更稳。
- 适合让用户等高潮。

风险：

- 开头如果铺垫太慢，会被划走。

### 3.3 歌词共鸣型

适合长度：15-35 秒
典型内容：

- 失恋、遗憾、成长、亲情、离别、逆袭等强情绪歌词。
- 字幕非常清晰。
- 画面能支撑情绪。

优点：

- 评论率、收藏率可能高。
- 适合标题做情绪引导。

风险：

- 标题煽情过度会伤害可信度。

### 3.4 改编惊喜型

适合长度：20-45 秒
典型内容：

- 老歌新编。
- 节奏、曲风、语言、编曲反转。
- 原唱/经典版本对比。

优点：

- 容易引发讨论。
- 适合做“没想到这首歌还能这样唱”的钩子。

风险：

- 对比原唱时要避免贬损艺人和引战。

### 3.5 舞台反应型

适合长度：15-30 秒
典型内容：

- 导师表情。
- 观众起立。
- 歌手落泪。
- 队友互看。
- 现场安静/爆发。

优点：

- 视觉情绪强。
- 帮助用户理解“这一段为什么值得听”。

风险：

- 反应镜头如果脱离歌曲主体，可能像营销剪辑。

### 3.6 节目叙事型

适合长度：45-90 秒
典型内容：

- 歌手故事。
- 选歌原因。
- 主持/导师一句评价。
- 歌曲高潮。

优点：

- 适合打造人物记忆点。
- 关注转化可能更好。

风险：

- 对完播率要求更高，开头必须强。

## 4. 候选片段生成算法

### 4.1 音乐/对话/反应分段

音乐综艺原视频应先分成三类区间：

```text
dialogue_segment
performance_segment
reaction_segment
```

基础特征：

- 人声语音 ASR。
- 歌声/音乐活动检测。
- 音量 RMS。
- 谱通量 spectral flux。
- 节拍和 onset density。
- 画面切换频率。
- 人脸/舞台镜头类型。

第一版可用规则：

- 连续音乐能量高、节拍明显、ASR 句子较少：performance。
- ASR 句子密集、背景音乐低：dialogue。
- 音乐持续但镜头切导师/观众，伴随欢呼：reaction。

### 4.2 歌曲结构识别

目标是识别：

```text
intro
verse
pre_chorus
chorus
bridge
rap
solo
climax
outro
```

可用算法：

- 基于 chroma / MFCC / tempogram 做结构重复检测。
- 基于 self-similarity matrix 找重复段，重复且能量更高的段候选为副歌。
- 基于 RMS、pitch、spectral flux 找高潮和高音。
- 基于歌词时间戳识别副歌重复歌词。
- 基于镜头和观众反应识别舞台爆点。

相关研究可参考：

- [Audio-Based Music Structure Analysis: Current Trends, Open Challenges, and Applications](https://transactions.ismir.net/articles/10.5334/tismir.54)
- [Supervised Chorus Detection for Popular Music](https://arxiv.org/abs/2103.14253)
- [Video-to-Music Recommendation using Temporal Alignment and Structure-Aware Recommendation](https://arxiv.org/html/2306.07187)
- [A Survey on Multimodal Music Emotion Recognition](https://arxiv.org/html/2504.18799v1)

### 4.3 歌曲爆点检测

候选爆点：

- 副歌开始前 2-5 秒。
- 高音峰值前 3 秒到后 8 秒。
- 转调点。
- 编曲 drop。
- 歌词金句。
- 观众尖叫/掌声峰值。
- 导师反应切镜。
- 歌手表情强变化。

爆点候选窗口：

```text
short_hook_window = [peak_start - 3s, peak_start + 15s]
build_to_peak_window = [peak_start - 12s, peak_start + 20s]
reaction_window = [reaction_start - 5s, reaction_end + 5s]
narrative_window = [story_start, climax_end]
```

### 4.4 歌词和字幕处理

音乐切片里字幕非常重要：

- 歌词字幕要逐句准确。
- 高潮歌词应卡节拍。
- 歌词中强情绪词应参与评分。
- 不要用字幕遮挡歌手表情和舞台动作。

应抽取：

```text
lyric_text
lyric_repetition
lyric_emotion
lyric_topic
lyric_memorability
```

## 5. 音乐综艺短视频切片评分模型

替换泛用评分为音乐综艺短视频切片评分。歌曲爆点是重要信号，但不能压过短视频叙事完整度：

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

### 5.1 short_video_hook_score

衡量前 3 秒是否具备短视频钩子，而不只是音乐进入：

- 是否一上来就给出“为什么要看下去”。
- 是否有字幕、标题或画面提示爆点。
- 是否包含强表情、导师反应、赛制悬念、歌手状态或一句抓人的歌词。
- 是否避免长铺垫和纯空镜。

### 5.2 musical_moment_score

衡量前 3 秒是否立刻抓耳：

- 是否直接进入副歌/高音/强节奏。
- 是否有明显人声辨识度。
- 是否有舞台视觉冲击。
- 是否有一句能单独传播的歌词。

### 5.3 narrative_context_score

衡量切片是否有短视频叙事闭环：

- 用户是否能在 5 秒内知道人物、歌曲或场景。
- 是否有“铺垫 -> 爆点 -> 反应/结果”的结构。
- 是否包含节目关系、导师评价、歌手故事、赛制节点或前后反差。
- 即使没看过正片，也能理解这一段为什么值得看。

### 5.4 chorus_climax_score

衡量是否包含歌曲最可传播的结构段：

- 副歌。
- 高音。
- 转调。
- 反复旋律。
- 合唱/和声。
- drop 或强节奏进入。

### 5.5 lyric_resonance_score

衡量歌词是否适合评论、收藏、转发：

- 情绪：遗憾、怀旧、热血、治愈、释怀。
- 场景：失恋、毕业、亲情、人生低谷、重逢。
- 语言：短句、押韵、可引用。

### 5.6 performer_stage_score

衡量歌手舞台表现：

- 表情感染力。
- 肢体动作。
- 镜头稳定性。
- 人声质感。
- 现场真实感。

### 5.7 audience_reaction_score

衡量现场反馈：

- 观众欢呼。
- 导师表情。
- 其他歌手反应。
- 掌声和静默。
- 弹幕/评论历史反馈。

### 5.8 comment_trigger_score

衡量是否适合引发短视频评论，而不依赖诱导互动：

- 改编是否有讨论空间。
- 导师评价是否与用户感受形成对照。
- 歌手表现是否有明显突破或争议。
- 歌词是否能引发用户讲自己的经历。
- 标题是否能自然提出观点，但不拉踩、不引战。

### 5.9 song_recognition_score

衡量歌曲自带传播势能：

- 经典老歌。
- 热门新歌。
- 节目首唱。
- 原唱/翻唱讨论度。
- 歌手粉丝基础。

### 5.10 novelty_arrangement_score

衡量改编记忆点：

- 曲风变化。
- 语言变化。
- 编曲反转。
- 男女声/组合重构。
- 乐器特色。

### 5.11 rights_risk_score

版权风险：

- 完整歌曲或长段连续副歌。
- 授权状态不明。
- 使用非平台授权音源。
- 节目画面原样搬运。
- 艺人肖像/表演授权不明。

## 6. 数据结构扩展

### 6.1 songs

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

### 6.2 performances

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

### 6.3 music_segments

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

### 6.4 music_variety_slice_scores

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
production_quality_score
rights_risk_score
low_originality_score
final_score
explanation
created_at
```

### 6.5 rights_clearance

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

## 7. 标题和封面策略

标题原则：

- 让用户知道“为什么这 20 秒值得听”。
- 避免绝对化、拉踩、过度营销。
- 不要只写歌名，要写情绪或舞台记忆点。

标题模板：

```text
这句副歌一出来，现场直接安静了
原来这首歌最戳人的不是高音，是这一句
他把这首老歌唱出了新的遗憾感
前面都在铺垫，真正爆点在第 12 秒
这个改编一进来，我才懂为什么导师会抬头
```

封面原则：

- 优先选择歌手强表情、高潮动作、导师反应、舞台大景。
- 封面文字 6-12 字。
- 不遮挡歌手脸和节目关键信息。
- 歌名、歌手名、情绪点三者最多突出两个。

封面文字模板：

```text
副歌封神
高音爆点
唱哭现场
老歌新唱
这一句太痛
导师抬头了
```

## 8. 发布实验设计

同一首歌建议至少生成 4 类版本：

1. **直接高潮版**
   - 从副歌或高音前 1-3 秒开始。
   - 测试完播和复播。

2. **铺垫爆发版**
   - 保留 8-12 秒铺垫。
   - 测试留存曲线和评论。

3. **歌词共鸣版**
   - 强字幕和情绪标题。
   - 测试收藏和评论。

4. **反应证明版**
   - 包含导师/观众反应。
   - 测试点击和停留。

实验指标优先级：

```text
five_second_retention
avg_watch_ratio
completion_rate
rewatch_rate
comment_rate
favorite_rate
share_rate
follow_rate
negative_feedback_rate
```

音乐切片特别要关注：

- `rewatch_rate`：歌声片段可能通过复听放大价值。
- `favorite_rate`：歌词共鸣型切片的强指标。
- `share_rate`：经典歌/改编惊喜型的强指标。
- `comment_sentiment`：是否引发拉踩和版权争议。

## 9. MVP 调整

原 MVP：

```text
ASR -> 候选片段 -> 评分 -> 导出 -> 数据回流
```

音乐综艺 MVP：

```text
ASR + 音频特征
  -> 表演区间识别
  -> 歌曲结构/节目叙事/反应素材识别
  -> 短视频候选切片生成
  -> 歌词情绪、舞台表现、叙事上下文和评论触发评分
  -> 版权风险过滤
  -> 多版本切片建议
  -> 表现数据回流
```

第一阶段必须优先做：

1. 表演区间识别。
2. 副歌/高潮/高音候选检测。
3. 歌手故事、导师评价、现场反应等节目叙事素材识别。
4. 歌词情绪、舞台表现和短视频钩子评分。
5. 版权状态字段和风险过滤。
6. 音乐综艺短视频切片专用评分模型。

可以延后：

- 完整音乐结构深度模型。
- 声源分离。
- 自动识别歌名。
- 自动歌词对齐。
- 复杂情绪识别模型。

## 10. 第一版实现建议

先做轻量算法：

```text
audio_energy = RMS(audio)
onset_density = onset_count_per_second(audio)
vocal_peak = pitch / volume peak heuristic
chorus_candidate = repeated_lyric + high_energy + stable_melody
reaction_candidate = audience_noise_peak + non-singer face shot
context_candidate = judge_comment + singer_story + before_after_contrast
lyric_resonance = LLM(lyrics)
narrative_context = LLM(dialogue + lyrics + reaction)
comment_trigger = LLM(title + context + performance_moment)
rights_risk = rules(rights metadata, duration, section type)
```

第一版输出：

```text
候选切片 1
- 歌曲：xxx
- 歌手：xxx
- 类型：铺垫到高潮型
- 时间：00:12:31 - 00:13:08
- 短视频结构：导师一句评价 -> 副歌进入 -> 现场反应
- 音乐爆点：副歌进入 + 高音尾音
- 节目上下文：这是该歌手本场第一次明显突破原编曲
- 标题建议：这句副歌一出来，现场直接安静了
- 评论触发点：用户可能讨论“改编是否比原版更有遗憾感”
- 分数：88
- 版权风险：中，需确认歌曲片段授权范围
- 推荐理由：前 3 秒有导师评价做观看理由，12 秒进入副歌爆点，后段有现场反应，歌词情绪明确，具备短视频叙事闭环
```
