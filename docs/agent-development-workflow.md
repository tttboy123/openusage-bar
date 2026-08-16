# Agent Development Workflow

OpenUsage Bar development uses four rotating roles coordinated by the primary
agent. Codex supports three child threads at once, so roles share the available
slots while preserving one writer per file.

## Role ownership

| Role | Owns | Must not own |
| --- | --- | --- |
| Server | Python Collector/Gateway, Local/Gateway API contracts, SQLite stores, credentials, platform services | Swift, renderer UI, visual styling |
| Client | Swift client core, Electron main-process integration, Web API/data layer | Provider credentials, API policy, page styling |
| UI | Web/Swift views, components, tokens, copy, localization, accessibility | Python APIs, bearer-token handling, credential persistence |
| Test | Tests, fixtures, snapshots, audits, CI and release evidence | Production behavior or weaker acceptance criteria |

The primary agent assigns exact files before every task. If a requested file is
already modified by another role or by the user, the new role stops and returns
the overlap instead of reverting or silently merging it.

## Iteration protocol

1. **Test Agent — contract call**
   - Convert the accepted behavior into the smallest failing deterministic
     test.
   - Return `expected_red`, the exact command, and the observed failure.
2. **Server Agent — capability call**
   - Implement the frozen schema and server behavior without expanding the
     client surface.
   - Return `contract_delta`, privacy impact, and sanitized client inputs.
3. **Client Agent — integration call**
   - Consume the committed schema through typed adapters.
   - Keep credentials and bearer tokens outside renderers.
   - Return the platform matrix and the user states the UI must represent.
4. **UI Agent — experience call**
   - Apply the shared state vocabulary and existing design system.
   - Verify keyboard, accessibility, localization, reduced motion, empty,
     loading, unknown, disabled, attention, and error states.
5. **Test Agent — acceptance call**
   - Run focused tests, then affected subsystem gates, then release/privacy
     gates in proportion to risk.
   - Return one `acceptance_report` with a go/no-go result.
6. **Primary Agent — integration checkpoint**
   - Review every handoff, check file ownership, run combined verification,
     update the implementation plan, and only then open the next slice.

## Role call envelopes

Every iteration uses the same bounded calls so a role cannot silently change
another role's contract:

| Call | Owner | Required input | Required output |
| --- | --- | --- | --- |
| `contract_call` | Test | `slice_id`, accepted behavior, owned test files | deterministic RED command and observed failure |
| `capability_call` | Server | frozen server contract and owned Python/schema files | `contract_delta`, privacy impact, sanitized client inputs |
| `integration_call` | Client | frozen schema/fixtures and owned Swift/Electron/Web-data files | platform matrix, renderer-safe shape, UI states needed |
| `experience_call` | UI | normalized state model and owned view/style/i18n files | state matrix, accessibility/localization/viewport checks |
| `performance_call` | Test | frozen fixture/schema, source revision, reference-machine class | verified privacy-safe report, provenance class, per-gate result, and release/non-release label |
| `acceptance_call` | Test | combined changed-file inventory and verification ladder | independent `acceptance_report` with P0/P1 and go/no-go |

The primary agent supplies one `slice_id` and exact file ownership to every
call, rejects handoffs without reproducible commands, and sends findings back
to the owning role instead of patching across role boundaries. A green focused
test is not acceptance until the Test role reruns affected subsystem and
privacy/release gates. Browser and native-platform evidence must name the
actual environment used; static source contracts cannot be relabelled as
interactive or native evidence.

For Gateway performance work, the Test role owns the schema, fixture, report
verification, and CI evidence wording. The authoritative exact-cache gate is
core lookup p99 < 5 ms; authenticated loopback cache-hit latency is
informational. The dispatch-only manual CI job runs, verifies, and uploads the
performance report, while absolute budgets remain non-blocking on its shared
runner. A performance report is release evidence only when it comes from a
clean source tree on an idle reference machine.

## Shared product contracts

