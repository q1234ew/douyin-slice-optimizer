# 当前工程状态

更新时间：2026-07-18

## 1. 产品状态

Douyin Slice Optimizer 是本地优先的音乐综艺短视频切片优化工作台。当前能力覆盖：

- 长视频导入、ASR 转写、候选切片生成、评分和 9:16 导出。
- 可选的授权腾讯系 / YouTube 单视频 URL 获取：经 `videofetch==0.9.1` 的白名单客户端解析/下载，默认落在当前工程的 `data/tmp/video_downloads`，可 dry-run，可选择进入既有节目导入链路；YouTube 固定单视频、最高 720p/H.264 并合并默认 AAC 音轨，不启用 `ytdown`/`downr`；不支持 DRM、Cookie、账号、代理或通用站点解析。
- Qwen3-ASR-1.7B + ForcedAligner-0.6B 已部署为 Shadow ASR 后端；冻结节目评测显示关键字幕锚点优于 Whisper，但 180 秒切块/context 曾出现静默漏口播和热词回显。客户端已改为 60 秒、低能量边界、空 context，并对空结果、异常慢低文本和 context 回显执行受限缩块重试，逐块记录 `ready/recovered/suspect/unresolved`。7506.73 秒完整节目复测已完成：音频块覆盖 100%、RTF `0.050345`、可见字幕锚点 `7/10`、历史遗漏的“蜿蜒的旋律”和投票规则均命中、未解决块和 context 回显均为 0；但锚点较固定 60 秒基线没有提升，2 个自动恢复块中有 1 个只得到单字“啊”的低信息假恢复，多节目逐字稿 Gold 仍缺失，因此保持 Shadow，不替代默认 Whisper。与 Omni 在 16GB GPU 上串行加载。完整结论见 [qwen3-asr-recovery-full-program-retest-20260718.md](./qwen3-asr-recovery-full-program-retest-20260718.md)。
- Web 工作台：节目管理、候选审核、推荐模拟、研究样本与模型学习。
- G1 已切短片批量入口：多文件上传、账号内 SHA-256 去重、每文件一个原片段锁边候选、后台特征提取、统一 scorer/ranker 跨文件排名，并可直接进入既有审核、导出和回流链路。
- Hybrid Slice Pipeline V1：ASR/静音/起音/切镜召回与边界吸附，Top 候选进入 Qwen2.5-Omni hook/middle/payoff 多窗口复排，模型不可用时自动回退规则排序。
- 抖音回流：Mock / 文件导入、平台映射、账号摘要、OAuth 配置检测。
- V1 Beta-C 校准优先排序器：历史采集样本入库、账号质量、工作台语义校准队列、v2 互动热度标签、历史证据排序器 v2.4、Slice Structure Evaluator、发布时间趋势、权重调参和时间切分回测。

当前服务地址：

```bash
http://127.0.0.1:8000/
http://121.199.170.85/  # 阿里云 ECS 测试入口，受 Nginx Basic Auth 保护
```

### 1.1 北极星目标完成度

规范性目标见 [product-goals.md](./product-goals.md)。当前完成度不能与目标状态混写：

| 目标 | 当前判断 | 已有基础 | 主要缺口 |
| --- | --- | --- | --- |
| G1 已切短片筛选排名 | 可运行 MVP | `precut_batch.v1` 已支持批量导入、内容去重、数据库级不可变边界、后台特征提取、共享排序、解释、审核、导出和表现回流 | 仍需冻结跨入口排序 benchmark，并用授权发布指标验证真实 NDCG/Top-K lift；批量吞吐和失败恢复还需真实大批次压测 |
| G2 完整节目智能切片与排名 | 已有可运行基线 | 长视频 ASR、时间轴召回、边界吸附、片段分类、Hybrid Slice、排序和人工审核已贯通 | 召回/分类 Gold 仍需扩充，部分多模态策略仍为 research/shadow，尚不能宣称稳定获得高流量 |
| G3 本地与公网模型协同 | 安全底座可运行，真实厂商 Adapter 未接入 | 本地模型继续优先；`public_model_provider.v1`、默认关闭策略、显式数据许可、请求/批次/日预算、内容缓存、独立审计台账、本地回退、Fake Provider 和 Shadow 评测已贯通 | 尚未接入 Qwen/Kimi 等真实公网 Adapter；启用前仍需用户提供数据许可、密钥、预算，并以厂商官方定价和冻结 benchmark 完成质量/费用/延迟门禁 |
| 新模型与算法主动汇报 | 规则已建立 | 新增 [model-and-algorithm-radar.md](./model-and-algorithm-radar.md) 作为持续登记入口 | 后续每次发现、benchmark 和状态变化都需要持续维护并主动向用户汇报 |

