# 当前工程状态

更新时间：2026-07-05

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
- 工作台语义校准队列：学习面板已接入 `/learning/semantic-calibration/queue`，可按账号和数据集查看高影响样本、缺失字段、当前标签、label reason、风险/分歧分和推荐校准字段，并快速编辑语义字段。保存人工标签后样本会退出待校准队列，并进入“最近已保存”。
- 语义校准接口：`/learning/semantic-calibration/queue` 支持 `limit`、`account_id`、`dataset_id`、`min_priority`、`label`、`queue_type`、`strategy`、`min_disagreement`；人工 PATCH 后写入 `change_events` 并且只有用户保存的样本标记 `manual_verified`。如需二次校准，可用 `/learning/historical-samples/{sample_id}/calibration/reopen` 或工作台“重新打开校准”按钮把样本放回队列。
- 互动热度标签 v2：`/learning/research-labels/rebuild` 按账号内相对表现重算 `research_labels.visible_engagement_v2`，加入发布时间年龄桶和时长桶基线降级，不改 `reward_proxy`。
- 历史相似召回：候选切片可优先从 `historical_capture_samples` 匹配相似高互动和低互动作品。
- 历史证据排序器 v2.4：`src/dso/learning/research_ranker.py` 输出高互动相似、低互动风险、账号基线、原型命中、语义可信度、长尾机会、信号可信度门控、`ranker_advice` 和与语义基线差异解释；离线回测增加标题/歌曲近重复多样性约束、诊断样本和下一批校准队列。
- V1 Beta-D-1 小规模多模态验证：新增只读本地素材采集计划、Chrome AppleEvents 媒体资源采集入口和离线多模态验证报告；采集只读取页面媒体资源并下载视频/封面/抽帧/音频，不做关注、点赞、评论或发布。
- 多模态采集保护：`multimodal-collect` 默认 `dry_run`，显式 `--download` 才执行下载；默认采集目标为 300 条，默认 `--max-storage-gb 5`，可用 `DSO_MULTIMODAL_COLLECTION_MAX_STORAGE_GB` 或 CLI/API 参数覆盖；采集器按 `data/douyin_media_assets` 整体目录计算容量，下载前、下载中和抽帧/音频后都会检查上限。
- V1 Beta-D-2 真实轻量多模态特征实验：`/learning/multimodal-feature-experiment/run` 可从本地视频/音频/封面/抽帧提取短窗音频能量和视觉亮度/对比/饱和/清晰度特征，并与语义基线并排比较；实验只写本地特征缓存，不改候选生产分数。
- Qwen2.5-Omni 低显存 Shadow Mode：`src/dso/learning/qwen_omni.py` 接入外部多模态模型服务，默认模型 `Qwen/Qwen2.5-Omni-7B-GPTQ-Int4`，支持 `/analyze/clip` 文本 shadow 和 `/analyze/clip-file` 真媒体 shadow；历史样本可先切 8-15 秒低码率窗口再上传，媒体 payload 会记录音频来源、当前 hook 窗口和 middle/payoff 多窗口计划，输出只作为校准建议，不写 `manual_verified`。
- V1 Beta-D-6 Omni Evidence Router：`research_ranker_v2_6_pool` 基于 v2.4、v2.5 shadow 证据和账号 trust profile 做 Top30 扩池研究门控，报告 `omni_pool_report`、`omni_pool_gate`、`omni_trust_profiles`、`omni_account_pool_gates` 和 `omni_account_pool_summary`；该策略只用于 cached eval only / pool research，不替代 v2.4 Top10 生产权重。
- V1 Beta-D-8 Material Gold Set：工作台“校准与回测”已提供素材形态人工审核区，支持确认 `domain_category / material_type / program_context / presentation_style`、查看中文标注说明、审核进度、最近确认和重新审核；人工结果独立写入 `material_gold_annotations`，不改互动数、`reward_proxy`、主语义标签或 `manual_verified`。
- 校准页交互已收敛为“素材审核 / 语义校准 / 算法回测”三个模式，默认进入素材审核；Gold Set 使用单样本聚焦编辑、短队列导航、三步状态条和“保存并进入下一条”，算法低频操作折叠到高级工具，避免多个长队列和实验按钮同时出现。
- Material Router v2.8：`research_ranker_v2_8_material_calibrated` 将已确认 Gold Set 按账号和稳定标题确定性拆为校准 70% / 独立审计 30%；校准样本从性能验证行排除，审计样本只用于质量门槛。无 Gold 支持时严格回退 v2.4。
- V1 Beta-D-9 Material Taxonomy Shadow：`research_ranker_v2_9_material_taxonomy` 保留人工原始 `material_type`，另派生 canonical 素材形态用于路由。当前仅将 `performance_highlight -> performance_clip`、`judge_comment -> commentary` 视为明确父子关系；报告同时返回严格准确率、canonical 准确率、部分得分、严重错判率和 v2.9 对 v2.8/v2.4 的 Top20/30/50 差值，不改写人工标注。
- V1 Beta-D-10A Confusion Queue：新增 `material_taxonomy.py` 和 `/learning/material-confusions/queue`。规范素材形态不再包含 `performance_highlight / judge_comment / program_context`；三者分别派生为 `performance_clip + highlight_signal`、`commentary + judge detail` 和独立 `program_context`，源标签保持不变。定向队列覆盖五类主要混淆，按账号与稳定标题去重并平衡抽样，只返回有 Omni 结果且默认有本地视频的样本。
- V1 Beta-D-10B Evidence Resolver Shadow：新增 `material_evidence.py`、`/learning/material-evidence/status`、`/learning/material-evidence/extract` 和 `/learning/material-resolver/shadow`。每条样本真实执行 hook / middle / payoff 三个 8 秒窗口，组合本地 Whisper ASR、macOS Vision 中文 OCR 和 Omni 紧凑证据；只写独立缓存与研究报告，不改 Gold、主语义标签或排序权重。Resolver 并排报告 title-only、Omni-only、ASR/OCR 和 multi-window，并以“已有完整证据的 confirmed Gold”作为 promotion 指标口径。
- 轻量权重调参：`/learning/ranker-tuning/run` 只搜索已有可解释组件权重，不训练 Logistic / LightGBM，不写候选生产分数。
- 原型库口径加固：无 `source_path` 时不再自动混读旧工作簿。
- 发布时间趋势：无训练样本时可用历史样本生成低/中置信趋势。
- 时间切分回测：无 `training_samples` 时按账号内 `published_at` 前 80% / 后 20% 做历史验证；缺时间时 fallback 到 hash holdout。报告并排比较 `current_rules`、`semantic_baseline_v2`、v2-v2.9 历史证据策略、Qwen embedding 策略和 ablation，并返回诊断样本、多样性摘要、语义差距分析、防泄漏摘要和下一批校准队列。

