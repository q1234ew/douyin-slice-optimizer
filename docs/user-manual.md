# Douyin Slice Optimizer 平台用户手册

更新日期：2026-07-20
适用版本：`douyin-slice-optimizer 0.1.0`，G1 `precut_batch.v1` / V1 Beta 工作台

## 1. 平台定位

Douyin Slice Optimizer 是本地优先的音乐综艺短视频筛选与切片优化工作台。平台既能批量导入已经剪好的短片并保持原边界完成统一排名，也能把完整节目处理成可人工审核的候选；两种入口共同使用评分解释、标题建议、封面建议、字幕预览、9:16 MP4 导出、发布后指标回流和历史样本学习。

平台当前覆盖以下工作：

| 工作 | 说明 |
| --- | --- |
| 已切短片批量排名 | 多文件导入、内容去重、原边界锁定、共享排序和审核 |
| 节目导入 | 上传本地节目视频，建立素材档案 |
| ASR 和音频提取 | 生成带时间戳字幕、音频峰值和候选生成所需特征 |
| 候选生成 | 从节目中生成 Top 候选切片 |
| 评分排序 | 输出综合分、评分解释、标题建议、封面建议和风险提示 |
| 候选审核 | 人工复核字幕、历史先验、质量 Gate 和导出状态 |
| 9:16 导出 | 生成竖屏 MP4、SRT 字幕和封面图 |
| 推荐链路模拟 | 查看冷启动、首轮留存、扩量和重排瓶颈 |
| 研究学习 | 管理历史研究样本、账号基线、原型库、发布时间建议和离线回测 |
| 数据回流 | 导入授权表现数据，生成指标快照、训练样本和账号复盘 |

平台不做自动发布、不刷量、不模拟互动，也不提供绕过平台规则的能力。当前历史样本主要来自可见互动数据，适合作为研究先验和趋势参考；在播放量缺失时，不应把互动热度表述为真实播放量预测。

## 2. 适用角色

| 角色 | 主要任务 | 推荐使用入口 |
| --- | --- | --- |
| 剪辑/内容操作员 | 导入节目、处理候选、审核字幕、导出成片 | Web 工作台 |
| 运营复盘人员 | 绑定平台 item、导入指标、查看账号基线和复盘摘要 | Web 工作台的“研究学习” |
| 数据/策略人员 | 重建历史样本、原型库、回测、语义校准 | Web 工作台和 CLI |
| 管理员 | 安装依赖、检查运行环境、配置 ASR 和 OAuth | CLI 和“运行环境”页 |

## 3. 首次安装和启动

### 3.1 环境要求

必需环境：

| 依赖 | 用途 |
| --- | --- |
| Python `>=3.11` | 后端、CLI、数据处理 |
| FFmpeg / FFprobe | 视频探测、音频提取、竖屏导出 |
| SQLite | 本地数据库 |
| 浏览器 | 打开 Web 工作台 |

可选环境：

| 依赖 | 用途 |
| --- | --- |
| Node.js / npm | 重新构建 Vue 前端 |
| whisper.cpp | 推荐的本地 ASR 后端，Apple Silicon 可使用 Metal/Core ML 加速 |
| faster-whisper | ASR 兜底后端 |
| `videofetch==0.9.1` | 可选的腾讯系 / YouTube 单视频授权测试下载入口；上游为 PolyForm Noncommercial 许可 |

### 3.2 安装后端依赖

在项目根目录执行：

```bash
cd /Users/fuqiang/Dev/douyin-slice-optimizer
python3 -m pip install -e ".[dev]"
dso init
```

如果本机没有把 `dso` 安装到 PATH，可使用等价命令：

```bash
PYTHONPATH=src python3 -m dso.cli init
```

### 3.3 配置 ASR

推荐先安装快速批处理模式：

```bash
dso setup-asr --profile fast
dso doctor
```

需要更高质量复核时，再安装质量模式：

```bash
dso setup-asr --profile quality
```

常用 ASR profile：

| Profile | 默认模型 | 使用场景 |
| --- | --- | --- |
| `fast` | `base` | 日常批量处理、默认节目切片 |
| `quality` | `small` | 英文歌名、人名密集、复杂舞台段落 |
| `verify` / `premium` | 候选级复核 | 对重点候选或 ASR 存疑片段做二次转写 |

如果未安装可用 ASR 后端，平台会生成占位 transcript，便于流程调试；正式生产前应在“研究学习 > 运行环境”确认 ASR 为可用状态。

### 3.4 启动 Web 工作台

```bash
dso web --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000/
```

如果需要开发或重新构建前端：

```bash
cd frontend
npm install
npm run build
cd ..
dso web --reload
```

## 4. 本地数据和安全边界

平台默认使用本地文件系统，不会把视频和数据库上传到远程服务。

| 路径 | 内容 |
| --- | --- |
| `data/db/dso.sqlite3` | 本地 SQLite 数据库 |
| `data/media/<video_id>/` | 导入后的节目视频副本 |
| `data/cache/<video_id>/` | ASR、音频特征和中间缓存 |
| `data/exports/<video_id>/` | 导出的 MP4、SRT 和封面 |
| `data/auth/` | 平台账号授权相关本地状态 |

清理或重导入数据库前，先备份：

```bash
cp data/db/dso.sqlite3 data/db/dso.sqlite3.backup-$(date +%Y%m%d%H%M%S)
```

当前默认 `DSO_RIGHTS_MODE=trusted_sample`，适合处理你已经确认可使用的合格样本。如需严格授权检查，可设置：

```bash
export DSO_RIGHTS_MODE=strict
dso rights set source_video <video_id> --program cleared --song cleared --performance cleared --artist cleared --platforms douyin --duration 90
```

## 5. Web 工作台总览

打开首页后，页面分为三块：

| 区域 | 主要内容 |
| --- | --- |
| 左侧“工作流程” | 素材、审核和发布准备的页面导航；完整节目模式提供单文件导入 |
| 中间工作区 | 已切短片批量排名、完整节目切片、研究学习、候选审核和推荐链路模拟器 |
| 右侧“预览与评分详情” | 在线预览、候选决策、字幕/ASR、历史先验、包装/平台 |

左侧流程共有 5 步：

| 步骤 | 操作 | 产出 |
| --- | --- | --- |
| 1. 导入节目 | 填写账号、节目标题，上传视频文件 | 节目素材档案 |
| 2. 处理评分 | 点击“处理选中” | 字幕、候选片段、评分结果 |
| 3. 候选审核 | 查看 Top 候选、质量 Gate、历史先验 | 人工通过、复核或暂缓 |
| 4. 历史先验 | 查看研究样本、账号质量、互动热度 | 剪辑策略参考 |
| 5. 复盘学习 | 校准语义字段、回测、重建原型库 | 下一轮策略依据 |

## 6. 标准使用流程

### 6.1 已切短片批量排名（G1）

进入“素材与节目”，默认选择“已切短片”。填写目标账号和可选批次名称，一次选择多条已经剪好的视频，然后点击“导入并排名”。

系统按以下固定口径处理：

1. 按同一账号内的文件 SHA-256 去重，重复文件复用已有素材、候选和评分。
2. 每个文件只创建一个候选，固定为 `0 秒 -> 原视频片尾`；页面显示“边界锁定”。
3. 后台执行快速 ASR、音频特征和共享 scorer/ranker；单条失败不会阻塞其他文件。
4. 排名完成后点击“审核”，进入与完整节目候选相同的解释、质量 Gate、审核、导出和指标回流页面。

锁边候选不允许修改开始、结束或时长。需要重新剪边时，应先在剪辑工具中生成新的源文件，再作为新短片导入；文本、分类、备注和审核状态仍可人工修正。

CLI 等价命令：

```bash
dso precut-import ./clips/clip-01.mp4 ./clips/clip-02.mp4 \
  --account main --batch-title "7 月候选"
```

只导入、不立即提取和排名：

```bash
dso precut-import ./clips/*.mp4 --account main --no-process
```

单批最多 100 个文件。ASR 或音频不可用时，批次条目会保留降级原因并使用现有确定性证据评分，不会伪造多模态结果或改变原始边界。

### 6.2 导入完整节目（G2）

在左侧“导入节目”区域填写：

| 字段 | 填写建议 |
| --- | --- |
| 账号 | 内部账号 ID，例如 `main`、`geshou2026` |
| 节目标题 | 便于检索的节目名称，例如“第 1 期 完整版” |
| 视频文件 | 本地视频文件，建议优先使用 MP4 |

提交后，节目会出现在“节目管理”列表中，状态通常为 `ingested`。

CLI 等价命令：

```bash
dso ingest ./program.mp4 --account main --title "第 1 期"
dso videos
```

如果自有或已获授权的视频只有腾讯视频或 YouTube 单视频链接，可安装可选下载适配器：

```bash
python3 -m pip install -e ".[videodl]"
```

先只解析、不下载也不写业务数据库：

```bash
dso download-video "https://v.qq.com/x/cover/.../...html" \
  --dry-run --acknowledge-noncommercial
```

YouTube 链接可以包含 `list/index` 参数，但适配器只处理 `v=` 指定的当前视频，不会展开播放列表：

```bash
dso download-video "https://www.youtube.com/watch?v=<video_id>&list=<playlist_id>" \
  --dry-run --acknowledge-noncommercial
```

