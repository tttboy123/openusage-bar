# Gateway capability UX contract

- Status: Task 10 implementation contract
- Audience: Web/Electron UI, Electron main process, Python capability producer, macOS Swift UI, and test owners
- Platforms: macOS, Windows, and Linux Electron/Web; semantic parity with the existing read-only macOS Swift surface
- Evidence date: 2026-08-08

This contract freezes how OpenUsage Bar explains its optional Gateway without weakening the existing Observer product or exposing private runtime details. In this document, **MUST**, **MUST NOT**, and **SHOULD** are normative.

## 1. Scope and evidence

The audit evidence below is synthesized for this review and contains no real user data.

| Evidence | Finding that this contract addresses |
| --- | --- |
| `<local>/openusage-ui-audit/01-automation-current.png` | Automation currently presents one generic Local API “Available” state and renderer-visible commands containing a loopback host/port and a Unix socket path. Transport guessing and command construction must be removed from the renderer. |
| `<local>/openusage-ui-audit/02-data-health-current.png` | The existing panel, textual green pills, filter buttons, and source rows are a useful visual foundation. The page currently describes 15 Observer collection sources, but has no separate Observer/Gateway service status and could be misread as Gateway Provider health. |
| `<local>/openusage-ui-audit/03-data-health-expanded.png` | The existing disclosure row, timestamps, cause text, and inline actions are the interaction pattern to retain. Gateway actions must use fixed action IDs and localized, sanitized feedback rather than payload-provided links or errors. |
| `<local>/openusage-ui-audit/04-data-health-no-anomalies.png` | Selecting “Needs attention” when the issue count is zero leaves the content area blank. The filtered-empty state must remain visibly meaningful and offer “Show all sources.” |

The screenshots establish visible hierarchy and empty-state problems. They do not prove keyboard behavior, screen-reader output, zoom reflow, forced-colors behavior, or contrast ratios; those are explicit verification requirements below, not claims inferred from screenshots.

This contract builds on the current `.panel`, `.panel-head`, `.panel-body`, `.automation-grid`, `.health-list`, `.health-item`, `.pill`, `.icon-btn`, and button/link patterns. It uses the existing design tokens, including `--accent`, `--accent-soft`, `--warn`, `--bad`, surfaces, hairlines, radii, typography, and motion tokens. It introduces no new visual language.

## 2. Frozen user mental model

### 2.1 Two independent layers

1. **Observer is the product default.** It collects and presents local usage facts through the read-only Local API. It remains useful when Gateway is unsupported, off, starting, degraded, unavailable, malformed, or unknown.
2. **Gateway is an explicit opt-in.** A fresh install or upgrade stays in `observe`. The UI MUST NOT imply that Gateway is required to keep Observer working.
3. **Their failure domains remain separate.** A Gateway failure MUST NOT replace, hide, downgrade, or blank an available Observer snapshot. Last-good Observer data remains visible with its own freshness wording.
4. **No silent escalation.** Seeing, opening, or refreshing either page MUST NOT enable Gateway, start Provider forwarding, read Provider credentials, or clear cache.

The minimum reassurance attached to an off, degraded, unavailable, or unknown Gateway is: “Observer keeps working” / “Observer 不受影响”.

### 2.2 Three facts, never one inferred badge

Every Gateway feature has three independent layers:

| Layer | Allowed values | Meaning | MUST NOT be inferred from |
| --- | --- | --- | --- |
| Support | `supported`, `unsupported`, `unknown` | This platform/build has an implementation contract for the feature. | Adapter count, configuration, or a successful historical request. |
| Enablement/configuration | `enabled: true \| false \| unknown` and `configured: true \| false \| unknown` | The user explicitly enabled the feature and supplied the non-secret configuration it needs. These remain two separate facts. | Listener liveness, Provider health, or support. |
| Operational | `disabled`, `starting`, `ready`, `degraded`, `unavailable`, `unknown` | The current runtime fact reported by the owning process. | Support, enabled/configured flags, adapter presence, or a catalog entry. |

Examples:

- `supported` does not mean enabled.
- `enabled: true` does not mean configured.
- `configured: true` does not mean running.
- `operational: ready` may only come from an explicit runtime/liveness fact; the UI never upgrades another state to `ready`.
- `operational: disabled` is neutral when the user has not opted in. It is not an error.
- A missing or contradictory Gateway subtree becomes `unknown`; it never contaminates the Observer subtree.

### 2.3 Mode is intent, not health

| Mode | User-facing meaning | Forwarding behavior |
| --- | --- | --- |
| `observe` | Observer only. Gateway is off unless the user opts in. | No Gateway listener and no Provider request. |
| `advise` | Should-Send advice only. | `/gateway/v1/should-send` may operate; Provider requests are never forwarded. |
| `gateway` | Full opt-in Gateway mode. | Requests submitted directly to Gateway may be forwarded according to explicit configuration. |
| `unknown` | Mode could not be verified. | The UI makes no forwarding claim. |

