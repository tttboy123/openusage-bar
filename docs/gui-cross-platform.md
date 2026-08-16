# UsageHub GUI 跨平台决策

状态：已定稿（2026-08-06）。

## 决策

UsageHub 的 GUI 分三层，核心与界面严格解耦：

1. **数据与控制层（跨平台）**：Python collector + SQLite 账本 + Local API v1
   + CLI。这一层是 UsageHub 的全部事实来源，三平台共用同一实现。
2. **loopback Web dashboard（当前阶段）**：`openusage-bar dashboard` 在
   `127.0.0.1:17822` 提供只读 HTML 概览与 `/v1/snapshot` JSON，供无原生客户端的
   平台（Windows/Linux）和 headless 用户使用。不渲染任何凭据。
3. **桌面壳（当前阶段）**：macOS 保留原生菜单栏（SwiftUI）；Electron + Web
   是 macOS、Windows、Linux 共用的桌面管理界面。打包壳加载内置静态资源，
   由主进程访问私有 Observer/Gateway 边界，renderer 不接触 bearer 或运行时路径。

演进说明（2026-08-09）：Electron 已确定为跨平台桌面壳。打包应用不再启动或
读取 `127.0.0.1:17822`；它从临时 loopback 静态服务器加载同一 `web/dist`，主
进程启动/探测一个私有 Observer，并通过 macOS/Linux owner-private Unix socket
或 Windows token + ACL 代理固定的 Local API 只读路由。托盘的 snapshot/capacity
也复用这一边界。Optional Gateway 启用后，主进程另提供唯一的只读建议桥接
`POST /gateway/v1/should-send`：renderer 只提交有界四字段 JSON，主进程重建请求、
注入私有 token，并重建白名单响应；该操作不转发 Provider 请求。17822 仅保留给
standalone dashboard 与 demo 兼容流程。

## Provider Center 快速连接

快速连接事实由 `openusage_bar/quick_connect.py` 提供（独立稳定映射，不改冻结的
provider catalog schema）：

- 每个 family 声明 `console_url`（跳转官方控制台拿 Key）与 `auth_modes`
  （`api_key` / `oauth` / `auto_detect`）。
- CLI `openusage-bar connect --family deepseek` 输出 JSON，SwiftUI Provider
  Center 与未来桌面壳复用同一映射。

## 不变量

- Collector 继续独占事实账本写入；GUI 不直接修改账本、凭据、Gateway 配置或缓存。
- Should-Send 的 HTTP `POST` 只读取已记录事实并返回建议，不代表产品状态写入，
  也不会触发 Provider 转发。
- 凭据只进 Keychain/系统凭据库，不进账本、日志、HTML 或 JSON。
- renderer 只使用固定相对路由；Electron 主进程只做冻结路由的鉴权与净化桥接，
  Gateway/Python 仍是策略与路由权威。

## 三端统一交互规范（2026-08-06）

以下规范保证 Web Dashboard、Swift 原生 App（菜单栏 + Activity 窗口）和 Desktop/Electron 壳在视觉、交互和反馈上保持一致，参考 CC Switch 的 provider-card 视觉语言。

### 1. Provider 卡片统一

任何展示 provider 列表的地方都使用同一卡片结构：

- **左侧**：圆角头像（brand color + 名称首字母），替代无意义的分类图标。
- **中左**：provider 显示名称 + 次要信息（URL / scope / connection method）。
- **中右**：状态徽章（StatusBadge），由带色圆点 + 短文字标签 + 相对时间组成；不能只用颜色或图标表达状态。
- **右侧**：悬停/聚焦后显示的操作（Open console / Get API key），移动端与 reduced-motion 默认常显。
- **容器**：12px 圆角、1px hairline 边框、极淡阴影；hover 时边框变 accent、阴影加深。

状态徽章使用短标签（如「已连接 / 已过期 / 失败 / 未连接」），完整诊断文案通过 title/tooltip 或展开状态提供，避免长文案挤压名称导致截断。

