# WQ-23 Runtime Producer Adapter 实施计划

> 状态：仓库实现与本地发行门禁已完成。本文冻结 WQ-23 的上游版本、隐私边界、
> 输入契约和验收顺序；不代表真实账号流量、外部 Canary、合并、推送或发布已经
> 完成。

## 目标

在不开放通用遥测写入口的前提下，为两类本地运行时增加可复用 Producer：

1. OpenTelemetry GenAI：在模型进程内把终态 Span 缩减为严格白名单事实，再通过
   stdin 写入现有 OpenUsage Collector。
2. CLIProxyAPI：通过官方 `usage.Plugin` 接口只读取规范化 Token Accounting，
   直接把严格 `runtime-observation/v1` 写入 Collector。

两条链路都不得写入 Prompt、Response、消息、工具参数、凭证、Header、异常正文、
用户身份、原始 Trace/Span/Request ID 或任意原始 Payload。Collector 仍是唯一的
数据库写入者；Adapter 失败不能影响模型请求。

## 冻结的上游契约

### OpenTelemetry

- OTLP protobuf / JSON：`open-telemetry/opentelemetry-proto v1.11.0`
  (`790608c4d51e6ffc12210b541e8514cbed9e91a4`)。
- GenAI semantic conventions：`open-telemetry/semantic-conventions-genai`
  commit `f77b9235f2ad49fe95b61e9809ca82bb08ef9d47`。
- 只读取以下 Span attribute：
  - `gen_ai.provider.name`
  - `gen_ai.request.model`
  - `gen_ai.response.model`
  - `gen_ai.operation.name`
  - `gen_ai.usage.input_tokens`
  - `gen_ai.usage.output_tokens`
  - `gen_ai.usage.cache_read.input_tokens`
  - `gen_ai.usage.cache_creation.input_tokens`
  - `gen_ai.usage.reasoning.output_tokens`
- `input_tokens` 包含缓存读写；`output_tokens` 包含 reasoning。因此写入
  `input_includes_cache`，总量只能是 `input + output`。
- 不启动 OTLP HTTP/gRPC Receiver，不接收或转发 OTLP/JSON 原始请求。Producer
  作为 OpenTelemetry `SpanExporter` 在进程内获得只读 Span，并在序列化前缩减。

### CLIProxyAPI

- CLIProxyAPI：`v7.2.113`
  (`bc71c77f5cc42f3fbe1bf040cf14d4f166894835`)。
- `usage.TokenAccountingSchemaVersion = 2`。
- 只使用 `Record.Provider`、`Record.Model`、`Record.RequestedAt`、
  `Record.Latency`、`Record.TTFT`、`Record.Failed` 和
  `Record.Detail.TokenBreakdown`。
- 只接受 `TokenBreakdown.Valid()` 且 `quality=complete`、
  `unclassified_tokens=0` 的记录；不完整或不一致的计数被丢弃，绝不伪造为零。
- 不使用 `GET /v0/management/usage-queue`：该接口会弹出队列记录，读取具有副作用。
  Producer 必须注册官方 `usage.Plugin`。

## 固定架构

```text
OTel SDK ReadableSpan ── allowlist exporter ─┐
                                            ├─ stdin ─ OpenUsage Collector ─ runtime.sqlite3
CLIProxyAPI usage.Record ─ allowlist plugin ─┘
```

- 匿名 `scopeRef` 由用户在本机随机生成并显式配置，禁止从邮箱、账号或 API Key
  派生。
- Provider 必须显式映射；未知 Provider 被忽略，不归入 `unknown` 或 `other`。
- OTel 的原始 trace/span ID 只允许在内存中参与单向散列生成本地 observation ID，
  不进入输出；CLIProxyAPI 使用进程随机盐和单调序号生成本地 ID。
- 单批最多 256 条、编码后最多 1 MiB；Collector 子进程三秒超时、最小环境、
  stdout/stderr 丢弃。
- TTFT 缺失或为零表示 Unknown；时间、计数或字段口径不自洽时丢弃整条记录。

## 执行顺序

### 任务 1：冻结 Fixture 与失败测试

- 添加只含公开模型和虚构计数的 OTel Span Fixture。
- Fixture 同时携带消息、工具参数、用户 ID 和原始 IDs，测试输出中不得出现。
- 添加 CLIProxyAPI Token Accounting v2 Fixture，并包含 API Key、Auth ID、失败
  正文和 Header 哨兵；插件输出中不得出现。
- 先证明缺失用量、重复/越界计数、未知 Provider、非匿名 Scope、错误版本和
  不完整 accounting 全部 fail closed。