Mode MUST be shown separately from operational state. “Gateway mode” does not mean “Gateway ready.”

## 3. Renderer-safe presentation contract

### 3.1 Normalized UI model

The Web client SHOULD normalize the wire snapshot once, before rendering. Component code consumes only this safe view model; it does not inspect runtime descriptors or loosely shaped payloads.

```ts
type Support = "supported" | "unsupported" | "unknown";
type KnownBoolean = boolean | "unknown";
type Operational =
  | "disabled"
  | "starting"
  | "ready"
  | "degraded"
  | "unavailable"
  | "unknown";
type GatewayMode = "observe" | "advise" | "gateway" | "unknown";

type FeatureId =
  | "listener"
  | "should_send"
  | "responses"
  | "cache"
  | "fallback"
  | "pii_redaction"
  | "streaming";

type CapabilityActionId =
  | "enable"
  | "disable"
  | "open_settings"
  | "retry"
  | "learn_more"
  | "clear_cache";
```

Each feature exposes `support`, `enabled`, `configured`, and `operational`. The page-level safe model also exposes only:

- Observer operational state and last-good/generated time;
- Gateway `mode` and overall operational state;
- the seven stable feature IDs above;
- configured Provider count and healthy Provider count as independently nullable facts;
- stable Provider/model catalog IDs only when a row genuinely represents measured health;
- whether cache is enabled, never a cache key or cached content;
- a fixed set of available action IDs;
- the last sanitized error as an allowlisted code, retryable flag, and optional timestamp;
- schema/API version and data revision when needed for compatibility display.

Unknown additive fields are ignored. An unknown enum becomes `unknown`. A malformed or oversized Gateway value produces an unknown Gateway card and a sanitized retry path while preserving a valid Observer card.

### 3.2 Fact validation and precedence

The normalizer MUST fail closed against optimistic health claims:

1. `support: unsupported` is displayed as “Not supported”; no enabled or operational claim is promoted above it.
2. `enabled: false` plus `operational: disabled` is the intentional “Off” state.
3. `enabled: true` plus `configured: false` is “Setup needed,” not “Failed.”
4. A contradictory combination such as `enabled: false` with `operational: ready`, `configured: false` with `operational: ready`, or `mode: observe` with a ready listener is displayed as `unknown` with Retry/Learn more. It is never repaired by client inference.
5. `starting`, `degraded`, `unavailable`, and `unknown` remain distinct. Loading and request error are client fetch lifecycle states, not operational values.
6. Missing counts display “—” and “Health not reported,” never zero.
7. `healthyProviderCount` MUST come from explicit recent Provider health facts. It MUST NOT come from the number of supported adapters, configured entries, catalog rows, or Observer sources.
8. Listener `ready` MUST come from explicit listener liveness. Configuration or process discovery alone is insufficient.
9. Streaming MUST NOT be shown as `ready` merely because adapters or `dispatch_events` exist. Until an end-to-end, closeable Provider event stream and backpressure/lifecycle proof exists, streaming remains `degraded` or `unsupported` according to the producer fact.

### 3.3 Renderer allowlist

Renderer-visible capability payloads, DOM, attributes, links, analytics-like events, console logs, error reports, and test snapshots may contain only the safe facts above plus localized copy.

The following MUST NOT cross into the renderer or be reconstructed there:

- host names, IP addresses, ports, base URLs, listener URLs, or endpoint origins;
- filesystem, home-directory, runtime-descriptor, Unix socket, named-pipe, token, database, or credential-store paths;
- Local API or Gateway bearer values, `Authorization` headers, cookies, API keys, credentials, Provider auth headers, or credential/account references;
- account refs, direct identity, environment variables, Provider request IDs, control IDs, tool-call IDs, cache keys, or raw headers;
- prompts, messages, request bodies, response bodies, `outputText`, Provider payloads, tool output, raw SSE/NDJSON chunks, or partial deltas;
- raw Provider errors, stack traces, exception strings, status bodies, or arbitrary error codes/messages supplied by an upstream;
- arbitrary action URLs, shell commands, file paths, arguments, or IPC channel names.

The intentionally private answer returned to an API caller is not a capability fact. It MUST NOT be copied into capability state, renderer logs, telemetry, diagnostics, or action feedback.

The current `buildCurlCommand`, browser-hostname transport guessing, and renderer-visible Local API command examples are therefore outside this contract and must be removed from Automation. The renderer uses relative, credential-free requests only. Electron main process owns discovery, token reads, bounded proxying, and response sanitization.

### 3.4 Sanitized error handling

The capability producer emits only a stable allowlisted code. The UI chooses the localized message; it never renders a payload-provided message or interpolates an unknown code into prose.

