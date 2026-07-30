#!/bin/zsh
set -euo pipefail

[[ $# == 1 ]] || { print -u2 "release_dmg_invalid"; exit 2; }
DMG=${1:A}
[[ "$DMG:t" =~ '^OpenUsage-Bar-v([0-9]+\.[0-9]+\.[0-9]+)-macos-arm64\.dmg$' ]] || {
  print -u2 "release_dmg_invalid"
  exit 1
}
VERSION=$match[1]
[[ -f "$DMG" && ! -L "$DMG" && -f "$DMG.sha256" ]] || {
  print -u2 "release_dmg_invalid"
  exit 1
}

hdiutil verify "$DMG" >/dev/null
(
  cd "$DMG:h"
  shasum -a 256 -c "$DMG:t.sha256" >/dev/null
)

MOUNT=$(mktemp -d "${TMPDIR:-/tmp}/openusage-dmg-audit.XXXXXX")
MOUNTED=0
cleanup() {
  if (( MOUNTED )); then hdiutil detach "$MOUNT" >/dev/null; fi
  rmdir "$MOUNT" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT" "$DMG" >/dev/null
MOUNTED=1

APP="$MOUNT/OpenUsage Bar.app"
AGENT="$APP/Contents/Library/LaunchAgents/com.lune.openusagebar.collector.plist"
STATUS_LAUNCHER="$APP/Contents/MacOS/OpenUsage Bar"
STATUS_RUNTIME="$APP/Contents/MacOS/OpenUsage Bar.runtime"
COLLECTOR_LAUNCHER="$APP/Contents/MacOS/OpenUsage Collector"
COLLECTOR_RUNTIME="$APP/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
GUIDE="$MOUNT/安装说明 Installation Guide.txt"
[[ -d "$APP" && -L "$MOUNT/Applications" ]]
[[ $(readlink "$MOUNT/Applications") == /Applications ]]
[[ -f "$GUIDE" && ! -L "$GUIDE" ]]
grep -F 'xattr -dr com.apple.quarantine "/Applications/OpenUsage Bar.app"' "$GUIDE" >/dev/null
grep -F '不要全局关闭 Gatekeeper' "$GUIDE" >/dev/null
codesign --verify --deep --strict "$APP"
[[ $(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist") == "$VERSION" ]]
[[ $(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist") == com.lune.openusagebar ]]
[[ -f "$AGENT" ]]
plutil -lint "$AGENT" >/dev/null
[[ $(plutil -extract Label raw "$AGENT") == com.lune.openusagebar.collector ]]
[[ $(plutil -extract BundleProgram raw "$AGENT") == \
  'Contents/MacOS/OpenUsage Collector' ]]
for executable in "$STATUS_LAUNCHER" "$STATUS_RUNTIME" "$COLLECTOR_LAUNCHER" "$COLLECTOR_RUNTIME"; do
  [[ -x "$executable" && ! -L "$executable" ]]
  otool -L "$executable" >/dev/null
  codesign --display "$executable" >/dev/null 2>&1
  codesign --verify --strict "$executable"
done

print "release_dmg_ok version=$VERSION"
