# 项目文档入口

本目录是 Douyin Slice Optimizer 的长期文档入口。本文只负责导航和文档治理，不替代各专项文档。

实验产物、采集批次和一次性输出保留在 `output/`、`outputs/`，但不作为当前事实或工程规范。仓库内开发 Agent 还必须遵守根目录 [AGENTS.md](../AGENTS.md)。

## 目录层级

```text
docs/
├── README.md                         # 唯一总入口
├── product-goals.md                  # 核心目标与长期边界
├── development-requirements.md       # 工程与验收规范
├── current-state.md                  # 当前真实状态
├── architecture.md                   # 主系统架构
├── model-and-algorithm-radar.md       # 模型与算法准入
├── user-manual.md                    # 用户与运维手册
├── architecture/                     # 专项架构设计
├── guides/                           # 专项操作与数据规范
├── design/
│   └── frontend/                     # 前端流程、状态和实施设计
├── research/
│   ├── providers/                    # 公网模型 Provider 调研
│   ├── evaluations/                  # 冻结 benchmark 与模型评测
│   └── strategy/                     # 产品、算法与论文研究
└── history/                          # 阶段计划、评审和迭代复盘
```

根目录只保留必须优先阅读的核心事实源；专项资料按用途下钻，日期文档统一视为冻结记录或阶段材料。

## 文档有效性与冲突处理

当文档之间出现冲突时，按以下顺序判断：

1. 用户当前明确要求和更高层指令。
2. 根目录 [AGENTS.md](../AGENTS.md) 的项目执行契约。
3. [product-goals.md](product-goals.md) 与 [development-requirements.md](development-requirements.md) 的长期目标和工程规范。
4. [current-state.md](current-state.md) 的当前真实完成度、服务状态和已知缺口。
5. [architecture.md](architecture.md)、[model-scheduling-architecture.md](architecture/model-scheduling-architecture.md) 等维护中的专项设计。
6. [model-and-algorithm-radar.md](model-and-algorithm-radar.md) 的模型与算法准入状态。
7. [user-manual.md](user-manual.md) 的当前用户操作流程。
8. 带日期的计划、评测、研究和历史记录，仅代表形成文档时的证据与判断。

文档状态说明：

- **规范**：长期约束，发生对应规则变化时必须同步更新。
- **当前**：当前事实快照，优先用于判断“现在已经有什么”。
- **维护中**：仍在演进的架构、流程或操作说明。
- **验证记录**：冻结的调研、benchmark 或模型评测，不原地改写结论。
- **历史**：阶段计划或复盘，用于追溯，不代表当前承诺。

## 按角色快速阅读

| 角色/任务 | 建议入口 |
| --- | --- |
| 产品规划与进度判断 | [product-goals.md](product-goals.md) → [current-state.md](current-state.md) |
| 开发、测试与接口变更 | [development-requirements.md](development-requirements.md) → [architecture.md](architecture.md) |
| 安装、部署和平台使用 | [user-manual.md](user-manual.md) |
| 模型、算法和 Provider 评估 | [model-and-algorithm-radar.md](model-and-algorithm-radar.md) → 本页“模型与 Provider 研究” |
| 前端流程和界面改造 | [frontend-design-proposal-20260718.md](design/frontend/frontend-design-proposal-20260718.md) → 本页“前端设计工作集” |
| 抖音数据采集 | [douyin-collection-standard.md](guides/douyin-collection-standard.md) → [douyin-media-collection-flow.md](guides/douyin-media-collection-flow.md) |

## 核心维护文档（规范与当前事实）

这组文档构成项目的主要事实来源。产品、工程、架构或操作发生实质变化时，应优先同步对应文档。

| 状态 | 文档 | 作用 | 主要更新时机 |
| --- | --- | --- | --- |
| 规范 | [product-goals.md](product-goals.md) | G1/G2/G3、成功指标、成本门和长期边界 | 产品目标、优先级或指标口径变化 |
| 规范 | [development-requirements.md](development-requirements.md) | 工程、测试、数据、API、前端和 Definition of Done | 工程约束或验收规则变化 |
| 当前 | [current-state.md](current-state.md) | 当前完成度、服务状态、验证结果、缺口和下一步 | 能力、数据、服务或重大界面状态变化 |
| 维护中 | [architecture.md](architecture.md) | 模块边界、统一候选链路、数据模型和 API contract | 模块、表结构或接口发生实质变化 |
| 维护中 | [model-and-algorithm-radar.md](model-and-algorithm-radar.md) | 新模型/算法的证据、成本、风险和准入状态 | 新发现、benchmark 或准入状态变化 |
| 维护中 | [user-manual.md](user-manual.md) | 安装、Web、CLI、数据回流、部署与排障 | 用户流程、命令或运行环境变化 |

## 专项规范与运行设计