| Safe code | EN copy | 简体中文 copy |
| --- | --- | --- |
| `gateway_disabled` | Gateway is off. | Gateway 已关闭。 |
| `gateway_starting` | Gateway is still starting. | Gateway 仍在启动。 |
| `gateway_timeout` | Gateway did not respond in time. | Gateway 未及时响应。 |
| `gateway_unavailable` | Gateway is unavailable. | Gateway 当前不可用。 |
| `provider_unavailable` | A configured Provider is unavailable. | 一个已配置的 Provider 不可用。 |
| `credential_backend_unavailable` | Secure credential storage is unavailable. | 安全凭证存储不可用。 |
| `capability_invalid` | Gateway status could not be verified. | 无法确认 Gateway 状态。 |
| any absent/unknown value | Gateway needs attention. | Gateway 需要处理。 |

The stable code may appear as secondary diagnostic text only when the producer guarantees it is allowlisted. Unknown values use the generic copy and are not echoed.

## 4. Progressive disclosure by phase

Phase is derived from the explicit mode and enablement facts for presentation only. It does not replace the three fact layers.

| Phase | Automation disclosure | Data Health disclosure | Forbidden interpretation |
| --- | --- | --- | --- |
| Phase 0 — Observer-only | Observer card first. Gateway appears as a compact optional card: Off, Not supported, or Status unknown. Show Enable only when advertised by a trusted host; otherwise Learn more/Open settings. Hide inactive feature detail behind one disclosure. | Observer service and source health remain complete. Gateway Provider health is omitted or says “Not active in Observe mode”; it is not an empty/error list. | Do not call Observe failed. Do not start a listener, read Provider credentials, or call a Provider because the page opened. |
| Phase 1 — Advise | Show mode `Advise`, listener/Should-Send facts, and the reassurance “Requests are not forwarded.” Responses, cache, fallback, and forwarding-only features are neutral “Not active in Advise mode” when disclosed. | Show Observer independently. Show Gateway service status for advice. Provider forwarding health is “Not active,” not 0 healthy or failed. | A ready Should-Send route does not make Responses, Provider forwarding, or streaming ready. |
| Phase 2 — Gateway | Show full Gateway summary and feature disclosure: listener, Should-Send, Responses, cache, fallback, PII redaction, and streaming. Show configured and healthy Provider counts as separate explicit facts. | Show separate Observer service/source health and Gateway service/Provider health. Keep last-good Observer data when Gateway degrades. | Five adapters do not mean five configured Providers, five healthy Providers, or a live listener. |

## 5. Automation page contract

Automation remains the single Web/Electron entry in the existing sidebar. It uses existing panels and green accent; it does not add a new nav item, dashboard, hero, illustration, or visual system.

### 5.1 Order and hierarchy

1. **Observer status card**, always first.
   - Title: Observer.
   - Primary textual operational pill.
   - Safe facts: read-only, generated/last-good time, data revision, route count, and schema version when present.
   - An Observer fetch error affects this card only.
2. **Gateway status card**, explicitly labeled Optional.
   - Primary facts: Mode, operational state, listener state, configured Providers, healthy Providers, cache enabled state, and last sanitized error.
   - The one-line consequence always says whether request advice/forwarding is active and that Observer remains independent.
   - Primary and secondary actions follow the stable action table in section 7.
3. **Should-Send advice**, inside the existing Gateway panel.
   - It is a read-only decision aid, not an enable/disable/settings/cache action.
   - It is disabled in Observe, while capability state is loading/starting, or
     when Should-Send support, enablement, configuration, or Gateway
     availability cannot be verified.
   - Advise/Gateway users submit explicitly; opening or refreshing the page
     never requests advice. The form fixes the preview window at `5m`, rejects
     stale responses after an input/capability change, and explains that the
     current policy uses recorded quota and burn facts.
   - A null quota, burn, or prediction fact says “Insufficient local facts”; it
     is never rendered as zero. An exhaustion estimate is labelled as an
     estimate, never a deadline.
4. **Gateway capability disclosure**, inside the existing Gateway panel.
   - Reuse the current disclosure/list treatment rather than inventing tiles.
   - Ordered rows: Listener, Should-Send, Responses, PII redaction, Streaming, Cache, Fallback.
   - A collapsed row shows feature name and textual operational state. Expanded detail uses a `dl` for Support, Enabled, Configured, and Operational.
   - Inactive phase-specific rows say “Not active in this mode,” not “Failed.”
5. **Recent automation/change feed**, preserving its current role below service state.

The current “Read-only commands” panel is removed from renderer presentation. No replacement card may reveal a host, port, socket, bearer, token path, or shell command.

### 5.2 Automation fetch states

- Initial load: preserve both card shells, show text “Checking service status…” once, and use decorative skeletons. Do not temporarily render zero Providers or Gateway unavailable.
- Refresh: retain last-good facts, mark only the refresh action busy, and keep focus on the action.
- Observer success + Gateway error: render Observer normally and Gateway “Status unknown” or “Unavailable” from the safe result.
- Gateway success + Observer error: render Gateway safe facts, but never represent Gateway as a replacement for Observer data.
- No last-good data + request error: show the affected card with localized generic copy and Retry. Do not render the exception string.