### 任务 2：实现 OpenTelemetry GenAI Exporter

- 新增独立标准库 Producer；OpenTelemetry SDK 仅为可选导入。
- 支持 `export(spans)`，只处理终态 GenAI Span；不保存事件、链接、Resource、
  InstrumentationScope 或任意未列入白名单的属性。
- 同步批量调用一次 Collector；失败返回 OTel exporter failure（存在 SDK 时）或
  `False`，不抛出到模型请求。

### 任务 3：实现 CLIProxyAPI usage.Plugin

- 新增可独立测试的 Go module，固定 CLIProxyAPI `v7.2.113`。
- `HandleUsage` 只构造 `runtime-observation/v1`，不序列化 `usage.Record`。
- Collector 调用使用绝对路径、stdin、三秒 context、最小环境和空输出。
- 注册方式由宿主显式调用，不修改 CLIProxyAPI 凭证或管理接口。

### 任务 4：打包、文档与本地门禁

- App bundle 只打包 OTel Python Producer；CLIProxyAPI Go plugin 作为源码模块和
  接入说明发布，不在用户机器运行时编译。
- 扩展 `docs/runtime-producers.md`，清楚标明支持矩阵和停用方式。
- 增加打包后 OTel Producer 冒烟，并确保只读 Integrations 目录不产生
  `__pycache__`，冒烟后重新验证 bundle seal。
- 执行定向测试、Go race test、完整 Python/Swift 门禁、覆盖率、依赖审计、
  密钥/隐私扫描、构建和最终 codesign 验证。

## 明确不在本轮

- 通用 OTLP Receiver、HTTP 写 API、任意自定义属性透传。
- Loom 预留、准入、路由或 Scheduler 决策（属于 WQ-24，且在 Loom 仓库实施）。
- 真实 Provider 账号流量和外部 30 天 Canary。
- 合并、推送、发布、签名身份或凭证变更。

## 完成记录（2026-08-01）

- OpenTelemetry GenAI 已实现为进程内、可选 SDK 依赖的 `SpanExporter`，固定
  `semantic-conventions-genai` commit `f77b9235...`，只接受显式 Provider
  映射和白名单 Token 属性。它不开放 HTTP/gRPC/OTLP Receiver，也不保存原始
  Span、事件、链接、Resource、Trace ID、Span ID 或内容型属性。
- CLIProxyAPI 已实现为独立 Go module，固定 `v7.2.113` 与 Token Accounting
  schema v2，通过官方 `usage.Plugin` 接口接入。预编译 CLIProxyAPI 不能动态
  加载 Go module，使用者需要在自有宿主中注册或重新构建；本轮没有修改其凭证、
  管理接口或运行实例。
- 两条 Producer 都只通过绝对路径和 stdin 调用 Collector，使用三秒超时、最小
  环境、1 MiB/256 条上限，失败不影响模型请求。未知 Provider、缺失计数、错误
  schema、非 complete 或不自洽数据全部丢弃，不写成零或 `unknown`。
- App bundle 只包含 LiteLLM 与 OpenTelemetry 两个 Python Producer，
  `Integrations` 目录为只读。构建中发现上一次只读目录会阻止下一次清理，现已在
  删除旧构建前只恢复该目录所有者写权限；连续完整构建已验证可重复。
- 新鲜验证结果：Python 985 项通过；OpenTelemetry 定向 7 项通过且模块行覆盖率
  95%；CLIProxyAPI `go test -race`、`go vet` 通过，语句覆盖率 90.0%；Swift
  257 项、21 个 suite 通过，完整构建记录的产品行覆盖率为 87.64%。依赖审计无
  已知漏洞，Git tree/history 密钥扫描为 0，生产适配器与最终发行包隐私扫描为
  0，三个打包后 Runtime 冒烟均成功，最终 App 深度签名验证通过。
- 两个攻击性 Fixture 故意包含虚构 Prompt、Response、API Key、Cookie、邮箱、
  原始 ID 与失败正文，因此原始 Fixture 必须被通用隐私扫描拒绝；Python/Go
  契约测试逐值证明这些哨兵不会进入 Collector 文档。Fixture 不进入 App bundle。
- Gatekeeper 分发评估仍为 `rejected`：当前仅使用 ad-hoc 本地签名，没有 Apple
  Developer ID 公证。这符合本轮“不变更签名身份”的边界，但不能宣称公开下载后
  可直接通过 Gatekeeper。

仍待外部证据：真实 OpenTelemetry/CLIProxyAPI 流量、外部 Canary、Developer ID
公证（如未来选择）、合并、推送和发布。
