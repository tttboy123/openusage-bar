# Provider support

OpenUsage Bar separates four different facts that provider dashboards often
mix together:

| Fact | Meaning |
|---|---|
| Detection | A provider or local client is installed or configured |
| Token activity | Daily input/output/cached Token totals, optionally by model |
| API spend | Provider-reported billed cost or a clearly marked estimate |
| API balance | Available currency balance reported by a provider |
| Subscription capacity | Remaining plan quota and authoritative reset time |

Detection never implies that the other three facts are available. Missing data
is shown as unavailable, not zero.

## Built-in adapters

These adapters fill gaps that OpenUsage does not currently expose:

| Provider | Available facts |
|---|---|
| Codex | Local subscription windows and resets; incremental local session logs are the primary daily Token source, with OpenUsage as fallback |
| Cursor | Remaining subscription percentage from OpenUsage auto discovery, with targeted OpenUsage direct-mode enrichment when auto lacks quota |
| Kiro | AWS CodeWhisperer plan quota and reset when Keychain credentials allow it; OpenUsage fallback |
| MiniMax | China and International Coding Plan capacity; delayed daily model billing activity only where a separately verified feed exists |
| Moonshot / Kimi API | China and International official API balance, kept separate from Token history and subscription capacity |
| StepFun Step Plan | China and International plan capacity from a Keychain session; API-key connection state |
| OpenAI Organization | Official daily Token activity and billed organization cost using an Admin key |
| Generic HTTPS Provider | Configured remaining-capacity fact from a bounded HTTPS JSON endpoint |
| Custom Daily Token Feed | Configured daily/provider/model Token history from a bounded HTTPS JSON feed |

Connection-specific notes:

- **OpenAI Organization** supports multiple connections when each organization
  uses a unique Provider ID and opaque account scope. It uses an Admin API key
  for official daily usage and billed organization cost; it does not expose
  ChatGPT or Codex subscription quota. Credentials stay in Keychain and
  failures preserve the last-good ledger. Token activity comes from official
  daily completions usage; billed cost comes from the official organization
  costs endpoint. Per the
  [OpenAI Usage API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage),
  input includes cache reads and cache writes. OpenUsage Bar records both cache
  components separately but does not add them to Total a second time. Usage and
  cost health are tracked as separate sources, and rows are committed only after
  every required cursor page validates.
- **Cursor** uses the Cursor CLI directory only inside a credential-free child
  process environment. OpenUsage `auto` remains the primary snapshot; when it
  finds Cursor but does not return capacity, one bounded `direct` export may
  replace only the Cursor card. The two modes are alternatives, never summed.
  A failed or empty direct result preserves the auto activity and cached
  last-good quota, while repeated failures enter bounded exponential backoff.
  Cursor currently exposes no verified reset timestamp, so reset remains
  unavailable rather than being inferred.
- **Kiro** reads the existing social-login item through the fixed macOS
  `security find-generic-password` command. The reader is bounded, shell-free,
  service-scoped, and never refreshes, rewrites, or logs a token. The validated
  region from the profile ARN selects one allowlisted
  `q.<region>.amazonaws.com` host for the official CodeWhisperer quota request.
  Its billing-cycle credits, remaining capacity, plan label, and reset time
  replace the OpenUsage consumption card only after a complete successful
  response. Authentication, network, or parsing failure preserves OpenUsage
  activity and cached last-good quota as stale data. On 2026-07-30, a bounded
  offline acceptance run also returned non-empty per-model Kiro Token history
  through the OpenUsage source. The run retained no Token values, paths,
  account identity, prompts, responses, or raw JSON. This verifies only the
  local Token-activity path; the AWS quota remains a separate official fact.
- **StepFun Step Plan** supports China and International accounts, but a web
  session is never retried against the other region. Follow the
  [StepFun quick start](stepfun-quick-start.md) for the safe connection flow.
- **MiniMax** locks every account to either China (`www.minimaxi.com`) or
  International (`www.minimax.io`) and never retries a credential against the
  other site. Both sites use their documented Coding Plan capacity endpoint.
  The delayed daily model billing feed is a separate, experimental China-only
  source because no equivalent International feed has been verified. Missing
  or incomplete billing coverage remains unavailable instead of becoming a
  real-time zero.
- **Moonshot / Kimi API** uses the fixed official balance endpoint for the
  selected China or International connection. It exposes currency,
  `available`, `voucher`, and `cash` through `/v1/balances` and the snapshot
  balance array. It does not invent daily Token history, a remaining
  percentage, or a reset time.
- **Custom Daily Token Feed** accepts only bounded, range-aware HTTPS JSON. It
  rejects embedded credentials, cross-host redirects, private-address targets,
  executable templates, and ambiguous partial pagination.

## OpenUsage-reused catalog

OpenUsage Bar reuses the released OpenUsage JSON boundary for these 35 catalog
families instead of copying its collectors:

- **Subscriptions:** Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot,
  Kiro, OpenCode.
