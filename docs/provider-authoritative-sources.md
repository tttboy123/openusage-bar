# GLM、Kimi 与 Qwen 权威数据源核验

本页记录 OpenUsage Bar 对第二批 Provider 的官方只读数据源核验结果。
核验日期为 2026-07-29。结论只描述公开、可复用的接口，不把控制台网页、
单次推理返回值或搜索别名冒充成历史用量与订阅额度。

## 结论摘要

| Provider | 每日 Token | API 费用 | 余额 | 订阅额度与重置 | 当前决策 |
|---|---|---|---|---|---|
| Z.AI / GLM | OpenUsage 可提供本地活动；官方仅公开单次请求 `usage` | 官方控制台次日更新，未公开历史查询 API | 未公开只读 API | 未公开只读 API | 保留 OpenUsage；不开发网页爬虫 |
| Moonshot / Kimi API | 官方控制台可查看，但 OpenAPI 未公开历史查询 API | 官方控制台可查看，但 OpenAPI 未公开历史查询 API | 中国站、国际站均有官方只读 API | Kimi API 是按量计费，不是订阅额度 | 下一步开发官方余额 Adapter |
| Alibaba Cloud / Qwen | OpenUsage 可提供本地活动；官方监控可通过 Prometheus 查询 | 费用中心 BSS OpenAPI 可查询账单 | 费用中心可查询账户余额 | 普通 DashScope Key 不提供订阅额度 | 默认保留 OpenUsage；高权限连接器后置 |

`Unknown` 仍然是 `Unknown`。余额、费用、Token 和订阅额度是不同事实，
不能互相换算，也不能把缺失数据写成零。

## Z.AI / GLM

### 官方能力

- 国际站通用端点是 `https://api.z.ai/api/paas/v4`，Coding Plan 使用独立的
  `https://api.z.ai/api/coding/paas/v4`。认证方式是 Bearer API Key。