确认结果为无 DRM 的清晰媒体后再下载；默认下载完成后进入既有节目导入链路：

```bash
dso download-video "https://v.qq.com/x/cover/.../...html" \
  --account main --title "授权测试节目" --acknowledge-noncommercial
```

下载任务默认写入当前工程的 `data/tmp/video_downloads/<job_id>/`，并保存 `dso-download-manifest.json`；成功入库时媒体会由既有 ingest 复制到 `data/media/<video_id>/`。`data/tmp` 属于可清理运行目录，如需把原始下载文件保存在其他持久位置，应显式传入 `--output-dir <目录>`。只保留文件、不入库可加 `--no-ingest`。

当前白名单为 `v.qq.com`、`wetv.vip`、`iflix.com`、`youtube.com`、`youtu.be`。适配器不传 Cookie、账号或代理，不启用通用解析器，并拒绝上游标记为 DRM 的资源。YouTube 路径不会调用 `ytdown`、`downr` 等第三方解析服务；它只解析一个视频，最高选择 720p，优先 H.264 MP4，并下载默认 AAC/MP4 音轨后用 FFmpeg 合并。dry-run 的 `candidates[].selected_format` 会显示完整流时长、清晰度、编码、itag 和预计字节数。若目标没有兼容的独立音轨，会回退到带音轨的渐进式 MP4，实际清晰度以 manifest 为准。`videodl` 上游使用 PolyForm Noncommercial 1.0.0；商业使用必须另行取得上游许可。

### 6.3 处理节目

在“节目管理”中选择节目，点击“处理选中”。系统会依次执行：

1. 提取：ASR 转写和音频特征提取。
2. 生成候选：生成候选切片。
3. 评分：计算综合分、标题建议、封面建议和风险说明。

也可以使用每行右侧的小按钮分别执行“提取”“生成候选”“评分”。

点击“智能切片”后，完整节目区域会显示转写分析、候选生成、评分排序和 Omni 复排四个阶段，并持续更新完成百分比、已耗时与预计剩余时间。预计时间会随阶段完成重新计算；进入 GPU 队列后不显示虚假的固定倒计时，而是展示真实处理条数并提示剩余时间由调度状态决定。完成或失败信息会保留，便于确认本轮实际耗时和停止阶段。

CLI 等价命令：

```bash
dso extract <video_id>
dso generate-segments <video_id> --top-k 30
dso score <video_id>
dso suggest <video_id> --top-k 10
```

质量较复杂的节目可使用质量 ASR 重跑：

```bash
dso extract <video_id> --asr-profile quality --asr-backend whisper_cpp --force-asr
```

音乐综艺完整节目和已切片输入默认优先使用 Qwen3-ASR 生成主转写；Qwen 不可用或失败时自动回退 Whisper：

```bash
# 先在 GPU 服务器执行，ASR 与 Omni 串行占用显存
~/bin/dso-asr-on

# 再在应用主机执行默认提取；auto 路由会选择 Qwen3-ASR
dso extract <video_id> --force-asr

# ASR 批次结束后，在 GPU 服务器恢复 Omni
~/bin/dso-omni-on
```

若 Qwen 模型未加载、服务不可用或返回空结果，主流程自动尝试 Whisper.cpp，再尝试 faster-whisper；metadata 中记录 `selected_backend`、`fallback_used` 和 `fallback_backend`。显式传入 `--asr-backend whisper_cpp` 可以对单次任务强制使用 Whisper。

需要保留双模型对照时，可额外执行 `dso qwen3-asr-shadow <video_id>`；Shadow artifact 固定写入 `data/cache/<video_id>/transcript/shadow/qwen3_asr/transcript.json`，不修改当前主转写。`GET/POST /videos/{video_id}/asr/shadow` 提供等价状态和执行入口。

Qwen3-ASR 默认把长音频切成不超过 60 秒的窗口，保留 1 秒重叠，并在目标切点前 5 秒内优先选择低能量边界；默认 context 为空。若某块出现有声音却空文本、异常慢且文本稀疏或 context 回显，客户端会清空 context 或缩成 30 秒窗口重试。每块的文本量、音频 RMS、尝试次数、恢复策略和最终质量状态会写入对应 transcript 目录的 `qwen3_asr_last_run.json`；仍未恢复的块会让本次运行标记为 `degraded`，不能只按 HTTP 成功判断转写完整。

可按节目覆盖配置，但 180 秒和固定热词串只建议用于复现实验：

```bash
DSO_QWEN3_ASR_CHUNK_SECONDS=60 \
DSO_QWEN3_ASR_BOUNDARY_SEARCH_SECONDS=5 \
DSO_QWEN3_ASR_RETRY_CHUNK_SECONDS=30 \
DSO_QWEN3_ASR_CONTEXT='' \
dso extract <video_id> --force-asr
```

主转写重跑成功后仍需重新生成候选与评分，旧候选不会自动改写；Shadow 重跑只产生对照证据。

### 6.4 查看质量 Gate

处理后，在“节目管理”和“推荐链路模拟器”里会出现质量哨兵。重点检查：

| 项目 | 含义 |
| --- | --- |
| ASR 后端/VAD | 当前转写是否使用真实后端和 VAD |
| 重复幻觉 | ASR 是否出现大段重复文本 |
| 广告口播 | 是否包含强品牌或广告口播 |
| Top 队列闭环率 | Top 候选是否具备完整评分和导出前检查 |
| 质量复核候选 | 哪些候选需要人工或 verify 模型复核 |

质量 Gate 是导出前的提醒，不等于绝对禁止。若出现复核项，应进入右侧详情确认字幕、风险和历史证据。

### 6.5 审核候选

进入“候选审核”，候选默认按当前规则分排序。每张候选卡片会展示：

| 信息 | 用途 |
| --- | --- |
| 时间段 | 切片在长视频中的起止位置 |
| 标题建议 | 可复制到发布端或 Variant |
| 字幕摘要 | 快速判断内容是否完整 |
| 结构 / 爆点 | 判断开头钩子、上下文和情绪推进 |
| 默认排序分 | 已采用的 `current_rules/final_score`；历史证据和多模态分只作研究对照 |
| 质量复核标记 | 标出可能需要人工处理的风险 |
| 导出状态 | 待导出、已导出或暂缓 |

点击候选后，右侧“预览与评分详情”会同步展示完整信息。

默认页面、批量短片排名和 `dso suggest` 都使用生产 scope。排查算法时可运行 `dso suggest <video_id> --ranking-scope research`，或请求 `GET /videos/{video_id}/suggestions?ranking_scope=research` 查看研究顺序；该操作不会改变审核、导出或发布状态。

### 6.6 使用右侧详情面板

右侧面板包含 4 个分区：

| 分区 | 使用方式 |
| --- | --- |
| 决策 | 查看候选决策、历史先验摘要、评分拆解、人工复核和运行清单 |
| 字幕/ASR | 检查字幕文本；对重点候选点击“verify 转写” |
| 历史先验 | 查看相似高互动样本、低互动风险、账号基线、原型命中和排序器建议 |
| 包装/平台 | 查看标题建议、封面建议、Variant 实验、平台映射和授权反馈样本 |

人工复核状态：

| 状态 | 建议含义 |
| --- | --- |
| 通过 | 字幕、内容结构、风险和导出预览都可接受 |
| 需复核 | 内容可疑但不应立即放弃，需要二次确认 |
| 暂缓 | 授权、低原创、广告口播或内容质量存在阻断风险 |

CLI 等价命令：

```bash
dso review-segment <segment_id> --status approved --reason "字幕和质量 Gate 已确认"
dso review-segment <segment_id> --status review --reason "ASR 需二次核对"
dso review-segment <segment_id> --status blocked --reason "授权或低原创风险"
dso verify-asr <segment_id> --profile verify
dso history <segment_id> --limit 8
```

### 6.7 导出 9:16 成片

在“候选审核”中点击候选卡片的“导出”，或在右侧“决策”分区点击主操作按钮。导出成功后会生成：

| 文件 | 说明 |
| --- | --- |
| MP4 | 9:16 竖屏预览视频 |
| SRT | 对应字幕文件 |
| JPG | 封面帧 |

Web UI 会展示在线预览，也可以在右侧点击“打开”访问导出文件。

CLI 等价命令：

```bash
dso export <segment_id>
```

导出文件位于：

```text
data/exports/<video_id>/
```

如果导出被阻断，常见原因包括候选被人工标记暂缓、严格授权模式下未录入授权、低原创风险过高或尚未完成评分。应先修复风险来源，再重新导出。

### 6.8 推荐链路模拟

进入“推荐链路模拟器”，选择节目后点击“刷新模拟”。页面会展示：

| 指标 | 说明 |
| --- | --- |
| 模拟均分 | 候选在影子推荐链路中的整体分 |
| 高潜候选 | 模拟中具备较好扩量潜力的候选数 |
| 主要瓶颈 | 冷启动、首轮留存、扩量或重排中的主要短板 |
| 主阶段 | 当前最影响分发的阶段 |

模拟结果用于辅助审核，不代表平台真实分发承诺。最终发布仍需人工结合节目内容、账号定位、版权和平台规则判断。

