# 当前工程状态

更新时间：2026-07-03

## 1. 产品状态

Douyin Slice Optimizer 是本地优先的音乐综艺短视频切片优化工作台。当前能力覆盖：

- 长视频导入、ASR 转写、候选切片生成、评分和 9:16 导出。
- Web 工作台：节目管理、候选审核、推荐模拟、研究样本与模型学习。
- 抖音回流：Mock / 文件导入、平台映射、账号摘要、OAuth 配置检测。
- V1 Beta-C 校准优先排序器：历史采集样本入库、账号质量、工作台语义校准队列、v2 互动热度标签、历史证据排序器 v2.4、Slice Structure Evaluator、发布时间趋势、权重调参和时间切分回测。

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
| 关注账号库账号数 | 25 |
| 已采集账号数 | 25 |
| 历史样本来源行 | 11,782 |
| API/互动可训练样本 | 10,853 |
| 可见采集卡片 | 536 |
| 新增可见采集样本 | 372 |
| 正式入库样本 | 11,002 |
| 排序器 ready 深度样本 | 10,853 |
| 重复视频组 | 0 |
| 播放量缺失率 | 100% |

互动字段覆盖率：

| 字段 | 覆盖率 |
| --- | ---: |
| 点赞/可见计数 | 98.65% |
| 收藏 | 97.81% |
| 评论 | 94.61% |
| 转发 | 96.16% |

当前播放量暂时缺失，不得用点赞、可见计数或其他互动数冒充播放量。页面和文档应使用“研究样本”“历史先验”“互动热度”“高可见热度”描述当前模型结果。

2026-06-30 已从 `account/抖音账号粉丝数据统计天赐7.xlsx` 导入 10 个高质量天赐 7 矩阵号到本地关注账号库；筛选规则为粉丝数不低于 30 万且存在明确抖音号。未把这些账号标记为已采集，后续需要单独跑账号采集流程。

2026-06-30 关注完成后已通过只读关注列表 API 补齐 25 个账号身份，`profile_url/sec_uid` 覆盖 25/25；随后进入只读可见采集流程，完成 10 个新矩阵号首采和 9 个旧账号补采，共采集 536 条主页可见卡片，新增入库 372 条可见采集样本。可见采集样本不包含 API 级评论、收藏、转发深度指标，作为排序器强证据前需先完成语义回填和后续深度采集。本轮报告见 `outputs/douyin_followed_recollect_20260630/collection_run_summary.md`。

