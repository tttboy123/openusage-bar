# OpenUsage Bar Native Product Surfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a coherent bilingual macOS product in which users can get a trustworthy first metric, manage providers, inspect usage, and discover the automation API without leaving the native client.

**Architecture:** Keep menu-bar presentation, Activity presentation, Provider mutation, and API diagnostics in focused Swift files. SwiftUI submits allowlisted requests to the Python Controller over bounded stdin; it never writes Keychain, provider config, or SQLite directly.

**Tech Stack:** Swift 6.2, SwiftUI, Foundation `Process`, native Charts, AppKit launch APIs, Swift Testing, Python Controller helper.

---

**Codex skill resolution:** In this environment, `superpowers:executing-plans` maps to the installed `executing-plans` skill. Use the subagent-driven option only when `subagent-driven-development` is actually available.

### Task 1: Split the large native files without changing behavior

**Files:**
- Create: `swift_app/Sources/OpenUsageActivity/ActivityDashboardViews.swift`
- Create: `swift_app/Sources/OpenUsageActivity/ActivityCapacityViews.swift`
- Create: `swift_app/Sources/OpenUsageActivity/ActivityUsagePages.swift`
- Create: `swift_app/Sources/OpenUsageActivity/ActivityViewSupport.swift`
- Create: `swift_app/Sources/OpenUsageActivity/ProviderCenterViews.swift`
- Create: `swift_app/Sources/UsageCore/UsageRepository+Queries.swift`
- Create: `swift_app/Sources/UsageCore/UsageRepository+SQLite.swift`
- Create: `swift_app/Sources/UsageCore/UsageRepository+Schema.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ActivityViews.swift`
- Modify: `swift_app/Sources/UsageCore/UsageRepository.swift`
- Test: all existing Swift tests

- [ ] **Step 1: Run the native baseline**

```bash
swift test --package-path swift_app --enable-code-coverage -Xswiftc -warnings-as-errors
```

Expected: all existing Swift tests pass.

- [ ] **Step 2: Move declarations by feature**

Use commits `9e57595` and `f83350d` only as move maps. Keep root routing and window composition in `ActivityViews.swift`; move dashboard, capacity, usage, shared support, and Provider Center views into the files above. Keep the `UsageRepository` public type in `UsageRepository.swift`; move queries, SQLite helpers, and schema checks into extensions.

- [ ] **Step 3: Verify the split is behavior-neutral**

```bash
swift test --package-path swift_app --enable-code-coverage -Xswiftc -warnings-as-errors
git diff --check
```

Expected: all tests pass and no copy, route, accessibility label, query, or schema behavior changes.

- [ ] **Step 4: Commit**

```bash
git add swift_app/Sources/OpenUsageActivity swift_app/Sources/UsageCore \
  swift_app/Tests
git commit -m "refactor: split native product surfaces"
```

### Task 2: Bound every Provider mutation subprocess

**Files:**
- Create: `swift_app/Sources/OpenUsageActivity/ProviderMutationClient.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ActivityAppLogic.swift`
- Test: `swift_app/Tests/OpenUsageActivityTests/ActivityAppLogicTests.swift`

- [ ] **Step 1: Add failing process-boundary tests**

Cover helper timeout, response larger than 128 KiB, invalid UTF-8, child non-zero exit, truncated JSON, and successful response. The public failure contract is:

```swift
enum ProviderMutationFailure: Error, Sendable, Equatable {
    case unavailable
    case couldNotLaunch
    case timedOut
    case responseTooLarge
    case invalidResponse
}
```

- [ ] **Step 2: Run RED**

```bash
swift test --package-path swift_app --filter ActivityAppLogicTests
```

Expected: timeout and output-bound tests fail because the current implementation calls `readDataToEndOfFile()` and `waitUntilExit()` without bounds.

- [ ] **Step 3: Implement a bounded client**

Add:

```swift
struct ProviderMutationLimits: Sendable, Equatable {
    let timeout: Duration
    let maximumResponseBytes: Int

    static let production = Self(
        timeout: .seconds(30),
        maximumResponseBytes: 131_072
    )
}
```

The client must write encoded JSON to stdin, close stdin, read at most the configured bytes, terminate and reap the child at timeout, discard stderr, and return only sanitized enum failures. It must never include request or helper output in an error.

- [ ] **Step 4: Verify GREEN**

```bash
swift test --package-path swift_app --filter ActivityAppLogicTests
```

Expected: all bounded-process tests pass and no child process remains alive.

- [ ] **Step 5: Commit**

