# OpenUsage Infrastructure Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze OpenUsage Bar's fact-plane boundary, remove macOS from the reusable core contract, and establish one machine-readable release state without changing ledger data semantics.

**Architecture:** Durable resource facts remain in the existing Python-owned ledger and read-only Local API. Core Provider metadata becomes OS-neutral, while the shipped macOS catalog is checked by a separate distribution invariant. A strict JSON release-state resource becomes the source checked against bundle metadata and public documentation.

**Tech Stack:** Python 3 dataclasses and standard-library JSON/plist parsing, existing unittest suite, Markdown ADRs, existing SwiftUI bundle metadata.

---

### Task 1: Freeze Fact, Telemetry, Reservation and Policy ownership

**Files:**
- Create: `docs/adr/0001-infrastructure-boundary.md`
- Modify: `docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md`
- Modify: `ROADMAP.md`

- [x] **Step 1: Record the accepted ownership table**

Write the ADR with these exclusive writers:

```text
Fact        -> Python Collector -> durable activity ledger
Telemetry   -> future bounded ingestor -> separate short-retention store
Reservation -> Loom/scheduler -> scheduler-owned store
Policy      -> Loom/scheduler -> scheduler-owned decision log
```

- [x] **Step 2: Record non-negotiable privacy and independence rules**

State that telemetry never stores prompts, responses, credentials or direct
identity; Loom remains optional; and the Local API remains UI-independent and
read-only.

- [x] **Step 3: Add the 0.7 queue without reopening completed 0.6 work**

Add four ordered items: boundary/release state, Card-first retirement,
`openusage-export/v1`, and optional Runtime Observation. Keep real-account and
external Canary gates open.

- [x] **Step 4: Verify and commit**

Run:

```bash
git diff --check
test -f docs/adr/0001-infrastructure-boundary.md
test -f docs/superpowers/plans/2026-08-01-openusage-infrastructure-boundary.md
```

Expected: both commands exit 0.

### Task 2: Make the Provider core contract OS-neutral

**Files:**
- Modify: `openusage_bar/capabilities.py`
- Modify: `openusage_bar/provider_catalog.py`
- Test: `tests/test_capabilities.py`
- Test: `tests/test_provider_catalog.py`

- [x] **Step 1: Write failing core and distribution tests**

Add a test proving this core value is valid:

```python
SourceCapability(
    "linux_log", SourceKind.LOCAL_LOG, 12, 300,
    CredentialType.LOCAL, frozenset({OperatingSystem.LINUX}),
    SourceStability.STABLE, SourceProvenance.PROVIDER_LOCAL,
)
```

Add catalog tests proving a Linux-only source parses successfully, while:

```python
catalog.require_operating_system("macos")
```

raises a sanitized `ValueError` for that distribution.

- [x] **Step 2: Run the tests to verify RED**

Run:

```bash
.build-venv/bin/python -m unittest \
  tests.test_capabilities tests.test_provider_catalog -v
```

Expected: the Linux-only core/catalog assertions fail under the current
implicit macOS invariant.

- [x] **Step 3: Move the invariant to the distribution boundary**

Remove `OperatingSystem.MACOS` enforcement from `SourceCapability.__post_init__`
and remove the matching rule from `_parse_source`. Add a strict
`ProviderCatalog.require_operating_system()` method that validates the enum
string and lists only public family/source IDs in its error. Call it once for
the shipped global catalog:

```python
catalog = load_provider_catalog()
catalog.require_operating_system("macos")
```

- [x] **Step 4: Run the tests to verify GREEN**

Run the Task 2 command again.

Expected: all capability and catalog tests pass, including unchanged checks
that the committed OpenUsage Bar catalog is macOS-capable.

### Task 3: Add one machine-readable release state

**Files:**
- Create: `openusage_bar/resources/release-state.v1.json`
- Modify: `scripts/verify_release_metadata.py`
- Modify: `tests/test_release_metadata.py`
- Modify: `ROADMAP.md`
- Modify: `scripts/build_app.sh`

- [x] **Step 1: Write failing strict-schema tests**

Extend each temporary release fixture with:

```json
{
  "schemaVersion": 1,
  "currentVersion": "0.4.0",
  "buildVersion": "4",
  "channel": "rc",
  "apiVersion": "1.0",
  "canary": {
    "qualifiedMachines": 0,
    "targetMachines": 5,
    "clock": "not_started"
  }
}
```

Add tests for mismatched version/build, unknown fields, invalid Canary counts,
and a stale ROADMAP version.

- [x] **Step 2: Run the release tests to verify RED**

Run:

```bash
.build-venv/bin/python -m unittest tests.test_release_metadata -v
```

Expected: the new release-state tests fail because the verifier does not yet
read the resource.

- [x] **Step 3: Implement strict validation**

Parse with the standard library, require the exact top-level and Canary key
sets, reject booleans as counts, require `qualifiedMachines <= targetMachines`,
and compare `currentVersion`/`buildVersion` with all three Info.plists and
`bundle_config.py`. Require ROADMAP markers for version, channel and Canary
state. Do not fetch GitHub or mutate a Release.

- [x] **Step 4: Add the committed 0.6.0 RC state**

Commit `currentVersion=0.6.0`, `buildVersion=9`, API `1.0`, 0/5 qualified Macs,
and `not_started`. Add the JSON resource to the build privacy scan.

- [x] **Step 5: Run the release tests to verify GREEN**

Run:

```bash
.build-venv/bin/python -m unittest \
  tests.test_release_metadata tests.test_build_script tests.test_bundle_config -v
.build-venv/bin/python scripts/verify_release_metadata.py
.build-venv/bin/python scripts/privacy_scan.py \
  openusage_bar/resources/release-state.v1.json
```

Expected: all tests pass, `release_metadata_ok version=0.6.0 build=9`, and the
privacy scan reports zero findings.

### Task 4: Verify the P0 slice without claiming external readiness

**Files:**
- Modify only files required by failures introduced by Tasks 1-3.

- [x] **Step 1: Run generated-contract and documentation checks**

```bash
.build-venv/bin/python scripts/generate_swift_provider_catalog.py --check
test -f docs/adr/0001-infrastructure-boundary.md
test -f docs/superpowers/plans/2026-08-01-openusage-infrastructure-boundary.md
git diff --check
```

- [x] **Step 2: Run the complete Python and Swift test suites**

```bash
.build-venv/bin/python -m unittest discover -s tests -v
swift test --package-path swift_app -Xswiftc -warnings-as-errors
```

- [x] **Step 3: Run the privacy and dependency gates**

```bash
scripts/audit_dependencies.sh
.build-venv/bin/python scripts/release_secret_scan.py
.build-venv/bin/python scripts/privacy_scan.py \
  openusage_bar/resources/release-state.v1.json \
  openusage_bar/resources/provider-catalog.v1.json
```

- [x] **Step 4: Review the final diff and commit**

Confirm no credential, raw Provider payload, Prompt/Response or direct account
identity is present. Commit the isolated slice as:

```bash
git add ROADMAP.md docs openusage_bar scripts tests
git commit -m "refactor: freeze infrastructure boundaries"
```

External real-account evidence, five-machine Canary qualification and its
30-day clock remain unchanged and must not be marked complete by this plan.
