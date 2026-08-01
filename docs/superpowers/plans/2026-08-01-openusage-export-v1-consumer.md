# OpenUsage Export v1 Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consume `openusage-export/v1` only after explicit capability negotiation while preserving the existing bounded fallback and Last-good/Unknown semantics.

**Architecture:** Add a strict OS-neutral decoder and frozen N-1 fixtures, then put a one-process capability probe in front of `OpenUsageDailyImporter`. A supported producer is preferred; unsupported binaries and failed contract calls use the existing legacy daily command, and the two sources are selected rather than summed.

**Tech Stack:** Python 3.13, unittest, bounded child processes, canonical SQLite activity ledger, JSON fixtures.

---

### Task 1: Freeze the consumer wire contract

**Files:**
- Create: `openusage_bar/openusage_export_v1.py`
- Create: `tests/fixtures/openusage-export-v1/capabilities.json`
- Create: `tests/fixtures/openusage-export-v1/daily-usage.json`
- Create: `tests/fixtures/openusage-export-v1/daily-usage-empty-complete.json`
- Create: `tests/fixtures/openusage-export-v1/daily-usage-partial.json`
- Create: `tests/test_openusage_export_v1.py`

- [ ] **Step 1: Write failing strict-decoder tests**

Test exact contract/schema/kind, UTC generated time, request echo, inclusive range,
coverage states, row bounds, nonnegative integer Tokens, nullable reasoning, three
counting conventions, three qualities, cursor bounds and additive unknown-field
compatibility. Reject booleans as integers and never echo rejected values.

```python
def test_failed_empty_is_not_covered_zero():
    page = decode_daily(fixture("daily-usage-partial.json"))
    assert page.coverage.state == "partial"
    assert page.covered_zero is False
```

- [ ] **Step 2: Run RED**

Run: `.build-venv/bin/python -m unittest tests.test_openusage_export_v1 -v`

Expected: FAIL because the decoder does not exist.

- [ ] **Step 3: Implement frozen dataclasses and decoder**

Define `ExportCapabilities`, `ExportCoverage`, `ExportDailyRow`, `ExportPage` and
`decode_capabilities`/`decode_daily`. Accept additive fields but require every frozen
field. Forbid credential, endpoint, cookie/session, Prompt/Response and raw payload
keys at every nesting level.

- [ ] **Step 4: Run GREEN and commit**

Run: `.build-venv/bin/python -m unittest tests.test_openusage_export_v1 -v`

Commit: `feat(openusage): decode export v1 facts`

### Task 2: Add one bounded capability probe

**Files:**
- Modify: `openusage_bar/openusage_export_v1.py`
- Modify: `openusage_bar/daily_history.py`
- Test: `tests/test_openusage_export_v1.py`
- Test: `tests/test_daily_history.py`

- [ ] **Step 1: Write failing probe tests**

Assert direct argv, shell disabled, closed stdin, the credential-free child environment,
3-second timeout, 128 KiB output limits, exact contract/kinds and one cached probe per
importer. Unsupported, timeout, oversize, nonzero, malformed and wrong-version results
become `unsupported` without logging payloads.

- [ ] **Step 2: Run RED**

Run: `.build-venv/bin/python -m unittest tests.test_openusage_export_v1 tests.test_daily_history -v`

- [ ] **Step 3: Implement capability negotiation**

Invoke only this direct argv:

```python
[openusage_path, "export", "--output", "-", "--format", "json",
 "--contract", "openusage-export/v1", "--kind", "capabilities"]
```

Cache only the typed supported/unsupported result, never stdout, stderr or paths.

- [ ] **Step 4: Run GREEN and commit**

Commit: `feat(openusage): negotiate export v1`

### Task 3: Prefer contract facts without double-counting

**Files:**
- Modify: `openusage_bar/daily_history.py`
- Test: `tests/test_daily_history.py`
- Test: `tests/test_aggregator.py`

- [ ] **Step 1: Write failing contract-first/fallback tests**

Cover supported success, complete empty, multi-page success, malformed page, cursor
loop, scope mismatch, later-page failure, unsupported producer and legacy success.
Prove v1 and legacy rows are selected, never summed; failed/partial empty preserves
Last-good and never writes zero.

- [ ] **Step 2: Run RED**

Run: `.build-venv/bin/python -m unittest tests.test_daily_history tests.test_aggregator -v`

- [ ] **Step 3: Implement exact bounded collection**

Invoke exact Provider/since/until with limit 500 and opaque cursor. Bound collection
to 366 days, 512 models/day, 200000 rows, 16 MiB/page and 100 pages. Validate every
page before constructing any `DailyUsageRow`; only complete coverage plus the final
page can commit covered-zero days. On any v1 failure, invoke legacy once.

- [ ] **Step 4: Preserve provenance and Last-good**

V1 rows use source `openusage.export.v1`; legacy rows retain `openusage.daily` and
fallback quality. Empty legacy, timeout or partial v1 never replaces official or
Last-good facts. Do not change SQLite or Local API v1.

- [ ] **Step 5: Run GREEN and commit**

Run: `.build-venv/bin/python -m unittest tests.test_openusage_export_v1 tests.test_daily_history tests.test_aggregator -v`

Commit: `feat(openusage): prefer export v1 daily facts`

### Task 4: Prove N-1 and legacy compatibility

**Files:**
- Modify: `tests/test_openusage_export_v1.py`
- Modify: `tests/test_daily_history.py`
- Modify: `docs/provider-authoritative-sources.md`
- Modify: `docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md`

- [ ] **Step 1: Add producer/consumer fixture agreement tests**

Decode all four producer fixtures. Accept additive fields, reject missing required
fields, and require exact provider filter, daily usage, coverage, page/range bounds
and all Token conventions in capabilities.

- [ ] **Step 2: Document source priority**

Record the exact selection chain:

```text
official Provider source
  -> openusage-export/v1 after successful negotiation
  -> bounded legacy OpenUsage daily command
  -> Last-good
  -> No data
```

The first successful source wins; sources are never added.

- [ ] **Step 3: Run compatibility tests and commit**

Run:

```bash
.build-venv/bin/python -m unittest \
  tests.test_openusage_export_v1 tests.test_daily_history \
  tests.test_query tests.test_local_api tests.test_collector_cli -v
git diff --check
```

Commit: `test(openusage): freeze export v1 compatibility`

### Task 5: Run full release gates and hand WQ-19B an exact boundary

**Files:**
- Modify only regressions introduced by Tasks 1-4.

- [ ] **Step 1: Run complete gates**

```bash
.build-venv/bin/python -m unittest discover -s tests -v
swift test --package-path swift_app -Xswiftc -warnings-as-errors
scripts/audit_dependencies.sh
.build-venv/bin/python scripts/release_secret_scan.py
.build-venv/bin/python scripts/verify_release_metadata.py
scripts/build_app.sh
```

- [ ] **Step 2: Verify the completion boundary**

Confirm an unsupported stock binary yields unchanged ledger facts through legacy
fallback and a capable fixture yields identical canonical facts. Do not remove
`OpenUsageAdapter` cards here; WQ-19B performs that deletion after this contract is
green.

- [ ] **Step 3: Commit final verification**

Commit: `test(openusage): verify export v1 release gates`

## Completion boundary

WQ-20 completes only when producer fixtures and consumer decoder agree, supported
producers are preferred, unsupported producers fall back safely, and complete-empty
is the only path that can create covered zero. WQ-19B may then publish OpenUsage
discovery as facts instead of cards.
