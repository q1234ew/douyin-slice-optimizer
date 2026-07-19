# 当前工程状态

更新时间：2026-07-20

## 1. 产品状态

Douyin Slice Optimizer 是本地优先的音乐综艺短视频切片优化工作台。当前能力覆盖：

- 长视频导入、ASR 转写、候选切片生成、评分和 9:16 导出。
- 可选的授权腾讯系 / YouTube 单视频 URL 获取：经 `videofetch==0.9.1` 的白名单客户端解析/下载，默认落在当前工程的 `data/tmp/video_downloads`，可 dry-run，可选择进入既有节目导入链路；YouTube 固定单视频、最高 720p/H.264 并合并默认 AAC 音轨，不启用 `ytdown`/`downr`；不支持 DRM、Cookie、账号、代理或通用站点解析。
- Qwen3-ASR-1.7B + ForcedAligner-0.6B 已按用户决策切为音乐综艺生产主 ASR；`input_mode=program/precut` 的 auto 路由优先写 Qwen 主 `transcript.json`，模型未加载、服务不可用或失败时自动回退 Whisper.cpp/faster-whisper，并记录实际后端，避免 placeholder 覆盖。独立 `qwen3_asr_shadow.v1` CLI/API 仍保留用于双模型对照，不修改主转写。客户端采用 60 秒低能量切界、空 context 和异常块 30 秒受限恢复，逐块记录 `ready/recovered/suspect/unresolved`。两个独立完整节目冻结锚点合计 Qwen `15/22`、Whisper small `7/22`、base `4/22`；该证据仍是局部字幕而非完整逐字稿，因此采用带 Whisper 回退和可回滚策略，不宣称通用 CER/WER 已达标。Qwen 与 Omni 在 16GB GPU 上仍需串行加载；局域网 Resource Agent 已部署并完成 Omni 后 ASR 自动回切，最终 ASR 保持 ready。报告见 [qwen3-asr-recovery-full-program-retest-20260718.md](research/evaluations/qwen3-asr-recovery-full-program-retest-20260718.md) 和 [qwen3-asr-subtitled-program-evaluation-20260718.md](research/evaluations/qwen3-asr-subtitled-program-evaluation-20260718.md)。
- Web 工作台：节目管理、候选审核、推荐模拟、研究样本与模型学习；研究中心“模型与环境”新增百炼连接配置面板，可填写北京业务空间兼容地址、固定模型、API Key 和三层预算。密钥只写 ECS 的 `0600` EnvironmentFile，公网 HTTP 禁止提交，保存后公网调用仍保持关闭。
- 完整节目“智能切片”在界面内显示四阶段真实进度（转写分析、候选生成、评分排序、Omni 复排）、已耗时和基于节目时长的预计剩余时间；主流程按接口完成推进，Omni 入队后展示真实队列计数并明确剩余时间由 GPU 调度决定，完成或失败状态会保留到下一次运行。
- G1 已切短片批量入口：多文件上传、账号内 SHA-256 去重、每文件一个原片段锁边候选、后台特征提取、统一 scorer/ranker 跨文件排名，并可直接进入既有审核、导出和回流链路。
- Hybrid Slice Pipeline V1：ASR/静音/起音/切镜召回与边界吸附，Top 候选进入 Qwen2.5-Omni hook/middle/payoff 多窗口复排，模型不可用时自动回退规则排序。
- 本地模型调度已实现 `model_scheduler.v1` Phase 0–3 batch-1，并在局域网 canary 显式启用：`192.168.31.143:8010` Agent 与本机 launchd Worker/8127 Web 常驻，token 使用 0600 env + macOS Keychain；逐块 ASR、逐窗口 Omni、Text/Visual Embedding、内容寻址准备、fencing 和恢复已贯通。19.1 秒 canary 的 5 个 Scheduler attempt 全部一次成功，三种 Profile 均完成真实推理/向量验证。状态仍为 `validate`：其他环境默认关闭，长节目/批量冻结混合 workload、GPU 空闲间隙、OOM、输出等价和 batch 2/4 尚未验收。
- 抖音回流：Mock / 文件导入、平台映射、账号摘要、OAuth 配置检测。
- 账号证据隔离：`platform_accounts.account_role` 明确区分 `publishing_target / research_source / unassigned`，平台作品映射使用 `target_outcome / research_proxy / unclassified` 证据用途；研究中心的账号筛选不再隐式切换发布账号。
- V1 Beta-C 校准优先排序器：历史采集样本入库、账号质量、工作台语义校准队列、v2 互动热度标签、历史证据排序器 v2.4、Slice Structure Evaluator、发布时间趋势、权重调参和时间切分回测。

当前服务地址：

```bash
http://127.0.0.1:8000/
http://127.0.0.1:8127/  # 局域网 Scheduler canary，launchd 常驻
http://121.199.170.85/  # 阿里云 ECS 测试入口，受 Nginx Basic Auth 保护
```

### 1.1 北极星目标完成度

规范性目标见 [product-goals.md](product-goals.md)。当前完成度不能与目标状态混写：

| 目标 | 当前判断 | 已有基础 | 主要缺口 |
| --- | --- | --- | --- |
| G1 已切短片筛选排名 | 可运行 MVP | `precut_batch.v1` 已支持批量导入、内容去重、数据库级不可变边界、后台特征提取、共享排序、解释、审核、导出和表现回流；跨入口基线 `dso-v1-cross-entry-20260718-r2` 已冻结 | 正式库仍缺真实 G1 批次及其授权发布指标，不能据历史代理分宣称真实 NDCG/Top-K lift；批量吞吐和失败恢复还需真实大批次压测 |
| G2 完整节目智能切片与排名 | 已有可运行基线 | 长视频 ASR、时间轴召回、边界吸附、片段分类、Hybrid Slice、排序和人工审核已贯通 | 召回/分类 Gold 仍需扩充，部分多模态策略仍为 research/shadow，尚不能宣称稳定获得高流量 |
| G3 本地与公网模型协同 | 安全底座与百炼有界 Shadow 可运行，局域网调度 canary 已部署 | 本地模型继续优先；`public_model_provider/runner/ledger.v2` 已实现精确许可、缓存前置、预算预留/结算、逐尝试台账和本地回退；冻结真实研究样本已跑通 `qwen3-vl-embedding`、`qwen3-vl-rerank`、`qwen3.7-plus`/`qwen3.6-flash` 与 Qwen3.5-Omni 完整音视频；传播事实特征完成 30 对/60 条、8 账号的账号隔离对照 | 状态仍为 `research_only`；Omni 固定 15% 融合成对命中较账号隔离 v2.4 增加 `6.67pp`，但提升只来自 1 个样本充足账号，Top15 命中不变且统计区间含 0，因此保持 v2.4；不扩 Judge，Kimi Adapter 未实现 |
| 新模型与算法主动汇报 | 规则已建立 | 新增 [model-and-algorithm-radar.md](model-and-algorithm-radar.md) 作为持续登记入口 | 后续每次发现、benchmark 和状态变化都需要持续维护并主动向用户汇报 |

