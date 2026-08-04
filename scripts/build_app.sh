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
STATUS_RUNTIME="$APP/Contents/MacOS/OpenUsage Bar.runtime"
COLLECTOR_LAUNCHER="$APP/Contents/MacOS/OpenUsage Collector"
RESOURCES="$SWIFT_PACKAGE/Resources"
ATOMIC_SWAP="$APP/Contents/Resources/atomic-swap"
INTEGRATIONS="$APP/Contents/Resources/Integrations"
SWIFT_MIN_LINE_COVERAGE=80
PYTHON_MIN_LINE_COVERAGE=80
# Declarative SwiftUI composition is exercised by native hosting smoke tests.
# Keep the line gate focused on deterministic product logic; compiler-generated
# view coverage changes across macOS/Xcode runner images.
SWIFT_COVERAGE_IGNORE='Tests|/Sources/(OpenUsageBar|OpenUsageActivity)/(main|[^/]*Views)\.swift'
CODESIGN_IDENTITY=${OPENUSAGE_CODESIGN_IDENTITY:--}

[[ -x "$PYTHON" ]] || { print -u2 "local build environment unavailable"; exit 1; }

cd "$ROOT"
"$PYTHON" scripts/release_secret_scan.py
"$PYTHON" scripts/verify_action_pins.py
(
  cd "$ROOT/integrations/cliproxyapi-openusage"
  GOTOOLCHAIN=local go test -race ./...
  GOTOOLCHAIN=local go vet ./...
)
CATALOG_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-provider-catalog.XXXXXX")
LOCAL_API_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-local-api-schema.XXXXXX")
ROUTING_API_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-routing-api-schema.XXXXXX")
ROUTING_SHADOW_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-routing-shadow-schema.XXXXXX")
ROUTING_REPLAY_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-routing-replay-schema.XXXXXX")
ACTIVITY_SCHEMA_TMP=$(mktemp "${TMPDIR:-/tmp}/openusage-activity-schema.XXXXXX")
PYTHON_COVERAGE_REPORT=$(mktemp "${TMPDIR:-/tmp}/openusage-python-coverage.XXXXXX")
PYTHON_COVERAGE_DIR="${TMPDIR:-/tmp}/openusage-build-trace-$$"
trap 'rm -f "$CATALOG_TMP" "$LOCAL_API_SCHEMA_TMP" "$ROUTING_API_SCHEMA_TMP" "$ROUTING_SHADOW_SCHEMA_TMP" "$ROUTING_REPLAY_SCHEMA_TMP" "$ACTIVITY_SCHEMA_TMP" "$PYTHON_COVERAGE_REPORT"; rm -rf "$PYTHON_COVERAGE_DIR"' EXIT
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
"$PYTHON" scripts/generate_routing_api_schema.py --output "$ROUTING_API_SCHEMA_TMP"
if ! cmp -s "$ROUTING_API_SCHEMA_TMP" "$ROOT/openusage_bar/resources/routing-api-v1.schema.json"; then
  print -u2 "generated routing API schema is stale"
  diff -u "$ROOT/openusage_bar/resources/routing-api-v1.schema.json" "$ROUTING_API_SCHEMA_TMP" || true
  exit 1
fi
"$PYTHON" scripts/generate_routing_api_schema.py --kind shadow --output "$ROUTING_SHADOW_SCHEMA_TMP"
if ! cmp -s "$ROUTING_SHADOW_SCHEMA_TMP" "$ROOT/openusage_bar/resources/routing-shadow-v1.schema.json"; then
  print -u2 "generated routing Shadow schema is stale"
  diff -u "$ROOT/openusage_bar/resources/routing-shadow-v1.schema.json" "$ROUTING_SHADOW_SCHEMA_TMP" || true
  exit 1
