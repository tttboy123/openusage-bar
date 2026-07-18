#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
SOURCE=${OPENUSAGE_SMOKE_APP:-"$ROOT/dist/OpenUsage Bar.app"}
[[ -d "$SOURCE" ]] || {
  print -u2 "release smoke requires a built app at $SOURCE"
  exit 1
}
/usr/bin/codesign --verify --deep --strict "$SOURCE"
SOURCE_VERSION=$(plutil -extract CFBundleShortVersionString raw "$SOURCE/Contents/Info.plist")

SMOKE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/openusage-release-smoke.XXXXXX")
cleanup_smoke() {
  local code=$?
  if (( code == 0 )) && [[ ${OPENUSAGE_KEEP_SMOKE_ROOT:-0} != 1 ]]; then
    rm -rf "$SMOKE_ROOT"
  else
    print -u2 "release smoke evidence retained at $SMOKE_ROOT"
  fi
}
trap cleanup_smoke EXIT
trap 'exit 130' INT TERM
export HOME="$SMOKE_ROOT/home"
export OPENUSAGE_INSTALL_DIR="$SMOKE_ROOT/Applications"
export OPENUSAGE_STATE_DIR="$HOME/.local/state/openusage-bar"
export OPENUSAGE_LABEL_SUFFIX="smoke-$$"
export OPENUSAGE_SOURCE_APP="$SOURCE"
export OPENUSAGE_REVEAL_IN_FINDER=0
mkdir -p "$HOME/Library/LaunchAgents" "$OPENUSAGE_INSTALL_DIR" "$OPENUSAGE_STATE_DIR"

SPY_BIN="$SMOKE_ROOT/bin"
SPY_STATE="$SMOKE_ROOT/launchctl-state"
SPY_LOG="$SMOKE_ROOT/launchctl.log"
KEYCHAIN_LOG="$SMOKE_ROOT/keychain.log"
mkdir -p "$SPY_BIN" "$SPY_STATE"
: > "$SPY_LOG"
: > "$KEYCHAIN_LOG"

if [[ ${OPENUSAGE_REAL_LAUNCH_AGENTS:-0} == 1 ]]; then
  export OPENUSAGE_LAUNCHCTL=/bin/launchctl
else
  cat > "$SPY_BIN/launchctl-spy" <<'SPY'
#!/bin/zsh
set -euo pipefail
print -r -- "$*" >> "$OPENUSAGE_SPY_LOG"
command=$1
shift
case "$command" in
  bootstrap)
    domain=$1
    plist=$2
    label=$(plutil -extract Label raw "$plist")
    : > "$OPENUSAGE_SPY_STATE/$label"
    ;;
  bootout)
    target=${1:t}
    rm -f "$OPENUSAGE_SPY_STATE/$target"
    ;;
  print)
    target=${1:t}
    [[ -f "$OPENUSAGE_SPY_STATE/$target" ]]
    ;;
  *) exit 2 ;;
esac
SPY
  chmod 755 "$SPY_BIN/launchctl-spy"
  export OPENUSAGE_SPY_STATE="$SPY_STATE"
  export OPENUSAGE_SPY_LOG="$SPY_LOG"
  export OPENUSAGE_LAUNCHCTL="$SPY_BIN/launchctl-spy"
fi

cat > "$SPY_BIN/health-spy" <<'HEALTH'
#!/bin/zsh
set -euo pipefail
[[ $1 == --timeout && $2 == 20 && -n $3 ]]
[[ ${OPENUSAGE_HEALTH_SPY_FAIL:-0} != 1 ]]
HEALTH
chmod 755 "$SPY_BIN/health-spy"
export OPENUSAGE_HEALTH_PROBE="$SPY_BIN/health-spy"

# A PATH spy proves rollback does not write or delete Keychain credentials.
cat > "$SPY_BIN/security" <<'KEYCHAIN'
#!/bin/zsh
print -r -- "$*" >> "$OPENUSAGE_KEYCHAIN_LOG"
exit 97
KEYCHAIN
chmod 755 "$SPY_BIN/security"
export OPENUSAGE_KEYCHAIN_LOG="$KEYCHAIN_LOG"
export PATH="$SPY_BIN:$PATH"

LEDGER="$OPENUSAGE_STATE_DIR/activity.sqlite3"
/usr/bin/sqlite3 "$LEDGER" <<'SQL'
CREATE TABLE daily_model_usage(day TEXT, total_tokens INTEGER);
INSERT INTO daily_model_usage VALUES('2026-07-18', 42);
CREATE TABLE quota_snapshots(snapshot_id INTEGER PRIMARY KEY, observed_at TEXT);
INSERT INTO quota_snapshots(observed_at) VALUES('2026-07-18T00:00:00Z');
CREATE TABLE change_log(change_seq INTEGER PRIMARY KEY);
INSERT INTO change_log VALUES(7);
SQL

ledger_facts() {
  /usr/bin/sqlite3 -readonly "$LEDGER" \
    "PRAGMA integrity_check; SELECT COUNT(*) FROM daily_model_usage; SELECT COUNT(*) FROM quota_snapshots; SELECT COALESCE(MAX(change_seq), 0) FROM change_log;" \
    | tr '\n' ':'
}

