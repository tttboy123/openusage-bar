# OpenUsage Bar Provider Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Provider coverage reuse-first, fact-specific, multi-account capable, and independently testable without growing central conditionals or inventing unsupported quota values.

**Architecture:** A registry builds a `ProviderBinding` for each sanitized provider instance. Quota, Token usage, and monetary cost use separate source contracts and health states; the collector persists each fact family independently and preserves last-good facts on failure.

**Tech Stack:** Python Protocol/dataclass contracts, existing bounded HTTP/subprocess/Keychain utilities, generated provider catalog, unittest fixtures.

---

**Codex skill resolution:** In this environment, `superpowers:executing-plans` maps to the installed `executing-plans` skill. Use the subagent-driven option only when `subagent-driven-development` is actually available.

### Task 1: Introduce Provider contracts and a runtime registry

**Files:**
- Create: `openusage_bar/providers/__init__.py`
- Create: `openusage_bar/providers/contracts.py`
- Create: `openusage_bar/providers/registry.py`
- Create: `openusage_bar/providers/builtins.py`
- Modify: `openusage_bar/aggregator.py`
- Create: `tests/test_adapter_registry.py`

- [ ] **Step 1: Write failing graph-equivalence tests**

For every current config type, assert the registry builds the same card and importer classes as `build_headless_refresher`. Also assert config order does not change bindings, duplicate source IDs are rejected, and unknown config classes fail closed.

- [ ] **Step 2: Define the fact-specific contracts**

```python
class QuotaAdapter(Protocol):
    source_id: str
    def fetch_quota(self) -> "QuotaFetchResult": ...

class UsageAdapter(Protocol):
    source_id: str
    def fetch_usage(self, since: date, until: date) -> "UsageFetchResult": ...

class CostAdapter(Protocol):
    source_id: str
    def fetch_costs(self, since: date, until: date) -> "CostFetchResult": ...

@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    family_id: str
    quota_sources: tuple[QuotaAdapter, ...] = ()
    usage_sources: tuple[UsageAdapter, ...] = ()
    cost_sources: tuple[CostAdapter, ...] = ()
```

Use concrete frozen success/failure dataclasses rather than exceptions as normal network outcomes. A result contains sanitized `error_code`, never exception text or raw payload.

- [ ] **Step 3: Implement registration**

`AdapterRegistry.register_global(factory)` handles always-on OpenUsage, Codex, and Kiro sources. `register_config(config_type, factory)` handles app-managed configs. `build(configs)` returns stable bindings sorted by Provider ID and source priority.

- [ ] **Step 4: Replace the central config switch**

Change `build_headless_refresher` to ask `builtins.default_registry(...)` for bindings. Remove the central `isinstance(config, ...)` chain only after graph-equivalence tests pass.

- [ ] **Step 5: Verify and commit**

```bash
.build-venv/bin/python -m unittest tests.test_adapter_registry tests.test_aggregator -v
git add openusage_bar/providers openusage_bar/aggregator.py \
  tests/test_adapter_registry.py tests/test_aggregator.py
git commit -m "refactor: register provider runtime bindings"
```

### Task 2: Separate quota, Token, and cost collection

**Files:**
- Modify: `openusage_bar/providers/contracts.py`
- Modify: `openusage_bar/daily_history.py`
- Modify: `openusage_bar/openai_organization.py`
- Modify: `openusage_bar/daily_feed.py`
- Modify: `openusage_bar/minimax.py`
- Test: `tests/test_daily_history.py`
- Test: existing provider tests

- [ ] **Step 1: Write failure-isolation tests**

For one binding, make quota succeed, usage time out, and cost succeed. Assert quota and cost commit, usage keeps last-good data, and three source-health rows are independently updated. Repeat each failure permutation.

- [ ] **Step 2: Move shared import result types**

Move `ImportFailure`, `UsageImportSuccess`, and `CostImportSuccess` out of `openai_organization.py` into `providers/contracts.py`. Add `QuotaFetchSuccess` with one or more quota observations and `QuotaFetchFailure` with a sanitized code.

- [ ] **Step 3: Schedule each fact family independently**

Refactor `ActivityCollector.refresh` into bounded private passes:

```python
def refresh(self, bindings: tuple[ProviderBinding, ...]) -> bool:
    self._publish_provider_instances(bindings)
    self._refresh_quota_sources(bindings)
    self._refresh_usage_sources(bindings)
    self._refresh_cost_sources(bindings)
    return True
```

Each pass catches source-local errors, records source health, and continues. One fact family cannot suppress another.

- [ ] **Step 4: Keep ProviderCard presentation-only**

Continue producing cards for menu compatibility during 0.5, but do not treat one card as the authoritative representation of all quota windows. All new quota persistence must consume `QuotaFetchResult`.

