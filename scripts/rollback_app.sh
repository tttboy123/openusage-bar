#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
source "$ROOT/scripts/install_location.sh"
INSTALL_DIR=$(resolve_openusage_install_dir)
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
ACTIVITY_APP="$TARGET/Contents/Helpers/OpenUsage Activity.app"
ACTIVITY_EXECUTABLE="$TARGET/Contents/Helpers/OpenUsage Activity.app/Contents/MacOS/OpenUsage Activity"
SETTINGS_APP="$TARGET/Contents/Helpers/OpenUsage Provider Settings.app"
SETTINGS_EXECUTABLE="$TARGET/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
STATE_DIR=${OPENUSAGE_STATE_DIR:-"$HOME/.local/state/openusage-bar"}
STATE_DIR=${STATE_DIR:A}
HOME_ROOT=${HOME:A}
[[ "$STATE_DIR" == "$HOME_ROOT"/* ]] || {
  print -u2 "OpenUsage state directory must be inside HOME"
  exit 2
}
BACKUP_ROOT="$STATE_DIR/backups/app"
LABEL_SUFFIX=${OPENUSAGE_LABEL_SUFFIX:-}
[[ -z "$LABEL_SUFFIX" || "$LABEL_SUFFIX" =~ '^[A-Za-z0-9][A-Za-z0-9.-]*$' ]] || {
  print -u2 "invalid LaunchAgent label suffix"
  exit 2
}
LABEL_SUFFIX_PART=${LABEL_SUFFIX:+.$LABEL_SUFFIX}
STATUS_LABEL="com.lune.openusagebar$LABEL_SUFFIX_PART"
COLLECTOR_LABEL="com.lune.openusagebar.collector$LABEL_SUFFIX_PART"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LAUNCHCTL=${OPENUSAGE_LAUNCHCTL:-/bin/launchctl}
HEALTH_PROBE=${OPENUSAGE_HEALTH_PROBE:-}
SOCKET="$HOME/.local/state/openusage-bar/openusage.sock"
NEW="$TARGET.rollback-new-$$"
FAILED="$TARGET.rollback-failed-$$"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ROLLBACK_ACTIVE=0
ACTIVITY_WAS_RUNNING=0
ACTIVITY_STOPPED=0
SETTINGS_WAS_RUNNING=0
SETTINGS_STOPPED=0

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

restore_current_runtime() {
  local failed=0
  if [[ -f "$AGENTS/$COLLECTOR_LABEL.plist" ]]; then
    bootstrap_agent "$COLLECTOR_LABEL" "$AGENTS/$COLLECTOR_LABEL.plist" || failed=1
  fi
  if [[ -f "$AGENTS/$STATUS_LABEL.plist" ]]; then
    bootstrap_agent "$STATUS_LABEL" "$AGENTS/$STATUS_LABEL.plist" || failed=1
  fi
  if (( ACTIVITY_STOPPED )); then
    reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE" || failed=1
  fi
  if (( SETTINGS_STOPPED )); then
    reopen_exact_activity "$SETTINGS_APP" "$SETTINGS_EXECUTABLE" || failed=1
  fi
  return "$failed"
}

preparation_failed() {
  local reason=$1
  prepare_bundle_stage_cleanup "$NEW" >/dev/null 2>&1 || true
  rm -rf "$NEW" || \
    print -u2 "rollback preparation recovery retained the staged app at $NEW"
  if ! restore_current_runtime; then
    print -u2 "rollback preparation recovery could not restart the current runtime"
  fi
  if ! prune_complete_app_backups "$BACKUP_ROOT" 2; then
    print -u2 "rollback preparation recovery could not prune app backups"
  fi
  print -u2 "rollback preparation failed: $reason"
  exit 1
}

if (( $# > 1 )); then
  print -u2 "usage: scripts/rollback_app.sh [backup-directory]"
  exit 2
fi
if (( $# == 1 )); then
  BACKUP=${1:A}
else
  BACKUP=$(newest_complete_app_backup "$BACKUP_ROOT") || {
    print -u2 "no complete rollback backup is available"
    exit 1
  }
fi
[[ "$BACKUP" == "$BACKUP_ROOT"/* ]] || {
  print -u2 "rollback backup must be inside the OpenUsage state directory"
  exit 2
}
validate_complete_app_backup "$BACKUP" || {
  print -u2 "rollback backup failed identity, version, signature, or hash validation"
  exit 1
}
[[ -d "$TARGET" ]] || {
  print -u2 "installed OpenUsage Bar app is unavailable"
  exit 1
}
validate_app_bundle "$TARGET" || {
  print -u2 "installed OpenUsage Bar app failed validation"
  exit 1
}
ATOMIC_SWAP="$TARGET/Contents/Resources/atomic-swap"
[[ -x "$ATOMIC_SWAP" ]] || {
  print -u2 "atomic swap helper is unavailable"
  exit 1
}
activity_has_exact_process "$ACTIVITY_EXECUTABLE" && ACTIVITY_WAS_RUNNING=1
activity_has_exact_process "$SETTINGS_EXECUTABLE" && SETTINGS_WAS_RUNNING=1

create_complete_app_backup "$TARGET" "$BACKUP_ROOT" "$STAMP" >/dev/null
prepare_bundle_stage_cleanup "$NEW" >/dev/null 2>&1 || true
prepare_bundle_stage_cleanup "$FAILED" >/dev/null 2>&1 || true
rm -rf "$NEW" "$FAILED"
/usr/bin/ditto "$BACKUP/OpenUsage Bar.app" "$NEW"
validate_app_bundle "$NEW"
COPIED_HASH=$(bundle_content_hash "$NEW") || {
  print -u2 "copied rollback bundle contains an unsafe symlink or unreadable file"
  exit 1
}
[[ "$COPIED_HASH" == $(plutil -extract bundleSHA256 raw "$BACKUP/metadata.plist") ]] || {
  print -u2 "copied rollback bundle hash changed during staging"
  exit 1
}

"$LAUNCHCTL" bootout "$DOMAIN/$STATUS_LABEL" >/dev/null 2>&1 || true
"$LAUNCHCTL" bootout "$DOMAIN/$COLLECTOR_LABEL" >/dev/null 2>&1 || true
if ! wait_unloaded "$STATUS_LABEL" || ! wait_unloaded "$COLLECTOR_LABEL"; then
  preparation_failed "LaunchAgents did not unload"
fi
if ! wait_for_socket_release; then
  preparation_failed "local API socket remained active"
fi
if (( ACTIVITY_WAS_RUNNING )); then
  if ! stop_exact_activity_processes "$ACTIVITY_EXECUTABLE"; then
    preparation_failed "Activity helper did not stop"
  fi
  ACTIVITY_STOPPED=1
fi
if (( SETTINGS_WAS_RUNNING )); then
  if ! stop_exact_activity_processes "$SETTINGS_EXECUTABLE"; then
    preparation_failed "Provider Settings helper did not stop"
  fi
  SETTINGS_STOPPED=1
fi
"$ATOMIC_SWAP" "$TARGET" "$NEW"

rollback_failed() {
  local code=${1:-$?}
  if (( ROLLBACK_ACTIVE )); then
    return "$code"
  fi
  ROLLBACK_ACTIVE=1
  trap '' EXIT INT TERM
  "$LAUNCHCTL" bootout "$DOMAIN/$STATUS_LABEL" >/dev/null 2>&1 || true
  "$LAUNCHCTL" bootout "$DOMAIN/$COLLECTOR_LABEL" >/dev/null 2>&1 || true
  if [[ -d "$NEW" && -d "$TARGET" ]]; then
    clear_activity_for_runtime_rollback "$ACTIVITY_EXECUTABLE" || {
      print -u2 "rollback recovery failed; visible Activity helper retained the rollback bundle"
      exit "$code"
    }
    clear_activity_for_runtime_rollback "$SETTINGS_EXECUTABLE" || {
      print -u2 "rollback recovery failed; visible Provider Settings helper retained the rollback bundle"
      exit "$code"
    }
    "$ATOMIC_SWAP" "$TARGET" "$NEW" || {
      print -u2 "rollback recovery failed; both app copies were retained"
      exit "$code"
    }
    mv "$NEW" "$FAILED"
  fi
  restore_current_runtime || \
    print -u2 "rollback recovery could not restart the original runtime"
  print -u2 "rollback health verification failed; the original app was restored"
  exit "$code"
}
trap rollback_failed EXIT INT TERM

[[ -f "$AGENTS/$COLLECTOR_LABEL.plist" ]] && \
  bootstrap_agent "$COLLECTOR_LABEL" "$AGENTS/$COLLECTOR_LABEL.plist"
[[ -f "$AGENTS/$STATUS_LABEL.plist" ]] && \
  bootstrap_agent "$STATUS_LABEL" "$AGENTS/$STATUS_LABEL.plist"
verify_local_api_contract "$SOCKET" "$HEALTH_PROBE"
validate_app_bundle "$TARGET"
if (( ACTIVITY_STOPPED )); then
  reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE"
fi
if (( SETTINGS_STOPPED )); then
  reopen_exact_activity "$SETTINGS_APP" "$SETTINGS_EXECUTABLE"
fi

trap - EXIT INT TERM
prepare_bundle_stage_cleanup "$NEW" || \
  print -u2 "rollback succeeded; previous app stage permissions were retained at $NEW"
commit_bundle_transaction "$NEW" || \
  print -u2 "rollback succeeded; previous app stage cleanup was skipped at $NEW"
prune_complete_app_backups "$BACKUP_ROOT" 2
print "rolled back OpenUsage Bar to $(bundle_metadata_value "$TARGET" CFBundleShortVersionString)"
print "rollback backups retained at $BACKUP_ROOT"
