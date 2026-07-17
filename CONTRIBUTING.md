# Contributing to OpenUsage Bar

OpenUsage Bar is a macOS-first SwiftUI application with a Python collection
layer. Keep credentials and network mutation in Python adapters; SwiftUI reads
only sanitized local facts.

## Development requirements

- Apple Silicon Mac
- macOS 15 or later
- Xcode with Swift 6.2 or later
- Python 3.11 or later
- Git

Prepare a clean checkout and run the full gate:

```bash
scripts/bootstrap.sh
scripts/build_app.sh
```

The build runs Python and Swift tests, coverage gates, privacy scans, the
repository secret scan, Release compilation, packaging, and code-signature
verification. Do not bypass a failing gate.

## Pull requests

1. Keep changes focused and add a regression test.
2. Do not add a dependency when the Python standard library or native Apple API
   already provides the capability.
3. Never commit real keys, cookies, tokens, provider payloads, prompts,
   responses, paths containing account identity, or copied Keychain data.
4. Run `scripts/release_secret_scan.py --history` before opening a pull request.
5. Run `scripts/build_app.sh` and include the exact result in the pull request.

Provider adapters must preserve last-good data on failure and represent unknown
quota as unavailable, never as zero.
