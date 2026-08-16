# Open-source audit

Audit date: 2026-08-16 · Branch: `feat/usagehub-release-prep` · Candidate: 0.8.6 RC

This file records the open-source readiness review of the repository and the
fixes applied. It is a snapshot, not a standing policy; rerun the listed gates
before each public release.

## Scope

The audit covered four areas:

1. Secrets and sensitive material in the working tree and Git history.
2. Accidental inclusion of real user data or author-private paths.
3. Open-source compliance files (license, notices, security, contributing,
   code of conduct, ignore rules).
4. Dependency and supply-chain health (Python and npm).

## Gates executed (all passing)

| Gate | Command / tool | Result |
| --- | --- | --- |
| Repository secret scan | `scripts/release_secret_scan.py` | pass (`scope=tree`) |
| Git-history secret scan | `git log --all -p` pattern scan (`sk-`, `ghp_`, `AKIA`, private keys) | no hits |
| Python dependency audit | `zsh scripts/audit_dependencies.sh` (hash-pinned build + linux locks) | no known vulnerabilities |
| npm audit | `npm audit --omit=dev` in `web/` and `desktop/` | 0 vulnerabilities |
| Action pinning | `scripts/verify_action_pins.py` | pass |
| Release metadata | `scripts/verify_release_metadata.py` / `scripts/verify_product_version_truth.py` | pass |

## Findings and fixes

1. **Author-private absolute paths leaked in docs** (HIGH)
   - `docs/gateway-capability-ux.md` (4 visualization paths),
     `docs/gateway-provider-protocol-research.md` (Task 7 code links),
     `docs/gui-cross-platform.md` (screenshot dir + demo command) contained
     `/Users/lune/...` paths.
   - Fixed: replaced with repo-relative or neutral `<local>`/`<repo-root>`
     references. Verified no `/Users/lune` remains in tracked files.
2. **Author username in test fixtures** (LOW)
   - `tests/test_gateway_pii.py` and `tests/test_provider_config.py` used
     `/Users/lune/...` as synthetic input; replaced with `/Users/example/...`.
     The 25 affected tests re-ran green.
3. **Missing `CODE_OF_CONDUCT.md`** (LOW)
   - Added Contributor Covenant 2.1.
4. **Stale repository description** (LOW)
   - Updated the GitHub repo description to reflect the UsageHub desktop
     client + native build.
5. **No other findings**
   - All `account_ref`/balance/usage fixtures are synthetic (`fixture-a`,
     `fixture-b`, `/Users/tester`); no real ledger, Keychain, credentials,
     prompts, responses, or account identity is committed.
   - Lockfiles (`requirements-*.txt` with hashes, `package-lock.json`) are
     committed for reproducible builds.
   - Workflows use only `${{ github.token }}` and CI-generated random
     passwords; no hardcoded credentials.

## Notes

- `scripts/audit_dependencies.sh` is a zsh script (`${0:A}` expansion); run it
  with `zsh`, not `bash`.
- The external release gates (hosted native runners, signing/notarization,
  protected release environment, 0/5 canary) are outside what a local
  open-source audit can verify; see
  [release-quick-start.md](release-quick-start.md) for the readiness preflight.
