# Agent Next Product Review

审阅日期：2026-06-25
角色：下一轮迭代产品评审员 Agent
范围：`docs/agent-product-review.md`、`docs/agent-architecture-plan.md`、`src/dso/api/dashboard.py`、`src/dso/quality/insights.py`、`src/dso/feedback/importer.py`、`tests/test_core.py`，并补充只读查看 `src/dso/api/main.py`、`src/dso/cli.py`、`README.md` 中的 API/首启入口。

## 1. 运营视角：quality gate 是否足够清楚

结论：方向已经接近，但还不够清楚。当前 `quality_insights()` 已返回 `health`、`issues`、`actions`、`watchlist` 和推荐模拟联动；`_simulation_decision()` 也已有“优先导出预览 / 高潜但需复核 / 等待 ASR 重跑 / 包装二次优化 / 小流量测试”。问题是这些仍是分散提示，还不是运营可一眼执行的 `allow / review / block` 门禁 contract。

当前 Dashboard 的质量哨兵位置合理：节目管理区和推荐模拟区都展示质量健康分，候选卡也能显示“质量复核”。但导出按钮仍是直接“导出/重导”，预览区也没有把质量结论作为主动作提示。运营会知道“这里有风险”，但不一定知道“现在应该导出、复核、重跑 ASR，还是先补流程”。

建议本轮新增只读 `quality_gate` 字段，不改变导出行为，先稳定文案、位置和动作：

| status | 运营文案 | 触发语义 | 主位置 | 下一步动作 |
| --- | --- | --- | --- | --- |
| `allow` | 可导出预览 | ASR、候选、评分齐全；健康分稳定；Top 队列无高危质量旗标；推荐模拟高潜且风险低 | 质量哨兵主徽标、候选卡状态、预览区导出按钮旁 | 导出 1-3 条 9:16 预览，进入标题/封面和人工终审 |
| `review` | 需人工复核 | 有广告口播、上下文不足、低原创/授权提示、ASR 轻中度风险，或“高潜但被质量风险拦下” | 质量哨兵主徽标、候选卡“质量复核”、复核列表 | 打开候选详情，检查字幕、上下文、版权/广告词；必要时重跑 quality ASR；复核通过后再导出 |
| `block` | 暂缓导出 | 缺 ASR、缺候选、缺评分、健康分高风险、ASR 重复幻觉严重，或 strict rights 明确不通过 | 质量哨兵主徽标、预览区主动作位、候选卡禁用态提示但本轮不实际阻断 | 先完成 ASR/候选/评分，或补授权/重跑 ASR，再刷新质量门 |

建议 API contract 最小形态：

```json
{
  "gate": {
    "version": "v0.3.1.quality_gate.v1",
    "status": "allow|review|block",
    "label": "可导出预览|需人工复核|暂缓导出",
    "summary": "一句运营可读解释",
    "reasons": [
      {
        "key": "asr_repetition_noise",
        "severity": "risk|warn|info",
        "scope": "video|segment",
        "label": "ASR 幻觉/重复文本",
        "evidence": "12-20s: ...",
        "next_action": "重跑 quality ASR 后刷新候选",
        "segment_ids": []
      }
    ],
    "primary_action": {
      "kind": "export_preview|open_review_queue|rerun_asr|generate_candidates|score_candidates",
      "label": "导出预览|打开复核队列|重跑 ASR|生成候选|运行评分"
    },
    "allowed_actions": ["view_candidates", "export_preview"],
    "blocked_actions": [],
    "source": {
      "health_score": 90,
      "health_level": "good",
      "rights_mode": "trusted_sample",
      "top_k": 30
    }
  }
}
```

关键产品约束：本轮是只读 gate，不应改变 `export_segment()` 或 `/segments/{id}/export` 的实际行为。Dashboard 可以展示“暂缓导出/需复核”提示，但不要在这一轮把导出硬阻断做进产品行为，否则验收面会变大。

## 2. 反馈复盘面板最少信息

结论：不要把反馈页扩成指标仓库。它应该回答三个运营问题：这批数据能不能信、表现最强的信号是什么、下一轮剪辑该怎么调。

建议保留四块最少信息：