## 2. 技术栈

| 层 | 当前实现 |
| --- | --- |
| 后端 | Python 3.11, FastAPI, SQLite |
| CLI | Typer 优先，缺失 Typer 时有 argparse fallback |
| 前端 | Vue 3, Vite, TypeScript, lucide-vue |
| 静态资源 | `frontend/` 构建到 `src/dso/api/static/dashboard/` |
| ASR | 音乐综艺默认 Qwen3-ASR 主后端；Whisper.cpp / faster-whisper 自动回退；独立 Shadow 路径用于双模型对照 |
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

25 个采集账号（包括 `tianci`）均是跨账号研究来源，不代表目标发布账号。当前 `main` 仅是本地发布账号槽位，`account_role=unassigned`；目标发布账号身份和结果数据暂不可用，因此账号个性化保持 `cold_start`。存量平台回流中的 57 条映射保持 `unclassified`；129 条旧指标迁移后均为审计数据，其中 111 条可关联到这些映射，18 条没有当前平台映射。它们不计入目标账号 readiness，也不改变生产排序。

平台指标导入已停止把 `可见计数 / 计数数值 / visible_count_number / best_visible_count_number` 映射为 `views`。只有明确的 `views / play_count / view_count` 等平台结果字段才标记为 `explicit_platform_outcome`；模糊可见计数保留 `ambiguous_visible_count` 告警且播放量写 0。

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

## 6.  学习状态

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
- 多模态向量价值评测 V1：冻结 benchmark `dso-multimodal-vector-value-20260719-r1`，以 60 条 confirmed Material Gold 各匹配一个不重复本地对照，形成 60 组 / 120 条评测样本；另冻结完全不重叠、账号平衡的 60 条高互动与 60 条低互动参考池。历史可见互动代理是客观结果主门禁，人工 A/B 仅作为编辑偏好和严重错判的次级诊断；两者均不回写 Material Gold、主语义标签或生产权重。
- 百炼向量研究链路 V1：`bailian_multimodal_vector_chain.v1` 已把同一冻结 manifest 接到 `qwen3-vl-embedding` 2560 维 Text/Fusion、cosine 初召回、`qwen3-vl-rerank` Top-N、客观互动代理门禁、v2.4 真实选择分歧队列以及盲裁版 `qwen3.7-plus`/`qwen3.6-flash` 双 Judge。ECS 当前 150/240 条具备 Text/Fusion 云向量；参考池强制高/低各 10 条。D12-B 独立留出 20 对上固定 85/15 融合与 v2.4 的平衡命中同为 `76.26%`，独立增量为 0；状态保持 `research_only`，Judge 不扩量。
- V1 Beta-D-12A 缓存信号归因：`bailian_cached_signal_ablation.v1` 已接入 CLI、API 和研究中心，固定比较 Text/Fusion cosine、平衡参考池、每侧 Top-3/5/10、缓存 Rerank、云内融合与 v2.4 低权重融合。ECS 对现有 110/240 Text/Fusion 缓存完成零网络运行：同一 40 对上，最佳纯云组合为 `text cosine + 25% cached rerank`，平衡命中 `75.64%`，较该子集 v2.4 `69.80%` 高 `5.84pp`；最佳观察融合为 `85% v2.4 + 15% cloud`，平衡命中 `81.34%`、原始命中 `77.5%`，分别较同子集提高 `11.54pp/7.5pp`。但 bootstrap 95% 区间仍宽（平衡增量 `0–23.08pp`），且只有 2 个样本量充足账号改善，未达到 3 个账号门槛；扩量 Gate 为 `keep_v2_4`。40 条参考池配置因当前只有 20 条高低平衡缓存而明确跳过。报告保持 `research_only`，未构造公网 Runtime、网络请求与费用均为 0，也未改变生产权重。
- V1 Beta-D-12B 独立留出复验：`bailian_independent_holdout_validation.v1` 已接入 CLI、API 和研究中心三步界面。`pair-041..060` 与 D12-A 40 对样本重叠为 0；固定配置、D12-A 归一化尺度和 20 对 v2.4 预测先写配置 SHA，40 条样本的 Text/Fusion 与 Rerank 全部完成后再写盲预测 SHA，最后才解锁互动代理。120 次真实请求 usage 估算 `0.2243942 CNY`，低于 10 元硬上限。独立 20 对固定融合/v2.4 平衡命中均为 `76.26%`、原始命中均为 `75%`，最终选择变化 0；纯 Text/Rerank/Cloud 平衡命中仅 `60.61%/66.16%/60.61%`。结论为 `inconclusive_keep_v2_4`；60 对合并 `+6.82pp` 仅是含校准集的次要指标，不是独立泛化证据。
- V1 Beta-D-12C1 证据质量重构：`bailian_evidence_quality_reconstruction.v1` 已接入 CLI、API 和研究中心。D12-B 40 条留出样本已生成 40/40 个 15 秒 hook/middle/payoff 证据包和 120 张真实时间帧，三时点覆盖 100%，派生缓存约 8.5 MB，网络请求/费用为 0。ECS 当前只有 30 条缓存参考（high 20 / low 10），同账号 high/low 双侧覆盖 20%，账号/节目/素材双侧语境覆盖 70%；当前冻结 manifest 全量同账号双侧覆盖上限仅 65%，4 个账号缺少低互动侧。缓存对照中全局 Text 平衡命中 `57.07%`，双侧分层 Text 为 `51.52%`，下降 `5.55pp`，因此状态保持 `research_only`，暂不构建新 Fusion 或修改 v2.4。报告见 [D12-C1](research/evaluations/bailian-evidence-quality-reconstruction-20260719.md)。
- Qwen3.5-Omni 传播事实特征账号隔离验证：冻结 30 对/60 条、8 个账号，60/60 schema、时间线、音频和视觉证据覆盖 100%。主批 60 次请求加 1 次受限截断恢复，usage 估算 `6.654878 CNY`。账号隔离 v2.4、纯 Omni、固定 85/15 融合成对命中分别为 `66.67% / 63.33% / 73.33%`；融合只纠正 2 对且都属于 `duanduanzhengzheng`，4 个 ready 账号只有 1 个改善，Top15 高互动命中不变，符号检验 `p=0.5`、账号簇 bootstrap 区间包含 0。Gate 为 `keep_v2_4`，报告见 [账号隔离验证](research/evaluations/qwen35-omni-propagation-account-holdout-20260720.md)。
- 轻量权重调参：`/learning/ranker-tuning/run` 只搜索已有可解释组件权重，不训练 Logistic / LightGBM，不写候选生产分数。
- 原型库口径加固：无 `source_path` 时不再自动混读旧工作簿。
- 发布时间趋势：无训练样本时可用历史样本生成低/中置信趋势。
- 时间切分回测：无 `training_samples` 时按账号内 `published_at` 前 80% / 后 20% 做历史验证；缺时间时 fallback 到 hash holdout。报告并排比较 `current_rules`、`semantic_baseline_v2`、v2-v2.9 历史证据策略、Qwen embedding 策略和 ablation，并返回诊断样本、多样性摘要、语义差距分析、防泄漏摘要和下一批校准队列。