当前限制：

- 历史样本主要来自可见数据，只适合作研究和趋势先验。
- 播放量缺失，因此当前排序和原型解释基于互动热度代理分。
- 官方或授权账号的 6h / 24h / 72h / 7d / 30d 窗口指标尚未接入。
- 样本少于 300 的账号只能展示低置信趋势，不输出确定性权重。
- `research_ranker_v2_4` 当前已高于 v2.3 和语义基线，并达到高互动命中率和低互动避让率目标；但 `topk_lift_vs_random` 尚未达到 1.85 生产门槛，因此继续标记为 `research_only`。
- Beta-D-1 当前仍为低置信实验：本地多模态 ready 覆盖不足 70%，多模态代理分未证明稳定优于语义基线前，不进入生产排序权重。
- Beta-D-2 已证明“真实轻量音频特征”有研究增益，但样本覆盖仍低：300 样本中真实特征可用 82 条，音频 44 条，视觉 82 条；仅允许进入下一步可解释权重搜索，不直接并入生产排序。
- Qwen2.5-Omni-7B BF16 不适合当前 15.47GB 显存目标机；低显存 GPTQ-Int4 仅按 15 秒以内、batch=1、离线 shadow-run 使用。30 秒及以上视频仍建议 48GB 级显存环境。Omni v2.6 只允许进入 Top30 扩池研究和校准队列诊断；未通过 pool gate 前，不进入自动导出、不改人工标签、不替代 v2.4 排序。
- 第一批 Material Gold Set 已完成 60 条人工确认，按同账号稳定标题去重后有效 59 条，拆为 41 条校准和 18 条独立审计。v2.8 在 119 条 Omni cached eval 的 Top30 上相对 v2.4 lift `+0.0442`、高互动命中 `+0.0333`、低互动避让 `+0.0333`；严格素材形态审计准确率 `72.22%`，因此仍为 `material_calibration_research_only`。
- v2.9 继续保持 shadow/research 口径：canonical 质量通过只表示素材大类可用于研究路由，不代表高光等细粒度标签已识别，也不等于通过最终 promotion gate。
- v2.9 首轮真实回放：独立审计严格准确率 `72.22%`、canonical 准确率 `77.78%`、部分得分 `76.39%`、严重错判率 `22.22%`；canonical 质量门槛通过。119 条 cached eval 上 v2.9 相对 v2.4 的 Top30 lift/high/low 为 `+0.0442 / +0.0333 / +0.0333`，但相对 v2.8 三项均为 `0`，说明层级修正改善了测量口径，没有新增排序收益。
- D10-A 真实队列：默认 80 条，候选池 491 条，五类混淆分别 15–17 条，覆盖 18 个账号，80/80 本地媒体就绪；已确认的首批 60 条 Gold 自动排除。该队列是下一步 ASR/OCR 与多窗口证据实验的定向输入，不自动写 Gold 或排序权重。
- D10-B 当前仍是低覆盖 Shadow：包含已审核样本时，100 条证据队列可优先纳入 51 条 confirmed Gold；当前只有 1 条 Gold 完成三窗口证据，因此 gate 使用 `cached_gold_evaluable_count=1` 并保持 `research_only`。首条真实 Gold 上 title-only / Omni-only 与人工 `reaction` 一致，ASR/OCR 偏向 `vocal_teaching`，multi-window 因两侧证据接近而弃权为 `unknown`，尚不能证明 Resolver 优于旧 Omni。

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

