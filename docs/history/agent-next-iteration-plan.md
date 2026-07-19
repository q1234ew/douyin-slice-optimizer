# 下一轮迭代规划与接口约束

生成日期：2026-06-25
补充日期：2026-06-26
补充日期：2026-06-28
角色：架构及规划师 Agent
范围：`README.md`、`docs/history/agent-architecture-plan.md`、`docs/history/agent-product-review.md`、`docs/history/platform-v0.3-plan.md`、`src/dso/quality/insights.py`、`src/dso/feedback/importer.py`、`src/dso/features/asr.py`、`src/dso/features/asr_profile.py`、`src/dso/api/dashboard.py`、`tests/test_core.py`。

2026-06-28 追加：抖音三账号采集、数据资产化和后续三轮迭代复盘已单独沉淀到 [2026-06-28 迭代复盘与后续计划](iteration-history-20260628.md)。该文档作为后续 `V0.6 数据资产化 + 账号洞察` 的历史入口，记录当前 983 条作品资产、账号分表约定、三轮计划和暂缓事项。

2026-06-28 追加完成记录：`V0.6 数据资产化 + 账号洞察` 已按当前代码实现落地。三账号 983 条作品已导入历史样本库，生成账号内 high/mid/low 弱标签、账号基线 JSON、账号洞察 Markdown，并重建 `visible_capture` prototype bank。验证结果：`pytest -q` 全量 78 个用例通过。详情见 [2026-06-28 迭代复盘与后续计划](iteration-history-20260628.md) 第 9 节。

## 1. 本轮结论

下一轮不要先扩模型、换队列、重做 Dashboard 或改数据库大 schema。本轮目标是把已经存在的质量哨兵、推荐模拟、反馈导入和账号洞察收束成稳定 contract，让工作台能可靠回答三件事：

1. 当前节目或候选能不能进入导出预览，为什么。
2. 指标 CSV 哪些行真的进入训练闭环，哪些只是研究/未链接数据。
3. 运营复盘看到的建议来自哪个版本、哪些样本、可信度如何。

优先级排序：

| 优先级 | 事项 | 本轮定位 | 为什么 |
| --- | --- | --- | --- |
| P0 | `quality_gate` contract | 先做，只读，不阻断导出 | 当前 `quality_insights()` 已有 health、issues、actions、watchlist 和推荐模拟联动，最小改动即可形成统一决策字段；它是候选卡、导出预览和人工复核状态的共同语言。 |
| P0 | 指标导入可信度 | 紧跟 `quality_gate` 做 | 现状未链接 CSV 行会写入 metrics/snapshots，但不会进入 training_samples，返回值没有显式说明；如果不先标可信度，反馈复盘面板会把不完整数据包装成洞察。 |
| P1 | 反馈复盘面板 | 在导入报告可信后做薄面板 | `account_insights()` 已经有 top_signals/rankings，Dashboard 只需把它变成运营动作；但它必须显示样本数、低可信提醒和本次导入结果，否则会误导决策。 |
| P1 | 版本化 contract | 作为上述三项的横切最小实现 | 版本信息必须进入响应和训练样本，但本轮不为版本化单独大迁移；先用常量和响应字段保证回测可追溯，再决定是否落列。 |
| P1 | ASR 模型组合与二次转写 | contract 稳定后做，不替换默认 | 已实测 `small` 与 `large-v3-turbo-q5_0` 分场景互有胜负；下一步应让 Top 候选可按需复核，并记录差异，而不是全片默认升级。 |

执行判断：本轮第一 PR 应该落 `quality_gate` 纯函数和 `/videos/{video_id}/quality` 的新增字段；第二 PR 落 metrics import 报告可信度；第三 PR 再接 Dashboard 复盘与 gate 展示。版本字段跟随每个 PR 增量加入。

2026-06-26 补充判断：P0 contract 已完成后，ASR 模型体系优化进入下一轮增量。它的定位不是“替换默认模型”，而是建立可回测的模型组合：

