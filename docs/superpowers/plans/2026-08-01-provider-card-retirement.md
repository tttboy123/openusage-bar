# Provider Card Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `ProviderCard` and `LegacyCardAdapter` from the Collector-to-ledger path while preserving the existing menu-bar and Usage Details output.

**Architecture:** Provider adapters return only fact-specific results, sanitized source failures and the actual source attribution used by that attempt. Stable Provider identity moves into `ProviderBinding`; credential/source kind remains attempt-specific because one configured Provider (for example Step Plan) may use either an API key or a browser session. The Collector combines both into `ProviderInstance` and writes facts directly. `ProviderCard`, `Overview`, stale-card merging and the card cache remain presentation compatibility code until the Python UI is retired, but they are no longer inputs to the durable ledger.

**Tech Stack:** Python 3 dataclasses and protocols, existing SQLite activity ledger, standard-library unittest, existing SwiftUI read-only client.

---

## Scope and delivery order

This migration ships as two independently reviewable slices:

1. **WQ-19A:** Direct quota/balance Providers and configured feed identities become fact-first.
2. **WQ-19B:** OpenUsage discovery leaves the card path after `openusage-export/v1` is frozen by WQ-20.

WQ-19A must not change Local API v1 JSON, SQLite fact semantics, Provider priority,
Unknown handling or SwiftUI behavior. WQ-19B must not infer Provider discovery from
daily Token rows; it consumes the explicit producer contract and publishes bounded
`ProviderInstance` plus `SourceStatus` facts.

### Task 1: Add fact-first Provider identity and adapter contracts

**Files:**
- Modify: `openusage_bar/providers/contracts.py`
- Modify: `openusage_bar/providers/registry.py`
- Modify: `openusage_bar/providers/__init__.py`
- Test: `tests/test_adapter_registry.py`

- [ ] **Step 1: Write failing identity and protocol tests**

Add tests that construct a binding with this descriptor and assert that registry
normalization preserves it without importing `ProviderCard`:

```python
ProviderDescriptor(
    provider_id="minimax-primary",
    family_id="minimax",
    display_name="MiniMax Primary",
    category="subscription",
)
```

Add a quota source exposing only:

```python
source_id = "minimax.coding_plan"
source_priority = 20

def fetch_quota(self) -> QuotaCollectionResult:
    return QuotaCollectionResult(
        result=QuotaFetchFailure("quota_unavailable"),
        attribution=SourceAttribution(
            credential_source="minimax_builtin_api",
            source_kind="builtin_api",
        ),
    )
```

Assert that a `fetch()`-only source is rejected from `quota_sources`, duplicate
fact source IDs remain rejected, and error messages expose only stable IDs/types.

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_adapter_registry -v
```

Expected: tests fail because `ProviderBinding` has no descriptor requirement and
still accepts `LegacyCardAdapter` in `quota_sources`.

- [ ] **Step 3: Implement the minimal contracts**

Add a frozen `ProviderDescriptor` with the four stable fields above. Validate
identifiers with `validate_id`, category against `PROVIDER_CATEGORIES` and display
name with `validate_safe_display_name`. Add frozen `SourceAttribution` with
`credential_source` and `source_kind`; validate the first as a stable ID and the
second against the Provider-instance source kinds.
Add:

```python
def observed(
    self, observed_at: datetime, attribution: SourceAttribution
) -> ProviderInstance:
    return ProviderInstance(
        provider_id=self.provider_id,
        family_id=self.family_id,
        display_name=self.display_name,
        category=self.category,
        credential_source=attribution.credential_source,
        source_kind=attribution.source_kind,
        observed_at=observed_at.isoformat(),
    )
```

Add `QuotaCollectionResult` and `BalanceCollectionResult` envelopes containing a
typed fact result plus `SourceAttribution`. This prevents a multi-mode adapter from
publishing a static credential/source claim that was not used by the attempt.

Change the protocols to:

```python
class QuotaAdapter(Protocol):
    source_id: str
    source_priority: int
    def fetch_quota(self) -> QuotaCollectionResult: ...

class BalanceAdapter(Protocol):
    source_id: str
    source_priority: int
    def fetch_balance(self) -> BalanceCollectionResult: ...
