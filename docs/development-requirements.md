# 开发要求

本文档定义当前项目的开发约束、验证要求和数据管理规则。产品目标和指标口径以 [product-goals.md](product-goals.md) 为准；后续开发优先遵守这两份规范性文档。如果历史计划文档与二者冲突，以规范性文档为准。

## 1. 基本原则

- 北极星对齐：每项功能、模型和重构必须标明服务 G1（已切短片排名）、G2（完整节目智能切片）或 G3（本地/公网模型协同）以及目标指标。
- 双入口一链路：已切短片和自动生成短片在标准化后必须复用同一分类、排序、解释、审核和反馈 contract。
- 本地优先：默认在本机处理视频、数据库、模型缓存和导出文件。
- API 按需：公网模型默认关闭，只有在数据许可、预算、缓存、超时、降级和成本观测完整时才允许启用。
- 合规优先：不自动发布、不刷量、不绕过平台规则；抖音数据优先使用官方 API 或授权文件。
- 小步可验收：每次变更必须能说明改了什么、影响哪些接口、如何验证。
- 证据准入：新算法或模型必须与冻结基线比较，并同时报告质量、延迟、资源/API 费用和失败率。
- 主动汇报：发现有助于目标的新模型或算法时，更新 `model-and-algorithm-radar.md` 并在当次结论中告知用户。
- 数据口径明确：界面、API 和文档必须区分源文件行数、去重作品数、正式入库样本数和训练样本数。
- 产物可追溯：清理数据库或重导入前必须保留备份。

### 1.1 G1 已切短片入口不变量

- 一个已切短片源文件只对应一个标准化候选，时间范围必须是 `0 -> source.duration_seconds`。
- `boundary_locked=1` 后不得通过页面、API、CLI 或直接数据库更新修改开始、结束、时长或解锁；重新切边必须作为新的源文件重新导入。
- 批量导入按 `account_id + SHA-256` 去重；重复文件复用已有源视频、候选和评分，但保留本次批次条目追踪。
- G1 不调用完整节目候选生成器；G1/G2 标准化后必须共同调用 scorer、ranker、解释、审核、导出、平台映射和反馈 contract。
- ASR、音频或可选模型失败必须写入条目级降级说明，不能改写原始边界，也不能用伪特征掩盖失败。
- 批量任务必须允许部分失败、状态查询和重试；单条失败不得使其他成功条目丢失。

## 2. 环境要求

后端：

- Python `>=3.11`
- FastAPI、Uvicorn、Typer
- SQLite 本地数据库

前端：

- Node.js / npm
- Vue 3、Vite、TypeScript

安装：

```bash
python3 -m pip install -e ".[dev]"
cd frontend
npm install
```

仅在需要从已获授权的腾讯系或 YouTube 单视频 URL 获取测试媒体时安装可选依赖：

```bash
python3 -m pip install -e ".[videodl]"
```

该 extra 当前固定 `videofetch==0.9.1`，其上游许可为 PolyForm Noncommercial 1.0.0，不得把非商业确认自动化或用于商业生产。

当前本机可用 Python：

```bash
/usr/local/Cellar/python@3.11/3.11.5/bin/python3.11
```

## 3. 服务启动要求

后端服务从项目根目录启动：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli web --host 127.0.0.1 --port 8000
```

如果 8000 被占用：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
kill <pid>
```

前端开发：

```bash
cd frontend
npm run dev
```

混合切片验证：

```bash
PYTHONPATH=src python3 -m dso.cli hybrid-slice <video_id> \
  --top-k 10 --candidate-limit 3 --max-clip-seconds 6 --omni-weight 0.15 --load-model
```

Omni 未就绪时命令必须返回 `status=fallback`，并确保所有候选仍有 `hybrid_score`；模型就绪时仅对预筛候选执行多窗口推理。

前端生产构建：

```bash
cd frontend
npm run build
```

构建产物会写入 `src/dso/api/static/dashboard/`。不要手工编辑这个目录里的构建文件，应修改 `frontend/src/` 后重新构建。

### 3.1 本地模型调度要求

调度实现以 [model-scheduling-architecture.md](architecture/model-scheduling-architecture.md) 为准，并遵守以下不变量：

