# AliyunBailianProvider 专项调研与接入规格

调研日期：2026-07-18

状态：`validate`（Adapter、治理 V2、无密钥 Mock contract 和 ECS 合成文本连通性已验证；业务数据仍 fail closed）

对齐目标：G3 本地/公网模型协同。用户可见结果是明确首个真实 Provider 的接口、模型、费用、安全边界、失败降级和验收门禁；非目标是替换本地模型、修改生产排序、写人工 Gold 或自动发布。

## 1. 结论

`AliyunBailianProvider` 已实现为项目第一个真实协议公网 Adapter，治理、无网络测试和 ECS 合成文本连通性均已验证。2026-07-19 已定位并修复压缩响应被二次解码的问题；修复后正式 Runner 首次尝试即取得有效 JSON、usage 和 Provider request ID。该结果只证明合成文本链路可用，不代表模型质量或真实业务数据已经获准；真实业务调用仍需明确保留政策、预算和数据许可。首版范围如下：

- Provider ID：`aliyun_bailian`。
- 地域：华北 2（北京），服务部署范围为中国内地。
- 接口：业务空间专属域名的 OpenAI 兼容 `POST /compatible-mode/v1/chat/completions`。
- 文本主模型：`qwen3.5-flash-2026-02-23`。
- 文本 Challenger：`qwen3.6-flash-2026-04-16`。
- 代表帧模型：`qwen3-vl-flash-2026-01-22`。
- 调用方式：非流式、非思考、JSON Mode、本地 JSON Schema 校验。
- 上传范围：只允许脱敏结构化摘要和最多 3 张代表帧；禁止完整视频、完整音频和公网媒体 URL。
- 默认状态：关闭；即使成功也只写 Shadow 证据，最终结果仍使用本地基线。

首版应使用固定快照而非滚动别名，确保冻结 benchmark 可复现。固定快照的官方实时价格与滚动别名相同，但价格页没有给这些快照单独标注 Batch 半价或上下文缓存折扣，因此预算必须按实时全价计算，不能提前计入未确认折扣。

## 2. 官方接口与配置

### 2.1 端点和鉴权

华北 2（北京）的推荐 Base URL：

```text
https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

请求地址：

```text
POST {base_url}/chat/completions
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

生产配置建议：

| 配置 | 建议值 | 规则 |
| --- | --- | --- |
| `DSO_BAILIAN_BASE_URL` | 业务空间专属 Base URL | 必须是 HTTPS，主机名必须匹配华北 2 专属域名；禁止任意 URL、IP 和重定向 |
| `DSO_BAILIAN_API_KEY` | 独立子业务空间 API Key | 只从进程环境或权限为 `0600` 的 systemd EnvironmentFile 读取，不回显 |
| `DSO_PUBLIC_MODEL_API_ENABLED` | 默认 `0` | 只有显式开关、数据许可、预算和 Provider 选择全部通过才允许联网 |

官方示例使用 `DASHSCOPE_API_KEY`。本项目建议使用 Provider 专用变量，避免同机其他 DashScope 组件意外复用同一密钥；实现时不应隐式回退到通用变量。

HTTP 客户端建议将 `httpx>=0.27` 从开发依赖提升为运行依赖，并通过注入 `httpx.Client`/`MockTransport` 完成无网络合约测试。设置 `follow_redirects=False`，分别限制 connect/read/write/pool timeout，并限制响应体大小。首轮建议单次尝试 20 秒、最多重试 1 次、整个调用总时限不超过 45 秒。

### 2.2 V1 请求体

```json
{
  "model": "qwen3.5-flash-2026-02-23",
  "messages": [
    {
      "role": "user",
      "content": "<版本化指令、JSON 字样、本地 schema 说明和不可信业务输入>"
    }
  ],
  "stream": false,
  "enable_thinking": false,
  "response_format": {"type": "json_object"},
  "temperature": 0,
  "seed": 1234,
  "n": 1
}
```

必须遵守：

