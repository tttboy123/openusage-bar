## Summary

## Verification

- [ ] `scripts/release_secret_scan.py --history`
- [ ] `scripts/build_app.sh`
- [ ] No credentials, private provider payloads, prompts, responses, or direct account identity added
- [ ] Unknown quota remains unavailable rather than zero

## Provider and privacy impact

Describe any new credential scope, endpoint, subprocess, ledger field, or exported API fact. Write `None` when this change has no provider or privacy impact.

## Local API compatibility impact

Classify the change as `None`, `additive`, `deprecated`, or `breaking` using
`docs/api/compatibility-v1.md`. For any Local API change, include current
schema validation and frozen N-1 client evidence. Breaking changes must use a
new API major version rather than changing v1 in place.
