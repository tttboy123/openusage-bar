# Runtime Summary Local API 前置切片

> 状态：已完成仓库内实现与本地发行门禁。该切片是 WQ-24 Loom X1
> observe-only 的生产者侧前置条件；
> 不实现 Loom 调度、预留、准入或路由。

## 目标

在现有只读、用户私有的 Unix Socket Local API 中增加：

```text
GET /v1/runtime/summary?windowSeconds=3600
```

它公开现有短保留 Runtime Ledger 的有界汇总，使 Loom 和其他调度消费者不必读取
SQLite、调用 Collector 子进程或解析 UI。OpenUsage Bar 仍可独立安装和运行。

## 契约

- `windowSeconds` 默认 `3600`，只接受 `60...86400` 的十进制整数。
- 顶层继续使用 Local API v1 envelope：`schemaVersion="1.0"`、
  `dataRevision`、`generatedAt`。
- Runtime 事实位于 `runtime` 字段，保留独立的数字
  `schemaVersion=1`、`runtimeRevision`、UTC window、coverage、Token、状态、
  费用、延迟和最多 512 个 Provider/Model/匿名 scope 分组。
- `dataRevision` 与 `runtimeRevision` 是两个独立高水位；不宣称跨两个 SQLite
  账本存在同一事务。
- Runtime 数据库缺失、不安全、损坏、版本不兼容或读取失败时返回经过清洗的
  `503 runtime_unavailable`，不得返回零值摘要，也不得回退到旧数据。
- 响应不包含 Prompt、Response、消息、工具参数、凭证、Header、异常正文、
  原始请求 ID、Source ID 或直接身份。
- 读取使用 SQLite `mode=ro` 与 `query_only`，不创建数据库、不迁移、不 chmod、
  不生成 WAL/SHM，不改变文件内容或 mtime。
- 现有 GET/HEAD、ETag、单请求连接、线程/时间上限和 TCP 鉴权边界保持不变。

## 文件边界

- `openusage_bar/runtime_store.py`：增加严格只读打开与共享 wire 编码。
- `openusage_bar/collector_cli.py`：复用共享 wire，并把 daemon 的 Runtime 路径交给 API。
- `openusage_bar/local_api.py`：增加只读路由、参数校验和 503 错误映射。
- `scripts/generate_local_api_schema.py` 与生成 Schema：增加机器契约。
- `tests/test_runtime_store.py`、`tests/test_local_api.py`、
  `tests/test_local_api_schema.py`：RED/GREEN 与隐私/非写入证明。
- `docs/api/local-api-v1.md`、总工作队列：公开契约和验证记录。

## 验收

1. 冻结 Runtime Fixture 可经真实 UDS API 读取，CLI 与 API 的嵌套 Runtime
   内容完全一致。
2. 缺失/损坏/符号链接数据库和非法 window 全部 fail closed，且不泄漏路径或细节。
3. 只读读取前后数据库 bytes、mtime、权限一致，且没有旁路文件。
4. JSON Schema、路由清单、HEAD/ETag 与 N-1 additive 兼容门禁通过。
5. 完整 Python、Swift、隐私、依赖、打包和签名门禁通过后，才允许 Loom X1
   消费此端点。

## 明确排除

- Runtime 写 HTTP API、通用 OTLP Receiver、远端绑定。
- Loom SessionBinding、Scheduler、ProviderRouter、Quota Guard 或 Policy Routing。
- 把缺失 Runtime 数据表达为 `0`。
- 合并、推送、发布、安装或凭证变更。

## 验证记录

- 新增路由、真实 UDS、CLI/API 同源编码、HEAD/ETag、Schema、
  非写入与错误降级定向测试全部通过。
- Python 完整测试在构建门禁内两轮各 `991` 项通过；
  `runtime_store` 产品行覆盖率 `96%`，`local_api` 为 `90%`，
  所有 Python 产品模块不低于 `80%`。
- Swift `257` 项 / `21` 个套件通过，产品行覆盖率 `87.65%`。
- CLIProxyAPI Go race/vet、依赖审计、生产与发行隐私扫描、Git
  tree/history 密钥扫描全部通过，已知漏洞与扫描命中均为 `0`。
- 应用包中 LiteLLM、OpenTelemetry GenAI 与 Runtime Collector 冒烟通过，
  `codesign --verify --deep --strict` 通过。
- 本切片未安装、未发布、未推送，也未修改凭证。