## 2. 技术栈

| 层 | 当前实现 |
| --- | --- |
| 后端 | Python 3.11, FastAPI, SQLite |
| CLI | Typer 优先，缺失 Typer 时有 argparse fallback |
| 前端 | Vue 3, Vite, TypeScript, lucide-vue |
| 静态资源 | `frontend/` 构建到 `src/dso/api/static/dashboard/` |
| ASR | 本地优先，`whisper.cpp` 默认、faster-whisper 兜底；Qwen3-ASR 作为局域网 Shadow/短窗复核后端 |
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
- V1 Beta-D-10B Evidence Resolver Shadow：新增 `material_evidence.py`、`/learning/material-evidence/status`、`/learning/material-evidence/extract` 和 `/learning/material-resolver/shadow`。每条样本真实执行 hook / middle / payoff 三个 8 秒窗口，组合本地 Whisper ASR、macOS Vision 中文 OCR 和 Omni 紧凑证据；只写独立缓存与研究报告，不改 Gold、主语义标签或排序权重。Resolver 并排报告 title-only、Omni-only、ASR/OCR 和 multi-window；Gold 口径固定为“confirmed、material form 已知、按账号与稳定标题去重”，并分别报告入队覆盖、证据覆盖、作答覆盖、端到端准确率、选择性准确率和 `unknown` 弃权率。
- V1 Beta-D-10C 描述特征实验：新增 `material_description_experiment.py` 和 `material-description-experiment` CLI。第二组实验固定 15 秒，比较单 hook 与 hook/middle/payoff 三窗口的描述文本、命名信号和弱标题；描述生成不读取标题，缓存和报告独立于 D10-B，仍为 Shadow。
- V1 Beta-D-11 Visual Window Scout：新增模态感知准入、FFmpeg 全视频低成本候选窗、三帧视觉窗口、Qwen3-VL embedding 原型检索、文本辅助动态融合和窗口级 Gold。视觉路线不要求音轨，舞台、彩排和后台样本不再因 ASR/OCR 低信息被挡掉；Omni 仅接收融合 Top2 manifest。窗口标注独立写入 `material_window_annotations`，不改主语义标签、Material Gold 或生产排序权重。
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
- D10-B 当前仍是低证据覆盖 Shadow：60 条 confirmed Gold 去重后为 59 组，其中 2 组人工标签为 `unknown`，最终可评估 57 组。Gold 优先与动态跨形态准入修正后，100 条 Resolver 队列已覆盖 57/57 可评估 Gold；当前只有 1/57 完成三窗口证据，证据覆盖率 `1.75%`，因此仍为 `research_only`。该 cached Gold 上 multi-window 弃权率为 `100%`、严重错判率为 `0%`；弃权不再被误计为严重错判，但样本量仍不足以证明 Resolver 优于旧 Omni。
- D10-C 首轮仅为 6 条、6 个混淆类型的单窗口 pilot：描述 schema 成功率 `100%`，Omni 实际使用音频 `4/6`。直接分类准确率/覆盖率/严重错误率为 `50% / 100% / 50%`；结构化描述为 `33.33% / 33.33% / 0%`；结构化描述加弱标题为 `50% / 50% / 0%`。六维数组有 3/6 出现“舞台表演被写入幕后信号位”的一致性问题，因此当前描述特征只能作为弃权和风险证据，不能替代多窗口 Resolver。
- D10-C 第二组 15 秒三窗口实验完成 6 条 Gold、18 个窗口：schema `18/18`，有音轨窗口的音频实际使用覆盖 `12/12`，命名信号一致性告警 `2/18`。15 秒单 hook 纯文本与 8 秒纯文本同为 `16.67%` 准确率、`33.33%` 作答覆盖；三窗口结构化描述将覆盖提高到 `50%`，但没有增加正确命中，反而新增两次严重错判，准确率仍为 `16.67%`。结构化描述加弱标题为 `33.33%` 准确率、`66.67%` 覆盖、`50%` 选择性严重错判率，弱于 8 秒首轮。结论是瓶颈主要在盲选窗口和任务信号丢失，不是窗口只有 8 秒。
- D10-D 本地选窗 smoke：对一条 389 秒 `reaction / vocal_teaching` 视频，仅用本机 FFmpeg、Whisper.cpp small、Silero VAD 和 macOS Vision OCR，在 `18.82` 秒内生成 76 个候选窗口；ASR 得到 26 段/828 字，OCR 30 帧中 24 帧非空，临时峰值约 15MB且未持久化媒体。自适应窗口为 `15–30s` 和 `330–345s`，均明显高于固定 middle 的信息分；视觉复核同时发现“reaction” Gold 与“教你们唱歌/发声讲解”强教学证据冲突，因此该样本应进入标签边界复核，不能直接作为分类胜负结论。
- D10-D 冻结盲测未通过 selector gate：清单 `dso-v1-beta-d10d-selector-blind6-20260717-r1` 固定 1 条已调参 pilot 和 5 条盲样本，运行期间未改线索词典。6/6 均完成，合计 `94.90s`，自适应/固定窗口平均信息分为 `0.3390 / 0.2083`，但 5 条盲样本只有 1 条同时满足信息增益与混淆对相关性门槛。根因分层后发现 2 条本地视频无音轨、1 条舞台视频无可用语音、1 条本地素材内容与数据库标题/时长明显错配；因此当前结果只能证明低成本扫描可运行，不能证明词表式选窗具有泛化收益，也不能进入正式 Resolver 或排序权重。
- D10-D 媒体替换 r2 仍未通过：对 60 条 confirmed Gold 执行音轨、实际/历史时长和本地文件准入，只有 26 条合格，33 条缺音轨、2 条时长不一致；合格样本只覆盖 `performance_program_context / reaction_vocal_teaching / behind_the_scenes_performance`。r2 保留 r1，替换 2 条无音轨和 1 条错片资产，5 条盲样本均通过媒体与人工身份核验。盲样本自适应/固定平均信息分为 `0.3569 / 0.2970`，平均样本增益仅 `+0.0302`，相关窗口 `2/5`、通过预注册代理门槛 `1/5`。这证明资产质量不是唯一瓶颈：纯舞台、彩排和节目语境主要依赖视觉场景，词表式 ASR/OCR selector 不能稳定泛化。
- D11 首轮真实准入：60 条 confirmed Gold 中 58 条视频满足视觉路线，27 条有音轨；即视觉可评估覆盖由音频路线的 `45.0%` 提升为 `96.67%`。一条 493.9 秒无音轨视频已完成 3 个 15 秒候选窗和 9 张帧图，新增缓存约 1.11MB。当前 Qwen 服务不可达，因此该轮只验证到帧级候选，embedding 为 0，状态为 `frames_ready_service_unavailable`，继续保持 `research_only`。