- `fast=base`：全片批量召回和快速初筛。
- `quality=small`：当前发布前主质量模式，尤其保留英文歌手、英文歌名、英文介绍的稳定性。
- `verify/premium=large-v3-turbo-q5_0`：只对高价值候选、中文长口播、人名密集和节目叙事段做二次转写。
- 后续 `large-v3-q5_0` 只能在评估集证明优于上述组合后再纳入，不直接设为默认。

## 2. API / Data Contract

### 2.1 `GET /videos/{video_id}/quality?top_k=30`

保持现有字段不删不改：`video_id`、`video_title`、`generated_at`、`health`、`transcript`、`queue`、`issues`、`actions`、`watchlist`、`simulation`。

新增顶层字段：

```json
{
  "contract_version": "quality_insights.v1",
  "gate": {
    "version": "quality_gate.v1",
    "status": "review",
    "severity": "warn",
    "label": "高潜但需复核",
    "summary": "推荐模拟存在高潜候选，但 ASR/广告/上下文仍命中复核信号。",
    "generated_at": "2026-06-25T00:00:00+00:00",
    "rights_mode": "trusted_sample",
    "export_policy": {
      "preview_allowed": true,
      "batch_export_allowed": false,
      "requires_human_review": true,
      "confirmation_required": true,
      "enforcement": "advisory"
    },
    "inputs": {
      "health_score": 72,
      "health_level": "warn",
      "top_k": 30,
      "candidate_count": 30,
      "scored_count": 30,
      "watchlist_count": 3,
      "repetition_noise_count": 2,
      "ad_read_count": 4,
      "sponsor_risk_count": 1,
      "closed_loop_count": 5,
      "ready_to_export_count": 2,
      "quality_blocked_high_potential_count": 1
    },
    "reasons": [
      {
        "key": "asr_repetition_noise",
        "severity": "risk",
        "source": "transcript",
        "blocking": false,
        "count": 2,
        "evidence": "12-20s: ...",
        "recommendation": "先人工复核字幕或重跑 quality ASR。"
      }
    ],
    "required_actions": [
      "人工复核 watchlist 中的字幕、广告口播和上下文。"
    ],
    "recommended_actions": [
      "优先导出低风险高分候选作为预览样本。"
    ]
  }
}
```

`gate.status` 枚举：

| 值 | 含义 | 本轮行为 |
| --- | --- | --- |
| `allow` | 可进入导出预览和少量人工终审样本 | 只提示，不自动发布。 |
| `review` | 有高潜候选，但需要人工复核或包装优化 | 默认状态，Dashboard 高亮复核原因。 |
| `block` | 基础输入缺失或风险过高，暂不应导出 | 本轮仍为 advisory，不改变 `export_segment()` 行为。 |

`gate.severity` 枚举：`ok`、`warn`、`risk`。

建议判定规则：

- `block`：缺 transcript、缺候选、缺评分、`health.level = risk` 且存在 `missing_transcript` / `missing_candidates` / `missing_scores`，或未来 strict rights 命中 hard block。
- `review`：存在 `asr_repetition_noise`、`sponsor_risk`、`transcript_ad_reads`、`weak_closed_loop`、watchlist 非空、`health.level = warn`、`rights_mode = trusted_sample` 但准备生产导出。
- `allow`：`health.level = good`、无 `risk` severity issue、watchlist 为空或仅 info、至少有评分候选，并且推荐模拟存在 `export_preview` 或普通候选池动作。

兼容旧客户端：

- 老客户端继续读 `health`、`issues`、`actions`、`simulation`，不依赖 `gate`。
- `gate` 只新增顶层字段，不重命名旧字段。
- 本轮不把 `gate.status = block` 接入导出硬阻断；导出阻断必须后续通过显式参数或配置启用。

### 2.2 候选状态 contract

数据库已有 `candidate_segments.status`，现有值包括 `candidate`、`corrected`。本轮不要强制迁移历史值，先在 API/UI 中提供规范化状态。

新增响应字段建议用于候选列表和复核面板：

```json
{
  "status": "corrected",
  "review_status": "needs_review",
  "review_status_label": "已修正，待复核",
  "gate_status": "review"
}
```

`review_status` 枚举：

