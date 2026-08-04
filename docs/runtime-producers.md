# Runtime Producer Kit 快速接入

Runtime Producer Kit 把本机模型调用产生的少量、结构化事实写入 OpenUsage
Bar 的 Runtime Observation 账本。它是可选组件：不启用 Producer 时，菜单栏、
Provider Center 和现有用量采集仍可独立工作。

当前 Producer 支持 LiteLLM Python SDK / Proxy、OpenTelemetry Python SDK 的
GenAI Span，以及 CLIProxyAPI 的官方 `usage.Plugin`。它们只在一次调用结束后读取
Provider、模型、时间、Token 计数和可选估算费用，并通过标准输入调用 OpenUsage
Collector。它们不会监听网络端口，也不会改变模型请求结果。

## 1. 生成匿名 Scope

安装 OpenUsage Bar 后执行一次：

```bash
python3 "/Applications/OpenUsage Bar.app/Contents/Resources/Integrations/litellm_openusage.py" \
  --create-scope-ref
```

输出形如 `anon_0123456789abcdef0123456789abcdef`。将它保存在本机配置中，
用于区分账号或路由。不要用邮箱、用户名、组织 ID 或 API Key 生成 Scope；不同
账号应使用不同的随机值。

应用内路径：

```text
Producer: /Applications/OpenUsage Bar.app/Contents/Resources/Integrations/litellm_openusage.py
OTel Adapter: /Applications/OpenUsage Bar.app/Contents/Resources/Integrations/otel_genai_openusage.py
Collector: /Applications/OpenUsage Bar.app/Contents/MacOS/OpenUsage Collector
Runtime DB: ~/.local/state/openusage-bar/runtime.sqlite3
```

## 2. LiteLLM Python SDK

在应用启动时注册一个 Logger。下面的示例直接加载应用内已签名的 Producer，不会
复制或修改应用文件：

```python
import importlib.util
from pathlib import Path

import litellm

APP = Path("/Applications/OpenUsage Bar.app")
PRODUCER = APP / "Contents/Resources/Integrations/litellm_openusage.py"
COLLECTOR = APP / "Contents/MacOS/OpenUsage Collector"
STATE = Path.home() / ".local/state/openusage-bar"
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("openusage_litellm", PRODUCER)
if spec is None or spec.loader is None:
    raise RuntimeError("OpenUsage Runtime Producer unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

openusage_logger = module.OpenUsageRuntimeLogger(
    collector_path=COLLECTOR,
    database_path=STATE / "runtime.sqlite3",
    scope_ref="anon_将这里替换为生成值",
    provider_map={
        "openai": "openai",
        "anthropic": "anthropic",
        "openrouter": "openrouter",
        "xai": "xai",
        "deepseek": "deepseek",
        "moonshot": "moonshot",
        "minimax": "minimax",
        "zai": "zai",
        "dashscope": "alibaba_cloud",
    },
)
existing_callbacks = list(getattr(litellm, "callbacks", None) or [])
litellm.callbacks = [*existing_callbacks, openusage_logger]
```

`provider_map` 的左侧必须与 LiteLLM 实际给出的 `custom_llm_provider` 一致，
右侧是 OpenUsage Bar 的公开 Provider ID。不在显式映射中的 Provider 会被忽略，
不会归入 `unknown` 或 `other`。

## 3. LiteLLM Proxy

LiteLLM Proxy 的官方注册方式是 `python_filename.logger_instance_name`。在
`proxy_config.yaml` 同目录创建 `openusage_callback.py`，内容沿用上一节的模块
加载代码，并将最后两行改为：

```python
proxy_handler_instance = module.OpenUsageRuntimeLogger(
    collector_path=COLLECTOR,
    database_path=STATE / "runtime.sqlite3",
    scope_ref="anon_将这里替换为生成值",
    provider_map={"openai": "openai", "anthropic": "anthropic"},
)
```

然后在 `proxy_config.yaml` 注册：

```yaml
litellm_settings:
  callbacks: openusage_callback.proxy_handler_instance
  turn_off_message_logging: true
```

`turn_off_message_logging` 是 LiteLLM 自身其他日志链路的隐私设置；OpenUsage
Producer 本身无论该值如何都不会持久化消息内容。若 Proxy 运行在容器中，容器
必须能只读访问 Producer 和 Collector，并能写入所选 Runtime DB；宿主机应用路径
不会自动出现在容器里。

## 4. 验证数据

完成一次 LiteLLM 调用后查询最近一小时：

```bash
"/Applications/OpenUsage Bar.app/Contents/MacOS/OpenUsage Collector" \
  runtime-summary --window-seconds 3600
```

成功时会返回严格 JSON，包含 `observationCount`、Token 汇总、Provider/模型分组、
延迟覆盖率和按币种费用。费用来自 LiteLLM 的 `response_cost` 时会标记为
`estimated`，不是厂商账单。

没有完整 Token 计数时，Producer 会丢弃该事件，不会用 `0` 伪装成真实用量。
首 Token 时间不可得时保留为 Unknown，并从 TTFT 样本数中排除。Collector 异常、
超时或退出失败只会导致本次遥测未写入，不会使模型请求失败。

## 5. 数据边界与保留策略

写入的字段仅包括：