- ASR、Omni、Embedding 等竞争同一物理 GPU 的任务必须经统一 `model_scheduler.v1` lease；Scheduler 启用时普通业务调用不得绕过 lease 直接调用 GPU 服务。
- 模型队列必须持久化并由独立 Worker 消费，不能把 FastAPI `BackgroundTasks`、线程锁或前端未等待的 Promise 当作可靠任务系统。
- 每个物理 GPU 同时最多一个有效推理 attempt；lease 必须有 heartbeat、expiry 和 fencing token，过期 attempt 不得提交业务结果。
- API 必须先返回规则、历史先验、Whisper 或已有缓存基线，并提供 job 状态；模型繁忙是 `waiting_resource`，不是伪成功或零分。
- 相同输入 hash、模型、运行时、提示词、参数和媒体 profile 必须复用完成缓存或合并 active job，不能重复计费或重复占用 GPU。
- 任务取消采用协作式语义；运行中的单个推理单元可以完成或超时，但取消后不得继续派发后续 item。
- 调度状态和高频心跳使用独立 `model_scheduler.sqlite3`；跨调度库与业务库通过 staged artifact 和幂等提交恢复，不假设跨库事务。
- 公网 Provider 不得作为本地 GPU 繁忙时的隐式溢出路径；外部调用仍必须通过数据许可、预算、缓存、台账和 Provider policy。
- 调度器首版不得改变模型、提示词、候选边界、排序权重、人工 Gold、导出或发布状态；批处理和自适应窗口必须单独通过冻结 benchmark。
- 当前 Phase 0–3 batch-1 基线通过 `DSO_MODEL_SCHEDULER_ENABLED=1` 显式启用；必须同时运行独立 `dso model-worker --resource gpu:0`。Omni、Qwen3-ASR 和 Text/Visual Embedding 已迁移 Adapter；新增 GPU 调用仍必须先迁移，lease guard 不允许静默绕过。
- Scheduler 默认关闭；未配置 `DSO_GPU_RESOURCE_AGENT_URL`/`DSO_GPU_RESOURCE_AGENT_TOKEN` 时，必须预先确认任务对应模型已驻留。Resource Agent 只能接受服务端白名单 Profile 和单调 fencing token，不得接受任意 shell、systemd unit、模型路径或来自前端的密钥。
- Resource Agent 健康检查与模型激活必须使用不同超时：健康检查默认 5 秒，激活默认 1800 秒并覆盖当前 8–45 秒实测切模；不得用短健康超时误判仍在正常加载的模型切换。
- Scheduler 启用时，候选详情、历史证据等读取接口只能复用已缓存模型结果；缺失向量必须返回 `deferred_scheduler`/弃权并由显式构建 job 补齐，读取请求不得调用 `/load` 或直接触发 GPU 推理。
- 正式启用前必须运行真实冻结混合 workload，验证零跨模型并发、输出等价、显存/OOM、GPU 空闲间隙、切模恢复和失败降级；合成 benchmark 只能验证队列/亲和/公平 contract，不能替代真实 GPU 验收。

### 3.2 公网 Provider 治理要求

