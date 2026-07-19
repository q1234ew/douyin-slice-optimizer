# Qwen3.7 完整短片 ECS 诊断集暂存记录

- 日期：2026-07-19
- 目标：G3 本地/公网模型协同
- 状态：`research_only / staged_no_provider_call`

## 1. 目的与边界

经用户明确允许，从本地已授权研究素材中选择一小批完整短片上传到用户控制的 ECS，供后续比较 Qwen3.7“完整短片”与现有“结构化摘要 + 代表帧”输入。此次操作只做 ECS 暂存和媒体完整性验证，不调用百炼，不产生模型费用，也不修改生产排序、人工 Gold、审核、导出或发布状态。

本批样本已出现在现有研究数据与 D12-B/D12-C0 诊断中，因此只能用于输入设计、错误归因和工程 Smoke，不能作为新的独立留出或 promotion 证据。正式验证仍需冻结未见账号/节目集。

## 2. 冻结传输清单

- 清单：`benchmarks/dso-qwen37-full-clip-diagnostic-20260719-r1.json`
- 清单 SHA-256：`55c57900ef14bbf6d9b8e00a1a0fd2f32b7c9d725c064371e608e904f95dd895`
- 来源 benchmark：`dso-multimodal-vector-value-20260719-r1`
- 来源 manifest SHA-256：`f32a6699ffc2cc7554ea6e2fa9a0550afc7e92a91cd2c46585beb130544a1510`
- 样本：10 条完整短片、5 组诊断/控制比较
- 覆盖：5 个账号、`music_variety / performance_clip / reaction` 三类内容
- 时长：`19.713–49.044s`
- 总字节：`44,763,762`
- 媒体：10/10 为 MP4、HEVC 视频、AAC 音频

选择包括 D12-B 中 `pair-043` 的共同错误、`pair-048` 与 `pair-052` 的 Cloud 错误方向、`pair-049` 的正确控制，以及一组同账号 reaction 高/低控制。清单不包含标题正文，远端文件名只使用内部 `sample_id`。

## 3. ECS 暂存与验证

远端目录：

```text
/srv/dso/app/data/research/qwen37_full_clip_diagnostic/dso-qwen37-full-clip-diagnostic-20260719-r1
```

安全与完整性结果：

- 目录权限：`0700`
- 10 个 MP4 与 `manifest.json`：`0600`
- 文件存在、大小、SHA-256：`10/10` 通过
- FFprobe 视频流：`10/10` 通过
- FFprobe AAC 音频流：`10/10` 通过
- 清单本地/远端 SHA-256：一致
- `dso-web`、Nginx：均为 `active`

上传完成后 Provider 台账仍为 703 条调用、495 次历史网络尝试，最新记录时间为 `2026-07-19T11:20:22.726+00:00`，早于本次 `12:11Z` 暂存；因此本次传输没有创建 Provider 调用或费用记录。

## 4. 后续分析前置条件

当前 `AliyunBailianProvider` 只接受结构化摘要和 JPEG 代表帧，不接受视频文件。这 10 条源视频也都是 HEVC；正式调用前需要单独实现并测试 `full_clip_analysis.v1` 视频输入 contract，并确认模型接口对容器/编码的兼容性。若需要 H.264 派生代理，应绑定源 SHA、保留原片不变，并重新记录时长、画面、音频与转码成本。

建议后续只先运行 2 组 bounded Smoke，对比：

1. 结构化摘要 + 代表帧；
2. 完整短片 + 同一 ASR/OCR/音频摘要。

先检查 schema、时序理解差异、P50/P95、Token/费用、失败率和缓存，再决定是否扩到 5 组。诊断集结果不能用于宣称流量、曝光、分享或关注提升。