```bash
git add swift_app/Sources/OpenUsageActivity/ProviderMutationClient.swift \
  swift_app/Sources/OpenUsageActivity/ActivityAppLogic.swift \
  swift_app/Tests/OpenUsageActivityTests/ActivityAppLogicTests.swift
git commit -m "fix: bound provider mutation helpers"
```

### Task 3: Make the menu bar a two-second decision surface

**Files:**
- Modify: `swift_app/Sources/OpenUsageBar/MenuLogic.swift`
- Modify: `swift_app/Sources/OpenUsageBar/MenuBarViews.swift`
- Modify: `swift_app/Sources/OpenUsageBar/MenuBarViewModel.swift`
- Test: `swift_app/Tests/OpenUsageBarTests/MenuLogicTests.swift`

- [ ] **Step 1: Add failing presentation tests**

Add assertions that:

- the header contains only product status and refresh state;
- Today Token is `Unavailable` only when coverage is missing, and is `0` only when coverage explicitly records zero;
- capacity rows are ordered by usable state, remaining ratio, reset time, then stable Provider ID;
- stale, unavailable, and unknown have distinct labels and symbols;
- the no-data primary action opens Providers;
- no predictive risk card is present.

- [ ] **Step 2: Introduce the explicit empty-state model**

```swift
struct MenuEmptyStatePresentation: Sendable, Equatable {
    enum Reason: Sendable, Equatable {
        case noSources
        case collecting
        case sourceFailure
    }

    let reason: Reason
    let titleKey: String
    let detailKey: String
    let primaryRoute: ActivityRouteMessage
}
```

Map `noSources` to the Providers route, `collecting` to Activity, and `sourceFailure` to Data Health.

- [ ] **Step 3: Verify the focused menu behavior**

```bash
swift test --package-path swift_app --filter MenuLogicTests
```

Expected: PASS; menu content remains compact and deterministic.

- [ ] **Step 4: Commit**

```bash
git add swift_app/Sources/OpenUsageBar swift_app/Tests/OpenUsageBarTests
git commit -m "feat: focus the menu on current resource facts"
```

### Task 4: Add a derived first-run experience

**Files:**
- Create: `swift_app/Sources/OpenUsageActivity/OnboardingLogic.swift`
- Create: `swift_app/Sources/OpenUsageActivity/OnboardingViews.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/OpenUsageActivityApp.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ActivityViews.swift`
- Create: `swift_app/Tests/OpenUsageActivityTests/OnboardingLogicTests.swift`

- [ ] **Step 1: Write the onboarding state tests**

Use a pure assessment type:

```swift
enum FirstRunPhase: Sendable, Equatable {
    case hidden
    case discoverableProviders([String])
    case needsConnection([String])
    case collecting
    case ready
}

struct FirstRunAssessment: Sendable {
    static func evaluate(
        wasExplicitlyOpened: Bool,
        userSkipped: Bool,
        providerFamilyIDs: [String],
        configuredFamilyIDs: [String],
        hasTrustworthyFact: Bool,
        isRefreshing: Bool
    ) -> FirstRunPhase
}
```

Cover: existing trusted data hides onboarding; background launch is silent; detected local clients are shown first; a configured but empty source enters collecting; explicit skip suppresses automatic reopening but leaves a Help-menu entry.

- [ ] **Step 2: Verify RED**

```bash
swift test --package-path swift_app --filter OnboardingLogicTests
```

Expected: FAIL because the assessment does not exist.

- [ ] **Step 3: Implement the pure logic and view**

Derive Provider candidates from sanitized provider instances/catalog. Do not probe Keychain from Swift. The view has one primary action at a time: Review Detected Providers, Add Connection, Refresh, or View First Metric.

- [ ] **Step 4: Verify the flow**

```bash
swift test --package-path swift_app --filter OnboardingLogicTests
```

Expected: PASS for all state transitions.

- [ ] **Step 5: Commit**

```bash
git add swift_app/Sources/OpenUsageActivity/OnboardingLogic.swift \
  swift_app/Sources/OpenUsageActivity/OnboardingViews.swift \
  swift_app/Sources/OpenUsageActivity/OpenUsageActivityApp.swift \
  swift_app/Sources/OpenUsageActivity/ActivityViews.swift \
  swift_app/Tests/OpenUsageActivityTests/OnboardingLogicTests.swift
git commit -m "feat: guide users to the first trusted metric"
```

### Task 5: Move daily Provider management into Provider Center

**Files:**
- Create: `swift_app/Sources/OpenUsageActivity/ProviderCenterLogic.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ProviderCenterViews.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ProviderMutationClient.swift`
- Modify: `openusage_bar/provider_commands.py`
- Modify: `openusage_bar/ui.py`
- Test: `swift_app/Tests/OpenUsageActivityTests/ProviderCenterPresentationTests.swift`
- Test: `tests/test_provider_commands.py`
- Test: `tests/test_ui_viewmodel.py`