- 公网调用顺序固定为：总开关/Provider/数据许可门禁、确定性本地缓存、最坏费用预留、网络调用、实际 usage 结算、固定字段台账和本地回退；缓存命中不得预留或消耗付费预算。
- 预算必须同时限制单请求、单批次和单日。预留覆盖全部允许重试；成功后按实际 usage 结算并释放余额，实际费用超过预留时 fail closed；响应丢失等未知账单按全额预留保守入账。
- 共享同一 Provider 台账的多个进程必须在预算刷新、预留、网络调用、结算和最终台账写入的完整区间持有跨进程排他锁；每次预留前重新读取批次/当日保守费用。不得只依赖 `threading.Lock` 或进程启动时快照；跨进程锁不可用时必须拒绝公网调用并保留本地基线。
- 调用与每次网络尝试必须分别记录实际输入/输出/缓存 Token、原始 HTTP 响应字节、延迟、状态、Provider request ID、价表版本和账单状态；不得用请求前 Token 估算或解析后业务 JSON 长度冒充实际值。
- 外发许可必须记录可审计的 `retention_policy_reference`；有书面固定期限时同时记录非负整数 `retention_days`。厂商只声明按服务所需最短期限保留、未给固定天数时，可显式使用 `provider_minimum_necessary` 语义并记录 `retention_days_known=false`，但不得伪填 `0`、不得省略政策引用，也不得把它表述成已知零保留。
- OpenAI 兼容只代表传输形态相近，不允许一个通用参数字典跨厂商透传。每个 Adapter 必须有独立模型 allowlist、Host allowlist、请求字段 allowlist、响应 schema、价格、错误重试和媒体限制。
- `AliyunBailianProvider` 标准 profile 只允许华北 2 业务空间专属 HTTPS Host、fixed snapshot、非流式/非思考 JSON Mode、脱敏摘要和最多三张本地 JPEG。完整短片只允许用户逐批授权的两个隔离研究 profile：视觉 `complete_short_clip` 固定 `qwen3.7-plus-2026-05-26`，只收源 SHA 绑定的本地无音轨 MP4 Base64；全模态 `qwen35_omni_complete_short_clip` 固定 `qwen3.5-omni-plus-2026-03-15`，必须有完整 AAC 音轨、只请求文本输出、流式汇总 usage，并按文本/视频输入、音频输入和文本输出分项费用。二者都限制为 2–60 秒、3.5 MB、`fps<=2`、固定像素与输出上限、`full_media` 进程许可和 0 次重试；都禁止滚动模型别名、任意 URL、完整节目、工具、联网搜索和调用方自定义 messages/parameters。
- API Key 只从进程环境或权限为 `0600` 的 systemd EnvironmentFile 读取。状态 API、日志、缓存、调用台账和测试产物必须断言不含 Key、Authorization、提示词、字幕正文或 Base64。
- Web 密钥配置必须使用 `provider_admin_config.v1`：GET 只返回 `api_key_configured`，POST 只接受同源 JSON，且仅允许可信 HTTPS 反向代理或直接 loopback/SSH 本地端口转发。公网 HTTP 必须禁用输入并返回 403；前端不得把 Key 写入 localStorage、sessionStorage、URL、错误消息或响应，也不得硬编码 SSH 主机、用户或客户端密钥路径，只能显示通用占位符模板。
- Web 保存只允许原子写入权限为 `0600` 的独立 EnvironmentFile，并强制保持 `DSO_PUBLIC_MODEL_API_ENABLED=0`；保存动作不得构造真实 Runner、发起模型调用或消耗预算。Nginx 必须覆盖而非透传客户端提交的 `X-Forwarded-Proto`、`X-Forwarded-For` 和 `X-Real-IP`。
- ECS 的 systemd drop-in 使用 `deployment/systemd/dso-web-provider.conf`，把 `/etc/dso/bailian.env` 注入进程并只为 `/etc/dso` 增加写入白名单；不得为配置面板放宽其他 `ProtectSystem` 或 `ReadWritePaths` 范围。

## 4. 验证要求

### 4.1 后端或学习逻辑变更

最低验证：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m py_compile <changed_files>
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m unittest discover -s tests
```

如果改动涉及历史样本、数据源、原型库，还要执行：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli historical-summary --account main
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli prototypes --account main --source visible_capture --dataset all --limit 3
```

### 4.2 前端变更

必须执行：

```bash
cd frontend
npm run build
```

涉及 UI 展示、交互或文案时，还应通过浏览器检查：

- 页面能加载最新 bundle。
- 关键数字和标签没有歧义。
- 移动端和桌面端无明显遮挡。

### 4.3 数据库或数据清理

清理或重导入前先备份：

```bash
cp data/db/dso.sqlite3 data/db/dso.sqlite3.backup-$(date +%Y%m%d%H%M%S)
```

清理后必须复查：

```bash
sqlite3 -header -column data/db/dso.sqlite3 "SELECT COUNT(*) FROM historical_capture_samples;"
sqlite3 -header -column data/db/dso.sqlite3 "SELECT COUNT(*) AS duplicate_item_groups FROM (SELECT platform_item_id FROM historical_capture_samples WHERE platform_item_id != '' GROUP BY platform_item_id HAVING COUNT(*) > 1);"
```