## 6. Data Health page contract

Data Health answers two different questions and MUST label them separately:

1. **Are the local services available?** Observer and optional Gateway service cards.
2. **Are the data sources/Providers healthy?** Observer collection sources and, only when explicitly reported, Gateway Provider health.

`/v1/sources/status` continues to describe Observer collection sources. `/v1/providers` or a five-adapter catalog is not Gateway Provider health. Do not join those collections and infer liveness.

### 6.1 Service status

- Place Observer and Gateway service status before source rows, using existing card/panel styles.
- Observer is first and never disappears because of a Gateway error.
- Each card includes a textual state, one-sentence consequence, and safe Retry/Open settings/Learn more actions as applicable.
- The page-level summary names both layers when both are present, for example “Observer is ready. Gateway is unavailable; Observer data is still available.” It does not flatten them into a single red/green page state.
- Green/accent is reserved for explicitly `ready` or the positive filtered-empty result. Intentional Off/Not active/Unknown uses neutral treatment; Degraded uses warning; Unavailable uses bad/error treatment. Every treatment includes text, not color alone.

### 6.2 Observer source health

- Keep the current filter buttons, source list, issue-first sorting, disclosure rows, cause, last attempt, last success, stale-after facts, and safe settings/help actions.
- Rename/label the section so its count is clearly “Observer sources,” not Gateway Providers.
- Only allowlisted, sanitized source error codes map to localized cause copy. An unknown value uses generic “Source status could not be verified” and is never echoed.
- On refresh error with previous rows, retain the rows, label them as last-known, and show one retry notice. Do not replace the list with raw error text.

### 6.3 Gateway Provider health

- Show the section in Phase 2 only when the capability snapshot explicitly provides Provider health or configured count facts.
- `configuredProviderCount = 0` is a valid empty state: “No Gateway Providers configured.”
- A missing configured count is Unknown, not zero.
- A configured Provider row may show only stable Provider/model IDs, explicit health state, safe timestamps, and an allowlisted sanitized code.
- If only counts are available, show the counts. Do not fabricate rows.
- If configured count is greater than zero but health is absent, say “Provider health is not reported.” Do not claim all healthy or all failed.
- In Phase 0 or 1, Provider forwarding health is “Not active in this mode” and is not counted as a problem.

### 6.4 Complete state table

| State | Trigger | Required presentation | Actions |
| --- | --- | --- | --- |
| Loading | First request is pending and no last-good facts exist. | Card/list shells plus one “Checking service status…” status message. Skeleton is `aria-hidden`. No zero counts or unavailable claims. | None until an action is safe. |
| Ready/healthy | Explicit operational/health fact is ready. | Textual green/accent state and safe freshness/count facts. | Settings/Learn more only if advertised. |
| Empty | Explicit Observer source count or configured Gateway Provider count is zero. | Explain what has not started or been configured. Do not use error styling. | Retry and/or Open settings when advertised. |
| Filtered empty | “Needs attention” is selected and issue count is zero. | Keep a visible positive empty block: “No sources need attention.” Add a normal local “Show all sources” button immediately after it. | Show all sources. |
| Degraded | Explicit operational/health state is degraded; last-good data may exist. | Warning text states what is limited and keeps last-good Observer/source facts visible. | Retry, Open settings, Learn more as advertised. |
| Unavailable | A valid sanitized snapshot explicitly says unavailable. | Bad/error treatment on the affected card/row only; say Observer is unaffected when Gateway is the affected layer. | Retry/Open settings as advertised. |
| Unknown | Missing, unrecognized, contradictory, malformed, or oversized capability facts. | Neutral “Status unknown”; show “—” for unknown counts. Never coerce to Off, zero, or Unavailable. | Retry/Learn more as advertised. |
| Request error | Capability/source fetch itself failed. | Preserve last-good UI if available; otherwise generic localized error in the affected region. Never show raw exception text. | Retry. |
| Not active | Feature is intentionally inactive in Observe or Advise mode. | Neutral “Not active in this mode.” It does not increment issue counts. | Enable/Open settings/Learn more as advertised. |

The filtered-empty block fixes the blank state in `04-data-health-no-anomalies.png`. It remains inside the existing panel so the result reads as an intentional outcome rather than missing content.

## 7. Stable action contract

The snapshot advertises IDs only. It MUST NOT advertise a URL, path, command, argument, credential, IPC name, or free-form label. The renderer ignores unknown IDs. A trusted host maps a known ID to an allowlisted operation.

The local “Show all sources” filter control is renderer state, not a privileged capability action and is not serialized in the capability snapshot.