| 值 | 映射/含义 |
| --- | --- |
| `candidate` | 原始候选，尚未人工处理。 |
| `needs_review` | 质量 gate、watchlist、人工修正或授权信号要求复核；旧 `corrected` 默认映射到此状态。 |
| `approved` | 人工确认可进入导出预览。 |
| `blocked` | 人工或规则确认暂不导出。 |
| `exported` | 已有 `slice_variants.export_path`。 |

兼容旧客户端：

- `candidate_segments.status` 原值继续保留。
- 新状态作为派生字段返回，不要求第一步 ALTER TABLE。
- 如果后续需要持久化审核状态，再新增列或审核事件表，不复用旧 `status` 做复杂状态机。

### 2.3 `POST /metrics/import`

保持旧字段：`imported`、`snapshots`、`training_samples`、`baselines`、`path`。

新增字段：

```json
{
  "contract_version": "metrics_import.v1",
  "status": "import_completed_with_warnings",
  "imported": 2,
  "snapshots": 2,
  "training_samples": 1,
  "baselines": 8,
  "path": "/tmp/upload.csv",
  "row_summary": {
    "total_rows": 2,
    "imported_metrics": 2,
    "created_snapshots": 2,
    "linked_rows": 1,
    "unlinked_rows": 1,
    "skipped_rows": 0,
    "created_training_samples": 1,
    "rebuilt_baselines": 8
  },
  "row_issues": [
    {
      "row_number": 3,
      "link_status": "unlinked",
      "trust_status": "partial",
      "reason": "candidate_segment_id not found",
      "identifiers": {
        "candidate_segment_id": "seg_missing",
        "slice_variant_id": "",
        "experiment_id": ""
      },
      "training_eligible": false,
      "action": "确认 CSV 中 candidate_segment_id，或先创建对应 variant/experiment。"
    }
  ],
  "training_eligibility": {
    "eligible_rows": 1,
    "ineligible_rows": 1,
    "policy": "Only linked metric_snapshots create training_samples."
  }
}
```

`status` 枚举：

| 值 | 含义 |
| --- | --- |
| `import_completed` | 所有行已导入且可链接。 |
| `import_completed_with_warnings` | 至少一行未链接或可信度不足，但文件处理完成。 |
| `import_failed` | 文件无法解析或没有有效行。 |

`link_status` 枚举：

| 值 | 含义 |
| --- | --- |
| `linked` | 行已解析到 `candidate_segment_id`，可进入 snapshots/training。 |
| `unlinked` | 行写入表现数据或快照，但无法进入 training_samples。 |
| `skipped` | 行无有效指标或格式错误，本轮建议先只报告，是否跳过由实现决定。 |

`trust_status` 枚举：

| 值 | 含义 |
| --- | --- |
| `trusted` | 自有/授权候选、可链接、字段完整，训练可用。 |
| `partial` | 指标可参考，但链接、窗口、曝光或字段不完整。 |
| `untrusted` | 不应进入训练或运营结论，只能作为错误报告。 |

兼容旧客户端：

- `imported` 继续表示写入的 `performance_metrics` 数。
- `snapshots` 继续表示创建的 `metric_snapshots` 数，新增 `linked_rows` 区分训练资格。
- 不删除旧返回字段，Dashboard 新文案优先使用 `row_summary`。

### 2.4 反馈复盘面板 contract

本轮优先复用 `GET /accounts/{account_id}/insights`，不新建复杂分析服务。建议在响应中新增薄层：

```json
{
  "contract_version": "account_insights.v1",
  "account_id": "main",
  "sample_count": 24,
  "data_quality": {
    "training_sample_count": 24,
    "low_confidence_count": 5,
    "minimum_reliable_sample_count": 10,
    "confidence_level": "medium",
    "warnings": [
      "部分分组样本少于 3 条，只能作为趋势先验。"
    ]
  },
  "recommendations": [
    {
      "key": "duration_bucket_medium",
      "label": "优先测试中等时长切片",
      "evidence": "medium bucket reward_proxy 排名第一，样本 8 条。",
      "action": "下一轮候选 Top 队列保留 24-60 秒闭环片段。",
      "confidence": "medium",
      "source": "rankings.duration_bucket"
    }
  ]
}
```

`confidence_level` / `recommendations[].confidence` 枚举：`low`、`medium`、`high`。

