# KimiK3Provider 专项调研与接入规格

调研日期：2026-07-18

状态：`watch`（完成官方资料和本地合约核对；未实现 Adapter、未配置密钥、未发送公网请求、未产生费用）

对齐目标：G3 本地/公网模型协同。用户可见结果是明确 Kimi K3 在项目中的合理位置、费用、安全边界、请求映射和验证门禁；非目标是替换本地模型、修改生产排序、写人工 Gold、上传完整节目或自动发布。

## 1. 结论

Kimi K3 值得保留为**少量疑难样本的长上下文/视觉 Challenger**，但不适合作为当前默认公网 Provider，也不应进入全量候选排序：

- 优点：官方提供 1,048,576 Token 上下文、图片/视频理解、自动上下文缓存和严格 JSON Schema Structured Output；结构化输出能力比首版百炼 JSON Mode 更直接。
- 主要成本：K3 始终推理，当前 `reasoning_effort` 只有 `max`；缓存未命中输入为 20 元/百万 Token，输出为 100 元/百万 Token，明显高于当前首选 Qwen Flash。
- 数据门槛：现行《Kimi 开放平台服务协议》包含将输入、输出和反馈用于模型服务优化的免费使用授权，且未给出固定客户数据保留天数。企业条款或书面确认前，只允许合成数据或不可逆脱敏摘要，禁止真实节目媒体、未授权逐字稿、人工 Gold 和账号信息。
- 可复现性：官方模型列表当前只列 `kimi-k3`，未列可固定的 dated snapshot。这意味着冻结 benchmark 还需记录响应模型、请求时间、请求 ID、提示词/schema 版本和输出 hash，并设置模型漂移 canary。
- 产品位置：先完成百炼低成本基线；K3 只处理本地模型和百炼均高不确定、存在语义冲突或确实需要超长上下文的少量候选。1M 上下文本身不是上传完整节目的许可。

因此当前状态继续为 `watch`。只有数据条款和版本漂移策略明确、无密钥 mock contract 通过后，才进入 `validate`；冻结 Shadow 通过后也只允许按不确定性路由，不写生产排序权重。

## 2. 官方接口与能力

### 2.1 端点和鉴权

中国站官方 Base URL：

```text
https://api.moonshot.cn/v1
```

首版请求地址：

```text
POST https://api.moonshot.cn/v1/chat/completions
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

生产配置建议：

| 配置 | 建议值 | 规则 |
| --- | --- | --- |
| `DSO_KIMI_BASE_URL` | `https://api.moonshot.cn/v1` | 固定 HTTPS allowlist；禁止任意 Host、IP、重定向和国际站端点 |
| `DSO_KIMI_API_KEY` | 独立 Shadow Key | 只从进程环境或权限为 `0600` 的 systemd EnvironmentFile 读取，不回显、不落库 |
| `DSO_PUBLIC_MODEL_API_ENABLED` | 默认 `0` | 总开关、Provider 选择、数据许可和预算全部通过才允许联网 |

官方示例使用 `MOONSHOT_API_KEY`。本项目使用 Provider 专用变量，避免与同机其他应用共享密钥。中国站和国际站 Key 相互隔离，端点与 Key 不匹配会返回 401。

HTTP 客户端沿用首个真实 Provider 的安全边界：`follow_redirects=False`，分别限制 connect/read/write/pool timeout，限制请求和响应体，使用可注入 MockTransport 做无网络合约测试。K3 始终推理，首轮建议单次尝试总时限 60 秒、并发 1；任何更长超时必须由冻结 P95 证据支持。

### 2.2 V1 请求体

```json
{
  "model": "kimi-k3",
  "messages": [
    {
      "role": "user",
      "content": "<版本化指令、脱敏业务摘要与输出证据要求>"
    }
  ],
  "stream": false,
  "reasoning_effort": "max",
  "max_completion_tokens": 4096,
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "dso_provider_result_v1",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "verdict": {
            "type": "string",
            "enum": ["agree", "disagree", "abstain"]
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "evidence": {
            "type": "array",
            "items": {"type": "string"}
          }
        },
        "required": ["verdict", "confidence", "evidence"],
        "additionalProperties": false
      }
    }
  }
}
```

实现规则：