## 7. 关键目录和模块

| 路径 | 作用 |
| --- | --- |
| `src/dso/api/main.py` | FastAPI 接口和 Dashboard 静态页面入口 |
| `src/dso/media/video_download.py` | `video_download.v1` 授权腾讯系 / YouTube 单视频 URL 获取、策略门禁、720p 音视频选择、manifest 和既有 ingest 衔接 |
| `src/dso/collectors/douyin_classification.py` | 已发布作品语义字段分类和版本化 |
| `src/dso/learning/historical_samples.py` | 历史采集样本入库、去重、账号质量 |
| `src/dso/learning/memory.py` | 记忆库和历史相似召回 |
| `src/dso/learning/research_ranker.py` | 历史证据排序器 v2.4 |
| `src/dso/learning/slice_structure_evaluator.py` | 切片结构评估器和复核队列 |
| `src/dso/learning/prototypes.py` | 原型发现和原型匹配 |
| `src/dso/learning/interest_clock.py` | 发布时间趋势 |
| `src/dso/learning/backtest.py` | 轻量离线回测 |
| `src/dso/learning/visual_window_scout.py` | D11 视觉候选窗、窗口原型、动态融合和冻结实验 |
| `src/dso/providers/` | G3 厂商无关 Provider 合约、默认关闭策略、预算、缓存、台账、Runner、Fake Provider 和 Shadow 评测 |
| `frontend/src/components/FeedbackView.vue` | 数据反馈与学习面板 |
| `frontend/src/components/InspectorPanel.vue` | 候选详情和历史相似展示 |
| `data/db/dso.sqlite3` | 本地 SQLite 数据库 |
| `data/db/public_model_ledger.sqlite3` | G3 独立调用台账；只记录安全元数据、用量和费用，不存密钥、提示词正文或原始媒体 |