fi
"$PYTHON" scripts/generate_routing_api_schema.py --kind replay --output "$ROUTING_REPLAY_SCHEMA_TMP"
if ! cmp -s "$ROUTING_REPLAY_SCHEMA_TMP" "$ROOT/openusage_bar/resources/routing-replay-v1.schema.json"; then
  print -u2 "generated routing Replay schema is stale"
  diff -u "$ROOT/openusage_bar/resources/routing-replay-v1.schema.json" "$ROUTING_REPLAY_SCHEMA_TMP" || true
  exit 1
fi
"$PYTHON" scripts/generate_swift_activity_schema.py --output "$ACTIVITY_SCHEMA_TMP"
if ! cmp -s "$ACTIVITY_SCHEMA_TMP" "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedActivitySchema.swift"; then
  print -u2 "generated Swift activity schema is stale"
  diff -u "$SWIFT_PACKAGE/Sources/UsageCore/GeneratedActivitySchema.swift" "$ACTIVITY_SCHEMA_TMP" || true
  exit 1
fi
PYTHON_BASE=$("$PYTHON" -c 'import sys; print(sys.base_prefix)')
"$PYTHON" -m unittest tests.test_provider_conformance -v
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m trace --count --summary --missing \
  --coverdir "$PYTHON_COVERAGE_DIR" \
  --ignore-dir "$PYTHON_BASE:$ROOT/.build-venv" \
  --module unittest discover -s tests -v 2>&1 | tee "$PYTHON_COVERAGE_REPORT"
"$PYTHON" scripts/python_coverage_gate.py \
  --report "$PYTHON_COVERAGE_REPORT" \
  --minimum "$PYTHON_MIN_LINE_COVERAGE" \
  --package-root "$ROOT/openusage_bar"
"$PYTHON" scripts/python_coverage_gate.py \
  --report "$PYTHON_COVERAGE_REPORT" \
  --minimum "$PYTHON_MIN_LINE_COVERAGE" \
  --package-root "$ROOT/integrations"
"$PYTHON" scripts/privacy_scan.py \
  "$ROOT/openusage_bar/resources/release-state.v1.json" \
  "$ROOT/openusage_bar/resources/provider-catalog.v1.json" \
  "$ROOT/openusage_bar/resources/local-api-v1.schema.json" \
  "$ROOT/openusage_bar/resources/routing-api-v1.schema.json" \
  "$ROOT/openusage_bar/resources/routing-shadow-v1.schema.json" \
  "$ROOT/openusage_bar/resources/routing-replay-v1.schema.json" \
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
  -ignore-filename-regex="$SWIFT_COVERAGE_IGNORE")
SWIFT_LINE_COVERAGE=$(print -r -- "$SWIFT_COVERAGE_REPORT" | awk '/^TOTAL/ {gsub("%", "", $10); print $10}')
[[ -n "$SWIFT_LINE_COVERAGE" ]] || { print -u2 "Swift coverage total unavailable"; exit 1; }
if ! awk -v actual="$SWIFT_LINE_COVERAGE" -v minimum="$SWIFT_MIN_LINE_COVERAGE" \
  'BEGIN { exit !(actual + 0 >= minimum + 0) }'; then
  print -u2 "Swift product line coverage below ${SWIFT_MIN_LINE_COVERAGE}% (actual=${SWIFT_LINE_COVERAGE}%)"
  exit 1
fi
print "swift_product_line_coverage=${SWIFT_LINE_COVERAGE}%"
swift package --package-path "$SWIFT_PACKAGE" show-dependencies --format json
swift build --package-path "$SWIFT_PACKAGE" -c release --product OpenUsageBar -Xswiftc -warnings-as-errors
swift build --package-path "$SWIFT_PACKAGE" -c release --product OpenUsageActivity -Xswiftc -warnings-as-errors

if [[ -d "$INTEGRATIONS" && ! -L "$INTEGRATIONS" ]]; then
  chmod u+w "$INTEGRATIONS"
fi
rm -rf "$BUILD_ROOT" "$DIST"
mkdir -p \
  "$APP/Contents/MacOS" \
  "$APP/Contents/Helpers" \
  "$INTEGRATIONS" \
  "$APP/Contents/Resources/LaunchAgents" \
  "$APP/Contents/Library/LaunchAgents"
