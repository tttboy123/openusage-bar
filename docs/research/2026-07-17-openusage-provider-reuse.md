# OpenUsage provider reuse verification

## Decision

Claude Code, OpenCode, Kimi CLI, Gemini CLI, Qwen CLI, Z.AI, and Moonshot use
the released OpenUsage CLI JSON boundary before any dedicated parser is built.
OpenUsage Bar owns only validation, source selection, ledger persistence,
visibility, health, query APIs, and presentation.

No OpenUsage source code, Go package, credential store, or parser is copied into
this repository.

## Local verification

OpenUsage `0.23.0 (3059f1b)` was found at `~/.local/bin/openusage`.

On this Mac it detected and exported Claude Code, Codex, DeepSeek, Hermes, Kiro,
and OpenClaw. OpenCode, Kimi CLI, Gemini CLI, and Qwen CLI were not installed or
configured, and therefore produced no runtime cards. This is the intended
absence behavior; catalog support must not fabricate a configured instance or
zero usage.

## Product boundary

- The checked-in Provider Catalog is the single source of display name,
  category, metric capabilities, source strategy, and platform support.
- A detected OpenUsage snapshot creates a runtime Provider instance.
- `openusage daily --json --breakdown --offline --provider <id>` supplies daily
  model activity through one bounded importer shared by all catalog families.
- Empty daily output remains an empty covered result; command/auth/format errors
  remain source failures and do not replace last-good rows.
- Kimi CLI (`kimi_cli`) and Moonshot API (`moonshot`) remain distinct families.
- Z.AI (`zai`) remains an API family; no unsupported subscription percentage or
  reset time is inferred from Token activity.

## Dedicated adapter threshold

A provider-specific adapter is added only if a documented official API or a
provider-owned local source supplies a more authoritative fact that OpenUsage
does not expose. It must use a separate source ID and obey cold-start-only
fallback so the same daily facts are never summed twice.
