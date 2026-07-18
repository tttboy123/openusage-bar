#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
SOURCE=${OPENUSAGE_SOURCE_APP:-"$ROOT/dist/OpenUsage Bar.app"}
source "$ROOT/scripts/install_location.sh"
INSTALL_DIR=$(resolve_openusage_install_dir)
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
ACTIVITY_APP="$TARGET/Contents/Helpers/OpenUsage Activity.app"
ACTIVITY_EXECUTABLE="$TARGET/Contents/Helpers/OpenUsage Activity.app/Contents/MacOS/OpenUsage Activity"
NEW="$TARGET.new-$$"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STATE_DIR=${OPENUSAGE_STATE_DIR:-"$HOME/.local/state/openusage-bar"}
STATE_DIR=${STATE_DIR:A}
HOME_ROOT=${HOME:A}
[[ "$STATE_DIR" == "$HOME_ROOT"/* ]] || {
  print -u2 "OpenUsage state directory must be inside HOME"
  exit 2
}
BACKUP_ROOT="$STATE_DIR/backups/app"
TRANSACTION_ROOT="$STATE_DIR/install-transactions"
BACKUP="$TRANSACTION_ROOT/$STAMP-$$"
ATOMIC_SWAP="$SOURCE/Contents/Resources/atomic-swap"
AGENTS="$HOME/Library/LaunchAgents"
LABEL_SUFFIX=${OPENUSAGE_LABEL_SUFFIX:-}
[[ -z "$LABEL_SUFFIX" || "$LABEL_SUFFIX" =~ '^[A-Za-z0-9][A-Za-z0-9.-]*$' ]] || {
  print -u2 "invalid LaunchAgent label suffix"
  exit 2
}
LABEL_SUFFIX_PART=${LABEL_SUFFIX:+.$LABEL_SUFFIX}
OLD_LABEL="com.lune.openusage-menubar$LABEL_SUFFIX_PART"
STATUS_LABEL="com.lune.openusagebar$LABEL_SUFFIX_PART"
COLLECTOR_LABEL="com.lune.openusagebar.collector$LABEL_SUFFIX_PART"
STATUS_PLIST="$AGENTS/$STATUS_LABEL.plist"
COLLECTOR_PLIST="$AGENTS/$COLLECTOR_LABEL.plist"
OLD_PLIST="$AGENTS/$OLD_LABEL.plist"
SOCKET="$HOME/.local/state/openusage-bar/openusage.sock"
DOMAIN="gui/$(id -u)"
LAUNCHCTL=${OPENUSAGE_LAUNCHCTL:-/bin/launchctl}
HEALTH_PROBE=${OPENUSAGE_HEALTH_PROBE:-}
TEST_FAIL_STAGE=${OPENUSAGE_TEST_FAIL_STAGE:-}
MUTATED=0
HAD_TARGET=0
SWAPPED=0
FIRST_INSTALLED=0
ACTIVITY_WAS_RUNNING=0
ACTIVITY_STOPPED=0
ROLLBACK_ACTIVE=0

source "$ROOT/scripts/install_app_transaction.sh"
source "$ROOT/scripts/activity_install_process.sh"

bootstrap_agent() {
  local label=$1
  local plist=$2
  local attempt
  for attempt in {1..20}; do
    if "$LAUNCHCTL" print "$DOMAIN/$label" >/dev/null 2>&1; then
      return 0
    fi
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1 || true
    sleep 0.1
  done
  "$LAUNCHCTL" print "$DOMAIN/$label" >/dev/null 2>&1
}

wait_unloaded() {
  local label=$1
  local attempt
  for attempt in {1..50}; do
    if ! "$LAUNCHCTL" print "$DOMAIN/$label" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

wait_for_health() {
  verify_local_api_contract "$SOCKET" "$HEALTH_PROBE"
}

wait_for_socket_release() {
  local attempt
  for attempt in {1..100}; do
    if ! curl --fail --silent --unix-socket "$SOCKET" \
      http://localhost/v1/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

rollback() {
  local code=${1:-$?}
  if (( ROLLBACK_ACTIVE )); then
    return "$code"
  fi
  ROLLBACK_ACTIVE=1
  local bundle_restored=1
  local activity_runtime_cleared=1
  local activity_clear_rc=0
  # Ignore, rather than reset, the outer EXIT trap while rolling back. zsh can
  # restore a function-scoped trap during exit and otherwise invoke rollback a
  # second time after the staged bundle has already moved.
  trap '' EXIT INT TERM
  if (( MUTATED )); then
    "$LAUNCHCTL" bootout "$DOMAIN/$STATUS_LABEL" >/dev/null 2>&1 || true
    "$LAUNCHCTL" bootout "$DOMAIN/$COLLECTOR_LABEL" >/dev/null 2>&1 || true
    if (( SWAPPED || FIRST_INSTALLED || ACTIVITY_STOPPED )); then
      clear_activity_for_runtime_rollback "$ACTIVITY_EXECUTABLE" || activity_clear_rc=$?
      if (( ACTIVITY_STOP_SIGNALLED )); then
        ACTIVITY_STOPPED=1
      fi
      if (( activity_clear_rc != 0 )); then
        activity_runtime_cleared=0
        bundle_restored=0
      fi
    fi
    if (( activity_runtime_cleared )); then
      rollback_bundle_transaction "$ATOMIC_SWAP" "$TARGET" "$NEW" "$BACKUP/failed-new.app" || {
        bundle_restored=0
        print -u2 "atomic rollback unavailable; both app copies were retained"
      }
    fi
    if (( activity_runtime_cleared && bundle_restored )); then
      for label in "$STATUS_LABEL" "$COLLECTOR_LABEL" "$OLD_LABEL"; do
        local live="$AGENTS/$label.plist"
        if [[ -f "$BACKUP/$label.plist" ]]; then
          cp "$BACKUP/$label.plist" "$live.restore-$$"
          mv "$live.restore-$$" "$live"
        else
          rm -f "$live"
        fi
      done
      for saved in "$BACKUP"/legacy-*.plist(N); do
        local name=${${saved:t}#legacy-}
        cp "$saved" "$AGENTS/$name.restore-$$"
        mv "$AGENTS/$name.restore-$$" "$AGENTS/$name"
      done
    fi
    [[ -f "$OLD_PLIST" ]] && "$LAUNCHCTL" bootstrap "$DOMAIN" "$OLD_PLIST" >/dev/null 2>&1 || true
    [[ -f "$STATUS_PLIST" ]] && "$LAUNCHCTL" bootstrap "$DOMAIN" "$STATUS_PLIST" >/dev/null 2>&1 || true
    [[ -f "$COLLECTOR_PLIST" ]] && "$LAUNCHCTL" bootstrap "$DOMAIN" "$COLLECTOR_PLIST" >/dev/null 2>&1 || true
    if (( HAD_TARGET && ACTIVITY_STOPPED && activity_runtime_cleared && bundle_restored )); then
      reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE" || \
        print -u2 "restored Activity helper could not be reopened"
    fi
  fi
  print -u2 "installation rolled back; transaction evidence retained at $BACKUP"
  exit "$code"
}
trap rollback EXIT INT TERM

[[ -d "$SOURCE" ]] || { print -u2 "build artifact unavailable"; exit 1; }
[[ -x "$ATOMIC_SWAP" ]] || { print -u2 "atomic swap helper unavailable"; exit 1; }
validate_app_bundle "$SOURCE" || { print -u2 "build artifact failed validation"; exit 1; }
activity_has_exact_process "$ACTIVITY_EXECUTABLE" && ACTIVITY_WAS_RUNNING=1
mkdir -p "$BACKUP" "$BACKUP_ROOT" "$AGENTS" "$HOME/Library/Logs" "$INSTALL_DIR"
if [[ -d "$TARGET" ]]; then
  create_complete_app_backup "$TARGET" "$BACKUP_ROOT" "$STAMP" > "$BACKUP/app-backup-path"
  prune_complete_app_backups "$BACKUP_ROOT" 2
fi
for label in "$OLD_LABEL" "$STATUS_LABEL" "$COLLECTOR_LABEL"; do
  [[ -f "$AGENTS/$label.plist" ]] && cp "$AGENTS/$label.plist" "$BACKUP/$label.plist"
done
for legacy in "$AGENTS"/com.lune.openusage-menubar*.plist(N); do
  cp "$legacy" "$BACKUP/legacy-${legacy:t}"
done

rm -rf "$NEW"
/usr/bin/ditto "$SOURCE" "$NEW"
codesign --verify --deep --strict "$NEW"
if [[ "$TEST_FAIL_STAGE" == helper-copy ]]; then
  print -u2 "injected helper-copy failure"
  exit 1
fi
for role in status collector; do
  if [[ "$role" == status ]]; then
    template_label=com.lune.openusagebar
    label=$STATUS_LABEL
  else
    template_label=com.lune.openusagebar.collector
    label=$COLLECTOR_LABEL
  fi
  sed "s|__HOME__|$HOME|g" "$SOURCE/Contents/Resources/LaunchAgents/$template_label.plist" > "$BACKUP/$label.new.plist"
  /usr/libexec/PlistBuddy -c "Set :Label $label" "$BACKUP/$label.new.plist"
  template_program=$(plutil -extract ProgramArguments.0 raw "$BACKUP/$label.new.plist")
  resolved_program=${template_program/__APP__/$TARGET}
  [[ "$resolved_program" != "$template_program" ]] || exit 1
  /usr/libexec/PlistBuddy -c \
    "Set :ProgramArguments:0 $resolved_program" "$BACKUP/$label.new.plist"
  [[ $(plutil -extract ProgramArguments.0 raw "$BACKUP/$label.new.plist") == "$resolved_program" ]]
  [[ $(plutil -extract ProgramArguments.1 raw "$BACKUP/$label.new.plist") != __APP__/* ]]
  plutil -lint "$BACKUP/$label.new.plist" >/dev/null
done

MUTATED=1
"$LAUNCHCTL" bootout "$DOMAIN/$OLD_LABEL" >/dev/null 2>&1 || true
"$LAUNCHCTL" bootout "$DOMAIN/$STATUS_LABEL" >/dev/null 2>&1 || true
"$LAUNCHCTL" bootout "$DOMAIN/$COLLECTOR_LABEL" >/dev/null 2>&1 || true
wait_unloaded "$OLD_LABEL"
wait_unloaded "$STATUS_LABEL"
wait_unloaded "$COLLECTOR_LABEL"
wait_for_socket_release
if [[ -z "$LABEL_SUFFIX" ]]; then
  rm -f "$AGENTS"/com.lune.openusage-menubar*.plist(N)
fi
if (( ACTIVITY_WAS_RUNNING )); then
  stop_exact_activity_processes "$ACTIVITY_EXECUTABLE"
  if (( ACTIVITY_STOP_SIGNALLED )); then
    ACTIVITY_STOPPED=1
  fi
fi
install_bundle_transaction "$ATOMIC_SWAP" "$TARGET" "$NEW"
stop_exact_activity_processes "$ACTIVITY_EXECUTABLE"
if (( ACTIVITY_STOP_SIGNALLED )); then
  ACTIVITY_STOPPED=1
fi
mv "$BACKUP/$STATUS_LABEL.new.plist" "$STATUS_PLIST.tmp-$$"
mv "$STATUS_PLIST.tmp-$$" "$STATUS_PLIST"
mv "$BACKUP/$COLLECTOR_LABEL.new.plist" "$COLLECTOR_PLIST.tmp-$$"
mv "$COLLECTOR_PLIST.tmp-$$" "$COLLECTOR_PLIST"
if [[ "$TEST_FAIL_STAGE" == launch-agent ]]; then
  print -u2 "injected LaunchAgent failure"
  exit 1
fi
bootstrap_agent "$COLLECTOR_LABEL" "$COLLECTOR_PLIST"
bootstrap_agent "$STATUS_LABEL" "$STATUS_PLIST"

"$LAUNCHCTL" print "$DOMAIN/$COLLECTOR_LABEL" >/dev/null
"$LAUNCHCTL" print "$DOMAIN/$STATUS_LABEL" >/dev/null
if ! wait_for_health; then
  rollback 1
fi
codesign --verify --deep --strict "$TARGET"
if (( ACTIVITY_STOPPED )); then
  reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE"
fi

SWAPPED=0
FIRST_INSTALLED=0
HAD_TARGET=0
MUTATED=0
trap - EXIT INT TERM

commit_bundle_transaction "$NEW" || \
  print -u2 "installed successfully; previous app stage cleanup was skipped at $NEW"
cleanup_legacy_previous_bundles "$INSTALL_DIR" || \
  print -u2 "installed successfully; historical app cleanup was skipped"
prune_complete_app_backups "$BACKUP_ROOT" 2 || \
  print -u2 "installed successfully; backup pruning was skipped"
print "installed $TARGET"
print "rollback backups retained at $BACKUP_ROOT"
reveal_openusage_install "$TARGET" || \
  print -u2 "installed successfully; Finder could not reveal $TARGET"