### 4.4 文档-only 变更

如果只改 Markdown，不需要跑完整测试。但必须检查：

- 新文档能从 [docs/README.md](README.md) 找到。
- 命令路径和文件路径准确。
- 当前状态数字来自实际 API、CLI 或数据库查询。

### 4.5 模型、排序算法或公网 API 变更

除对应代码测试外，还必须提交或更新可复查的评测说明：

- 对齐的 G1/G2/G3 目标、当前问题和基线版本。
- 冻结数据集、时间/账号/节目切分方式以及防泄漏说明。
- Recall@K、NDCG@K、Top-K lift、严重错判、覆盖率或校准指标中的相关项。
- P50/P95 延迟、显存/内存、单节目/单候选 API 费用、缓存命中和失败率。
- Shadow、生产权重、人工 Gold、导出和发布行为是否发生变化。
- 超时、限流、模型不可用或预算耗尽时的本地回退结果。

只展示少量成功案例不能替代冻结 benchmark。新方案的状态和结论还应同步到 [model-and-algorithm-radar.md](model-and-algorithm-radar.md)。

排序策略准入还必须遵守：默认生产排序只能读取已明确采用的 production policy；`ranker_score`、`hybrid_score` 或模型分不得因为字段存在而隐式成为默认分。研究策略必须同时通过绝对门槛、相对 `current_rules` 与语义基线的强基线保护以及账号级门槛；即使通过，也只能标记为 `eligible_for_promotion`，需冻结新 benchmark 并显式修改 policy 后才可采用。

## 5. 数据开发要求

### 5.1 数据源和入库口径

- `GET /learning/datasets` 是源文件目录，不等于正式样本库。
- `sample_count` 在数据源目录里表示源文件有效行数。
- `unique_count` 表示按视频 ID 或稳定标题 key 去重后的作品数。
- `historical_capture_samples` 表示正式入库样本，模型优先读取这里。
- Dashboard 学习指标必须优先展示去重数和入库数，避免把源文件重复行当成模型样本。

### 5.2 去重规则

历史样本正式入库必须遵守：

- 有视频 ID 时，按 `account_id + platform + platform_item_id` 全局去重。
- 无视频 ID 时，按标题稳定 key 去重。
- 跨批次重复时优先保留更新日期更晚的数据集。
- 日期相同时优先保留播放量更高的数据。
- `all` 只作为聚合视图，不写入物理 `dataset_id=all`。

### 5.3 数据隔离

- 必须保留 `dataset_id` 和 `program_key`，支持节目/数据源隔离分析。
- 不同节目、不同账号、不同视频 ID 不应被标题相似性误合并。
- 已查看 test 后，下一轮可学习排序必须先通过 `interaction_heat_holdout_readiness.v1`：只允许显式 metric provenance 合格、sample ID 未在冻结集且 `published_at` 严格晚于冻结 cutoff 的样本进入前向候选；整账号候选必须来自冻结账号集合外，可包含较早发布但冻结后新收集的作品。默认门槛为前向至少 1,000 条、5 个账号、覆盖 7 天，以及至少 3 个新账号各 100 条。阈值如需调整，必须在读取新标签结果前先冻结理由和新阈值；`not_ready` 时不得把旧 test 改名复用，也不得据此解锁新的模型依赖或调参。
- 标题相同但视频 ID 不同，默认视为不同发布作品。
- 冻结 artifact 的 verifier 不得信任 manifest 提供的任意文件名；目录项必须与 contract 精确相等，只能以 no-follow 方式读取固定 basename 的普通文件，并拒绝绝对路径、`..`、内部/外部 symlink、FIFO/device 和 resolved path 越界。
- 自签 manifest SHA 只能证明内部一致。需要声明 artifact 未被整体重写时，必须从 artifact 外的可信记录传入 pinned manifest SHA-256；缺少 pinned digest 时 fail closed。

### 5.4 账号与结果证据隔离