## 7. 研究学习和数据回流

“研究学习”分为 5 个标签：

| 标签 | 用途 |
| --- | --- |
| 概览 | 查看样本覆盖、账号质量、最近回测和平台账号状态 |
| 研究样本 | 查看数据口径、训练样本和账号基线 |
| 校准与回测 | 重建记忆库、发布时间、历史样本、原型库，运行回测和语义校准 |
| 平台账号 | 连接抖音账号，导入授权指标或同步文件 |
| 运行环境 | 检查 FFmpeg、FFprobe、ASR、模型和本地路径 |

### 7.1 数据口径

平台会严格区分不同“样本”概念：

| 名称 | 含义 |
| --- | --- |
| 源文件行数 | 原始采集文件中的有效行数，可能包含重复作品 |
| 源去重 | 按作品 ID 或稳定标题 key 去重后的作品数 |
| 正式历史样本 | 已写入 `historical_capture_samples` 的研究样本 |
| 可训练样本 | 可用于学习或回测的样本 |
| 训练样本 | 授权指标导入后，由指标快照生成的训练记录 |
| 重复视频组 | 入库后仍被识别为重复的作品组 |

当前历史样本播放量缺失时，页面会使用“互动热度”“高可见热度”“历史先验”等表述。不要把点赞、评论、收藏、转发直接当作播放量。

研究中心顶部的“研究账号”只筛选历史研究数据。`tianci` 等采集账号不会因此成为发布账号；发布账号使用独立的 `main` 本地槽位，并在未明确身份和结果数据时显示“尚未指定 / 冷启动”。

### 7.2 授权指标导入

进入“研究学习 > 平台账号 > 授权指标导入”，上传 CSV 或 XLSX 文件。最小 CSV 示例：

```csv
candidate_segment_id,window_name,hours_since_publish,views,impressions,avg_watch_ratio,five_second_retention,completion_rate,rewatch_rate,likes,comments,favorites,shares,follows,negative_feedback
seg_demo,24h,24,10000,24000,0.74,0.82,0.51,0.08,600,180,220,90,35,12
```

可用标识字段：

| 字段 | 说明 |
| --- | --- |
| `candidate_segment_id` | 直接关联候选片段，最推荐 |
| `slice_variant_id` | 关联已创建的导出版本 |
| `experiment_id` | 关联发布实验 |
| `platform_item_id` | 关联平台作品 ID，需要先完成平台映射 |

只有成功关联到候选片段的指标行会生成训练样本。未关联行会被导入为指标记录，但不会进入训练样本。

`可见计数`、`计数数值`、`visible_count_number` 和 `best_visible_count_number` 不属于播放量字段。系统会保留审计告警，但不会把它们写入 `views`；需要提供明确的 `views`、`play_count` 或 `view_count`。

CLI 等价命令：

```bash
dso import-metrics ./douyin_metrics.csv
dso rebuild-feedback --account main
dso training-samples --account main --limit 50
dso baselines --account main
```

### 7.3 平台账号和抖音同步

“研究学习 > 平台账号”固定操作发布账号槽位，不跟随顶部研究账号筛选器切换。可进行：

| 操作 | 说明 |
| --- | --- |
| 连接发布账号 | 使用 OAuth 配置生成扫码授权入口 |
| Mock 链路测试 | 使用模拟数据验证链路，不计入真实目标结果 |
| 同步文件 | 上传 CSV、XLSX 或 JSON 文件进行授权数据同步 |
| 平台映射 | 在候选详情中绑定平台 item ID |

账号和证据用途：

| 字段 | 可选值 | 说明 |
| --- | --- | --- |
| `account_role` | `unassigned / publishing_target / research_source` | 目标发布账号必须显式指定 |
| `evidence_scope` | `unclassified / target_outcome / research_proxy` | 旧映射默认未分类，不会自动升级 |
| `metric_semantics` | `explicit_platform_outcome / engagement_proxy / ambiguous_visible_count / legacy_unverified / mock` | 只有明确目标结果可进入账号 readiness |

目标账号结果暂不可用时无需伪造数据或运行 Mock 充数。系统继续使用跨账号研究先验和 `current_rules`，并保持 `cold_start`；平台个性化至少需要 30 个已链接且语义明确的目标账号作品结果。

OAuth 需要配置环境变量：

```bash
export DSO_DOUYIN_CLIENT_KEY=<client_key>
export DSO_DOUYIN_CLIENT_SECRET=<client_secret>
export DSO_DOUYIN_REDIRECT_URI=<redirect_uri>
export DSO_DOUYIN_SCOPES=<optional_scopes>
```

CLI 常用命令：

```bash
dso douyin-account --account main --display-name "主账号" --platform-account-id "<platform_account_id>"
dso douyin-login-url --account main
dso douyin-auth-status --account main
dso douyin-sync --account main --source mock --windows 6h,24h,72h
dso douyin-sync --account main --source csv --path ./douyin_metrics.csv --windows 24h
dso douyin-summary --account main
```

### 7.4 历史样本和账号基线

“研究样本”用于查看账号历史表现先验，包括账号质量、互动字段覆盖率、播放量缺失率、Top 信号、账号基线和训练样本。

常用 CLI：

```bash
dso datasets
dso historical-summary --account ''
dso historical-samples --account main --limit 20
dso douyin-history-baselines --account main --dataset all --min-count 2 --limit 80
dso research-coverage --account main --dataset all
```

导入新的清洗后抖音历史数据：

```bash
dso douyin-history-import \
  --account <account_id> \
  --clean-dir <clean_dir> \
  --dataset <dataset_id> \
  --dataset-name "<dataset_name>"
```

### 7.5 语义校准和离线回测

进入“研究学习 > 校准与回测”后，页面分为四个工作模式：

| 模式 | 用途 |
| --- | --- |
| 素材审核 | 默认入口；逐条确认 Material Gold Set，页面一次只展开一个样本 |
| 语义校准 | 修正类别、Hook、结构、艺人、歌曲和标签 |
| 向量评测 | 先看冻结可见互动代理结果，再用隐藏信息的 A/B 判断补充编辑偏好诊断 |
| 算法回测 | 运行 v2.4-v2.9、语义、结构和 Qwen 实验；低频操作收在“数据与高级工具” |

素材审核按页面顶部三步完成：

1. 在左侧短队列选择样本，必要时点击“原视频”查看来源。
2. 对照“Omni 原判”，确认领域分类、素材形态、呈现方式和节目语境；无法确定的字段保留“未知”。
3. 点击“保存并进入下一条”。完成 12 条试标后可运行回放；完成首轮 60 条后运行 v2.9，分别检查严格标签准确率、规范形态准确率、严重错判率和 Top30 变化。

其他可执行操作：

| 操作 | 说明 |
| --- | --- |
| 重建记忆库 | 重新生成文本记忆和历史相似召回所需资产 |
| 刷新发布时间 | 计算账号和主题维度的发布时间建议 |
| 刷新历史样本 | 从已配置来源重新导入历史样本 |
| 重建原型库 | 发现高互动内容原型 |
| 运行回测 | 对比当前规则、语义基线和历史证据排序器 |
| 刷新队列 | 获取高影响、低可信或缺关键字段样本 |
| 保存人工标签 | 人工修正类别、Hook、结构、艺人、歌名和标签 |
| 重新打开校准 | 将最近已保存的样本重新放回待校准队列 |
| 重建标签与回测 | 重新计算互动热度标签并运行回测 |
| 刷新素材审核 | 获取素材形态 Gold Set 候选 |
| 确认素材形态 | 人工确认领域、素材形态、节目语境和呈现方式 |
| 运行 v2.9 回放 | 将 Gold 拆为校准/独立审计，比较严格标签与 canonical 形态，并对照 v2.4/v2.8 的 Top20/30/50 |
| 3 条 Smoke | 对 confirmed Gold 优先队列执行 hook / middle / payoff 三窗口 ASR、OCR 和 Omni 证据抽取 |
| Resolver 回放 | 在已有 D10-B 缓存上比较 title-only、Omni-only、ASR/OCR 和多窗口 Resolver；不会改标签或权重 |
| 扫描下一批 10 条 | 按素材形态和账号平衡选择未审核样本，执行约 5 秒步进、15 秒候选窗和三帧抽取；缺音轨不会阻断 |
| 保存窗口 Gold | 确认视觉形态、节目语境和选窗质量，只写独立窗口标注 |
| 累计对比 | 累计已冻结批次并按样本去重，成对比较固定窗、文本窗、视觉窗和动态融合；缺失策略显示 N/A |

保存人工标签后，样本会被标记为 `manual_verified`，因此会从“语义校准队列”的待处理列表中消失。这是已完成状态，不是刷新失败。页面会在“最近已保存”里保留最近样本；如果误保存或需要二次校准，点击“重新打开校准”，样本会恢复为低置信校准样本并重新出现在队列里。

“素材形态 Gold Set”与上述主语义校准相互独立。确认素材形态只写入 `material_gold_annotations`，不会修改互动数、`reward_proxy`、主语义标签或 `classification_confidence`。v2.9 不会把 `performance_highlight` 等人工细粒度标签改写掉；它只在排序路由侧派生 canonical 形态，并继续单独报告细节缺失。无法确定时保留 `unknown`，不要为了提高覆盖率强行归类。

