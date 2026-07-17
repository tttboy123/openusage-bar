#!/bin/zsh
set -euo pipefail

INSTALL_DIR=${OPENUSAGE_INSTALL_DIR:-/Applications}
TARGET="$INSTALL_DIR/OpenUsage Bar.app"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
PURGE=0

if [[ ${1:-} == --purge-data ]]; then
  PURGE=1
elif [[ $# -gt 0 ]]; then
  print -u2 "usage: scripts/uninstall_app.sh [--purge-data]"
  exit 2
fi

for label in com.lune.openusagebar com.lune.openusagebar.collector; do
  launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
  rm -f "$AGENTS/$label.plist"
done
rm -rf "$TARGET"

if (( PURGE )); then
  rm -rf \
    "$HOME/.local/state/openusage-bar" \
    "$HOME/.config/openusage-bar"
  rm -f "$HOME/Library/Logs/OpenUsageBar."*.log
  print "uninstalled OpenUsage Bar and removed local usage data"
else
  print "uninstalled OpenUsage Bar; local data and Keychain items were preserved"
fi
