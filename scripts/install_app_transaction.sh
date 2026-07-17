#!/bin/zsh

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
