#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
APP="$ROOT/dist/OpenUsage Bar.app"
PROFILE=${OPENUSAGE_NOTARY_PROFILE:-}
[[ -d "$APP" ]] || { print -u2 "build artifact unavailable"; exit 1; }
[[ -n "$PROFILE" ]] || {
  print -u2 "OPENUSAGE_NOTARY_PROFILE is required"
  exit 1
}

TEAM=$(codesign -dv --verbose=4 "$APP" 2>&1 | awk -F= '/^TeamIdentifier=/{print $2}')
[[ -n "$TEAM" && "$TEAM" != "not set" ]] || {
  print -u2 "a Developer ID signed build is required"
  exit 1
}

NOTARY_ZIP=$(mktemp "${TMPDIR:-/tmp}/OpenUsage-Bar-notary.XXXXXX.zip")
trap 'rm -f "$NOTARY_ZIP"' EXIT
/usr/bin/ditto -c -k --keepParent --sequesterRsrc "$APP" "$NOTARY_ZIP"
xcrun notarytool submit "$NOTARY_ZIP" --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=2 "$APP"
"$ROOT/scripts/package_release.sh"
print "notarization_ready team=$TEAM"