- 历史采集账号默认是 `research_source`，只用于跨账号研究先验；不得因为账号 ID 出现在筛选器、平台映射或 `main` 默认值中就推断为目标发布账号。
- 目标发布账号必须显式设置 `account_role=publishing_target` 并具有稳定 `platform_account_id`；缺少目标结果时必须返回 `cold_start`。
- 平台映射必须带 `evidence_scope`。旧数据默认 `unclassified`，不得通过迁移自动升级为 `target_outcome`。
- 同一 `platform_item_id` 不允许静默跨本地账号重分配；需要改归属时必须走可审计的显式数据修复流程。
- `可见计数`、`计数数值`、`visible_count_number` 和 `best_visible_count_number` 属于模糊展示字段，禁止映射为 `views`。只有明确平台结果字段才能标记 `explicit_platform_outcome`。
- 旧平台指标默认 `legacy_unverified`，保留审计但不满足目标账号个性化或生产 promotion readiness。
- 离线研究 gate 和目标账号生产 readiness 必须分开返回、分开展示；前者通过不能覆盖后者的冷启动状态。

### 5.5 采集数据要求

- 新采集文件优先放在 `outputs/douyin_<program>_<YYYYMMDD>/` 或 `outputs/douyin_three_accounts_<YYYYMMDD>/accounts/<program>/`。
- Excel 导入必须支持 `.xlsx`，历史兼容 `.xslx` 拼写。
- 采集工作簿优先读取 `作品去重`、`作品明细`、`三账号作品`、`天赐作品`、`歌手2026作品`、`思绪作品`、`原始清洗记录`。
- 采集字段命名变化时，必须同步更新解析逻辑和文档口径。

### 5.6 授权远程媒体获取

- 只处理自有或已取得下载、处理许可的媒体；Provider 的开源/源码可见不等于媒体版权授权。
- 当前 `video_download.v1` 只允许经审计的 `TencentVideoClient`、受限 `YouTubeVideoClient` 和对应白名单域名，不启用上游通用解析器。YouTube 路径只调用 `videodl` 内置直连工具，必须禁用上游的 `ytdown`、`downr` 第三方解析服务。
- 不把 Cookie、账号、代理、设备凭据或 DRM/CDM 能力传给适配器；`has_drm=true` 必须在下载前拒绝。
- 默认 `max_items=1`，避免剧集链接意外批量下载；YouTube 只接受单视频 URL，必须去掉 `list/index` 等播放列表参数并拒绝 playlist/channel URL；线程和条数必须有硬上限。
- YouTube 当前最高选择 720p，优先 H.264 MP4，并选择默认 AAC/MP4 音轨交给 FFmpeg 合并；若目标没有兼容的独立音轨，只允许回退到带音轨的 MP4 渐进式清晰流，不得静默选择 1080p/AV1 增加资源成本或兼容风险。
- 下载目录必须按 job 隔离并写 manifest；默认根目录是当前工程的 `data/tmp/video_downloads`，并随 `DSO_ROOT` 切换，其他持久化根目录只能由 `--output-dir` 显式覆盖。只有显式启用 ingest 时才进入现有 `ingest_video` 和 G2 链路。
- 长下载不得进入同步 API 或阻塞健康检查；当前使用 CLI，未来若新增 API 必须采用可查询状态、超时和可恢复任务 contract。
- 最低验证为 `python3 -m pytest -q tests/test_video_download.py`，真实链接先使用 `--dry-run --acknowledge-noncommercial`，不得以 DRM 内容验证成功下载。

## 6. API 和 contract 要求

- 对外 API 返回应包含 `contract_version`，已有版本字段不得随意删除。
- 前端依赖的字段只能增量扩展，不能静默改名。
- 新增 API 要补 CLI 或文档入口，至少说明用途、入参、出参和验证命令。
- 数字口径要写进字段名或文案，例如 `数据集去重`、`入库样本`，避免只写“样本”。
- 失败信息要能指导下一步，例如 OAuth 缺少哪些环境变量。

## 7. 前端开发要求

- 源码只改 `frontend/src/`。
- 使用现有 Vue 3 + TypeScript + lucide-vue 体系。
- 新增数据展示时优先使用 API 中最接近业务含义的字段，不要在界面上混用源文件行数和模型样本数。
- 工具按钮使用图标加短文本，复杂说明不要塞进页面正文。
- 涉及业务口径的标签必须清楚，例如“数据集去重”“入库样本”“账号优秀线”。
- 构建后确认 `src/dso/api/static/dashboard/index.html` 引用的是新 bundle。

