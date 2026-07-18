#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
PYTHON="$ROOT/.build-venv/bin/python"
SWIFT_PACKAGE="$ROOT/swift_app"
BUILD_ROOT="$ROOT/build/task9"
DIST="$ROOT/dist"
APP="$DIST/OpenUsage Bar.app"
ACTIVITY_APP="$APP/Contents/Helpers/OpenUsage Activity.app"
SETTINGS_APP="$APP/Contents/Helpers/OpenUsage Provider Settings.app"
RESOURCES="$SWIFT_PACKAGE/Resources"
ATOMIC_SWAP="$APP/Contents/Resources/atomic-swap"
SWIFT_MIN_LINE_COVERAGE=80
PYTHON_MIN_LINE_COVERAGE=80
CODESIGN_IDENTITY=${OPENUSAGE_CODESIGN_IDENTITY:--}
PYTHON_TOUCHED_MODULES=(
  openusage_bar.activity_records
  openusage_bar.activity_schema
  openusage_bar.activity_store
  openusage_bar.aggregator
  openusage_bar.bounded_process
  openusage_bar.capabilities
  openusage_bar.codex_subscription
  openusage_bar.collector_cli
  openusage_bar.config
  openusage_bar.daily_feed
  openusage_bar.daily_history
  openusage_bar.generic
  openusage_bar.kiro
  openusage_bar.local_api
  openusage_bar.minimax
  openusage_bar.model_ids
  openusage_bar.models
  openusage_bar.openusage_adapter
  openusage_bar.openusage_catalog
  openusage_bar.openai_organization
  openusage_bar.provider_catalog
  openusage_bar.provider_commands
  openusage_bar.query
  openusage_bar.step_plan
)

[[ -x "$PYTHON" ]] || { print -u2 "local build environment unavailable"; exit 1; }

cd "$ROOT"
"$PYTHON" scripts/release_secret_scan.py
CATALOG_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-provider-catalog.XXXXXX")
LOCAL_API_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-local-api-schema.XXXXXX")
ACTIVITY_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-activity-schema.XXXXXX")
PYTHON_COVERAGE_REPORT=$(mktemp "${TMPDIR:-/tmp}/openusage-python-coverage.XXXXXX")
PYTHON_COVERAGE_DIR="${TMPDIR:-/tmp}/openusage-build-trace-$$"
trap 'rm -f "$CATALOG_TMP" "$LOCAL_API_SCHEMA_TMP" "$ACTIVITY_SCHEMA_TMP" "$PYTHON_COVERAGE_REPORT"; rm -rf "$PYTHON_COVERAGE_DIR"' EXIT
"$PYTHON" scripts/generate_swift_provider_catalog.py --output "$CATALOG_TMP"
if ! cmp -s "$CATALOG_TMP" "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedProviderCatalog.swift"; then
  print -u2 "generated Swift provider catalog is stale"
  diff -u "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedProviderCatalog.swift" "$CATALOG_TMP" || true
  exit 1
fi
"$PYTHON" scripts/generate_local_api_schema.py --output "$LOCAL_API_SCHEMA_TMP"
if ! cmp -s "$LOCAL_API_SCHEMA_TMP" "$ROOT/openusage_bar/resources/local-api-v1.schema.json"; then
  print -u2 "generated local API schema is stale"
  diff -u "$ROOT/openusage_bar/resources/local-api-v1.schema.json" "$LOCAL_API_SCHEMA_TMP" || true
  exit 1
fi
"$PYTHON" scripts/generate_swift_activity_schema.py --output "$ACTIVITY_SCHEMA_TMP"
if ! cmp -s "$ACTIVITY_SCHEMA_TMP" "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedActivitySchema.swift"; then
  print -u2 "generated Swift activity schema is stale"
  diff -u "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedActivitySchema.swift" "$ACTIVITY_SCHEMA_TMP" || true
  exit 1