- [ ] **Step 1: Define mutation v2 with strict discriminated actions**

The stdin envelope is:

```json
{
  "version": 2,
  "action": "create_connection",
  "providerId": "minimax-local-1",
  "kind": "minimax",
  "configuration": {"name": "MiniMax"},
  "credentialMaterial": {"primary": "replacement-only"}
}
```

Allowed actions are `create_connection`, `update_connection`, and `remove_connection`. Python must enforce an exact field set for the envelope and an exact schema per `kind`. Version 1 update requests remain accepted during the 0.4 release.

- [ ] **Step 2: Write failing Python boundary tests**

Cover all managed types: MiniMax, Step Plan, OpenAI Organization, Generic HTTPS, and Daily Usage Feed. Cover duplicate IDs, wrong site, unsupported fields, remove rollback, empty required credential, oversized stdin, and secret-free responses.

- [ ] **Step 3: Implement Controller operations**

Add `ProviderController.create_connection(...)` as a strict dispatcher to existing validated add methods. Add `remove_connection(provider_id)` that removes config only after all provider-owned Keychain accounts are identified; if a delete or config write fails, restore prior Keychain values and config. Never allow mutation of discovered local-client logins.

- [ ] **Step 4: Build native forms**

Use typed Swift drafts rather than a dictionary. Generic and feed connections must retain the complete declarative mappings already validated by Python:

```swift
struct GenericQuotaDraft: Sendable, Equatable {
    let providerID: String
    let name: String
    let familyID: String
    let endpoint: String
    let headerName: String
    let authPrefix: String
    let primaryPath: String
    let remainingPercentPath: String?
    let resetPath: String?
    let detailPath: String?
    let replacementCredential: String
}

struct DailyUsageFeedDraft: Sendable, Equatable {
    let providerID: String
    let name: String
    let familyID: String
    let endpoint: String
    let headerName: String
    let authPrefix: String
    let itemsPath: String
    let datePath: String
    let modelPath: String
    let inputTokensPath: String
    let outputTokensPath: String
    let cacheReadTokensPath: String?
    let cacheCreationTokensPath: String?
    let reasoningTokensPath: String?
    let totalTokensPath: String
    let sinceParameter: String
    let untilParameter: String
    let replacementCredential: String
}

enum ManagedConnectionDraft: Sendable, Equatable {
    case minimax(providerID: String, name: String, replacementCredential: String)
    case stepPlan(providerID: String, name: String, site: String, replacementCredential: String, replacementSession: String)
    case openAIOrganization(providerID: String, name: String, replacementCredential: String)
    case generic(GenericQuotaDraft)
    case dailyUsageFeed(DailyUsageFeedDraft)
}
```

Keep credentials as transient view state, never initialize them from saved data, and clear them after submit or cancel. Keep auto-discovered integrations read-only. Visibility continues through the existing sanitized visibility store.

- [ ] **Step 5: Verify both boundaries**

```bash
.build-venv/bin/python -m unittest tests.test_provider_commands tests.test_ui_viewmodel -v
swift test --package-path swift_app --filter ProviderCenterPresentationTests
```

Expected: add/edit/remove/hide/multi-account tests pass; secrets never appear in stdout, config, logs, or Swift models.

- [ ] **Step 6: Commit**

```bash
git add openusage_bar/provider_commands.py openusage_bar/ui.py \
  tests/test_provider_commands.py tests/test_ui_viewmodel.py \
  swift_app/Sources/OpenUsageActivity/ProviderCenterLogic.swift \
  swift_app/Sources/OpenUsageActivity/ProviderCenterViews.swift \
  swift_app/Sources/OpenUsageActivity/ProviderMutationClient.swift \
  swift_app/Tests/OpenUsageActivityTests/ProviderCenterPresentationTests.swift
git commit -m "feat: manage providers in the native center"
```

### Task 6: Add the read-only Automation surface

**Files:**
- Create: `swift_app/Sources/UsageCore/LocalAPIClient.swift`
- Create: `swift_app/Sources/OpenUsageActivity/AutomationLogic.swift`
- Create: `swift_app/Sources/OpenUsageActivity/AutomationViews.swift`
- Modify: `swift_app/Sources/UsageCore/UsageDetailsPresentation.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ActivityViews.swift`
- Create: `swift_app/Tests/UsageCoreTests/LocalAPIClientTests.swift`
- Create: `swift_app/Tests/OpenUsageActivityTests/AutomationLogicTests.swift`

- [ ] **Step 1: Write Unix-socket client tests**

