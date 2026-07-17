#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
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

rm -rf "$STAGE" "$ARCHIVE" "$CHECKSUM"
mkdir -p "$STAGE/dist" "$STAGE/scripts"
/usr/bin/ditto "$APP" "$STAGE/dist/OpenUsage Bar.app"
cp \
  "$ROOT/scripts/install_app.sh" \
  "$ROOT/scripts/install_app_transaction.sh" \
  "$ROOT/scripts/activity_install_process.sh" \
  "$ROOT/scripts/uninstall_app.sh" \
  "$STAGE/scripts/"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$ROOT/docs/release-quick-start.md" "$STAGE/"
chmod 755 "$STAGE/scripts/"*.sh

(
  cd "$STAGE:h"
  /usr/bin/ditto -c -k --keepParent --sequesterRsrc "$NAME" "$ARCHIVE"
)
(
  cd "$ARCHIVE:h"
  shasum -a 256 "$ARCHIVE:t"
) > "$CHECKSUM"
(
  cd "$ARCHIVE:h"
  shasum -a 256 -c "$CHECKSUM:t"
)
print "release_archive=$ARCHIVE"
print "release_checksum=$CHECKSUM"
