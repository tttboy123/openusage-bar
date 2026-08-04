# UsageHub GUI 跨平台决策

状态：已定稿（2026-08-04）。

## 决策

UsageHub 的 GUI 分三层，核心与界面严格解耦：

1. **数据与控制层（跨平台）**：Python collector + SQLite 账本 + Local API v1
   + CLI。这一层是 UsageHub 的全部事实来源，三平台共用同一实现。
2. **loopback Web dashboard（当前阶段）**：`openusage-bar dashboard` 在
   `127.0.0.1:17822` 提供只读 HTML 概览与 `/v1/snapshot` JSON，供无原生客户端的
   平台（Windows/Linux）和 headless 用户使用。不渲染任何凭据。
3. **桌面壳（后续阶段）**：macOS 保留原生菜单栏（SwiftUI）；跨平台管理界面
   用 Tauri 或 Electron 包壳访问同一 Web dashboard/API。技术选型推迟到
   Windows/Linux 实机验证之后，避免在 shell 上过早锁定。

## Provider Center 快速连接

快速连接事实由 `openusage_bar/quick_connect.py` 提供（独立稳定映射，不改冻结的
provider catalog schema）：

- 每个 family 声明 `console_url`（跳转官方控制台拿 Key）与 `auth_modes`
  （`api_key` / `oauth` / `auto_detect`）。
- CLI `openusage-bar connect --family deepseek` 输出 JSON，SwiftUI Provider
  Center 与未来桌面壳复用同一映射。

## 不变量

- 核心独占写入；所有 GUI 只读。
- 凭据只进 Keychain/系统凭据库，不进账本、日志、HTML 或 JSON。
- 不新增请求代理；dashboard 与桌面壳都不是路由权威。