“向量评测”使用冻结 benchmark `dso-multimodal-vector-value-20260719-r1`。云端重排报告优先显示“平衡互动代理命中”和“相对 v2.4”：这是账号内归一化的抓取时点点赞、评论、收藏、分享等客观可见结果，不是播放量、曝光或关注转化。系统按真实结果的左/右侧分别算命中再取平均，同时报告原始准确率、方向分布和多数类基线；审核可选“相当”，但客观结果按分差正负二选一。至少 40 个完整 pair 且云端较 v2.4 提升 `5pp` 才通过研究门禁；门禁不会自动改生产权重。

页面下方 A/B 是次级编辑偏好诊断。每组只显示视频和时长，不显示账号、标题、互动标签、Material Gold 身份或算法分数；选择 A、B、两者相当或无法判断，把握程度和依据只用于代理冲突与严重错判审计。保存只写 `multimodal_vector_reviews`。向量构建必须经持久 Scheduler；模型服务不可达时保留 manifest 与缓存，切换云端后仍按同一冻结 ID 定向补齐，不能重新抽样。

百炼 Shadow 链路继续使用同一冻结 benchmark，顺序固定为 `Qwen3-VL-Embedding -> Qwen3-VL-Rerank -> 可见互动代理门禁 -> Qwen3.7-Plus/Qwen3.6-Flash 真实分歧盲裁`。冻结侧车必须为 60/60 个 pair 提供与 manifest SHA 匹配的 v2.4 选择和 `proxy_choice`；缺失或不匹配时页面应显示基线不可用，Judge 不发送请求。`rerank --limit` 会按完整 A/B pair 选择样本，参考池必须同时有等量高/低互动向量。Judge 不接收两套策略的选择、分差或代理结果，避免锚定。打开页面只读取本地状态，不调用公网模型、不产生费用；每个运行按钮都需要人工点击，Web 单次最多处理 40 条，`full` 仅允许从 CLI 启动。云端结果只写独立向量缓存、成本台账和研究报告，不写 Material Gold、不修改生产排序权重，也不触发导出或发布。

“缓存消融”按钮运行 D12-A，只读取现有 Text/Fusion 向量、缓存 Rerank 和冻结 v2.4 侧车，不要求公网开关开启，也不会消耗 Token。结果区显示同一可比较 pair 子集上的 v2.4、最佳缓存配置、增量、bootstrap 95% 区间和扩量判定。`可扩至 60 对` 只代表允许冻结新的研究扩展集；`保持 v2.4` 表示至少一个覆盖、账号或类别条件未通过。页面中的最高配置来自同一缓存集搜索，不能直接写生产权重。

“20 对独立留出复验”运行 D12-B，操作顺序固定为“冻结配置 -> 生成盲预测 -> 解锁并评估”。冻结后不能更改 Text ref20/K3、Embedding/Rerank 50/50、v2.4/cloud 85/15 或 D12-A 归一化尺度；生成预测时单实验费用硬上限为 `10.00 CNY`，只补 40 条留出样本的 Text/Fusion 和 Rerank。预测文件通过 SHA 防覆盖且不含互动结果字段，必须先完成预测才能解锁代理标签。当前冻结集已经评估完成，独立增量为 0，页面显示“保持 v2.4”；重复点击不会重新调参或写生产权重。

“零成本失败归因”运行 D12-C0，只在 D12-B 已评估后可用。它校验冻结 SHA，读取缓存向量和 Rerank，显示云信号命中、最终选择变化、Top1 表现/内容分类一致、视觉源数量和同账号参考覆盖。该操作不会调用公网模型，费用固定为 0；后验权重网格只解释为什么 15% 没有改变选择，不能用于继续调当前 60 对。

“重构三窗口证据”运行 D12-C1。它从本地原视频确定性生成 15 秒 hook / middle / payoff 三时点帧，使用独立 source hash，并以同账号、同节目、同素材形态、全局的顺序检查 high/low 双侧参考。只有高低两侧共享同一语境层级时才使用该层，否则双侧一起降级。按钮只复用帧和向量缓存，固定显示 0 元、0 请求；页面会同时显示三时点覆盖、缓存参考数、同账号覆盖上限、分层文本对照和新 Fusion 缺口。当前结论是参考池覆盖不足且分层文本弱于全局，仍保持 v2.4。

素材审核页底部的“定向错判队列”用于 D10-A。它默认从已有 Omni 缓存和本地视频中平衡选择 80 条样本，可按五类固定混淆和“跨领域/形态分歧”筛选，并显示原始 Omni 形态、规范形态、关键词证据和媒体就绪状态。包含已审核样本时，去重后且标签已知的 confirmed Gold 会先于普通候选占位；动态跨领域候选只使用已有标签与 Omni 分歧准入，不读取 Gold 真值反推候选。`performance_highlight` 在此派生为 `performance_clip + highlight_signal`，`program_context` 作为独立字段；队列候选不会自动写入 Gold。

“三窗口证据与 Resolver Shadow”用于 D10-B。证据抽取默认包含并优先选择已确认 Gold，每条真实执行三个 8 秒窗口；ASR 提示词回声或纯音乐低信息结果会保留原文审计，但不参与语义投票。页面分别显示 Gold 入队覆盖与 Gold 证据覆盖，分母是 confirmed、素材形态已知且按账号与稳定标题去重后的 Gold。`unknown` 预测计为弃权：端到端准确率仍把弃权留在分母，作答准确率只看非 `unknown` 结果，严重错判率也只在实际作答中统计。大批量抽取应使用 CLI 断点执行，工作台的 Smoke 只用于小批验证。

“视觉候选窗与窗口 Gold”用于 D11B。视觉路径只要求本地视频、可读取时长且与历史时长基本一致，音轨是可选证据。每个候选窗展示起止时间和三张帧图；人工需确认 `视觉形态 / 节目语境 / 选窗质量`，无法稳定判断时选“未知”或“暂不确定”。窗口 Gold 写入 `material_window_annotations`，不会改写历史样本主标签。

每次扫描都会创建不可覆盖的 `build_id`、报告和 SHA-256 manifest；新批次排除已审核样本，当前批次未完成时不能继续取下一批。累计对比只合并已冻结 build，同一样本因向量重试出现多次时只保留最新版本。原型对每个验证样本执行 leave-one-sample-out，`unknown/uncertain` 作为合法弃权而不是严重漏选。Qwen 服务不可用时仍可生成帧，但真实 2048 维窗口向量覆盖低于 90% 时页面只允许“重试窗口向量”，不会进入评测。

常用 CLI：

```bash
dso memory-build --account main --force
dso interest-clock --account main --rebuild --limit 5
dso prototype-build --account main --source visible_capture --dataset all --force
dso semantic-calibration-queue --account main --dataset all --limit 50
dso semantic-calibration-reopen <sample_id> --confidence low --reason "second calibration pass"
dso research-labels-rebuild --account main --dataset all
dso interaction-heat-export-input --db-path data/db/dso.sqlite3 --input-jsonl <input.jsonl> --media-index <media-sha.json>
dso interaction-heat-freeze --input-jsonl <input.jsonl> --media-index <media-sha.json> --output-root benchmarks
dso interaction-heat-verify \
  --artifact-dir benchmarks/dso-interaction-heat-v3-20260720-r3 \
  --expected-manifest-sha256 <另处保存的64位SHA-256>
dso interaction-heat-pairwise-local \
  --label-artifact-dir benchmarks/dso-interaction-heat-v3-20260720-r3 \
  --expected-label-manifest-sha256 <阶段1另处保存的64位SHA-256> \
  --db-path data/db/dso.sqlite3 \
  --output-root benchmarks
dso interaction-heat-target-encoding-local \
  --experiment-id dso-interaction-heat-target-encoding-20260720-r2 \
  --label-artifact-dir benchmarks/dso-interaction-heat-v3-20260720-r3 \
  --expected-label-manifest-sha256 <阶段1另处保存的64位SHA-256> \
  --db-path data/db/dso.sqlite3 \
  --output-root benchmarks \
  --evaluation-scope validation
dso interaction-heat-holdout-readiness \
  --label-artifact-dir benchmarks/dso-interaction-heat-v3-20260720-r3 \
  --expected-label-manifest-sha256 <阶段1另处保存的64位SHA-256> \
  --db-path data/db/dso.sqlite3
dso backtest --account main --k 10 --strategy research_ranker_v2_1 --holdout-policy time
dso ranker-tuning-run --account main --k 10 --max-trials 12
dso backtest-reports --account main --limit 10
dso material-evidence-extract --limit 3 --window-seconds 8 --include-reviewed
dso material-resolver-shadow --limit 100 --include-reviewed
dso bailian-vector-status --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-run --stage smoke --limit 10 --top-n 10 --judge-limit 5
dso bailian-vector-ablation --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-holdout --stage freeze --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-holdout --stage predict --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-holdout --stage evaluate --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-attribution --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-evidence-quality --benchmark-id dso-multimodal-vector-value-20260719-r1 --scope holdout --limit 40
```

