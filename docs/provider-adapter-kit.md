# Provider Adapter Kit

Provider Adapter Kit 是 OpenUsage Bar 的声明式 Provider 接入规范。它优先复用
现有通用适配器，不加载第三方 Python 代码，也不要求在 SwiftUI 中为每个厂商增加
`switch` 分支。

一个 Provider bundle 可以描述三类相互独立的事实：

| 模板 | 配置类型 | 事实 |
| --- | --- | --- |
| `quota.providers.json` | `generic` | 订阅容量、剩余比例、重置时间 |
| `daily-usage.providers.json` | `daily_usage_feed` | 每日输入、输出、缓存、推理及总 Token |
| `daily-cost.providers.json` | `daily_cost_feed` | 每日实际费用和币种 |

没有来源的事实必须保持 **Unknown**。覆盖完整、确认当天没有活动时才可以写零。
同一事实有多个来源时只选择一个有效来源，不能把官方数据与 fallback 相加。

## 快速开始

复制 `examples/provider-adapter-kit/example-provider`，保留目录结构并替换其中的
公开 endpoint、字段路径和合成 fixture。示例使用 `example.invalid`，不会连接真实
服务。

在仓库根目录运行：

```bash
python3 scripts/check_provider_adapter.py examples/provider-adapter-kit/example-provider
```

成功时命令只输出一行、无身份信息的 JSON 摘要。它不会发起网络请求，也不会读取
Keychain。

## Bundle 结构

```text
provider-name/
├── manifest.json
├── quota.providers.json
├── daily-usage.providers.json
├── daily-cost.providers.json
└── fixtures/
    └── conformance.json
```

- `manifest.json` 声明覆盖的 capability、运行时来源和全部 conformance case。
- 三个 `*.providers.json` 是可复用的 Feed v2 模板；每个文件只放一个合成账号。
- `fixtures/conformance.json` 只描述预期结果，不能包含真实响应或账号信息。
- 不支持某类事实时，贡献仍应保留对应模板并使用公开的最小合成映射；这可以证明
  该 bundle 没有把不同事实混为一谈。实际用户配置只启用有可信来源的模板。

## Feed v2 语义

所有模板使用配置 envelope `{"version": 2, "providers": [...]}`。凭证绝不能写进
配置；运行时通过 `provider_id` 从 macOS Keychain 读取。

### Quota

`primary_path` 指向厂商报告的主要值。只有单位确实是百分比时才配置
`remaining_percent_path`。`quota_window`、`quota_name` 和 `unit` 必须与官方
语义一致；消费量不能伪装成剩余量。

### Daily usage

输入、输出、cache read、cache creation、reasoning 和 total 是不同字段。
`total_tokens_path` 必须代表厂商定义的总量，不能在不清楚 cache 是否重复计数时自行
相加。日期查询必须由 `since_parameter` 与 `until_parameter` 约束。

### Daily cost

`amount_path` 和 `currency_path` 必须来自同一账单粒度。`cost_kind` 区分 actual、
estimated 等语义，`basis` 说明它是厂商报告值还是本地估算值。

## v1 迁移到 v2

1. 将旧列表放入 `version: 2` 的 `providers` envelope。
2. 为每个账号分配稳定但不含邮箱、姓名或组织名称的 `account_ref`。
3. 从配置中删除 API key、Cookie、session 和其他秘密。
4. 在 Provider Center 中新增或替换凭证，使其只进入 Keychain。
5. 补齐 bounded date、pagination 和字段映射。
6. 运行 conformance 命令；验证通过后再做单独的真实账号 canary。

静态 conformance 通过只证明 bundle 结构、安全边界和语义完整，不等同于真实账号
已经可用。

## Security checklist

- **Keychain**：配置和 fixture 中不得出现凭证；密钥只允许由应用写入 Keychain。
- **HTTPS**：endpoint 必须是无嵌入凭证、无 fragment 的 HTTPS URL。
- **redirect**：网络适配器必须限制 redirect host；不能跨站携带 Authorization。
- **pagination**：必须设置页大小、最大页数并检测 cursor/page 循环。
- **response size**：响应有硬上限；过大结果失败并保留 Last-good。
- **sanitized errors**：失败输出只能使用固定错误码，不能回显 URL、路径、payload 或身份。
- **Last-good**：认证失败、超时、限流、格式错误或空的未覆盖结果不得覆盖历史成功值。
- **Unknown**：缺失、不支持和未覆盖必须是 Unknown，不能以零代替。
- fixture 只能使用合成账号；不得提交 Cookie、原始响应、Prompt、Response、邮箱、
  本机用户路径或直接账号身份。

## Conformance matrix

每个 bundle 必须覆盖：

`success`、`empty_result`、`authentication_expiry`、`rate_limit`、`timeout`、
`malformed_response`、`oversized_response`、`pagination_loop`、
`partial_coverage`、`last_good_preservation`、`source_priority`、
`multi_account_isolation`、`secret_non_disclosure` 和 `unknown_not_zero`。

校验器会验证 case 集合、预期结果、合成多账号隔离、三类模板、唯一 Provider ID、
配置 schema 和敏感材料扫描。它只输出 bundle 名称、case 数量和模板类型。

## 贡献流程

1. 先搜索已有 OpenUsage 集成或官方只读 API；记录来源文档和数据语义。
2. 使用 [Provider 接入请求](../.github/ISSUE_TEMPLATE/provider_request.yml) 描述
   capability、区域、认证类型和覆盖范围，绝不粘贴凭证。
3. 从示例复制 bundle，不提交真实 payload。
4. 运行 Provider Adapter Kit 校验、Python/Swift 测试、release secret scan 和
   `scripts/build_app.sh`。
5. Pull Request 中分别报告静态 fixture 与真实账号 canary；没有 live canary 时明确
   标注 `fixture`，不能宣称已验证可用。

新增声明式 Provider 不需要修改中央 registry 的 `isinstance` 分支，也不需要新增
厂商专属 SwiftUI。只有通用协议获得新 capability 时，才应单独扩展共享适配器。
