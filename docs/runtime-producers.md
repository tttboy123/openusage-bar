# Runtime Producer Kit 快速接入

Runtime Producer Kit 把本机模型调用产生的少量、结构化事实写入 OpenUsage
Bar 的 Runtime Observation 账本。它是可选组件：不启用 Producer 时，菜单栏、
Provider Center 和现有用量采集仍可独立工作。

当前首个 Producer 支持 LiteLLM Python SDK 与 LiteLLM Proxy。它只在一次调用
结束后读取 Provider、模型、时间、Token 计数和可选估算费用，并通过标准输入调用
OpenUsage Collector。它不会监听网络端口，也不会改变模型请求结果。

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

## 6. OpenTelemetry 状态

WQ-22 没有开放通用 OTLP 写入口。LiteLLM 的 OpenTelemetry 集成可能携带内容或
身份元数据；即使配置 `NO_CONTENT=True`，后续仍需版本锁定、字段白名单和恶意
Fixture 验证。因此 OTLP GenAI 与 CLIProxyAPI 会作为独立的 WQ-23 Adapter
交付，当前不要把任意 OTLP payload 直接写入 OpenUsage Bar。

## 7. 停用与清理

停用时，从 SDK 的 `litellm.callbacks` 或 Proxy 的 `litellm_settings.callbacks`
删除 OpenUsage Logger 即可，不影响 OpenUsage Bar 的其他功能。

卸载应用不会自动删除用户数据。如需同时清理 Runtime 观测历史，请先退出相关
LiteLLM 进程，再删除：

```text
~/.local/state/openusage-bar/runtime.sqlite3
```

这不会删除 Provider Keychain 凭证、活动账本或 OpenUsage 的历史数据。