`interaction-heat-export-input` 只导出 V3 所需的最小字段和本地媒体 SHA 索引，不复制媒体或其他业务表；input 与 media 路径必须不同且不得已存在（包括 dangling symlink），先写 staging，再以 no-replace 语义发布，任一发布失败只会按 inode 回滚本操作创建的文件。`interaction-heat-freeze` 生成不可变的五文件 artifact，同一 ID 已存在时失败。`interaction-heat-verify` 必须接收在 artifact 外单独保存的 manifest SHA-256，要求目录精确包含 manifest 和固定四个 payload，以 no-follow 方式拒绝 symlink/非普通文件，并校验 manifest/payload SHA、零网络/零模型费用以及不覆盖 `visible_engagement_v2`；不传 pinned SHA 只能得到失败结果，不能把自签 manifest 当成可信验签。V3 是抓取时点互动热度研究标签，不是播放量、曝光归一化转化或生产排序分。

`interaction-heat-pairwise-local` 在本地从已验签的 V3 artifact 和 SQLite 白名单内容字段训练两个纯 Python Pairwise Logistic 研究模型，不需要 NumPy、sklearn、LightGBM、ECS 或公网 API。命令拒绝覆盖同名实验目录，输出 `manifest.json / model.json / predictions.jsonl / report.json`。预测文件不包含互动标签；报告中的 heat lift 仅表示抓取时点互动热度差值，不是播放量或转化提升。当前模型只作为 `research_only` 基线，不能自动修改排序权重、Gold、导出或发布。

`interaction-heat-target-encoding-local` 在同一 V3 contract 上训练两个纯 Python 平滑 Target Encoding 基线。默认 `--evaluation-scope validation`，只生成 train OOF 和 validation 预测，不读取纯 test 行、不输出 test 指标；标题特征默认关闭，只有显式 `--include-title` 才启用。命令拒绝覆盖同名实验目录并输出相同四文件 artifact；predictions 不含标签或互动 outcome。当前 r2 只是 `research_only` validation 基线，整账号泛化仍不稳定，不能据此修改生产排序、Gold、导出或发布。

`interaction-heat-holdout-readiness` 是下一轮非线性 ranker 的只读数据门禁。命令验签冻结 V3，但只解析 `splits.jsonl` 的 sample ID、账号和发布时间 cutoff；数据库候选沿用 V3 显式 metric provenance 准入。默认要求新前向窗口至少 1,000 条、5 个账号并覆盖 7 天，且至少 3 个冻结集外账号各有 100 条；新账号可以包含较早发布但冻结后新收集的作品。只有输出 `status=ready` 且 `unlock_nonlinear_ranker=true` 才能冻结下一 holdout 并开始 LightGBM/LambdaRank；`not_ready` 时不应调低门槛追分或重用旧 test。

样本少于 300 的账号只能展示低置信趋势，不应输出确定性权重。

### 7.6 Qwen2.5-Omni 低显存 Shadow Mode

低显存 Omni 模块用于离线验证短片段多模态理解，默认模型为 `Qwen/Qwen2.5-Omni-7B-GPTQ-Int4`。当前只建议在具备 CUDA 的目标机上运行 15 秒以内片段，`batch_size=1`，`return_audio=false`。

该模块输出只作为语义校准建议，不会自动写入 `manual_verified`，不会直接改变候选生产分数，也不会替代人工审核。V1 Beta-D-6 增加 `research_ranker_v2_6_pool`，用于 Omni cached eval only 的 Top30 扩池研究门控；通过回测报告查看 `omni_pool_report`、`omni_pool_gate`、`omni_trust_profiles` 和账号级 `omni_account_pool_gates`，未通过 gate 前仍保持 `pool_research_only`。

常用 CLI：

```bash
dso qwen-omni-status
dso qwen-omni-analyze <segment_id> --max-clip-seconds 15 --load-model
dso qwen-omni-shadow-run --account main --dataset all --limit 20 --max-clip-seconds 15 --load-model
dso qwen-omni-shadow-run --limit 20 --max-clip-seconds 15 --use-media --allow-windowed-clips --visual-ready-only
dso qwen-omni-media-batch --limit 20 --max-clip-seconds 8 --load-model
dso backtest --account main --k 30 --strategy research_ranker_v2_6_pool --holdout-policy time
```

常用 API：

| API | 用途 |
| --- | --- |
| `GET /learning/qwen-omni/status` | 检查服务、显存门控和当前加载模型 |
| `POST /segments/{segment_id}/qwen-omni/analyze` | 对单条候选做短片段 shadow 分析 |
| `POST /learning/qwen-omni/shadow-run` | 对历史样本批量做 shadow-run |
| `POST /learning/qwen-omni/media-batch` | 对本地真视频历史样本做切窗、上传和断点缓存 |

### 7.7 G3 公网模型安全底座

当前版本具备 `public_model_provider/runner/ledger.v2`、默认关闭策略、显式数据许可、请求/批次/日预算、缓存前置、usage 结算、逐网络尝试台账、本地回退和 Shadow 评测，并已注册默认关闭的 `AliyunBailianProvider`。即使只设置 API Key，系统也不会自动启用网络请求。

Web 配置入口位于顶部“模型 API”，或“研究中心 → 模型与环境 → 公网模型 API”。可填写百炼页面显示的 OpenAI 兼容地址、固定模型快照、API Key 和单请求/单批次/单日人民币预算。页面不回显已有 Key，保存成功会立即清空密码框；浏览器不在 localStorage 或 sessionStorage 保存密钥。

ECS 当前测试入口是公网 HTTP，因此页面会禁用密钥输入和保存。请在本机终端建立 SSH 端口转发：

```bash
ssh -i /Users/fuqiang/aliyun/douyin.pem \
  -L 8765:127.0.0.1:8000 root@121.199.170.85
```

保持终端运行，浏览器打开 `http://127.0.0.1:8765/`，进入上述面板后再保存。`localhost/127.0.0.1` 属于浏览器安全上下文，后端同时校验直连 loopback；不要在 `http://121.199.170.85/` 输入 Key。以后配置受信 HTTPS 反向代理后也可直接保存。

保存会原子写入 ECS `/etc/dso/bailian.env` 并设置为 `0600`，同时强制 `DSO_PUBLIC_MODEL_API_ENABLED=0`。因此本步骤只配置连接，不测试模型、不上传内容、不产生费用。数据许可和保留政策未确认前，真实 Runner 继续 fail closed。

先查看只读状态：

```bash
dso provider-status
```

再用 Fake Provider 验证完整链路。`--repeat 2` 应看到第二次命中缓存，且网络调用数和估算费用都为 0：

```bash
dso provider-smoke --text "G3 local-only smoke" --repeat 2 --batch-id local-g3-smoke
```

运行数据分别写入 `data/cache/public_models/` 和 `data/db/public_model_ledger.sqlite3`。台账只保存 Provider/模型/提示词版本、实际 Token/响应字节、Provider request ID、逐尝试状态、重试/限流、延迟、缓存、preflight/usage/账单状态、许可和保留政策引用，不保存 API Key、Authorization、提示词正文、字幕正文、Base64 或原始媒体。

`GET /providers/status` 会逐项返回以下百炼门禁，但不会返回任何密钥值：

| 门禁 | 环境配置 |
| --- | --- |
| 总开关和 Provider 选择 | `DSO_PUBLIC_MODEL_API_ENABLED=1`、`DSO_PUBLIC_MODEL_PROVIDER=aliyun_bailian` |
| 固定模型与北京业务空间 | `DSO_BAILIAN_MODEL_ID`、`DSO_BAILIAN_BASE_URL` |
| 独立密钥 | `DSO_BAILIAN_API_KEY`，只写权限为 `0600` 的 systemd EnvironmentFile |
| 三层预算 | `DSO_PUBLIC_MODEL_BUDGET_PER_REQUEST_CNY`、`...PER_BATCH_CNY`、`...PER_DAY_CNY` |
| 数据许可 | `DSO_BAILIAN_DATA_ALLOWED`、`DSO_BAILIAN_AUTHORIZATION_BASIS`、`DSO_BAILIAN_REDACTION_STRATEGY` |
| 保留政策 | `DSO_BAILIAN_RETENTION_DAYS`、`DSO_BAILIAN_RETENTION_POLICY_REFERENCE` |
| 上传等级 | `DSO_BAILIAN_ALLOWED_UPLOAD_LEVELS`；标准运行只允许 `structured_summary,representative_frames`，单次完整短片研究需另行授权并在当次进程使用 `full_media` |

当前 ECS 研究环境的三层预算为单请求 `2.00 CNY`、单批次 `50.00 CNY`、单日 `50.00 CNY`。单日值是停止新增请求的硬上限，不是建议花满的目标；单请求上限用于阻止异常大 payload，批次上限用于限制一次实验的总暴露。厂商赠送 Token 不会自动进入本地预算台账，Runner 仍按公开价格保守预留和记录，最终免费抵扣以百炼控制台账单为准。

只有全部门禁为 `true` 时，内部运行时工厂才允许构造真实 Runner。阿里云公开政策未给固定保留天数时，不得填写 `0`；经授权的研究批次可把 `DSO_BAILIAN_RETENTION_DAYS` 显式设为 `provider_minimum_necessary`，同时必须提供政策引用，此时状态显示 `retention_days_known=false`。项目已提供显式的百炼向量研究入口，但不会自动运行。研究中心可分别执行 10 条 Smoke、40 条向量构建、20 条重排和真实分歧盲裁；Web 禁止 `full`，避免长任务占用请求线程。所有真实调用仍须先通过许可与预算检查，失败时保留本地 v2.4 结果。