实现位置：
- Web: `web/src/pages/ProvidersPage.tsx` / `web/src/components/ProviderCard.tsx` / `web/src/styles/app.css`
- Swift: `swift_app/Sources/UsageCore/UnifiedUIComponents.swift` + `MenuBarViews.swift` + `ProviderCenterViews.swift`
- Desktop: Electron 壳托盘菜单内嵌 provider 状态摘要，主窗口仍使用同一 Web dashboard 渲染。

### 2. 状态颜色统一

| 语义 | Web token | Swift `UnifiedStatusBadge` | Desktop 托盘 | 使用场景 |
|---|---|---|---|---|
| 正常/已连接 | `--accent` / `#087F52` | `.ok` | 🟢 | 容量正常、连接成功、数据完整 |
| 警告/注意 | `--warn` / `#B45309` | `.warning` | 🟡 | 容量 < 40%、部分数据、stale |
| 严重/失败 | `--bad` / `#C0392B` | `.critical` | 🔴 | 容量 < 20%、认证失败、缺失 |
| 中性/未连接 | `--text-faint` | `.neutral` | ⚪ | 未配置、不可用、占位 |

### 3. 加载与刷新反馈统一

- 刷新按钮在异步进行时必须显示旋转动画（Web spinner / Swift `UnifiedRefreshButton` rotation），且禁用重复提交。
- 列表加载超过 300ms 优先使用骨架屏或保持上次数据 + 微状态提示，避免空白或长时间 spinner。
- 失败时保留上次可用数据，并在标题/托盘/状态栏显示「Showing last-good data」等文案。
- Provider 列表顶部显示相对时间，如「刚刚 更新」「19 分钟前 更新」。

### 4. 空状态统一

空状态由图标 + 标题 + 说明 + 主操作按钮组成，不能只放一行文字。例如：

- 无 provider 时：「No capacity data」+ 「Connect a supported provider」→ 打开 Provider Center。
- 无历史时：「No token history」+ 说明文案。
- 失败时：标题 + 错误摘要 + 重试/打开 Data Health 操作。

实现：Swift `UnifiedEmptyState`；Web `.empty-block`；Desktop 在托盘菜单中显示摘要文案。

### 5. 分段控制器（Segmented Control）统一

周期/分类切换使用圆角药丸分段控制器：

- 容器：浅灰背景、2px 内边距、10px 圆角。
- 选中项：accent 软背景 + accent 文字 + semibold。
- 未选中项：透明背景 + 次级文字。
- 键盘支持：左右箭头切换、Enter 确认。

实现：Web `.segmented` CSS；Swift `UnifiedSegmentedControl`；Desktop 依赖 Web 渲染。

### 6. 快捷键与导航统一

| 操作 | Web | Swift App | Desktop/Electron |
|---|---|---|---|
| 刷新 | `Ctrl/Cmd + R` | `Cmd + R`（菜单栏/Activity） | `Ctrl/Cmd + R`（应用菜单 + 托盘） |
| 设置/Provider Center | `Ctrl/Cmd + ,` | `Cmd + ,`（菜单栏 footer） | `Ctrl/Cmd + ,` |
| 打开主窗口 | — | `Cmd + D` / 点击状态栏 | `Ctrl/Cmd + O` |
| 关闭浮层/弹窗 | `Esc` | `Esc`（菜单栏 popover） | `Esc` |
| 切换语言 | Web header 按钮 | — | — |

### 7. 动效统一

- 微交互时长：`150–300ms`（Web `var(--motion-fast/base/slow)`；Swift `DesignTokens.motionFast/Base/Slow`）。
- 仅使用 `transform` / `opacity` 动画，不触发布局重排。
- 支持 `prefers-reduced-motion` / `accessibilityReduceMotion`：减少或消除动画。
- 状态切换必须有视觉反馈；hover、active、focus 三态必须有区分。

### 8. 暗色模式统一

- Web 与 Desktop 使用同一 CSS token 系统：`--bg`、`--surface`、`--text`、`--accent` 等，按 `prefers-color-scheme` 切换。
- Desktop Electron 窗口背景色和标题栏跟随系统主题（`nativeTheme`），并通过 `syncTheme()` 与 Web 内部主题保持一致。
- Swift 使用 `DesignTokens` 的 light/dark 颜色值，由系统外观自动切换。