- **API providers:** Alibaba Cloud, Anthropic, Azure OpenAI, DeepSeek, Gemini
  API, Groq, Mistral, Moonshot, OpenAI, OpenRouter, Perplexity, xAI, Z.AI.
- **Local tools:** Amp, Codebuff, Crush, Droid, Goose, Hermes, Kilo Code, Kimi
  CLI, Mux, Ollama, OpenClaw, Pi, Qwen CLI, Roo Code, Zed.

MiniMax and StepFun are additional OpenUsage Bar families, bringing the
version-one catalog to 37 families. Actual data depends on the installed
OpenUsage version, local clients, provider authentication, and what each
upstream source can authoritatively report.

GLM、Kimi 与 Qwen 的官方接口边界、区域、认证和后续实现决策记录在
[权威数据源核验](provider-authoritative-sources.md)；搜索别名和单次推理
`usage` 不代表历史用量或订阅额度已经可用。

Each catalog source also declares the fact families it can provide, whether
the data is provider-official, provider-local, third-party-derived, or
user-supplied, its account and model scope, and one of four verification
levels:

| Verification | Meaning |
|---|---|
| `live_account` | The adapter path passed a sanitized real-account acceptance run |
| `fixture` | Hermetic fixtures pass, but a real account is not yet recorded |
| `upstream_declared` | The released upstream catalog declares the source |
| `unverified` | No stronger reusable evidence exists yet |

This verification describes adapter evidence, not current connection health.
Current health, freshness, and sanitized errors come from
`/v1/sources/status`. Provider Center renders the same canonical evidence as
`/v1/capabilities`; it does not maintain a second hand-written matrix.

The catalog also requires every supported Token, capacity, balance, or spend
capability to name a matching source fact. OpenUsage-backed billing facts are
third-party price-table estimates unless their cost records say otherwise;
they are not promoted to Provider-official invoices.

### Local client evidence

On 2026-07-29, a bounded, offline OpenUsage daily export returned non-empty
per-model Token history for Claude Code, OpenCode, Hermes, and OpenClaw on this
macOS machine. The acceptance run retained only provider-level coverage facts;
it did not record paths, account identity, prompts, responses, raw payloads, or
exact personal usage totals.

That evidence proves the OpenUsage Token-history path, not subscription
capacity. All four families still declare unknown quota windows and their
OpenUsage source exposes only detection and Token activity. Consequently they
cannot create Capacity rows unless a separate authoritative quota source is
added later. OpenUsage cost values attached to these local histories are
price-table estimates, not billed subscription charges.

An empty or failed daily export remains a Source Health issue and cannot replace
last-good rows with zero. An unattributed model remains `unknown` inside its
original Provider scope; OpenUsage Bar never moves it to another client or
vendor.

The locally installed exporter reports a development build. `live_account`
therefore records that the current adapter path passed a real local acceptance
run; it is not a compatibility guarantee for every historical OpenUsage
release.

### Provider discovery names

The catalog carries public search aliases so common product names remain easy
to find without inventing a second Provider identity:

| Search name | Canonical families |
|---|---|
| GLM / Zhipu / 智谱 | Z.AI (`zai`) |
| Kimi | Kimi CLI (`kimi_cli`) and Moonshot API (`moonshot`) |
| Claude | Anthropic API (`anthropic`) and Claude Code (`claude_code`) |
| Qwen / 通义千问 | Alibaba Cloud API (`alibaba_cloud`) and Qwen CLI (`qwen_cli`) |
| OpenCode | OpenCode (`opencode`) |
| Grok | xAI (`xai`) |

Aliases are discovery metadata only. They never reclassify a configured
instance: the selected or collected `familyId` remains the sole capability and
source boundary.

## Adding another provider

Use this order:

1. Reuse a released OpenUsage JSON source when it already supplies the fact.
2. Use an official read-only provider endpoint when OpenUsage lacks the fact.
3. Configure Generic HTTPS Provider or Custom Daily Token Feed when a stable
   JSON endpoint exists.
4. Develop a built-in adapter only when the first three paths cannot preserve
   correct quota, billing, or Token semantics.

## Daily Token source selection

OpenUsage Bar selects one effective source for each Provider/account range; it
does not sum overlapping official and OpenUsage rows:

1. Use the official daily usage response when it succeeds.
2. If the official source fails, try `openusage.daily` with a 60-second process
   timeout and mark accepted rows as `quality=fallback`.
3. If OpenUsage fails or returns no model rows, preserve last-good rows and mark
   source health stale/temporarily unavailable.
4. If no source has ever succeeded, report missing data rather than numeric zero.

Codex and local clients without an official daily Token endpoint start at step
2, so their OpenUsage rows retain their native quality rather than being
mislabelled as an official fallback. API records expose `sourceId`, `quality`,
and `importedAt`; Usage Details shows the same provenance in chart details.

Every new adapter must use Keychain for credentials, fixed or validated HTTPS
destinations, bounded requests, sanitized errors, last-good data, and explicit
coverage. It must never store prompts, responses, raw provider payloads, or
direct account identity in the ledger or API.