2026-06-30 已将 AppleEvents 页面上下文 post API 采集固化为 `scripts/collect_douyin_post_api.py`，并完成 25 个账号的深度补采。14 个作品量充足账号已达到 500+ 历史样本，11 个作品总数低于 500 的账号已按账号作品上限补齐；全量正式历史样本达到 11,002，互动可训练样本 10,853。采集不读取 Cookie、localStorage、sessionStorage，不执行关注、点赞、评论、发布等状态变更操作；播放量仍保持缺失，不使用互动数替代播放量。

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
- 语义字段回填 v3：已发布 clean JSON 导入和 `/learning/semantic-features/backfill` 会补齐 `content_category`、`hook_type`、`slice_structure`、结构置信度/证据、节目、艺人、歌曲、原声 owner、实体信号、标签和 `semantic_feature_version`；三类核心语义字段已收敛到固定枚举，无法判断时保留 `unknown` 并记录原因。
- 语义特征实验：`/learning/semantic-feature-experiment/run` 可用同一套时间切分验证语义字段遮蔽实验，报告 lift 变化、疑似噪声字段和下一步建议，不写入候选生产分数。
- Slice Structure Evaluator：`/learning/slice-structure/evaluate` 只读评估 `slice_structure` 的可判定率、一致率、冲突率和结构复核队列；不伪造人工标注，不直接改写历史样本。
- 工作台语义校准队列：学习面板已接入 `/learning/semantic-calibration/queue`，可按账号和数据集查看高影响样本、缺失字段、当前标签、label reason、风险/分歧分和推荐校准字段，并快速编辑语义字段。
- 语义校准接口：`/learning/semantic-calibration/queue` 支持 `limit`、`account_id`、`dataset_id`、`min_priority`、`label`、`queue_type`、`strategy`、`min_disagreement`；人工 PATCH 后写入 `change_events` 并且只有用户保存的样本标记 `manual_verified`。
- 互动热度标签 v2：`/learning/research-labels/rebuild` 按账号内相对表现重算 `research_labels.visible_engagement_v2`，加入发布时间年龄桶和时长桶基线降级，不改 `reward_proxy`。
- 历史相似召回：候选切片可优先从 `historical_capture_samples` 匹配相似高互动和低互动作品。
- 历史证据排序器 v2.4：`src/dso/learning/research_ranker.py` 输出高互动相似、低互动风险、账号基线、原型命中、语义可信度、长尾机会、信号可信度门控、`ranker_advice` 和与语义基线差异解释；离线回测增加标题/歌曲近重复多样性约束、诊断样本和下一批校准队列。
- V1 Beta-D-1 小规模多模态验证：新增只读本地素材采集计划、Chrome AppleEvents 媒体资源采集入口和离线多模态验证报告；采集只读取页面媒体资源并下载视频/封面/抽帧/音频，不做关注、点赞、评论或发布。
- 多模态采集保护：`multimodal-collect` 默认 `dry_run`，显式 `--download` 才执行下载；默认 `--max-storage-gb 3`，采集器按 `data/douyin_media_assets` 整体目录计算容量，下载前、下载中和抽帧/音频后都会检查上限。
- V1 Beta-D-2 真实轻量多模态特征实验：`/learning/multimodal-feature-experiment/run` 可从本地视频/音频/封面/抽帧提取短窗音频能量和视觉亮度/对比/饱和/清晰度特征，并与语义基线并排比较；实验只写本地特征缓存，不改候选生产分数。
- 轻量权重调参：`/learning/ranker-tuning/run` 只搜索已有可解释组件权重，不训练 Logistic / LightGBM，不写候选生产分数。
- 原型库口径加固：无 `source_path` 时不再自动混读旧工作簿。
- 发布时间趋势：无训练样本时可用历史样本生成低/中置信趋势。
- 时间切分回测：无 `training_samples` 时按账号内 `published_at` 前 80% / 后 20% 做历史验证；缺时间时 fallback 到 hash holdout。报告并排比较 `current_rules`、`semantic_baseline_v2`、`research_ranker_v2`、`research_ranker_v2_1`、`research_ranker_v2_2`、`research_ranker_v2_3`、`research_ranker_v2_4` 和两个 ablation，并返回诊断样本、多样性摘要、语义差距分析、防泄漏摘要和下一批校准队列。

当前限制：

- 历史样本主要来自可见数据，只适合作研究和趋势先验。
- 播放量缺失，因此当前排序和原型解释基于互动热度代理分。
- 官方或授权账号的 6h / 24h / 72h / 7d / 30d 窗口指标尚未接入。
- 样本少于 300 的账号只能展示低置信趋势，不输出确定性权重。
- `research_ranker_v2_4` 当前已高于 v2.3 和语义基线，并达到高互动命中率和低互动避让率目标；但 `topk_lift_vs_random` 尚未达到 1.85 生产门槛，因此继续标记为 `research_only`。
- Beta-D-1 当前仍为低置信实验：本地多模态 ready 覆盖不足 70%，多模态代理分未证明稳定优于语义基线前，不进入生产排序权重。
- Beta-D-2 已证明“真实轻量音频特征”有研究增益，但样本覆盖仍低：300 样本中真实特征可用 82 条，音频 44 条，视觉 82 条；仅允许进入下一步可解释权重搜索，不直接并入生产排序。

## 7. 关键目录和模块