- [ ] **Step 5: Verify and commit**

```bash
.build-venv/bin/python -m unittest \
  tests.test_daily_history tests.test_openai_organization \
  tests.test_daily_feed tests.test_minimax -v
git add openusage_bar/providers/contracts.py openusage_bar/daily_history.py \
  openusage_bar/openai_organization.py openusage_bar/daily_feed.py \
  openusage_bar/minimax.py tests
git commit -m "refactor: collect provider facts independently"
```

### Task 3: Persist multiple quota windows with source provenance

**Files:**
- Modify: `openusage_bar/activity_records.py`
- Modify: `openusage_bar/activity_schema.py`
- Modify: `openusage_bar/activity_store.py`
- Modify: `openusage_bar/query.py`
- Modify: `openusage_bar/codex_subscription.py`
- Modify: `openusage_bar/minimax.py`
- Modify: `openusage_bar/step_plan.py`
- Modify: `openusage_bar/kiro.py`
- Modify: `openusage_bar/generic.py`
- Test: related provider, store, and query tests

- [ ] **Step 1: Define the observation identity**

```python
@dataclass(frozen=True)
class QuotaObservation:
    record_id: str
    provider_id: str
    account_ref: str
    source_id: str
    quota_name: str
    quota_window: str
    unit: str
    remaining: str | None
    remaining_ratio: float | None
    resets_at: str | None
    observed_at: str
    state: str
    quality: str
    stale: bool
```

Identity is stable `provider_id + account_ref + source_id + quota_window + quota_name`; display names and email must not participate.

- [ ] **Step 2: Add schema migration tests**

Migrate existing quota rows with `source_id="current.quota"` and `quota_window="subscription"`. Assert history count, remaining values, reset times, and cursor monotonicity are preserved.

- [ ] **Step 3: Emit all known windows**

Codex, MiniMax, and Step Plan emit each available five-hour, weekly, monthly, or model-specific window separately. Kiro emits its billing-cycle window. A missing window emits no numeric observation and updates source health/capability only.

- [ ] **Step 4: Replace brand-specific merging**

Remove the Codex/Kiro branch in `Aggregator.refresh`. Select last-good facts by declared source priority, quality, freshness, and stable source ID rather than Provider brand.

- [ ] **Step 5: Verify and commit**

```bash
.build-venv/bin/python -m unittest \
  tests.test_activity_store tests.test_query tests.test_codex_subscription \
  tests.test_minimax tests.test_step_plan tests.test_kiro tests.test_generic -v
git add openusage_bar tests
git commit -m "feat: preserve multi-window quota provenance"
```

### Task 4: Normalize multi-account identity

**Files:**
- Modify: `openusage_bar/config.py`
- Modify: `openusage_bar/activity_records.py`
- Modify: `openusage_bar/daily_history.py`
- Modify: `openusage_bar/query.py`
- Test: `tests/test_config.py`
- Test: `tests/test_daily_history.py`
- Test: `tests/test_query.py`

- [ ] **Step 1: Add two-account isolation tests**

Create two connections for each managed multi-account family. Assert distinct Keychain IDs, provider IDs, quotas, Token rows, costs, source health, and change records. Assert filtering one Provider ID never returns the other.

- [ ] **Step 2: Apply the identity rule**

- `provider_id`: stable app-managed connection ID.
- `family_id`: canonical vendor/client family.
- `account_ref`: optional opaque sub-scope supplied by one connection.

Remove the OpenAI Organization restriction that forces `provider_id == "openai"`. Validate account refs with the existing stable-ID pattern and reject email, whitespace, slash-delimited identity, or copied display names.

- [ ] **Step 3: Permit importer rows to use an expected account ref**

Replace the current blank-only account validation with an importer binding that declares its expected opaque account ref. Reject any row that does not match the binding.

- [ ] **Step 4: Verify and commit**

```bash
.build-venv/bin/python -m unittest \
  tests.test_config tests.test_daily_history tests.test_query -v
git add openusage_bar tests
git commit -m "feat: isolate provider accounts in the ledger"
```

### Task 5: Add reuse-first discovery aliases

**Files:**
- Modify: `openusage_bar/provider_catalog.py`
- Modify: `openusage_bar/resources/provider-catalog.v1.json`
- Modify: `scripts/generate_swift_provider_catalog.py`
- Modify: `swift_app/Sources/UsageCore/GeneratedProviderCatalog.swift`
- Modify: `swift_app/Sources/UsageCore/UsageDetailsPresentation.swift`
- Test: `tests/test_provider_catalog.py`
- Test: `swift_app/Tests/UsageCoreTests/ProviderCatalogTests.swift`
- Modify: `docs/provider-support.md`

- [ ] **Step 1: Add exact alias expectations**