1. **导入可信度**
   - 最近一次导入：导入行数、成功链接候选数、未链接行数、生成训练样本数。
   - 当前账号样本数、窗口覆盖：6h/24h/72h/7d/30d 有哪些。
   - 低样本提示：`n < 5` 时明确“仅作方向参考”。

2. **Top 表现信号**
   - 从 `account_insights()` 的 `top_signals` 取最多 5 个维度：切片类型、结构、hook、时长桶、发布时间。
   - 每个只展示：名称、`reward_proxy`、样本数 `n`、播放转化或 5 秒留存中的一个代表指标。
   - 不展示全量 group 表，不展示所有原始指标列。

3. **下一轮建议**
   - 一句话建议，例如“下轮优先增加节目上下文 -> 歌曲爆点 -> 现场反应的 30-60s 候选”。
   - 同时展示置信度来源：样本数、曝光量、是否归一化过基线。
   - 如果没有样本，显示空态：“暂无表现数据，先导入已发布候选的 CSV”。

4. **不可学习/需处理**
   - 未链接 CSV 行、rights blocked/review 样本、高负反馈样本。
   - 这块只做异常清单，不进入排行榜，避免运营误以为所有导入数据都进入学习闭环。

当前源码发现：`account_insights()` 已经能产出 `top_signals`、`rankings` 和多维 group；但 Dashboard 的 `loadFeedback()` 只调用 `/training-samples` 和 `/accounts/{id}/baselines`，没有调用 `/accounts/{id}/insights`。所以下一步最小产品改动应该是接入 insights 并做摘要，不是继续增加训练样本/基线表格。

## 3. 本轮验收清单

### API 级

- `GET /videos/{video_id}/quality?top_k=30` 返回新增 `gate` 字段，且字段只包含稳定枚举：`allow / review / block`。
- `gate.status` 与 `gate.label`、`gate.summary`、`gate.primary_action` 一致，不出现“export_preview”这类模拟内部枚举作为顶层门禁状态。
- 缺视频仍返回 404；缺 ASR、缺候选、缺评分返回 200 + `gate.status=block` 或 `review`，不把待处理状态当异常。
- clean fixture：whisper.cpp + VAD、无广告口播、闭环候选、高潜模拟，得到 `allow`。
- ASR 重复幻觉 fixture：得到 `review` 或严重时 `block`，`reasons` 包含 `asr_repetition_noise` 和重跑 ASR 动作。
- 广告/品牌口播 fixture：得到 `review`，`reasons` 包含 `sponsor_risk` 或 `transcript_ad_reads`。
- 未生成候选或未评分 fixture：得到 `block`，主动作分别是 `generate_candidates` 或 `score_candidates`。
- strict rights 不清晰时进入 `review/block`；`trusted_sample` 要在 `gate.source.rights_mode` 中显式展示。
- `/accounts/{account_id}/insights` 在空数据时返回稳定空态，在有样本时返回 `top_signals` 可用于 Dashboard 摘要。
- 如果本轮改 `import_metrics()` 报告，应验收 linked/unlinked/skipped 计数；未链接候选不应被误报为训练样本。

### Dashboard 源码级

- `renderQuality()` 读取 `report.gate`，主视觉从“质量健康分”升级为“门禁结论 + 健康分”，文案优先显示 `gate.label` 和 `gate.summary`。
- 节目管理区、推荐模拟区共用同一 gate 渲染，不复制两套判断。
- 候选卡显示 segment 相关的复核原因；无 segment 级原因时只显示视频级 gate。
- 预览区导出按钮旁显示 gate 主动作：`allow` 时“导出预览”，`review` 时“先复核”，`block` 时“先重跑 ASR/补候选/运行评分”。本轮只提示，不硬阻断。
- `quality_gate` 与现有推荐模拟联动共存：模拟内部仍可保留 `export_preview/review/wait_for_asr`，但顶层门禁只暴露 `allow/review/block`。
- `loadFeedback()` 增加 `/accounts/{account_id}/insights`，反馈页只渲染导入可信度、Top 表现信号、下一轮建议、不可学习/需处理四块。
- 反馈页空态必须覆盖：无样本、无 baselines、无 insights、当前账号不存在。
- Dashboard 首屏无视频时不报错；有视频但无 quality 时仍能显示空态并继续查看候选。