- 官方 [Chat Completion API](https://docs.z.ai/api-reference/llm/chat-completion)
  在单次调用结束时返回输入、输出、缓存输入和总 Token。这是单次响应事实，
  不是可回查的每日历史。
- 官方 [OpenAPI 文档](https://docs.z.ai/openapi.json) 当前没有余额、账单历史、
  每日用量或 Coding Plan 剩余额度端点。
- 国际站 [FAQ](https://docs.z.ai/help/faq) 说明账单明细按日延迟更新，速率限制
  需要登录控制台查看；没有发布可供本地客户端轮询的历史账单 API。
- 中国站公开 [GLM Coding Plan 页面](https://open.bigmodel.cn/glm-coding) 说明
  5 小时和每周限制，并提供登录后的用量页，但未发布对应的只读 API。

### 认证、区域与限制

| 项目 | 结论 |
|---|---|
| 认证 | Bearer API Key；凭证只能进入 Keychain |
| 区域 | 中国站与国际站端点分离；在官方证明可互换前按区域绑定连接 |
| 分页 | 没有公开历史用量端点，因此无可实现的历史分页协议 |
| 速率限制 | 账号控制台展示；官方未发布固定公共数值 |
| 隐私 | 不能读取或保存 Prompt、响应正文、`user_id`、请求 ID 或账号身份 |

### OpenUsage Bar 决策

1. 继续复用 OpenUsage 已发布的 Z.AI 每日模型活动。
2. 单次 API 响应只能由实际发起请求的客户端记录，OpenUsage Bar 不做代理，
   因此不会为了统计而发送模型请求。
3. 余额、账单历史、5 小时额度、每周额度与重置时间保持不支持。
4. 不读取登录 Cookie，不调用未公开网页接口，不维护 DOM 或 Network 爬虫。

## Moonshot / Kimi API

### 官方能力

- 中国站提供
  [`GET https://api.moonshot.cn/v1/users/me/balance`](https://platform.kimi.com/docs/api/balance)，
  返回人民币可用余额、代金券余额和现金余额。
- 国际站提供
  [`GET https://api.moonshot.ai/v1/users/me/balance`](https://platform.kimi.ai/docs/api/balance)，
  返回美元可用余额、代金券余额和现金余额。
- 两个站点的 API Key 完全隔离，混用端点会返回 401。
- 官方 [API 概览](https://platform.kimi.ai/docs/api/overview) 只公开余额查询、
  推理、模型、Token 预估和文件等端点。中国站和国际站 OpenAPI 都没有每日
  Token、每日费用或历史账单查询端点。
- 官方 [Balance & usage](https://www.kimi.com/help/kimi-api/api-balance-and-usage)
  说明每日用量、模型拆分和费用在控制台查看，并在次日更新；这不是公开 API。
- Kimi API 开放平台是按量计费产品，不等同于 Kimi 会员或 Kimi Code 订阅。

### 认证、区域与限制

| 项目 | 结论 |
|---|---|
| 认证 | `Authorization: Bearer $MOONSHOT_API_KEY` |
| 区域 | 中国站 `.cn` 与国际站 `.ai` 必须分别建连接、分别保存 Key |
| 分页 | 余额响应是单对象，无分页 |
| 速率限制 | 余额端点未发布固定频率；遇到 429 时遵循 `Retry-After` 并退避 |
| 隐私 | 只解析三个余额数字；不保存原始响应、Key、组织身份或账单页面内容 |

### OpenUsage Bar 决策

1. 新增官方 Moonshot Balance Adapter，支持中国站和国际站多账号。
2. UI/API 显示带币种的 `available`、`voucher`、`cash`，不转换为剩余百分比。
3. 不生成重置时间，不把余额叫作订阅额度，不用余额推导 Token。
4. 官方余额失败时保留 Last-good 并标记 Stale；从未成功时显示 No data。
5. 每日 Token 与费用在官方发布历史 API 前保持不支持；允许用户接入 Custom
   Daily Feed，但不能与未来官方数据相加。

## Alibaba Cloud / Qwen

### 官方能力

- Model Studio API Key 按区域和工作空间管理。官方
  [API Key 文档](https://help.aliyun.com/en/model-studio/get-api-key) 明确列出
  北京、新加坡、东京、法兰克福和弗吉尼亚等区域，并要求保护 Key。
- 单次 Qwen 推理响应可返回 input、output、cached input 和 total Token。
- 官方 [Model monitoring](https://help.aliyun.com/en/model-studio/model-telemetry)
  提供最近 30 天的历史 Token；基础监控约有一小时延迟。
- 开启高级监控后，可以通过私有 Prometheus HTTP API 查询 `model_usage`，
  并按模型、工作空间和时间范围筛选。该接口使用 Alibaba Cloud
  AccessKey/AccessKeySecret，不是普通 DashScope API Key。
- 官方 [Billing Management API](https://help.aliyun.com/en/user-center/developer-reference/api-overview-1)
  提供账户余额、账单和计量记录查询。
  [`QueryBillOverview`](https://help.aliyun.com/en/user-center/developer-reference/api-bssopenapi-2017-12-14-querybilloverview)
  需要 RAM 的 `bss:DescribeBillList` 权限并按账期查询。

### 认证、区域与限制

| 项目 | 结论 |
|---|---|
| 认证 | 推理用 DashScope API Key；监控/账单用 RAM AccessKey 对 |
| 区域 | API Key、模型、价格和端点按区域隔离 |
| 分页 | Prometheus 使用时间范围查询；账单 API 按各自分页/账期契约读取 |
| 速率限制 | 监控和费用中心分别受云产品 API 限制；不能假设 DashScope 限制相同 |
| 隐私 | AccessKey 权限面明显更大；不得采集推理日志、Prompt 或响应正文 |

### OpenUsage Bar 决策

1. 默认继续复用 OpenUsage 的 Alibaba Cloud 每日模型活动。
2. 不要求普通用户提供高权限 RAM AccessKey，也不自动开启可能产生费用的高级监控。
3. 需要官方历史 Token 的团队可先通过只读 Prometheus 地址接入 Custom Daily
   Feed；内置连接器必须等最小权限策略、费用和多区域行为完成真实账号验证。
4. 费用中心账单是 Alibaba Cloud 账户级事实。未证明能稳定隔离 Model Studio
   产品和工作空间前，不把账户账单归入 Qwen。
5. Coding Plan、Token Plan 或资源包剩余量没有被普通 DashScope Key 证明，
   因此保持不支持。

## 后续实现顺序

1. **Moonshot Balance Adapter**：接口稳定、只读、低权限，优先实现。
2. **GLM**：等待官方公开余额或订阅用量 API；当前仅保留 OpenUsage Token 活动。
3. **Qwen**：先设计可选的 Prometheus/费用中心最小权限连接，再进行真实账号验证。
4. 三者都必须遵守 Provider Conformance Kit：Keychain、固定 HTTPS 端点、区域锁定、
   有界响应、错误脱敏、Last-good、Unknown 非零，以及 UI/API/CLI 同一事实。