Use a fake socket server to cover success, timeout, schema drift, malformed framing, body over 1 MiB, non-JSON, and unavailable socket. The client exposes read-only methods:

```swift
public protocol LocalAPIReading: Sendable {
    func health() async throws -> LocalAPIHealth
    func schema() async throws -> LocalAPISchema
    func snapshot(localDay: String?) async throws -> LocalAPIResourceSnapshot
}
```

- [ ] **Step 2: Implement the bounded native reader**

Connect only to the configured Unix socket, use a three-second total timeout and 1 MiB body cap, send `GET` with `Connection: close`, require HTTP/1.1 and `schemaVersion == "1.0"`, and never add mutation methods.

- [ ] **Step 3: Add the Automation route and page**

Show API state, socket path, schema version, data revision, generated time, and a sanitized JSON snapshot preview. Provide copy buttons for exact curl and bundled-helper commands. Do not expose refresh, provider configuration, bearer tokens, account identity, or raw change payloads.

- [ ] **Step 4: Verify**

```bash
swift test --package-path swift_app --filter LocalAPIClientTests
swift test --package-path swift_app --filter AutomationLogicTests
```

Expected: all boundary and presentation tests pass.

- [ ] **Step 5: Commit**

```bash
git add swift_app/Sources/UsageCore/LocalAPIClient.swift \
  swift_app/Sources/OpenUsageActivity/AutomationLogic.swift \
  swift_app/Sources/OpenUsageActivity/AutomationViews.swift \
  swift_app/Sources/UsageCore/UsageDetailsPresentation.swift \
  swift_app/Sources/OpenUsageActivity/ActivityViews.swift \
  swift_app/Tests/UsageCoreTests/LocalAPIClientTests.swift \
  swift_app/Tests/OpenUsageActivityTests/AutomationLogicTests.swift
git commit -m "feat: expose local automation status"
```

### Task 7: Complete Usage Details behavior and bilingual coverage

**Files:**
- Modify: `swift_app/Sources/OpenUsageActivity/ActivityDashboardViews.swift`
- Modify: `swift_app/Sources/OpenUsageActivity/ActivityUsagePages.swift`
- Modify: `swift_app/Sources/UsageCore/Localization.swift`
- Modify: `swift_app/Resources/en.lproj/Localizable.strings`
- Modify: `swift_app/Resources/zh-Hans.lproj/Localizable.strings`
- Create: `swift_app/Tests/UsageCoreTests/LocalizationContractTests.swift`
- Modify: `tests/test_ui_localization.py`

- [ ] **Step 1: Add failing behavior tests**

Cover Day/Week/Month/Year filtering, Missing/Partial/Covered Zero, square heatmap cells, trailing-most date visible by default, keyboard focus, and daily model trend hover/focus content with model composition, quality, and collection time.

- [ ] **Step 2: Add localization contract tests**

Require English and Simplified Chinese key sets to be identical. Require every literal passed to `AppLocalization.text` to exist in both files. Reject mismatched format placeholders and newly introduced user-facing literals outside the localization boundary.

- [ ] **Step 3: Implement the detail behavior**

Keep one Activity overview; do not reintroduce a duplicate Overview page. The annual heatmap stays vertically above the 30-day stacked trend. Every heatmap cell has a square frame and exposes the same facts to hover and keyboard focus.

- [ ] **Step 4: Localize all product surfaces**

Cover Menu Bar, Activity, Capacity, API Spend, Local Tools, Providers, Data Health, Automation, onboarding, validation, empty states, and error states. Keep Provider names, model IDs, API keys, and technical schema identifiers untranslated.

- [ ] **Step 5: Run full native and product gates**

```bash
.build-venv/bin/python -m unittest tests.test_ui_localization -v
swift test --package-path swift_app --enable-code-coverage -Xswiftc -warnings-as-errors
scripts/build_app.sh
```

Expected: all tests pass, no visible mixed-language copy remains, and Swift coverage stays at least 80%.

- [ ] **Step 6: Commit**

```bash
git add swift_app/Sources swift_app/Resources swift_app/Tests \
  tests/test_ui_localization.py
git commit -m "feat: complete bilingual native control center"
```

## Visible acceptance gate

- A clean first launch leads to one trustworthy metric in under five minutes.
- Menu Bar contains Today Token and capacity facts without redundant risk cards.
- Usage Details distinguishes missing, partial, covered-zero, stale, and live data.
- Provider add/edit/remove/hide and multi-account management stay inside Provider Center.
- Automation shows a working read-only API handoff without exposing credentials.
- English and Simplified Chinese paths are complete, keyboard accessible, and VoiceOver readable.