### 9. 设计评审与测试辅助

为在 macOS 上稳定截取菜单栏 popover 与托盘菜单，工程侧提供以下辅助：

- Swift 菜单栏：监听 `com.lune.openusagebar.showPopover` 分布式通知并直接弹出 popover，不受刘海 overflow 影响。
- Electron 托盘：应用菜单增加「Show Tray Menu」，调用 `tray.popUpContextMenu()` 展示当前 provider 摘要。
- React Storybook：`web/.storybook` 提供 ProviderCard 的 Connected / Stale / Error / Grid 状态，便于独立评审。

### 10. 文件清单

- Web 端：`web/src/styles/tokens.css`、`web/src/styles/app.css`、`web/src/App.tsx`、`web/src/pages/ProvidersPage.tsx`、`web/src/components/ProviderCard.tsx`、`web/src/components/PeriodSelector.tsx`、`web/.storybook/` 等。
- Swift 端：`swift_app/Sources/UsageCore/UnifiedUIComponents.swift`、`swift_app/Sources/UsageCore/GeneratedDesignTokens.swift`、`swift_app/Sources/OpenUsageBar/OpenUsageBarApp.swift`、`swift_app/Sources/OpenUsageBar/MenuBarViews.swift`、`swift_app/Sources/OpenUsageActivity/ProviderCenterViews.swift`、`swift_app/Sources/OpenUsageActivity/ActivityDashboardViews.swift`。
 - Desktop 端：`desktop/main.js`、`desktop/package.json`。

## 11. Activity / Capacity / Local Tools / Automation 统一（v0.7.1 能力补齐）

在 v0.7.1 原生 Activity 窗口中已有的能力，跨平台 Web Dashboard 与 Desktop 壳必须保留，不降级：

- **Activity 活动页**：
  - 顶部周期分段控制器（日/周/月/年） + Provider/Model 联动筛选器。
  - MetricStrip：Total / Input / Output Tokens、Cache Read / Cache Creation / Reasoning、Peak Day、Active Days、Current Streak；数据缺失时显示 `Partial` 标签。
  - 按日/模型 Token 折线图（recharts `LineChart`），Top 6 模型用固定色板；图例可切换。
  - 日历热力图（7 行 × N 周），强度映射当天 Token 总量，支持 hover 提示与键盘导航。
  - 实现位置：`web/src/pages/ActivityPage.tsx`。

- **Capacity 额度页**：
  - 保留现有额度表格与进度条。
  - 新增 Quota History 折线图，展示各 provider/quota 窗口剩余比例随时间变化；支持 `Focus top 6` / `Show all` 切换、hover tooltip、键盘左右键浏览数据点。
  - 实现位置：`web/src/pages/CapacityPage.tsx`。

- **Local Tools 本地工具页**：
  - 从占位改为真实数据：按本地工具 family 汇总 Token，展示近 30 天柱状图与最近活动明细表。
  - 实现位置：`web/src/pages/LocalToolsPage.tsx`。

- **Automation 自动化页**：
  - 从占位改为展示 `/v1/changes` 账本变更流（自动导入、配额更新等），支持时间排序与最近 50 条。
  - 实现位置：`web/src/pages/AutomationPage.tsx`。

- **新增 API 路由**：`web_dashboard.py` 增加 `/v1/quotas/history` 与 `/v1/changes` 路由，使 Web Dashboard 独立运行也能提供图表与变更数据。

- **统一数据接口**：`web/src/api.ts` 扩展 `ActivityRow` 字段（input/output/cache/reasoning）、新增 `fetchQuotaHistory`、`fetchChanges`，并在 `fetchActivity` 中支持 Provider/Model 筛选参数。

- **统一文案**：`web/src/i18n.ts` 新增上述所有中文/英文键，避免各端文案不一致。

