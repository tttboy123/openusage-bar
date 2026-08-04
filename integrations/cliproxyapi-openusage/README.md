# CLIProxyAPI → OpenUsage Bar Runtime Plugin

这个独立 Go module 实现 CLIProxyAPI 官方的 `usage.Plugin`，把一次请求结束后的
公开 Provider/模型、时间和 Token Accounting v2 写入 OpenUsage Bar 的本地
Runtime Observation 账本。

当前兼容契约固定为：

- CLIProxyAPI `v7.2.113`；
- `usage.TokenAccountingSchemaVersion = 2`；
- OpenUsage Bar `runtime-observation/v1`。

它不使用 `/v0/management/usage-queue`，因为读取该接口会弹出上游队列记录。

## 注册

在自定义 CLIProxyAPI 宿主初始化时注册：

```go
package main

import (
	"log"
	"os"
	"path/filepath"

	openusageplugin "github.com/tttboy123/openusage-bar/integrations/cliproxyapi-openusage"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/usage"
)

func registerOpenUsage() {
	home, err := os.UserHomeDir()
	if err != nil {
		log.Fatal(err)
	}
	state := filepath.Join(home, ".local", "state", "openusage-bar")
	if err := os.MkdirAll(state, 0o700); err != nil {
		log.Fatal(err)
	}
	plugin, err := openusageplugin.New(openusageplugin.Config{
		CollectorPath: "/Applications/OpenUsage Bar.app/Contents/MacOS/OpenUsage Collector",
		DatabasePath:  filepath.Join(state, "runtime.sqlite3"),
		ScopeRef:      "anon_替换为随机生成值",
		ProviderMap: map[string]string{
			"openai":    "openai",
			"anthropic": "anthropic",
			"gemini":    "google",
		},
	})
	if err != nil {
		log.Fatal(err)
	}
	usage.RegisterNamedPlugin("openusage-bar", plugin)
}
```

在本仓库尚未发布独立 Go module tag 前，宿主的 `go.mod` 应使用本机源码：

```go
replace github.com/tttboy123/openusage-bar/integrations/cliproxyapi-openusage => /绝对路径/openusage-bar/integrations/cliproxyapi-openusage
```

CLIProxyAPI 的预编译应用不会动态加载任意 Go module，因此需要在自定义宿主或
上游构建入口注册后重新编译。OpenUsage Bar 不会改写 CLIProxyAPI 的二进制、配置
或凭证。

## 数据边界

插件只读取：

- `Record.Provider`、`Record.Model`；
- `Record.RequestedAt`、`Record.Latency`、`Record.TTFT`、`Record.Failed`；
- `Record.Detail.TokenBreakdown`。

以下字段即使存在也不会被读取或序列化：API Key、Auth ID、Alias、Source、
Response Header、失败响应正文、请求/响应内容。只有 `TokenBreakdown.Valid()`、
`quality=complete`、`unclassified_tokens=0` 且总 Token 大于零的记录会写入；
其他记录会被丢弃，不会变成零用量。

Collector 通过绝对路径启动，文档只走 stdin，三秒超时，并使用不含 Provider
凭证的最小环境。Collector 失败不会改变 CLIProxyAPI 的模型响应。