| Action ID | Visible when | Usable when | Execution and feedback |
| --- | --- | --- | --- |
| `enable` | Gateway support is `supported`, enabled is `false`, the ID is advertised, and a trusted host executor exists. | No Gateway action is in flight. | Opens a host-owned opt-in confirmation. Confirming sends only `{ id: "enable" }` through the trusted boundary. Pending: “Enabling Gateway…”; success refreshes capability; failure uses sanitized generic copy. Opening the page never triggers it. |
| `disable` | Gateway enabled is `true`, the ID is advertised, and a trusted host executor exists. | No Gateway action is in flight. | Confirmation states that Provider advice/forwarding stops and Observer keeps working. Pending/success/failure are localized. |
| `open_settings` | The ID is advertised for an off, setup-needed, degraded, unavailable, or user-requested configuration state. | A trusted, allowlisted settings destination exists. | Electron main/native host opens the fixed local settings surface. The payload cannot choose a route or external URL. Generic Web without that executor does not render the action. |
| `retry` | A card/list is degraded, unavailable, unknown, stale, or in request error and Retry is advertised or is a safe renderer GET refresh. | That region is not already refreshing. | Re-fetches only the allowlisted relative safe endpoints. Focus remains on Retry. It never retries a Provider prompt/request. Announces one result after completion. |
| `learn_more` | The ID is advertised, especially for Optional, Observe, Unsupported, or Unknown explanations. | The bundled help destination is available. | Opens a fixed bundled documentation key. No URL from capability data is used. |
| `clear_cache` | Cache support is `supported`, cache enabled is `true`, mode is `gateway`, the ID is advertised, and a trusted host executor exists. | No cache action is in flight. Runtime readiness is not guessed; the producer decides whether to advertise the action. | Requires confirmation: Observer data is not removed. Confirming sends only `{ id: "clear_cache" }`. Pending disables this action only; success says “Gateway cache cleared”; failure is sanitized. Generic Web never calls a mutation endpoint directly. |

Additional action rules:

- Mutating IDs (`enable`, `disable`, `clear_cache`) are hidden, not simulated, when no trusted executor exists. Learn more remains the fallback if advertised.
- During an action, keep its label visible, add busy copy, set `aria-busy="true"` on the affected region, and prevent duplicate submission.
- Canceling a confirmation returns focus to the invoking button and emits no action call.
- Success refreshes the capability snapshot before claiming the resulting operational state. “Enabled” success does not immediately claim “Ready.”
- Host action results contain only action ID, `succeeded | failed | cancelled`, optional allowlisted sanitized code, and optional safe timestamp.
- Renderer logs may record a stable UI event name and action ID/outcome only. They never log the capability object or host result wholesale.

## 8. EN/ZH short-copy contract

These keys are proposed additions to the existing `messages` object. English and Simplified Chinese keys MUST remain exactly paired.