- **Swift 原生端**：上述数据展示逻辑在 `OpenUsageActivity` 中已完整实现；Web 补齐后，Desktop 壳自动继承同一前端，三端信息一致。

## 12. 构建与验证清单

- Web：`npm run build`（`web`）通过；`npm run build`（`desktop`）通过。
- Swift：`swift test --package-path swift_app --enable-code-coverage -Xswiftc -warnings-as-errors` 通过。
- Python：`python -m unittest tests.test_web_dashboard tests.test_local_api` 通过。
- 每小时 heartbeat automation `usagehub-v0-7-1-parity-continuation` 已激活，用于在额度中断后自动继续并询问用户状态。
 - 历史实机验证：使用 `scripts/demo_backend.py` 在 macOS 上启动带数据的 demo 后端（TCP 17822 + Unix socket），分别运行当时的 Desktop `UsageHub.app`、Swift `OpenUsage Activity.app` 与菜单栏 `OpenUsage Bar.runtime --background --show-popover` 并截取真实窗口/Popover 图像。当前 packaged Electron 只使用私有 Observer 边界。
 - 截图目录：本地工作区 `outputs/screenshots/`（不入库）。

 ## 13. 实机验证与 demo 后端

 为在本地快速验证三端 UI 而不依赖真实 collector，新增 `scripts/demo_backend.py`：

 - 在内存 SQLite 中生成 30 天、多 Provider/Model 的合成 activity、cost 与 quota 数据。
 - 同时启动：
   - Web Dashboard TCP 服务器 `http://127.0.0.1:17822`（供浏览器与 legacy Desktop demo 使用）。
   - Local API Unix socket `~/.local/state/openusage-bar/openusage.sock`（供 Swift app 使用）。
 - 保留 legacy Desktop demo 的 `USAGEHUB_COLLECTOR dashboard --port 17822` 调用形式；当前 packaged Electron 不依赖它。

 启动方式：

 ```bash
 cd <repo-root>  # 仓库根目录
 .build-venv/bin/python scripts/demo_backend.py dashboard --port 17822
 ```

 Desktop 壳：

 ```bash
 open dist-desktop/mac-arm64/UsageHub.app
 ```

 Swift Activity 窗口：

 ```bash
 open dist/OpenUsage\ Bar.app/Contents/Helpers/OpenUsage\ Activity.app
 ```

 Swift 菜单栏 Popover：

 ```bash
 dist/OpenUsage\ Bar.app/Contents/MacOS/OpenUsage\ Bar.runtime --background --show-popover
 ```

 验证结果：

 - Desktop Activity 页正常渲染周期分段、MetricStrip、Provider/Model 筛选、模型趋势折线图与日历热力图。
 - Swift Activity 窗口正常渲染用量详情、每日 Token 活动热力图、每日模型趋势。
 - Swift 菜单栏 Popover 正常渲染今日 Token、Provider 额度摘要与快捷入口。
 - 注意：Swift 端若本地已安装旧版 collector/launch agent，可能优先读取其数据库而非 demo 后端；验证前需临时卸载 `com.lune.openusagebar.collector` 或停止其 daemon，以确保数据来自 demo 后端。
 - 注意：Swift 端若本地已安装旧版 collector/launch agent，可能优先读取其数据库而非 demo 后端；验证前需临时卸载 `com.lune.openusagebar.collector` 或停止其 daemon，以确保数据来自 demo 后端。

 ## 14. Provider 实时状态徽标（Web / Swift 统一）

 Web 与 Swift 的 Provider 列表在状态为「已连接」且最近一次成功同步在 5 分钟内时，显示绿色脉冲「实时」徽标：

 - Web：`ProviderCard` 在 `provider-status-ok` 徽标前增加 `.provider-status-live` 脉冲圆点，CSS 动画 `provider-pulse` 1.6s 循环，支持 `prefers-reduced-motion`。
 - Swift：`UnifiedStatusBadge` 新增 `isLive` 参数，`ProviderCenterItem` 从 `data.health.sources` 聚合最新 `lastSuccessAt` 并计算 `isLive`，在 `ProviderCenterRow` 与详情页中传入。
 - 实现文件：`web/src/components/ProviderCard.tsx`、`web/src/styles/app.css`、`swift_app/Sources/UsageCore/UnifiedUIComponents.swift`、`swift_app/Sources/OpenUsageActivity/ProviderCenterViews.swift`、`swift_app/Sources/OpenUsageActivity/ActivityAppLogic.swift`。
 - 三端统一：Desktop 通过 Web 前端自动继承该徽标；Swift 原生 Provider Center 使用同一语义。
 - 截图目录：本地工作区 `outputs/screenshots/`（不入库）。
 - Storybook：ProviderCard / ProviderGrid 已提供 Live / Connected / Stale / Error / Grid 故事，便于独立设计评审与截图，构建输出到 `outputs/storybook`。
