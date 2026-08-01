# Runtime Observation Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a privacy-safe, bounded and independently stored request-observation plane that exposes recent Token, cost, latency and status summaries without changing the durable ledger or Local API v1.

**Architecture:** Strict value objects decode one versioned ingestion document. A dedicated SQLite store keeps terminal observations for 24 hours under hard row and byte caps and publishes a separate runtime revision. The existing Collector executable receives documents on stdin and emits bounded read-only summaries; `activity.sqlite3`, SwiftUI, Provider credentials and Loom remain untouched.

**Tech Stack:** Python 3 dataclasses, standard-library JSON/SQLite/argparse, existing unittest and release gates.

---

### Task 1: Freeze ownership, privacy and capacity limits

**Files:**
- Create: `docs/adr/0002-runtime-observation-plane.md`
- Create: `docs/superpowers/plans/2026-08-01-runtime-observation.md`
- Modify: `docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md`

- [x] **Step 1: Record the separate-writer decision**

Record that Python Collector code is the only writer, raw observations use
`runtime.sqlite3`, and neither `activity.sqlite3` nor Local API v1 changes.

- [x] **Step 2: Freeze the privacy allowlist**

Allow only UTC timestamps, public Provider/model IDs, an `anon_` hexadecimal
scope, Token counters, terminal status, optional micro-unit cost, source and
quality. Explicitly reject Prompt, Response, credential, raw payload, upstream
request ID and direct identity fields.

- [x] **Step 3: Freeze hard bounds**

Use 24-hour retention, 100,000 rows, 64 MiB, 1 MiB ingestion documents, 256
observations per document, 24-hour query windows and 512 summary groups.

- [x] **Step 4: Verify the documents**

Run:

```bash
git diff --check
test -f docs/adr/0002-runtime-observation-plane.md
test -f docs/superpowers/plans/2026-08-01-runtime-observation.md
```

Expected: all commands exit 0.

### Task 2: Define the strict runtime observation contract

**Files:**
- Create: `openusage_bar/runtime_observation.py`
- Create: `tests/test_runtime_observation.py`

- [x] **Step 1: Write failing contract tests**

Test a schema-v1 document containing a completed request with:

```python
{
    "observationId": "obs_0123456789abcdef0123456789abcdef",
    "providerId": "openai",
    "modelId": "gpt-5",
    "scopeRef": "anon_0123456789abcdef",
    "startedAt": "2026-08-01T00:00:00Z",
    "firstTokenAt": "2026-08-01T00:00:01Z",
    "completedAt": "2026-08-01T00:00:03Z",
    "inputTokens": 12,
    "outputTokens": 3,
    "cacheReadTokens": 4,
    "cacheCreationTokens": 0,
    "reasoningTokens": 1,
    "totalTokens": 20,
    "status": "completed",
    "costMicros": 25,
    "costCurrency": "usd",
    "sourceId": "litellm.otel",
    "quality": "provider_reported"
}
```

Also test unknown/private fields, invalid timestamp ordering, noncanonical IDs,
inconsistent Token totals, invalid status/cost pairs, more than 256 rows and a
document larger than 1 MiB.

- [x] **Step 2: Run the tests to verify RED**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_runtime_observation -v
```

Expected: FAIL because `openusage_bar.runtime_observation` does not exist.

- [x] **Step 3: Implement the minimal strict decoder**

Add immutable `RuntimeObservation` and `RuntimeIngestDocument` values plus
`decode_runtime_document()`. Require exact key sets, canonical UTC timestamps,
terminal status, safe identifiers and nonnegative bounded integers. Derive
`ttft_ms` and `duration_ms`; never retain the raw JSON document.

- [x] **Step 4: Run the tests to verify GREEN**

Run the Task 2 command and expect all tests to pass.

- [ ] **Step 5: Commit the contract slice**

```bash
git add docs/adr/0002-runtime-observation-plane.md \
  docs/superpowers/plans/2026-08-01-runtime-observation.md \
  openusage_bar/runtime_observation.py tests/test_runtime_observation.py
git commit -m "feat(runtime): freeze bounded observation contract"
```

### Task 3: Add the separate short-retention store

**Files:**
- Create: `openusage_bar/runtime_store.py`
- Create: `tests/test_runtime_store.py`

- [ ] **Step 1: Write failing store tests**

Use a temporary `runtime.sqlite3` to prove:

- inserting and retrying the same observation is idempotent;
- the revision advances only when stored facts change;
- observations older than 24 hours are pruned;
- more than 100,000 rows prune oldest-first;
- the SQLite page cap is at most 64 MiB;
- a symlink database is rejected and a new database is mode `0600`;
- opening the runtime store never creates or modifies `activity.sqlite3`.

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_runtime_store -v
```