推荐按以下顺序运行。`--limit 0` 仅在 CLI 表示冻结 manifest 全量；正常重跑会复用 `manifest SHA + 输入摘要 + 代表帧 SHA + 模型版本` 缓存。除非模型、输入或提示词已变化，不要使用 `--force`，以免重复计费。

```bash
dso bailian-vector-status --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-run --stage preflight --limit 10
dso bailian-vector-run --stage smoke --limit 10 --top-n 10 --judge-limit 5
dso bailian-vector-run --stage embeddings --limit 0
dso bailian-vector-run --stage rerank --limit 0 --top-n 20
dso bailian-vector-run --stage judge --judge-limit 20
```

`preflight` 不读取 Key、不调用网络、不产生费用，会使用真实本地帧校验 JPEG、Base64、请求 schema、序列化大小和最坏预算。超过 1280px/1MB 的图片只在缓存目录生成源 SHA 绑定的派生 JPEG，原文件不变。当前冻结 manifest 包含 240 条样本；ECS 全量预检得到 `480/480` 个 Text/Fusion 请求通过，但含一次重试的最坏预留约 `7.7589 CNY`，高于当前 1 元日限额，因此必须先做小批而不是直接全量。

完整研究计划最多为 480 次 Text/Fusion Embedding、120 次 Rerank 和 80 次双模型 Judge；只上传结构化摘要与代表帧，不上传完整视频。开始真实样本调用前应先检查数据许可，再核对 Smoke 的返回 schema、usage、实际账单、失败率和缓存命中。

完整短片不属于上述向量/Judge 计划。只有用户明确授权某个冻结批次时，运维人员才能使用 `scripts/run_bailian_complete_short_clip_shadow.py`。命令默认只预检；只有增加 `--execute` 才会调用网络。Adapter 仍会强制固定 Qwen3.7 快照、本地 MP4 Base64、2–60 秒、3.5 MB、`fps<=2`、无音轨语义、`full_media` 许可、固定 JSON schema 和 0 次重试。不要把 `full_media` 写入日常 EnvironmentFile，也不要用它上传完整节目。首条实测见 [Qwen3.7 完整短片单样本报告](research/evaluations/qwen37-complete-short-clip-shadow-20260719.md)。

需要同时分析画面、语音、歌词、音乐和音效时，使用独立的 `scripts/run_bailian_qwen35_omni_short_clip_shadow.py`，不要复用 Qwen3.7 视觉 profile。该命令同样默认只做零网络预检，`--execute` 才发送一次请求；Adapter 固定 `qwen3.5-omni-plus-2026-03-15`，要求 2–60 秒、3.5 MB 内的 H.264/AAC MP4、完整音轨、`fps<=2`、仅文本输出、流式 usage、模态分项价格、`full_media` 当次进程许可和 0 次重试。输出歌词仍需与人工听写或独立 ASR 核验，不能因与烧录字幕一致就视为已证实。首条同样本成本对照见 [Qwen3.5-Omni 完整短片单样本报告](research/evaluations/qwen35-omni-complete-short-clip-shadow-20260719.md)。

冻结清单需要批量直接分析完整短片时，使用 `scripts/run_bailian_complete_clip_batch.py`。脚本先按源 SHA 校验每条媒体，再生成保留完整时域和音轨的 H.264/AAC 代理；它不会生成代表帧，也不会把互动标签、账号或旧策略结果放入请求。默认不加 `--execute` 时只做转码、schema 和费用预检：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_bailian_complete_clip_batch.py \
  --manifest <frozen-manifest.json> \
  --media-root <complete-clip-directory> \
  --limit 10 --hard-budget-cny 10 \
  --batch-id <unique-research-batch> \
  --output <preflight-report.json>
```

只有预检通过、用户逐批授权且当次进程显式允许 `full_media` 时才能追加 `--execute`。脚本最多处理 10 条、0 次自动重试，批次预留超过 10 元会在网络前停止；日常 EnvironmentFile 仍不得持久开放 `full_media`。首轮 10 条实测只有 7 条通过严格 schema，模型传播分未通过排序诊断，详见 [完整短片批量报告](research/evaluations/qwen35-omni-complete-clip-batch-20260719.md)。

后续传播研究使用 `scripts/run_bailian_propagation_features.py`，不要继续读取 Omni 原生传播分。该入口只抽取音画事实，Provider 请求不包含账号、标题、标签或平台结果；全部响应完成后才在本地生成 `propagation_feature_outcome_dataset.v1`。命令支持 1-100 条，默认 10 条和 50 元批次异常停止线，实际规模及硬上限均需显式记录：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_bailian_propagation_features.py \
  --manifest <frozen-manifest.json> \
  --media-root <complete-clip-directory> \
  --limit 60 --hard-budget-cny 50 \
  --batch-id <unique-feature-batch> \
  --output <feature-report.json>
```

不加 `--execute` 时只转码和预检。正式运行仍需当次进程允许 `full_media`，不会把该权限写入日常配置。报告中的 `visible_engagement_heat` 是可见互动代理；只有同时存在真实播放/曝光分母时才计算 `share_rate`，只有同时存在分母和新增关注时才计算 `follow_conversion_rate`，观看质量还要求 5 秒留存、平均观看比例和完播率。缺失时显示 `unavailable_*`，不会用 0 补齐。首轮 v2 在冻结 10 条上达到 10/10 schema，通过的是特征抽取门禁，不是排序或流量门禁；详见 [传播特征试验](research/evaluations/qwen35-omni-propagation-features-20260719.md)。

若只有个别长片因输出 Token 上限截断，应从主报告生成只含失败 sample ID 的受限恢复 manifest，使用显式 `--output-tokens` 提高上限并单独预检/执行，再通过验证脚本的 `merge` 子命令合并。输出上限属于缓存身份，不能用不同参数静默复用旧缓存，也不能手工把 fallback 改成成功。

冻结新的跨账号高低互动配对和运行留一账号对照时，使用独立验证脚本。`build` 会校验真实音轨、时长、源 SHA 和近重复，并写不可变 manifest；`evaluate` 只在 Omni 结果覆盖完整时比较账号隔离 v2.4、Omni 事实特征和固定 85/15 融合：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_propagation_feature_validation.py build \
  --db-path data/db/dso.sqlite3 \
  --media-root data/douyin_media_assets \
  --repo-root . \
  --benchmark-dir benchmarks \
  --exclude-manifest <prior-complete-video-manifest.json> \
  --pair-count 30 --min-accounts 8 --max-duration-delta 5 \
  --output <new-frozen-manifest.json>

PYTHONPATH=src .venv/bin/python scripts/run_propagation_feature_validation.py evaluate \
  --manifest <new-frozen-manifest.json> \
  --feature-report <omni-feature-report.json> \
  --db-path data/db/dso.sqlite3 \
  --omni-weight 0.15 --top-k 15 \
  --output <account-holdout-report.json>
```

当前门禁要求 60 条完整覆盖、至少 8 个账号、固定融合总体成对命中和样本充足账号宏平均均较账号隔离 v2.4 至少增加 5 个百分点、至少 3 个样本充足账号改善且无样本充足账号回退。满足门禁也不自动改 v2.4；可见互动代理是结果富集研究标签，不等于曝光、播放量、分享率、关注转化或观看质量。

常用 API：

| API | 用途 |
| --- | --- |
| `GET /providers/status` | 查询注册 Provider、启用状态和安全边界，不返回密钥 |
| `GET /providers/config` | 查询连接配置和安全提交状态，只返回密钥是否已配置 |
| `POST /providers/config` | 通过 HTTPS 或 SSH 本地端口转发保存连接配置；保存不启用、不调用模型 |
| `POST /providers/fake-smoke` | 执行零网络、零费用的端到端 Smoke；只用于研究验收 |
| `GET /learning/multimodal-vector-experiment/cloud/status` | 查询冻结 manifest、百炼门禁、云向量覆盖和预计请求规模；零公网调用 |
| `POST /learning/multimodal-vector-experiment/cloud/run` | 显式运行 Preflight/Smoke/Embedding/Rerank/Judge；Preflight 零网络，Web 不接受 `full` |
| `POST /learning/multimodal-vector-experiment/cloud/holdout/{action}` | D12-B `freeze / predict / evaluate` 三步独立留出；预测批次硬上限 10 元 |
| `POST /learning/multimodal-vector-experiment/cloud/evidence-quality/rebuild` | D12-C1 本地三窗口证据包和分层参考缓存对照；0 元、0 请求 |

真实调用前仍必须由管理员显式提供控制台隔离、数据许可、保留政策引用、环境变量密钥和硬预算，并先以 3 条合成 Smoke 校准 usage/账单，再在冻结 benchmark 以 Shadow 模式比较质量、费用、P50/P95、失败率和缓存命中率。未通过门禁时，候选始终使用本地基线。

### 7.8 本地模型持久调度（Phase 0–3 batch-1 基线）

调度器默认关闭。逐窗口 Omni、逐块 Qwen3-ASR 和 Text/Visual Embedding 已共用 `model_scheduler.v1`，CPU/IO 媒体准备可以并发，但同一物理 GPU 始终只执行一个推理 item。

```bash
# 终端 1：启动独立 GPU Worker
DSO_MODEL_SCHEDULER_ENABLED=1 dso model-worker --resource gpu:0