当前限制：

- 历史样本主要来自可见数据，只适合作研究和趋势先验。
- 播放量缺失，因此当前排序和原型解释基于互动热度代理分。
- 官方或授权账号的 6h / 24h / 72h / 7d / 30d 窗口指标尚未接入。
- 目标发布账号尚未明确登记，且没有可验证的目标账号平台结果；离线研究门控与目标账号生产 readiness 必须分开显示。
- 样本少于 300 的账号只能展示低置信趋势，不输出确定性权重。
- `research_ranker_v2_4` 在冻结跨入口参考回测中为 lift `1.4305`、高互动命中 `0.5667`、低互动避让 `0.9000`；lift 低于 `current_rules=1.4905`，低互动避让也低于语义基线 `0.9333`。绝对门槛和强基线保护均未通过，因此继续 `research_only`；默认生产排序固定为 `current_rules/final_score`。
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
| `src/dso/learning/multimodal_vector_value.py` | 60 组短片盲审、定向向量覆盖、独立参考池和五策略成对对照 |
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
| `GET /providers/config` | 查询 Web 连接配置、预算和治理门禁；只返回密钥是否已配置，响应禁止缓存 |
| `POST /providers/config` | 经 HTTPS 或 SSH 本地端口转发安全保存连接配置；公网 HTTP 返回 403，保存强制保持 API 关闭 |
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
| `GET /learning/multimodal-vector-experiment/status` | 查询冻结向量实验、双模态覆盖、盲审进度和下一条隐藏身份任务 |
| `POST /learning/multimodal-vector-experiment/embeddings` | 经持久 Scheduler 只为冻结样本与参考池补 Text/Visual embedding |
| `POST /learning/multimodal-vector-experiment/compare` | 生成 current/v2.4/text/visual/fusion 成对对照，不改变生产策略 |
| `POST /learning/multimodal-vector-experiment/reviews/{task_id}` | 保存独立 A/B/相当/弃权盲审，不写 Material Gold |
| `GET /learning/multimodal-vector-experiment/cloud/status` | 查询百炼四模型门禁、Text/Fusion 覆盖、报告和全量请求计划；只读且不计费 |
| `POST /learning/multimodal-vector-experiment/cloud/run` | 显式运行最多 10 条 Smoke 或 40 条 Embedding/Rerank/Judge 有界批次；始终 `research_only` |
| `POST /learning/multimodal-vector-experiment/cloud/ablation` | D12-A 只读缓存消融，零网络、零费用 |
| `POST /learning/multimodal-vector-experiment/cloud/holdout/{action}` | D12-B 冻结配置、生成盲预测或解锁评估；单实验硬上限 10 元 |
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
| `GET /model-scheduler/status` | 调度开关、队列、活动 lease、runtime 与最近一小时指标 |
| `GET /model-jobs/{job_id}` | `model_job.v1` 状态、进度、结果摘要与安全错误 |
| `POST /model-jobs/{job_id}/cancel` | 幂等取消持久模型任务 |
| `GET /videos/{video_id}/quality` | 质量哨兵和 ASR 路由建议 |
| `GET /videos/{video_id}/asr/shadow` | Qwen3-ASR Shadow 策略、服务、缓存和最近运行状态 |
| `POST /videos/{video_id}/asr/shadow` | 显式执行完整节目 Qwen3-ASR Shadow，不覆盖主转写 |
| `POST /metrics/import` | 授权指标导入和训练样本生成 |

## 10. 最近验证

最近一次目标验证：

