# Codex Unknown Attribution Compatibility Note

Date: 2026-07-17

## Reuse assessment

- OpenUsage 0.23.0 remains the Token and cost parser. OpenUsage Bar continues to
  call only `openusage daily --json --breakdown --offline` and does not copy or
  import OpenUsage source code.
- The released OpenUsage CLI has no Codex session export. Its `session` command
  is Claude Code-only, while its Codex daily output can contain `(unknown)` for
  Token events that precede the first model context.
- Codex's existing local JSONL already contains the missing authoritative model
  evidence in `turn_context.payload.model`; no new credential or network source
  is required.

## Minimal local compatibility layer

`CodexAttributionResolver` reads only event type, timestamp, and model name. It
does not retain prompts, responses, Token payloads, paths, or session IDs.

An Unknown daily row is reassigned only when every relevant pre-context Token
event can be traced to sessions that each contain exactly one explicit model,
and every such session for that local day agrees on the same model. A model
switch, a session without model evidence, an incomplete scan, invalid data, or
disagreement leaves the row as `unknown`.

The compatibility layer changes attribution only. It conserves the exact Token
field totals and keeps `source_id=openusage.daily` because OpenUsage remains the
source of the measured Token counts.

## Local verification

The current machine contained 26 relevant sessions across July 11, 14, 15, and
16. All 26 independently resolved to `gpt-5.6-sol`. The repaired daily import
removed the Unknown rows on those days without changing the overall Token sum.

## Deletion condition

Remove this compatibility layer after the supported OpenUsage release both:

1. attributes pre-context Codex Token events using later single-model session
   evidence; and
2. passes the captured OpenUsage 0.23 contract and the conservative single-model,
   multi-model, no-model, and disagreement fixtures in this repository.
