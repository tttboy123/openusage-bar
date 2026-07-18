# Provider support

OpenUsage Bar separates four different facts that provider dashboards often
mix together:

| Fact | Meaning |
|---|---|
| Detection | A provider or local client is installed or configured |
| Token activity | Daily input/output/cached Token totals, optionally by model |
| API spend | Provider-reported billed cost or a clearly marked estimate |
| Subscription capacity | Remaining plan quota and authoritative reset time |

Detection never implies that the other three facts are available. Missing data
is shown as unavailable, not zero.

## Built-in adapters

These adapters fill gaps that OpenUsage does not currently expose:

| Provider | Available facts |
|---|---|
| Codex | Local subscription windows and resets; OpenUsage is the primary daily Token source |
| Cursor | Remaining subscription percentage when the local client exposes it; OpenUsage fallback |
| Kiro | AWS CodeWhisperer plan quota and reset when Keychain credentials allow it; OpenUsage fallback |
| MiniMax | Coding Plan capacity plus delayed daily model billing activity when the selected site supplies it |
| StepFun Step Plan | China and International plan capacity from a Keychain session; API-key connection state |
| OpenAI Organization | Official daily Token activity and billed organization cost using an Admin key |
| Generic HTTPS Provider | Configured remaining-capacity fact from a bounded HTTPS JSON endpoint |
| Custom Daily Token Feed | Configured daily/provider/model Token history from a bounded HTTPS JSON feed |

Connection-specific notes:

- **OpenAI Organization** accepts one canonical connection in this release. It
  uses an Admin API key for official daily usage and billed organization cost;
  it does not expose ChatGPT or Codex subscription quota. Credentials stay in
  Keychain and failures preserve the last-good ledger. Token activity comes
  from official daily completions usage; billed cost comes from the official
  organization costs endpoint. Cached input is treated as part of input tokens
  and is not added twice. Usage and cost health are tracked as separate
  sources, and rows are committed only after the required cursor pages validate.
- **StepFun Step Plan** supports China and International accounts, but a web
  session is never retried against the other region. Follow the
  [StepFun quick start](stepfun-quick-start.md) for the safe connection flow.
- **MiniMax** keeps documented Coding Plan capacity separate from delayed
  platform billing activity. Missing or incomplete billing coverage remains
  unavailable instead of becoming a real-time zero.
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
