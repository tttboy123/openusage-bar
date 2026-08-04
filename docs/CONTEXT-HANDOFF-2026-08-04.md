# UsageHub 完整交接上下文（2026-08-04）

> 本文档用于在新 Codex 会话中无缝接手「客户端重构和优化」及后续任务。
> 生成时点：2026-08-04，作者：上一轮 Codex 会话（含全部历史决策与实测结果）。

## 0. 一句话定位

**UsageHub**（原 OpenUsage Bar / openusage-bar-public-release）是一个**跨平台的 AI 用量观测工具**：本地优先、只读、数据留在本机；与 CC Switch、OmniRoute 生态互补，作为**可插拔的观测组件**存在，只做观测领域并做到最好。产品名已改为 **UsageHub**，但代码/包名/安装产物仍沿用 OpenUsage Bar。

## 1. 仓库与环境

### 仓库

- 路径：`/Users/lune/Documents/Codex/2026-07-13/new-chat/work/openusage-bar-public-release`
- 分支：`feat/usagehub-core-portability`（当前工作分支）
- HEAD：`d9c467a feat: instrument-style cross-platform dashboard redesign`
- 工作树：干净（生成本文档时无未提交改动）
- 远程：`https://github.com/tttboy123/openusage-bar.git`，PR #58 跟踪该分支

### 本机运行状态（已核验）

| 组件 | 状态 |
|---|---|
| CC Switch | 运行中（`/Applications/CC Switch.app`，PID 98211） |
| CC Switch 本地路由 | 监听 `127.0.0.1:15721`，Codex 接管已开启 |
| UsageHub 菜单栏 runtime | 运行中（PID 82910，LaunchAgent `com.lune.openusagebar`） |
| UsageHub collector daemon | 运行中（PID 82900，`OpenUsage Provider Settings daemon`） |
| UsageHub Activity 窗口 | 进程运行中（PID 82980） |
| Web dashboard (17822) | 当前未监听（由 Electron 按需拉起） |
| Electron 桌面壳 | 当前未运行（`desktop/` 下 `npx electron .` 拉起） |
| 已安装 App | `/Applications/OpenUsage Bar.app`（含 Helpers: Activity / Provider Settings） |
| Codex 当前模型 | `deepseek-v4-flash`（`~/.codex/config.toml` 指向本地路由） |

### Codex 配置文件（CC Switch 接管后）

- `~/.codex/config.toml`：`model_provider="custom"`、`model="deepseek-v4-flash"`、`base_url="http://127.0.0.1:15721/v1"`、`wire_api="responses"`、`model_catalog_json="cc-switch-model-catalog.json"`
- `~/.codex/auth.json`：由 CC Switch 用占位符管理，真实 Key 在 CC Switch 数据库
- `~/.codex/cc-switch-model-catalog.json`：当前 provider 的模型目录（切换时重新生成）

## 2. 产品决策（用户已确认，勿偏离）

1. **只做观测，做最好**：OpenUsage Bar / UsageHub 只承担观测（数据与账本），作为可插拔组件存在，不抢 CC Switch / OmniRoute 的切换与管理职责。
2. **与 CC Switch 结合**：读取 CC Switch 的成本/用量数据（只读适配器），并可结合 CC Switch 做快速登录/跳转。
3. **免费额度聚合**：参考 OmniRoute 的免费额度聚合概念，作为产品吸引点（含 DeepSeek 等余额展示，已有 Quota Hub）。
4. **快速登录跳转**：参考 CC Switch 直接提供跳转逻辑（quick-connect）。
5. **命名**：产品名 = **UsageHub**（参考 Prometheus 等开源项目命名风格），代码内可渐进迁移。
6. **跨平台**：Electron 桌面端 + Web 端都要有；支持 macOS / Linux / Windows；核心本质是 Python/JS 多平台支持，打包不同产物即可。
7. **App 入口必须存在**：菜单栏图标 / 系统托盘 / App 栏入口不能缺失（此前用户多次抱怨入口问题，已修复为 NSStatusItem + Electron 托盘）。
8. **消耗口径一致**：日/周/月/年消耗视图要与额度口径保持一致（此前用户明确要求）。
9. **UI 风格**：参照苹果原生 OpenUsage Bar 与 CC Switch 的 UI 设计；最近一轮已把 Web 仪表盘重做为「精密仪器」风格（见 §4），但**与原生 SwiftUI 界面的风格统一尚未完成**（见 §5 待办）。

