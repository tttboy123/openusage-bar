#!/bin/zsh

verify_local_api_contract() {
  local socket_path=$1
  local external_probe=${2:-}
  if [[ -n "$external_probe" ]]; then
    "$external_probe" --timeout 20 "$socket_path"
    return
  fi
  # This dependency-free probe mirrors scripts/verify_local_api.py so the
  # packaged installer does not require a separately installed Python runtime.
  local attempt route payload schema
  for attempt in {1..100}; do
    if [[ -S "$socket_path" ]]; then
      local healthy=1
      for route in health schema summary; do
        payload=$(/usr/bin/curl --fail --silent --unix-socket "$socket_path" \
          "http://localhost/v1/$route") || { healthy=0; break; }
        schema=$(print -rn -- "$payload" | plutil -extract schemaVersion raw - 2>/dev/null) || {
          healthy=0
          break
        }
        [[ "$schema" == 1.0 ]] || { healthy=0; break; }
        case "$route" in
          health) [[ $(print -rn -- "$payload" | plutil -extract health.ok raw - 2>/dev/null) == true ]] || healthy=0 ;;
          schema) [[ $(print -rn -- "$payload" | plutil -type routes - 2>/dev/null) == array ]] || healthy=0 ;;
          summary) print -rn -- "$payload" | plutil -extract todayTokens raw - >/dev/null 2>&1 || healthy=0 ;;
        esac
        (( healthy )) || break
      done
      (( healthy )) && return 0
    fi
    sleep 0.2
  done
  return 1
}

bundle_metadata_value() {
  local app=$1
  local key=$2
  plutil -extract "$key" raw "$app/Contents/Info.plist" 2>/dev/null
}

