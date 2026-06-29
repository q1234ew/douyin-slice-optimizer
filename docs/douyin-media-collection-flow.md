# 抖音切片视频媒体采集流程

生成日期：2026-06-29
适用范围：本地研究样本、模型特征实验、切片质量分析。
当前实现：`src/dso/collectors/douyin_media.py` 和 CLI 命令 `douyin-media-collect`。

## 1. 目标

本流程把抖音切片视频从“手工测试”固化为可重复执行的本地采集任务，重点解决三件事：

1. 按账号、批次、样本计划分开保存视频、封面、抽帧和音频。
2. 每轮采集生成 JSON/Markdown 报告，便于复盘成功率和失败原因。
3. 保持只读边界，不读取 cookie、LocalStorage、SessionStorage、浏览器私有文件、密码、令牌。

当前流程服务于研究和建模，不用于自动发布、刷量、绕过平台限制或高频抓取。

## 2. 前置条件

运行前需要：

- macOS 本机环境。
- Google Chrome 已登录用户自己的抖音 Web 账号。
- Chrome 已开启“允许 Apple 事件中的 JavaScript”。
- 已安装 `ffmpeg` 和 `ffprobe`，用于视频验证、抽帧和音频提取。
- 已准备样本计划文件，例如：

```text
outputs/v0.7_media_collection_test/media_collection_test_sample_v1.json
```

样本计划中的每条样本至少需要：

| 字段 | 说明 |
| --- | --- |
| `sample_id` | 样本唯一 ID |
| `collection_order` | 采集顺序 |
| `account_id` | 内部账号 key，例如 `tianci`、`geshou2026`、`sixuweilive` |
| `dataset_id` | 样本来源批次 |
| `performance_label` | 高中低表现标签，例如 `high`、`mid`、`low` |
| `aweme_id` | 抖音作品 ID |
| `source_url` | 作品页 URL |
| `title` | 标题或人工备注 |
| `stage` | 采集阶段，例如 `smoke_v1`、`pilot_v1` |

## 3. 只读采集边界

脚本通过 Chrome Apple Events 打开作品页，并在当前 Chrome 页面执行只读 JavaScript。页面脚本只读取：

- `video`、`source` 标签中的媒体 URL。
- `meta`、`img` 中可见封面 URL。
- `performance.getEntriesByType("resource")` 中的页面资源 URL。
- 页面标题、当前 URL、资源数量。

脚本不会读取：

- cookie。
- LocalStorage。
- SessionStorage。
- Chrome Profile 文件。
- 密码、token、私信、草稿、发布后台隐私数据。

脚本不会执行：

- 点赞、关注、取消关注、评论、分享、收藏、删除、发布等状态改变操作。
- CAPTCHA、登录验证、风控弹窗绕过。
- 高频并发抓取。

## 4. 执行命令

先做 dry-run，确认计划筛选和输出路径：

```bash
PYTHONPATH=src python3 -m dso.cli douyin-media-collect \
  outputs/v0.7_media_collection_test/media_collection_test_sample_v1.json \
  --stage smoke_v1 \
  --run-id 20260629_test_v1 \
  --dry-run
```

执行 smoke 样本采集：

```bash
PYTHONPATH=src python3 -m dso.cli douyin-media-collect \
  outputs/v0.7_media_collection_test/media_collection_test_sample_v1.json \
  --stage smoke_v1 \
  --run-id 20260629_test_v1
```

只采一个账号：

```bash
PYTHONPATH=src python3 -m dso.cli douyin-media-collect \
  outputs/v0.7_media_collection_test/media_collection_test_sample_v1.json \
  --stage pilot_v1 \
  --account sixuweilive \
  --limit 30 \
  --run-id 20260629_pilot_sixuweilive
```

如果只需要视频、封面和抽帧，暂时不抽音频：

```bash
PYTHONPATH=src python3 -m dso.cli douyin-media-collect \
  outputs/v0.7_media_collection_test/media_collection_test_sample_v1.json \
  --stage pilot_v1 \
  --no-extract-audio
```

## 5. 输出结构

默认媒体资产保存到：

