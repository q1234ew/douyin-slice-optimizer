# Douyin Slice Optimizer

本地优先的音乐综艺短视频切片 MVP：导入节目视频，识别高潜短视频片段，生成评分、标题、封面、字幕和样本说明，并导出 9:16 MP4。

## Documentation

项目文档入口见 [docs/README.md](docs/README.md)。

当前最重要的维护文档：

- [Agent 全局开发要求](AGENTS.md)：适用于整个仓库的目标、实现、评测、模型/API、验证和文档执行约束。
- [产品北极星目标](docs/product-goals.md)：已切短片排名、完整节目智能切片、公网模型 API 和持续迭代准入原则。
- [平台用户手册](docs/user-manual.md)：面向剪辑、运营和管理员的安装、Web 工作台、CLI、数据回流和排障说明。
- [当前工程状态](docs/current-state.md)：样本规模、数据口径、原型库状态、运行命令、已知缺口。
- [开发要求](docs/development-requirements.md)：测试门槛、数据去重、API contract、前端构建、文档维护规则。
- [系统架构](docs/architecture.md)：模块设计、数据模型、API 草案。
- [模型与算法雷达](docs/model-and-algorithm-radar.md)：候选模型、算法机会、验证状态、成本和主动汇报记录。
- [抖音采集标准](docs/douyin-collection-standard.md)：采集边界、字段规范、质量门和去重规则。

## Quick Start

```bash
python3 -m pip install -e ".[dev]"
dso init
dso setup-asr --profile fast
dso setup-asr --profile quality
dso doctor
dso ingest ./program.mp4 --account main --title "第 1 期"
dso extract <video_id>
dso extract <video_id> --asr-profile quality --asr-backend whisper_cpp --force-asr
dso bench-asr ./program.mp4 --backend whisper_cpp --profile compare --duration-seconds 60
dso generate-segments <video_id> --top-k 30
dso score <video_id>
dso suggest <video_id> --top-k 10
dso export <segment_id>
dso import-metrics ./douyin_metrics.csv
dso training-samples --account main
dso baselines --account main
dso qwen-omni-status
dso qwen-omni-shadow-run --account main --dataset all --limit 20 --max-clip-seconds 15 --load-model
dso qwen-omni-media-batch --limit 20 --max-clip-seconds 8 --load-model
dso web --reload
```

如果暂未安装 Typer，`python -m dso.cli ...` 会使用内置 argparse fallback。

## Web Frontend

Web 工作台已重构为 `Vue 3 + Vite + TypeScript`，源码位于 `frontend/`。后端仍由 FastAPI 提供 API 和静态资源服务：

```bash
cd frontend
npm install
npm run build
cd ..
dso web --reload
```

Vite 构建产物输出到 `src/dso/api/static/dashboard/`，FastAPI 会在 `/static/dashboard/...` 服务资源，并在 `GET /` 注入初始 `stats/videos` 状态。开发前端时可运行 `npm run dev`，Vite 已配置把现有 API 根路径代理到 `127.0.0.1:8000`。

## Notes