- 2026-07-18 冻结跨入口 benchmark `dso-v1-cross-entry-20260718-r2`，内容 SHA-256 为 `a4df0cbac32689a475a6abbacdd5eff81a86163900184b1d5a8ce642cbf66386`，即时复验无漂移。快照包含 10,984 条历史互动代理样本、25 个账号、60 条 Material Gold、623 条 Omni cache、1 个 G2 节目及 30 条 `standard_candidate.v1` 候选；生产 contract 固定 `current_rules/final_score`，研究策略不自动晋级。当前状态为 `baseline_frozen_with_known_gaps`：正式库尚无真实 G1 precut 批次，G2 人工召回 Gold 也未建立。r1 因并行 ASR 任务修改无关全局版本常量而严格报源码漂移，r2 将指纹收窄到实际排序行为模块。参考回测与操作见 [cross-entry-benchmark-20260718.md](research/evaluations/cross-entry-benchmark-20260718.md)。
- 2026-07-18 完成 [本地模型资源调度 Phase 0–3 batch-1 基线](architecture/model-scheduling-architecture.md)：在 Phase 0/1 的持久队列、单 GPU lease/fencing、恢复和 Omni 异步路径上，新增逐 item 生命周期、resident-profile 亲和与 parent 公平轮转、CPU/IO Preparation Pool、统一内容寻址媒体清单、逐窗口 Omni、逐块 Qwen3-ASR、Text/Visual Embedding Adapter，以及带 Bearer token、白名单 Profile 和 fencing 的 GPU Resource Agent/安装脚本。冻结合成 benchmark `dso-model-scheduler-mixed-20260718-r1` 共 6 个 job/44 个 item，入队 P95 `2.094ms`、调度开销 `0.411%`、模拟切模 `36 -> 3`；隔离数据根的 20 秒真实 ASR smoke 返回 3 段，warm-hit 推理 `1394ms`、commit `9ms`。全量 `231` 项测试、前端生产构建、脚本语法、CLI benchmark 和候选审核浏览器回归通过。该记录对应部署前基线；未改变模型、提示词、排序权重、人工 Gold、候选边界、正式业务数据库、导出或发布。
- 2026-07-18 完成局域网生产 canary 部署：GPU 主机 `192.168.31.143` 安装并启用 `gpu-resource-agent.v1`，未授权请求返回 401、同 token 不同 attempt 返回 409，三种白名单 Profile 均完成真实验证；Omni 加载 `44.389s`、text-only 推理 `5.609s`，Embedding 加载 `25.773s`、文本向量 `2048` 维，ASR 最终回切 ready。正式 `model_scheduler.sqlite3` 记录 3 个 succeeded job/5 个一次成功 attempt：19.1 秒视频 ASR 产生 6 段、Omni hook/middle/payoff 三窗全部成功、ASR Shadow 回切保持主 transcript。Agent/lease fencing 最终一致为 `5`。本机 token 写入 macOS Keychain，launchd `com.dso.lan-model-worker` 与 `com.dso.lan-web` 常驻，8127 Scheduler API ready，生产候选审核浏览器回归无 console warning/error；日志检查发现并修复候选历史读取绕过队列加载 Embedding 的 500，现返回 HTTP 200 与 `deferred_scheduler` 弃权。全量 `235` 项测试、前端生产构建、脚本语法、密钥扫描和 `git diff --check` 通过；原 8000 服务和阿里云 ECS 未修改。部署备份位于 GPU 主机 `/home/aidev/backups/dso-gpu-agent-20260718T220633` 与本机 `data/backups/model_scheduler-before-lan-deploy-20260718T2215.sqlite3`。状态仍为 `validate`，未修改生产排序权重、人工 Gold、候选边界、正式业务数据库、导出或发布。
- 2026-07-18 完成 G3 真实 Provider 官方资料调研：比较阿里云百炼、Moonshot Kimi、火山方舟和腾讯混元/TokenHub 的 API、模型、价格、地域、限流与数据条款。首选百炼 `qwen3.5-flash-2026-02-23` 文本 + `qwen3-vl-flash-2026-01-22` 代表帧，状态仅提升到 `validate`；Kimi 保留为第二 Provider，方舟需先确认数据授权，腾讯待 TokenHub 迁移稳定。调研未配置密钥、未调用付费 API，完整报告见 [public-model-provider-research-20260718.md](research/providers/public-model-provider-research-20260718.md)。
- 2026-07-19 完成 `AliyunBailianProvider` 与 G3 治理 V2：实现华北 2 业务空间 Host/fixed snapshot allowlist、非流式/非思考 JSON Mode、本地冻结 schema、脱敏摘要/三 JPEG 边界、MockTransport、错误与一次重试；Runner 改为缓存前置，预算支持 reserve/settle/release/unknown，Ledger 记录 preflight、usage、逐网络尝试、实际 Token/响应字节、Provider request ID、缓存 Token、价表与保留政策引用。Provider 目标测试 `43 passed`、全量回归 `259 passed, 4 warnings`、compileall 和隔离 CLI status/Fake Smoke 通过；默认仍关闭，未配置密钥、未调用 API、费用为 0。公开保留期仍未知，因此真实业务数据继续 fail closed；详见 [专项调研](research/providers/aliyun-bailian-provider-research-20260718.md)。
- 2026-07-19 已将百炼 Provider/G3 治理 V2 的运行文件定向同步到阿里云 ECS `/srv/dso/app`，同步前备份为 `/srv/dso/backups/provider-v2-before-20260718T175348Z.tar.gz`；服务器虚拟环境补装 `httpx 0.28.1` 并刷新 editable 包，`dso-web` 与 Nginx 重启后均为 `active`。服务器内部状态返回 `public_model_runtime/provider/runner/ledger.v2`、`disabled`、`network_calls_allowed=false`、`secret_configured=false`；迁移后的 4 条历史台账合计网络尝试 0、费用 0。公网 80 端口首页和 Provider 状态均返回预期的 HTTP 401 登录挑战，原认证未移除。该部署未配置真实密钥、未调用百炼、未上传业务数据，也未改变生产排序、人工 Gold 或发布流程。
- 2026-07-19 已部署 `provider_admin_config.v1` 与 Web 百炼连接面板到 ECS。部署前备份为 `/srv/dso/backups/provider-admin-before-20260719T032733.tar.gz`；systemd drop-in 注入 `/etc/dso/bailian.env` 并只对白名单目录 `/etc/dso` 增加写权限，目录/文件权限实测为 `700/600`。Nginx 原配置已覆盖 `Host/X-Real-IP/X-Forwarded-For/X-Forwarded-Proto`，无需放宽安全组或认证。部署后 `dso-web`/Nginx 均为 `active`，新 bundle 为 `index-Bh9gKodf.js`；SSH loopback GET 返回 `secure_submission_allowed=true`，模拟公网 HTTP GET 返回 false，POST 返回 403 且 env 文件仍为 0 bytes，公网未认证仍返回 401。Provider 保持 `disabled`、网络调用 false、密钥未配置、台账仍为 4 条；没有真实调用、费用、业务数据外发、生产排序、Gold 或发布变化。
- 2026-07-19 经用户授权将本机百炼 CSV 上传到 ECS 的 root-only 临时目录，解析后用 `provider_admin_config.v1` 写入 `/etc/dso/bailian.env`；权限实测保持 `0600`，临时 CSV 随即删除。安全状态 API 仅确认 `api_key_configured=true`，固定模型、北京工作空间、密钥与预算门禁均为 true；单请求/批次/日预算为 `0.05/0.20/1.00 CNY`。数据许可和保留政策仍为 false，总开关与网络调用均为 false，台账仍为 4 条，因此没有 API 请求、费用或业务数据外发，也未改变生产排序、人工 Gold、导出或发布。
- 2026-07-19 经用户明确批准在 ECS 执行一次百炼合成文本连通性 Smoke：只发送无业务/个人数据的合成摘要，进程内临时开放数据许可与公网开关，`max_retries=0`，共发生 1 次网络请求。Runner 返回 `fallback_local/provider_failed`，逐尝试台账为 `network_error`、HTTP 状态 0、延迟 `891ms`、响应 0 bytes、无 Provider request ID；调用 ID 为 `5181e18d99b74a70895188e8f4c902f3`，批次为 `bailian-synthetic-smoke-20260718T193550Z`。账单状态为 `unknown`，治理底座按最坏预留保守占用 `0.0002582 CNY`，这不是已确认的厂商扣费。随后只读探针确认 DNS/TLS 正常、鉴权 `GET /models` 返回 HTTP 200、固定模型 `qwen3.5-flash-2026-02-23` 在 229 个可见模型中；当时尚未定位本地响应重建缺陷，根因与修复见下一条。该阶段持久状态为 `public_api_enabled=false`、`network_calls_allowed=false`、台账 5 条，`dso-web=active`，EnvironmentFile 为 `0600 root:root`。没有继续重试，没有业务数据外发，也未改变生产排序、人工 Gold、导出或发布。
- 2026-07-19 已解决上述 `network_error`：百炼返回合法 gzip 内容，Adapter 的有界响应路径却在 `iter_bytes()` 解压后保留 `Content-Encoding: gzip`，导致新建响应二次解压并抛出 `httpx.DecodingError`。修复后会移除失效的编码/长度/传输头，补充安全传输错误分类、gzip 回归测试，去掉非必要 `n=1`，生产客户端不继承系统代理。ECS 备份为 `/srv/dso/backups/bailian-network-fix-before-20260719T040441.tar.gz` 与 `/srv/dso/backups/bailian-gzip-fix-before-20260719T040755.tar.gz`。最终正式 Runner 合成探针批次 `bailian-gzip-fix-smoke-20260718T200818Z` 首尝试 HTTP 200，延迟 `1003ms`、响应 `668` bytes、输入/输出 `179/101` Token、Provider request ID 存在，五字段本地 schema 通过；调用 `10975faff59240f8ae5a11123177cf52`，usage 估算 `0.0002378 CNY`。服务保持 active、持久总开关为 0、网络调用 false、EnvironmentFile 为 `0600 root:root`、台账 7 条；只发送合成文本，没有业务数据、排序/Gold/导出/发布变化。
- 2026-07-18 完成 `KimiK3Provider` 专项核对：K3 提供 1M 上下文、视觉、自动缓存和 strict JSON Schema，但始终使用 `reasoning_effort=max`，缓存未命中输入/输出为 20/100 元每百万 Token，公开列表无 dated snapshot；现行服务协议还包含将输入、输出和反馈用于模型服务优化的授权，且固定数据保留期未知。状态保持 `watch`，只规划为 10 条高分歧疑难样本 Challenger；企业条款未明确前仅允许合成 Smoke，禁止真实节目媒体、未授权逐字稿和人工 Gold。未实现 Adapter、未配置密钥、未调用 API；详见 [K3 专项调研](research/providers/kimi-k3-provider-research-20260718.md)。
- 2026-07-18 已将当前工作区同步到阿里云 ECS `/srv/dso/app`，保留服务器 `data/`、`.venv/`、密钥和运行缓存；重新安装 editable 包并重启 `dso-web`。公网首页、`GET /providers/status` 和 `POST /providers/fake-smoke` 均返回 HTTP 200，未认证状态请求返回 401，`dso-web` 与 Nginx 均为 `active`。公网 Fake Smoke 两次请求命中一次缓存，网络请求 `0`、费用 `0`，真实公网 Provider 仍为 `0`。
- 2026-07-18 完成 G3 公网模型安全底座初版：三条并行支线实现厂商无关 Provider 合约、权限/预算/缓存/独立 SQLite 台账和 Shadow 质量/成本评测，并由统一 Runner、CLI、API 汇合；该记录保留为 V1 历史，后续已由 `public_model_provider/runner/ledger.v2` 和百炼 Adapter 扩展。
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
- 2026-07-19 向量价值实验 T0：冻结 manifest SHA-256 为 `f32a6699ffc2cc7554ea6e2fa9a0550afc7e92a91cd2c46585beb130544a1510`，60 组、120 评测样本、120 独立参考样本通过结构校验。入队前评测集文本覆盖 `12/120`、视觉 `0/120`，参考池文本 `9/120`、视觉 `0/120`；历史互动代理成对准确率为 `current_rules 61.67%`、`v2.4 71.67%`、低覆盖文本策略 `73.33%`、视觉/融合 `71.67%`。该 `+1.66pp` 处于极低覆盖且没有人工盲审的 T0，不作为有效增益。持久调度任务 `model_job_d6404fe1f6834d6e` 曾接收 459 个缺失 Text/Visual item；局域网模型地址不可达并连续三次超时后，任务已取消，完成数为 0，21 个已有缓存继续保留。用户已明确允许云端采用更新且更适合的模型，因此 21 个本地向量只作为旧基线；云端模型必须使用独立 model/version，并在同一 manifest 上重算全部 480 个 Text/Visual 向量，不重新抽样或扫描全库。
- 2026-07-19 完成百炼线上模型工程接入：`AliyunBailianProvider` 新增 Multimodal Embedding、Multimodal Rerank 与 Pairwise Judge 独立 endpoint/allowlist/schema，`build_aliyun_bailian_runtime` 支持按研究模型构造共享预算/缓存/台账 runtime；新增 `bailian_multimodal_vector_chain.v1`、Web/CLI 状态、有界执行与零网络 `preflight` 入口。ECS 纯合成文本能力 Smoke 跑通 `qwen3-vl-embedding` 2560 维、`qwen3-vl-rerank` 结果映射和两种 Judge 冻结 schema；首次 Rerank 因预留低于实际 usage 被正确回退，修正协议开销下界后复测成功。该批共 5 次网络尝试，usage 估算 `0.0035096 CNY`。持久公网开关仍关闭，没有业务样本外发，也没有修改生产排序、人工 Gold、导出或发布。
- 2026-07-19 已将冻结 manifest 引用的 478 张代表帧定向上传 ECS，逐文件 SHA-256 通过，原始总量 `21,501,712` bytes，不含完整视频。新增上传前 FFmpeg 派生帧归一化，源素材不改；ECS 全量 `preflight` 对 240 条样本验证 `480/480` 个 Text/Fusion 请求、475 张实际入选帧、零缺失、零失败、零网络、零费用，序列化请求总量约 `24.89MB`。含一次重试的公开价最坏预留为 `7.7589056 CNY`；用户已说明当前有免费 Token，费用暂不作为实验决策门槛，但本地预算护栏尚不了解免费额度，真实调用前仍需显式调整或接入额度台账。真实研究帧公网推理本轮未执行，云覆盖仍为 `0/240`。
- 2026-07-19 经用户明确授权，在 ECS 对冻结真实研究摘要和代表帧完成有界公网 Smoke。2 条探针产生 4 个 2560 维向量；10 条评测加 20 条参考产生 60 个 Text/Fusion 向量、10/10 Rerank 和双 Judge 调用，均无 schema/网络错误，云覆盖累计为 32/240。随后修复 ECS 缺少 v2.4 pair 基线导致的 `unknown` 假分歧：新增 SHA 绑定的 60/60 冻结侧车、缺基线零网络弃权、只选择真实 choice disagreement，并将 Judge 升级为不看策略选择/分差的 `pairwise-input.v2`。最终缓存复跑的 5 个可比 pair 只有 2 个真实分歧，Plus/Flash 各盲裁 2 次且一致率 `0/2`，新增 usage 估算 `0.0115072 CNY`；Plus 延迟 `5.446–5.794s`，Flash `1.946–2.001s`。链路工程通过但质量门禁未过，不扩大到全量；详见 [真实帧 Smoke 报告](research/evaluations/bailian-real-frame-smoke-20260719.md)。
- 2026-07-19 将冻结侧车的 `proxy_choice` 正式接入云端结果门禁，指标为账号内归一化的抓取时点可见互动代理，不含播放量、曝光或关注转化。实验连续发现并修复三处评测缺陷：`rerank --limit` 不成完整 pair；首批参考池 20 条全为高互动；审核用 `0.5` tie 阈值误用于只有左右高低侧的客观结果。最终 contract 按完整 pair、等量高/低参考和分差正负二选一执行，并用结果侧 macro-average 消除 27:13 的左右朝向偏置。40 对冻结门禁最终为云端 `66.24%`、v2.4 `69.80%`，差 `-3.56pp`，未达到 `+5pp`。扩展阶段新增 usage 估算：误抽样 `0.0037415 CNY`、20 对未平衡参考 `0.1117166 CNY`、补平衡参考与重排 `0.0901307 CNY`、40 对扩展 `0.1431662 CNY`；最终校准复算全部命中缓存、费用 0。人工偏好只保留为辅助诊断。
- 2026-07-19 用户确认百炼当前有赠送 Token，并授权将费用硬门槛提高到约 50 元。ECS 三层预算由 `0.10/20.00/20.00 CNY` 调整为单请求 `2.00`、单批次 `50.00`、单日 `50.00 CNY`；更新前备份为 `/srv/dso/backups/bailian-budget-before-50cny-20260719.env`。`/etc/dso/bailian.env` 仍为 `0600 root:root`，重启后服务为 `active/ready_for_shadow`、全部门禁通过。配置更新没有调用模型、没有新增 usage，也未改变生产排序、人工 Gold、导出或发布。赠送额度不会自动冲抵本地公开价估算，50 元是停止新增请求的硬上限而非消费目标。
- 2026-07-19 完成 D12-A 缓存消融。冻结输入仍为 `dso-multimodal-vector-value-20260719-r1`，未重抽样；报告写入 `outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/ablation-latest.json`。最高观察融合在同一 40 对上较 v2.4 为 `+11.54pp`，但这是同集权重搜索结果，置信区间下界为 0，账号胜出仅 `2/3`，因此不扩大公网调用、不冻结晋级权重。详见 [D12-A 评测记录](research/evaluations/bailian-cached-signal-ablation-20260719.md)。
- 2026-07-19 完成 D12-B 独立留出复验。ECS 部署前备份位于 `/srv/dso/backups/d12b-before-20260719`；配置、预测、评估 SHA 分别为 `7b83dbf0...8b00`、`310d49bc...a65e3`、`f2311ed5...8c680`。40 条留出样本 Text/Fusion 与 Rerank 全覆盖，120 次请求 usage 估算 `0.2243942 CNY`；独立增量为 0，yuhuan/hukan_music 均未改善，Gate 为 `inconclusive_keep_v2_4`。服务保持 `active/ready_for_shadow`，没有 Judge、完整视频上传、生产排序、人工 Gold、导出或发布变化。详见 [D12-B 评测记录](research/evaluations/bailian-independent-holdout-validation-20260719.md)。
- 2026-07-19 完成 D12-C0 零成本失败归因。ECS 只读取 D12-B 冻结 artifact 和缓存，网络请求/费用均为 0，报告 SHA 为 `1cc88ddc...624f`。20 对中 Cloud 纠正 v2.4 为 `0`，误导但被 15% 权重抑制为 `3`，共同错误为 `5`；Fusion embedding 平衡命中仅 `45.96%`。40/40 条视觉输入只有封面加单帧，同账号参考覆盖 `50%`、Text Top1 同账号率 `12.5%`。结论是先补真实三窗口帧与账号/节目分层参考池，不提高云权重、不扩 Judge。详见 [D12-C0 归因记录](research/evaluations/bailian-holdout-failure-attribution-20260719.md)。
- 2026-07-19 经用户允许，将 10 条典型完整短片暂存到 ECS 独立 `research_only` 目录，供后续 Qwen3.7 完整短片输入诊断。清单 `dso-qwen37-full-clip-diagnostic-20260719-r1` 覆盖 5 个账号、3 类内容和 5 组失败/控制比较，时长 `19.713–49.044s`、总量 `44,763,762` bytes；10/10 源 SHA、远端 SHA、FFprobe 视频与 AAC 音频通过，目录/文件权限为 `0700/0600`。该批已被既有 D12 研究观察，只能作诊断，不能冒充新留出；暂存发生时标准 Adapter 禁止视频输入，上传本身没有创建 Provider 调用或费用，也未改生产排序、Gold、导出或发布。后续单条隔离 profile 验证见下一条。详见 [完整短片暂存记录](research/evaluations/qwen37-full-clip-diagnostic-staging-20260719.md)。
- 2026-07-19 经用户明确要求分析一条视频并核算云端开销，在 ECS 为 `hcap_0e30f296cc774fcb` 生成不改时长的 540×960 H.264 无音轨代理，并使用新隔离的 `complete_short_clip` profile 执行一次 `qwen3.7-plus-2026-05-26` Shadow。预检请求体 `3,995,340` bytes、最坏预留 `0.024176 CNY`、网络 0；正式调用 HTTP 200、网络 1、重试 0、输入/输出 `5,810/836` Token、延迟 `17.048s`、usage 估算 `0.018308 CNY`，台账由 703 增至 704。输出形成 5 段视觉时间轴，代理分 `0.65`、置信度 `0.90`；模型明确音频不可用但仍出现节奏推断，因此只证明技术可行和成本，不证明排序或流量增益。`full_media` 仅为当次进程许可，持久上传等级仍为 `structured_summary,representative_frames`；未改生产排序、Gold、导出或发布。详见 [单样本报告](research/evaluations/qwen37-complete-short-clip-shadow-20260719.md)。
- 2026-07-19 使用同一源样本为 Qwen3.5-Omni 派生 21.805 秒、540×960 H.264 + 24kHz 单声道 AAC 代理，并通过隔离 `qwen35_omni_complete_short_clip` profile 执行一次固定 `qwen3.5-omni-plus-2026-03-15` Shadow。零网络预检请求体 `3,860,151` bytes、最坏预留 `0.106453 CNY`；正式调用 HTTP 200、网络 1、重试 0、输入/文本输出 `6,019/1,076` Token、延迟 `24.562s`、usage 估算 `0.092027 CNY`，台账由 704 增至 705。输出含 6 段音画时间轴、分段歌词和音乐特征；费用约为同样本 Qwen3.7 视觉版 `5.03×`、延迟 `1.44×`。歌词与同屏字幕一致，尚无独立听写证明其来自音轨，故只证明全模态链路与成本，不证明歌词准确率、排序或流量增益。持久上传等级仍未包含 `full_media`；服务为 `active/ready_for_shadow`，未改生产排序、Gold、导出或发布。详见 [Omni 单样本报告](research/evaluations/qwen35-omni-complete-short-clip-shadow-20260719.md)。
- 2026-07-19 按用户要求完成 10 条 Qwen3.5-Omni 完整短片批量诊断。`bailian_complete_clip_batch.v1` 为每条源视频生成保持完整时域的 H.264/AAC 代理，不抽代表帧；10/10 代理为 2.96–3.05 MB，最大时长误差 0.045 秒。零网络预检最坏预留 `1.292704 CNY`，低于单实验 10 元硬上限；正式 10 次请求中 7 次通过冻结 schema、3 次 `invalid_provider_response` 回退，输入/输出共 `85,368/10,798` Token，usage 估算 `1.131524 CNY`，成功请求延迟中位数/P95 为 `25.363/28.747s`。成功样本音频证据覆盖 100%，但仅 2 组双侧可比且均选错，high/low 平均传播代理分为 `0.635/0.680`，因此拒绝把模型原生传播分接入 ranker，只保留完整音视频语义抽取为研究候选。服务保持 active，持久上传等级未加入 `full_media`，未改生产排序、Gold、导出或发布。详见 [完整短片批量报告](research/evaluations/qwen35-omni-complete-clip-batch-20260719.md)。
- 2026-07-19 将完整音视频研究改为“Omni 事实特征抽取 -> 本地连接平台结果”的两阶段 contract。新增 `qwen35_omni_propagation_features`、`bailian_propagation_feature_batch.v1`、`propagation_feature_outcome_dataset.v1` 和批量 CLI；Provider 请求不含账号、标题、标签或互动结果，也不再输出模型原生传播分。字段级诊断确认首版 2/10 schema 成功主要来自时间线重叠和 `limitations` 类型/条数的机械漂移；prompt v2 与确定性结构归一化后，3 条 smoke 及冻结 10 条均为 100% 成功，音频/视觉/时序证据覆盖 100%。全量新增 7 次请求、3 次缓存复用，usage 估算 `0.733441 CNY`，延迟中位数/P95 为 `19.348/23.188s`。当前只有可见互动热度代理 10/10 可用，分享率、关注转化率、观看质量均因缺少真实分母/指标而为 0/10；5+5 配对差异只标记为低置信探索，不改生产排序。下一门禁为冻结 30 组新配对、60 条、至少 8 个账号，并以账号隔离或时间切分验证事实特征对 v2.4 的独立增量。详见 [传播特征试验](research/evaluations/qwen35-omni-propagation-features-20260719.md)。
- 2026-07-20 完成上述 30 对/60 条账号隔离门禁。4 秒配对上限经真实媒体校验后只有 29 对，因此显式放宽到 5 秒；60 个平台 item/媒体 SHA 唯一、全部含音轨，与此前完整视频 Omni 清单重叠为 0，另披露 26 条代表帧向量研究重叠。主批 59/60 严格成功，一条 58 秒样本在 1200 Token 截断后以独立 1800 Token manifest 恢复，最终 61 次请求、`6.654878 CNY`、60/60 覆盖。固定融合较 v2.4 成对命中 `+6.67pp`，但只改变同账号 2 对，Top15 命中无增益、4 个 ready 账号仅 1 个改善，故 `keep_v2_4`。新增缓存身份包含输出 Token 上限、受限报告合并、账号宏平均、精确符号检验和账号簇 bootstrap。
- 冻结前已修复 token set、同分排名和相同发布时间样本的非确定性顺序；`PYTHONHASHSEED=101/202` 两个独立全量进程指标完全一致。冻结参考报告 `bt_3eb5b599720f480e` 的可比口径为：`current_rules=1.4905`、`semantic_baseline_v2=1.4856`、`research_ranker_v2_2=1.5941`、`v2.4=1.4305`、`v2.8/v2.9=1.4356`。7 月 1–2 日的旧 lift 只保留为历史记录，不再与该 manifest 下的结果直接比较。
- 后端当前全量 `unittest discover -s tests` 为 `261` 项通过，`pytest -q` 为 `315 passed, 4 warnings`（2026-07-20，新增传播配对冻结、账号隔离评测、受限恢复合并、缓存参数失效和统计诊断测试）。前端最近一次 `npm run build` 通过；真实浏览器从顶部“模型 API”进入面板，桌面和窄屏均无横向溢出，API Key 为 `password/new-password`，控制台 warning/error 为 0。ECS 当前仅为显式授权、硬预算约束的 `research_only` Shadow 开启网络，真实摘要、代表帧及完整音视频调用均写入 usage 和逐尝试台账；没有生产排序、Gold、导出或发布变化。G1 另以 3 秒真实 FFmpeg 视频完成探测、特征降级、共享排序和边界不变量探针。
- 2026-07-19 完成本地 Provider 代码质量加固：移除前端 bundle 中硬编码的 ECS IP、root 用户和本机 PEM 路径，SSH 提示仅保留占位符模板；`public_model_runner.v2` 对共享台账的付费调用增加 POSIX 跨进程排他锁，并在每次预留前刷新批次/当日保守费用，跨批次结算 fail closed，预算恢复异常写入安全台账而不再静默吞掉。目标 Provider/预算测试 `48 passed`、全量 `315 passed, 3 warnings`、前端构建、compileall、`git diff --check`、敏感字符串扫描和浏览器回归通过。本轮只运行本地状态/Mock 测试，网络模型请求与新增费用均为 0；修复尚未部署 ECS，未改变生产排序、人工 Gold、导出或发布。
- D12-B 增量验证使用项目 Python 3.11 运行 `python -m unittest discover -s tests`，`242` 项全部通过；新增测试覆盖盲 artifact 拒绝结果字段、配置/预测 SHA、防覆盖、10 元门禁、解锁顺序和 Web action 路由。`npm run build`、`compileall`、`git diff --check` 与项目文档审计均通过。