- K3 始终启用推理，`reasoning_effort` 当前只支持 `max`。不要发送 K2.x 的 `thinking` 参数。
- 不发送 `temperature`、`top_p`、`n` 和 penalties。K3 对这些参数使用固定取值，通用 Adapter 透传可能直接导致请求错误。
- `max_completion_tokens` 默认 131,072、最大 1,048,576；项目必须显式限制。首轮从 4,096 开始并统计 `finish_reason=length`，不能使用厂商默认值做预算预留。
- 只解析 `choices[0].message.content`；`reasoning_content` 不属于业务输出，不落日志、台账、缓存或数据库。
- 服务器端 `strict=true` 不能替代本地 JSON Schema、枚举、置信度、证据引用和提示词回显校验。外部响应始终是不可信输入。
- V1 不发送 `tools`、`tool_choice`、联网搜索、Partial Mode、文件上传、视频、`prompt_cache_key`、多轮历史或任意调用方透传字段。
- 官方说明联网搜索正在更新，近期不建议使用；项目也不需要模型替代真实平台指标或外部事实源。

## 3. 输入映射

Provider ID 建议固定为 `moonshot_kimi`，模型 ID 单独记录为 `kimi-k3`。Provider 只做协议映射，不在 Adapter 内复制业务分类或排序规则。

| 本地请求 | Kimi 映射 | V1 限制 |
| --- | --- | --- |
| `text_analysis` | 单条 User Message，冻结指令后附不可逆脱敏摘要 | 不含姓名、账号、联系方式、未授权逐字稿、Gold 或密钥 |
| `structured_analysis` | 稳定排序后的候选结构化摘要 | 只接受本地 allowlist 字段，不允许任意 messages |
| `representative_frame_analysis` | `content` 使用对象数组，最多 3 个 Base64 `image_url` 加文本指令 | 只允许 JPEG 代表帧，不允许公网 URL、文件 ID、视频或完整节目 |

官方 Vision 支持 png/jpeg/webp/gif 和多种视频格式，图片数量本身没有上限，但请求 Body 不得超过 100MB；推荐图片不超过 4K、视频不超过 1080p。项目首版采用更严格的数据最小化边界：

- 最多 3 张 JPEG，长边不超过 1280 像素。
- 单张原始图片不超过 1MB，合计不超过 3MB，编码后请求体不超过 5MB。
- 图片真实 MIME、尺寸、文件头和内容 hash 在编码前校验。
- `content` 必须是 `array[object]`，不能把数组序列化为字符串。
- 禁止 URL 图片；官方 K3 Vision 目前只接受 Base64 或 `ms://` 文件 ID。
- 不使用 Moonshot 文件上传和视频理解。文件生命周期、删除和保留政策未确认前，完整媒体不进入 Provider。
- 多模态 Token 动态计算。未来若进入 Shadow，应先调用官方 Token 计算接口做预估，但该预估本身不能绕过本地预算和上传许可。

## 4. 响应、缓存与台账

HTTP 200 只有同时满足以下条件才算成功：

1. 响应体是 JSON object，`choices` 恰好一个结果。
2. 响应 `model` 符合允许的 `kimi-k3` 标识；任何意外模型名进入漂移告警。
3. `finish_reason == "stop"`；`length` 是截断失败，`tool_calls` 是协议越界。
4. `message.content` 是非空字符串，可解析为 JSON object，并通过本地冻结 schema。
5. `reasoning_content` 被丢弃，不参与业务证据、缓存 key 或解释展示。
6. 响应不含提示词回显、代码围栏、未知字段或越界自由文本。

应记录的安全元数据：

| Kimi 字段 | 本地用途 |
| --- | --- |
| `id` | `provider_request_id`，用于排障和账单关联 |
| `model` | 实际模型标识与漂移 canary |
| `created` | Provider 创建时间 |
| `usage.prompt_tokens` | 实际输入 Token |
| `usage.cached_tokens` | Provider 自动缓存命中的输入 Token |
| `usage.completion_tokens` | 实际总输出 Token 结算；官方响应没有单列 `reasoning_tokens` |
| `usage.total_tokens` | usage 一致性诊断 |
| `finish_reason` | 截断、工具越界和正常结束判定 |

项目本地响应缓存仍在预算预留和网络请求之前查询。Kimi 的自动上下文缓存只是输入计费优惠，`usage.cached_tokens` 不能与本地 `cache_hit` 混为一项。V1 不主动设置 `prompt_cache_key`，避免把会话标识或可关联业务 ID 发给厂商。

由于 K3 返回 `reasoning_content`、但 usage 不单列推理 Token，台账必须按 `completion_tokens` 整体进行输出费用结算，不能只按最终 JSON 长度估价，也不能把未暴露的推理成本写成零。

## 5. 错误与重试