- Modes are `observe`, `advise`, and `gateway`; a fresh install or upgrade
  starts in `observe`.
- User-facing semantic states are `ok`, `attention`, `error`, `disabled`, and
  `unknown`. Unknown is not zero and disabled is not an error.
- Local API `/v1/*` remains GET/HEAD-only. Stateful Gateway requests use the
  separate `/gateway/v1/*` listener and token.
- `/v1/schema.json` is the exact current-server contract. Every advertised
  Local API route must validate with both representative seeded data and its
  empty response; routes sharing top-level keys must use one closed envelope so
  empty arrays cannot create root `oneOf` ambiguity.
- The Collector is the only durable fact-ledger writer. Gateway telemetry and
  cache stay in separate bounded stores.
- Python and operating-system credential stores own Provider credentials.
  Electron and Web renderers never receive Provider credentials or bearer
  tokens.
- Swift remains the native macOS observation client. Electron/Web is the shared
  desktop client for macOS, Windows, and Linux.
- One injected Observer platform resolver owns catalog support truth, Local API
  counts/reason codes, and adapter construction gates. A known verified zero is
  not an unknown count; renderers must preserve `0` versus `null` without
  exposing raw reason codes.
- Adapter-to-catalog source dependencies are explicit contracts. A shared
  adapter is enabled only when every catalog use is verified on that platform;
  portable evidence for one source ID must not enable unmodeled legacy sources.
- Windows/Linux source support may change only after native evidence covers the
  executable, local-file discovery, credential backend, and fact parser seams.
  A cross-platform shell or a successful package build is not source evidence.
- Source evidence reports every required seam independently. A failed or
  skipped parser may never be labeled verified because credential or file
  discovery failed; source status and verified counts must be derived from the
  closed seam state rather than caller-supplied booleans.
- `observer-source-native-evidence/v1` is verified offline before embedding in
  `native-ci-evidence-v1`. Its probe accepts no platform/home/status override,
  cleans only synthetic credentials and device/inode-owned files, and fails
  closed on existing, symlinked, crossed, non-canonical, or cleanup state. It
  remains embedded evidence, not a sixth release-handoff file.
- Should-Send prediction may use burn rate only when remaining quota and burn
  rate have compatible units. A remaining ratio is not a Token balance.

### Product and version truth

- `openusage_bar/resources/product-version-truth.v1.json` is the machine source
  for product identity. Its Draft 2020-12 schema lives at
  `docs/schemas/product-version-truth-v1.schema.json`; every role runs
  `scripts/verify_product_version_truth.py` after changing a bound surface.
- Candidate identity and published identity are independent facts. The current
  candidate is UsageHub 0.8.6 build 28 RC; the published stable baseline remains
  v0.7.1. A candidate may never use the `stable` channel.
- Product versions are not API versions. Local API `1.0`, Gateway
  `gateway.openusage/v1`, and runtime capability
  `runtime-capability.openusage/v1` remain separate namespaces.
- The legacy native macOS release track retains `OpenUsage Bar.app` and
  `OpenUsage-Bar-v...` assets. Cross-platform UsageHub artifacts remain local/CI
  handoff evidence until their independent native, signing, and canary gates
  pass. Roles must not merge the two release tracks by renaming artifacts.
- Client projections expose only the closed renderer-safe identity shape.
  Build Identity UI must show candidate version/build/channel, publication
  state, published baseline, and Canary progress as text; it must not imply
  release eligibility or use a color-only status.
- Candidate publication is a closed three-row state machine:
  `candidate/not_published/false`,
  `prerelease_ready/not_published/true`, and
  `prerelease_published/published_prerelease/false`. The published row also
  requires a verified closed receipt; every crossed or partial combination
  fails closed to `unknown` in renderer presentation.
- `artifact-build-identity/v1` is the canonical renderer-safe build identity.
  Web, Swift, and Electron packaging consume the same canonical bytes. Artifact
  audit must bind those bytes to source `product-version-truth/v1` and native
  package metadata before native evidence or release handoff may pass.