## 8. 常用命令

启动服务：

```bash
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli web --host 127.0.0.1 --port 8000
```

授权腾讯视频或 YouTube 单视频 dry-run：

```bash
python3 -m pip install -e ".[videodl]"
PYTHONPATH=src python3 -m dso.cli download-video "https://v.qq.com/x/cover/.../...html" --dry-run --acknowledge-noncommercial
PYTHONPATH=src python3 -m dso.cli download-video "https://www.youtube.com/watch?v=<video_id>" --dry-run --acknowledge-noncommercial
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
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli material-description-experiment --limit 6 --window-seconds 15 --windows-per-sample 3 --no-direct
PYTHONPATH=src /usr/local/Cellar/python@3.11/3.11.5/bin/python3.11 -m dso.cli backtest --account main --k 30 --strategy research_ranker_v2_6_pool --holdout-policy time
```

G3 安全底座状态和零网络 Smoke：

```bash
dso provider-status
dso provider-smoke --repeat 2 --batch-id local-g3-smoke
```

## 9. 关键 API

| API | 用途 |
| --- | --- |
| `GET /providers/status` | 查询 G3 Provider 注册、默认关闭、网络调用和预算/缓存/台账路径，不返回密钥 |
| `POST /providers/fake-smoke` | 用 Fake Provider 验证策略、缓存、台账和本地回退；不访问公网、不产生 API 费用 |
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
| `GET /learning/visual-window-scout/status` | 查询视觉准入、窗口向量、原型和窗口 Gold 状态 |
| `POST /learning/visual-window-scout/build` | 生成 15 秒视觉候选窗、三帧预览、embedding 和 Omni Top2 manifest |
| `PATCH /learning/material-window-gold/{sample_id}` | 保存独立窗口级视觉形态与选窗质量 Gold |
| `POST /learning/visual-window-scout/experiment` | 对比 fixed/text/visual/fusion 的 Recall@2 与严重漏选率 |
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