- 提示词必须包含 `JSON` 字样，否则百炼会拒绝 `json_object` 请求。
- `enable_thinking=false` 必须显式发送；Qwen3.5/3.6 默认可能开启思考，非思考模式的结构化输出更稳定、成本也更可控。
- `temperature=0` 和固定 `seed` 用于降低冻结评测的随机性；厂商只承诺尽可能复现，因此仍需保留输出 hash 和多次稳定性检查。
- 不设置 `max_tokens`。百炼官方明确提示，结构化输出设置该参数可能截断 JSON。
- 不发送 `tools`、联网搜索、文件上传、显式 Context Cache、`n>1` 或任意调用方透传参数。
- `parameters` 必须采用本地允许列表；未知字段直接拒绝，避免调用方打开付费工具或改变数据出口。
- 模型返回的 JSON 仍是不可信输入，必须在本地解析并按 `request_type + prompt_version` 对应的冻结 schema 校验。

百炼 OpenAI 兼容 Chat 的 `response_format` 当前只有 `text` 和 `json_object`，不是 Provider 端强制的 JSON Schema。报告或代码中提到“JSON Schema 校验”时，均指本地校验。

## 3. 输入映射

Provider 只负责协议映射，不把业务 taxonomy 固化到厂商 Adapter。具体输出 schema 放在本地版本化 registry 中，由 `request_type` 和 `prompt_version` 选择。

| 本地请求 | 百炼映射 | V1 限制 |
| --- | --- | --- |
| `text_analysis` | 单条 User Message，前半是冻结指令，后半是被明确标记为不可信的脱敏摘要 | UTF-8 文本；禁账号标识、联系方式、未授权逐字稿和任意密钥 |
| `structured_analysis` | 将稳定排序后的结构化摘要序列化到 User Message | 只接受 Adapter 定义的字段，不透传任意 messages |
| `representative_frame_analysis` | 多个 `image_url` Data URI 与文本说明放入同一 User Message | 最多 3 张 JPEG；不得使用公网 URL、OSS URL 或视频文件 |

代表帧建议采用三张独立 `image_url`，而不是 `type=video` 的图片列表：官方的视频图片列表输入至少需要 4 张，而项目首版上限是 3 张。三张帧应在文本中明确标为 hook/middle/payoff 或帧 1/2/3，模型只把它们作为多图输入。

本项目限制比厂商上限更严格：

- 只接收 JPEG，长边不超过 1280 像素。
- 单张原始图片不超过 1 MB，三张合计不超过 3 MB；编码后请求体不超过 5 MB。
- Adapter 在编码前验证真实 MIME、尺寸和文件头，Data URI 固定为 `data:image/jpeg;base64,...`。
- `content_sha256` 必须覆盖脱敏摘要、帧内容、帧顺序、模型、提示词版本和参数；任何一项变化都不得命中旧缓存。
- 不允许 Provider 从 URL 下载媒体，可同时降低数据驻留、SSRF、URL 过期和服务端下载超时风险。

官方允许更大的图片和 Base64 输入，但那是平台能力上限，不是本项目的数据许可。

## 4. 响应解析与本地 contract

HTTP 200 只有同时满足以下条件才算 `ProviderCallStatus.SUCCEEDED`：

1. 响应体是 JSON object，`choices` 恰好包含一个结果。
2. 响应 `model` 与请求的固定快照一致。
3. `finish_reason == "stop"`；`length` 视为截断失败，`tool_calls` 视为协议越界。
4. `choices[0].message.content` 是非空字符串且可解析为 JSON object。
5. 解析后的对象通过本地冻结 JSON Schema、枚举、置信度范围和证据字段校验。
6. 外部文本不包含提示词回显、代码围栏或超出 schema 的自由字段。

应提取的安全元数据：

| 百炼字段 | 本地字段/用途 |
| --- | --- |
| `id` | `provider_request_id`，仅用于排障，不替代本地 `request_id` |
| `model` | 实际模型快照核对 |
| `usage.prompt_tokens` | 实际输入 Token |
| `usage.completion_tokens` | 实际输出 Token |
| `usage.prompt_tokens_details.cached_tokens` | Provider 侧缓存命中 Token，和本地响应缓存分开记录 |
| `usage.prompt_tokens_details.image_tokens/video_tokens/text_tokens` | 模态成本诊断；字段缺失时保留 `unknown`，不填零伪装成功 |
| 原始响应字节数 | 网络与台账诊断，不能用解析后业务 output 的长度替代 |