## 11. 下一步优先级

### 11.1 产品主线

1. 冻结跨入口统一候选 benchmark：G1 标准化候选看 NDCG@K/Top-K lift，G2 同时看 Recall@K 与排序；报告账号/时间切分、严重错判、缺失信号和入口一致性。
2. 用 20-100 条真实已切短片做 G1 批量吞吐、重复文件、无音轨、ASR 失败、重试、审核和导出验收，并记录 P50/P95、失败率和人工复核时间。
3. 优先接入官方或授权账号的曝光、观看、分享和关注窗口指标，让目标从“可见互动代理”逐步升级为真实多目标反馈闭环。
4. G3 D12-C1 已完成 40 条三窗口证据包，但进一步确认当前冻结参考池即使全部补齐，同账号 high/low 双侧覆盖上限也只有 65%，且当前分层 Text 比全局 Text 低 `5.55pp`。若继续云路线，先为 4 个单侧账号补低互动对照并冻结新参考池版本，再补 9 条可恢复缓存；参考门禁通过前不构建新 Fusion。否则资源回到 G1 真实批量验收与授权平台指标闭环。
5. G3 完整音视频传播特征的 30 对/60 条账号隔离验证已完成，固定融合只有单账号局部纠错，Top15 无增益，继续 `keep_v2_4`。不要在同一 60 条继续调权；Omni 只保留为低分差分歧诊断。若继续验证，需冻结新的未见 pair，并保证至少 3 个账号各有足够配对；更优先接入真实分享率、关注转化率和观看质量分母指标。
6. Kimi K3 暂不与百炼并行实现。先按 [K3 专项规格](research/providers/kimi-k3-provider-research-20260718.md) 获取企业数据优化/保留条款和版本漂移策略；之后才实现无密钥 mock，并以 3 条合成 Smoke、10 条高分歧样本、显式批次预算验证其相对百炼的新增纠错价值。