## 8. ASR 开发要求

- 默认保持自托管优先；音乐综艺 auto 路由以 Qwen3-ASR 为主，`whisper.cpp`、faster-whisper 依次兜底。
- `fast=base` 用于批量默认模式。
- `quality=small` 用于复杂舞台、英文歌名、人名密集场景。
- `verify/premium` 可用于候选级复核，但不要直接替换全片默认策略。
- ASR 路由策略需要能解释为什么选择某个 profile。
- 重跑 ASR 必须明确是否绕过缓存，使用 `--force-asr`。
- 长音频 ASR 的 HTTP 200 只代表请求完成，不代表语义完整；必须记录逐块文本量、耗时、空结果、重试和最终 `ready/degraded` 状态。
- Qwen3-ASR 默认使用 60 秒窗口、低能量边界吸附和空 context；context 回显、有声音却空文本、异常慢且文本稀疏时必须缩块重试或显式标为未解决，不得静默进入候选生成。
- 切块长度、重叠、context 和恢复阈值属于 ASR 缓存键；修改后不得复用旧 transcript 冒充新配置结果。
- Qwen3-ASR 完整节目 Shadow 必须写入独立 artifact，保持 `auto_promote=false`，不得修改 `source_videos.transcript_path/status`、Whisper 主转写、人工 Gold、候选边界或生产排序；服务未加载时返回 `waiting_model_switch`，不得写 placeholder 冒充 Shadow 成功。
- 音乐综艺 auto ASR 生产路由固定为 `Qwen3-ASR primary -> Whisper.cpp/faster-whisper fallback`；Qwen 不可用、失败或空结果时必须记录实际回退后端，且不得用 placeholder 覆盖可用旧转写。已缓存的同配置 Qwen 主转写不得仅因模型暂时卸载而被 Whisper 反向覆盖。

## 9. 抖音账号和合规要求

- 研究中心顶部账号选择器只筛选研究数据；平台连接、item 绑定和结果同步使用独立发布账号上下文，不得跟随研究筛选器切换。
- 生产链路优先接抖音开放平台 OAuth 和官方 API。
- 扫码登录需要配置：
  - `DSO_DOUYIN_CLIENT_KEY`
  - `DSO_DOUYIN_CLIENT_SECRET`
  - `DSO_DOUYIN_REDIRECT_URI`
- 可见数据采集仅作为研究和历史样本先验，不应替代授权数据源。
- 不在系统中实现自动发布、刷量、绕过风控或非授权抓取。

## 10. 文档维护要求

每次完成以下事项，都必须更新文档：

| 变更 | 更新文档 |
| --- | --- |
| 产品目标、成功指标、优先级或长期边界变化 | `docs/product-goals.md` |
| 数据规模、去重口径、原型库结果变化 | `docs/current-state.md` |
| 新增开发命令、测试门槛、数据规则 | `docs/development-requirements.md` |
| 数据表、模块边界、API 合同变化 | `docs/architecture.md` |
| 发现新模型/算法、完成 benchmark 或准入状态变化 | `docs/model-and-algorithm-radar.md` |
| 采集字段、质量门、合规边界变化 | `docs/guides/douyin-collection-standard.md` |
| 阶段性复盘和下一步计划 | 新增或更新 `docs/iteration-history-YYYYMMDD.md` |

文档不得只写结论，必须给出至少一种验证方式：命令、API、数据库查询或页面位置。

## 11. Definition of Done

一次开发任务完成前，至少满足：

- 代码或文档已经落盘。
- 相关测试或构建已通过，或明确说明为何无需执行。
- 服务需要重启时已经重启。
- 用户能从界面、API 或 CLI 看到结果。
- 当前状态和开发要求没有被新变更打破。
- 变更已说明对齐的 G1/G2/G3、目标指标、成本和降级方式；发现的新模型/算法已更新雷达并告知用户。
- 对数据有破坏性操作时已备份，并能说明备份路径。
