#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
source "$ROOT/scripts/install_location.sh"
INSTALL_DIR=$(resolve_openusage_install_dir)
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
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
rm -rf "$TARGET"

if (( PURGE )); then
  rm -rf \
    "$STATE_DIR" \
    "$HOME/.config/openusage-bar"
  rm -f "$HOME/Library/Logs"/OpenUsageBar.*.log(N)
  print "uninstalled OpenUsage Bar and removed local usage data"
else
  print "uninstalled OpenUsage Bar; local data and Keychain items were preserved"
fi