| 路径 | 作用 |
| --- | --- |
| `src/dso/api/main.py` | FastAPI 接口和 Dashboard 静态页面入口 |
| `src/dso/collectors/douyin_classification.py` | 已发布作品语义字段分类和版本化 |
| `src/dso/learning/historical_samples.py` | 历史采集样本入库、去重、账号质量 |
| `src/dso/learning/memory.py` | 记忆库和历史相似召回 |
| `src/dso/learning/research_ranker.py` | 历史证据排序器 v2.4 |
| `src/dso/learning/slice_structure_evaluator.py` | 切片结构评估器和复核队列 |
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
| `POST /learning/semantic-features/backfill` | 回填 v3 语义结构证据、原声 owner 和实体信号 |
| `POST /learning/semantic-feature-experiment/run` | 只读语义字段遮蔽实验和噪声诊断 |
| `POST /learning/slice-structure/evaluate` | 只读切片结构评估、冲突诊断和复核队列 |
| `POST /learning/multimodal/collection-plan` | 生成 Beta-D-1 多模态素材采集计划 |
| `POST /learning/multimodal/collect` | 执行只读媒体资源采集，默认 dry-run，支持 3GB 存储上限 |
| `POST /learning/multimodal-validation/run` | 只读多模态素材覆盖和代理信号增益验证 |
| `POST /learning/multimodal-feature-experiment/run` | 真实轻量音频/视觉特征提取、策略对比和研究门控 |
| `GET /learning/semantic-calibration/queue?account_id=&dataset_id=&min_priority=&label=&queue_type=&strategy=&min_disagreement=` | 人工语义校准队列 |
| `PATCH /learning/historical-samples/{sample_id}/labels` | 人工修正历史样本语义标签并写 change log |
| `POST /learning/research-labels/rebuild` | 重算 v2 互动热度相对标签 |
| `GET /learning/douyin-history/baselines?account_id=&min_count=1` | 历史样本账号基线和 Top 信号 |
| `GET /segments/{segment_id}/history` | 候选片段历史相似召回 |
| `POST /learning/prototypes/build` | 构建高互动原型库 |
| `GET /accounts/{account_id}/prototypes` | 查询原型库 |
| `GET /accounts/{account_id}/interest-clock` | 发布时间建议 |
| `POST /learning/ranker-tuning/run` | v2.4 可解释权重调参研究报告 |
| `POST /learning/backtest` | v2.4 策略对比、时间切分回测、诊断样本、多样性摘要、语义基线差距和 promotion gate |
| `GET /videos/{video_id}/quality` | 质量哨兵和 ASR 路由建议 |
| `POST /metrics/import` | 授权指标导入和训练样本生成 |

## 10. 最近验证

最近一次目标验证：

