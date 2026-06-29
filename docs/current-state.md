# 当前工程状态

更新时间：2026-06-29

## 1. 产品状态

Douyin Slice Optimizer 是本地优先的音乐综艺短视频切片优化工作台。当前能力覆盖：

- 长视频导入、ASR 转写、候选切片生成、评分和 9:16 导出。
- Web 工作台：节目管理、候选审核、推荐模拟、研究样本与模型学习。
- 抖音回流：Mock / 文件导入、平台映射、账号摘要、OAuth 配置检测。
- V1 Beta-C 校准优先排序器：历史采集样本入库、账号质量、工作台语义校准队列、v2 互动热度标签、历史证据排序器 v2.1、发布时间趋势、权重调参和时间切分回测。

当前服务地址：

```bash
http://127.0.0.1:8000/
```

## 2. 技术栈

| 层 | 当前实现 |
| --- | --- |
| 后端 | Python 3.11, FastAPI, SQLite |
| CLI | Typer 优先，缺失 Typer 时有 argparse fallback |
| 前端 | Vue 3, Vite, TypeScript, lucide-vue |
| 静态资源 | `frontend/` 构建到 `src/dso/api/static/dashboard/` |
| ASR | 本地优先，`whisper.cpp` 优先，faster-whisper 兜底 |
| 数据库 | `data/db/dso.sqlite3` |

## 3. 当前数据口径

`/learning/datasets` 是数据目录和血缘视图；`historical_capture_samples` 是正式入库后的研究样本表。短期不依赖新切片发布回流，模型、原型库和学习面板优先使用已发布视频的可见/授权数据。

| 指标 | 当前数量 |
| --- | ---: |
| 关注账号数 | 15 |
| 原始获取作品数 | 5,709 |
| clean 后作品数 | 5,554 |
| 正式入库样本 | 5,554 |
| 可训练历史样本 | 5,554 |
| 重复视频组 | 0 |
| 播放量缺失率 | 100% |

互动字段覆盖率：

| 字段 | 覆盖率 |
| --- | ---: |
| 点赞 | 100.00% |
| 收藏 | 99.39% |
| 评论 | 97.34% |
| 转发 | 98.33% |

当前播放量暂时缺失，不得用点赞、可见计数或其他互动数冒充播放量。页面和文档应使用“研究样本”“历史先验”“互动热度”“高可见热度”描述当前模型结果。

## 4. 去重和聚合规则

- 有 `platform_item_id`：按 `account_id + platform + platform_item_id` 去重。
- 无 `platform_item_id`：按标题稳定 key 去重。
- `all` 只作为聚合视图，不写入物理 `dataset_id=all`。
- `main` 不等于全量聚合；CLI 查询全量历史样本使用 `--account ''`。
- `source=visible_capture` 的原型库优先读取正式历史样本；只有显式传入 `source_path` 时才读取旧 XLSX/CSV/JSON 文件。

## 5. 当前账号样本分布

| 账号 | 样本数 |
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

## 6. V1 Beta-C 学习状态

已具备：