- 第一版不自动发布、不刷量、不绕过平台规则。
- 当前默认 `DSO_RIGHTS_MODE=trusted_sample`：你提供的合格 sample 数据不做版权/授权拦截，`rights_risk_score=0`，可直接评分和导出。
- 如需恢复严格授权检查，可设置 `DSO_RIGHTS_MODE=strict`，再使用 `dso rights set source_video <video_id> --program cleared --song cleared --performance cleared --artist cleared --platforms douyin --duration 90` 录入授权。
- ASR 本地优先：默认 `DSO_ASR_BACKEND=auto`，优先使用 `whisper.cpp`。项目会自动识别 `DSO_WHISPER_CPP_BIN` / `DSO_WHISPER_CPP_MODEL`，也会优先使用 `tools/whisper.cpp` 和 `data/models/whisper.cpp`；不可用时回退到 faster-whisper；未安装可用后端时会生成占位 transcript，便于流程调试。
- Qwen3-ASR 高质量后端：GPU 服务器使用 `Qwen/Qwen3-ASR-1.7B` 与 `Qwen/Qwen3-ForcedAligner-0.6B`，服务端口为 `8002`。运行 `scripts/server_prepare_qwen3_asr.sh` 可安装隔离环境、下载 ModelScope 权重并生成用户级 systemd 服务；服务器上的 `~/bin/dso-asr-on` 会释放 Omni 后加载 ASR，`~/bin/dso-omni-on` 会卸载 ASR 并恢复 Omni。应用侧用 `DSO_ASR_BACKEND=qwen3_asr DSO_QWEN3_ASR_SERVICE_URL=http://192.168.31.143:8002 dso extract <video_id> --force-asr` 启用。安全默认采用 60 秒、1 秒重叠、5 秒低能量边界搜索和空 context；有声音却空文本、异常慢且文本稀疏或 context 回显时会自动清空 context/缩成 30 秒重试，并在 `qwen3_asr_last_run.json` 显式记录恢复或未解决状态。
- Apple Silicon 加速建议使用 `whisper.cpp` 的 Metal/Core ML 后端。可用 `dso setup-asr --profile fast` 复用/安装项目本地 `base` 后端，并默认配置 Silero VAD；可用 `dso setup-asr --profile quality` 安装 `small` 质量模式模型。手动配置示例：`DSO_ASR_BACKEND=whisper_cpp DSO_WHISPER_CPP_BIN=/path/to/whisper-cli DSO_WHISPER_CPP_MODEL=/path/to/ggml-base.bin DSO_WHISPER_CPP_VAD_MODEL=/path/to/ggml-silero-v6.2.0.bin DSO_WHISPER_LANGUAGE=zh dso extract <video_id>`；未设置语言时默认按中文 `zh` 运行。
- ASR profile：`fast=base` 作为默认批量模式，`quality=small` 用于英文歌手、专有名词和复杂舞台段落复核；也可设置 `DSO_ASR_PROFILE=quality`，或用 `dso extract <video_id> --asr-profile quality --asr-backend whisper_cpp --force-asr` 对单个节目重跑 whisper.cpp small。
- 更高规格模型策略：`large-v3-turbo-q5_0` 已验证可作为候选级 `verify/premium` 复核模型，适合中文长口播、人名密集和节目叙事段；实测它在英文歌名/英文介绍上不总是优于 `small`，因此暂不作为全片默认替换。
- ASR 模型路由：`GET /videos/{video_id}/quality` 会给出全片与 Top 候选的路由建议；`dso verify-asr <segment_id> --profile auto` 会按片段信号选择 `quality` 或 `verify`，英文歌手/英文歌名场景会保留 `small` 结果，不被高规格模型自动覆盖。
- ASR transcript 会按音频 hash、profile、后端、模型、语言、prompt 和后处理版本做缓存去重；需要强制重跑时使用 `dso extract <video_id> --force-asr`。
- ASR 性能/质量对比使用 `dso bench-asr <audio_or_video> --backend whisper_cpp --profile compare --duration-seconds 60`，默认跑 `base,small`，输出 wall time、RTF、实际后端和 transcript 路径；也可继续用 `--models base,small` 显式指定。
- 中文 ASR 默认带音乐综艺热词 prompt，并会做基础热词修正、过短片段合并、重复幻觉过滤、广告口播标记；候选切片排序会降低品牌/广告口播密集片段的优先级。英文歌手/英文演唱不会被直接过滤，但缺少节目上下文或现场反应支撑的孤立英文歌词段会降权。
- faster-whisper 兜底默认模型为 `base`，可用 `DSO_WHISPER_MODEL=small` 提高准确度；可通过 `DSO_WHISPER_DEVICE`、`DSO_WHISPER_COMPUTE_TYPE`、`DSO_WHISPER_CPU_THREADS` 调整运行参数。
- Web UI 会在候选区展示最新导出的 9:16 MP4 在线预览；导出文件也可通过 `/exports/...` 静态路径访问。
- Web UI 和 `GET /videos/{video_id}/quality?top_k=30` 会展示发布前质量哨兵：ASR 后端/VAD、重复幻觉、广告口播、Top 队列闭环率、质量复核候选和下一步动作。
- Qwen2.5-Omni 低显存模式默认使用 `Qwen/Qwen2.5-Omni-7B-GPTQ-Int4`，仅作为 15 秒以内短片段 shadow 分析和语义校准建议，不自动写人工标签，不进入生产排序权重；支持 `--use-media --allow-windowed-clips --visual-ready-only` 对本地历史视频切窗后上传真媒体 payload。V1 Beta-D-6 新增 `research_ranker_v2_6_pool`，只做 Omni Top30 扩池研究门控和 trust profile，不替代 v2.4 Top10 排序。目标服务器准备脚本见 `scripts/open_server_proxy_tunnel.sh` 和 `scripts/server_prepare_qwen_omni.sh`。
- D10-B 使用 `material-evidence-extract` 对定向 Gold 优先队列真实执行 hook / middle / payoff 三窗口 ASR、中文 OCR 和 Omni 紧凑证据，并用 `material-resolver-shadow` 生成 cached-eval-only 策略对比。Gold 报告区分去重后可评估数、入队覆盖和证据覆盖；`unknown` 单独计为弃权，不并入严重错判。两者都只写 Shadow 缓存/报告，不会自动改 Gold、主语义标签或生产排序权重。
- D10-C 描述特征实验可用 `dso material-description-experiment --limit 6 --window-seconds 15 --windows-per-sample 3 --no-direct` 复现。它比较单 hook 与 hook/middle/payoff 三窗口的纯描述、命名信号和弱标题策略；结果写入独立缓存，不改 Gold 或生产排序权重。15 秒三窗口仅用于离线 Shadow，默认请求超时为 480 秒。
- D11B Visual Window Scout 已接入研究中心：每批默认平衡选择 10 条未审核样本，使用 15 秒候选窗和三帧预览建立窗口级 Gold；每次扫描都冻结独立 build 与 SHA-256 manifest，累计评测按样本去重并使用 leave-one-sample-out 原型。Qwen3-VL embedding 只接受目标模型返回的 2048 维真实向量，覆盖低于 90% 时阻断评测；`unknown/uncertain` 计为弃权，缺失策略显示 N/A。该流程保持 `research_only`，不改主语义标签或生产排序权重。
- D10-A/B 的唯一冻结基准为 `dso-v1-beta-d10-ab-20260715-r1`。运行 `dso benchmark-verify` 检查历史样本、Gold、Omni、D10-B 证据和源码是否漂移；只有校验通过后才可用 `dso benchmark-run` 生成可比较报告。冻结文件位于 `benchmarks/`，不得原地修改。
- 表现数据导入会同步生成指标快照、`reward_proxy`、训练样本和账号基线；公开数据仅建议作为人工研究和趋势先验，训练主数据应来自自有/授权/许可来源。

## Metrics CSV

最小字段：

```csv
candidate_segment_id,window_name,hours_since_publish,views,impressions,avg_watch_ratio,five_second_retention,completion_rate,rewatch_rate,likes,comments,favorites,shares,follows,negative_feedback
seg_demo,24h,24,10000,24000,0.74,0.82,0.51,0.08,600,180,220,90,35,12
```
