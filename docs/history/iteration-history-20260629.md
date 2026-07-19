# 2026-06-29 历史迭代复盘与下一步计划

生成日期：2026-06-29
定位：历史样本资产、V1 可学习排序闭环的阶段入口
范围：关注账号采集、正式样本库、数据口径、原型库、下一轮历史迭代任务。

## 1. 阶段结论

项目已经从 `V0.6 数据资产化 + 账号洞察` 推进到 `V1 Alpha 可学习排序闭环` 的准备阶段。

当前最重要的变化是：历史样本不再只是 Excel 或采集文件，而是已经进入正式样本库，并按账号、平台、视频 ID 去重。下一步的重点不应继续盲目扩大采集量，而应把这批样本用于历史相似、账号基线、原型解释、发布时间建议和轻量回测。

当前主线：

```text
关注账号库 -> 账号历史作品采集 -> 清洗去重 -> 正式入库 -> 账号基线 -> 原型发现 -> 历史相似 -> 候选排序解释 -> 回测
```

## 2. 最新数据资产

### 2.1 采集批次

本轮关注账号历史作品采集批次：

| 项目 | 值 |
| --- | --- |
| run_id | `20260628T155319Z_appleevents_post` |
| 账号库 | `data/douyin_capture/douyin_account_library_latest.json` |
| 采集报告 | `data/douyin_capture/douyin_account_collection_report_latest.json` |
| 采集账号数 | 15 |
| ready 账号数 | 15 |
| error 账号数 | 0 |
| 原始获取作品数 | 5,709 |
| clean 后作品数 | 5,554 |

说明：采集报告中的旧 `stored_sample_count` 可能包含清理前的历史标题兜底样本；当前正式口径以 `historical_capture_samples` 数据库查询和 `historical-summary --account ''` 为准。

### 2.2 正式样本库

当前 `historical_capture_samples` 正式样本库：

| 指标 | 数量 |
| --- | ---: |
| 正式入库样本 | 5,554 |
| 可训练样本 | 5,554 |
| 唯一作品键 | 5,554 |
| 重复视频组 | 0 |
| 播放量缺失率 | 100% |

互动字段覆盖率：

| 字段 | 覆盖率 |
| --- | ---: |
| 点赞 | 100.00% |
| 收藏 | 99.39% |
| 评论 | 97.34% |
| 转发 | 98.33% |

当前播放量不可得，必须继续保持为空或 0，不得用点赞、可见计数或其他互动数冒充播放量。界面和文档应使用“高互动”“高可见热度”描述当前模型结论，避免写成稳定的“高流量”。

### 2.3 账号样本分布

| 账号 | 正式样本数 |
| --- | ---: |
| `geshou2026` | 996 |
| `tianci` | 914 |
| `sixuweilive` | 874 |
| `hukan_music` | 300 |
| `jason_teacher` | 300 |
| `weibabibibi` | 300 |
| `ricot` | 299 |
| `dk_voice_teacher` | 297 |
| `haiye_yelaoshi` | 296 |
| `singer_yuhang` | 296 |
| `kim0330music` | 276 |
| `adai_valerio` | 106 |
| `duanduanzhengzheng` | 100 |
| `taotao_daxiaojie` | 100 |
| `wccyu` | 100 |

## 3. 当前口径决定

1. 正式样本库一条作品一行，优先按 `account_id + platform + platform_item_id` 去重。
2. `all` 只作为聚合视图，不写入物理 `dataset_id=all`。
3. `main` 不等于全量聚合。CLI 查询全量历史样本时使用 `--account ''`。
4. 模型训练和原型库优先消费 `historical_capture_samples`，不要直接消费源文件总表。
5. 当前 `views/play_count` 缺失，`reward_proxy` 使用点赞、评论、收藏、转发生成，只代表互动热度。
6. 可见数据采集只作为研究和历史先验；生产闭环仍应优先接官方 API 或授权账号指标。

## 4. 已完成工作

### 4.1 数据工程

- 已建立关注账号库，支持账号分级、账号类型、节目 key、来源类型等字段。
- 已按账号隔离保存 raw、clean 和质量报告。
- 已把 15 个账号历史作品导入正式样本库。
- 已清理重复样本，当前重复视频组为 0。
- 已修正 `play_count` 口径，不再把点赞或可见数回填为播放量。
- 已让 `/learning/datasets` 和数据反馈页展示源文件行数、源去重、入库样本、可训练样本。

### 4.2 原型和学习准备

- 已为账号级样本生成原型库。
- 原型表现口径支持在无播放量时回退到 `reward_proxy`。
- 原型文案已经从单纯“高流量”调整为按表现依据解释。
- 当前样本量已满足 V1 最低实验目标：超过 10 个账号，每个重点账号接近或超过 300 条。

### 4.3 验证状态

最近一轮验证已覆盖：

- 后端历史样本和 Web API 测试。
- 前端类型检查。
- 前端生产构建。
- 页面数据反馈显示 `5,554` 的正式入库口径。

## 5. 已知短板

1. `docs/current-state.md` 仍有旧的 992 样本口径，需要在下一次文档整理中同步更新。
2. 播放量缺失率仍为 100%，当前模型只能做互动热度判断。
3. 部分账号样本只有 100 条，只能用于趋势先验，不能输出强置信结论。
4. 原型库仍需要避免 `main/all` 回退到旧工作簿来源，后续应固定读取正式去重样本或在 UI 隐藏旧口径。
5. 内容类别、hook、切片结构、艺人、歌曲字段仍有半自动和规则识别成分，需要建立标注校准流程。
6. 官方/授权账号的 6h、24h、72h、7d、30d 窗口指标尚未接入，无法做稳定发布时间回流校准。

## 6. 下一轮历史迭代任务

