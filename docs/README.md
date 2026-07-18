# 技术文档入口

本文档是项目文档的总入口。长期维护文档放在 `docs/`，实验输出、采集批次说明和一次性报告保留在 `output/`、`outputs/`，但不作为最新技术口径。

仓库内开发 Agent 还必须遵守根目录 [AGENTS.md](../AGENTS.md)，它汇总了全局执行要求，并链接到本目录中的详细规范。

## 推荐阅读顺序

1. [product-goals.md](product-goals.md): 项目最高层目标、指标口径、公网模型成本门和持续迭代原则。
2. [user-manual.md](user-manual.md): 面向剪辑、运营和管理员的平台用户手册，覆盖安装、Web 工作台、CLI、数据回流和日常 SOP。
3. [current-state.md](current-state.md): 当前工程状态、目标完成度、数据口径、服务启动、验证结果和近期风险。
4. [development-requirements.md](development-requirements.md): 开发约束、测试要求、数据入库规则、前端构建要求。
5. [architecture.md](architecture.md): 系统架构、模块边界、数据模型和 API 草案。
6. [model-and-algorithm-radar.md](model-and-algorithm-radar.md): 新模型与算法的发现、证据、成本、状态和汇报记录。
7. [douyin-collection-standard.md](douyin-collection-standard.md): 抖音数据采集标准、合规边界、质量门和去重规则。
8. [douyin-media-collection-flow.md](douyin-media-collection-flow.md): 抖音切片视频媒体采集流程、CLI 命令、目录结构和质量门。
9. [iteration-history-20260629.md](iteration-history-20260629.md): 2026-06-29 历史样本资产、V1 可学习排序闭环的阶段入口。
10. [iteration-history-20260628.md](iteration-history-20260628.md): 2026-06-28 这一轮迭代复盘和后续计划。

## 核心维护文档

| 文档 | 作用 | 什么时候更新 |
| --- | --- | --- |
| [product-goals.md](product-goals.md) | 北极星目标、成功指标、模型/API 准入和迭代原则 | 产品目标、优先级、成功指标或长期边界变化后 |
| [user-manual.md](user-manual.md) | 面向平台使用者的安装、操作、数据回流和排障手册 | Web 操作流程、CLI 命令、导入字段、运行环境或合规边界变化后 |
| [current-state.md](current-state.md) | 当前状态快照，包括样本规模、原型库、运行命令、已知缺口 | 每次完成数据入库、模型策略、服务能力或重大 UI 变更后 |
| [development-requirements.md](development-requirements.md) | 开发要求和验收标准 | 每次新增工程约束、测试要求、数据口径或部署流程后 |
| [architecture.md](architecture.md) | 长期系统架构与数据模型 | 表结构、模块边界、API 合同发生实质变化后 |
| [model-and-algorithm-radar.md](model-and-algorithm-radar.md) | 新模型/算法机会、验证状态、成本与结论 | 发现新方案、完成 benchmark 或准入状态变化后 |
| [douyin-collection-standard.md](douyin-collection-standard.md) | 采集规范和数据质量标准 | 采集字段、去重规则、质量门或合规边界变化后 |
| [douyin-media-collection-flow.md](douyin-media-collection-flow.md) | 抖音切片视频媒体采集流程和 CLI 使用方法 | 媒体采集字段、目录结构、命令参数或质量门变化后 |

## 产品与策略文档

| 文档 | 作用 |
| --- | --- |
| [music-variety-strategy.md](music-variety-strategy.md) | 音乐综艺切片策略、评分维度、标题封面策略 |
| [algorithm-study.md](algorithm-study.md) | 推荐系统与算法学习笔记，作为后续模型路线参考 |
| [research.md](research.md) | 早期研究调研和论文到系统的映射 |
| [paper-architecture-review.md](paper-architecture-review.md) | 主线论文复核后的架构更新建议 |
| [public-model-provider-research-20260718.md](public-model-provider-research-20260718.md) | G3 大陆真实 Provider 官方资料、价格、数据条款、首选百炼与冻结 Shadow 方案 |

## 迭代与评审文档

这些文档保留当时的判断，不一定代表当前最新口径。若和 `current-state.md` 或 `development-requirements.md` 冲突，以后两者为准。

| 文档 | 作用 |
| --- | --- |
| [platform-v0.3-plan.md](platform-v0.3-plan.md) | V0.3 阶段计划 |
| [agent-architecture-plan.md](agent-architecture-plan.md) | 多 Agent 架构诊断和成熟产品迭代计划 |
| [agent-next-iteration-plan.md](agent-next-iteration-plan.md) | 下一轮接口约束与小步实现清单 |
| [agent-product-review.md](agent-product-review.md) | 产品评审记录 |
| [agent-next-product-review.md](agent-next-product-review.md) | 下一轮产品评审记录 |
| [douyin-visible-collection-flow.md](douyin-visible-collection-flow.md) | 可见数据采集流程记录 |
| [iteration-history-20260629.md](iteration-history-20260629.md) | 15 个关注账号、5,554 条正式样本后的历史迭代入口 |
| [iteration-history-20260628.md](iteration-history-20260628.md) | 三账号数据资产化和 V0.6 完成记录 |
| [qwen3-asr-whisper-replacement-evaluation-20260718.md](qwen3-asr-whisper-replacement-evaluation-20260718.md) | Qwen3-ASR 与 Whisper 的冻结替代评测、长音频风险和 Shadow 判定 |
| [qwen3-asr-recovery-full-program-retest-20260718.md](qwen3-asr-recovery-full-program-retest-20260718.md) | Qwen3-ASR 漏段恢复策略的 7506.73 秒完整节目复测和门禁结论 |

## 文档管理规则

- 新需求、模型或重构先映射到 [product-goals.md](product-goals.md) 的 G1/G2/G3；没有目标和指标映射的工作不进入主线优先级。
- 新功能先更新 `current-state.md` 的状态，再按需要更新架构或开发要求。
- 发现可能提升目标指标的新模型或算法时，更新 `model-and-algorithm-radar.md`，并在当次工作结论中主动向用户汇报。
- 新增数据口径时必须写清楚字段含义，尤其是 `raw_rows`、`sample_count`、`unique_count`、入库样本数的区别。
- `output/` 和 `outputs/` 是产物区，不承载最新技术要求；需要沉淀的结论迁移到 `docs/`。
- 已过期但仍有参考价值的内容保留原文件，不直接覆盖历史判断。
- 文档中的命令必须能从项目根目录执行，或明确说明需要进入 `frontend/`。

## 当前关键口径

- 项目同时面向“已切短片直接排名”和“完整节目自动切片后排名”两种入口，标准化候选后必须共用同一排序与反馈链路。
- 项目默认本地优先，公网模型 API 只按需启用并受数据许可、预算、缓存、质量增益和失败降级约束。
- 项目不自动发布、不刷量、不绕过平台规则；目标是提高优质流量、转发和关注的概率，不承诺爆款。
- 历史采集样本已经正式入库，并按同账号、同平台、同视频 ID 全局去重。
- 源文件目录仍会保留原始有效行数，模型和界面学习指标使用去重后的样本数。
- 当前 V0.5 学习评估以历史样本、账号基线、兴趣时钟、原型库和回测为主，参数权重仍处于弱标签阶段。
