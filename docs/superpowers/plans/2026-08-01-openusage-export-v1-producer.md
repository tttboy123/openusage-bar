# OpenUsage Export v1 Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a stable `openusage-export/v1` producer contract for exact-provider daily Token facts, truthful coverage, bounded pagination and capability negotiation.

**Architecture:** Implement the contract additively in the OpenUsage fork on a clean upstream branch plus the isolated exact-provider patch. Existing export/report JSON remains compatible; consumers opt in with `--contract openusage-export/v1`, so unsupported installations retain their legacy fallback.

**Tech Stack:** Go, Cobra, OpenUsage report/event packages, JSON golden fixtures, `go test`.

---

## Frozen wire boundary

Daily facts are requested with:

```bash
openusage export --output - --format json \
  --contract openusage-export/v1 --kind daily_usage \
  --provider codex --since 2026-07-01 --until 2026-07-31 --limit 500
```

Capabilities are requested with the same command and `--kind capabilities`.
The daily envelope contains `contract`, `schema_version`, `generated_at`,
`openusage_version`, `kind`, an exact request echo, coverage, rows and page.
Rows contain only day, Provider/model IDs, input/output/cache/reasoning/total Tokens,
counting convention and quality. `coverage.state=complete` plus empty rows means
covered zero; partial/none plus empty rows never means zero. Cost, Prompt, Response,
credentials, raw payload, project/session labels and direct account identity are out
of scope.

### Task 1: Add the explicit contract domain

**Repository:** `/Users/lune/Documents/Codex/2026-07-17/bang/work/openusage-export-v1`

**Files:**
- Create: `internal/exportv1/types.go`
- Create: `internal/exportv1/types_test.go`

- [x] **Step 1: Write failing validation and JSON tests**

Test exact contract/kind enums, UTC timestamps, inclusive ranges of at most 366 days,
limits 1...1000, exact Provider IDs, nonnegative integer Token fields, nullable
reasoning, total/convention consistency and forbidden-field absence.

```go
func TestDailyEnvelopeFailedEmptyIsNotCoveredZero(t *testing.T) {
    env := validDailyEnvelope()
    env.Rows = nil
    env.Coverage.State = CoverageNone
    if err := env.Validate(); err != nil { t.Fatal(err) }
    if env.CoveredZero() { t.Fatal("failed empty became zero") }
}
```

- [x] **Step 2: Run RED**

Run: `go test ./internal/exportv1 -run Test -count=1`

Expected: FAIL because the package does not exist.

- [x] **Step 3: Implement immutable wire types**

Define `Contract = "openusage-export/v1"`, schema `1`, capabilities/daily kinds,
complete/partial/none coverage, three counting conventions, three qualities, request,
coverage, daily row, page and envelope types. Validation fails closed without echoing
rejected values.

Before publication, WQ-19B exposed a missing explicit discovery boundary: daily Token
rows cannot be used to infer installed Providers. The same v1 contract therefore also
freezes a `providers` kind with at most 512 sorted, account-free rows containing only
`provider_id`, sanitized state and UTC observation time. Complete empty is a truthful
"none discovered" result; partial/none coverage cannot replace Last-good discovery.

- [x] **Step 4: Run GREEN and commit**

Run: `go test ./internal/exportv1 -count=1`

Commit: `feat(export): define openusage-export v1 contract`

### Task 2: Produce truthful daily facts and coverage

**Files:**
- Create: `internal/exportv1/daily.go`
- Create: `internal/exportv1/daily_test.go`
- Modify: `cmd/openusage/report.go`

- [x] **Step 1: Write failing source-outcome tests**

Cover successful empty, successful model rows, failed empty and mixed partial
collection. A provider request may claim complete only when its chosen source examined
the entire requested range successfully.

- [x] **Step 2: Run RED**

Run: `go test ./internal/exportv1 -run TestBuildDaily -count=1`

Expected: FAIL because `BuildDaily` is undefined.

- [x] **Step 3: Separate collection outcome from presentation notes**

Add a typed internal source outcome to report collection while preserving current
human notes. Aggregate by local calendar day and canonical model, omit cost and direct
identity, and set partial/none instead of inventing complete coverage on failures.

- [x] **Step 4: Verify and commit**

Run: `go test ./internal/exportv1 ./internal/report ./cmd/openusage -count=1`

