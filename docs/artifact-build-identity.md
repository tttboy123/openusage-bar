# Artifact build identity

`openusage_bar/resources/artifact-build-identity.v1.json` is the canonical,
renderer-safe projection of
[`product-version-truth/v1`](product-version-truth.md). It carries only the 13
fields needed to identify and present a build. Publication receipts, legacy
product names, repository data, paths, credentials, and environment state are
intentionally excluded.

The source file and its Web and Swift resource copies are canonical ASCII JSON
and must be byte-identical. Packaging places the same bytes at the fixed name
`product-build-identity.v1.json`:

- Electron: the application resources root;
- Web: `/product-build-identity.v1.json` in the built renderer;
- Swift: the application resources root.

Consumers may format localized copy from this identity, but they must not infer
publication or release eligibility from a package version, filename, tag, or
UI label. The valid lifecycle combinations are the same closed three-state
union as the product truth source.

Run the repository consistency gate with:

```bash
python scripts/verify_artifact_build_identity.py
```

For an extracted or mounted package, `release_artifact_audit.py` performs a
three-way binding among the packaged bytes, the canonical source, and product
truth. It also validates native metadata:

- macOS: `UsageHub`, version `0.8.7`, build `29` in `Info.plist`;
- Windows: `UsageHub`, product version `0.8.7.29`, and a file version whose
  normalized build is `28` in PE `VersionInfo`;
- Linux: version/build fields in `app.asar`; a final AppImage additionally has
  exactly one desktop entry with `Name=UsageHub` and
  `X-AppImage-Version=28`.

Native CI evidence embeds the full identity together with the canonical file
name, byte size, and SHA-256 digest. The release handoff copies that binding
inline, so its exact-five-file transport contract does not need a sixth
identity file and remains independently verifiable offline.

This contract identifies bytes; it does not sign, notarize, attest, publish,
start a canary, or grant release eligibility.