| 状态 | 文档 | 作用 |
| --- | --- | --- |
| 维护中 | [model-scheduling-architecture.md](architecture/model-scheduling-architecture.md) | 单 GPU 任务队列、lease、模型亲和、恢复、API 和迁移设计 |
| 维护中 | [douyin-collection-standard.md](guides/douyin-collection-standard.md) | 抖音数据采集合规、字段、质量门和去重标准 |
| 维护中 | [douyin-media-collection-flow.md](guides/douyin-media-collection-flow.md) | 抖音切片媒体采集流程、CLI、目录结构和质量门 |

## 前端设计工作集

以下文档是 2026-07-18 至 2026-07-19 形成的配套设计与落地资料。它们共同描述目标体验和实施顺序，但属于带日期的设计参考；若与核心规范、当前状态、架构或用户手册冲突，以前述事实来源为准。

建议按以下顺序使用：

1. [frontend-design-proposal-20260718.md](design/frontend/frontend-design-proposal-20260718.md)：前端信息架构和任务流改造基线。
2. [功能逻辑与界面业务流程梳理-20260719.md](design/frontend/功能逻辑与界面业务流程梳理-20260719.md)：业务对象、页面职责和主流程对齐。
3. [页面跳转与状态流转图-20260719.md](design/frontend/页面跳转与状态流转图-20260719.md)：页面导航与对象状态流转。
4. [状态系统定义-20260719.md](design/frontend/状态系统定义-20260719.md)：状态命名、层级、颜色和使用约束。
5. [组件替换清单-20260719.md](design/frontend/组件替换清单-20260719.md)：组件级改造范围与替换顺序。
6. [前端落地改造优先级清单-20260719.md](design/frontend/前端落地改造优先级清单-20260719.md)：分阶段实施与验收优先级。

## 模型与 Provider 研究

这组文档用于 G3 选型和准入，不直接代表生产默认配置。最终状态以模型雷达和当前状态为准。

| 状态 | 文档 | 作用 |
| --- | --- | --- |
| 验证记录 | [public-model-provider-research-20260718.md](research/providers/public-model-provider-research-20260718.md) | 大陆公网 Provider 的官方资料、成本、数据条款和 Shadow 方案 |
| 验证记录 | [aliyun-bailian-provider-research-20260718.md](research/providers/aliyun-bailian-provider-research-20260718.md) | 百炼请求映射、错误重试、费用、权限和验收矩阵 |
| 验证记录 | [kimi-k3-provider-research-20260718.md](research/providers/kimi-k3-provider-research-20260718.md) | Kimi K3 接口、结构化输出、成本、数据条款和门禁建议 |

## 冻结 benchmark 与模型评测

以下文件保留对应日期的数据、代码、模型和判定背景。产生新结果时应新增版本或新文件，并同步模型雷达，不覆盖旧结论。

| 状态 | 文档 | 作用 |
| --- | --- | --- |
| 验证记录 | [cross-entry-benchmark-20260718.md](research/evaluations/cross-entry-benchmark-20260718.md) | G1/G2 跨入口冻结快照、排序 contract 和参考回测 |
| 验证记录 | [qwen3-asr-whisper-replacement-evaluation-20260718.md](research/evaluations/qwen3-asr-whisper-replacement-evaluation-20260718.md) | Qwen3-ASR 与 Whisper 的冻结替代评测 |
| 验证记录 | [qwen3-asr-recovery-full-program-retest-20260718.md](research/evaluations/qwen3-asr-recovery-full-program-retest-20260718.md) | 漏段恢复策略的完整节目复测和门禁结论 |
| 验证记录 | [qwen3-asr-subtitled-program-evaluation-20260718.md](research/evaluations/qwen3-asr-subtitled-program-evaluation-20260718.md) | 带烧录字幕节目的对照、消融和跨节目汇总 |
| 验证记录 | [bailian-cached-signal-ablation-20260719.md](research/evaluations/bailian-cached-signal-ablation-20260719.md) | D12-A 缓存信号归因、低权重融合和跨账号扩量门禁 |
| 验证记录 | [bailian-independent-holdout-validation-20260719.md](research/evaluations/bailian-independent-holdout-validation-20260719.md) | D12-B 20 对独立留出、盲预测冻结、费用与泛化门禁 |
| 验证记录 | [bailian-holdout-failure-attribution-20260719.md](research/evaluations/bailian-holdout-failure-attribution-20260719.md) | D12-C0 缓存-only 失败归因、决策弹性、参考池与视觉输入诊断 |
| 验证记录 | [bailian-evidence-quality-reconstruction-20260719.md](research/evaluations/bailian-evidence-quality-reconstruction-20260719.md) | D12-C1 三窗口证据包、分层高低互动参考池、覆盖上限与缓存负结果 |
| 验证记录 | [qwen37-full-clip-diagnostic-staging-20260719.md](research/evaluations/qwen37-full-clip-diagnostic-staging-20260719.md) | 10 条完整短片的 ECS 暂存、SHA/FFprobe 验证与后续完整视频输入边界 |
| 验证记录 | [qwen37-complete-short-clip-shadow-20260719.md](research/evaluations/qwen37-complete-short-clip-shadow-20260719.md) | Qwen3.7 单条完整短片的时序分析、实际 Token/延迟/费用与研究门禁 |
| 验证记录 | [qwen35-omni-complete-short-clip-shadow-20260719.md](research/evaluations/qwen35-omni-complete-short-clip-shadow-20260719.md) | Qwen3.5-Omni 同样本完整音视频的流式分析、模态分项费用与 Qwen3.7 成本对照 |
| 验证记录 | [qwen35-omni-complete-clip-batch-20260719.md](research/evaluations/qwen35-omni-complete-clip-batch-20260719.md) | 10 条完整音视频直传的 schema 稳定性、语义证据、传播排序负结果、费用与门禁 |
| 验证记录 | [qwen35-omni-propagation-features-20260719.md](research/evaluations/qwen35-omni-propagation-features-20260719.md) | 完整音视频事实特征 schema v2、平台结果缺失语义、10 条配对诊断与 60 条下一门禁 |
| 验证记录 | [qwen35-omni-propagation-account-holdout-20260720.md](research/evaluations/qwen35-omni-propagation-account-holdout-20260720.md) | 30 对/60 条、8 账号的完整音视频事实特征账号隔离对照、受限恢复、费用与 `keep_v2_4` 结论 |