百炼错误响应可能来自 OpenAI 兼容错误结构或 DashScope 顶层 `code/message/request_id` 结构。Adapter 应兼容读取两种结构，只保存短错误码、HTTP 状态和 Provider request ID；不得把请求正文、提示词、Base64、Authorization 或未经清洗的服务端回显写入日志和台账。

## 5. 错误与重试矩阵

Adapter 对预期的 HTTP/网络失败应返回带完整 metrics 的非成功 `ProviderResult`，不要直接抛异常；否则现有 Runner 无法保留网络请求数、延迟、错误码和可能已发生的费用。只有本地不变量或程序错误才抛异常。

| 情况 | 本地状态 | 自动重试 | 处理 |
| --- | --- | ---: | --- |
| 400 参数、JSON 关键词缺失、媒体格式、内容安全 | `FAILED` | 0 | 记录安全错误码，立即回退本地；不得把内容安全拒绝当空结果 |
| 401 无效密钥 | `DENIED` | 0 | 停止该 Provider，提示检查密钥配置 |
| 403 权限、欠费、模型下线、Workspace 拒绝 | `DENIED` | 0 | 停止该 Provider；不自动从固定快照切滚动别名 |
| 404 模型/端点/Workspace 不存在 | `FAILED` | 0 | 配置错误，回退本地 |
| 429 RPM/TPM/突发保护 | `RATE_LIMITED` | 最多 1 | 尊重有限的 `Retry-After`；否则指数退避加随机抖动，重试后仍失败则本地回退 |
| 500/502/503/504 或连接重置 | `FAILED` | 最多 1 | 在总时限和预算内重试一次 |
| connect/read timeout | `FAILED` | 最多 1 | 不超过总时限；失败后本地回退 |
| 200 但 JSON/schema/finish_reason 失败 | `FAILED` | 最多 1 | 允许一次相同冻结提示词的格式修复重试；仍失败则回退，不让另一个模型静默改写 |

Chat 请求重试不具备账单幂等性：第一次响应丢失仍可能已经计费。因此预算预留必须按 `单次最坏估算 × (1 + max_retries)` 计算，台账记录每次网络尝试和未知账单风险。

## 6. 价格、限流和缓存

### 6.1 华北 2（北京）实时价格

单位为人民币/百万 Token，查询日期 2026-07-18：

| 固定模型 | 输入 Token 档位 | 输入价 | 输出价 |
| --- | --- | ---: | ---: |
| `qwen3.5-flash-2026-02-23` | 0–128K / 128–256K / 256K–1M | 0.2 / 0.8 / 1.2 | 2 / 8 / 12 |
| `qwen3.6-flash-2026-04-16` | 0–256K / 256K–1M | 1.2 / 4.8 | 7.2 / 28.8 |
| `qwen3-vl-flash-2026-01-22` | 0–32K / 32–128K / 128K–256K | 0.15 / 0.3 / 0.6 | 1.5 / 3 / 6 |

费用估算使用整次请求的输入 Token 档位，并分别计算输入和输出。V1 不依赖免费额度、促销、Batch 或 Provider 缓存折扣。调用后用官方 usage 估算费用，再与控制台账单按日校准；偏差超过 5% 停止新请求并调查。

### 6.2 官方账号级限流

固定快照的华北 2 默认额度：

| 模型 | RPM | TPM（输入+输出） |
| --- | ---: | ---: |
| `qwen3.5-flash-2026-02-23` | 600 | 1,000,000 |
| `qwen3.6-flash-2026-04-16` | 600 | 1,000,000 |
| `qwen3-vl-flash-2026-01-22` | 60 | 100,000 |

百炼按阿里云主账号聚合所有 RAM 用户、业务空间和 API Key 的流量，并可能按秒级 RPS/TPS 和突发斜率保护。平台额度不是项目预算。`dso-shadow` 子业务空间仍建议主动降为 `10 QPM / 100,000 TPM`，应用侧并发首轮固定为 1。

### 6.3 缓存策略