兼容旧客户端：

- 旧字段 `top_signals`、`rankings`、`by_slice_type`、`by_structure` 等继续存在。
- 新面板只消费新增 `data_quality` 和 `recommendations`；缺失时退回展示旧 rankings。

### 2.5 版本化 contract

本轮只做最小版本字段，不做 schema 大迁移。

建议常量：

```text
QUALITY_INSIGHTS_CONTRACT_VERSION = "quality_insights.v1"
QUALITY_GATE_VERSION = "quality_gate.v1"
METRICS_IMPORT_CONTRACT_VERSION = "metrics_import.v1"
ACCOUNT_INSIGHTS_CONTRACT_VERSION = "account_insights.v1"
SEGMENTER_VERSION = "music_variety_segmenter.v1"
SCORER_VERSION = "music_variety_rules.v1"
TRAINING_FEATURE_VERSION = "rules.v1"
```

响应字段建议：

```json
{
  "component_versions": {
    "segmenter": "music_variety_segmenter.v1",
    "scorer": "music_variety_rules.v1",
    "quality_gate": "quality_gate.v1",
    "metrics_import": "metrics_import.v1"
  }
}
```

训练样本：

- 保持 `training_samples.feature_version` 字段。
- 当前 `v1.rules` 可继续兼容。
- 新写入建议使用 `rules.v1` 或继续 `v1.rules`，但在 `account_baseline_snapshot` JSON 中附加 `component_versions`，避免第一步改表。

## 3. 小步实现任务清单

### PR 1：`quality_gate` 只读 contract

任务：

1. 在 `src/dso/quality/insights.py` 增加纯函数 `quality_gate_from_insights(report, rights_mode=None)`。
2. 在 `quality_insights()` 返回中新增 `contract_version`、`component_versions`、`gate`。
3. `gate.export_policy.enforcement` 固定为 `advisory`，不阻断导出。
4. Dashboard 先只展示 gate label/status，不改导出按钮行为。

测试：

- clean transcript + good health -> `gate.status = allow`。
- missing transcript / candidates / scores -> `gate.status = block`。
- ASR repetition / sponsor risk / watchlist -> `gate.status = review`。
- 老测试继续断言 `health`、`issues`、`simulation` 不变。

回滚：

- 删除新增 gate 函数和返回字段即可，旧客户端不受影响。

### PR 2：指标导入可信度报告

任务：

1. 在 `import_metrics()` 中统计 `total_rows`、`linked_rows`、`unlinked_rows`、`skipped_rows`。
2. 新增 `row_summary`、`row_issues`、`training_eligibility`。
3. 未链接行必须显式 `training_eligible = false`。
4. 保持旧字段 `imported`、`snapshots`、`training_samples`、`baselines`。

测试：

- 一条有效 `candidate_segment_id` + 一条不存在 ID：`imported = 2`，`linked_rows = 1`，`unlinked_rows = 1`，`training_samples = 1`。
- 百分比字段和 `avg_watch_seconds` 推导 `avg_watch_ratio` 继续通过。
- `row_issues` 不包含敏感文件路径，只含行号和业务 ID。

回滚：

- 新字段只影响返回报告；旧导入链路和 DB 表不需要回滚。

### PR 3：反馈复盘薄面板

任务：

1. Dashboard 的反馈页接入 `/accounts/{account_id}/insights`。
2. 显示 `data_quality`、`top_signals`、`rankings` 前 3 项和 `recommendations`。
3. metrics import 上传完成后，直接显示 `row_summary` 和未链接提醒。
4. 样本数低于阈值时，文案必须标成趋势先验，不得显示成确定结论。

测试：

- `account_insights("main")` 样本为空时返回可渲染空态。
- 有样本时 `recommendations` 至少能从 duration/type/structure 中生成一条。
- Dashboard render 不要求浏览器即可包含必要容器或状态文本。

回滚：

- UI 只读消费 API；删除面板不影响核心导入、评分和导出。

### PR 4：版本字段横切补齐

任务：