- 2026-07-18 完成 G3 真实 Provider 官方资料调研：比较阿里云百炼、Moonshot Kimi、火山方舟和腾讯混元/TokenHub 的 API、模型、价格、地域、限流与数据条款。首选百炼 `qwen3.5-flash-2026-02-23` 文本 + `qwen3-vl-flash-2026-01-22` 代表帧，状态仅提升到 `validate`；Kimi 保留为第二 Provider，方舟需先确认数据授权，腾讯待 TokenHub 迁移稳定。调研未配置密钥、未调用付费 API，完整报告见 [public-model-provider-research-20260718.md](./public-model-provider-research-20260718.md)。
- 2026-07-18 已将当前工作区同步到阿里云 ECS `/srv/dso/app`，保留服务器 `data/`、`.venv/`、密钥和运行缓存；重新安装 editable 包并重启 `dso-web`。公网首页、`GET /providers/status` 和 `POST /providers/fake-smoke` 均返回 HTTP 200，未认证状态请求返回 401，`dso-web` 与 Nginx 均为 `active`。公网 Fake Smoke 两次请求命中一次缓存，网络请求 `0`、费用 `0`，真实公网 Provider 仍为 `0`。
- 2026-07-18 完成 G3 公网模型安全底座：三条并行支线分别实现厂商无关 Provider 合约、权限/预算/缓存/独立 SQLite 台账和 Shadow 质量/成本评测，并由统一 Runner、CLI、API 汇合。本地 Fake Provider 连续两次请求得到一次执行和一次缓存命中，网络请求数 `0`、估算费用 `0`，本地基线始终保留；真实 Qwen/Kimi Adapter 未实现且默认关闭。全量回归 `215 passed, 4 warnings`，compileall 和 `git diff --check` 通过；未改变排序权重、人工 Gold、导出或发布行为。
- 2026-07-18 已实际下载 YouTube 目标 `pkl-Lr6gkCo` 的完整 720p 媒体到工程临时目录 `data/tmp/video_downloads/download_86c2ac7a0ae74609/`，未进入 ingest。下载使用 videodl 的受限直连解析、H.264 itag 136 和 AAC itag 140；由于 GoogleVideo 直连 TLS 超时，传输显式使用本机 `127.0.0.1:7892` 代理，并用独立音频任务与断点续传完成。最终 FFmpeg 无重编码合并文件为 `740,777,070` bytes、时长 `5862.57415s`，包含 1280x720/25fps H.264 与 44.1kHz 双声道 AAC；首/中/尾解码抽检无错误，SHA-256 为 `3f70a04089e41c9d17dae9f326e151f52d6f8ea0e25a7c4e7e51a661b12d91bf`。任务 manifest 已记录代理使用、格式、包计数、验证和未入库状态。
- 2026-07-18 扩展 `video_download.v1` 的 YouTube 单视频支持：只使用 `videodl` 内置直连工具，不调用上游 `ytdown`/`downr`；watch/短链/shorts/live/embed 统一规范化并移除播放列表参数，playlist/channel URL 硬拒绝。目标 `pkl-Lr6gkCo` 的真实 dry-run 解析到完整 `5862.52s` 流，耗时约 `2s`，选中 720p H.264 itag 136（`644,195,519` bytes）和 AAC itag 140（`94,880,508` bytes），两条 CDN URL 的单字节 Range 探针均返回 HTTP 206；本机 FFmpeg 7.1 可用于合并。目标测试 `12 passed`、全量回归 `178 passed, 4 warnings`、CLI help 和 compileall 通过；该次 dry-run 没有写业务数据库，也没有改变排序权重、人工 Gold 或发布行为。
- 2026-07-18 新增 `video_download.v1`：可选 `videofetch==0.9.1` 适配器只加载 `TencentVideoClient`，白名单限定腾讯系 HTTPS 域名，禁用 Cookie/代理/通用解析并在下载前拒绝 DRM；下载后复用既有 `ingest_video`，未新增排序权重、人工 Gold、发布或同步 API。目标 URL `v41023mwbqr` 在隔离 Python 3.11 环境 dry-run 解析耗时 `13.373s`，返回 MP4、`has_drm=false`，文件数和入库数均为 0；目标测试、CLI help 和编译检查通过。
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
- 2026-07-12 Beta-D-10A：完成规范素材形态契约、五类定向错判候选、跨账号平衡抽样、本地媒体门控、API 和工作台筛选视图。后续增加动态跨领域/形态分歧准入，并在包含已审核样本时先保留去重后的可评估 Gold，避免混淆对平衡配额压低 Gold 覆盖。真实运行得到 80 条媒体就绪样本，可直接进入 D10-B 证据抽取。
- 2026-07-13 Beta-D-10B：完成三窗口证据缓存、Whisper 低信息/提示词回声门控、macOS Vision 中文 OCR、Omni 紧凑证据协议、Gold 优先证据队列、cached-eval-only Resolver 报告、API/CLI 和工作台入口。8 秒单窗经 2 fps / 448px / 64 token 优化后由约 149.7 秒降到 31.2 秒；真实三窗口样本约 107–112 秒。
- 2026-07-15 建立 D10-A/B Git 检查点与唯一冻结基准 `dso-v1-beta-d10-ab-20260715-r1`。manifest 固定 10,984 条 `visible_engagement_v2` 样本、60 条 confirmed Gold、623 条 Omni 缓存、2 条 D10-B 证据、关键算法源码指纹、账号内时间切分和 `k=30`；内容 SHA-256 为 `4b1fe0594dd30c4ba2e2b9c027a7d62d467b4201478fad49c8bea1b539170c04`。
- 2026-07-15 D10-C 描述特征 pilot：远端服务增加受限 `material_description_d10c` prompt profile，并验证自定义 prompt 与真音频输入生效。6 条单窗口实验中，结构化描述加弱标题与直接分类同为 3/6 命中，但前者将 3 次直接分类严重错判改为弃权；代价是作答覆盖降到 50%。平均每窗直接分类约 56 秒、描述约 82 秒，暂不适合交互式全量处理。
- 2026-07-15 D10-C 第二组实验：命名信号修复数组错位后，完成 6 条 Gold 的 15 秒三窗口回放。18 窗平均推理 `127.44` 秒、总推理 `2293.95` 秒；相比 8 秒，延长 hook 没有提升，三窗口只增加错误作答。D10-B 的任务型提示词在 reaction 样本上仍能识别编辑语境，而通用事实描述偏向前景舞台画面，因此描述字段暂时只保留为解释证据。
- 2026-07-17 D10-D selector-only 冻结盲测：新增顺序执行、断点复用和无音轨 OCR-only 降级。5 条盲样本仅《年轮》事件样本通过预注册代理门槛；`reaction / vocal_teaching` pilot 不计入盲测胜负。视觉复核确认 `performance / program_context` 本身非互斥，`behind_the_scenes` 依赖视觉场景而非关键词，另有一条跨领域样本为错误/截断媒体。结论为 `selector_gate_not_met`，不扩大到 30 条，不调用远端 Omni。
- 2026-07-17 D10-D 媒体准入与 r2：新增 `material_gold_media_audit.v1`，从 60 条 confirmed Gold 中识别出 26 条 selector-ready。由于原三类混淆没有任何同类音轨完整样本，r2 明确记录覆盖降级，改用声乐教学、后台访谈和舞台直拍替换，不伪装成同类准确率对照。媒体完整后仍只有声乐教学样本通过 selector gate，因此停止扩大词表，后续应验证视觉 embedding/场景变化候选生成。
- 2026-07-17 D11 模型服务恢复：服务器以严格离线模式加载 `Qwen/Qwen3-VL-Embedding-2B`，`transformers` 升级至 `4.57.1`；文本和真实三帧探针均返回 `status=model`、2048 维。首批 5 条样本生成 15 个视觉窗口，15/15 embedding 成功并形成 11 条待审核队列。历史库中 11,296 条 64 维 fallback 已标记失败，不再计入 Qwen3 覆盖率、原型或回测；工作台默认预算收紧为每条 3 个代表窗口。
- 2026-07-17 Omni 运行时修复：远端服务升级为 `dso-multimodal-model-service.v0.2-omni-runtime`，修复 `async /analyze/clip-file` 同步阻塞事件循环、`max_new_tokens` 未映射到 `thinker_max_new_tokens`、客户端超时后孤儿推理继续占用服务、prompt 回显误解析和 `semantic_suggestions` 数据契约不一致。服务现在由 systemd 用户服务托管并启用 linger，单并发推理期间 `/health` 以约 13ms 返回 `busy`，150 秒 watchdog 可触发监督重启；Qwen2.5-Omni GPTQ-Int4 text-only 模型加载约 8–24 秒，6 秒真音视频窗口约 5–8 秒。服务启用 `DSO_MODEL_LOCKED=1`，共享端口上的 Qwen3 Embedding 切模请求返回 HTTP 423，避免双模型残留导致 CUDA OOM。真实候选三窗口复排已返回 `omni_score=61.51`、`confidence=0.90` 并写入混合排序。
- 2026-07-17 Beta-D-11B：完成每批 10 条的跨账号/素材形态平衡抽样、已审核样本排除、未完成批次恢复、不可变 build/manifest、leave-one-sample-out 原型、累计冻结批次去重评测、paired strategy comparison、N/A 缺失策略和 `unknown/uncertain` 弃权口径。promotion gate 固定要求至少 30 条可判定样本、5 个账号、3 种正向视觉形态、Fusion Recall@2 `>=70%`、较 fixed `+10pp`、严重漏选 `<=10%`、决策覆盖 `>=75%`、真实 2048 维 embedding 覆盖 `>=90%` 且零原型泄漏。
- D11B 当前真实批次为 10 条、30 个候选窗、7 个账号和 7 类历史素材形态；局域网单服务与 Omni 任务切换时只有 4/30 个窗口向量可复用。状态被正确降为 `needs_embedding_retry`，26 个缺失窗口不会用 64 维 fallback 或零分代替，也不会提前开放累计评测。
- 冻结前已修复 token set、同分排名和相同发布时间样本的非确定性顺序；`PYTHONHASHSEED=101/202` 两个独立全量进程指标完全一致。冻结参考报告 `bt_3eb5b599720f480e` 的可比口径为：`current_rules=1.4905`、`semantic_baseline_v2=1.4856`、`research_ranker_v2_2=1.5941`、`v2.4=1.4305`、`v2.8/v2.9=1.4356`。7 月 1–2 日的旧 lift 只保留为历史记录，不再与该 manifest 下的结果直接比较。
- 后端全量测试通过：`215 passed, 4 warnings`（2026-07-18，含 G1 批量入口、锁边触发器、跨入口 contract、G3 Provider/治理/评测/Runner 和真实 FastAPI API 回归）。前端类型检查与生产构建通过：`npm run build`；G1 另以 3 秒真实 FFmpeg 视频完成探测、特征降级、共享排序和边界不变量探针。候选审核此前已完成真实 Omni 3 窗复排浏览器检查；本轮浏览器自动验收受当前浏览器的 `127.0.0.1:8000` 安全限制阻止，未绕过该限制。