/usr/bin/clang -Wall -Wextra -Werror -mmacosx-version-min=15.0 \
  "$ROOT/scripts/atomic_swap.c" -o "$ATOMIC_SWAP"
chmod 755 "$ATOMIC_SWAP"
cp "$RESOURCES/OpenUsageBar-Info.plist" "$APP/Contents/Info.plist"
cp "$RESOURCES/OpenUsageBar.icns" "$APP/Contents/Resources/OpenUsageBar.icns"
cp "$SWIFT_PACKAGE/.build/release/OpenUsageBar" "$STATUS_RUNTIME"
/usr/bin/clang -Wall -Wextra -Werror -mmacosx-version-min=15.0 \
  "$ROOT/scripts/clean_env_launcher.c" -o "$APP/Contents/MacOS/OpenUsage Bar"
cp "$APP/Contents/MacOS/OpenUsage Bar" "$COLLECTOR_LAUNCHER"
chmod 755 "$APP/Contents/MacOS/OpenUsage Bar" "$STATUS_RUNTIME" "$COLLECTOR_LAUNCHER"
cp "$ROOT/integrations/litellm_openusage.py" "$INTEGRATIONS/litellm_openusage.py"
cp "$ROOT/integrations/otel_genai_openusage.py" "$INTEGRATIONS/otel_genai_openusage.py"
chmod 644 \
  "$INTEGRATIONS/litellm_openusage.py" \
  "$INTEGRATIONS/otel_genai_openusage.py"
chmod 555 "$INTEGRATIONS"

mkdir -p "$ACTIVITY_APP/Contents/MacOS"
cp "$RESOURCES/OpenUsageActivity-Info.plist" "$ACTIVITY_APP/Contents/Info.plist"
mkdir -p "$ACTIVITY_APP/Contents/Resources"
cp "$RESOURCES/OpenUsageBar.icns" "$ACTIVITY_APP/Contents/Resources/OpenUsageBar.icns"
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
mkdir -p "$SETTINGS_APP/Contents/Resources"
cp "$RESOURCES/OpenUsageBar.icns" "$SETTINGS_APP/Contents/Resources/OpenUsageBar.icns"
if [[ ! -x "$SETTINGS_APP/Contents/MacOS/OpenUsage Provider Settings" ]]; then
  PY_EXEC=$(find "$SETTINGS_APP/Contents/MacOS" -maxdepth 1 -type f -perm +111 -print -quit)
  [[ -n "$PY_EXEC" ]] || { print -u2 "settings helper executable unavailable"; exit 1; }
  mv "$PY_EXEC" "$SETTINGS_APP/Contents/MacOS/OpenUsage Provider Settings"
fi
/usr/libexec/PlistBuddy -c "Delete :PythonInfoDict:PythonExecutable" \
  "$SETTINGS_APP/Contents/Info.plist"
# py2app copies Python development headers even though this app never compiles
# extensions at runtime. Hosted Python's pyconfig.h can contain its build-home
# prefix, so remove the unused development-only tree before signing/package audit.
rm -rf "$SETTINGS_APP/Contents/Resources/include"
find "$SETTINGS_APP/Contents/Resources/lib" -type d \
  -name 'config-*darwin*' -prune -exec rm -rf {} +
# Package-manager metadata is not used by the frozen helper. Some hosted
# Python distributions record their absolute installation prefix in METADATA.
find "$SETTINGS_APP/Contents/Resources" -type d \
  \( -name '*.dist-info' -o -name '*.egg-info' \) -prune -exec rm -rf {} +
# A frozen production helper does not ship test packages or loose test modules.
find "$SETTINGS_APP/Contents/Resources" -type d \
  \( -name test -o -name tests \) -prune -exec rm -rf {} +
