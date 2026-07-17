# OpenUsage Bar Core Ledger and API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local ledger a complete revisioned fact source and expose one coherent resource snapshot to native clients and optional generic local consumers.

**Architecture:** Split schema/record declarations from storage operations, then make every public fact mutation participate in the change cursor. A single SQLite read transaction produces a resource snapshot; QueryService is the only wire-model boundary used by both API and CLI.

**Tech Stack:** Python dataclasses, sqlite3, HTTP/1.1 Unix socket API, unittest, JSON Schema, Swift generated constants and Swift Testing.

---

**Codex skill resolution:** In this environment, `superpowers:executing-plans` maps to the installed `executing-plans` skill. Use the subagent-driven option only when `subagent-driven-development` is actually available.

### Task 1: Split schema and record declarations without behavior changes

**Files:**
- Create: `openusage_bar/activity_records.py`
- Create: `openusage_bar/activity_schema.py`
- Modify: `openusage_bar/activity_store.py`
- Modify: `scripts/build_app.sh`
- Test: `tests/test_build_script.py`
- Test: `tests/test_activity_store.py`

- [ ] **Step 1: Add build-gate tests for the new modules**

Add assertions to `tests/test_build_script.py` that the coverage gate contains `openusage_bar.activity_records` and `openusage_bar.activity_schema`.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_build_script -v
```

Expected: FAIL because the modules are not covered by the build gate.

- [ ] **Step 3: Move declarations by responsibility**

Move dataclasses, constants, and value validation from `activity_store.py` into `activity_records.py`. Move schema version, table/index signatures, and DDL into `activity_schema.py`. Keep `ActivityStore` public imports backward compatible:

```python
from .activity_records import (
    ChangeRecord,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
    SourceStatus,
)
from .activity_schema import LEDGER_SCHEMA_VERSION, create_schema, migrate_schema
```

Use commit `9e57595` only as a move map; do not merge its unrelated branch history.

- [ ] **Step 4: Run behavior-preservation tests**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_activity_store tests.test_build_script -v
```

Expected: PASS with no schema or payload changes.

- [ ] **Step 5: Commit the pure refactor**

```bash
git add openusage_bar/activity_records.py openusage_bar/activity_schema.py \
  openusage_bar/activity_store.py scripts/build_app.sh \
  tests/test_build_script.py tests/test_activity_store.py
git commit -m "refactor: split ledger records and schema"
```

### Task 2: Make `dataRevision` cover every public fact change

**Files:**
- Modify: `openusage_bar/activity_records.py`
- Modify: `openusage_bar/activity_schema.py`
- Modify: `openusage_bar/activity_store.py`
- Modify: `swift_app/Sources/UsageCore/UsageRepository.swift`
- Test: `tests/test_activity_store.py`
- Test: `swift_app/Tests/UsageCoreTests/RepositoryTests.swift`

- [ ] **Step 1: Write failing cursor tests**

Add tests with this behavior to `tests/test_activity_store.py`:

```python
def test_source_health_change_advances_public_cursor(self):
    before = self.store.snapshot_changes(0, 100).cursor
    self.store.record_source_failure(
        "codex", "current.quota", "timeout", self.now
    )
    page = self.store.snapshot_changes(before, 100)
    self.assertEqual([row.record_type for row in page.rows], ["source_status"])
    self.assertGreater(page.cursor, before)

def test_hourly_quota_snapshot_is_visible_in_change_feed(self):
    self.store.record_quota(self.quota(observed_at="2026-07-18T00:00:00Z"))
    before = self.store.snapshot_changes(0, 100).cursor
    self.store.record_quota(self.quota(observed_at="2026-07-18T01:00:00Z"))
    page = self.store.snapshot_changes(before, 100)
    self.assertEqual([row.record_type for row in page.rows], ["quota_snapshot"])

def test_known_zero_coverage_round_trips_through_change_feed(self):
    before = self.store.snapshot_changes(0, 100).cursor
    self.store.replace_daily_usage("codex", "2026-07-18", [])
    self.store.replace_daily_costs("openai", "2026-07-18", [])
    inserted = self.store.snapshot_changes(before, 100)
    self.assertEqual(
        [row.record_type for row in inserted.rows],
        ["daily_coverage", "daily_cost_coverage"],
    )

    self.store.purge_before("2026-07-19", "2026-07-19T00:00:00Z")
    deleted = self.store.snapshot_changes(inserted.cursor, 100)
    self.assertEqual(
        [(row.record_type, row.operation) for row in deleted.rows],
        [("daily_cost_coverage", "delete"), ("daily_coverage", "delete")],
    )
```

Also cover success, failure, delete, retention deletion, stale writes, duplicate no-op writes, and the `/v1/changes` serialization of both coverage record types. Coverage is a public fact because it distinguishes observed zero from unknown.

- [ ] **Step 2: Run tests and verify RED**

```bash
.build-venv/bin/python -m unittest tests.test_activity_store -v
```

Expected: source status and history-only quota changes do not appear in the change feed.

