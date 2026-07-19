# Qwen3-ASR 替代 Whisper 冻结评测

评测日期：2026-07-18  
Benchmark：`dso-qwen3-asr-whisper-replacement-20260718-r1`

## 结论

**当前不建议让 Qwen3-ASR 全局替代 Whisper。**

Qwen3-ASR-1.7B 在本节目里的中文口播、节目术语、品牌名和部分中英混说上明显优于 Whisper，字符级时间戳也足够准确；但当前长音频配置存在无 HTTP 错误却漏掉整段关键口播、热词 context 在低语音窗被复述、英文词间空格丢失、切块配置敏感等问题。模型应从“按需采用”调整为 `shadow`，现阶段适合对 Whisper 低置信片段或高价值候选做 30–60 秒二次识别，不适合成为唯一 ASR。

## 评测范围与限制

- 冻结输入为《我是歌手2025》一条 7506.73 秒完整节目，音频 SHA-256 为 `77fc1cb01f48b0cdfcdcba5dc9c8ce2b3847ec179e0697156e37c3dd35866f22`。
- 运行 3 份 Qwen 全节目转写：180 秒带 context、180 秒无 context、60 秒无 context；另对 14 个窗口共 550 秒做 Qwen/Whisper 同窗测试。
- 10 个可见烧录字幕锚点用于部分硬参考；其余分歧按字幕、语义和上下文人工核验。没有官方人工逐字稿，因此本报告不能给出跨领域 CER/WER，也不能外推到其他节目。
- Qwen 实测服务器检测为 RTX 5080 Laptop 15.47 GB，而不是用户所述 RTX 5070 16 GB；显存结论可参考，绝对速度需要在目标 5070 上复测。

## 质量结果

### 可见字幕锚点

| 全节目配置 | 命中 | Recall |
| --- | ---: | ---: |
| Whisper.cpp base | 3/10 | 30% |
| Whisper.cpp small | 5/10 | 50% |
| Qwen3-ASR，180 秒，带 context | 7/10 | 70% |
| Qwen3-ASR，180 秒，无 context | 4/10 | 40% |
| Qwen3-ASR，60 秒，无 context | 7/10 | 70% |

Qwen 的明确优势包括：

- 正确识别“非常蜿蜒的旋律”，Whisper base 为“歪言的选择/选丽”，历史 small 全节目在该演唱段出现重复幻觉。
- 正确识别“选择三个最打动你的表演”，Whisper base/small 均为“最大动力”。
- 正确识别“云端国际听审的心声”和“两位补位歌手”，Whisper 出现“新声/不会歌手”等错误。
- 正确识别“蒙牛酸酸乳”“梦龙乐队”“Believer”“超强鼓点”等词，整体优于 base，并在多个窗口优于 small。

仍存在的关键错误：

- “想得最多”识别为“瑕疵最多”。
- “格瑞丝·金斯勒”识别为“格格”；“卫兰”识别为“魏岚/魏楠”。
- 范玮琪长口播中多次把“她”写成“他”。
- 当前通用后处理合并字符时没有保留英文词间空格，长段英文会变成连续字符串，降低检索、字幕和切片摘要可用性。

### 长音频稳定性与 context 消融

- 180 秒带 context 的 42/42 个请求成功，但节目约 5370 秒处出现一次热词串注入：“陈楚生张韶涵范玮琪 Grace 竞演……”。
- 三个低语音 16 秒窗带 context 时均复述整段热词；清空 context 后只输出单个“嗯”，且短窗中的“蜿蜒的旋律”、Believer 和竞演排名没有退化。
- 但 180 秒无 context 全节目转写漏掉了米奇点评和投票规则两段关键口播，服务没有报告失败；同一片段独立短窗又可正常识别。
- 60 秒无 context 恢复了上述关键口播，锚点 Recall 回到 70%，但请求数从 42 增加到 128，并在节目末尾低语音区多出单个“嗯”。

这表明当前主要风险不是 HTTP 可用性，而是**切块长度、音乐/口播转换和 context 共同造成的静默语义漏失**。仅清空 context 或仅缩短切块都不能直接视为生产修复。

## 时间戳

Qwen ForcedAligner 在成功识别的 7 个字幕锚点上：

- 中位绝对误差：0.12 秒
- P95 绝对误差：0.32 秒
- 最大绝对误差：0.34 秒