### CLI/Web 首启级

- `PYTHONPATH=src python3 -m dso.cli doctor` 输出 JSON，并包含 FFmpeg、FFprobe、ASR、rights mode、路径信息。
- `PYTHONPATH=src python3 -m dso.cli web --help` 在 Typer 缺失时仍通过 argparse fallback 显示 `web` 用法。
- FastAPI/Uvicorn 缺失时，`cmd_web()` 和 argparse fallback 都给出清晰依赖安装提示，不出现 traceback。
- 安装 dev 依赖后，`dso web --reload` 应能启动；本轮自动验收不要启动长时间服务，可用 import/TestClient 或帮助命令做轻量检查。
- Dashboard 首次加载应能调用 `/runtime`，并在 Web UI 可见 ASR/FFmpeg 状态；运行环境不应只藏在反馈页。

### 端到端核心链路级

- 使用临时 `DSO_ROOT`，跑通 ingest -> extract/sidecar ASR -> generate_segments -> score -> quality -> simulation -> export preview。
- clean 样本断言：`gate.status=allow`、至少 1 条模拟高潜、导出结果包含 MP4/SRT/cover 路径。
- 风险样本断言：ASR 重复、广告口播、弱上下文分别进入 `review/block`，并出现在 watchlist/reasons。
- feedback 样本断言：导入一条可链接 CSV 后生成 metric snapshot、training sample、baseline，并且 `/accounts/{id}/insights` 的 Top 信号可渲染摘要。
- unlinked CSV 断言：导入不存在的 `candidate_segment_id` 后，不应生成 training sample；报告中应显示未链接行。
- 回归测试保留现有 31 个核心 unittest，并为 `quality_gate` 新增至少 4 个 fixture：clean allow、missing flow block、ASR risk review/block、sponsor review。

## 4. 本地只读验证

已运行：

- `PYTHONPATH=src python3 -m unittest tests.test_core`：通过，31 个测试，约 0.77 秒。
- `PYTHONPATH=src python3 -m dso.cli doctor`：通过。FFmpeg/FFprobe 可用；ASR ready；active backend 为 whisper.cpp；VAD 已启用；rights mode 为 `trusted_sample`。
- `PYTHONPATH=src python3 -m dso.cli web --help`：通过，当前走 argparse fallback，能显示 `dso web` 的 host/port/reload 参数。
- `rg -n "quality_gate" src/dso/quality/insights.py src/dso/api/dashboard.py src/dso/api/main.py tests/test_core.py`：无匹配，说明当前尚未有显式 `quality_gate` contract。
- `PYTHONPATH=src python3 -m pytest -q tests/test_core.py`：失败，当前 Python 环境没有 `pytest` 模块。
- `PYTHONPATH=src python3 -c "import fastapi, uvicorn; print('fastapi/uvicorn ok')"`：失败，当前环境没有 FastAPI/Uvicorn，不能启动 Web 服务。
- `PYTHONPATH=src python3 -c "import typer; print('typer ok')"`：失败，当前环境没有 Typer；argparse fallback 可用。
- `PYTHONPATH=src python3 -m dso.cli insights --account main`：通过，当前 DB 返回 `sample_count=0`、`暂无表现数据`，反馈页空态需要保持清楚。

环境发现：

- `git status --short` 在当前工作区返回 `fatal: not a git repository`，本次无法用 git 状态确认改动范围。
- 本次未启动长时间服务，未做浏览器点击流验证。
- 本次未修改业务代码；仅新增本报告文件。

## 5. 产品结论

本轮最重要的产品收敛不是增加更多风险指标，而是把已有质量哨兵、推荐模拟、候选复核和导出动作统一成一个只读门禁语言。只要 API 顶层稳定返回 `allow/review/block`，Dashboard 就能从“信息很多”变成“下一步明确”。

反馈复盘同理：已有 `account_insights()` 足够支撑第一版摘要，不需要先做复杂数据表。最小可用面板只展示导入可信度、Top 表现信号、下一轮建议和不可学习异常，运营就能知道下一轮剪辑该往哪里调。