| Key | English | 简体中文 |
| --- | --- | --- |
| `observerTitle` | Observer | Observer |
| `gatewayTitle` | Gateway | Gateway |
| `gatewayOptional` | Optional | 可选 |
| `serviceStatus` | Service status | 服务状态 |
| `observerSources` | Observer sources | Observer 数据源 |
| `gatewayProviders` | Gateway Providers | Gateway Provider |
| `capabilitySupport` | Support | 支持情况 |
| `capabilityEnabled` | Enabled | 已启用 |
| `capabilityConfigured` | Configured | 已配置 |
| `capabilityOperational` | Status | 运行状态 |
| `supportSupported` | Supported | 支持 |
| `supportUnsupported` | Not supported | 不支持 |
| `supportUnknown` | Support unknown | 支持情况未知 |
| `valueYes` | Yes | 是 |
| `valueNo` | No | 否 |
| `valueUnknown` | Unknown | 未知 |
| `operationalDisabled` | Off | 已关闭 |
| `operationalStarting` | Starting… | 正在启动… |
| `operationalReady` | Ready | 可用 |
| `operationalDegraded` | Limited | 部分可用 |
| `operationalUnavailable` | Unavailable | 不可用 |
| `operationalUnknown` | Status unknown | 状态未知 |
| `modeLabel` | Mode | 模式 |
| `modeObserve` | Observe | Observe |
| `modeAdvise` | Advise | Advise |
| `modeGateway` | Gateway | Gateway |
| `modeUnknown` | Unknown | 未知 |
| `observeModeHint` | Observer only; Gateway is off. | 仅使用 Observer；Gateway 已关闭。 |
| `adviseModeHint` | Advice only; requests are not forwarded. | 仅提供建议；不会转发请求。 |
| `gatewayModeHint` | Submitted requests may use Gateway. | 主动提交的请求可使用 Gateway。 |
| `gatewayOptionalHint` | Gateway is optional. Observer keeps working. | Gateway 为可选功能；Observer 保持工作。 |
| `gatewayDegradedHint` | Gateway is limited. Observer data is still available. | Gateway 部分可用；Observer 数据仍可使用。 |
| `gatewayUnavailableHint` | Gateway is unavailable. Observer is unaffected. | Gateway 当前不可用；Observer 不受影响。 |
| `gatewayUnknownHint` | Gateway status could not be verified. Observer is unaffected. | 无法确认 Gateway 状态；Observer 不受影响。 |
| `setupNeeded` | Setup needed | 需要配置 |
| `notActiveInMode` | Not active in this mode | 当前模式未启用 |
| `checkingServiceStatus` | Checking service status… | 正在检查服务状态… |
| `serviceRefreshFailed` | Service status could not be refreshed. | 无法刷新服务状态。 |
| `providerCount` | {healthy} of {configured} Providers healthy | {configured} 个 Provider 中 {healthy} 个健康 |
| `providerHealthUnknown` | Provider health is not reported. | 未报告 Provider 健康状态。 |
| `noGatewayProviders` | No Gateway Providers configured. | 尚未配置 Gateway Provider。 |
| `noSourceIssues` | No sources need attention. | 没有需要处理的数据源。 |
| `showAllSources` | Show all sources | 显示全部数据源 |
| `sourceStatusUnknown` | Source status could not be verified. | 无法确认数据源状态。 |
| `enableGateway` | Enable Gateway | 启用 Gateway |
| `enablingGateway` | Enabling Gateway… | 正在启用 Gateway… |
| `disableGateway` | Disable Gateway | 关闭 Gateway |
| `openGatewaySettings` | Open settings | 打开设置 |
| `retryGatewayStatus` | Retry Gateway status | 重试 Gateway 状态 |
| `learnMoreGateway` | Learn more | 了解更多 |
| `clearGatewayCache` | Clear Gateway cache | 清除 Gateway 缓存 |
| `clearingGatewayCache` | Clearing Gateway cache… | 正在清除 Gateway 缓存… |
| `gatewayCacheCleared` | Gateway cache cleared. | Gateway 缓存已清除。 |
| `actionFailed` | The action could not be completed. | 无法完成此操作。 |
| `enableGatewayConfirm` | Enable the optional Gateway? Observer will keep working independently. | 启用可选 Gateway？Observer 将继续独立工作。 |
| `disableGatewayConfirm` | Disable Gateway? Observer will keep working. | 关闭 Gateway？Observer 将继续工作。 |
| `clearGatewayCacheConfirm` | Clear the Gateway cache? Observer data will not be removed. | 清除 Gateway 缓存？不会删除 Observer 数据。 |

Copy rules:

- Keep `Observer`, `Gateway`, Provider, Should-Send, API, and technical mode names consistent across Electron/Web and Swift.
- Do not say “Connected” for support, configuration, or adapter presence. Use it only if an explicit connection state contract exists.
- Do not use “All healthy” when configured or health counts are unknown.
- Do not interpolate a raw/surprising error code into `healthUnknown`-style copy.
- Action labels state the object, for example “Retry Gateway status,” so accessible names remain clear outside visual context.

## 9. Keyboard, focus, accessibility, and responsive requirements

### 9.1 Semantics and status announcements

- Each Observer/Gateway card is a labeled `section` with a heading. Facts use `dl`, `dt`, and `dd`; status is visible text. Decorative icons and status dots are `aria-hidden="true"`.
- Status pills do not rely on green/amber/red alone. The full textual state is in the accessibility tree.
- The page has at most one polite, atomic live region for user-initiated loading/action completion. Do not put `aria-live` on every card, Provider row, timestamp, or poll update.
- Initial skeletons are `aria-hidden="true"`; one status message says what is loading. Routine background polling does not announce unless the user must act.
- A user-triggered action failure may use `role="alert"` once. Initial/background fetch failures use the polite status region so repeated polling does not interrupt.
- Accordion buttons retain `aria-expanded` and point with `aria-controls` to an existing unique element. DOM IDs are derived from stable sanitized IDs, never account refs or private paths.
- The All/Needs attention filter is a named group of real buttons with `aria-pressed`; it is not presented as tabs unless full tab keyboard semantics are implemented.

### 9.2 Keyboard and focus

- All controls work with Tab/Shift+Tab and Enter/Space. Escape closes a confirmation dialog.
- Refresh/filter actions preserve focus on the invoking control. Refresh does not move focus to the first changed row.
- A confirmation traps focus, starts on its least destructive safe control, and returns focus to the invoker on cancel or completion.
- If a refreshed row that held focus disappears, move focus to the list heading/summary with a polite announcement; never drop focus to `body`.
- Expanded content follows its disclosure button in DOM order. Collapsing a row leaves focus on the disclosure button.
- Do not disable a focused filter just because its count becomes zero; show the filtered-empty result.

### 9.3 Contrast and targets