find "$SETTINGS_APP/Contents/Resources" -type f \
  \( -name 'test_*.py' -o -name '*_test.py' \) -delete

cp "$RESOURCES/com.lune.openusagebar.plist" "$APP/Contents/Resources/LaunchAgents/"
cp "$RESOURCES/com.lune.openusagebar.collector.plist" "$APP/Contents/Resources/LaunchAgents/"
cp "$RESOURCES/com.lune.openusagebar.collector.sm.plist" \
  "$APP/Contents/Library/LaunchAgents/com.lune.openusagebar.collector.plist"
find "$APP" -type f -name Makefile -delete

"$PYTHON" scripts/privacy_scan.py \
  "$APP/Contents/Info.plist" \
  "$ACTIVITY_APP/Contents/Info.plist" \
  "$SETTINGS_APP/Contents/Info.plist" \
  "$APP/Contents/Resources/LaunchAgents" \
  "$APP/Contents/Library/LaunchAgents"

plutil -lint "$APP/Contents/Info.plist" "$ACTIVITY_APP/Contents/Info.plist" "$SETTINGS_APP/Contents/Info.plist"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$SETTINGS_APP"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$ACTIVITY_APP"
codesign --force --sign "$CODESIGN_IDENTITY" "$ATOMIC_SWAP"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP"
codesign --verify --deep --strict "$APP"

[[ $(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist") == com.lune.openusagebar ]]
[[ $(plutil -extract CFBundleIconFile raw "$APP/Contents/Info.plist") == OpenUsageBar ]]
[[ -f "$APP/Contents/Resources/OpenUsageBar.icns" ]]
[[ $(plutil -extract CFBundleIdentifier raw "$ACTIVITY_APP/Contents/Info.plist") == com.lune.openusagebar.activity ]]
[[ $(plutil -extract CFBundleIdentifier raw "$SETTINGS_APP/Contents/Info.plist") == com.lune.openusagebar.settings ]]
[[ $(plutil -extract CFBundleIconFile raw "$ACTIVITY_APP/Contents/Info.plist") == OpenUsageBar ]]
[[ $(plutil -extract CFBundleIconFile raw "$SETTINGS_APP/Contents/Info.plist") == OpenUsageBar ]]
[[ -f "$ACTIVITY_APP/Contents/Resources/OpenUsageBar.icns" ]]
[[ -f "$SETTINGS_APP/Contents/Resources/OpenUsageBar.icns" ]]
[[ $(plutil -extract LSUIElement raw "$APP/Contents/Info.plist") == true ]]
! plutil -extract LSUIElement raw "$ACTIVITY_APP/Contents/Info.plist" >/dev/null 2>&1
! plutil -extract LSUIElement raw "$SETTINGS_APP/Contents/Info.plist" >/dev/null 2>&1
otool -L "$APP/Contents/MacOS/OpenUsage Bar" >/dev/null
otool -L "$STATUS_RUNTIME" >/dev/null
otool -L "$COLLECTOR_LAUNCHER" >/dev/null
otool -L "$ACTIVITY_APP/Contents/MacOS/OpenUsage Activity" >/dev/null
"$PYTHON" scripts/runtime_observation_smoke.py \
  --collector "$COLLECTOR_LAUNCHER" \
  --fixture "$ROOT/tests/fixtures/runtime-observation-v1.json"
"$PYTHON" scripts/runtime_producer_smoke.py \
  --collector "$COLLECTOR_LAUNCHER" \
  --integration "$INTEGRATIONS/litellm_openusage.py" \
  --fixture "$ROOT/tests/fixtures/runtime-producers/litellm-success-v1.json"
"$PYTHON" scripts/runtime_adapter_smoke.py \
  --collector "$COLLECTOR_LAUNCHER" \
  --integration "$INTEGRATIONS/otel_genai_openusage.py" \
  --fixture "$ROOT/tests/fixtures/runtime-producers/otel-genai-f77b923-success-v1.json"
codesign --verify --deep --strict "$APP"
print "built $APP"