# 终端 2：启动 Web/API
DSO_MODEL_SCHEDULER_ENABLED=1 dso web

# 运维检查
dso model-scheduler-status
dso model-jobs --status queued --limit 20
dso model-job-cancel <job_id>
dso model-scheduler-reconcile
dso model-scheduler-benchmark --manifest benchmarks/model-scheduler-mixed-20260718-r1.json
```

启用后，“智能切片”会先显示规则候选，Omni 进入 `model_scheduler.sqlite3` 后由前端轮询完成状态；ASR 提取会先返回已有音频特征/转写基线并显示音频块进度，成功后再继续候选生成；Embedding 补库同样以 job 状态返回。重复请求会合并到现有 job，Worker 或 Web 重启后可以恢复；关闭开关即可回到原同步路径。

如果目标 GPU 主机尚未安装 Resource Agent，必须先用现有运维命令让本批任务对应模型驻留；模型不匹配时任务会受限重试并保留规则、Whisper 或已有结果，不会伪造零分。受控自动切模的部署方式：

```bash
# 在目标 GPU 主机执行；脚本只安装仓库预定义 Profile/服务
./scripts/server_prepare_gpu_resource_agent.sh

# Web 与 Worker 从密钥环境读取，不要写入仓库或前端
export DSO_GPU_RESOURCE_AGENT_URL=http://<gpu-host>:8010
export DSO_GPU_RESOURCE_AGENT_TOKEN=<secret>
```

安全边界：Agent 使用 Bearer token 和单调 fencing token，只接受白名单 Profile 及固定 wrapper/systemd 命令，不接受任意 shell、unit 或模型路径。局域网 Agent 已部署并完成短 canary；冻结合成 benchmark 与 19.1 秒 canary 都不能替代长节目/批量真实 GPU 显存、OOM、空闲间隙和输出等价验收，状态继续保持 `validate`。

macOS 局域网常驻部署使用 Keychain 保存 Agent token，launchd 配置不包含密钥：

```bash
TOKEN=$(ssh owen "sed -n 's/^DSO_GPU_RESOURCE_AGENT_TOKEN=//p' /home/aidev/dso_gpu_resource_agent/resource-agent.env")
security add-generic-password -U -a "$(id -un)" -s dso-gpu-resource-agent -w "$TOKEN"
unset TOKEN
./scripts/install_lan_scheduler_launchd.sh