- Text meets WCAG AA contrast: at least 4.5:1 for normal text and 3:1 for large text. Status boundaries, controls, and visible focus indicators meet at least 3:1 against adjacent colors.
- Existing green accent is used for ready/positive states and focus, but meaning is always duplicated in text and shape/border.
- Forced-colors/high-contrast mode preserves card boundaries, button borders, textual state, and focus with system colors. Soft background color alone is insufficient.
- Interactive targets SHOULD remain at least 40 × 40 CSS px to match current controls and MUST be at least 24 × 24 CSS px with adequate spacing. Small text links receive padded hit areas.

### 9.4 Reflow and motion

- At 320 CSS px, cards, fact grids, filters, actions, timestamps, and Provider IDs wrap into one column without page-level horizontal scrolling. Long stable IDs may break safely; they are not truncated without an accessible full name.
- At 200% browser zoom, no status, action, error recovery, or count is clipped or hidden; reading order remains meaningful.
- Action groups wrap. Fixed spacers must not push Retry or empty-state recovery off screen.
- `prefers-reduced-motion: reduce` removes spinner rotation, accordion animation, pulsing status indicators, and transition-dependent feedback. Text still changes immediately.
- Status updates do not flash or animate continuously. Loading is communicated by copy even when motion is absent.

## 10. Cross-platform parity and host boundary

| Surface | Required parity | Permitted difference |
| --- | --- | --- |
| macOS/Windows/Linux Electron/Web | Same normalized fixtures, state vocabulary, phase rules, hierarchy, copy keys, empty/error behavior, accessibility semantics, and privacy allowlist. | Only action availability may differ when the trusted host explicitly does or does not advertise an executor. OS-specific host/path text is never shown. |
| Standalone Web renderer | Same read-only status semantics and safe relative reads. | Mutating actions are absent without a trusted host executor. Lack of a bridge is Unknown unless the capability contract explicitly says Unsupported. |
| Existing macOS Swift surface | Same Observer-first mental model, state meanings, localized short copy, non-color cues, and failure isolation. It remains read-only for Task 10. | No new Gateway mutation UI is required in this task. Semantic parity does not require identical layout or Electron-only action placement. |

Electron main process and Python own all secrets, discovery, authenticated loopback calls, settings mutations, service control, and cache mutation. Renderer requests remain credential-free and relative. Provider mutations stay on the private Python command boundary.

The one renderer-visible write-shaped exception is
`POST /gateway/v1/should-send`. It is semantically read-only: the Electron main
process accepts only a bounded four-field JSON contract, injects the private
Gateway token, and rebuilds the closed advice response. It never forwards a
Provider request and never exposes upstream headers, raw errors, private paths,
credentials, or unknown response fields.

## 11. UI Agent implementation acceptance checklist

- [ ] Add one strict capability normalizer/view model; components do not consume raw runtime descriptors or arbitrary Gateway health objects.
- [ ] Preserve the Observer snapshot when Gateway is missing, malformed, oversized, unauthorized, timed out, or returns 5xx.
- [ ] Remove browser-hostname transport guessing, renderer-built curl/helper commands, host/port/socket display, and any bearer-capable command from Automation.
- [ ] Render Observer first and Gateway as Optional; show mode separately from Support, Enabled, Configured, and Operational.
- [ ] Reuse existing panels, pills, disclosure rows, tokens, green accent, warning/bad colors, typography, radii, and spacing.
- [ ] Implement Phase 0/1/2 disclosure without marking Observe or Advise-only disabled features as failed.
- [ ] Show listener, Should-Send, Responses, cache, fallback, PII redaction, and streaming only from explicit facts; never infer ready from adapter/configuration presence.
- [ ] Label existing source rows as Observer sources and add separate Gateway Provider health only from explicit Provider health facts.
- [ ] Implement visible loading, empty, filtered-empty, degraded, unavailable, unknown, request-error, and not-active states.
- [ ] Ensure “Needs attention” with zero issues renders “No sources need attention” and a focusable “Show all sources” control.
- [ ] Implement only the six stable host action IDs; ignore unknown IDs and never accept payload targets/arguments.
- [ ] Route enable/disable/settings/cache mutation through a trusted host executor; generic Web does not simulate them with fetch.
- [ ] Localize every new string with exact EN/ZH key parity; never show raw exceptions or unknown error strings.
- [ ] Preserve keyboard focus across refresh/filter/action completion and implement the single restrained live-region policy.
- [ ] Verify non-color cues, focus contrast, 40 px target preference, 320 px reflow, 200% zoom, dark/forced-colors, and reduced motion.
- [ ] Scan renderer payloads, DOM, attributes, logs, test snapshots, and action results against the forbidden-data list.
- [ ] Compare macOS/Windows/Linux fixtures for semantic parity and confirm the existing Swift Observer surface remains read-only and unaffected.

Implementation evidence, 2026-08-09: deterministic contracts cover the strict
capability/advice normalizers, Observe/Advise/Gateway availability gating,
explicit submit, stale-result suppression, EN/ZH parity, narrow reflow,
forced-colors/reduced-motion CSS, the credential-free renderer request, private
main-process token injection, response allowlisting, integer-lexeme parity,
non-200 rejection, and bounded partial-write closure. Web passes 80/80 plus its
production build and Desktop passes 44/44. Real-browser keyboard, 320 px, 200%
zoom, forced-colors, reduced-motion, and native Windows/Linux package evidence
remain open and are not inferred from static contracts.