### 11.2 当前视觉研究支线

1. D12-C1 已生成 40/40 个 15 秒三窗口证据包和 120 张真实帧；证据输入问题已解决到可重建状态，但参考池双侧覆盖和传播目标对齐仍未通过。同一 60 对不得继续用于选择新权重或宣称独立增益。
2. 暂停扩到 240 条全量和 Judge。先为 `dk_voice_teacher / hukan_music / jason_teacher / sixuweilive` 冻结低互动对照，并补当前 manifest 内 9 条可恢复参考缓存；新参考池通过覆盖审计后才允许显式构建 `fusion_d12c1`。
3. `qwen3.7-plus-2026-05-26` 与 `qwen3.6-flash-2026-04-16` 只处理 v2.4 与 Fusion+Rerank 的真实选择分歧。首批 2 个分歧上二者一致率为 `0/2`；客观结果门禁未过前不扩大 Judge。
4. 研究中心人工 A/B 继续用于编辑偏好、严重错判和代理冲突诊断，不作为证明平台流量提升的主门禁。D12-B 20 对补充集已经使用完毕，后续复验必须建立新的冻结集。
5. 向量价值实验与 D11B 选窗实验分开：前者判断整条已采集短片的检索/排序价值，后者判断完整节目内视觉窗召回；两者都通过后再讨论统一视觉证据 contract。
6. 在已完成的 [model_scheduler.v1 Phase 0–3 batch-1 基线](architecture/model-scheduling-architecture.md) 上继续验证 Omni、ASR、Embedding 的真实冻结混合 workload；通过输出等价、显存/OOM 和 GPU 空闲门禁后才评估默认启用。