- V1 Beta-C-6 已落地：Slice Structure Evaluator、信号可信度门控、v2.4 策略对比、promotion gate、多样性摘要和下一批校准队列。
- 前端学习面板展示校准队列批次摘要、v2.4 策略对比、promotion gate、权重配置、语义基线差距、多样性摘要和诊断样本；候选详情展示排序器原因、组件分、排序器建议和基线差异说明。
- 本地数据库已基于 post API 补采后样本重建 `research_labels.visible_engagement_v2`：10,853 条可训练样本更新，high/mid/low = 2,181 / 6,491 / 2,181。
- 2026-07-01 完成 V1 Beta-C-4 语义可信化后，基于 11,002 样本执行 v3 语义回填：验证样本 2,198，`research_ranker_v2_3 topk_lift_vs_random=1.7947`，高互动命中率 0.90，低互动避让率 1.00；promotion gate 仍为 `research_only`，未达到 1.85 lift 生产门槛。
- 2026-07-02 完成 V1 Beta-C-5 信号可信度门控后，`research_ranker_v2_4 topk_lift_vs_random=1.8184`，高互动命中率 0.90，低互动避让率 1.00，NDCG 0.7778；promotion gate 仍为 `research_only`，距离 1.85 lift 生产门槛差 0.0316。
- 回测性能：11,002 样本口径全量 v2.4 时间切分回测耗时约 17.69 秒，低于 30 秒目标。
- v2.4 策略对比：`current_rules=1.4017`，`semantic_baseline_v2=1.4102`，`research_ranker_v2=1.5680`，`research_ranker_v2_1=1.4102`，`research_ranker_v2_2=1.7684`，`research_ranker_v2_3=1.7947`，`research_ranker_v2_4=1.8184`，`ranker_without_prototypes=1.6283`，`ranker_without_low_risk=1.6157`；v2.4 当前比语义基线高约 +0.4082。
- Slice Structure Evaluator 全量评估 11,002 条：当前结构已知率 34.40%，评估器可判定率 37.59%，一致率 93.79%，可信结构 719 条，suggested_update 263 条，conflict_review 132 条，review queue 默认返回 Top 30；结论仍是结构字段适合作为诊断/校准字段，暂不进入排序强权重。
- V1 Beta-D-1 只读采集已进入第二轮：`beta_d1_music` 8 条采集成功 6 条、partial 2 条；`beta_d1_balanced` 24 条采集成功 23 条、partial 1 条；`beta_d1_round2` 48 条采集成功 40 条、partial 7 条、failed 1 条，视频 40/48、封面 41/48、抽帧 40/48、音频 22/48。`data/douyin_media_assets` 当前约 980MB，低于 3GB 上限，未触发停止。
- Beta-D-1 验证口径已改为“已有素材强制入选 + high/low/mid 平衡补足”：300 样本中多模态 ready 79 条，覆盖 26.33%；视频覆盖 26.33%，视觉覆盖 27.33%，音频覆盖 11.67%；修正代理实验同分排序泄漏后，语义基线 `topk_lift_vs_random=1.0459`，多模态代理分 `0.8909`，promotion gate 仍未通过，主要原因是素材覆盖不足且代理分未证明增益。
- Beta-D-2 真实轻量特征实验已完成第一轮：300 样本中真实特征可用 82 条，覆盖 27.33%；音频 44 条，视觉 82 条；修正同分排序泄漏后，`semantic_baseline=1.1592`，`semantic_plus_audio=1.2863`（+0.1271），`semantic_plus_audio_visual=1.2180`（+0.0588），`visual_only=0.9128`。promotion gate 为 `ready_for_weight_search`，结论是“音频短窗能量可进入可解释权重搜索，视觉封面/首帧特征暂不单独加权”。
- v3 字段遮蔽实验：遮蔽 `artist_names + song_title + original_sound_owner + entity_signal` 后 lift 降至 1.3432，说明实体/歌曲信号仍是强证据；遮蔽全部语义字段降至 1.2816；遮蔽 `content_category` 降至 1.7058。遮蔽 `slice_structure` 反而升至 1.8184，遮蔽 `song_title` 升至 1.8129，说明这两个字段当前应作为待校准/诊断字段，不宜直接强权重。
- 25 个账号已按 v3 语义字段回填；仅用户保存样本会标记 `manual_verified`。
- 正式历史样本 11,002 条，重复视频组 0，播放量正数样本 0。
- 全局研究覆盖率 v3：`content_category` 98.67%，`hook_type` 28.69%，`slice_structure` 34.40%，`artist_names` 92.00%，`song_title` 98.35%，`original_sound_owner` 63.67%，`entity_signal` 99.58%，`tags` 99.21%。
- `research-coverage` 全局状态仍需结合遮蔽实验解释：覆盖率升高不等于可强加权，`slice_structure` 和 `hook_type` 仍需人工校准或更强语义抽取。
- 后端全量测试通过：`Ran 105 tests in 2.683s OK (skipped=12)`。前端生产构建通过：`npm run build`。

## 11. 下一步优先级

1. 批量处理语义校准队列和 Slice Structure Evaluator 的 `suggested_update/conflict_review` 样本，优先校准高互动、高冲突和当前 unknown 但评估器可判定的结构样本。
2. 持续跟踪 v2.4 与 `semantic_baseline_v2` 的差距；若手工校准后 v2.4 稳定达到 1.85 lift，再提升生产权重，否则继续作为研究证据排序。
3. 加强弱分类：针对 Hook 和结构字段建立 gold set、样例和人工校准回放，只有通过遮蔽实验验证后再进入排序强权重。
4. 扩展小样本多模态研究：先验证封面、首帧、音频节奏、ASR/OCR 对 high/mid/low 的解释力。
5. 接入官方或授权账号窗口指标，补齐 6h / 24h / 72h / 7d / 30d；在此之前不把结果表述为播放量或发布效果承诺。