- Native CI evidence carries the packaged identity as `buildIdentity` and must
  pass both `productVersionTruth` and `packagedBuildIdentity` checks. The local
  release handoff remains an exact five-file bundle; identity is embedded in
  evidence and manifest, not uploaded as a sixth handoff file.
- The tag release workflow calls
  `verify_product_version_truth.py --require-release-eligible` before
  attestation or publication. The current candidate intentionally fails this
  gate with one fixed error until its independent release criteria are met.
- A published pre-release requires an online-derived
  `github-release-receipt/v1` bound to repository, tag, source SHA, manifest
  digest, exact asset set digest, and verified provenance attestation. Renderer
  projections must never expose that receipt.
- `scripts/release_readiness.py` is a path-free release inventory, not a
  release authority. The public CLI does not accept self-reported external
  evidence. It must not produce an authoritative `ready` state until each native
  runner, signing/notarization, performance, accessibility, and canary claim is
  bound to its real verifier or protected workflow evidence.
- Release build/audit stays read-only. Write, OIDC, and attestation permissions
  exist only in the protected publish job, which reverifies the downloaded
  six-asset set and release barrier before its first external side effect.
  If the current run creates a pre-release but cannot generate and preserve the
  public receipt, the workflow must delete that receiptless pre-release without
  deleting the source git tag. Same-tag runs are serialized; remote probing and
  creation are separate steps, and only a successful creation-step outcome
  grants automatic cleanup ownership. Pre-existing or race-ambiguous releases
  are retained for the explicit recovery runbook.

## Verification ladder

1. Focused red/green unit or contract test.
2. Local API/Gateway schema and N-1 compatibility tests.
3. Privacy, credential, diagnostics, and renderer-isolation tests.
4. Python full suite on macOS, Windows, and Linux.
5. Web build/tests, Electron main-process/package tests, and Swift tests.
6. Accessibility, localization, viewport, screenshot, and keyboard checks.
7. Gateway performance evidence, when in scope: nearest-rank p99, worst-of-3,
   paired signed proxy delta, separately reported late completions, and clean
   idle reference-machine provenance.
8. Artifact, dependency, version metadata, upgrade/rollback, native evidence,
   signing/notarization, and canary gates.

The Test role must use event-driven handshakes for process and native-service
tests whenever setup depends on scheduler progress. A fixed one- or two-second
assumption is not an acceptance signal under a loaded full-suite run. Flaky
failures are promoted into a repeatable concurrent feedback loop, then fixed at
the observation seam without widening production timeouts or weakening cleanup.

No role may weaken validation, copy secrets into a fixture, or mark a platform
verified without native evidence.

Browser acceptance covers 1440, 768, 375, and 320 CSS-pixel viewports for Web
changes. It records console/network failures, keyboard focus, landmarks, body
overflow, and local Core Web Vitals. Without a committed screenshot baseline,
visual regression remains `INCONCLUSIVE`; automated landmark checks are only
partial accessibility evidence and never replace a screen-reader pass.

## Remaining Release Ownership

Before any stable claim, the primary agent must keep four ownership tracks
explicit:

| Track | Owner | Remaining evidence |
| --- | --- | --- |
| Native evidence | Test + Client | macOS, Windows, and Linux build/install/upgrade/rollback/uninstall/offline smoke artifacts from real runners. |
| UI parity | UI + Client | Runtime capability, Gateway disabled/unknown/error states, localization, accessibility, keyboard, viewport, and renderer privacy checks. |
| Signing and release | Test + Server | Dependency audit, SBOM, checksums, provenance/attestation, macOS signing/notarization, Windows/Linux packaging audits. |
| Canary | Primary + Test | Existing Apple Silicon observation cohort remains separate from future opt-in Gateway and Windows/Linux cohorts; no cohort reduction is allowed for stable. |