```

Make `ProviderBinding.descriptor` required and require its Provider/family IDs to
match the binding. Remove `LegacyCardAdapter` from the core contract and exports.

- [ ] **Step 4: Run the tests to verify GREEN**

Run the Step 2 command again.

Expected: all adapter registry tests pass.

- [ ] **Step 5: Commit the contract slice**

```bash
git add openusage_bar/providers tests/test_adapter_registry.py
git commit -m "refactor: add fact-first provider contracts"
```

### Task 2: Make the headless refresh path consume facts directly

**Files:**
- Modify: `openusage_bar/aggregator.py`
- Modify: `openusage_bar/daily_history.py`
- Modify: `openusage_bar/providers/builtins.py`
- Test: `tests/test_aggregator.py`
- Test: `tests/test_daily_history.py`
- Test: `tests/test_adapter_registry.py`

- [ ] **Step 1: Write failing headless-path tests**

Create fake quota and balance adapters whose `fetch()` raises immediately but
whose `fetch_quota()` / `fetch_balance()` return valid facts. Assert one headless
refresh:

- writes their `ProviderInstance` descriptors;
- writes quota and balance facts;
- records source success at the exact source ID;
- never calls `fetch()`;
- preserves Last-good data when the next result is a sanitized failure.

Add a source spy and assert one network call per refresh; the Collector must not
call an adapter once for a card and again for its fact.

- [ ] **Step 2: Run tests to verify RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_aggregator tests.test_daily_history tests.test_adapter_registry -v
```

Expected: the headless builder still calls card-producing `fetch()` and reads
`last_quota_result` / `last_balance_result` side channels.

- [ ] **Step 3: Replace the side-channel bridge**

Change `LedgerRefresher` to own sorted tuples of `(ProviderDescriptor, adapter)`.
Within one refresh it must call each fact method through `measure_source_call`,
capture a typed failure with the adapter's bounded public attribution on exceptions,
and pass immutable result tuples to the Collector. Delete all reads of
`last_quota_result` and `last_balance_result`.

Change `ActivityCollector.refresh` to receive:

```python
provider_instances: tuple[ProviderInstance, ...]
provider_families: Mapping[str, str]
quota_results: tuple[tuple[str, str, QuotaFetchResult], ...]
balance_results: tuple[tuple[str, str, BalanceFetchResult], ...]
```

Use `provider_families` for OpenUsage fallback scope, and use the descriptors for
Provider IDs and `ProviderInstance` writes. Remove `_provider_instance`,
`_quota_observation`, `_tracks_current_quota`, `_persist_current_quotas` and all
`Overview`/`ProviderCard` imports from `daily_history.py`.

- [ ] **Step 4: Keep presentation compatibility isolated**

`Aggregator`, `CardCache`, `merge_cards`, `ProviderCard` and `Overview` may remain
in the presentation path used by `openusage_bar/ui.py`. Add a module docstring and
test proving `build_headless_refresher()` does not construct `CardCache` or
`Aggregator`.

- [ ] **Step 5: Run tests to verify GREEN**

Run the Step 2 command again.

Expected: all tests pass and the headless spy reports zero card fetches.

- [ ] **Step 6: Commit the headless migration**

```bash
git add openusage_bar/aggregator.py openusage_bar/daily_history.py \
  openusage_bar/providers/builtins.py tests
git commit -m "refactor: collect provider facts before presentation"
```

### Task 3: Migrate direct subscription quota Providers

**Files:**
- Modify: `openusage_bar/codex_subscription.py`
- Modify: `openusage_bar/kiro.py`
- Modify: `openusage_bar/minimax.py`
- Modify: `openusage_bar/step_plan.py`
- Modify: `openusage_bar/generic.py`
- Test: `tests/test_codex_subscription.py`
- Test: `tests/test_kiro.py`
- Test: `tests/test_minimax.py`
- Test: `tests/test_step_plan.py`
- Test: `tests/test_generic.py`

- [ ] **Step 1: Add failing direct-result tests**

For every adapter, invoke `fetch_quota()` and assert the returned success/failure,
source ID, Provider ID, account scope, quota window, reset time and Unknown-not-zero
behavior. Add call-count assertions around Keychain and HTTP/session readers.

- [ ] **Step 2: Run the Provider tests to verify RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_codex_subscription tests.test_kiro tests.test_minimax \
  tests.test_step_plan tests.test_generic -v
