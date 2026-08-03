# Public product assets

Everything in this directory is safe for the public repository. Product
screenshots must not contain credentials, account identifiers, real quotas, or
machine-specific paths.

## Screenshots

- `openusage-bar-activity-demo-zh.png` is rendered from an isolated synthetic
  activity ledger.
- `openusage-bar-menu-demo.png` is rendered by the native SwiftUI hosting test
  from a synthetic menu-bar ledger. Regenerate it with:

  ```bash
  OPENUSAGE_SCREENSHOT_DIR="$PWD/docs/assets" \
    swift test --package-path swift_app \
      --filter VisibilityStoreTests.nativeMenuRendering
  ```

- `openusage-bar-provider-catalog-demo-zh.png` contains only the static
  **Add Provider** catalog. It must be recaptured from that isolated sheet, not
  from the account list behind it.

## App icon

`brand/openusage-bar-icon.png` is the 1024 px transparent source. The selected
AI-assisted concept uses a capacity gauge, three provider nodes, and a central
activity pulse; it contains no text or vendor marks. Its generation prompt was:

> Create a polished macOS utility icon for a local-first AI usage and quota
> control center. Combine a restrained capacity gauge and pulse with three
> converging provider nodes. Use graphite, electric blue, cyan, and a subtle
> mint status accent. Keep it recognizable at 16 px. No text, vendor marks,
> robot, brain, sparkle, or watermark.

The public source was converted from a flat chroma-key background to transparent
RGBA locally. Rebuild the checked-in bundle icon with:

```bash
scripts/generate_app_icon.sh
```