## 3. 架构概览

```
┌────────────────────────────────────────────────────────────┐
│  Electron 壳 (desktop/)                                    │
│  - main.js: 拉起 dashboard --port 17822 子进程             │
│  - 原生窗口 1200x800 + 系统托盘（macOS/Linux/Windows）      │
└───────────────┬────────────────────────────────────────────┘
                │ http://127.0.0.1:17822
┌───────────────▼────────────────────────────────────────────┐
│  Python 核心 (openusage_bar/)                              │
│  - collector_cli.py: daemon / dashboard / status / ... CLI  │
│  - web_dashboard.py: 跨端 Web 仪表盘（instrument 风格）      │
│  - cc_switch.py: CC Switch 只读成本/用量适配器              │
│  - omniroute.py / deepseek.py / minimax.py / moonshot.py    │
│  - quick_connect.py: 快速登录跳转                           │
│  - reconciliation.py: 对账                                 │
│  - executors.py: 执行器（cc_switch / omniroute 插件）        │
│  - local_api.py: 本地 API (/v1/quick-connect 等)            │
│  - activity_store / query / aggregator: 本地账本            │
└───────────────┬────────────────────────────────────────────┘
                │ SQLite: ~/.local/state/openusage-bar/
┌───────────────▼────────────────────────────────────────────┐
│  Swift 原生 (swift_app/)                                   │
│  - OpenUsageBar: 菜单栏 NSStatusItem + NSPopover            │
│  - OpenUsageActivity: 用量/额度/对账窗口（原生 SwiftUI）     │
│  - UsageCore: 共享数据层（Swift）                           │
└────────────────────────────────────────────────────────────┘
```

### 关键模块说明

- `openusage_bar/cc_switch.py`：只读读取 `~/.cc-switch/cc-switch.db`（`mode=ro`、`query_only=ON`），绝不触碰 `settings_config` 里的凭据。
- `openusage_bar/web_dashboard.py`：`render_dashboard(snapshot, today)` 输出单文件 HTML（内联 CSS，无 JS 依赖），`make_dashboard_server(query, port)` 起 HTTP 服务。
- `openusage_bar/collector_cli.py`：CLI 入口是 `main([...])` 函数，**没有 `if __name__ == "__main__"` 守卫**，`python -m` 不会执行；测试/调试用 `.build-venv/bin/python -c "from openusage_bar.collector_cli import main; main(['dashboard','--port',...])"`。
- `desktop/main.js`：`dashboardCommand()` 优先 `USAGEHUB_COLLECTOR` 环境变量，否则用已安装 App 的 Provider Settings helper；`ensureDashboard()` 检测 17822 是否已就绪。
- Swift 菜单栏：`OpenUsageBarApp.swift` 中 `StatusItemController` 实现托盘按钮 + NSPopover。

## 4. 近期已完成（里程碑）

- **Web 仪表盘第二次重做（HEAD `d9c467a`，已推送）**：「精密仪器」风格（design-taste 反 AI 味路线）：
  - 单一翡翠绿强调色（浅色 `#087F52` / 深色 `#34D399`），去掉原 AI 蓝紫渐变与卡片堆叠
  - KPI 发丝线分组、Quota Hub 币种卡强调色左边条、Provider/Source 面板、等宽数字、状态胶囊
  - `prefers-color-scheme` 浅/深、`prefers-reduced-motion`、焦点可见、响应式（170px/640px 折叠）
  - 对比度修正：浅色 accent 3.16→4.70，text-faint 两主题均 ≥4.5:1；零 em-dash（空值用 `n/a`）
  - 已通过 `py_compile` + 单元测试 + 完整 `build_app.sh`（954 Python + 257 Swift 测试、覆盖门禁、签名）
- **TokenHub Kimi K3 接入 CC Switch**（见 §6）。
- **CC Switch 只读成本/用量导入**（`304999c`）：`usage_daily_rollups` 读入本地账本。
- **DeepSeek 余额与花费**（`3ad49a1`）：Quota Hub 展示 DeepSeek 余额。
- **Electron 桌面壳**（`bb62ef3`）。
- **历史稳定提交**：`cc3047e` 之前的全部里程碑 1-6（CC Switch/OmniRoute 适配、对账、执行器、本地 API、Web 仪表盘、跨平台文档）。

## 5. 待办任务（新会话主任务）

### 5.1 客户端重构和优化（主任务）