Expected: FAIL because `RuntimeStore` does not exist.

- [ ] **Step 3: Implement schema-v1 storage**

Create only `runtime_observations` and `runtime_meta`. Validate existing table
signatures and `PRAGMA user_version=1`, use transactions, set the page cap from
the actual page size, reject newer or incompatible schemas, and prune by both
retention and row count after every accepted batch.

- [ ] **Step 4: Implement bounded summaries**

Return one `RuntimeSummary` containing `runtimeRevision`, exact UTC window,
coverage, overall Token/status/cost/latency totals and at most 512 sorted
Provider/model/scope groups. A complete empty window is covered zero; an
oversized group set is partial and never silently complete.

- [ ] **Step 5: Run the tests to verify GREEN**

Run the Task 3 command and expect all tests to pass.

- [ ] **Step 6: Commit the storage slice**

```bash
git add openusage_bar/runtime_store.py tests/test_runtime_store.py
git commit -m "feat(runtime): add short-retention observation store"
```

### Task 4: Expose bounded Collector CLI ingestion and summaries

**Files:**
- Modify: `openusage_bar/collector_cli.py`
- Modify: `tests/test_collector_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add tests proving:

```text
runtime-ingest --database /absolute/runtime.sqlite3
runtime-summary --database /absolute/runtime.sqlite3 --window-seconds 3600
```

The ingest command reads only stdin, emits counts/revision without echoing the
document, rejects oversized/private input with sanitized stderr, and does not
open `ActivityStore`. Summary emits stable compact JSON with no credential or
direct identity fields. Relative/symlink paths and windows outside 60..86400
seconds fail closed.

- [ ] **Step 2: Run the CLI tests to verify RED**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_collector_cli -v
```

Expected: the new runtime commands fail because the parser does not know them.

- [ ] **Step 3: Implement the minimal commands**

Parse runtime commands before opening `ActivityStore`. Use a bounded stdin
reader, `decode_runtime_document()`, `RuntimeStore.ingest()` and
`RuntimeStore.summary()`. Use the standard compact JSON renderer and sanitized
exit codes only; do not add a network route or accept telemetry in argv.

- [ ] **Step 4: Run the CLI tests to verify GREEN**

Run the Task 4 command and expect all tests to pass.

- [ ] **Step 5: Commit the CLI slice**

```bash
git add openusage_bar/collector_cli.py tests/test_collector_cli.py
git commit -m "feat(runtime): expose bounded local ingestion"
```

### Task 5: Verify WQ-21 without claiming scheduler readiness

**Files:**
- Modify: `docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md`
- Modify: `docs/superpowers/plans/2026-08-01-runtime-observation.md`
- Modify only files required by regressions introduced by Tasks 1-4.

- [ ] **Step 1: Prove database and API separation**

```bash
! rg -n "RuntimeStore|runtime_observation|runtime\.sqlite3" \
  openusage_bar/activity_store.py openusage_bar/local_api.py \
  openusage_bar/activity_schema.py
```

Expected: no matches.

- [ ] **Step 2: Run targeted and complete suites**

```bash
.build-venv/bin/python -m unittest \
  tests.test_runtime_observation tests.test_runtime_store \
  tests.test_collector_cli -v
.build-venv/bin/python -m unittest discover -s tests -v
swift test --package-path swift_app -Xswiftc -warnings-as-errors
```

- [ ] **Step 3: Run coverage, dependency, privacy and build gates**

```bash
scripts/audit_dependencies.sh
.build-venv/bin/python scripts/release_secret_scan.py --history
scripts/build_app.sh
```

Expected: no known dependency vulnerability, zero secret/privacy matches,
every Python product module and Swift product lines at or above 80%, release
metadata remains `0.6.0 (9)`, and the signed App bundle completes.

- [ ] **Step 4: Update queue status honestly**

Record repository-local implementation and exact gate counts. Keep Provider
adapters, live telemetry producers, Loom reservations/policy, external Canary,
merge and publication open.

- [ ] **Step 5: Commit final verification**

```bash
git add docs openusage_bar tests
git commit -m "docs: record runtime observation verification"
```

## Completion boundary

WQ-21 is repository-complete when strict ingestion, separate bounded storage,
read-only summaries, database/API separation and full release gates are proven.
It does not make live LiteLLM/CLIProxyAPI/hooks available, does not authorize
Loom scheduling, and does not qualify any external Canary machine.