| 情况 | 本地状态 | 自动重试 | 处理 |
| --- | --- | ---: | --- |
| 400 参数、上下文、内容安全、schema 请求错误 | `FAILED` | 0 | 立即本地回退；内容安全拒绝不能伪装为空结果 |
| 401 Key/站点不匹配 | `DENIED` | 0 | 停止 Provider，检查中国站端点与密钥 |
| 403 权限/IP 白名单 | `DENIED` | 0 | 停止 Provider，不切换其他端点 |
| 404 模型不存在或账号无权限 | `FAILED` | 0 | 配置/版本问题，触发模型漂移告警 |
| 429 `rate_limit_reached_error` / `engine_overloaded_error` | `RATE_LIMITED` | 最多 1 | 尊重有限 `Retry-After`，在总时限和重试预算内退避一次 |
| 429 `exceeded_current_quota_error` | `DENIED` | 0 | 欠费或额度不足，停止新请求，不重试烧预算 |
| 500/503 或连接/读取超时 | `FAILED` | 最多 1 | 总时限和重试预算允许时重试一次，否则本地回退 |
| 499 客户端取消 | `FAILED` | 0 | 记录取消和潜在未知账单，不由后台继续请求 |
| 200 但模型/schema/finish_reason 不合格 | `FAILED` | 0 | 初始 K3 Shadow 不做格式修复重试；直接回退并保留失败证据 |

网络重试不具备账单幂等性，响应丢失仍可能已计费。预算预留应按允许的最大网络尝试数计算，每次网络尝试独立写台账；无法确定 usage 时标记 `billing_status=unknown` 并保守占用预算。

## 6. 价格、限流与预算

### 6.1 官方按量价格

单位为人民币/百万 Token，查询日期 2026-07-18：

| 模型 | 缓存命中输入 | 缓存未命中输入 | 输出 | 上下文 |
| --- | ---: | ---: | ---: | ---: |
| `kimi-k3` | 2.00 | 20.00 | 100.00 | 1,048,576 |

假设 100 个候选，每条输入 3,000 Token、总 completion 500 Token：

- 输入全部未命中：`0.3M × 20 + 0.05M × 100 = 11.00 元`。
- 输入全部命中：`0.3M × 2 + 0.05M × 100 = 5.60 元`。
- 同口径 Qwen3.5-Flash 约 0.16 元；K3 未命中约为其 69 倍。该比较只说明成本量级，不代表质量相同。

500 completion Token 对始终推理的 K3 可能偏乐观。若 V1 设置 `max_completion_tokens=4096`，单次尝试的最坏输出预留即 `4096 / 1M × 100 = 0.4096 元`；再加 3,000 个未命中输入约 0.06 元，单次最坏约 0.4696 元。若允许一次网络重试，单请求预留接近 0.94 元。因此通用 Provider 当前 0.10 元单请求预算不能直接套用于 K3。

K3 初始验证建议使用独立预算：

- 并发：1。
- 单请求硬预留：1 元，包含最多一次网络重试。
- 3 条合成连通性 Smoke：批次 3 元。
- 10 条疑难样本 Shadow：批次/单日 10 元。
- 不按 100 条全量运行；只有 10 条门禁证明了相对百炼的新增纠错价值后，才单独申请扩大预算。

### 6.2 官方账号级限流

Moonshot 按累计充值等级限制并发、RPM、TPM 和 TPD：

| 等级 | 累计充值 | 并发 | RPM | TPM | TPD |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tier0 | 0 元 | 1 | 3 | 500,000 | 1,500,000 |
| Tier1 | 50 元 | 50 | 200 | 2,000,000 | 不限 |

更高等级额度不等于应用侧许可或预算。项目首轮固定并发 1，并继续使用单请求、单批次和单日预算；平台可能随集群负载临时调整限流，429 必须正常回退本地。

## 7. 数据、隐私与复现门槛

现行官方协议同时包含两类需要一起理解的表述：

- 客户数据原则上由用户控制，平台按用户指示、法律、产品规则或故障排查需要处理和存储。
- 用户授予平台免费的使用权，可将输入、输出和反馈用于模型服务优化。

因此不能把“客户数据受控”解释成“业务输入绝不用于优化”。项目真实业务数据外发前至少需要 Moonshot 企业合同或书面工单明确：

1. 输入、输出和反馈是否可排除模型训练/服务优化。
2. 数据和文件的固定保留期限、删除机制、备份清除周期和处理地域。
3. 子处理者、访问审计、加密和安全事件通知。
4. 是否支持独立组织、模型/IP 白名单和用途隔离。

当前 `ProviderDataPermissionRecord` 要求可审计的保留期；公开协议只说按法律或服务需要存储，没有固定天数，不能伪填 `0`。确认前仅允许本地生成的合成文本做连通性 Smoke；真实候选摘要也继续 fail closed。