1. **UI 风格统一（用户明确提出的问题）**：
   - 现状：Web 仪表盘是「精密仪器」深绿自定义风格；原生 SwiftUI（菜单栏 Popover / Activity 窗口 / 设置页）仍是系统默认蓝色 accent + 原生组件，两者差异大。
   - 方向建议：定义共享设计令牌（语义色、状态色、间距），两端对齐「色相与语义」；原生端保持系统原生组件形态（符合 HIG），Web 端保持仪器感但统一 accent 与状态色。用户上一轮正问到这个问题，尚未决策实施。
2. **App 入口完善**：菜单栏状态项已修复；核对桌面 App / Activity 窗口 / 设置页入口在 macOS、Linux、Windows 都可见可打开。
3. **跨平台交付**：Electron 壳 + Web 仪表盘需覆盖 Linux / Windows；打包不同产物（macOS .app/.dmg、Windows .exe、Linux AppImage/deb），并有对应 CI/发布脚本（参考现有 `scripts/package_release.sh`、`release_dmg_audit.sh`）。
4. **消耗口径一致**：日/周/月/年消耗视图与额度口径保持一致（此前用户要求「日周月年的消耗呢，要和额度的保持一致」）。
5. **Web 端也要能访问**：仪表盘（17822）除桌面壳外，应能作为 Web 应用访问（局域网/本地浏览器），注意只读与鉴权边界。

### 5.2 后续产品任务

1. **免费额度聚合**：参考 OmniRoute 的免费额度聚合概念做成产品噱头（已有 Quota Hub 雏形，需扩展到多 provider 免费额度聚合展示）。
2. **快速登录/跳转**：参考 CC Switch 的跳转逻辑（`quick_connect.py` 已有 `/v1/quick-connect`，可在 UI 增加入口）。
3. **可插拔观测组件**：保证 UsageHub 只做观测、可独立插拔（对外提供稳定只读 API/数据接口）。
4. **云服务器验证**（用户已授权）：可用 `cloud-skills-mcp` 能力买云服务器验证 Linux 部署与 Web 端。
5. **命名迁移**：产品显示名 UsageHub（代码/包名渐进迁移，注意 Electron 窗口标题已是 UsageHub）。

### 5.3 建议执行顺序

1. 先做 **UI 统一**（用户当前最在意的感知问题）：拉取原生 SwiftUI 的 UI 代码（`swift_app/Sources/OpenUsageActivity/*.swift`、`MenuBarViews.swift`），截图对比，定义共享 tokens 并两端实施。
2. 再做**消耗口径**（日周月年）与 Web 端访问。
3. 最后做跨平台打包与免费额度聚合。

## 6. CC Switch / 模型接入状态

### 当前 provider（codex，`~/.cc-switch/cc-switch.db`）

| name | is_current | apiFormat | 说明 |
|---|---|---|---|
| DeepSeek | 1 | openai_responses | 原生直连，当前使用 |
| MiniMax | 0 | openai_responses | 原生直连 |
| **TokenHub Kimi K3** | 0 | openai_chat | 新增，Chat 格式走本地路由 |
| OpenAI Official | 0 | - | 官方账号 |

### TokenHub Kimi K3 接入细节（已完成）

- 端点：`https://tokenhub.tencentmaas.com/v1/chat/completions`，模型 `kimi-k3`（推理模型，返回 `reasoning_content`），已实测可用。
- provider 配置按官方 Kimi 预设结构写入：`meta.apiFormat="openai_chat"`、`codexChatReasoning`（supportsThinking=true、thinkingParam="thinking"、outputFormat="reasoning_content"）、modelCatalog `kimi-k3` 上下文 1048576。
- base_url 保存为 `https://tokenhub.tencentmaas.com/v1`、`wire_api="responses"`（CC Switch 接管时 Codex live 配置指向 `127.0.0.1:15721/v1`，路由做 Responses→Chat 转换）。
- 本地路由已开启：`settings.json` `enableLocalProxy=true`；`proxy_config` codex `enabled=1, proxy_enabled=1`；15721 监听中。
- 数据库备份：`~/.cc-switch/backups/cc-switch.db.bak-20260804T141216Z`、`settings.json.bak-*`。

### 使用 Kimi 的步骤（用户尚未执行）

1. CC Switch → Codex 标签 → 启用 **TokenHub Kimi K3**（当前未启用，`is_current=0`）。
2. 重启 Codex 会话（config.toml 与 model catalog 是新进程读取的，不会热加载）。

### 多 session 同时用不同模型（机制已确认）

