# G3 真实公网模型 Provider 调研

调研日期：2026-07-18

状态：`validate`（只完成官方资料核对和接入设计，未配置密钥、未调用真实 API、未产生费用）

## 1. 结论

首个真实 Provider 建议选择**阿里云百炼（中国内地，华北 2 北京）**，先接文本与代表帧，不上传完整节目：

- 文本主模型：`qwen3.5-flash-2026-02-23`。
- 文本质量对照：`qwen3.6-flash-2026-04-16`。
- 代表帧视觉模型：`qwen3-vl-flash-2026-01-22`。
- 第二 Provider 对照：优先评估 `kimi-k2.6`；`kimi-k3` 只保留为极少量疑难样本 Challenger，不进入全量入口。

选择百炼的主要原因不是“同属阿里云”这一点本身，而是它同时满足当前项目最重要的五项约束：OpenAI 兼容接口、国内地域、低成本固定版本、API Key 模型/IP 白名单和业务空间限流，以及官方明确说明调用数据不会用于模型训练。现有 ECS 位于杭州，访问北京地域也不需要改变当前部署架构。

Kimi K2.6 的 256K 上下文和原生图片/视频输入适合做长上下文或视频理解对照，但单价更高，且开放平台协议包含将输入输出用于服务优化的授权表述；在未获得更严格的企业数据条款前，不上传完整节目或未脱敏素材。Kimi K3 虽提供 1M 上下文和 strict JSON Schema，但始终使用最高推理强度、输出单价为 100 元/百万 Token，官方模型列表又未提供 dated snapshot，因此只适合单独预算的少量困难样本，详见 [Kimi K3 专项调研](kimi-k3-provider-research-20260718.md)。

火山方舟具备完整图片、视频、音频和 Responses API 能力，但官方发布的数据授权规则包含用于模型优化的较宽授权范围，需要先确认当前账号是否参与或可退出相关数据授权。腾讯混元旧平台正在迁移到 TokenHub，且公告计划停服旧平台，因此不作为当前首接。

## 2. 官方资料对比

价格均为调研日官方公开的按量标准价格，单位为人民币/百万 Token；促销、免费额度和控制台最终报价可能变化。冻结 benchmark 必须记录日期、地域、模型快照和实际账单。

| Provider | 推荐模型或接口 | 公开价格与上下文 | 数据与权限 | 本项目判断 |
| --- | --- | --- | --- | --- |
| 阿里云百炼 | `qwen3.5-flash-2026-02-23` | 0–128K：输入 0.2、输出 2；128–256K：0.8/8；256K–1M：1.2/12。固定快照按实时全价预算；不预先计入滚动别名标注的 Batch/缓存折扣 | 中国内地北京地域；可创建子业务空间，限制模型、QPM/TPM，并给 API Key 配置模型范围和 IP 白名单；官方称调用数据不用于训练，但会按法律和服务协议要求存储调用数据 | **首选，进入 `validate`** |
| 阿里云百炼 | `qwen3-vl-flash-2026-01-22` | 0–32K：0.15/1.5；32–128K：0.3/3；128–256K：0.6/6；固定快照先按实时全价预算 | OpenAI 兼容 Chat 支持 Qwen-VL；先上传 3 张代表帧和结构化摘要，不传完整视频 | **首个视觉 Shadow** |
| 阿里云百炼 | `qwen3.6-flash-2026-04-16` | 0–256K：1.2/7.2；256K–1M：4.8/28.8 | 与主模型共用治理边界，但必须独立记录模型版本与费用 | **质量 Challenger，不做全量默认** |
| Moonshot Kimi | `kimi-k2.6` | 缓存命中输入 1.1、未命中输入 6.5、输出 27；256K 上下文 | OpenAI 兼容，支持文本/图片/视频、JSON Mode、自动上下文缓存；Batch 为标准价 60% | **第二 Provider 对照，保持 `watch`** |
| Moonshot Kimi | `kimi-k3` | 缓存命中输入 2、未命中输入 20、输出 100；1M 上下文 | 支持视觉和 JSON Schema，但始终推理且费用明显更高 | 只用于少量超长上下文困难样本，不进入首轮 |
| 火山方舟 | Doubao Seed 系列 / Responses API | 官方价格按模型和单请求输入长度动态分档；模型迭代快，实施时必须在控制台锁定 dated model 和价表 | OpenAI SDK/Responses API、图片/视频/音频、结构化输出、私网接入均可用；数据授权需单独确认 | 能力强，但**数据条款确认前不上传业务媒体** |
| 腾讯混元 | `hunyuan-a13b`、Vision/Video | a13b 0.5/2；Vision 和 Video 3/9 | OpenAI 兼容旧接口默认 5 并发；旧平台正在迁移 TokenHub，API Host 与模型将变化 | 当前不接，待 TokenHub 稳定后重新调研 |