fi
PYTHON_BASE=$("$PYTHON" -c 'import sys; print(sys.base_prefix)')
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m trace --count --summary --missing \
  --coverdir "$PYTHON_COVERAGE_DIR" \
  --ignore-dir "$PYTHON_BASE:$ROOT/.build-venv" \
  --module unittest discover -s tests -v 2>&1 | tee "$PYTHON_COVERAGE_REPORT"
"$PYTHON" scripts/python_coverage_gate.py \
  --report "$PYTHON_COVERAGE_REPORT" \
  --minimum "$PYTHON_MIN_LINE_COVERAGE" \
  "${PYTHON_TOUCHED_MODULES[@]}"
"$PYTHON" scripts/privacy_scan.py \
  "$ROOT/openusage_bar/resources/provider-catalog.v1.json" \
  "$ROOT/openusage_bar/resources/local-api-v1.schema.json" \
  "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedProviderCatalog.swift" \
  "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedActivitySchema.swift"
swift test --package-path "$SWIFT_PACKAGE" --enable-code-coverage -Xswiftc -warnings-as-errors
SWIFT_PROFILE="$SWIFT_PACKAGE/.build/debug/codecov/default.profdata"
SWIFT_TEST_BINARY=$(find "$SWIFT_PACKAGE/.build" -type f \
  -path '*/OpenUsageBarPackageTests.xctest/Contents/MacOS/OpenUsageBarPackageTests' \
  -print -quit)
[[ -n "$SWIFT_TEST_BINARY" && -f "$SWIFT_PROFILE" ]] || {
  print -u2 "Swift coverage artifacts unavailable"
  exit 1
}
SWIFT_COVERAGE_REPORT=$(xcrun llvm-cov report "$SWIFT_TEST_BINARY" \
  -instr-profile="$SWIFT_PROFILE" \
  -ignore-filename-regex='Tests|/Sources/(OpenUsageBar|OpenUsageActivity)/main.swift')
SWIFT_LINE_COVERAGE=$(print -r -- "$SWIFT_COVERAGE_REPORT" | awk '/^TOTAL/ {gsub("%", "", $10); print $10}')
[[ -n "$SWIFT_LINE_COVERAGE" ]] || { print -u2 "Swift coverage total unavailable"; exit 1; }
if ! awk -v actual="$SWIFT_LINE_COVERAGE" -v minimum="$SWIFT_MIN_LINE_COVERAGE" \
  'BEGIN { exit !(actual + 0 >= minimum + 0) }'; then
  print -u2 "Swift product line coverage below ${SWIFT_MIN_LINE_COVERAGE}%"
  exit 1
fi
print "swift_product_line_coverage=${SWIFT_LINE_COVERAGE}%"
swift package --package-path "$SWIFT_PACKAGE" show-dependencies --format json
swift build --package-path "$SWIFT_PACKAGE" -c release --product OpenUsageBar -Xswiftc -warnings-as-errors
swift build --package-path "$SWIFT_PACKAGE" -c release --product OpenUsageActivity -Xswiftc -warnings-as-errors

rm -rf "$BUILD_ROOT" "$DIST"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Helpers" "$APP/Contents/Resources/LaunchAgents"
/usr/bin/clang -Wall -Wextra -Werror -mmacosx-version-min=15.0 \
  "$ROOT/scripts/atomic_swap.c" -o "$ATOMIC_SWAP"
chmod 755 "$ATOMIC_SWAP"
cp "$RESOURCES/OpenUsageBar-Info.plist" "$APP/Contents/Info.plist"
cp "$SWIFT_PACKAGE/.build/release/OpenUsageBar" "$APP/Contents/MacOS/OpenUsage Bar"
chmod 755 "$APP/Contents/MacOS/OpenUsage Bar"