Qwen2.5-Omni 低显存 shadow 检查：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli qwen-omni-status
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli qwen-omni-analyze <segment_id> --max-clip-seconds 15 --load-model
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli qwen-omni-shadow-run --account main --dataset all --limit 20 --max-clip-seconds 15 --load-model
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli qwen-omni-media-batch --limit 20 --max-clip-seconds 8 --load-model
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli backtest --account main --k 30 --strategy research_ranker_v2_6_pool --holdout-policy time
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
| `POST /learning/multimodal/collect` | 执行只读媒体资源采集，默认 dry-run，默认支持 5GB 存储上限 |
| `POST /learning/multimodal-validation/run` | 只读多模态素材覆盖和代理信号增益验证 |
| `POST /learning/multimodal-feature-experiment/run` | 真实轻量音频/视觉特征提取、策略对比和研究门控 |
| `GET /learning/qwen-omni/status` | 检查 Qwen2.5-Omni 低显存服务、显存门控和当前加载模型 |
| `POST /segments/{segment_id}/qwen-omni/analyze` | 对 15 秒以内候选片段执行 Omni shadow 分析，只返回建议 |
| `POST /learning/qwen-omni/shadow-run` | 对历史样本批量执行低显存 Omni shadow-run，不写生产标签 |
| `POST /learning/qwen-omni/media-batch` | 对本地真视频样本做切窗、上传、逐条缓存和可恢复报告 |
| `GET /learning/semantic-calibration/queue?account_id=&dataset_id=&min_priority=&label=&queue_type=&strategy=&min_disagreement=` | 人工语义校准队列 |
| `PATCH /learning/historical-samples/{sample_id}/labels` | 人工修正历史样本语义标签并写 change log |
| `POST /learning/historical-samples/{sample_id}/calibration/reopen` | 将已保存人工标签的样本重新打开，回到语义校准队列 |
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
- 2026-07-05 校准队列修复验证：`PATCH /learning/historical-samples/{sample_id}/labels` 保存后样本进入 `recently_saved_samples`，不再出现在待校准 `samples`；`POST /learning/historical-samples/{sample_id}/calibration/reopen` 可把 `classification_confidence` 改回 `low/medium/high` 并重新进入队列，前端已提供“重新打开校准”按钮。
- 2026-07-05 目标模型服务检查：`qwen-omni-status` 可连接 `http://192.168.31.143:8001`，服务主机为 `aidev-OMEN-MAX-Gaming-Laptop-16-ah0xxx`，GPU 为 `NVIDIA GeForce RTX 5080 Laptop GPU`，显存 15.47GB，CUDA 可用。资源门控结论：支持 GPTQ-Int4 15 秒短片段，无法稳定支持 GPTQ-Int4 30 秒或 BF16 15 秒。
- 2026-07-05 服务端 Omni 准备：服务以 `aidev` 用户运行 `/home/aidev/dso_multimodal_model_service/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8001`。远端 `app.py` 已改为 `/load` 可读取 `model_id/backend` 并识别 `qwen_omni`，`/health` 会报告 `qwen_omni_utils/gptqmodel/decord/soundfile/librosa` 状态，加载失败会回滚到当前 Qwen3 embedding 配置，避免误跑 heuristic。当前已安装 `qwen-omni-utils/decord/soundfile/librosa/ninja`，仍缺系统 `nvcc` 与 `gptqmodel`。仓库新增 `scripts/open_server_proxy_tunnel.sh` 和 `scripts/server_prepare_qwen_omni.sh`，用于 Mac 端反向代理、服务器下载模型、写 Omni 环境和后续本机 sudo 安装 CUDA Toolkit / 构建 GPTQ。
- 2026-07-05 Omni 模型下载：已通过 Mac 端 `127.0.0.1:7892` 代理建立 SSH reverse tunnel，服务器使用 `http://127.0.0.1:17892` 完成 `Qwen/Qwen2.5-Omni-7B-GPTQ-Int4` 下载，落盘路径为 `/home/aidev/models/Qwen2.5-Omni-7B-GPTQ-Int4`；模型目录约 12GB，包含 21 个文件和 4 个 safetensors 权重分片。下载时需设置 `HF_HUB_DISABLE_XET=1`，否则容易卡在 Xet/LFS 链路。
- 2026-07-05 100 条 media 运行：`multimodal-feature-experiment --limit 100` 成功，100 条中真实特征 ready 82 条，音频 44 条、视觉 82 条；`semantic_baseline=1.1592`，`semantic_plus_audio=1.2863`，`semantic_plus_audio_visual=1.2180`，`visual_only=0.9128`，promotion gate 为 `ready_for_weight_search`。
- 2026-07-05 Qwen embedding 运行：`qwen-embeddings-build --limit 100 --modality text` 成功创建 92 条、复用 8 条、失败 0；`qwen-embedding-evidence --limit 100 --modality text` 返回 `status=ready`。
- 2026-07-07 Omni 真媒体探针：目标服务 `http://192.168.31.143:8001` 已暴露 `/analyze/clip-file`，客户端支持 `qwen-omni-shadow-run --use-media --allow-windowed-clips --visual-ready-only` 和可恢复的 `qwen-omni-media-batch`。本地可用真视频多模态样本 600 条，其中 571 条超过 15 秒需要切窗；8 秒窗口 smoke 1/1 成功、warm batch 3/3 成功，15 秒窗口 smoke 1/1 成功。远端服务 PATH 已加入官方 ffmpeg `8.1.2`，音频输入 smoke 返回 `use_audio_in_video=true`。输出路径示例：`outputs/qwen_omni_shadow/media_payload_warm3_20260707_005707.json`、`outputs/qwen_omni_shadow/media_payload_15s_smoke_20260707_010209.json` 和 `outputs/qwen_omni_shadow/media_audio_path_smoke_20260707_011308.json`。当前吞吐为几十秒到一分钟级/条，不建议直接交互式跑 600 全量，应通过断点续跑报告分批执行。
- 2026-07-10 Beta-D-8 初始回放：Gold Set 候选 60 条，其中素材冲突复核 6 条；人工确认 0 条时 v2.8 与 v2.4 的 Top20/30 换位数均为 0，lift/high/low 差值均为 0，符合“无校准证据不调分”。
- 2026-07-12 Beta-D-8 完整回放：60 条人工确认去重后有效 59 条，41 条校准、18 条独立审计、分组重叠 0；v2.8 已不再退化为 v2.4，但严格素材形态准确率和高互动增益仍未达到最终门槛。
- 2026-07-12 Beta-D-9：新增不改写 Gold 的素材形态 canonical taxonomy、严格/规范双口径质量审计、严重错判率和 `research_ranker_v2_9_material_taxonomy` shadow 对照。
- 2026-07-12 Beta-D-9 首轮回放结论：canonical 质量通过，但 Top20/30/50 相对 v2.8 均无新增 lift/high/low；继续 `material_taxonomy_research_only`，下一步转向 4 条独立审计严重错判，而不是继续放大 taxonomy 路由权重。
- 2026-07-12 Beta-D-10A：完成规范素材形态契约、五类定向错判候选、跨账号平衡抽样、本地媒体门控、API 和工作台筛选视图。真实运行得到 80 条媒体就绪样本，可直接进入 D10-B 证据抽取。
- 2026-07-13 Beta-D-10B：完成三窗口证据缓存、Whisper 低信息/提示词回声门控、macOS Vision 中文 OCR、Omni 紧凑证据协议、Gold 优先证据队列、cached-eval-only Resolver 报告、API/CLI 和工作台入口。8 秒单窗经 2 fps / 448px / 64 token 优化后由约 149.7 秒降到 31.2 秒；真实三窗口样本约 107–112 秒。
- 2026-07-15 建立 D10-A/B Git 检查点与唯一冻结基准 `dso-v1-beta-d10-ab-20260715-r1`。manifest 固定 10,984 条 `visible_engagement_v2` 样本、60 条 confirmed Gold、623 条 Omni 缓存、2 条 D10-B 证据、关键算法源码指纹、账号内时间切分和 `k=30`；内容 SHA-256 为 `4b1fe0594dd30c4ba2e2b9c027a7d62d467b4201478fad49c8bea1b539170c04`。
- 冻结前已修复 token set、同分排名和相同发布时间样本的非确定性顺序；`PYTHONHASHSEED=101/202` 两个独立全量进程指标完全一致。冻结参考报告 `bt_3eb5b599720f480e` 的可比口径为：`current_rules=1.4905`、`semantic_baseline_v2=1.4856`、`research_ranker_v2_2=1.5941`、`v2.4=1.4305`、`v2.8/v2.9=1.4356`。7 月 1–2 日的旧 lift 只保留为历史记录，不再与该 manifest 下的结果直接比较。
- 后端全量测试通过：`129 tests OK (15 skipped)`。前端生产构建通过：`npm run build`。

## 11. 下一步优先级

1. 按混淆对分层补齐至少 30 条 confirmed Gold 的 D10-B 三窗口证据；使用断点缓存、batch=1，不在工作台同步启动大批次。
2. 在 cached-eval-only 子集复核 Resolver 分歧，优先处理 `reaction / vocal_teaching` 和 `performance / program_context`；对 `unknown` 弃权与严重错判分开计量。
3. 只有当证据覆盖至少 85%、cached Gold 至少 30、canonical accuracy 至少 85%、严重错判不高于 10%，且相对 Omni 至少提升 3%，才进入排序器 ablation；仍不直接修改生产权重。
4. 持续跟踪 v2.4 与 `semantic_baseline_v2` 的差距；未稳定达到生产门槛前，v2.8-v2.10 都只作为研究证据排序。
5. 接入官方或授权账号窗口指标，补齐 6h / 24h / 72h / 7d / 30d；在此之前不把结果表述为播放量或发布效果承诺。