launchctl print "gui/$(id -u)/com.dso.lan-model-worker"
launchctl print "gui/$(id -u)/com.dso.lan-web"
curl http://127.0.0.1:8127/model-scheduler/status
```

局域网 Web 默认监听 `127.0.0.1:8127`，避免未经认证暴露到整个 LAN；原有 8000 服务不会被安装脚本替换。Worker 与 Web 通过 `scripts/run_lan_service.sh` 从 Keychain 取 token，使用正式 `data/db/model_scheduler.sqlite3`。回滚时执行：

候选详情和历史证据是只读路径：Scheduler 启用后只复用缓存向量，缺失向量显示 `deferred_scheduler`/证据不足，不会为了打开页面切换 GPU 模型。需要补向量时使用 `qwen-embeddings-build` 或对应 API 显式提交 job。

```bash
launchctl bootout "gui/$(id -u)/com.dso.lan-model-worker"
launchctl bootout "gui/$(id -u)/com.dso.lan-web"
```

## 8. CLI 快速手册

完整主流程：

```bash
dso init
dso doctor
dso precut-import ./clips/*.mp4 --account main --batch-title "本周候选"
dso ingest ./program.mp4 --account main --title "第 1 期"
dso download-video "https://v.qq.com/x/cover/.../...html" --dry-run --acknowledge-noncommercial
dso extract <video_id>
dso generate-segments <video_id> --top-k 30
dso score <video_id>
dso suggest <video_id> --top-k 10
dso export <segment_id>
dso import-metrics ./douyin_metrics.csv
dso training-samples --account main
dso baselines --account main
dso provider-status
dso provider-smoke --repeat 2
dso bailian-vector-status --benchmark-id dso-multimodal-vector-value-20260719-r1
dso bailian-vector-run --stage smoke --limit 10 --top-n 10 --judge-limit 5
dso web --reload
```

运维和排查：

```bash
dso doctor
dso videos
dso manifest <video_id>
dso verify-asr <segment_id> --profile verify
dso review-segment <segment_id> --status approved --reason "人工确认"
dso historical-summary --account ''
dso research-coverage --account main --dataset all
```

如果 `dso` 命令不可用，可使用：

```bash
PYTHONPATH=src python3 -m dso.cli <command>
```

## 9. 常见问题

### 9.1 页面打不开

确认服务已启动：

```bash
dso web --host 127.0.0.1 --port 8000
```

如果 8000 端口被占用：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
dso web --host 127.0.0.1 --port 8001
```

### 9.2 “运行环境”显示 FFmpeg 缺失

安装 FFmpeg 后重新运行：

```bash
dso doctor
```

FFmpeg 缺失时，视频探测、音频提取和导出都可能失败。

### 9.3 ASR 显示占位降级

先查看诊断：

```bash
dso doctor
```

推荐处理：

```bash
dso setup-asr --profile fast
dso setup-asr --profile quality
```

也可安装 faster-whisper：

```bash
python3 -m pip install -e ".[asr]"
```

如果已有准确字幕，可以提供同名 `.srt` 字幕作为 sidecar 兜底。

### 9.4 候选为空

按顺序检查：

```bash
dso videos
dso extract <video_id>
dso generate-segments <video_id> --top-k 30
dso score <video_id>
dso suggest <video_id> --top-k 10
```

Web 上也可以在“节目管理”点击“处理选中”重新跑全流程。

### 9.5 导出失败或被阻断

常见原因：

| 原因 | 处理方式 |
| --- | --- |
| 候选被标记为暂缓 | 在右侧“决策”中重新复核状态 |
| 未完成评分 | 先运行“评分” |
| 严格授权模式未录入授权 | 使用 `dso rights set ...` 录入授权 |
| 低原创风险过高 | 修改切片策略或放弃该候选 |
| FFmpeg 缺失 | 安装 FFmpeg 并运行 `dso doctor` |

### 9.6 指标导入后没有训练样本

检查 CSV 是否包含可关联字段：

| 推荐字段 | 说明 |
| --- | --- |
| `candidate_segment_id` | 最直接，导入后应生成训练样本 |
| `slice_variant_id` | 需要对应已有 Variant |
| `experiment_id` | 需要对应已有 Experiment |
| `platform_item_id` | 需要先在候选详情绑定平台 item |

导入结果中如果出现 `unlinked_rows`，说明部分行未关联到候选，因此不会生成训练样本。

### 9.7 研究样本数量和源文件行数不一致

这是预期行为。源文件可能包含重复作品，正式入库会按账号、平台和作品 ID 去重。以模型和界面学习指标为准时，应优先看“正式历史样本”“源去重”和“可训练样本”，不要直接使用源文件行数。

### 9.8 OAuth 显示未配置

检查环境变量：

```bash
echo $DSO_DOUYIN_CLIENT_KEY
echo $DSO_DOUYIN_CLIENT_SECRET
echo $DSO_DOUYIN_REDIRECT_URI
```

配置后重启 Web 服务，并在“研究学习 > 平台账号”重新查看状态。

### 9.9 远程视频下载不可用或被拒绝

- 提示 `optional videodl runtime is unavailable`：执行 `python3 -m pip install -e ".[videodl]"`。
- 提示 `DRM-protected`：该资源不进入下载链路；系统不会解密或绕过保护。
- 提示域名不支持：先把授权媒体下载为本地文件，再使用 `dso ingest`。
- 提示 YouTube URL 必须是单视频：改用 watch、`youtu.be`、shorts、live 或 embed 链接；playlist/channel URL 不会批量展开。
- YouTube 下载前先检查 dry-run 的 `selected_format` 和预计字节数；720p 使用独立音视频时必须确保本机 FFmpeg 可用。
- 提示非商业确认：仅在符合上游许可且已确认用途后添加 `--acknowledge-noncommercial`。

## 10. 日常运营 SOP

每期节目推荐按以下顺序操作：

1. 启动服务并打开工作台。
2. 在左侧导入节目，填写账号和节目标题。
3. 在“节目管理”点击“处理选中”。
4. 查看质量 Gate，优先处理 ASR、广告口播、重复幻觉和低原创风险。
5. 进入“候选审核”，按综合分从上到下复核 Top 候选。
6. 对重点候选检查“字幕/ASR”和“历史先验”。
7. 将合格候选标记为“通过”，风险候选标记为“需复核”或“暂缓”。
8. 导出 9:16 MP4，在线播放检查画面、字幕、声音和封面。
9. 在平台外手动发布，不使用本系统自动发布。
10. 发布后绑定平台 item ID，导入授权窗口指标。
11. 刷新“研究学习”，查看账号基线、复盘摘要和校准队列。
12. 下一期前处理高优先级语义校准样本，并运行一次回测。

导出前检查清单：

| 检查项 | 通过标准 |
| --- | --- |
| 字幕 | 无明显错字、重复幻觉或时间错位 |
| 内容结构 | 开头有钩子，中段有上下文，结尾有情绪或互动点 |
| 画面 | 竖屏裁切主体清晰，关键人物不被裁掉 |
| 标题 | 与内容一致，不夸大或误导 |
| 封面 | 能体现歌手、舞台高潮、导师反应或情绪点 |
| 授权 | 节目、歌曲、表演、艺人、平台使用范围已确认 |
| 风险 | 低原创、广告口播、平台规则风险已复核 |

## 11. 关键术语

| 术语 | 解释 |
| --- | --- |
| Source Video | 导入的长节目视频 |
| Candidate Segment | 系统生成的候选切片 |
| Slice Variant | 某个候选的包装版本，包括标题、封面、字幕样式和导出文件 |
| Publishing Experiment | 发布实验记录，用于关联发布窗口、标题和表现指标 |
| Performance Metrics | 授权导入的表现指标 |
| Training Sample | 由可链接指标快照生成的训练样本 |
| Historical Capture Sample | 正式入库的历史研究样本 |
| Reward Proxy | 多指标综合得到的互动热度代理分 |
| Interest Clock | 发布时间趋势建议 |
| Prototype Bank | 高互动内容原型库 |
| Quality Gate | 发布前质量哨兵 |
| Research Ranker | 基于历史证据的候选排序器 |

## 12. 管理员 API 入口

日常推荐优先使用 Web 和 CLI。需要集成或排查时，可访问这些 API：

| API | 用途 |
| --- | --- |
| `GET /runtime` | 运行环境诊断 |
| `GET /providers/status` | 查询 G3 Provider 安全底座状态，不返回密钥 |
| `GET /providers/config` | 查询 Provider Web 配置与安全提交状态，不返回密钥 |
| `POST /providers/config` | 仅经 HTTPS/SSH loopback 安全保存连接和预算，强制保持公网调用关闭 |
| `POST /providers/fake-smoke` | 运行零网络、零费用的 Provider Smoke |
| `GET /learning/multimodal-vector-experiment/cloud/status` | 查询百炼向量研究链路门禁、缓存覆盖和计划规模，不调用模型 |
| `POST /learning/multimodal-vector-experiment/cloud/run` | 显式运行有上限的百炼 Shadow 阶段；`full` 仅允许 CLI |
| `POST /learning/multimodal-vector-experiment/cloud/holdout/{action}` | 冻结 D12-B 配置、生成不可变盲预测或解锁评估；始终 research_only |
| `POST /learning/multimodal-vector-experiment/cloud/evidence-quality/rebuild` | 重建 D12-C1 三窗口证据并诊断分层 high/low 参考覆盖，不自动调用公网模型 |
| `GET /ranking/policy` | 查询当前生产排序策略、研究状态和 promotion gate 门槛 |
| `GET /model-scheduler/status` | 查询队列、活动 lease、runtime 和最近一小时调度指标 |
| `GET /model-scheduler/resources` | 查询 GPU resource lease 与驻留状态 |
| `GET /model-jobs/{job_id}` | 查询持久模型任务状态和条目进度 |
| `GET /model-jobs/{job_id}/events` | 查询任务状态转换和安全错误事件 |
| `POST /model-jobs/{job_id}/cancel` | 幂等取消任务 |
| `POST /model-jobs/{job_id}/retry` | 为允许重试的终态任务创建新 job |
| `GET /videos` | 节目列表 |
| `POST /videos` | 上传节目 |
| `POST /precut-batches` | 批量上传已切短片并可排入后台处理 |
| `GET /precut-batches` | 查询最近批次 |
| `GET /precut-batches/{batch_id}` | 查询批次进度、条目和跨文件排名 |
| `POST /precut-batches/{batch_id}/process` | 继续或重跑批次特征提取和共享排序 |
| `POST /videos/{video_id}/extract` | 提取 ASR 和音频特征；调度启用时返回 HTTP 202、已有基线和逐块 job |
| `POST /videos/{video_id}/asr/shadow` | 调度启用时提交不覆盖主 transcript 的 Qwen3-ASR Shadow job |
| `POST /videos/{video_id}/segments` | 生成候选 |
| `POST /videos/{video_id}/score` | 评分 |
| `POST /videos/{video_id}/omni-rerank` | 调度启用时返回 HTTP 202、规则基线和 `model_job.v1` |
| `POST /learning/qwen-embeddings/build` | 调度启用时提交 Text/Visual Embedding job，结果在 fenced commit 后写入 |
| `GET /learning/multimodal-vector-experiment/status` | 查询冻结短片向量实验、覆盖、盲审进度和下一组 |
| `POST /learning/multimodal-vector-experiment/embeddings` | 只为 manifest 内 240 条评测/参考样本提交 Text/Visual job |
| `POST /learning/multimodal-vector-experiment/compare` | 刷新规则、v2.4、文本、视觉和融合的成对对照 |
| `POST /learning/multimodal-vector-experiment/reviews/{task_id}` | 保存独立盲审判断，不修改 Material Gold 或生产权重 |
| `GET /videos/{video_id}/suggestions` | 查询 Top 候选；默认 production，可显式传 `ranking_scope=research` 做研究对照 |
| `GET /videos/{video_id}/quality` | 查询质量哨兵 |
| `POST /segments/{segment_id}/export` | 导出候选 |
| `GET /segments/{segment_id}/history` | 查询历史相似和先验 |
| `POST /metrics/import` | 导入授权指标文件 |
| `GET /learning/historical-samples/summary` | 历史样本汇总 |
| `GET /learning/semantic-calibration/queue` | 语义校准队列 |
| `PATCH /learning/historical-samples/{sample_id}/labels` | 保存人工语义标签 |
| `POST /learning/historical-samples/{sample_id}/calibration/reopen` | 重新打开已保存样本的校准状态 |
| `GET /learning/material-gold-set/queue` | 获取素材形态 Gold Set 审核队列 |
| `PATCH /learning/material-gold-set/{sample_id}` | 保存素材形态人工确认 |
| `POST /learning/material-gold-set/{sample_id}/reopen` | 重新打开素材形态审核 |
| `POST /learning/material-gold-set/replay` | 运行 v2.9 素材形态层级校准回放 |
| `GET /learning/material-taxonomy` | 获取 D10 规范素材形态和旧标签派生规则 |
| `GET /learning/material-confusions/queue` | 获取跨账号平衡的 D10-A 定向错判队列 |
| `GET /learning/material-evidence/status` | 获取 D10-B 三窗口证据覆盖与最近 Resolver 摘要 |
| `POST /learning/material-evidence/extract` | 抽取 ASR、OCR、Omni 三窗口证据；只写 Shadow 缓存 |
| `POST /learning/material-resolver/shadow` | 运行 cached-eval-only Resolver 策略对比与研究门控 |
| `GET /learning/visual-window-scout/status` | 查询 D11 视觉准入、窗口 Gold、原型和最近扫描状态 |
| `POST /learning/visual-window-scout/build` | 生成下一批 15 秒视觉候选窗、三帧预览、冻结 build/manifest 和 Top2 Shadow 清单 |
| `GET /learning/visual-window-scout/builds/{build_id}` | 读取指定不可变 D11B build |
| `GET /learning/visual-window-scout/builds/{build_id}/manifest` | 读取并验证指定 build 的 SHA-256 manifest |
| `PATCH /learning/material-window-gold/{sample_id}` | 保存窗口级视觉形态、节目语境和选窗质量 |
| `POST /learning/visual-window-scout/experiment` | 单批或累计成对比较 fixed/text/visual/fusion 四种选窗策略 |
| `POST /learning/backtest` | 运行离线回测 |
| `GET /learning/qwen-omni/status` | 检查 Omni 低显存服务状态 |
| `POST /learning/qwen-omni/shadow-run` | 执行 Omni shadow-run |
| `GET /platform/douyin/summary` | 抖音账号同步摘要 |

## 13. 当前限制

- 历史研究样本主要用于趋势先验，不等同于真实发布效果承诺。
- 当前播放量缺失时，排序和原型解释基于互动热度代理分。
- 官方或授权账号的 6h / 24h / 72h / 7d / 30d 完整窗口指标仍需持续接入。
- 样本少于 300 的账号只输出低置信趋势。
- Qwen2.5-Omni 低显存模式只用于短片段离线研究；当前目标机不适合完整 BF16 版或 30 秒以上视频常驻分析。
- G3 已实现默认关闭、显式开启的 `AliyunBailianProvider`；ECS 已配置业务空间、密钥、预算、用户授权依据和厂商政策引用，现有 150/240 条真实研究样本具备 Text/Fusion 云向量，并已分别以 Qwen3.7 视觉和 Qwen3.5-Omni 音画 profile 跑通一条完整短片。D12-B 独立 20 对上固定融合相对 v2.4 增量为 0，单条完整短片只证明能力与成本，质量门禁未过，暂停全量与 Judge；Kimi Adapter 未实现，任何公网结果都不影响生产排序。
- 系统不会自动发布或代替人工做最终内容合规判断。