## 11. 下一步优先级

### 11.1 产品主线

1. 冻结跨入口统一候选 benchmark：G1 标准化候选看 NDCG@K/Top-K lift，G2 同时看 Recall@K 与排序；报告账号/时间切分、严重错判、缺失信号和入口一致性。
2. 用 20-100 条真实已切短片做 G1 批量吞吐、重复文件、无音轨、ASR 失败、重试、审核和导出验收，并记录 P50/P95、失败率和人工复核时间。
3. 优先接入官方或授权账号的曝光、观看、分享和关注窗口指标，让目标从“可见互动代理”逐步升级为真实多目标反馈闭环。
4. G3 首个真实 Provider 已选为阿里云百炼，详见 [2026-07-18 调研](./public-model-provider-research-20260718.md)；下一步先实现不带密钥的 `AliyunBailianProvider` 和 mock contract，再由用户创建独立子业务空间、模型/IP 白名单、限流、API Key 与硬预算，只允许脱敏摘要和代表帧进入冻结 Shadow。

### 11.2 当前视觉研究支线

1. 先让局域网模型服务稳定保持 `Qwen/Qwen3-VL-Embedding-2B`，在没有 Omni 长任务/切模时点击“重试窗口向量”，将当前冻结样本补到至少 27/30 个真实向量。
2. 向量门槛通过后完成当前 20 个候选窗 Gold；优先确认 fixed/visual/fusion 分歧窗口，`unknown/mixed/uncertain` 保留为合法弃权。
3. 每完成一批再扫描下一批 10 条，累计至少 30 条可判定样本；重试产生的重复样本由累计评测自动取最新版本，不重复计数。
4. 运行累计 paired evaluation；达到 D11B promotion gate 后，才把融合 Top2 manifest 送入 Omni Shadow，未过门槛时不写生产排序权重。
5. 将 embedding 与 Omni 拆成独立服务进程或端口，避免共享 `/load` 和 GPU 任务导致批处理中途切模；在拆分前使用串行任务锁。
6. 当前支线结论继续写入冻结 manifest 和模型雷达；未通过 D11B promotion gate 前不挤占 G1/G2 主线，也不写生产排序权重。
