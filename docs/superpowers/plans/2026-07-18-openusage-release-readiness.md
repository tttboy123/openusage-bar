# OpenUsage Bar Release and 1.0 Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current source-ready pre-release into a repeatable, auditable, source-first open-source release with safe installation, upgrade, rollback, and external canary evidence.

**Architecture:** CI produces ad-hoc signed convenience artifacts from immutable tags, with dependency audit, manifest, SBOM, checksums, and artifact inspection. Install/upgrade tests run in isolated homes and preserve the ledger/Keychain boundary; no automatic telemetry or Developer ID requirement is introduced.

**Tech Stack:** zsh, Python standard library, pip-audit in an isolated audit environment, GitHub Actions, GitHub artifact attestations, macOS codesign, SQLite integrity checks.

---

**Codex skill resolution:** In this environment, `superpowers:executing-plans` maps to the installed `executing-plans` skill. Use the subagent-driven option only when `subagent-driven-development` is actually available.

### Task 1: Make release provenance immutable and self-consistent

**Files:**
- Create: `scripts/verify_release_metadata.py`
- Create: `tests/test_release_metadata.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `docs/release-quick-start.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write failing metadata tests**

Build synthetic repositories/configs and cover mismatched app/helper versions, duplicated build numbers, missing CHANGELOG entry, a tag not reachable from main, a moved tag record, and a valid docs-only commit after a tag.

- [ ] **Step 2: Implement the verifier**

The script compares:

- all three `CFBundleShortVersionString` and `CFBundleVersion` values;
- `openusage_bar/bundle_config.py` version/build declarations;
- release tag when present;
- CHANGELOG release heading;
- tag reachability from main.

It permits docs-only commits after a tag and never moves a tag. A failed release is retried under a new patch/build version.

- [ ] **Step 3: Pin Actions to immutable commits**

Resolve each official action tag to its full commit through GitHub's API, dereference annotated tags when the returned object type is `tag`, and verify a 40-character hexadecimal commit SHA. Then write each resolved SHA as a literal value in the committed YAML `uses:` line; shell variables are not valid immutable workflow references. Retain the human version only as a comment, for example:

```yaml
- uses: actions/checkout@0123456789abcdef0123456789abcdef01234567 # v5
```

The hexadecimal value above illustrates the required 40-character shape; replace it with the commit actually resolved and verified at implementation time. Start resolution with:

```bash
gh api repos/actions/checkout/git/ref/tags/v5
gh api repos/actions/setup-python/git/ref/tags/v6
gh api repos/actions/upload-artifact/git/ref/tags/v4
```

For a returned `{"object":{"type":"tag","sha":"TAG_SHA"}}`, obtain the commit with `gh api repos/OWNER/REPOSITORY/git/tags/TAG_SHA --jq .object.sha`; for type `commit`, use the ref SHA directly. Record the resolved full commit in the workflow diff and verify it belongs to the official repository before commit.

- [ ] **Step 4: Protect release refs in GitHub settings**

Create a repository ruleset requiring CI for `main`, blocking force-push/delete, and blocking force-update/delete for `v*` tags. This is an external repository-setting action and requires the repository owner's explicit approval at execution time.

- [ ] **Step 5: Verify and commit local changes**

```bash
.build-venv/bin/python -m unittest tests.test_release_metadata -v
.build-venv/bin/python scripts/verify_release_metadata.py
git add scripts/verify_release_metadata.py tests/test_release_metadata.py \
  .github/workflows docs/release-quick-start.md CHANGELOG.md
git commit -m "build: verify immutable release provenance"
```

### Task 2: Add dependency, coverage, and artifact audits

**Files:**
- Create: `.github/dependabot.yml`
- Create: `requirements-audit.txt`
- Create: `scripts/audit_dependencies.sh`
- Create: `scripts/release_artifact_audit.py`
- Create: `tests/test_release_artifact_audit.py`
- Modify: `scripts/build_app.sh`
- Modify: `tests/test_python_coverage_gate.py`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Remove the hand-maintained Python coverage allowlist**

Add a test that creates a new `openusage_bar/example_module.py` in a temporary repository view and proves the coverage gate discovers it automatically. Enumerate product modules from `openusage_bar/*.py`, excluding only `__init__.py` and generated resources through an explicit checked list.