### P0：先把历史样本变成可解释能力

1. **同步当前状态文档**
   - 更新 `docs/current-state.md` 的样本数、账号数、数据口径和验证结果。
   - 明确 `main`、`all`、源文件目录、正式样本库的区别。

2. **数据质量面板**
   - 展示每个账号的样本数、互动字段覆盖率、播放量缺失率、重复数、低质量样本数。
   - 对样本少于 300 的账号显示低置信度。

3. **历史相似召回**
   - 基于标题、标签、艺人、节目、hook、内容类别、发布时间段构建相似度。
   - 对候选切片返回相似高互动样本、相似低互动样本和置信度。

4. **账号/节目基线 UI**
   - 每个账号展示自己的互动分位线，而不是只看全局 P75。
   - 节目账号、艺人账号、媒体账号分开比较。

5. **原型库口径加固**
   - 原型只读正式去重样本。
   - 原型卡展示表现依据：播放量或互动热度。
   - 原型样本不足时只显示趋势先验。

### P1：把历史能力接入候选排序

1. **候选评分解释增强**
   - 候选卡展示命中的历史原型、相似样本、账号基线位置和低置信原因。

2. **发布时间建议**
   - 先做规则版：账号内星期、小时、内容类别、互动热度分布。
   - 样本不足时不输出确定建议。

3. **轻量回测**
   - 用历史样本模拟排序，验证 Top 20% 是否优于随机样本。
   - 输出按账号、节目、原型的回测报告。

4. **特征标签校准**
   - 建立人工修正入口，沉淀内容类别、hook、切片结构、艺人、歌曲。
   - 后续训练模型只使用版本化后的特征。

### P2：进入 V1 baseline

1. 建立 Logistic / LightGBM baseline，但只在回测证明规则版有效后推进。
2. 接入官方或授权账号指标，进入 `platform_sync_runs -> metric_snapshots -> training_samples -> account_baselines`。
3. 建立增量采集和样本刷新任务，定期更新账号基线、原型库和回测。
4. 引入窗口指标后再做权重校准，不用当前可见数据直接训练生产权重。

## 7. 验收标准

下一轮完成时至少满足：

- 数据质量页能显示 15 个账号的样本规模和字段覆盖率。
- 历史相似召回能返回高互动、低互动样本各至少一组，并显示置信度。
- 原型库不再混用旧 Excel 源文件和正式样本库口径。
- 候选评分解释中能说明账号基线、历史相似和原型命中。
- 回测报告能说明排序策略是否优于随机样本。
- 样本不足或播放量缺失时，界面不输出确定性权重结论。

## 8. 验证命令

全量历史样本汇总：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli historical-summary --account ''
```

数据库去重检查：

```bash
sqlite3 -header -column data/db/dso.sqlite3 "SELECT COUNT(*) AS total_samples FROM historical_capture_samples;"
sqlite3 -header -column data/db/dso.sqlite3 "SELECT COUNT(*) AS duplicate_item_groups FROM (SELECT account_id, platform, platform_item_id, COUNT(*) c FROM historical_capture_samples WHERE COALESCE(platform_item_id,'') != '' GROUP BY account_id, platform, platform_item_id HAVING c > 1);"
```

账号样本分布：

```bash
sqlite3 -header -column data/db/dso.sqlite3 "SELECT account_id, COUNT(*) AS samples FROM historical_capture_samples GROUP BY account_id ORDER BY samples DESC;"
```

服务 API 检查：

```bash
curl "http://127.0.0.1:8000/learning/historical-samples/summary?account_id="
curl "http://127.0.0.1:8000/learning/datasets?account_id="
```

## 9. 视频媒体采集方法固化

在历史样本和可见互动数据之外，本轮补充了抖音切片视频媒体采集测试，用于后续 ASR、OCR、视觉节奏、封面元素和模型训练特征实验。

已固化内容：

- 新增采集器：`src/dso/collectors/douyin_media.py`。
- 新增 CLI：`douyin-media-collect`。
- 新增流程文档：[douyin-media-collection-flow.md](../guides/douyin-media-collection-flow.md)。
- 资产目录固定为 `data/douyin_media_assets/<account_id>/<run_id>/`，按账号和批次隔离保存。
- 报告固定输出 JSON 和 Markdown，记录成功、部分成功、失败、下载、抽帧、音频提取和错误原因。

合规边界：

- 只读取页面 DOM 媒体 URL、封面 URL、页面资源 URL 和页面标题。
- 不读取 cookie、LocalStorage、SessionStorage、Chrome Profile 文件、密码或令牌。
- 不执行点赞、关注、评论、分享、发布等状态改变操作。

已完成 smoke 测试：

| 指标 | 结果 |
| --- | ---: |
| 样本数 | 9 |
| 视频下载成功 | 9 |
| 封面下载成功 | 9 |
| 抽帧成功 | 9 |
| `ffprobe` 验证有效 | 9 |
| 失败 | 0 |

后续扩量入口：

```bash
PYTHONPATH=src python3 -m dso.cli douyin-media-collect \
  outputs/v0.7_media_collection_test/media_collection_test_sample_v1.json \
  --stage smoke_v1 \
  --run-id 20260629_test_v1
```

扩量前应先运行 `--dry-run`，确认样本筛选、账号目录和报告路径，再进行真实下载。

## 10. 参考文档

- [current-state.md](../current-state.md)
- [development-requirements.md](../development-requirements.md)
- [douyin-collection-standard.md](../guides/douyin-collection-standard.md)
- [douyin-media-collection-flow.md](../guides/douyin-media-collection-flow.md)
- [iteration-history-20260628.md](iteration-history-20260628.md)
- [agent-next-iteration-plan.md](agent-next-iteration-plan.md)
