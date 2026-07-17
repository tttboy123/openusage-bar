#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
SOURCE="$ROOT/dist/OpenUsage Bar.app"
INSTALL_DIR=${OPENUSAGE_INSTALL_DIR:-/Applications}
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
ACTIVITY_APP="$TARGET/Contents/Helpers/OpenUsage Activity.app"
ACTIVITY_EXECUTABLE="$TARGET/Contents/Helpers/OpenUsage Activity.app/Contents/MacOS/OpenUsage Activity"
NEW="$TARGET.new-$$"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$ROOT/deployed-backups/$STAMP"
ATOMIC_SWAP="$SOURCE/Contents/Resources/atomic-swap"
AGENTS="$HOME/Library/LaunchAgents"
OLD_LABEL=com.lune.openusage-menubar
STATUS_LABEL=com.lune.openusagebar
COLLECTOR_LABEL=com.lune.openusagebar.collector
STATUS_PLIST="$AGENTS/$STATUS_LABEL.plist"
COLLECTOR_PLIST="$AGENTS/$COLLECTOR_LABEL.plist"
OLD_PLIST="$AGENTS/$OLD_LABEL.plist"
SOCKET="$HOME/.local/state/openusage-bar/openusage.sock"
DOMAIN="gui/$(id -u)"
MUTATED=0
HAD_TARGET=0
SWAPPED=0
FIRST_INSTALLED=0
ACTIVITY_WAS_RUNNING=0
ACTIVITY_STOPPED=0

source "$ROOT/scripts/install_app_transaction.sh"
source "$ROOT/scripts/activity_install_process.sh"

bootstrap_agent() {
  local label=$1
  local plist=$2
  local attempt
  for attempt in {1..20}; do
    if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
      return 0
    fi
    launchctl bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1 || true
    sleep 0.1
  done
  launchctl print "$DOMAIN/$label" >/dev/null 2>&1
}

wait_unloaded() {
  local label=$1
  local attempt
  for attempt in {1..50}; do
    if ! launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

rollback() {
  local code=$?
  local bundle_restored=1
  local activity_runtime_cleared=1
  local activity_clear_rc=0
  trap - EXIT INT TERM
  if (( MUTATED )); then
    launchctl bootout "$DOMAIN/$STATUS_LABEL" >/dev/null 2>&1 || true
    launchctl bootout "$DOMAIN/$COLLECTOR_LABEL" >/dev/null 2>&1 || true
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
    [[ -f "$OLD_PLIST" ]] && launchctl bootstrap "$DOMAIN" "$OLD_PLIST" >/dev/null 2>&1 || true
    [[ -f "$STATUS_PLIST" ]] && launchctl bootstrap "$DOMAIN" "$STATUS_PLIST" >/dev/null 2>&1 || true
    [[ -f "$COLLECTOR_PLIST" ]] && launchctl bootstrap "$DOMAIN" "$COLLECTOR_PLIST" >/dev/null 2>&1 || true
    if (( HAD_TARGET && ACTIVITY_STOPPED && activity_runtime_cleared && bundle_restored )); then
      reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE" || \
        print -u2 "restored Activity helper could not be reopened"
    fi
  fi
  print -u2 "installation rolled back; backup retained at $BACKUP"
  exit "$code"
}
trap rollback EXIT INT TERM

[[ -d "$SOURCE" ]] || { print -u2 "build artifact unavailable"; exit 1; }
[[ -x "$ATOMIC_SWAP" ]] || { print -u2 "atomic swap helper unavailable"; exit 1; }
codesign --verify --deep --strict "$SOURCE"
activity_has_exact_process "$ACTIVITY_EXECUTABLE" && ACTIVITY_WAS_RUNNING=1
mkdir -p "$BACKUP" "$AGENTS" "$HOME/Library/Logs" "$INSTALL_DIR"
if [[ -d "$TARGET" ]]; then
  /usr/bin/ditto "$TARGET" "$BACKUP/OpenUsage Bar.app"
  [[ -x "$BACKUP/OpenUsage Bar.app/Contents/MacOS/OpenUsage Bar" ]] || exit 1
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
for label in "$STATUS_LABEL" "$COLLECTOR_LABEL"; do
  sed "s|__HOME__|$HOME|g" "$SOURCE/Contents/Resources/LaunchAgents/$label.plist" > "$BACKUP/$label.new.plist"
  template_program=$(plutil -extract ProgramArguments.0 raw "$BACKUP/$label.new.plist")
  resolved_program=${template_program/__APP__/$TARGET}
  [[ "$resolved_program" != "$template_program" ]] || exit 1
  plutil -replace ProgramArguments.0 -string "$resolved_program" "$BACKUP/$label.new.plist"
  plutil -lint "$BACKUP/$label.new.plist" >/dev/null
done

MUTATED=1
launchctl bootout "$DOMAIN/$OLD_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$DOMAIN/$STATUS_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$DOMAIN/$COLLECTOR_LABEL" >/dev/null 2>&1 || true
wait_unloaded "$OLD_LABEL"
wait_unloaded "$STATUS_LABEL"
wait_unloaded "$COLLECTOR_LABEL"
rm -f "$AGENTS"/com.lune.openusage-menubar*.plist(N)
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
bootstrap_agent "$COLLECTOR_LABEL" "$COLLECTOR_PLIST"
bootstrap_agent "$STATUS_LABEL" "$STATUS_PLIST"

launchctl print "$DOMAIN/$COLLECTOR_LABEL" >/dev/null
launchctl print "$DOMAIN/$STATUS_LABEL" >/dev/null
curl --fail --silent --show-error --unix-socket "$SOCKET" http://localhost/v1/health >/dev/null
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
print "installed $TARGET"
print "backup retained at $BACKUP"