官方依据：

- [阿里云百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)
- [阿里云百炼 OpenAI 兼容 Chat](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [阿里云百炼地域和业务空间专属域名](https://help.aliyun.com/zh/model-studio/regions/)
- [阿里云百炼 API Key 权限](https://help.aliyun.com/zh/model-studio/get-api-key)
- [阿里云百炼合规与隐私说明](https://help.aliyun.com/zh/model-studio/privacy-notice)
- [Kimi 模型列表](https://platform.kimi.com/docs/models)
- [Kimi K2.6 价格](https://platform.kimi.com/docs/pricing/chat-k26)
- [Kimi K3 价格](https://platform.kimi.com/docs/pricing/chat-k3)
- [Kimi Batch 价格](https://platform.kimi.com/docs/pricing/batch)
- [Kimi 开放平台服务协议](https://platform.kimi.com/docs/agreement/modeluse)
- [火山方舟模型价格](https://www.volcengine.com/docs/82379/1544106)
- [火山方舟私网访问](https://www.volcengine.com/docs/82379/1339360)
- [腾讯混元价格](https://cloud.tencent.com/document/product/1729/97731)
- [腾讯混元旧平台迁移公告](https://cloud.tencent.com/document/product/1729/131925)

## 3. 首个 Adapter 的最小范围

Provider ID 建议固定为 `aliyun_bailian`，不把“Qwen”写成厂商 ID，避免以后同一百炼 Adapter 调用第三方直供模型时混淆来源。V1 只支持 `POST /chat/completions` 的非流式结构化分析：

```text
本地候选/Gold
  -> 脱敏结构化摘要或最多 3 张代表帧
  -> PublicModelPolicy（默认关闭、许可、上传等级）
  -> Cache / BudgetGuard
  -> AliyunBailianProvider（超时 20 秒，最多重试 1 次）
  -> 本地 JSON Schema 校验（百炼侧仅启用 JSON Mode）
  -> 独立调用台账与 Shadow 对比
  -> 最终结果仍为本地基线
```

首版明确不做：完整视频上传、完整音频上传、自动调用 Omni、流式输出、Agent 工具调用、生产排序加权、自动写人工 Gold、自动导出或发布。

## 4. 阿里云控制台安全配置

1. 在华北 2（北京）建立独立子业务空间，例如 `dso-shadow`，不要使用默认业务空间。
2. 只授权 `qwen3.5-flash-2026-02-23`、`qwen3.6-flash-2026-04-16` 和 `qwen3-vl-flash-2026-01-22`。
3. API Key 使用自定义权限，只允许 ECS 出口 IP，并配置为环境变量；密钥不写仓库、数据库、报告或前端。
4. 初始业务空间限流建议 `10 QPM / 100,000 TPM`；应用内继续保留单请求、单批次和单日预算，平台限流不能代替应用预算。
5. 优先使用业务空间专属 OpenAI 兼容 Base URL；开发期可使用 `https://dashscope.aliyuncs.com/compatible-mode/v1`，但生产应切到专属域名。
6. 只允许 `structured_summary` 和 `representative_frames` 上传等级；人物姓名、账号标识、联系方式和未授权字幕先脱敏。

## 5. 费用估算

以下只是用于设置硬预算的情景估算，不代表真实账单。假设 100 个候选，每个文本请求平均输入 3,000 Token、输出 500 Token：

| 模型 | 100 条估算 | 说明 |
| --- | ---: | --- |
| Qwen3.5-Flash | 约 0.16 元 | `0.3M×0.2 + 0.05M×2`，未计算缓存和 Batch 折扣 |
| Qwen3.6-Flash | 约 0.72 元 | `0.3M×1.2 + 0.05M×7.2` |
| Kimi K2.6 | 约 3.30 元 | 全部缓存未命中：`0.3M×6.5 + 0.05M×27`；全部命中约 1.68 元 |
| Kimi K3 | 约 11.00 元 | 全部缓存未命中：`0.3M×20 + 0.05M×100`；全部命中约 5.60 元 |

视觉情景：100 个候选、每条最多 3 张代表帧，若模型计量后平均输入 8,000 Token、输出 500 Token，则 Qwen3-VL-Flash 约 `0.8M×0.15 + 0.05M×1.5 = 0.195` 元。图片 Token 会随分辨率和模型计量变化，因此上线前必须用官方 usage 字段和账单校准，不以该估算替代台账。

建议硬预算：

- 单请求：0.10 元。
- 首轮文本批次：5 元。
- 首轮代表帧批次：10 元。
- 单日：20 元。
- 任何费用字段缺失、币种不符、预算耗尽或账单偏差超过 20%，立即停止新请求并回退本地结果。

## 6. 冻结 Shadow 验证

首轮建议 100 条，不做随机“看起来不错”的案例展示：

- 40 条本地模型高不确定或多模型分歧候选。
- 30 条已有 Material/Window Gold 的严重混淆样本。
- 30 条按账号、节目、素材形态分层的固定对照样本。

预注册门禁建议：

- JSON Schema 成功率不低于 99%。
- 请求失败率不高于 2%，P95 不高于 20 秒。
- Gold 作答覆盖不低于 90%，严重错判率不得高于本地基线。
- 至少一个核心指标获得可复现改善：分类准确率提升 5 个百分点，或高不确定样本中至少 15% 被正确解决且严重错误不增加。
- 单候选平均费用不高于 0.05 元；台账费用与控制台账单偏差不高于 5%。

以上门禁是待验证假设，不是已经证明的收益。通过后也只把 Adapter 状态升级为 `shadow`；生产排序权重、人工 Gold 和发布行为仍需独立批准。

## 7. 实施顺序

1. ~~实现 `AliyunBailianProvider` 和 mock HTTP contract 测试，不配置真实密钥。~~ 已完成：固定 Host/模型/请求/schema、治理 V2 和 MockTransport 目标测试已通过，仍未配置真实密钥。
2. 用户在百炼创建子业务空间、模型白名单、IP 白名单、限流和 API Key。
3. 用 3 条脱敏文本执行付费连通性 Smoke，预算上限 1 元。
4. 冻结 100 条文本 Shadow，比较 Qwen3.5-Flash、Qwen3.6-Flash 和本地基线。
5. 文本门禁通过后，再开放最多 3 张代表帧给 Qwen3-VL-Flash。
6. 百炼结果稳定后，才用同一冻结集接 `kimi-k2.6` 做第二 Provider 对照；K3 必须单独满足数据条款、版本漂移和 1 元单请求预算门禁，只验证 10 条困难样本。

Adapter 的请求/响应映射、错误重试和费用结算见 [AliyunBailianProvider 专项调研](aliyun-bailian-provider-research-20260718.md)。缓存前置、预算结算、实际 usage、Provider request ID、逐尝试台账和保留政策引用语义已经实现；厂商公开页仍未给固定数据保留天数，因此真实业务数据调用继续 fail closed。

Kimi K3 的独立参数映射、strict JSON Schema、自动缓存分价、始终推理成本、现行数据授权和 10 条疑难样本门禁见 [KimiK3Provider 专项调研](kimi-k3-provider-research-20260718.md)。