## 12. Test Agent automatable assertions

### 12.1 State and inference assertions

1. Render fixtures for `observe`, `advise`, `gateway`, and unknown mode with all operational values. Assert the exact localized textual state and that mode and operational state occupy separate fields.
2. In Observe with Gateway disabled, assert neutral “Off/Optional,” no failure count, no Provider call, and a still-ready Observer card.
3. In Advise, assert “Requests are not forwarded,” Should-Send may use its explicit state, and Responses/cache/fallback/Provider forwarding do not render Ready by association.
4. In Gateway degraded/unavailable, assert Observer last-good data and source rows remain visible and the page summary names the isolated Gateway problem.
5. Give the fixture five adapters, zero explicitly configured Providers, unknown healthy count, and a non-live listener. Assert the UI never contains “5 healthy,” “5 configured,” or Listener Ready.
6. Omit Provider counts. Assert “—/Provider health is not reported,” not `0` or “All healthy.”
7. Feed contradictory, malformed, additive-unknown, and oversized Gateway values. Assert Gateway becomes Unknown, unknown fields are absent, and Observer still renders.
8. Mark streaming adapters present while the end-to-end stream fact is degraded/unsupported. Assert Streaming is not Ready.

### 12.2 Empty, loading, and error assertions

1. During first load, assert one loading status message, decorative hidden skeletons, and no transient “0 Providers” or Unavailable copy.
2. With zero Observer sources, assert the intentional empty explanation and Retry when available.
3. With zero configured Gateway Providers, assert “No Gateway Providers configured,” not an error.
4. Select Needs attention with zero issues. Assert a non-empty result block, “No sources need attention,” and a working Show all sources button that restores rows and keeps focus predictable.
5. Fail a refresh after last-good data. Assert rows remain, a localized retry notice appears, and the injected raw exception sentinel does not.
6. Fail initial loading without last-good data. Assert only localized generic copy and Retry in the affected region; the other service card remains independently renderable.

### 12.3 Action assertions

1. For every stable action fixture, assert visibility and enabled state match section 7. Unknown action IDs render no control and invoke nothing.
2. Assert `enable`, `disable`, and `clear_cache` are absent without a trusted executor even if an untrusted payload advertises them.
3. Confirming a mutation sends exactly one object containing the action ID and no target, URL, path, command, arguments, or capability snapshot. Cancel sends nothing and returns focus.
4. Assert Retry uses only the allowlisted relative safe GET requests, never a Provider request or credential mutation.
5. Assert Clear cache requires confirmation, does not remove Observer UI, disables only duplicate cache action while pending, and announces one sanitized outcome.
6. Assert action success refreshes capability before Ready is shown; an enable success alone can remain Starting.

### 12.4 Renderer privacy assertions

Inject unique canary strings into every forbidden fixture field and assert none appears in:

- normalized capability output;
- renderer request headers or bodies;
- rendered text, HTML, attributes, `href`, `src`, or datasets;
- console/warning/error output;
- action invocation/result payloads;
- serialized test snapshots or accessibility trees.

Canaries cover host, port, URL, socket/named-pipe/token/database/home paths, Local/Gateway bearer values, authorization/cookies/API keys, Provider headers, credentials, account refs, request/control/tool-call IDs, cache keys, prompts, request bodies, response/output text, response bodies, raw chunks/deltas, and raw Provider error/stack text. Use sentinel values rather than broad word scans so legitimate labels such as “Token usage” elsewhere do not create false positives.

### 12.5 Accessibility, i18n, and platform assertions

1. Assert each service card has a unique accessible heading; each disclosure button has valid `aria-expanded`/`aria-controls`; status icons are decorative; status text remains in the accessibility tree.
2. Assert exactly one restrained polite live region for status/action feedback and no live region on each row or polling timestamp.
3. Keyboard-test filter, disclosure, Retry, and confirmation actions with Tab, Shift+Tab, Enter/Space, and Escape; assert focus return/preservation rules.
4. At 320 CSS px and at 200% zoom, assert `scrollWidth <= clientWidth` for the page and that all status/action text remains visible and operable.
5. With reduced motion, assert spinner/pulse/accordion motion is absent while loading and state copy remains present.
6. In dark and forced-colors modes, assert textual state and visible focus/borders remain discernible; run contrast checks against the existing tokens.
7. Assert every new English key has the same Chinese key and placeholder set, and render representative long Chinese/English states without clipping.
8. Render the same sanitized fixture for `darwin`, `win32`, and `linux`; assert identical semantic state/copy/action IDs except explicitly advertised host action availability, and assert no OS path/transport text appears.
