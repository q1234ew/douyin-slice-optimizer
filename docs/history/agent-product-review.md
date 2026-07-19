# Agent Product Review

审阅日期：2026-06-25
角色：产品评审员 Agent
范围：README、V0.3 计划、架构文档、Dashboard/API 实现、核心测试与本地只读验证。

## 1. 关键路径完整性

结论：核心引擎链路已闭合，Dashboard 工作台形态已具备，但当前环境下 Web 首启不可用，产品化闭环还没有达到“运营拿来就能稳定使用”的成熟度。

- 导入视频 -> 提取/ASR -> 生成候选 -> 评分 -> 建议 -> 导出：基本完整。README 已承诺这条 CLI 路径，API 也提供 `/videos`、`/videos/{id}/extract`、`/videos/{id}/segments`、`/videos/{id}/score`、`/videos/{id}/suggestions`、`/segments/{id}/export` 和 `/exports/...`。本次用临时样本跑通，并成功导出 1080x1920 的 9:16 MP4、SRT 和封面。
- Dashboard 对导入、处理、候选审核、导出预览有入口：左侧导入表单、节目表格、候选队列、评分详情、导出按钮和在线预览区域已经存在。
- 导入指标 -> 生成快照/训练样本/账号基线：基本完整。`/metrics/import`、`/training-samples`、`/accounts/{id}/baselines` 已接入，测试也覆盖指标快照、训练样本和基线生成。
- 指标回流后的“复盘”仍偏弱：API 有 `/accounts/{id}/insights`，但 Dashboard 的反馈页只加载训练样本和 baselines，没有展示 `account_insights` 里的结构、hook、时长或账号最优信号。运营能看到“有样本和 P75/P90”，但难以立刻回答“下一条应该剪哪类、什么时候发、该优化什么”。
- 人工审核/校正能力未充分产品化：API 有 performances 和 candidate correction 端点，但 Dashboard 没有明显的人工修正入口。对于音乐综艺这种 ASR、歌曲段落、导师点评边界常需人工确认的场景，这是从工具到工作台的关键缺口。

## 2. Dashboard 决策信息不够直观的地方

- 工作流程状态偏静态：侧栏“节目解析与提取 / ASR / 高能点检测 / 候选生成 / 评分”都显示“就绪”，没有跟随选中节目变成“未开始、处理中、已完成、失败、需重跑”。
- 质量哨兵方向正确，但还不够像发布门禁：它展示健康分、ASR 后端、重复幻觉、广告口播、Top 闭环和待复核数，但没有明确的“允许导出 / 先复核 / 重跑 ASR / 阻断批量导出”总判定。
- 候选卡对运营仍需解读：候选列表展示综合分、结构、类型、合规和导出状态，但推荐模拟里的决策标签没有稳定并入候选卡主视图。运营需要在候选、详情、推荐模拟之间来回切换。
- 数据反馈页更像数据仓库状态，不像复盘面板：训练样本数和基线 P75/P90 有用，但缺少“本次导入后新增了什么洞察”“高表现结构/时长/发布时间/标题模板是什么”“哪些候选应该影响下一轮评分”。
- 运行环境入口藏得较深：运行环境在反馈页，而 README 的 Quick Start 很依赖环境正确。首次使用者更需要在工作台首屏直接看到 Web/ASR/FFmpeg 是否可用。

## 3. 功能完成度评分

MVP 核心功能完成度：7/10。
运营工作台成熟度：5.5/10。

主要依据：

- 已完成：视频导入、ASR/sidecar/占位降级、候选生成、评分、标题/封面/字幕建议、导出、质量洞察、推荐模拟、指标导入、训练样本、账号基线。
- 未成熟：Web 首启依赖与 README 体验不稳；质量和推荐尚未形成明确门禁；反馈复盘没有把指标变成运营动作；人工校正入口缺失；端到端 Web/UI 自动化测试缺失。

阻碍成熟产品的 Top 问题：

1. Web 首启不可用会直接打断 Quick Start。当前环境缺少 FastAPI/uvicorn/Typer，`python -m dso.cli web --reload` 走 argparse fallback 后报 `invalid choice: 'web'`，无法验证 Dashboard 截图或真实浏览器流程。
2. “发布前质量门”还主要是提示，不是工作流控制。质量风险、推荐模拟、导出按钮之间已有联动雏形，但高风险候选仍缺少明确的导出拦截、复核清单状态和通过/驳回记录。
3. 指标回流没有形成可操作复盘。导入后能生成 reward_proxy、训练样本和 baselines，但 Dashboard 没把账号洞察、变化解释和下一轮剪辑建议呈现出来。
4. 人工审核数据入口不足。音乐综艺切片高度依赖歌曲段、导师点评、反应镜头和授权判断，API 有校正能力，Dashboard 未把它做成运营日常动作。
5. 测试偏核心逻辑，缺少 Web/API 级回归。`tests/test_core.py` 覆盖很好，但当前没有可运行的 FastAPI/TestClient 或浏览器级端到端检查来防止 Dashboard 流程断裂。

## 4. 本轮建议开发优先修的 3 件事

1. 修复 Web 首启体验：确保 `python3 -m pip install -e ".[dev]"` 后 `dso web --reload` 可用；如果 Typer 缺失走 argparse fallback，也要支持 `web` 或给出明确安装提示。把 FastAPI/uvicorn/Typer 缺失变成 `doctor` 和 Dashboard 可见的阻塞项。
2. 把质量哨兵升级成运营门禁：在候选卡和预览区直接显示 `优先导出 / 高潜需复核 / 重跑 ASR / 包装优化 / 小流量测试`，并在风险候选导出前要求人工确认或记录复核状态。
3. 完成反馈复盘面板：Dashboard 接入 `/accounts/{id}/insights`，展示本次导入后的高表现切片类型、时长桶、发布时间、hook/结构信号和下一轮候选生成建议。

## 5. 测试与验证

- `pytest -q tests/test_core.py`：失败，当前 shell 无 `pytest` 命令。
- `python3 -m pytest -q tests/test_core.py`：失败，当前 Python 无 `pytest` 模块。
- `PYTHONPATH=src python3 -m unittest tests.test_core`：通过，27 个测试，约 0.95 秒。
- `PYTHONPATH=src python3 -m dso.cli doctor`：通过。FFmpeg/FFprobe 可用；ASR 状态 ready；当前 active backend 为 whisper.cpp，VAD 已配置；默认 rights mode 为 `trusted_sample`。
- 本地临时端到端脚本：通过。临时 `DSO_ROOT=/private/tmp/dso-product-review-6d23nk8i`，生成 48 秒测试视频和 sidecar SRT，跑通 ingest、transcribe、audio features、generate_segments、score、suggestions、export、quality_insights、simulate_video、import_metrics、training_samples、baselines。结果：候选 1 条、评分 1 条、Top 分 83.4、质量分 90 good、推荐模拟均分 85.83、指标导入 1 条、训练样本 1 条、基线 8 条。
- `ffprobe` 导出文件：通过。导出 MP4 为 1080x1920，时长 41 秒，并生成字幕和封面。
- `PYTHONPATH=src python3 -m dso.cli web --reload`：失败。argparse fallback 不包含 `web` 命令；同时当前 Python 环境缺少 `fastapi` 和 `uvicorn`，所以未能启动服务或截图。

## 6. 证据限制

本次没有修改业务代码。由于 Web 依赖未安装且服务无法启动，未完成浏览器截图和真实 Dashboard 点击流验证；Dashboard 评审基于源码、核心测试和 CLI/函数级端到端验证。
