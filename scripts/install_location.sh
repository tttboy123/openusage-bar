#!/bin/zsh

# Resolve one stable lifecycle location. Existing installations take precedence
# so updates, rollbacks, and uninstalls keep targeting the same app bundle.
resolve_openusage_install_dir() {
  if [[ -n ${OPENUSAGE_INSTALL_DIR:-} ]]; then
    print -r -- "$OPENUSAGE_INSTALL_DIR"
    return 0
  fi

  local system_dir=${OPENUSAGE_SYSTEM_APPLICATIONS_DIR:-/Applications}
  local user_dir="$HOME/Applications"
  local app_name="OpenUsage Bar.app"

  if [[ -d "$system_dir/$app_name" ]]; then
    print -r -- "$system_dir"
  elif [[ -d "$user_dir/$app_name" ]]; then
    print -r -- "$user_dir"
  elif [[ -d "$system_dir" && -w "$system_dir" ]]; then
    print -r -- "$system_dir"
  else
    print -r -- "$user_dir"
  fi
}

reveal_openusage_install() {
  local app=$1
  case ${OPENUSAGE_REVEAL_IN_FINDER:-1} in
    0|false|FALSE|no|NO) return 0 ;;
  esac
  local open_command=${OPENUSAGE_OPEN_COMMAND:-/usr/bin/open}
  "$open_command" -R "$app" >/dev/null 2>&1
}
