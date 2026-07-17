# OpenUsage Bar engineering rules

Use Ponytail full. Reuse working code, the standard library, native Apple APIs, and installed dependencies before adding code.

Never remove validation, data-loss protection, credential isolation, accessibility, error handling, or verification. Do not expose API keys, cookies, provider payloads, prompts, responses, or direct account identity.

OpenUsage Bar is the product. OpenUsage.sh is a reusable data source. Python adapters own credentials and ledger writes; SwiftUI surfaces are read-only. Add no third-party UI, chart, database, state-management, or dependency-injection package.