```text
GLM, Zhipu, 智谱 -> zai
Kimi -> kimi_cli, moonshot
Qwen -> qwen_cli, alibaba_cloud
Claude -> claude_code, anthropic
OpenCode -> opencode
```

Aliases affect search/discovery only. They must never rewrite an observed Provider identity or claim quota capability.

- [ ] **Step 2: Port the isolated alias change**

Use commit `17cf7ec` as the reviewed source map and reapply its alias data/tests to current main. Regenerate Swift catalog output with the repository generator.

- [ ] **Step 3: Keep missing capabilities honest**

Default to `OpenUsageDailyImporter` for Token history where supported. If no authoritative reset/quota endpoint exists, leave quota and reset capability `unknown`; do not add a browser scraper or numeric zero.

- [ ] **Step 4: Verify and commit**

```bash
.build-venv/bin/python -m unittest tests.test_provider_catalog -v
swift test --package-path swift_app --filter ProviderCatalogTests
.build-venv/bin/python scripts/generate_swift_provider_catalog.py --check
git add openusage_bar scripts swift_app docs/provider-support.md tests
git commit -m "feat: add provider discovery aliases"
```

### Task 6: Version custom quota, usage, and cost feeds

**Files:**
- Modify: `openusage_bar/config.py`
- Modify: `openusage_bar/generic.py`
- Modify: `openusage_bar/daily_feed.py`
- Create: `openusage_bar/cost_feed.py`
- Test: `tests/test_config.py`
- Test: `tests/test_generic.py`
- Test: `tests/test_daily_feed.py`
- Create: `tests/test_cost_feed.py`

- [ ] **Step 1: Add v1 compatibility fixtures**

Load every existing Generic and Daily Feed config fixture, save it with the new store, reload it, and assert endpoint, field mappings, Provider identity, and Keychain account remain unchanged.

- [ ] **Step 2: Extend Generic quota declarations**

Add explicit `family_id`, `quota_window`, `quota_name`, and `unit`. Reject a percent path when unit is not percent and reject reset fields without a declared window. Preserve all existing SSRF, redirect, response-size, header, and JSON-path validation.

- [ ] **Step 3: Add `DailyCostFeedConfig`**

Use range-aware HTTPS JSON, explicit amount/currency/day mappings, bounded pagination, Keychain authentication, and `CostAdapter`. Never encode monetary cost as a Token row.

- [ ] **Step 4: Verify and commit**

```bash
.build-venv/bin/python -m unittest \
  tests.test_config tests.test_generic tests.test_daily_feed tests.test_cost_feed -v
git add openusage_bar tests
git commit -m "feat: version custom usage and cost feeds"
```

### Task 7: Build the Provider Conformance Kit

**Files:**
- Create: `tests/provider_conformance.py`
- Create: `tests/test_provider_conformance.py`
- Create: `tests/fixtures/providers/codex/`
- Create: `tests/fixtures/providers/kiro/`
- Create: `tests/fixtures/providers/minimax/`
- Create: `tests/fixtures/providers/step_plan/`
- Create: `tests/fixtures/providers/openusage/`
- Create: `tests/fixtures/providers/custom/`
- Modify: `scripts/build_app.sh`

- [ ] **Step 1: Define the reusable conformance cases**

Every registered source must pass applicable cases for success, empty result, authentication expiry, rate limit, timeout, malformed response, oversized response, pagination loop, partial coverage, last-good preservation, source priority, multi-account isolation, secret non-disclosure, and Unknown-not-zero.

- [ ] **Step 2: Use synthetic redacted fixtures only**

Fixtures contain stable fake IDs and values. The test loader rejects JWT-like strings, cookie names, API-key prefixes, email addresses, absolute home paths, prompts, responses, and raw copied provider payload metadata.

- [ ] **Step 3: Enforce registry/catalog agreement**

Generate a report from catalog declarations and registered runtime sources. Fail if a runtime source has no catalog provenance or if a catalog says `supported` without at least one registered source/fixture.

- [ ] **Step 4: Run the complete gate and commit**

```bash
.build-venv/bin/python -m unittest tests.test_provider_conformance -v
scripts/build_app.sh
git add tests/provider_conformance.py tests/test_provider_conformance.py \
  tests/fixtures/providers scripts/build_app.sh
git commit -m "test: add provider conformance kit"
```

## Acceptance gate

- Adding a Provider never adds a central aggregator type branch.
- Quota, usage, and cost failures are isolated and independently visible.
- Every quota value explains source, window, freshness, quality, and reset time.
- GLM, Kimi, Qwen, Claude, and OpenCode discovery reuses canonical families without false capability claims.
- Multiple accounts never overwrite one another and never expose direct identity.
- Every registered source passes the conformance kit with synthetic fixtures.