官方模型列表当前没有 `kimi-k3-YYYY-MM-DD` 快照。该判断来自公开列表未提供固定版本，不等于厂商承诺模型一定会静默更新。为了降低漂移风险，后续 Shadow 必须：

- 冻结请求体、schema、提示词、输入 hash、模型标识、API 文档日期和价表版本。
- 保存响应 `id/model/created`、usage、输出 hash 和本地解析版本，但不保存 raw reasoning。
- 每批前运行不含业务数据的固定 canary；schema、usage 分布或答案 hash 显著变化则暂停新业务请求。
- 不把 K3 与具有 dated snapshot 的百炼结果合并成不可区分的同一 benchmark 版本。

## 8. 对现有 Provider contract 的影响

百炼专项已经发现的缓存顺序、预算结算、实际 usage、Provider request ID、未知账单和运行依赖缺口，Kimi 同样必须先修正。K3 还增加四项 Adapter 特有要求：

1. **缓存分价**：费用结算必须用 `cached_tokens` 将输入拆成命中和未命中两档，不能全部按估计输入 Token 结算。
2. **始终推理**：预算按 `completion_tokens` 整体结算；忽略 `reasoning_content` 不能等同于忽略其 Token 成本。
3. **参数 allowlist**：不能复用百炼的 `temperature=0`、`seed`、`enable_thinking=false` 或 `max_tokens` 行为；Provider 必须拥有独立请求映射。
4. **模型漂移**：没有 dated snapshot 时，Runner/评测报告需增加模型响应标识、canary 和 `provider_model_revision_known=false` 语义。

这些差异说明厂商无关 contract 应统一业务输入、输出、预算和回退语义，但不能用一个“OpenAI 兼容请求字典”直接透传给所有厂商。

## 9. 分阶段验证

### 阶段 A：无密钥 contract

- 实现 `MoonshotKimiProvider` 或 `KimiK3Provider` 的 MockTransport 请求/响应测试，不访问公网。
- 精确验证 K3 请求不包含百炼专用参数、工具、搜索、文件、URL 媒体或任意透传字段。
- 覆盖 strict JSON Schema、非法 JSON、`reasoning_content` 丢弃、`length`、模型漂移和错误重试矩阵。
- 完成缓存前置、预算 reserve/settle/release、缓存分价、实际 usage、未知账单和 request ID 台账。
- 日志/台账断言不含 Key、Authorization、prompt、摘要、Base64 或 reasoning。

### 阶段 B：合成连通性 Smoke

前提是用户完成账户、充值、独立 Key 和 3 元预算，并明确同意合成数据调用：

- 3 条本地生成的无业务内容文本；不发真实素材。
- 验证 structured output、本地 schema、usage、缓存命中、费用估算、429/失败回退和控制台账单。
- 任何 usage 缺失、价格偏差超过 5%、意外工具/模型或日志泄漏均停止。

### 阶段 C：10 条疑难样本 Shadow

仅在企业数据条款满足后，选取 10 条不可逆脱敏、已有人工结论且本地/百炼高分歧的固定样本：

- 与本地基线、Qwen3.5-Flash 和 Qwen3.6-Flash 做成对比较。
- 核心指标是严重错判减少、正确解决的高不确定样本比例、schema 成功率、P50/P95、实际 completion Token 和单个新增正确样本成本。
- 预注册门槛：schema 成功率 100%，失败/截断显式回退；严重错判不增加；至少正确解决 2 条百炼与本地均未解决的困难样本；单条平均实付不高于 0.50 元。
- 门槛通过也只从 `watch` 进入 `shadow`，并限制到疑难路由。未通过则保留研究报告，不扩大样本和预算。

## 10. 官方资料

- [Kimi K3 快速开始](https://platform.kimi.com/docs/guide/kimi-k3-quickstart)
- [模型列表](https://platform.kimi.com/docs/models)
- [Kimi K3 定价](https://platform.kimi.com/docs/pricing/chat-k3)
- [充值与限速](https://platform.kimi.com/docs/pricing/limits)
- [Chat Completions API](https://platform.kimi.com/docs/api/chat)
- [Structured Output](https://platform.kimi.com/docs/guide/response_format)
- [视觉输入](https://platform.kimi.com/docs/guide/use-kimi-vision-model)
- [常见错误码](https://platform.kimi.com/docs/api/errors)
- [Kimi 开放平台服务协议](https://platform.kimi.com/docs/agreement/modeluse)

以上能力、价格、限流和协议均按 2026-07-18 官方公开页面核对。真实实施前应重新核价并复核最新协议；本报告中的质量收益和门禁是待验证假设，不是已证实效果。
