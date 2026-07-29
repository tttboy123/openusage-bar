# OpenUsage Bar GitHub 工作面执行草案

> 状态：本地草案，未执行任何 GitHub 写操作。任务范围、依赖与验收仍以
> [工作队列](2026-07-18-openusage-work-queue.zh-CN.md) 为唯一权威；本文只把 WQ-02
> 转换成可审阅的远端执行清单。

## 当前远端事实

- 默认分支：`main`，只读审计时为 `9cd134a4ff8631f4ed5e58cae7f063331247f098`。
- 普通 Issue：0；Milestone：0；Ruleset：0；`main` 未启用分支保护。
- Secret scanning 与 push protection 已开启。
- 开放 Dependabot PR：#5、#10、#11、#12、#13、#14。
- #5 与 #10 因远端旧版 Action 注释门禁失败；#11 的 CI 虽成功，但 7.0.1
  对应的人类注释仍为 `# v5`。当前本地门禁已要求 workflow 同时匹配受控清单中的
  完整 SHA 与精确 `vX.Y.Z`；同步门禁、更新清单并重跑前，不合并这些 PR。

## Milestone 草案

| Milestone | 描述 | 完成条件 |
| --- | --- | --- |
| `0.4.x Hardening` | 完成当前发布线的数据可信与实机收尾。 | 真实重启、自动刷新、安装、升级与回滚均通过可见验收；依赖更新通过当前完整门禁。 |
| `0.5 Data Trust` | 为 Provider 能力、Token、额度与费用补齐权威来源和真实账号证据。 | 第一批 Provider 有脱敏实证；第二批 Provider 有权威来源或明确 unsupported；Adapter Kit 可复用。 |
| `0.6 RC` | 冻结 Local API v1 兼容面并启动公开 Beta。 | N-1 API 兼容、外部安装与回滚、性能基线通过。 |
| `1.0 Canary` | 运行无遥测外部 Canary 并发布稳定版。 | 五台外部 Mac、五类 Provider 连续 30 天通过，发布供应链证据完整。 |

Milestone 暂不设置虚构截止日期。30 天 Canary 只能从第五台合格机器加入后开始计时。

## Issue 草案

Issue 正文统一从对应 WQ 小节复制目标、实施、验收、验证和依赖；不得省略
Unknown-not-zero、凭证不进入 Issue、真实验证不能由 hermetic 测试替代等边界。

| 标题 | Milestone | 现有标签 | 来源与依赖 |
| --- | --- | --- | --- |
| `Complete real macOS reboot recovery acceptance` | `0.4.x Hardening` | `enhancement` | WQ-05；真实内核重启后验证登录项、collector、API、账本与菜单栏。 |
| `Reconcile immutable Action pin gates with Dependabot updates` | `0.4.x Hardening` | `dependencies`, `github_actions` | WQ-02；同步当前 Action pin manifest 门禁后更新清单并重跑 #5、#10、#11，不在旧基线判断可合并。 |
| `Audit Provider capability declarations against live evidence` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-06；依赖 0.4.x Checkpoint。 |
| `Reconcile Codex local-session Token coverage with account totals` | `0.5 Data Trust` | `enhancement` | WQ-07；依赖 WQ-03、WQ-04、WQ-06。 |
| `Validate MiniMax capacity and delayed daily billing` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-08；中国站、国际站与多账号分别验证。 |
| `Validate StepFun regional quota and session isolation` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-08；中国站与国际站禁止跨站重试。 |
| `Validate Cursor capacity, PATH, timeout, and fallback semantics` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-09；依赖 WQ-06。 |
| `Validate Kiro AWS quota and read-only Keychain access` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-09；依赖 WQ-06。 |
| `Validate OpenAI Organization usage and billed-cost pagination` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-09；依赖 WQ-06。 |
| `Validate Claude Code, OpenCode, and local-tool coverage` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-10；依赖 WQ-06。 |
| `Research authoritative GLM usage and capacity sources` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-11；先调研，未证明前不得声明支持。 |
| `Research authoritative Kimi usage and capacity sources` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-11；先调研，未证明前不得声明支持。 |
| `Research authoritative Qwen usage and capacity sources` | `0.5 Data Trust` | `enhancement`, `help wanted` | WQ-11；先调研，未证明前不得声明支持。 |
| `Publish the Provider Adapter Kit and conformance example` | `0.5 Data Trust` | `enhancement`, `good first issue` | WQ-12；依赖 WQ-06 与 Checkpoint 0.5-A。 |
| `Enforce Local API v1 N-1 compatibility` | `0.6 RC` | `enhancement` | WQ-13；依赖 Checkpoint 0.5。 |
| `Audit canary intake and start the public beta` | `0.6 RC` | `enhancement`, `help wanted` | WQ-14；依赖 WQ-13，包含外部参与者协调。 |
| `Establish measured performance and lightweight-host budgets` | `0.6 RC` | `enhancement`, `help wanted` | WQ-15；先测量，语言迁移必须另立 ADR。 |
| `Run the 30-day no-telemetry external canary` | `1.0 Canary` | `enhancement`, `help wanted` | WQ-16；依赖 Checkpoint 0.6 RC。 |
| `Release OpenUsage Bar 1.0` | `1.0 Canary` | `enhancement` | WQ-17；依赖 WQ-16 全部通过。 |

Provider 接入需求由
`.github/ISSUE_TEMPLATE/provider_request.yml` 收集。它只接受公开来源、数据语义和
凭证类型说明，不接受密钥、Cookie、原始响应、Prompt、Response 或直接账号身份。

## Ruleset 草案

### `main` 分支

- 目标仅为默认分支 `main`。
- 禁止删除和非快进更新。
- 合并前要求当前 `CI / verify` 成功。
- 不允许管理员默认绕过；如 GitHub 套餐或现有发布流程要求 bypass，必须先记录具体
  actor、原因和最小权限，再单独审批。
- 启用后用临时分支验证普通 PR、Dependabot PR 和 release workflow，不用真实版本 Tag
  作为试验品。

### `v*` Tag

- 目标仅为 `v*`。
- 允许创建新 Tag；禁止更新或删除既有 Tag。
- 不移动失败的发布 Tag；修复后使用新的 patch/build 版本。
- Developer ID 公证不是开源发布门禁，Tag 保护也不得被用来绕过 checksum、manifest、
  SBOM、provenance 或 artifact attestation。

## 获得授权后的执行顺序

1. 先推送当前实现分支并让完整 CI 在远端当前代码上运行。
2. 创建四个 Milestone，再按上表创建 Issue 并设置依赖说明。
3. 发布 Provider 接入 Issue Form。
4. 修正 #11 的版本注释；让 #5、#10、#11 基于当前 `main` 重跑完整门禁。
5. 创建并复查 `main` 与 `v*` ruleset；从临时分支验证 required check 和 release workflow。
6. 只读复核 Issue、Milestone、ruleset、required checks 与 Tag 行为。
7. Dependabot 合并、Release 和外部 Canary 分别再次取得授权，不与上述配置动作捆绑。

以下远端动作必须分别获得明确授权：推送/开 PR、创建 Issue/Milestone、修改 ruleset、
更新或合并 Dependabot PR、发布 Release、协调外部 Canary。
