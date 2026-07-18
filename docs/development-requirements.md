# 开发要求

本文档定义当前项目的开发约束、验证要求和数据管理规则。产品目标和指标口径以 [product-goals.md](./product-goals.md) 为准；后续开发优先遵守这两份规范性文档。如果历史计划文档与二者冲突，以规范性文档为准。

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

只展示少量成功案例不能替代冻结 benchmark。新方案的状态和结论还应同步到 [model-and-algorithm-radar.md](./model-and-algorithm-radar.md)。

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
- 标题相同但视频 ID 不同，默认视为不同发布作品。

### 5.4 采集数据要求

- 新采集文件优先放在 `outputs/douyin_<program>_<YYYYMMDD>/` 或 `outputs/douyin_three_accounts_<YYYYMMDD>/accounts/<program>/`。
- Excel 导入必须支持 `.xlsx`，历史兼容 `.xslx` 拼写。
- 采集工作簿优先读取 `作品去重`、`作品明细`、`三账号作品`、`天赐作品`、`歌手2026作品`、`思绪作品`、`原始清洗记录`。
- 采集字段命名变化时，必须同步更新解析逻辑和文档口径。

### 5.5 授权远程媒体获取

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

- 默认保持本地优先，`whisper.cpp` 优先，faster-whisper 兜底。
- `fast=base` 用于批量默认模式。
- `quality=small` 用于复杂舞台、英文歌名、人名密集场景。
- `verify/premium` 可用于候选级复核，但不要直接替换全片默认策略。
- ASR 路由策略需要能解释为什么选择某个 profile。
- 重跑 ASR 必须明确是否绕过缓存，使用 `--force-asr`。
- 长音频 ASR 的 HTTP 200 只代表请求完成，不代表语义完整；必须记录逐块文本量、耗时、空结果、重试和最终 `ready/degraded` 状态。
- Qwen3-ASR 默认使用 60 秒窗口、低能量边界吸附和空 context；context 回显、有声音却空文本、异常慢且文本稀疏时必须缩块重试或显式标为未解决，不得静默进入候选生成。
- 切块长度、重叠、context 和恢复阈值属于 ASR 缓存键；修改后不得复用旧 transcript 冒充新配置结果。

## 9. 抖音账号和合规要求

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
| 采集字段、质量门、合规边界变化 | `docs/douyin-collection-standard.md` |
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