- [ ] **Step 2: Add hashed dependency locks and a pinned audit environment**

Add hashes to every requirement in `requirements-build.txt`. Resolve the latest non-yanked `pip-audit` release during implementation, pin it with hashes in `requirements-audit.txt`, and install it into a separate temporary virtual environment. `scripts/audit_dependencies.sh` audits the locked build requirements and fails on any known unfixed vulnerability; a documented ignore requires an issue URL and expiry date. Network/tool failure is a distinct CI failure, not a clean result.

- [ ] **Step 3: Add Dependabot**

Configure weekly updates for `pip` and `github-actions`, with a limit of five open pull requests per ecosystem. Do not enable automatic merge.

- [ ] **Step 4: Audit packaged ZIP contents**

`release_artifact_audit.py` must reject:

- absolute or `..` archive paths;
- symlinks escaping the app bundle;
- ledger, provider config, Keychain dump, logs, `.env`, or home paths;
- unexpected executable architectures;
- inconsistent bundle versions;
- unsigned nested executables;
- files outside the documented bundle and checksum set.

- [ ] **Step 5: Verify and commit**

```bash
.build-venv/bin/python -m unittest \
  tests.test_python_coverage_gate tests.test_release_artifact_audit -v
scripts/audit_dependencies.sh
scripts/build_app.sh
scripts/package_release.sh
.build-venv/bin/python scripts/release_artifact_audit.py dist/OpenUsage-Bar-*.zip
git add .github/dependabot.yml requirements-audit.txt scripts \
  tests/test_python_coverage_gate.py tests/test_release_artifact_audit.py \
  .github/workflows/ci.yml
git commit -m "build: audit dependencies and release artifacts"
```

### Task 3: Generate release manifest, SBOM, and provenance

**Files:**
- Create: `scripts/generate_release_manifest.py`
- Create: `tests/test_release_manifest.py`
- Modify: `scripts/package_release.sh`
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Define the deterministic manifest test**

For a synthetic app bundle, assert the manifest contains schema version, Git commit, product version/build, macOS minimum, architecture, Python locked dependencies, Swift dependency graph, and SHA-256 for every executable and published asset. Assert file ordering and JSON bytes are deterministic.

- [ ] **Step 2: Generate an SPDX-compatible SBOM**

Use committed Python requirements, `swift package show-dependencies --format json`, and app bundle inventory. Do not inspect or include user runtime state. Package:

```text
OpenUsage-Bar-v${VERSION}-macos-arm64.zip
OpenUsage-Bar-v${VERSION}-macos-arm64.zip.sha256
OpenUsage-Bar-v${VERSION}-manifest.json
OpenUsage-Bar-v${VERSION}-sbom.spdx.json
```

- [ ] **Step 3: Add GitHub build provenance**

Grant only the workflow permissions required by the official GitHub attestation action and attest the ZIP, checksum, manifest, and SBOM. Keep the release marked pre-release until the 1.0 gate passes. Attestation is independent of Developer ID signing.

- [ ] **Step 4: Verify and commit**

```bash
.build-venv/bin/python -m unittest tests.test_release_manifest -v
scripts/package_release.sh
shasum -a 256 -c dist/OpenUsage-Bar-*.zip.sha256
git add scripts/generate_release_manifest.py scripts/package_release.sh \
  tests/test_release_manifest.py .github/workflows/release.yml
git commit -m "build: publish release manifest and provenance"
```

### Task 4: Prove install, upgrade, rollback, and uninstall

**Files:**
- Create: `scripts/release_smoke.sh`
- Create: `scripts/rollback_app.sh`
- Create: `tests/test_release_smoke.py`
- Modify: `scripts/install_app.sh`
- Modify: `scripts/install_app_transaction.sh`
- Modify: `scripts/verify_local_api.py`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add isolated-home smoke scenarios**

Use a temporary `HOME`, `OPENUSAGE_INSTALL_DIR`, and unique smoke-test LaunchAgent label suffix. Cover clean install, v0.3.0-to-current upgrade, injected helper-copy failure, injected LaunchAgent failure, rollback, uninstall preserving data, explicit purge, and daemon restart. Local runs default to a launchctl spy; only a disposable CI runner or an explicitly authorized local run may bootstrap real smoke-test LaunchAgents.

- [ ] **Step 2: Define the data-preservation assertions**

Before and after upgrade/rollback, record:

