#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
source "$ROOT/scripts/install_location.sh"
INSTALL_DIR=$(resolve_openusage_install_dir)
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
SYSTEM_TARGET="${OPENUSAGE_SYSTEM_APPLICATIONS_DIR:-/Applications}/OpenUsage Bar.app"
USER_TARGET="$HOME/Applications/OpenUsage Bar.app"
APP_TARGETS=("$TARGET")
if [[ -z ${OPENUSAGE_INSTALL_DIR:-} ]]; then
  APP_TARGETS+=("$SYSTEM_TARGET" "$USER_TARGET")
fi
ACTIVITY_SUFFIX="Contents/Helpers/OpenUsage Activity.app/Contents/MacOS/OpenUsage Activity"
SETTINGS_SUFFIX="Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABEL_SUFFIX=${OPENUSAGE_LABEL_SUFFIX:-}
[[ -z "$LABEL_SUFFIX" || "$LABEL_SUFFIX" =~ '^[A-Za-z0-9][A-Za-z0-9.-]*$' ]] || {
  print -u2 "invalid LaunchAgent label suffix"
  exit 2
}
LABEL_SUFFIX_PART=${LABEL_SUFFIX:+.$LABEL_SUFFIX}
LAUNCHCTL=${OPENUSAGE_LAUNCHCTL:-/bin/launchctl}
STATE_DIR=${OPENUSAGE_STATE_DIR:-"$HOME/.local/state/openusage-bar"}
STATE_DIR=${STATE_DIR:A}
HOME_ROOT=${HOME:A}
PURGE=0

source "$ROOT/scripts/activity_install_process.sh"
source "$ROOT/scripts/install_app_transaction.sh"

is_openusage_bundle() {
  local app=$1
  local info="$app/Contents/Info.plist"
  [[ -f "$info" ]] || return 1
  [[ $(plutil -extract CFBundleIdentifier raw "$info" 2>/dev/null) == \
    com.lune.openusagebar ]]
}

if [[ ${1:-} == --purge-data ]]; then
  PURGE=1
elif [[ $# -gt 0 ]]; then
  print -u2 "usage: scripts/uninstall_app.sh [--purge-data]"
  exit 2
fi
if (( PURGE )) && [[ "$STATE_DIR" != "$HOME_ROOT"/* ]]; then
  print -u2 "refusing to purge an OpenUsage state directory outside HOME"
  exit 2
fi

for label in "com.lune.openusagebar$LABEL_SUFFIX_PART" "com.lune.openusagebar.collector$LABEL_SUFFIX_PART"; do
  "$LAUNCHCTL" bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
  rm -f "$AGENTS/$label.plist"
done
for app_target in "${(@u)APP_TARGETS}"; do
  if is_openusage_bundle "$app_target"; then
    stop_exact_activity_processes "$app_target/$ACTIVITY_SUFFIX"
    stop_exact_activity_processes "$app_target/$SETTINGS_SUFFIX"
    prepare_bundle_stage_cleanup "$app_target" || {
      print -u2 "refusing to remove an unsafe OpenUsage app bundle at $app_target"
      exit 1
    }
    rm -rf "$app_target"
  else
    stop_exact_activity_processes "$app_target/$ACTIVITY_SUFFIX" 50 0.1 \
      /bin/kill com.lune.openusagebar.activity
    stop_exact_activity_processes "$app_target/$SETTINGS_SUFFIX" 50 0.1 \
      /bin/kill com.lune.openusagebar.settings
  fi
done

if (( PURGE )); then
  for staged_app in "$STATE_DIR"/**/*.app(N/); do
    if is_openusage_bundle "$staged_app"; then
      prepare_bundle_stage_cleanup "$staged_app" || {
        print -u2 "refusing to purge an unsafe OpenUsage app backup at $staged_app"
        exit 1
      }
    fi
  done
  rm -rf \
    "$STATE_DIR" \
    "$HOME/.config/openusage-bar"
  rm -f "$HOME/Library/Logs"/OpenUsageBar.*.log(N)
  print "uninstalled OpenUsage Bar and removed local usage data"
else
  print "uninstalled OpenUsage Bar; local data and Keychain items were preserved"
fi
