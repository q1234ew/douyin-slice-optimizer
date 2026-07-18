# Douyin Slice Optimizer 平台用户手册

更新日期：2026-07-18
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

音乐、演唱和复杂背景声节目可切换到 GPU 服务器上的 Qwen3-ASR：

```bash
# 先在 GPU 服务器执行，ASR 与 Omni 串行占用显存
~/bin/dso-asr-on

# 再在应用主机执行
DSO_ASR_BACKEND=qwen3_asr \
DSO_QWEN3_ASR_SERVICE_URL=http://192.168.31.143:8002 \
dso extract <video_id> --force-asr

# ASR 批次结束后，在 GPU 服务器恢复 Omni
~/bin/dso-omni-on
```

Qwen3-ASR 默认把长音频切成不超过 60 秒的窗口，保留 1 秒重叠，并在目标切点前 5 秒内优先选择低能量边界；默认 context 为空。若某块出现有声音却空文本、异常慢且文本稀疏或 context 回显，客户端会清空 context 或缩成 30 秒窗口重试。每块的文本量、音频 RMS、尝试次数、恢复策略和最终质量状态会写入 `qwen3_asr_last_run.json`；仍未恢复的块会让本次运行标记为 `degraded`，不能只按 HTTP 成功判断转写完整。

可按节目覆盖配置，但 180 秒和固定热词串只建议用于复现实验：

```bash
DSO_QWEN3_ASR_CHUNK_SECONDS=60 \
DSO_QWEN3_ASR_BOUNDARY_SEARCH_SECONDS=5 \
DSO_QWEN3_ASR_RETRY_CHUNK_SECONDS=30 \
DSO_QWEN3_ASR_CONTEXT='' \
dso extract <video_id> --asr-backend qwen3_asr --force-asr
```

重跑成功后仍需重新生成候选与评分，旧候选不会自动改写。

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

CLI 等价命令：

```bash
dso import-metrics ./douyin_metrics.csv
dso rebuild-feedback --account main
dso training-samples --account main --limit 50
dso baselines --account main
```

### 7.3 平台账号和抖音同步

在“研究学习 > 平台账号”中选择账号后，可进行：

| 操作 | 说明 |
| --- | --- |
| 连接抖音账号 | 使用 OAuth 配置生成扫码授权入口 |
| Mock 同步 | 使用模拟数据验证链路 |
| 同步文件 | 上传 CSV、XLSX 或 JSON 文件进行授权数据同步 |
| 平台映射 | 在候选详情中绑定平台 item ID |

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

进入“研究学习 > 校准与回测”后，页面分为三个工作模式：

| 模式 | 用途 |
| --- | --- |
| 素材审核 | 默认入口；逐条确认 Material Gold Set，页面一次只展开一个样本 |
| 语义校准 | 修正类别、Hook、结构、艺人、歌曲和标签 |
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
dso backtest --account main --k 10 --strategy research_ranker_v2_1 --holdout-policy time
dso ranker-tuning-run --account main --k 10 --max-trials 12
dso backtest-reports --account main --limit 10
dso material-evidence-extract --limit 3 --window-seconds 8 --include-reviewed
dso material-resolver-shadow --limit 100 --include-reviewed
```

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

当前版本已经具备厂商无关的 Provider 合约、默认关闭策略、显式数据许可、请求/批次/日预算、内容缓存、独立调用台账、本地回退和 Shadow 评测，但尚未接入 Qwen、Kimi 等真实公网 Adapter。即使设置了某个 API key，系统也不会自动启用网络请求。

先查看只读状态：

```bash
dso provider-status
```

再用 Fake Provider 验证完整链路。`--repeat 2` 应看到第二次命中缓存，且网络调用数和估算费用都为 0：

```bash
dso provider-smoke --text "G3 local-only smoke" --repeat 2 --batch-id local-g3-smoke
```

运行数据分别写入 `data/cache/public_models/` 和 `data/db/public_model_ledger.sqlite3`。台账只保存 Provider/模型/提示词版本、输入规模、token、请求/重试/限流、延迟、缓存、费用和许可等审计元数据，不保存 API key、提示词正文或原始媒体。

常用 API：

| API | 用途 |
| --- | --- |
| `GET /providers/status` | 查询注册 Provider、启用状态和安全边界，不返回密钥 |
| `POST /providers/fake-smoke` | 执行零网络、零费用的端到端 Smoke；只用于研究验收 |

真实 Adapter 上线前仍必须由管理员显式提供数据许可、环境变量密钥和硬预算，并先在冻结 benchmark 以 Shadow 模式比较质量、费用、P50/P95、失败率和缓存命中率。未通过门禁时，候选仍使用本地基线。

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
| `POST /providers/fake-smoke` | 运行零网络、零费用的 Provider Smoke |
| `GET /ranking/policy` | 查询当前生产排序策略、研究状态和 promotion gate 门槛 |
| `GET /videos` | 节目列表 |
| `POST /videos` | 上传节目 |
| `POST /precut-batches` | 批量上传已切短片并可排入后台处理 |
| `GET /precut-batches` | 查询最近批次 |
| `GET /precut-batches/{batch_id}` | 查询批次进度、条目和跨文件排名 |
| `POST /precut-batches/{batch_id}/process` | 继续或重跑批次特征提取和共享排序 |
| `POST /videos/{video_id}/extract` | 提取 ASR 和音频特征 |
| `POST /videos/{video_id}/segments` | 生成候选 |
| `POST /videos/{video_id}/score` | 评分 |
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
- G3 目前只有零网络 Fake Provider；Qwen/Kimi 等真实公网 Adapter 尚未接入，未经显式数据许可、密钥、预算和冻结 Shadow 门禁不会启用。
- 系统不会自动发布或代替人工做最终内容合规判断。