- 账号级数据质量：样本数、互动字段覆盖率、播放量缺失率、重复组数、置信等级。
- 语义字段回填：已发布 clean JSON 导入时会统一补齐 `content_category`、`hook_type`、`slice_structure`、节目、艺人、歌曲、标签和 `semantic_feature_version`；三类核心语义字段已收敛到固定枚举，无法判断时保留 `unknown` 并记录 `semantic_unknown_reason`。
- 工作台语义校准队列：学习面板已接入 `/learning/semantic-calibration/queue`，可按账号和数据集查看高影响样本、缺失字段、当前标签、label reason、风险/分歧分和推荐校准字段，并快速编辑语义字段。
- 语义校准接口：`/learning/semantic-calibration/queue` 支持 `limit`、`account_id`、`dataset_id`、`min_priority`、`label`、`queue_type`、`strategy`、`min_disagreement`；人工 PATCH 后写入 `change_events` 并且只有用户保存的样本标记 `manual_verified`。
- 互动热度标签 v2：`/learning/research-labels/rebuild` 按账号内相对表现重算 `research_labels.visible_engagement_v2`，加入发布时间年龄桶和时长桶基线降级，不改 `reward_proxy`。
- 历史相似召回：候选切片可优先从 `historical_capture_samples` 匹配相似高互动和低互动作品。
- 历史证据排序器 v2.2：`src/dso/learning/research_ranker.py` 输出高互动相似、低互动风险、账号基线、原型命中、语义可信度、长尾机会、`ranker_advice` 和与语义基线差异解释；候选评分返回 `component_scores`、`evidence_quality`、`ranker_reason`、`ranker_advice`。
- 轻量权重调参：`/learning/ranker-tuning/run` 只搜索已有可解释组件权重，不训练 Logistic / LightGBM，不写候选生产分数。
- 原型库口径加固：无 `source_path` 时不再自动混读旧工作簿。
- 发布时间趋势：无训练样本时可用历史样本生成低/中置信趋势。
- 时间切分回测：无 `training_samples` 时按账号内 `published_at` 前 80% / 后 20% 做历史验证；缺时间时 fallback 到 hash holdout。报告并排比较 `current_rules`、`semantic_baseline_v2`、`research_ranker_v2`、`research_ranker_v2_1`、`research_ranker_v2_2` 和两个 ablation，并返回诊断样本、语义差距分析、防泄漏摘要和下一批校准队列。

当前限制：

- 历史样本主要来自可见数据，只适合作研究和趋势先验。
- 播放量缺失，因此当前排序和原型解释基于互动热度代理分。
- 官方或授权账号的 6h / 24h / 72h / 7d / 30d 窗口指标尚未接入。
- 样本少于 300 的账号只能展示低置信趋势，不输出确定性权重。
- `research_ranker_v2_2` 当前已略高于语义基线，但尚未达到 +0.03 lift 差距和 1.85 lift 的生产门槛，因此继续标记为 `research_only`。

## 7. 关键目录和模块

| 路径 | 作用 |
| --- | --- |
| `src/dso/api/main.py` | FastAPI 接口和 Dashboard 静态页面入口 |
| `src/dso/collectors/douyin_classification.py` | 已发布作品语义字段分类和版本化 |
| `src/dso/learning/historical_samples.py` | 历史采集样本入库、去重、账号质量 |
| `src/dso/learning/memory.py` | 记忆库和历史相似召回 |
| `src/dso/learning/research_ranker.py` | 历史证据排序器 v2.2 |
| `src/dso/learning/prototypes.py` | 原型发现和原型匹配 |
| `src/dso/learning/interest_clock.py` | 发布时间趋势 |
| `src/dso/learning/backtest.py` | 轻量离线回测 |
| `frontend/src/components/FeedbackView.vue` | 数据反馈与学习面板 |
| `frontend/src/components/InspectorPanel.vue` | 候选详情和历史相似展示 |
| `data/db/dso.sqlite3` | 本地 SQLite 数据库 |

## 8. 常用命令

启动服务：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli web --host 127.0.0.1 --port 8000
```

全量历史样本汇总：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli historical-summary --account ''
```

去重检查：

```bash
sqlite3 -header -column data/db/dso.sqlite3 "SELECT COUNT(*) AS total_samples FROM historical_capture_samples;"
sqlite3 -header -column data/db/dso.sqlite3 "SELECT COUNT(*) AS duplicate_item_groups FROM (SELECT account_id, platform, platform_item_id, COUNT(*) c FROM historical_capture_samples WHERE COALESCE(platform_item_id,'') != '' GROUP BY account_id, platform, platform_item_id HAVING c > 1);"
```

前端构建：

```bash
cd frontend
npm run build
```