mkdir -p "$ACTIVITY_APP/Contents/MacOS"
cp "$RESOURCES/OpenUsageActivity-Info.plist" "$ACTIVITY_APP/Contents/Info.plist"
cp "$SWIFT_PACKAGE/.build/release/OpenUsageActivity" "$ACTIVITY_APP/Contents/MacOS/OpenUsage Activity"
chmod 755 "$ACTIVITY_APP/Contents/MacOS/OpenUsage Activity"
for LANGUAGE in en zh-Hans; do
  mkdir -p \
    "$APP/Contents/Resources/$LANGUAGE.lproj" \
    "$ACTIVITY_APP/Contents/Resources/$LANGUAGE.lproj"
  cp "$RESOURCES/$LANGUAGE.lproj/Localizable.strings" \
    "$APP/Contents/Resources/$LANGUAGE.lproj/Localizable.strings"
  cp "$RESOURCES/$LANGUAGE.lproj/Localizable.strings" \
    "$ACTIVITY_APP/Contents/Resources/$LANGUAGE.lproj/Localizable.strings"
done

mkdir -p "$BUILD_ROOT/python-dist" "$BUILD_ROOT/python-build"
"$PYTHON" setup.py py2app --dist-dir "$BUILD_ROOT/python-dist" --bdist-base "$BUILD_ROOT/python-build"
PY_APP=$(find "$BUILD_ROOT/python-dist" -maxdepth 1 -type d -name '*.app' -print -quit)
[[ -n "$PY_APP" ]] || { print -u2 "settings helper build unavailable"; exit 1; }
/usr/bin/ditto "$PY_APP" "$SETTINGS_APP"
if [[ ! -x "$SETTINGS_APP/Contents/MacOS/OpenUsage Provider Settings" ]]; then
  PY_EXEC=$(find "$SETTINGS_APP/Contents/MacOS" -maxdepth 1 -type f -perm +111 -print -quit)
  [[ -n "$PY_EXEC" ]] || { print -u2 "settings helper executable unavailable"; exit 1; }
  mv "$PY_EXEC" "$SETTINGS_APP/Contents/MacOS/OpenUsage Provider Settings"
fi

cp "$RESOURCES/com.lune.openusagebar.plist" "$APP/Contents/Resources/LaunchAgents/"
cp "$RESOURCES/com.lune.openusagebar.collector.plist" "$APP/Contents/Resources/LaunchAgents/"

"$PYTHON" scripts/privacy_scan.py \
  "$APP/Contents/Info.plist" \
  "$ACTIVITY_APP/Contents/Info.plist" \
  "$SETTINGS_APP/Contents/Info.plist" \
  "$APP/Contents/Resources/LaunchAgents"

plutil -lint "$APP/Contents/Info.plist" "$ACTIVITY_APP/Contents/Info.plist" "$SETTINGS_APP/Contents/Info.plist"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$SETTINGS_APP"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$ACTIVITY_APP"
codesign --force --sign "$CODESIGN_IDENTITY" "$ATOMIC_SWAP"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP"
codesign --verify --deep --strict "$APP"

[[ $(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist") == com.lune.openusagebar ]]
[[ $(plutil -extract CFBundleIdentifier raw "$ACTIVITY_APP/Contents/Info.plist") == com.lune.openusagebar.activity ]]
[[ $(plutil -extract CFBundleIdentifier raw "$SETTINGS_APP/Contents/Info.plist") == com.lune.openusagebar.settings ]]
[[ $(plutil -extract LSUIElement raw "$APP/Contents/Info.plist") == true ]]
! plutil -extract LSUIElement raw "$ACTIVITY_APP/Contents/Info.plist" >/dev/null 2>&1
! plutil -extract LSUIElement raw "$SETTINGS_APP/Contents/Info.plist" >/dev/null 2>&1
otool -L "$APP/Contents/MacOS/OpenUsage Bar" >/dev/null
otool -L "$ACTIVITY_APP/Contents/MacOS/OpenUsage Activity" >/dev/null
print "built $APP"