```

Expected: direct `fetch_quota()` is absent and current tests rely on the mutable
`last_quota_result` side channel.

- [ ] **Step 3: Implement one fact fetch per Provider**

Move each Provider's bounded credential/network operation into `fetch_quota()`.
Return `QuotaFetchFailure` for auth, rate-limit, network, parse and unavailable
states using the existing sanitized codes. Return `QuotaFetchSuccess` only when
at least one validated `QuotaObservation` exists. Never convert a failure to a
zero-percent observation.

Keep any `fetch()` method only in a presentation wrapper that calls a shared
private payload function; do not place it in `ProviderBinding.quota_sources` and
do not mutate `last_quota_result`.

- [ ] **Step 4: Verify no duplicated source calls**

Run the Step 2 command again and assert every fixture performs at most one bounded
credential/network read per fact refresh.

- [ ] **Step 5: Commit Provider migrations in small commits**

```bash
git add openusage_bar/codex_subscription.py openusage_bar/kiro.py tests
git commit -m "refactor: collect local subscription quota facts directly"
git add openusage_bar/minimax.py openusage_bar/step_plan.py openusage_bar/generic.py tests
git commit -m "refactor: collect configured quota facts directly"
```

### Task 4: Remove non-quota cards from the quota registry

**Files:**
- Modify: `openusage_bar/providers/builtins.py`
- Modify: `openusage_bar/openai_organization.py`
- Modify: `openusage_bar/daily_feed.py`
- Modify: `openusage_bar/cost_feed.py`
- Test: `tests/test_adapter_registry.py`
- Test: `tests/test_openai_organization.py`
- Test: `tests/test_daily_feed.py`
- Test: `tests/test_cost_feed.py`

- [ ] **Step 1: Write failing registry tests**

Assert OpenAI Organization, Daily Usage Feed and Daily Cost Feed bindings contain
only their real `usage_sources` / `cost_sources`; they must not register a status
card as a quota source. Assert each binding still publishes its descriptor and
source health through importer success/failure.

- [ ] **Step 2: Run tests to verify RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_adapter_registry tests.test_openai_organization \
  tests.test_daily_feed tests.test_cost_feed -v
```

Expected: the three bindings still include card adapters in `quota_sources`.

- [ ] **Step 3: Remove the false quota registrations**

Delete `OpenAIOrganizationCardAdapter`, `DailyUsageFeedCardAdapter` and
`DailyCostFeedCardAdapter` from production `ProviderBinding.quota_sources`.
Keep Provider visibility from the descriptor and Source Health from the actual
usage/cost importer. Do not synthesize subscription capacity for these sources.

- [ ] **Step 4: Run tests and commit**

Run the Step 2 command; expected: all tests pass.

```bash
git add openusage_bar/providers/builtins.py openusage_bar/openai_organization.py \
  openusage_bar/daily_feed.py openusage_bar/cost_feed.py tests
git commit -m "refactor: remove status cards from quota sources"
```

### Task 5: Migrate API balance collection to the direct contract

**Files:**
- Modify: `openusage_bar/moonshot.py`
- Modify: `openusage_bar/providers/builtins.py`
- Test: `tests/test_moonshot.py`
- Test: `tests/test_balance_pipeline.py`

- [ ] **Step 1: Write failing direct balance tests**

Call `fetch_balance()` and assert success/failure, currency, numeric balance,
Provider/account scope, source ID, reset/expiry semantics and one HTTP request.
Assert a later failure marks source health stale without deleting Last-good balance.

- [ ] **Step 2: Run tests to verify RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_moonshot tests.test_balance_pipeline -v
```

Expected: the adapter still returns a card and exposes facts through
`last_balance_result`.

- [ ] **Step 3: Implement and verify the direct balance path**

Implement `fetch_balance()` with existing bounded HTTP and sanitized failures;
remove `last_balance_result`. Run the Step 2 command and expect all tests to pass.

- [ ] **Step 4: Commit**

```bash
git add openusage_bar/moonshot.py openusage_bar/providers/builtins.py tests
git commit -m "refactor: collect balance facts directly"
```

### Task 6: Complete OpenUsage discovery after WQ-20

**Files:**
- Modify: `openusage_bar/openusage_adapter.py`
- Modify: `openusage_bar/providers/builtins.py`
- Modify: `openusage_bar/aggregator.py`
- Test: `tests/test_openusage_adapter.py`
- Test: `tests/test_adapter_registry.py`
- Test: `tests/test_daily_history.py`

- [x] **Step 1: Require the frozen producer fixture**

Use only the WQ-20 `openusage-export/v1` decoder. Add tests proving dynamic Provider
snapshots produce bounded `ProviderInstance` and `SourceStatus` facts without
creating `ProviderCard`; empty coverage remains Unknown and malformed/oversized
exports fail closed.

- [x] **Step 2: Run tests to verify RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_openusage_adapter tests.test_adapter_registry \
  tests.test_daily_history -v
```

