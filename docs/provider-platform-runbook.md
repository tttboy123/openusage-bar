# Provider Platform Runbook

## OpenAI Organization

OpenAI Organization is the first official API provider that writes both daily token activity and provider-reported billed cost into the native ledger. It does not expose ChatGPT or Codex subscription quota.

### Connect

1. Open **Add Provider** from OpenUsage Bar.
2. Choose **OpenAI Organization**.
3. Enter an account label and an OpenAI Admin API key. A read-only Admin key is recommended.
4. Save. The credential is stored in macOS Keychain under service `com.lune.openusage-menubar` and account `openai`; provider JSON contains no credential.

Only one canonical OpenAI Organization connection is supported in this version. Remove the existing `openai` provider before adding a replacement.

### Data semantics

- Token activity comes from `GET /v1/organization/usage/completions`, grouped by model in UTC daily buckets.
- Billed cost comes from `GET /v1/organization/costs`, aggregated independently for each UTC day and currency.
- Cached input is a subset of input tokens. Total Token is input plus output and does not add cached input twice.
- All cursor pages must finish and validate before rows or coverage are committed.
- Usage and cost have separate source health: `openai.organization.usage` and `openai.organization.costs`.
- After the first official usage success, a later failure preserves last-good official rows. OpenUsage is used only as a cold-start fallback before any official success.

### Inspect

```sh
openusage-bar usage --from 2026-07-01 --to 2026-07-16 --provider-ids openai
openusage-bar costs --from 2026-07-01 --to 2026-07-16 --provider-ids openai
openusage-bar sources
```

The local API exposes the same ledger through `/v1/activity/daily`, `/v1/costs/daily`, and `/v1/sources/status`. Missing coverage means unknown, not zero; covered days with no cost row are known zero.

### Failure handling

- `auth_required`: the Keychain entry is missing or unreadable.
- `auth_rejected`: OpenAI rejected the Admin key.
- `rate_limited`: the official API returned a rate limit.
- `invalid_response`: pagination or a daily bucket failed strict validation.
- `network_error`: the bounded fixed-host request failed.

Errors are stored as stable codes only. Keys, cursors, organization IDs, project IDs, response bodies, and account emails are not written to the ledger or diagnostics.