- 匿名 Scope、公开 Provider ID、公开模型 ID；
- 开始、首 Token（可空）、结束时间；
- 输入、输出、缓存读取、缓存写入、推理和总 Token；
- 调用状态、可选估算费用、来源与质量。

不会写入或输出：

- Prompt、Response、消息、工具参数或工具结果；
- API Key、Cookie、Session、请求头或环境中的 Provider 凭证；
- 异常正文、原始 LiteLLM payload、用户/邮箱/组织身份；
- 原始调用 ID 或原始上游响应 ID。

原始调用 ID 只在内存中单向散列为本地去重 ID。Runtime DB 权限为 `0600`，默认
保留窗口为 24 小时，并受 100,000 条记录和 64 MiB 上限约束。

## 6. OpenTelemetry GenAI Span

应用内的 OTel Adapter 实现 OpenTelemetry Python SDK `SpanExporter`。它没有
HTTP/gRPC 端口，也不会接收 OTLP/JSON 原始 Payload；Span 仍在产生它的进程内，
Adapter 在序列化前只摘取固定的 GenAI 字段。

兼容契约固定为：

- `opentelemetry-proto v1.11.0`；
- `semantic-conventions-genai` commit `f77b9235f2ad49fe95b61e9809ca82bb08ef9d47`；
- OpenUsage Bar `runtime-observation/v1`。

在现有 OTel 初始化代码中注册：

```python
import importlib.util
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

APP = Path("/Applications/OpenUsage Bar.app")
ADAPTER = APP / "Contents/Resources/Integrations/otel_genai_openusage.py"
COLLECTOR = APP / "Contents/MacOS/OpenUsage Collector"
STATE = Path.home() / ".local/state/openusage-bar"
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("openusage_otel_genai", ADAPTER)
if spec is None or spec.loader is None:
    raise RuntimeError("OpenUsage OTel Adapter unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(
    module.OpenUsageGenAISpanExporter(
        collector_path=COLLECTOR,
        database_path=STATE / "runtime.sqlite3",
        scope_ref="anon_将这里替换为生成值",
        provider_map={
            "openai": "openai",
            "anthropic": "anthropic",
            "gcp.vertex_ai": "google",
            "aws.bedrock": "aws_bedrock",
        },
    )
))
trace.set_tracer_provider(provider)
```

若应用已经创建 `TracerProvider`，应把 Processor 加到已有 Provider，不要创建第二
个。Adapter 只读取 `gen_ai.provider.name`、请求/响应模型、operation 和输入、输出、
缓存读写、推理 Token；Resource、Event、Link、消息、工具参数、用户/Session ID
和其他属性不会进入 Collector。其他 OTel Exporter 的隐私策略不受它控制。

OTel 约定中缓存 Token 已包含在输入总量、推理 Token 已包含在输出总量，因此总量
严格按 `input + output` 计算；缺失或矛盾的计数会丢弃整条 Span，不补零。

## 7. CLIProxyAPI

源码仓库中的 `integrations/cliproxyapi-openusage` 是独立 Go module，固定兼容
CLIProxyAPI `v7.2.113` 与 Token Accounting Schema v2。它实现官方
`usage.Plugin`，不读取会弹出记录的 `/v0/management/usage-queue`。

接入代码和本地 `replace` 示例见：

```text
integrations/cliproxyapi-openusage/README.md
```

插件只接受 `TokenBreakdown.Valid()`、`quality=complete`、
`unclassified_tokens=0` 且总量大于零的记录。API Key、Auth ID、Header、失败正文、
Alias 和 Source 即使存在于上游 `usage.Record`，也不会被读取或序列化。

CLIProxyAPI 的预编译程序不会动态加载任意 Go module，因此当前需要在自定义宿主
中注册后重新编译。这是源码集成，不代表 OpenUsage Bar 会修改用户已有的
CLIProxyAPI 二进制、配置或凭证。

## 8. 支持矩阵

| Producer | 交付形态 | Token 口径 | 费用 | 直接内容边界 |
|---|---|---|---|---|
| LiteLLM | Python callback | 完整终态 usage | 可选估算 | callback 内白名单 |
| OpenTelemetry GenAI | Python SpanExporter | input/output 含子集 | 不支持 | Span 序列化前白名单 |
| CLIProxyAPI | Go `usage.Plugin` | Accounting v2 complete | 不支持 | 从不序列化完整 Record |

三者都只是 Runtime Observation Producer，不拥有订阅额度事实，也不执行 Loom 的
预留、准入或路由决策。

## 9. 停用与清理

停用时，从 SDK 的 `litellm.callbacks` 或 Proxy 的 `litellm_settings.callbacks`
删除 OpenUsage Logger；停止在 OTel 启动代码中注册对应 Span Processor 并重启；
或在自定义 CLIProxyAPI 宿主中不再注册 `openusage-bar` Plugin。停用任一
Producer 都不影响 OpenUsage Bar 的其他功能。

卸载应用不会自动删除用户数据。如需同时清理 Runtime 观测历史，请先退出相关
LiteLLM 进程，再删除：

```text
~/.local/state/openusage-bar/runtime.sqlite3
```

这不会删除 Provider Keychain 凭证、活动账本或 OpenUsage 的历史数据。