每小时 heartbeat automation `usagehub-v0-7-1-parity-continuation` 已激活，用于在额度中断后自动继续并询问用户状态。
- Web：`ProviderCard` 在 `provider-status-ok` 徽标前增加 `.provider-status-live` 脉冲圆点，CSS 动画 `provider-pulse` 1.6s 循环，支持 `prefers-reduced-motion`。
- Storybook 故事：`web/src/stories/ProviderCard.stories.tsx` 新增 `Live`、`ConnectedNotLive` 等状态；`ProviderGrid.stories.tsx` 展示多 Provider 状态网格。

## 15. v0.7.1 Automation / Local Tools 能力补齐与原生壳 Storybook

2026-08-07 追加：

- Web Dashboard 的 **Automation** 页从仅展示变更流升级为同时展示「Local API」只读事实面板（state、data revision、routes）与「Read-only commands」复制区域，对齐 v0.7.1 原生 Activity 的 Automation 页。
- Web Dashboard 的 **Local Tools** 页接入 `scripts/demo_backend.py` 生成的 Hermes / OpenClaw / Kiro CLI 合成数据，展示 30 天汇总柱状图与最近活动明细表，不再空态。
- 新增 React Storybook 组件用于原生壳设计评审：`MenuBarPopover` 预览 Swift 菜单栏 Popover，`TrayMenu` 预览 Electron 托盘菜单；对应 stories 构建到 `outputs/storybook`。
- 新增 API 路由：`/v1/health`、`/v1/schema`、`/v1/schema.json` 在 `openusage_bar/web_dashboard.py` 中实现，与 Local API 返回同一结构。
- 实现文件：`web/src/pages/AutomationPage.tsx`、`web/src/pages/LocalToolsPage.tsx`、`web/src/components/MenuBarPopover.tsx`、`web/src/components/TrayMenu.tsx`、`web/src/stories/MenuBarPopover.stories.tsx`、`web/src/stories/TrayMenu.stories.tsx`、`web/src/api.ts`、`web/src/styles/app.css`、`scripts/demo_backend.py`、`openusage_bar/web_dashboard.py`。
- 定时任务：每小时 heartbeat automation `usagehub-v0-7-1-parity-continuation` 已设置，用于在额度中断后询问状态并继续任务。
- 新增 API 路由：`/v1/health`、`/v1/schema`、`/v1/schema.json` 在 `openusage_bar/web_dashboard.py` 中实现，与 Local API 返回同一结构。
- 实现文件：`web/src/pages/AutomationPage.tsx`、`web/src/pages/LocalToolsPage.tsx`、`web/src/components/MenuBarPopover.tsx`、`web/src/components/TrayMenu.tsx`、`web/src/stories/MenuBarPopover.stories.tsx`、`web/src/stories/TrayMenu.stories.tsx`、`web/src/api.ts`、`web/src/styles/app.css`、`scripts/demo_backend.py`、`openusage_bar/web_dashboard.py`。
- 定时任务：每小时 heartbeat automation `usagehub-v0-7-1-parity-continuation` 已设置，用于在额度中断后询问状态并继续任务。
- Local Tools 详情页进一步升级为每个工具展示详情卡片：Observed Tokens / Active Days / Known Models / Last Activity / 状态徽章 / 采集时间，对齐 v0.7.1 原生详情指标。