- [ ] **Step 3: Introduce ledger schema v4**

Add `revision` and `payload_hash` to `source_status`. Add change types `source_status` and `quota_snapshot`. Implement an idempotent v3-to-v4 migration that preserves every existing row and initializes revisions without decreasing the existing global cursor.

The semantic rule is:

```python
PUBLIC_CHANGE_TYPES = frozenset({
    "daily_cost",
    "daily_cost_coverage",
    "daily_coverage",
    "daily_usage",
    "ledger_schema",
    "provider_instance",
    "quota",
    "quota_snapshot",
    "source_status",
})
```

Every inserted, updated, or deleted public row appends exactly one sanitized change record. Duplicate writes with the same semantic hash append none. The v3-to-v4 migration appends one `ledger_schema` change after the transaction has preserved and validated existing rows; incremental consumers that see this type must obtain a fresh `/v1/snapshot` instead of expecting one change record per pre-v4 row.

- [ ] **Step 4: Update Swift schema acceptance**

Teach `UsageRepository` to accept ledger v4 and verify the new columns. Retain read support for v3 during the migration release only; writing remains Python-owned.

- [ ] **Step 5: Verify migration and cursor monotonicity**

```bash
.build-venv/bin/python -m unittest tests.test_activity_store -v
swift test --package-path swift_app --filter RepositoryTests
```

Expected: PASS; v3 fixtures open and migrate without row loss, and all public changes are cursor-visible.

- [ ] **Step 6: Commit**

```bash
git add openusage_bar/activity_records.py openusage_bar/activity_schema.py \
  openusage_bar/activity_store.py tests/test_activity_store.py \
  swift_app/Sources/UsageCore/UsageRepository.swift \
  swift_app/Tests/UsageCoreTests/RepositoryTests.swift
git commit -m "feat: revision every public ledger fact"
```

### Task 3: Add a single-transaction resource snapshot

**Files:**
- Modify: `openusage_bar/activity_records.py`
- Modify: `openusage_bar/activity_store.py`
- Modify: `openusage_bar/query.py`
- Test: `tests/test_activity_store.py`
- Test: `tests/test_query.py`

- [ ] **Step 1: Define the store snapshot contract in a failing test**

The snapshot must contain one cursor and all facts needed by native surfaces and generic resource consumers:

```python
@dataclass(frozen=True)
class ResourceStateSnapshot:
    local_day: str
    cursor: int
    today_tokens: int
    model_count: int
    covered_day_count: int
    quota_states: tuple[QuotaState, ...]
    provider_instances: tuple[ProviderInstance, ...]
    source_statuses: tuple[SourceStatus, ...]
```

Add `ResourceSnapshotTests(unittest.TestCase)` to `tests/test_activity_store.py`. Its concurrent writer test pauses between internal SELECT statements and proves the returned rows and cursor all come from one read transaction.

- [ ] **Step 2: Verify RED**

```bash
.build-venv/bin/python -m unittest \
  tests.test_activity_store.ResourceSnapshotTests.test_resource_snapshot_is_revision_consistent -v
```

Expected: FAIL because `snapshot_resource_state` does not exist.

- [ ] **Step 3: Implement `ActivityStore.snapshot_resource_state(day)`**

Validate the canonical local day, enter one `_read_snapshot()` transaction, read summary, quota states, provider instances, source statuses, and the cursor, then return `ResourceStateSnapshot`. Do not call existing public snapshot methods from inside the transaction; extract private `_locked` SELECT helpers to avoid nested transactions.

- [ ] **Step 4: Define the public QueryService result**

Add these wire dataclasses to `query.py`:

```python
@dataclass(frozen=True)
class SnapshotSummary:
    today_tokens: int
    model_count: int
    covered_day_count: int

@dataclass(frozen=True)
class ResourceSnapshotResult(ResultEnvelope):
    local_day: str
    summary: SnapshotSummary
    quota_windows: tuple[CapacityProvider, ...]
    providers: tuple[ProviderInstanceItem, ...]
    sources: tuple[SourceStatusItem, ...]
    catalog_revision: str
```

Implement `QueryService.resource_snapshot(today: date)` by converting exactly one `ResourceStateSnapshot`. Return every quota window; do not choose a route or synthesize missing numbers.

- [ ] **Step 5: Verify the focused contract**

```bash
.build-venv/bin/python -m unittest tests.test_activity_store tests.test_query -v
```

Expected: PASS; all nested facts share the envelope's `dataRevision`.

- [ ] **Step 6: Commit**

```bash
git add openusage_bar/activity_records.py openusage_bar/activity_store.py \
  openusage_bar/query.py tests/test_activity_store.py tests/test_query.py
git commit -m "feat: add coherent resource snapshot"
```

### Task 4: Expose the snapshot through API and CLI

**Files:**
- Modify: `openusage_bar/local_api.py`
- Modify: `openusage_bar/collector_cli.py`
- Test: `tests/test_local_api.py`
- Test: `tests/test_collector_cli.py`
- Modify: `docs/api/local-api-v1.md`