## 产品、算法与论文参考

| 状态 | 文档 | 作用 |
| --- | --- | --- |
| 维护中 | [music-variety-strategy.md](research/strategy/music-variety-strategy.md) | 音乐综艺切片策略、评分维度和标题封面策略 |
| 参考 | [algorithm-study.md](research/strategy/algorithm-study.md) | 推荐系统与算法学习笔记 |
| 参考 | [research.md](research/strategy/research.md) | 早期研究调研和论文到系统的映射 |
| 参考 | [paper-architecture-review.md](research/strategy/paper-architecture-review.md) | 主线论文复核后的架构建议 |

## 历史计划与评审

以下内容用于追溯阶段判断，不作为当前完成度、排期或接口承诺。若与核心文档冲突，以核心文档为准。

| 状态 | 文档 | 作用 |
| --- | --- | --- |
| 历史 | [platform-v0.3-plan.md](history/platform-v0.3-plan.md) | V0.3 阶段计划 |
| 历史 | [agent-architecture-plan.md](history/agent-architecture-plan.md) | 多 Agent 架构诊断和迭代计划 |
| 历史 | [agent-next-iteration-plan.md](history/agent-next-iteration-plan.md) | 阶段接口约束与实现清单 |
| 历史 | [agent-product-review.md](history/agent-product-review.md) | 阶段产品评审记录 |
| 历史 | [agent-next-product-review.md](history/agent-next-product-review.md) | 下一轮产品评审记录 |
| 历史 | [douyin-visible-collection-flow.md](history/douyin-visible-collection-flow.md) | 可见数据采集流程记录 |
| 历史 | [iteration-history-20260629.md](history/iteration-history-20260629.md) | 2026-06-29 样本资产和排序闭环阶段入口 |
| 历史 | [iteration-history-20260628.md](history/iteration-history-20260628.md) | 2026-06-28 迭代复盘和后续计划 |

## 文档管理规则

- 每项工作先映射到 G1、G2 或 G3，并明确用户可感知结果、指标和非目标。
- 稳定、持续维护的规范使用不带日期的文件名；冻结调研、评测和阶段设计使用日期后缀。
- 新增 `docs/**/*.md` 时必须放入对应分类目录，并在本页归类、说明状态，避免形成无法发现的孤立文档。
- 当前能力变化先更新 [current-state.md](current-state.md)，再按影响同步架构、开发要求、模型雷达或用户手册。
- 新模型、算法或 Prompt 必须登记到 [model-and-algorithm-radar.md](model-and-algorithm-radar.md)，写清证据、成本、风险、最小验证和准入状态。
- 历史计划和冻结评测保留原始结论；需要修正时新增勘误、版本或后续文档，不静默改写历史。
- `output/`、`outputs/` 只承载产物；可复用结论应沉淀到 `docs/`，但不得把产物目录当成事实来源。
- 文档命令必须能从仓库根目录执行，或明确标注工作目录；文档链接使用仓库内相对路径。
- `docs/` 根目录只保留本页列出的核心事实源；专项架构、指南、设计、研究和历史材料分别进入既有分类目录，不再新增根目录散落文件。

## 当前统一口径

- 项目同时支持“已切短片直接排名”和“完整节目自动切片后排名”，标准化候选后共用排序、解释、审核和反馈链路。
- 默认本地模型优先；公网模型只有在配置 Provider、密钥、预算和数据许可后才启用，并必须具备缓存、费用记录和失败降级。
- 项目不自动发布、不刷量、不绕过平台规则；目标是提高优质流量、转发和关注的概率，不承诺爆款。
- 当前完成度、服务状态、测试结果和下一步优先级以 [current-state.md](current-state.md) 为准。
