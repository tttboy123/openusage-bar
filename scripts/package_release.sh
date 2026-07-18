#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
PYTHON="$ROOT/.build-venv/bin/python"
APP="$ROOT/dist/OpenUsage Bar.app"
INFO="$APP/Contents/Info.plist"
[[ -d "$APP" ]] || { print -u2 "build artifact unavailable"; exit 1; }
codesign --verify --deep --strict "$APP"

VERSION=$(plutil -extract CFBundleShortVersionString raw "$INFO")
ARCH=$(uname -m)
NAME="OpenUsage-Bar-v${VERSION}-macos-${ARCH}"
STAGE="$ROOT/build/release/$NAME"
ARCHIVE="$ROOT/dist/$NAME.zip"
CHECKSUM="$ARCHIVE.sha256"
DMG="$ROOT/dist/$NAME.dmg"
DMG_CHECKSUM="$DMG.sha256"
DMG_STAGE="$ROOT/build/release/dmg-$NAME"
MANIFEST="$ROOT/dist/OpenUsage-Bar-v${VERSION}-manifest.json"
SBOM="$ROOT/dist/OpenUsage-Bar-v${VERSION}-sbom.spdx.json"

rm -rf "$STAGE" "$DMG_STAGE" "$ARCHIVE" "$CHECKSUM" "$DMG" "$DMG_CHECKSUM" "$MANIFEST" "$SBOM"
mkdir -p "$STAGE/dist" "$STAGE/scripts"
/usr/bin/ditto "$APP" "$STAGE/dist/OpenUsage Bar.app"
cp \
  "$ROOT/scripts/install_app.sh" \
  "$ROOT/scripts/install_location.sh" \
  "$ROOT/scripts/install_app_transaction.sh" \
  "$ROOT/scripts/activity_install_process.sh" \
  "$ROOT/scripts/export_diagnostics.py" \
  "$ROOT/scripts/rollback_app.sh" \
  "$ROOT/scripts/uninstall_app.sh" \
  "$ROOT/scripts/privacy_scan.py" \
  "$ROOT/scripts/verify_local_api.py" \
  "$STAGE/scripts/"
cp \
  "$ROOT/LICENSE" \
  "$ROOT/THIRD_PARTY_NOTICES.md" \
  "$ROOT/docs/release-quick-start.md" \
  "$ROOT/docs/canary.md" \
  "$STAGE/"
chmod 755 "$STAGE/scripts/"*.sh "$STAGE/scripts/"*.py

(
  cd "$STAGE:h"
  /usr/bin/ditto -c -k --keepParent --norsrc "$NAME" "$ARCHIVE"
)
(
  cd "$ARCHIVE:h"
  shasum -a 256 "$ARCHIVE:t"
) > "$CHECKSUM"
(
  cd "$ARCHIVE:h"
  shasum -a 256 -c "$CHECKSUM:t"
)
mkdir -p "$DMG_STAGE"
/usr/bin/ditto "$APP" "$DMG_STAGE/OpenUsage Bar.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create \
  -volname "OpenUsage Bar" \
  -srcfolder "$DMG_STAGE" \
  -format UDZO \
  -ov \
  "$DMG"
hdiutil verify "$DMG"
(
  cd "$DMG:h"
  shasum -a 256 "$DMG:t"
) > "$DMG_CHECKSUM"
(
  cd "$DMG:h"
  shasum -a 256 -c "$DMG_CHECKSUM:t"
)
"$PYTHON" "$ROOT/scripts/generate_release_manifest.py" \
  --app "$APP" \
  --archive "$ARCHIVE" \
  --requirements "$ROOT/requirements-build.txt" \
  --swift-package "$ROOT/swift_app" \
  --output "$MANIFEST" \
  --sbom-output "$SBOM"
print "release_archive=$ARCHIVE"
print "release_checksum=$CHECKSUM"
print "release_dmg=$DMG"
print "release_dmg_checksum=$DMG_CHECKSUM"
print "release_manifest=$MANIFEST"
print "release_sbom=$SBOM"