Expected: `OpenUsageAdapter.parse()` still returns `Overview` and the headless
builder still registers `openusage.cards` as a quota/card source.

- [x] **Step 3: Split discovery from presentation**

Replace the production card adapter with an `OpenUsageDiscoveryAdapter` that
returns Provider descriptors and sanitized source results from the frozen export.
Keep `OpenUsageDailyImporter` as the daily Token producer. Move card rendering, if
the legacy Python UI still requires it, into a presentation-only wrapper.

- [x] **Step 4: Remove the final core card dependency**

Delete `LegacyCardAdapter`; ensure `daily_history.py`, `providers/contracts.py`,
`providers/registry.py` and `build_headless_refresher()` contain no imports or type
references to `ProviderCard` or `Overview`.

- [x] **Step 5: Verify and commit WQ-19B**

Run the Step 2 command and expect all tests to pass.

```bash
git add openusage_bar tests
git commit -m "refactor: publish openusage discovery facts directly"
```

### Task 7: Run complete compatibility and release gates

**Files:**
- Modify only files required by regressions introduced by Tasks 1-6.

- [x] **Step 1: Prove the core no longer depends on cards**

```bash
! rg -n "ProviderCard|Overview|LegacyCardAdapter" \
  openusage_bar/providers/contracts.py openusage_bar/providers/registry.py \
  openusage_bar/daily_history.py
```

Expected: no matches.

- [x] **Step 2: Run all Python and Swift tests**

```bash
.build-venv/bin/python -m unittest discover -s tests -v
swift test --package-path swift_app -Xswiftc -warnings-as-errors
```

Expected: all tests pass; Local API v1 and generated Swift fixtures are unchanged.

- [x] **Step 3: Run security, privacy and release gates**

```bash
scripts/audit_dependencies.sh
.build-venv/bin/python scripts/release_secret_scan.py
.build-venv/bin/python scripts/privacy_scan.py \
  openusage_bar/resources/release-state.v1.json \
  openusage_bar/resources/provider-catalog.v1.json \
  openusage_bar/resources/local-api-v1.schema.json \
  swift_app/Sources/UsageCore/GeneratedProviderCatalog.swift \
  swift_app/Sources/UsageCore/GeneratedActivitySchema.swift
.build-venv/bin/python scripts/verify_release_metadata.py
scripts/build_app.sh
```

Expected: dependency audit has no known vulnerability, both scans report zero,
release metadata remains `0.6.0 (9)` until an explicitly authorized version change,
and the complete App bundle build passes.

- [x] **Step 4: Review compatibility evidence**

Confirm Snapshot/Changes payloads, `dataRevision`, quota history, Source Health,
Provider visibility, menu-bar values and N-1 fixtures have no breaking changes.
External Canary remains 0/5 and its 30-day clock remains `not_started`.

- [x] **Step 5: Commit final compatibility fixes**

```bash
git add openusage_bar swift_app tests scripts docs
git commit -m "test: verify fact-first provider compatibility"
```

## Completion boundary

WQ-19 is complete only when the headless Collector and durable ledger have zero
dependency on `ProviderCard`, `Overview`, card cache freshness or mutable adapter
side channels. Keeping these types in `presentation.py`, `ui.py` or an explicitly
named compatibility module is acceptable until the Python UI is separately retired.

This plan does not add request telemetry, change Loom, start external Canary,
publish a new App version or change Provider credentials.

## Verification record

2026-08-01: the card-dependency proof returned no matches. Python passed 938
tests twice during the release build; every product module remained at or above
80% line coverage (`aggregator` 83%, `openusage_export_v1` 92%). Swift passed
257 tests with 87.64% product line coverage. Dependency audit reported no known
vulnerabilities; tree/history secret scan and both privacy scans reported zero
matches. Release metadata remained `0.6.0 (9)`, producer and consumer
`providers` fixtures matched byte-for-byte, and the ad-hoc signed
`dist/OpenUsage Bar.app` passed deep strict signature and plist validation.
These are local repository gates only: no merge, publication, installation or
external Canary validation was performed.