- Codex 配置启动时读取一次，运行中的会话不热加载。
- 因此：保持当前 DeepSeek 会话不关 → 在 CC Switch 启用 Kimi → 新开一个 Codex 会话即为 Kimi，两者并行互不影响；session 历史各自保留。
- 同一会话内 `/model` 切换仅限同一 provider 模型目录内的模型（跨 provider 无效，路由只有一个当前上游）。
- 进阶：CLI 可用 `-c 'model=...'` / `-c 'model_providers.custom.base_url=...'` 或独立 config 覆盖，但会绕过 CC Switch 接管与用量归属，谨慎使用。

### CC Switch 官方参考

- 开源仓库：`farion1231/cc-switch`（v3.19.1，安装于 `/Applications/CC Switch.app`，运行中）。
- Kimi 路由指南：`docs/guides/codex-kimi-routing-guide-zh.md`（本地拉取过，要点：Chat 上游必须走本地路由；接管时 Codex 始终连 `127.0.0.1:15721/v1`、`wire_api="responses"`；`meta.apiFormat="openai_chat"` 决定路由转换）。
- 数据：`providers.settings_config` 为 JSON（auth/config TOML/modelCatalog），`meta` 存 `apiFormat`、`codexChatReasoning` 等；`provider_endpoints` 存地址。

## 7. 关键命令

```bash
ROOT=/Users/lune/Documents/Codex/2026-07-13/new-chat/work/openusage-bar-public-release
cd "$ROOT"

# 单元测试（快速）
python -m unittest tests.test_web_dashboard -q
python -m unittest discover -s tests -q

# 完整构建（含测试/覆盖/签名，约 3-4 分钟；产出 dist/OpenUsage Bar.app）
./scripts/build_app.sh

# 安装到 /Applications（原子替换 + LaunchAgent 管理 + 自动备份回滚）
./scripts/install_app.sh

# 本地启动 dashboard（注意：CLI 无 __main__ 守卫）
.build-venv/bin/python -c "from openusage_bar.collector_cli import main; main(['dashboard','--port','17822'])"

# 启动 Electron 桌面壳（会拉起 17822）
cd desktop && npx electron .

# 推送（PR #58 自动更新；需要 gh workflow scope）
git -c credential.helper= -c credential.helper='!gh auth git-credential' push

# Swift 测试
swift test --package-path swift_app
```

## 8. 安全与注意事项

- **绝不把 API Key 提交进仓库/git 历史**。CC Switch 数据库、`~/.codex/auth.json` 里的凭据只在本地；UsageHub 适配器只读不碰 `settings_config` 凭据。
- **不要重新集成 `feature/runtime-producer-adapters`**：该上游分支（71 次提交）之前有一次中断的半完成 merge 已被撤销；保留 `origin/feature/runtime-producer-adapters` 不动，除非用户再次明确要求（后续整合应独立进行）。
- 修改 `~/.codex/config.toml`、`~/.cc-switch/settings.json`、`proxy_config` 会改变用户全局 Codex/CC Switch 行为；操作前备份（已形成惯例：`*.bak-时间戳`）。
- 推送使用专用 git 凭证命令（见 §7），不要用默认 credential helper 覆盖。
- 用户偏好：直接交付可验证的本地运行结果（截图/App 打开），不要假装未验证的内容；技术命名空间（包名/端点）保持稳定；中文交流。

## 9. 可用技能（新会话可复用）

- `ui-ux-pro-max`：UI/UX 设计智能（`/Users/lune/.codex/skills/ui-ux-pro-max.bak.1784013622/SKILL.md`）
- `design-taste-frontend`：反 AI 味前端设计（`/Users/lune/.agents/skills/design-taste-frontend/SKILL.md`）
- `define-goal`：目标整理（`/Users/lune/.codex/skills/define-goal/SKILL.md`）
- `apple-design`、`product-lens`、`loop-design-check` 等

## 10. 参考截图（最近一次 GUI 交付）

- 新 Web 仪表盘浅色：`/Users/lune/.codex/visualizations/2026/08/03/019fc983-20e6-73d0-b985-047eab43c90d/usagehub-cdp.png`
- 新 Web 仪表盘深色：`/Users/lune/.codex/visualizations/2026/08/03/019fc983-20e6-73d0-b985-047eab43c90d/usagehub-dark-cdp.png`
- 原生 Activity 截图：`/tmp/usagehub-native-activity.png`（用于风格对比）