Commit: `feat(export): publish daily token coverage`

### Task 3: Add deterministic bounded pagination

**Files:**
- Create: `internal/exportv1/cursor.go`
- Create: `internal/exportv1/cursor_test.go`
- Modify: `internal/exportv1/daily.go`

- [x] **Step 1: Write failing cursor tests**

Test stable `(day, provider_id, model_id)` order, maximum 1000 rows, URL-safe opaque
cursors, exact request binding, no duplicate keys across pages, malformed/foreign
cursor rejection and final-page completion.

- [x] **Step 2: Run RED**

Run: `go test ./internal/exportv1 -run 'TestCursor|TestPage' -count=1`

- [x] **Step 3: Implement keyset pagination**

Encode only version, Provider, since/until and last day/model key. Never encode rows,
credentials or direct identity. Return stable `invalid_cursor` errors.

- [x] **Step 4: Run GREEN and commit**

Run: `go test ./internal/exportv1 -count=1`

Commit: `feat(export): bound v1 daily pages`

### Task 4: Wire capability negotiation without changing legacy export

**Files:**
- Modify: `cmd/openusage/export.go`
- Modify: `cmd/openusage/export_test.go`
- Modify: `internal/export/types.go`
- Modify: `internal/export/export.go`
- Test: `internal/export/provider_filter_test.go`

- [x] **Step 1: Write failing CLI tests**

Assert legacy export keeps its current shape. The explicit contract accepts only
capabilities/daily usage; daily requires Provider/since/until, rejects CSV and
out-of-bound inputs, and writes one clean JSON envelope to stdout.

- [x] **Step 2: Run RED**

Run: `go test ./cmd/openusage ./internal/export -run TestExport -count=1`

- [x] **Step 3: Add flags and exact dispatch**

Add `--contract`, `--kind`, `--since`, `--until`, `--limit` and `--cursor`. Dispatch
only for the exact contract. Reuse exact `--provider`; never match aliases, display
names, prefixes or substrings.

- [x] **Step 4: Run GREEN and commit**

Run: `go test ./cmd/openusage ./internal/export ./internal/exportv1 -count=1`

Commit: `feat(export): negotiate openusage-export v1`

### Task 5: Freeze current/N-1 fixtures and run producer gates

**Files:**
- Create: `internal/exportv1/testdata/capabilities-v1.json`
- Create: `internal/exportv1/testdata/daily-usage-v1.json`
- Create: `internal/exportv1/testdata/daily-usage-v1-empty-complete.json`
- Create: `internal/exportv1/testdata/daily-usage-v1-partial.json`
- Create: `internal/exportv1/fixture_test.go`

- [x] **Step 1: Add exact golden and additive-field tests**

The deterministic encoder must equal each fixture. The decoder accepts an unknown
additive top-level field but rejects removed/renamed required fields.

- [x] **Step 2: Run complete gates**

Run:

```bash
go test ./internal/exportv1 ./internal/export ./cmd/openusage -count=1
env HOME=/tmp/openusage-export-v1-test-home go test ./...
gofmt -w internal/exportv1 cmd/openusage/export.go internal/export
git diff --check
```

Expected: all tests pass and formatting is clean.

The isolated HOME is required because three pre-existing upstream tests assume
Hermes/Kiro source files are absent; the developer machine contains real source
files under HOME. The same three failures reproduce on untouched upstream main.

- [x] **Step 3: Build and inspect the real CLI**

Run capabilities plus complete-empty daily through a temporary binary. Confirm one
JSON envelope on stdout and no credentials, Prompt/Response, raw payload or direct
identity in output/stderr.

- [x] **Step 4: Commit verification**

Commit: `test(export): freeze v1 producer fixtures`

## Completion boundary

Producer work ends with a green branch in the user's OpenUsage fork. Opening or
merging an upstream PR is a separate external action. No consumer requires this
contract until capability negotiation succeeds.

## Verification record

2026-08-01: focused Go packages passed; the complete Go suite passed with an
isolated empty HOME. The same three HOME-sensitive Hermes/Kiro tests fail on
untouched upstream main when the developer's real source files are visible.
Capabilities and complete-empty daily CLI invocations emitted one valid envelope;
all four current/N-1 fixtures matched the encoder; output files are forced to mode
`0600`. The producer branch remains local/unpublished.