```text
data/douyin_media_assets/<account_id>/<run_id>/
```

账号之间必须分开保存。一个账号和一个批次下的目录结构为：

```text
videos/       原始下载视频，按 aweme_id 命名
covers/       封面图，按 aweme_id 命名
frames/       抽帧结果，每个 aweme_id 一个子目录
audio/        16kHz 单声道 wav，用于 ASR 或音频特征
transcripts/  后续 ASR 结果预留
ocr/          后续字幕 OCR 结果预留
features/     后续视觉、音频、文本特征预留
```

默认报告保存到：

```text
outputs/v0.7_media_collection_test/
```

报告文件包括：

- `media_collection_<stage>_report.json`
- `media_collection_<stage>_report.md`

如果指定 `--account`，报告文件名会带账号；如果指定 `--dry-run`，报告文件名会带 `dry_run`。

## 6. 单条样本处理步骤

1. 打开 `source_url`。
2. 等待 `--page-delay-seconds`，默认 14 秒。
3. 读取页面媒体候选 URL 和封面 URL。
4. 下载前 5 个候选视频 URL 中第一个可用文件。
5. 下载封面。
6. 用 `ffprobe` 读取时长、尺寸、编码等元数据。
7. 用 `ffmpeg` 抽取第 1 秒画面。
8. 默认抽取 16kHz 单声道 WAV 音频。
9. 写入样本级结果、批次汇总和 Markdown 报告。

状态含义：

| 状态 | 含义 |
| --- | --- |
| `planned` | dry-run 计划成功，未真实下载 |
| `success` | 视频可用，且至少有封面或抽帧 |
| `partial` | 部分步骤成功，但缺少关键媒体或视觉资产 |
| `failed` | 页面读取、下载或处理失败 |

## 7. 质量门

已完成的 smoke 测试结果：

| 指标 | 结果 |
| --- | ---: |
| 样本数 | 9 |
| 视频下载成功 | 9 |
| 封面下载成功 | 9 |
| 抽帧成功 | 9 |
| `ffprobe` 验证有效 | 9 |
| 失败 | 0 |

后续扩量建议：

| 阶段 | 样本规模 | 目标 |
| --- | ---: | --- |
| `smoke_v1` | 9 条左右 | 验证登录态、页面脚本、下载、抽帧链路 |
| `pilot_v1` | 30 条左右 | 覆盖 3 个账号和高中低表现样本 |
| `account_300_v1` | 每账号 300 条以内分批 | 建立账号级媒体样本资产 |

扩量验收门槛：

- 视频下载成功率不低于 80%。
- 抽帧或封面成功率不低于 80%。
- `ffprobe` 可解析率不低于 80%。
- 每个账号独立报告，不混写资产目录。
- 失败样本必须记录 `errors`，不能静默跳过。

## 8. 失败处理

常见失败和处理方式：

| 失败表现 | 优先排查 |
| --- | --- |
| `video_src_found=false` | 页面是否加载完成、是否出现登录或验证页、等待时间是否太短 |
| `video_download_failed` | 候选 URL 是否过期、Referer 是否匹配、是否需要重新打开页面 |
| `cover_download_failed` | 封面 URL 是否是短期资源，是否可忽略并依赖抽帧 |
| `ffprobe` 出错 | 下载文件是否为 HTML、空文件或被平台拦截 |
| 抽帧失败 | `ffmpeg` 是否安装，视频文件是否完整 |

如果同一账号连续失败，应停止扩量，先查看 Chrome 页面是否停在登录、验证、推荐流或不可播放状态。

## 9. 后续接入

媒体资产采集完成后，下一步建议顺序：

1. 对 `audio/` 做 ASR，结果写入 `transcripts/`。
2. 对 `frames/` 做字幕 OCR、封面元素和画面结构识别。
3. 对视频时长、尺寸、节奏、音量峰值、标题标签做统一特征。
4. 把特征回填到历史样本库，用于高互动样本解释、相似召回和轻量回测。

媒体文件本身不应直接进入生产发布链路；当前用途是本地研究、样本理解、特征提取和模型训练实验。