该结果通过本次预注册的 1.5 秒中位数和 3 秒 P95 门槛。但 3 个未识别锚点不计入误差统计；时间戳准确不能抵消文本漏识别。应用当前还会把约 1.59 万个字符级片段合并为约 900 个最长 8 秒的片段，若不保留原始字符对齐，业务层无法直接利用上述精度。

## 性能与资源

| 配置 | 完整节目耗时 | RTF | 请求数 |
| --- | ---: | ---: | ---: |
| Qwen 180 秒，带 context | 380.10 秒 | 0.0506 | 42 |
| Qwen 180 秒，无 context | 384.70 秒 | 0.0512 | 42 |
| Qwen 60 秒，无 context | 382.86 秒 | 0.0510 | 128 |

- ASR/ForcedAligner 切换加载耗时 7.19 秒（模型文件已有系统缓存）。
- 推理期间 GPU 显存稳定约 8909 MiB，低于 14.5 GB 门槛。
- 同一批 550 秒窗口：Whisper base RTF 0.0471、Qwen RTF 0.0546、Whisper small RTF 0.0668。由于 Qwen 与 Whisper 运行在不同机器，只能比较当前系统端到端时延。
- 16 GB 卡无法与当前 Omni 同驻；评测期间 Omni 必须停止。评测结束后已恢复 Omni 为 `ready/loaded/text_only`，Qwen ASR 已卸载。

## 对智能切片的影响

使用相同音频特征和现有规则候选器做只读代理比较：

| 转写 | 生成候选 | 7 个关键窗 Top30 覆盖 |
| --- | ---: | ---: |
| Whisper base | 558 | 2/7 |
| Whisper small | 576 | 2/7 |
| Qwen 180 秒带 context | 639 | 3/7 |
| Qwen 60 秒无 context | 683 | 2/7 |

Qwen 让转写更完整、候选池更大，但没有稳定提升现有规则的 Top30 覆盖。该指标只是不带人工候选 Gold 的代理，不能当成推荐收益。要让 ASR 精度转化为切片收益，还需要调整候选器对正确节目术语、歌词、英文和长口播的使用方式，并在人工 Gold 上测 Recall@K/NDCG@K。

## 准入判断

| 门禁 | 结果 | 说明 |
| --- | --- | --- |
| HTTP/长任务完成 | 通过 | 42/42、42/42、128/128 请求成功 |
| 速度与显存 | 通过 | RTF 约 0.051，峰值约 8.9 GB |
| 字符时间戳 | 条件通过 | 7 个成功锚点误差优秀，漏识别未计入 |
| 识别质量 | 部分通过 | 关键术语优于 Whisper，但仍有专名、代词和英文格式问题 |
| 语义稳定性 | 不通过 | 配置变化可无报错漏掉整段关键口播 |
| 下游切片收益 | 未证明 | Top30 代理没有稳定提升，缺人工候选 Gold |
| 失败恢复 | 不通过 | 客户端无逐块重试、检查点和断点续跑；失败可能降为 placeholder |

因此本轮总体判定为：**不通过全局替代门禁，进入 Shadow。**

## 下一步建议

1. 保留 Whisper 为默认和回退；Qwen 只处理 Whisper 低置信、专名密集或 Top 候选 30–60 秒窗口。
2. 下一轮候选配置以 60 秒、1 秒重叠、空 context 为起点；先加 VAD/低语音门控，再测试受控短 context，而不是直接复用固定热词串。
3. 增加 context 回显检测、重复/低信息过滤、每 chunk 重试、检查点、断点续跑和完整性校验；任何 chunk 失败不得自动写 placeholder 覆盖已有转写。
4. 保留字符级时间戳，并重新实现中英混合合并，英文 token 之间保留空格。
5. 建立至少 3–5 个节目、多人/音乐/噪声/中英混说覆盖的人工逐字稿冻结集，再决定是否从 `shadow` 升为 `candidate`。

## 产物

- 冻结清单：`benchmarks/dso-qwen3-asr-whisper-replacement-20260718-r1.json`
- 全节目结果：`outputs/qwen3_asr_whisper_replacement_20260718_r1/full_program*`
- 锚点结果：`outputs/qwen3_asr_whisper_replacement_20260718_r1/anchor_recall.json`
- 时间戳结果：`outputs/qwen3_asr_whisper_replacement_20260718_r1/timestamp_alignment.json`
- 下游代理：`outputs/qwen3_asr_whisper_replacement_20260718_r1/downstream_candidate_comparison_all.json`
- 同窗速度：`outputs/qwen3_asr_whisper_replacement_20260718_r1/whisper_window_bench/report.json`