```sql
PRAGMA integrity_check;
SELECT COUNT(*) FROM daily_model_usage;
SELECT COUNT(*) FROM quota_snapshots;
SELECT COALESCE(MAX(change_seq), 0) FROM change_log;
```

Require `integrity_check=ok`, no decrease in fact/history counts, and no decrease in change cursor. Keychain commands are replaced with a spy that must receive no delete/write during rollback.

- [ ] **Step 3: Make install health verification contractual**

After LaunchAgent activation, wait at most 20 seconds for `/v1/health`, `/v1/schema`, and `/v1/summary`. A failure triggers atomic application rollback while leaving the prior ledger and configuration untouched.

- [ ] **Step 4: Keep two user-state backups**

Store application backups under the documented user state directory, never beside the downloaded ZIP. Retain exactly the two newest complete versions. `rollback_app.sh` validates hash, bundle identity, version, and signature before swap.

- [ ] **Step 5: Verify and commit**

```bash
.build-venv/bin/python -m unittest tests.test_release_smoke -v
scripts/release_smoke.sh
git add scripts/release_smoke.sh scripts/rollback_app.sh \
  scripts/install_app.sh scripts/install_app_transaction.sh \
  scripts/verify_local_api.py tests/test_release_smoke.py \
  .github/workflows/ci.yml
git commit -m "test: prove app upgrade and rollback"
```

### Task 5: Run an opt-in external canary and enforce 1.0 gates

**Files:**
- Create: `docs/canary.md`
- Create: `.github/ISSUE_TEMPLATE/canary_report.yml`
- Create: `scripts/export_diagnostics.py`
- Create: `tests/test_export_diagnostics.py`
- Modify: `SECURITY.md`
- Modify: `docs/release-quick-start.md`

- [ ] **Step 1: Build a redacted diagnostics exporter**

Export product/build version, schema version, source-state/error codes, capability declarations, aggregate counts, and revision. Reject provider configuration, account labels, tokens, cookies, raw payload/change JSON, prompts, responses, absolute home paths, and direct account identity.

- [ ] **Step 2: Publish the manual canary protocol**

No telemetry is added. Testers explicitly submit a GitHub canary form and may attach the redacted diagnostics file. Cover at least five external Apple Silicon Macs across macOS 15 and the current macOS release, both `/Applications` and `~/Applications`, empty setup, local-client-only setup, and multi-Provider setup.

- [ ] **Step 3: Run for 30 consecutive days**

For each machine record install, first fact, refresh, restart, upgrade, rollback drill, Provider configuration class, and any Unknown/stale incidents. Never request credentials or raw provider responses.

- [ ] **Step 4: Apply the 1.0 release gate**

All must pass:

- zero data-loss, credential-leak, Unknown-to-zero, or upgrade-blocking incidents for 30 days;
- at least five machines and five distinct Provider configurations complete install/refresh/restart/upgrade;
- every release uses immutable tag, checksum, manifest, SBOM, and attestation;
- zero known High/Critical dependency vulnerabilities;
- Python and Swift line coverage remain at least 80%;
- N-1 upgrade and automated rollback pass continuously;
- Local API v1 compatibility suite passes;
- source-first and ad-hoc signed distribution remains accurately disclosed.

- [ ] **Step 5: Verify and commit the canary tooling**

```bash
.build-venv/bin/python -m unittest tests.test_export_diagnostics -v
.build-venv/bin/python scripts/export_diagnostics.py --output /tmp/openusage-diagnostics.json
.build-venv/bin/python scripts/privacy_scan.py /tmp/openusage-diagnostics.json
git add docs/canary.md .github/ISSUE_TEMPLATE/canary_report.yml \
  scripts/export_diagnostics.py tests/test_export_diagnostics.py \
  SECURITY.md docs/release-quick-start.md
git commit -m "docs: define the OpenUsage Bar 1.0 canary"
```

## Acceptance gate

- Release source, tag, metadata, artifact, checksum, manifest, and SBOM agree.
- Dependency and artifact audits are mandatory CI gates.
- Install/upgrade/rollback tests preserve ledger integrity and Keychain isolation.
- No automatic telemetry is introduced.
- Developer ID signing and notarization remain optional distribution choices, not 1.0 requirements.
- 1.0 is published only after the 30-day external evidence gate passes.
