# 开发要求

本文档定义当前项目的开发约束、验证要求和数据管理规则。后续开发优先遵守本文档；如果历史计划文档与本文档冲突，以本文档为准。

## 1. 基本原则

- 本地优先：默认在本机处理视频、数据库、模型缓存和导出文件。
- 合规优先：不自动发布、不刷量、不绕过平台规则；抖音数据优先使用官方 API 或授权文件。
- 小步可验收：每次变更必须能说明改了什么、影响哪些接口、如何验证。
- 数据口径明确：界面、API 和文档必须区分源文件行数、去重作品数、正式入库样本数和训练样本数。
- 产物可追溯：清理数据库或重导入前必须保留备份。

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
| 数据规模、去重口径、原型库结果变化 | `docs/current-state.md` |
| 新增开发命令、测试门槛、数据规则 | `docs/development-requirements.md` |
| 数据表、模块边界、API 合同变化 | `docs/architecture.md` |
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
- 对数据有破坏性操作时已备份，并能说明备份路径。