bundle_content_hash() {
  local app=$1
  [[ -d "$app" ]] || return 1
  setopt local_options pipe_fail
  (
    cd "$app"
    local link target resolved digest root=${PWD:A}
    # `find -s` gives stable ordering and `-exec ... +` hashes the whole bundle
    # in bounded batches instead of spawning one process per file.
    /usr/bin/find -s . -type f -exec /usr/bin/shasum -a 256 {} + || return 1
    for link in **/*(@DN); do
      target=$(readlink "$link") || return 1
      [[ "$target" != /* ]] || return 1
      resolved=${link:A}
      [[ "$resolved" == "$root"/* ]] || return 1
      digest=$(print -rn -- "$target" | /usr/bin/shasum -a 256 | awk '{print $1}') || return 1
      print -r -- "$digest  $link -> $target"
    done
  ) | /usr/bin/shasum -a 256 | awk '{print $1}'
}

validate_app_bundle() {
  local app=$1
  local expected_id=${2:-com.lune.openusagebar}
  [[ ! -L "$app" ]] || return 1
  [[ -x "$app/Contents/MacOS/OpenUsage Bar" ]] || return 1
  [[ $(bundle_metadata_value "$app" CFBundleIdentifier) == "$expected_id" ]] || return 1
  bundle_metadata_value "$app" CFBundleShortVersionString >/dev/null || return 1
  bundle_metadata_value "$app" CFBundleVersion >/dev/null || return 1
  /usr/bin/codesign --verify --deep --strict "$app" >/dev/null 2>&1
}

create_complete_app_backup() {
  local app=$1
  local backup_root=$2
  local stamp=${3:-$(date -u +%Y%m%dT%H%M%SZ)}
  local version build identifier bundle_hash temp destination metadata
  validate_app_bundle "$app" || return 1
  version=$(bundle_metadata_value "$app" CFBundleShortVersionString) || return 1
  build=$(bundle_metadata_value "$app" CFBundleVersion) || return 1
  identifier=$(bundle_metadata_value "$app" CFBundleIdentifier) || return 1
  mkdir -p "$backup_root"
  temp="$backup_root/.incomplete-$stamp-$$"
  destination="$backup_root/$stamp-v$version-b$build"
  [[ ! -e "$destination" ]] || destination="$destination-$$"
  rm -rf "$temp"
  mkdir -p "$temp"
  /usr/bin/ditto "$app" "$temp/OpenUsage Bar.app" || {
    rm -rf "$temp"
    return 1
  }
  validate_app_bundle "$temp/OpenUsage Bar.app" || {
    rm -rf "$temp"
    return 1
  }
  bundle_hash=$(bundle_content_hash "$temp/OpenUsage Bar.app") || {
    rm -rf "$temp"
    return 1
  }
  metadata="$temp/metadata.plist"
  plutil -create xml1 "$metadata"
  plutil -insert schemaVersion -integer 1 "$metadata"
  plutil -insert bundleIdentifier -string "$identifier" "$metadata"
  plutil -insert version -string "$version" "$metadata"
  plutil -insert build -string "$build" "$metadata"
  plutil -insert bundleSHA256 -string "$bundle_hash" "$metadata"
  plutil -insert createdAt -string "$stamp" "$metadata"
  mv "$temp" "$destination"
  print -r -- "$destination"
}

validate_complete_app_backup() {
  local backup=$1
  local metadata="$backup/metadata.plist"
  local app="$backup/OpenUsage Bar.app"
  local identifier version build expected_hash actual_hash
  [[ -f "$metadata" ]] || return 1
  plutil -lint "$metadata" >/dev/null || return 1
  [[ $(plutil -extract schemaVersion raw "$metadata") == 1 ]] || return 1
  identifier=$(plutil -extract bundleIdentifier raw "$metadata") || return 1
  version=$(plutil -extract version raw "$metadata") || return 1
  build=$(plutil -extract build raw "$metadata") || return 1
  expected_hash=$(plutil -extract bundleSHA256 raw "$metadata") || return 1
  [[ "$identifier" == com.lune.openusagebar ]] || return 1
  validate_app_bundle "$app" "$identifier" || return 1
  [[ $(bundle_metadata_value "$app" CFBundleShortVersionString) == "$version" ]] || return 1
  [[ $(bundle_metadata_value "$app" CFBundleVersion) == "$build" ]] || return 1
  actual_hash=$(bundle_content_hash "$app") || return 1
  [[ "$actual_hash" == "$expected_hash" ]]
}

prune_complete_app_backups() {
  local backup_root=$1
  local keep=${2:-2}
  local -a complete
  local entry remove_count index
  [[ "$keep" == <-> ]] || return 2
  complete=()
  for entry in "$backup_root"/*/metadata.plist(N); do
    validate_complete_app_backup "$entry:h" && complete+=("$entry:h")
  done
  remove_count=$(( ${#complete} - keep ))
  if (( remove_count > 0 )); then
    for (( index = 1; index <= remove_count; index++ )); do
      rm -rf "$complete[$index]"
    done
  fi
  for entry in "$backup_root"/.incomplete-*(N); do
    rm -rf "$entry"
  done
}

newest_complete_app_backup() {
  local backup_root=$1
  local entry newest=""
  for entry in "$backup_root"/*/metadata.plist(N); do
    validate_complete_app_backup "$entry:h" || continue
    newest="$entry:h"
  done
  [[ -n "$newest" ]] || return 1
  print -r -- "$newest"
}

install_bundle_transaction() {
  local atomic_swap=$1
  local target=$2
  local staged=$3
  if [[ -d "$target" ]]; then
    HAD_TARGET=1
    "$atomic_swap" "$target" "$staged"
    SWAPPED=1
  else
    mv "$staged" "$target"
    FIRST_INSTALLED=1
  fi
}

rollback_bundle_transaction() {
  local atomic_swap=$1
  local target=$2
  local staged=$3
  local failed=$4
  rm -rf "$failed"
  if (( SWAPPED )); then
    "$atomic_swap" "$target" "$staged" || return 1
    mv "$staged" "$failed"
  elif (( FIRST_INSTALLED )); then
    [[ -d "$target" ]] && mv "$target" "$failed"
    rm -rf "$staged"
  else
    rm -rf "$staged"
  fi
}

commit_bundle_transaction() {
  local staged=$1
  rm -rf "$staged"
}

cleanup_legacy_previous_bundles() {
  local applications_dir=$1
  local previous
  local failed=0
  for previous in "$applications_dir"/OpenUsage\ Bar.app.previous-<->T<->Z(N); do
    if [[ $(plutil -extract CFBundleIdentifier raw "$previous/Contents/Info.plist" 2>/dev/null) == com.lune.openusagebar ]]; then
      rm -rf "$previous" || failed=1
    fi
  done
  return "$failed"
}