- 先使用项目的确定性本地响应缓存；命中后不得发网络请求，也不得占用付费预算。
- V1 不创建百炼显式 Context Cache。该功能有创建费用、5 分钟 TTL 和独立命中语义，不适合首轮固定候选 Shadow。
- 官方说明支持模型的隐式缓存无法关闭，命中 Token 可从 usage 读取；但固定快照未在当前价格表中单独标注缓存折扣，所以首轮不把折扣写入预估收益。
- 本地 `cache_hit` 与 `provider_cached_input_tokens` 必须分字段记录，不能混为一个布尔值。

## 7. 已完成的本地治理修正

`public_model_provider/runner/ledger.v2` 已完成原专项列出的代码门槛：

1. 策略和数据许可通过后先查本地缓存，缓存命中不预留付费预算。
2. `BudgetGuard` 支持并发安全的 `reserve/settle/release/settle_unknown`；预留覆盖允许重试，实际费用超过预留时回退本地并把实际费用计入额度。
3. 台账分开记录 preflight reservation、usage estimate、账单校准位、价表版本和 `billing_status`；未知账单保守按全额预留计入。
4. Runner 使用 Provider 返回的实际输入/输出/缓存 Token 和原始 HTTP 响应字节，不再以请求前估算或解析后 JSON 长度替代。
5. 调用表与逐网络尝试表均记录安全 Provider request ID、HTTP 状态、重试、限流、延迟、usage 和费用。
6. 数据许可用可空 `retention_days` 表达保留期是否已知，并增加 `retention_policy_reference`；台账另落 `retention_days_known`，没有书面保留政策时无法构造可用业务数据运行时。
7. `httpx>=0.27` 已成为运行依赖，Adapter 可注入 `httpx.Client/MockTransport`，生产禁重定向。

仍未由代码解决的是厂商外部事实：公开页面没有固定数据保留天数。获得合同或工单依据前，真实业务数据继续 fail closed；不能用实现完成代替外部授权。

## 8. 无密钥实现与测试矩阵

第一阶段 Adapter 和 mock contract 已完成，且没有启用网络：

- Base URL HTTPS/地域/主机名校验，禁止重定向。
- 密钥缺失、总开关关闭、预算缺失、数据许可缺失时均零网络回退。
- 文本和三代表帧请求的精确 JSON 快照测试。
- 验证 `stream=false`、`enable_thinking=false`、`json_object`、无 `max_tokens/tools/search`。
- 200 合法结果、空 choices、模型不符、`length/tool_calls`、非法 JSON、schema 错误和超大响应。
- 400/401/403/404 零重试；429/5xx/timeout 最多一次重试；检查网络次数、退避、总时限和本地回退。
- 成功、失败、缓存、重试、未知账单风险都写安全台账；断言台账和日志不含 API Key、Authorization、提示词、字幕正文或 Base64。
- 缓存命中发生在预算预留前；预算并发预留、结算、释放和跨日/批次边界测试。
- 现有本地基线、生产排序、人工 Gold、候选边界、导出和发布状态全部不变。

目标测试覆盖上述边界并通过；真实密钥、业务空间和付费账单不在该测试范围内。

无密钥阶段通过后，再由用户完成控制台配置并批准真实调用：

1. 创建独立子业务空间 `dso-shadow`，只授权三个固定模型。
2. 创建自定义 API Key，模型范围只含上述模型，IP 白名单只含 ECS 出口 IP `121.199.170.85/32`。
3. 将密钥写入服务器 systemd EnvironmentFile，文件权限 `0600`；不发到聊天、不写仓库。
4. 先用 3 条合成/脱敏文本做 1 元硬预算连通性 Smoke。
5. 账单、usage、schema、重试和回退全部一致后，才申请 100 条冻结文本 Shadow。
6. 文本门禁通过后才开放三代表帧；完整媒体仍禁止。

## 9. 准入门禁

Adapter 完成不代表模型晋级。首轮仍执行通用 G3 门禁：

