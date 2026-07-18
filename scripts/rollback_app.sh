#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
source "$ROOT/scripts/install_location.sh"
INSTALL_DIR=$(resolve_openusage_install_dir)
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
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

source "$ROOT/scripts/install_app_transaction.sh"

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

create_complete_app_backup "$TARGET" "$BACKUP_ROOT" "$STAMP" >/dev/null
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
    "$ATOMIC_SWAP" "$TARGET" "$NEW" || {
      print -u2 "rollback recovery failed; both app copies were retained"
      exit "$code"
    }
    mv "$NEW" "$FAILED"
  fi
  [[ -f "$AGENTS/$COLLECTOR_LABEL.plist" ]] && \
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$COLLECTOR_LABEL.plist" >/dev/null 2>&1 || true
  [[ -f "$AGENTS/$STATUS_LABEL.plist" ]] && \
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$STATUS_LABEL.plist" >/dev/null 2>&1 || true
  print -u2 "rollback health verification failed; the original app was restored"
  exit "$code"
}
trap rollback_failed EXIT INT TERM

[[ -f "$AGENTS/$COLLECTOR_LABEL.plist" ]] && \
  "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$COLLECTOR_LABEL.plist" >/dev/null
[[ -f "$AGENTS/$STATUS_LABEL.plist" ]] && \
  "$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$STATUS_LABEL.plist" >/dev/null
verify_local_api_contract "$SOCKET" "$HEALTH_PROBE"
validate_app_bundle "$TARGET"

trap - EXIT INT TERM
rm -rf "$NEW"
prune_complete_app_backups "$BACKUP_ROOT" 2
print "rolled back OpenUsage Bar to $(bundle_metadata_value "$TARGET" CFBundleShortVersionString)"
print "rollback backups retained at $BACKUP_ROOT"