1. 增加版本常量，避免在多个文件中写散字符串。
2. `/videos/{id}/quality`、`/metrics/import`、`/accounts/{id}/insights` 都返回 `contract_version`。
3. 新 training sample 的 `feature_version` 保持兼容，并在 baseline snapshot JSON 中附加 `component_versions`。
4. scorer/generator 暂不新增 DB 列；先在解释或响应中暴露版本。

测试：

- 三个 API/函数返回 JSON 可序列化。
- training sample 仍能被旧测试读取 `feature_version` 字符串。

回滚：

- 常量和新增响应字段可单独删除，不涉及数据迁移。

### PR 5：导出前提示，不做硬阻断

任务：

1. `POST /segments/{segment_id}/export` 返回中新增 `quality_gate` 或 `quality_warnings`。
2. 默认不改变 `export_segment()` 的权限行为，仍只由 rights hard block 阻断。
3. 预留显式开关：未来可用 `DSO_QUALITY_GATE_ENFORCE=1` 或 `?enforce_quality_gate=true` 启用阻断。

测试：

- trusted sample 仍可导出。
- strict rights 仍由现有 `PermissionError` 阻断。
- review/block gate 仅出现在返回 warning，不导致现有导出测试失败。

回滚：

- 删除返回 warning 字段即可。

### PR 6：ASR 模型组合与候选二次转写

任务：

1. 在 `src/dso/features/asr_profile.py` 中扩展 profile：保留 `fast=base`、`quality=small`，新增 `verify` 或 `premium=large-v3-turbo-q5_0`。
2. 在 `doctor` 和 README 中展示 profile 用途、模型路径、VAD 状态、最近 benchmark artifact。
3. 新增候选级二次转写入口：输入 `segment_id` 或 `video_id + start/end`，只重跑该片段，不覆盖全片 transcript。
4. 二次转写输出保存为独立 artifact，包含 `quality` 与 `verify/premium` 文本差异、耗时、模型、prompt、后处理版本。
5. Dashboard 候选详情只读展示二次转写结果，允许人工选择采信版本；默认仍使用 `quality=small` 的全片 transcript。
6. 英文歌手/英文歌名场景必须保留 `small` 结果作为候选之一，不允许被 `verify/premium` 自动覆盖。

测试：

- `resolve_asr_model_size(profile="verify")` 或 `profile="premium"` 返回 `large-v3-turbo-q5_0`。
- 缺少 premium 模型时，命令明确返回 `missing_whisper_cpp` 或可操作的 setup 提示，不回退到 faster-whisper。
- 候选级二次转写不会修改 `source_videos.transcript_path` 指向的全片 transcript。
- 英文自然句和英文歌名不会被质量哨兵误判为重复噪声。
- 生成的 compare artifact JSON 可序列化，并包含两个模型的 source、wall_seconds、segments 和文本摘要。

回滚：

- 删除新增 profile 和候选级入口即可；`fast/base` 与 `quality/small` 继续可用，已有全片 transcript 不受影响。

## 4. 本轮不做

- 不训练 LightGBM/深度模型。
- 不引入 Celery/RQ/Postgres。
- 不做自动发布、自动互动或平台规避。
- 不接真实抖音 OAuth/OpenAPI/SDK；本轮只保留 CSV 回流和后续数据来源 contract 的兼容空间。
- 不把 `quality_gate` 第一版接成硬阻断。
- 不为了版本化立即改所有历史表。
- 不重构 `api/dashboard.py` 大文件；只做最小 UI 消费。
- 不把 `large-v3-turbo-q5_0` 或 `large-v3` 设为全局默认模型。
- 不批量重跑历史视频；只对 sample 和人工选定候选做可回滚的片段级验证。
- 不让 premium 结果自动覆盖英文歌手/英文歌名相关片段，必须保留 `small` 对照。

## 5. 验收标准

本轮完成后，工程师应能用 `PYTHONPATH=src python3 -m unittest tests.test_core` 验证核心逻辑，并能通过函数/API 返回看到：

1. 质量报告有稳定 `gate.status`、原因和导出策略。
2. 指标导入能明确说明 linked/unlinked/skipped，以及哪些行进入训练样本。
3. 反馈面板能展示账号洞察、样本可信度和下一轮动作建议。
4. 每个新 contract 都有版本号，旧字段保持可用。