测试：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m unittest discover -s tests
```

## 9. 关键 API

| API | 用途 |
| --- | --- |
| `GET /learning/datasets?account_id=` | 数据目录、血缘和正式样本口径 |
| `GET /learning/historical-samples/summary?account_id=` | 历史样本汇总和账号质量 |
| `GET /learning/research/coverage?account_id=&dataset_id=` | 研究语义字段覆盖率和标签口径 |
| `GET /learning/semantic-calibration/queue?account_id=&dataset_id=&min_priority=&label=&queue_type=&strategy=&min_disagreement=` | 人工语义校准队列 |
| `PATCH /learning/historical-samples/{sample_id}/labels` | 人工修正历史样本语义标签并写 change log |
| `POST /learning/research-labels/rebuild` | 重算 v2 互动热度相对标签 |
| `GET /learning/douyin-history/baselines?account_id=&min_count=1` | 历史样本账号基线和 Top 信号 |
| `GET /segments/{segment_id}/history` | 候选片段历史相似召回 |
| `POST /learning/prototypes/build` | 构建高互动原型库 |
| `GET /accounts/{account_id}/prototypes` | 查询原型库 |
| `GET /accounts/{account_id}/interest-clock` | 发布时间建议 |
| `POST /learning/ranker-tuning/run` | v2.2 可解释权重调参研究报告 |
| `POST /learning/backtest` | v2.2 策略对比、时间切分回测、诊断样本、语义基线差距和 promotion gate |
| `GET /videos/{video_id}/quality` | 质量哨兵和 ASR 路由建议 |
| `POST /metrics/import` | 授权指标导入和训练样本生成 |

## 10. 最近验证

最近一次目标验证：

- V1 Beta-C-2 已落地：固定语义枚举、`semantic_unknown_reason`、校准队列风险/分歧优先级、历史证据排序器 v2.2、近重复防泄漏、回测诊断样本和下一批校准队列。
- 前端学习面板展示校准队列批次摘要、v2.2 策略对比、promotion gate、权重配置、语义基线差距和诊断样本；候选详情展示排序器原因、组件分、排序器建议和基线差异说明。
- 本地数据库已重建 `research_labels.visible_engagement_v2`：5,554 条样本更新，high/mid/low = 1,116 / 3,322 / 1,116。
- 全量时间切分回测已完成：验证样本 1,107，`research_ranker_v2_2 topk_lift_vs_random=1.8357`，高互动命中率 0.90，低互动避让率 1.00；promotion gate 仍为 `research_only`，12 个 ready 账号优于 `current_rules`。
- 回测性能已优化：历史召回索引加入语义字段倒排索引，全量 v2.2 时间切分回测耗时约 18.42 秒，满足 30 秒目标。
- 策略对比：`current_rules=0.4975`，`research_ranker_v2=1.6176`，`semantic_baseline_v2=1.8335`，`research_ranker_v2_1=1.8335`，`research_ranker_v2_2=1.8357`；v2.2 当前只比语义基线高 +0.0022，未达到 +0.03 的生产提升门槛。
- 15 个账号已按保守弱分类规则重跑语义字段回填。
- 正式历史样本 5,554 条，重复视频组 0，播放量正数样本 0。
- 全局研究覆盖率：`content_category` 89.07%，`hook_type` 35.83%，`slice_structure` 17.92%，`artist_names` 89.59%，`tags` 99.55%。
- `research-coverage` 全局状态为 `needs_semantic_backfill`；这是当前保守弱分类策略的预期结果，hook 和结构字段需要人工校准或更强语义抽取。
- 后端全量测试通过：`Ran 96 tests in 3.401s OK (skipped=12)`。前端生产构建通过：`npm run build`。

## 11. 下一步优先级

1. 批量处理语义校准队列中高影响样本，优先校准 `hook_type`、`slice_structure`、`artist_names`，每批保存后使用“重建标签与回测”验证变化。
2. 持续跟踪 v2.2 与 `semantic_baseline_v2` 的差距；若手工校准后 v2.2 稳定达到 +0.03 lift，再提升生产权重，否则继续作为研究证据排序。
3. 加强弱分类：针对 Hook 和结构字段引入更明确的枚举、样例和人工校准回放，避免为追覆盖率牺牲可信度。
4. 扩展小样本多模态研究：先验证封面、首帧、音频节奏、ASR/OCR 对 high/mid/low 的解释力。
5. 接入官方或授权账号窗口指标，补齐 6h / 24h / 72h / 7d / 30d；在此之前不把结果表述为播放量或发布效果承诺。