assert_ledger_unchanged() {
  local expected=$1
  local actual
  actual=$(ledger_facts)
  [[ "$actual" == "$expected" ]] || {
    print -u2 "ledger integrity or fact cursor changed during release smoke"
    exit 1
  }
}

EXPECTED_FACTS=$(ledger_facts)
[[ "$EXPECTED_FACTS" == ok:1:1:7: ]]

# Clean install and daemon restart through the isolated launchctl-spy.
"$ROOT/scripts/install_app.sh"
assert_ledger_unchanged "$EXPECTED_FACTS"
STATUS_LABEL="com.lune.openusagebar.$OPENUSAGE_LABEL_SUFFIX"
COLLECTOR_LABEL="com.lune.openusagebar.collector.$OPENUSAGE_LABEL_SUFFIX"
"$OPENUSAGE_LAUNCHCTL" bootout "gui/$(id -u)/$COLLECTOR_LABEL" || true
"$OPENUSAGE_LAUNCHCTL" bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$COLLECTOR_LABEL.plist"
"$OPENUSAGE_LAUNCHCTL" print "gui/$(id -u)/$COLLECTOR_LABEL"

# Uninstall preserves the ledger and all credential state by default.
"$ROOT/scripts/uninstall_app.sh"
[[ ! -d "$OPENUSAGE_INSTALL_DIR/OpenUsage Bar.app" ]]
assert_ledger_unchanged "$EXPECTED_FACTS"

# Build a signed 0.3.0 fixture and prove N-1 upgrade.
OLD_APP="$OPENUSAGE_INSTALL_DIR/OpenUsage Bar.app"
/usr/bin/ditto "$SOURCE" "$OLD_APP"
plutil -replace CFBundleShortVersionString -string 0.3.0 "$OLD_APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string 3 "$OLD_APP/Contents/Info.plist"
/usr/bin/codesign --force --deep --sign - "$OLD_APP"
"$ROOT/scripts/install_app.sh"
[[ $(plutil -extract CFBundleShortVersionString raw "$OLD_APP/Contents/Info.plist") == "$SOURCE_VERSION" ]]
assert_ledger_unchanged "$EXPECTED_FACTS"

# Explicit rollback validates the stored hash, identity, version, and signature.
ROLLBACK_BACKUP=""
for metadata in "$OPENUSAGE_STATE_DIR"/backups/app/*/metadata.plist(N); do
  if [[ $(plutil -extract version raw "$metadata") == 0.3.0 ]]; then
    ROLLBACK_BACKUP=$metadata:h
  fi
done
[[ -n "$ROLLBACK_BACKUP" ]]
"$ROOT/scripts/rollback_app.sh" "$ROLLBACK_BACKUP"
[[ $(plutil -extract CFBundleShortVersionString raw "$OLD_APP/Contents/Info.plist") == 0.3.0 ]]
assert_ledger_unchanged "$EXPECTED_FACTS"

# Restore current, then inject failures on both sides of the atomic swap.
"$ROOT/scripts/install_app.sh"
for stage in helper-copy launch-agent; do
  before=$(plutil -extract CFBundleVersion raw "$OLD_APP/Contents/Info.plist")
  if OPENUSAGE_TEST_FAIL_STAGE=$stage "$ROOT/scripts/install_app.sh"; then
    print -u2 "injected $stage failure unexpectedly succeeded"
    exit 1
  fi
  [[ $(plutil -extract CFBundleVersion raw "$OLD_APP/Contents/Info.plist") == "$before" ]]
  assert_ledger_unchanged "$EXPECTED_FACTS"
done
before=$(plutil -extract CFBundleVersion raw "$OLD_APP/Contents/Info.plist")
HEALTH_FAILURE_LOG="$SMOKE_ROOT/health-failure.log"
if OPENUSAGE_HEALTH_SPY_FAIL=1 "$ROOT/scripts/install_app.sh" >"$HEALTH_FAILURE_LOG" 2>&1; then
  print -u2 "injected health failure unexpectedly succeeded"
  exit 1
fi
cat "$HEALTH_FAILURE_LOG"
[[ $(grep -c "installation rolled back" "$HEALTH_FAILURE_LOG") == 1 ]]
! grep -q "atomic rollback unavailable" "$HEALTH_FAILURE_LOG"
[[ $(plutil -extract CFBundleVersion raw "$OLD_APP/Contents/Info.plist") == "$before" ]]
assert_ledger_unchanged "$EXPECTED_FACTS"

complete=("$OPENUSAGE_STATE_DIR"/backups/app/*/metadata.plist(N))
(( ${#complete} <= 2 ))
[[ ! -s "$KEYCHAIN_LOG" ]]

# Preserve once more, then explicitly purge all local application state.
"$ROOT/scripts/uninstall_app.sh"
assert_ledger_unchanged "$EXPECTED_FACTS"
"$ROOT/scripts/install_app.sh"
"$ROOT/scripts/uninstall_app.sh" --purge-data
[[ ! -e "$OPENUSAGE_STATE_DIR" ]]
[[ ! -s "$KEYCHAIN_LOG" ]]
print "release_smoke_ok clean_install=1 upgrade=1 rollback=1 failures=3 preserve=1 purge=1"