- [ ] **Step 1: Write failing API and CLI parity tests**

Add `GET /v1/snapshot?today=2026-07-18` and:

```bash
openusage-bar snapshot --today 2026-07-18 --format json --offline
```

Under a fixed clock and ledger fixture, decode both JSON responses, remove no fields, and assert exact equality.

- [ ] **Step 2: Verify RED**

```bash
.build-venv/bin/python -m unittest tests.test_local_api tests.test_collector_cli -v
```

Expected: both entry points reject the unknown snapshot command/route.

- [ ] **Step 3: Add the shared route and command**

Register only `today` as an optional canonical date parameter. When omitted, resolve the same machine-local calendar day in API and CLI. Both paths must call `QueryService.resource_snapshot` and `to_wire`; neither may construct its own JSON.

- [ ] **Step 4: Formalize incremental changes**

Add `has_more: bool` to `ChangePage` and serialize it as `hasMore`. Define `hasMore` as `nextCursor < dataRevision`. Keep `payloadJson` and all current v1 fields intact. Document that clients must ignore unknown `recordType` values and persist `nextCursor` only after processing the complete page.

- [ ] **Step 5: Run parity and compatibility tests**

```bash
.build-venv/bin/python -m unittest \
  tests.test_query tests.test_local_api tests.test_collector_cli -v
```

Expected: PASS, including all pre-existing v1 routes.

- [ ] **Step 6: Commit**

```bash
git add openusage_bar/local_api.py openusage_bar/collector_cli.py \
  openusage_bar/query.py tests/test_query.py tests/test_local_api.py \
  tests/test_collector_cli.py docs/api/local-api-v1.md
git commit -m "feat: publish local resource snapshot"
```

### Task 5: Publish machine-readable schemas and cross-language gates

**Files:**
- Create: `openusage_bar/resources/local-api-v1.schema.json`
- Create: `scripts/generate_local_api_schema.py`
- Create: `scripts/generate_swift_activity_schema.py`
- Create: `swift_app/Sources/UsageCore/GeneratedActivitySchema.swift`
- Create: `tests/test_local_api_schema.py`
- Create: `swift_app/Tests/UsageCoreTests/CrossLanguageContractTests.swift`
- Modify: `scripts/build_app.sh`
- Modify: `openusage_bar/local_api.py`

- [ ] **Step 1: Add failing schema checks**

Tests must reject missing `schemaVersion`, non-integer `dataRevision`, `unknown` facts with numeric values, secret-like fields, and changes without a cursor. They must accept the committed snapshot and change fixtures.

- [ ] **Step 2: Generate and serve the schema**

Generate a deterministic Draft 2020-12 JSON Schema for the v1 envelope, snapshot, changes, and error response. Add `GET /v1/schema.json`; preserve the existing descriptive `/v1/schema` route. The new route keeps the normal API envelope and returns the Draft 2020-12 document under a `schema` field, so `schemaVersion`, `dataRevision`, and `generatedAt` remain present on every successful v1 response.

- [ ] **Step 3: Generate Swift ledger signatures**

Produce `GeneratedActivitySchema.swift` from `activity_schema.py` and replace the duplicated hand-written table/index signatures in `UsageRepository`. Fail the build when regenerated output differs from the committed file.

- [ ] **Step 4: Add a cross-language golden fixture**

Use Python to create a deterministic synthetic SQLite ledger and expected snapshot JSON under a temporary directory. Swift opens that ledger through `UsageRepository`; the test compares provider identity, Token totals, quota windows, source health, and revision against the expected JSON.

- [ ] **Step 5: Run the full gate**

```bash
.build-venv/bin/python -m unittest discover -s tests -v
swift test --package-path swift_app --enable-code-coverage -Xswiftc -warnings-as-errors
scripts/build_app.sh
```

Expected: all tests pass, generated files are current, both coverage gates remain at least 80%, and privacy scans report zero findings.

- [ ] **Step 6: Commit**

```bash
git add openusage_bar/resources/local-api-v1.schema.json \
  scripts/generate_local_api_schema.py scripts/generate_swift_activity_schema.py \
  swift_app/Sources/UsageCore/GeneratedActivitySchema.swift \
  tests/test_local_api_schema.py \
  swift_app/Tests/UsageCoreTests/CrossLanguageContractTests.swift \
  openusage_bar/local_api.py scripts/build_app.sh
git commit -m "feat: publish versioned fact schemas"
```

## Acceptance gate

- One snapshot is produced from one SQLite read transaction and one revision.
- Source health, quota history, provider identity, daily usage, and daily cost mutations are visible through `/v1/changes`.
- API and CLI snapshot JSON are identical under the same clock and ledger.
- v3-to-v4 migration preserves every row and never decreases the public cursor.
- Existing Local API v1 consumers continue to pass unchanged tests.
- No schema contains credentials, raw provider payloads, prompts, responses, email, or direct account identity.