- JSON Schema 成功率至少 99%，失败必须显式弃权或本地回退。
- 请求失败率不高于 2%，P50/P95、重试率和 429 分开报告。
- Gold 作答覆盖至少 90%，严重错判率不得高于本地基线。
- 至少一个预注册核心指标获得可复现改善，且账号/节目切分无泄漏。
- 平均单候选费用不高于 0.05 元，usage 估算与控制台账单偏差不高于 5%。
- 本地缓存率、Provider 缓存 Token 和网络请求减少量分开报告。
- 通过后最多从 `validate` 升为 `shadow`；不自动进入生产排序。

## 10. 2026-07-19 ECS 合成 Smoke 与网络错误修复记录

用户明确批准后，在 ECS `121.199.170.85` 通过正式 Provider Runner 执行无业务/个人数据的合成文本连通性请求。所有请求只在单进程内临时打开公网与合成数据许可，持久 EnvironmentFile 中的 `DSO_PUBLIC_MODEL_API_ENABLED` 始终为 `0`。

可复查结果：

- 初始失败：批次 `bailian-synthetic-smoke-20260718T193550Z`、调用 `5181e18d99b74a70895188e8f4c902f3`；Runner 安全回退，1 次尝试记录为旧版笼统 `network_error`，HTTP 状态 0、延迟 `891ms`、响应 0 bytes、无 Provider request ID，账单状态 `unknown`，最坏预留 `0.0002582 CNY`。
- 定位证据：DNS/TLS 正常；鉴权 `/models` 返回 HTTP 200，固定模型在业务空间 229 个可见模型中；无效模型请求可得到 HTTP 404 和 request ID。进一步使用原始响应路径确认百炼返回合法 `Content-Encoding: gzip` 内容。
- 根因：Adapter 使用 `iter_bytes()` 已完成 gzip 解压，却把原 `Content-Encoding` 头复制到新建 `httpx.Response`，使已解压内容被二次解压并抛出 `httpx.DecodingError`。状态 0/响应 0 是本地重建响应失败造成的观测丢失，不是百炼不可达。
- 修复：重建有界响应时移除已失效的 `Content-Encoding`、`Content-Length`、`Transfer-Encoding`；新增 gzip 回归测试和安全传输错误分类；去掉非必要 `n=1`，生产客户端不继承系统代理环境，仍限制最多一次重试。
- 修复后验证：批次 `bailian-gzip-fix-smoke-20260718T200818Z`、调用 `10975faff59240f8ae5a11123177cf52`；首尝试 HTTP 200，延迟 `1003ms`、响应 `668` bytes、输入 `179` Token、输出 `101` Token，Provider request ID 存在，本地冻结 schema 五字段全部通过。
- 费用：修复后预留上限 `0.0005172 CNY`（覆盖至多两次尝试），实际 usage 估算 `0.0002378 CNY`，`billing_status=usage_estimated`；仍需以百炼控制台最终账单校准。失败诊断调用的扣费状态保持 `unknown`。
- 最终运行态：`public_api_enabled=false`、`network_calls_allowed=false`、台账 7 条、`dso-web=active`，`/etc/dso/bailian.env` 为 `0600 root:root`。

该记录验证了失败降级、压缩响应修复以及合成文本的真实 schema/usage 链路，但不是质量 benchmark，也不改变 `validate` 状态。没有上传节目、字幕、图片或其他业务数据，没有修改生产排序、候选边界、人工 Gold、导出或发布。下一步先在百炼控制台校准最终账单；取得可审计的数据许可与保留政策前，不执行真实业务 Shadow。

## 11. 官方资料

- [OpenAI 兼容 Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [千问结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)
- [图像与视频理解](https://help.aliyun.com/zh/model-studio/vision)
- [模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)
- [限流](https://help.aliyun.com/zh/model-studio/rate-limit)
- [错误码](https://help.aliyun.com/zh/model-studio/error-code)
- [上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)
- [地域、部署范围和接入域名](https://help.aliyun.com/zh/model-studio/regions/)
- [API Key 与自定义权限](https://help.aliyun.com/zh/model-studio/get-api-key)
- [业务空间权限管理](https://help.aliyun.com/zh/model-studio/permission-management-overview)
- [合规资质与隐私说明](https://help.aliyun.com/zh/model-studio/privacy-notice)
