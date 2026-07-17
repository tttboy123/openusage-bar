#!/bin/zsh

ACTIVITY_STOP_MATCHED=0
ACTIVITY_STOP_SIGNALLED=0

activity_command_matches() {
  local expected=$1
  local command=$2
  case "$command" in
    "$expected"|\
    "$expected --route activity"|\
    "$expected --route capacity"|\
    "$expected --route api-spend"|\
    "$expected --route local-tools"|\
    "$expected --route providers"|\
    "$expected --route health") return 0 ;;
  esac
  return 1
}

activity_process_identity() {
  local pid=$1
  local expected=$2
  local line weekday month day clock year command
  [[ "$pid" == <-> ]] || return 1
  line=$(/bin/ps -ww -p "$pid" -o lstart=,command= 2>/dev/null) || return 1
  read -r weekday month day clock year command <<< "$line"
  activity_command_matches "$expected" "$command" || return 1
  print -r -- "$weekday $month $day $clock $year"
}

activity_process_matches() {
  local pid=$1
  local expected=$2
  local started=${3:-}
  local current
  current=$(activity_process_identity "$pid" "$expected") || return 1
  [[ -z "$started" || "$current" == "$started" ]]
}

activity_exact_processes() {
  local expected=$1
  local snapshot pid weekday month day clock year command
  snapshot=$(/bin/ps -ww -axo pid=,lstart=,command=) || return 1
  while read -r pid weekday month day clock year command; do
    if activity_command_matches "$expected" "$command"; then
      print -r -- "$pid"$'\t'"$weekday $month $day $clock $year"
    fi
  done <<< "$snapshot"
  return 0
}

activity_exact_pids() {
  local expected=$1
  local entry tab=$'\t'
  local snapshot
  snapshot=$(activity_exact_processes "$expected") || return 1
  for entry in ${(f)snapshot}; do
    print -r -- "${entry%%${tab}*}"
  done
  return 0
}

activity_has_exact_process() {
  local expected=$1
  local entry pid started snapshot tab=$'\t'
  snapshot=$(activity_exact_processes "$expected") || return 1
  for entry in ${(f)snapshot}; do
    pid=${entry%%${tab}*}
    started=${entry#*${tab}}
    activity_process_matches "$pid" "$expected" "$started" && return 0
  done
  return 1
}

signal_exact_activity_pid() {
  local expected=$1
  local pid=$2
  local started=$3
  local signal=$4
  local signaler=${5:-/bin/kill}
  activity_process_matches "$pid" "$expected" "$started" || return 0
  if "$signaler" "-$signal" "$pid" 2>/dev/null; then
    ACTIVITY_STOP_SIGNALLED=1
    return 0
  fi
  activity_process_matches "$pid" "$expected" "$started" || return 0
  return 1
}

stop_exact_activity_processes() {
  local expected=$1
  local attempts=${2:-50}
  local delay=${3:-0.1}
  local signaler=${4:-/bin/kill}
  local term_rounds=$(( attempts / 2 ))
  local empty_rounds=0
  local attempt entry pid started signal snapshot tab=$'\t'
  local -a processes
  ACTIVITY_STOP_MATCHED=0
  ACTIVITY_STOP_SIGNALLED=0

  for (( attempt = 1; attempt <= attempts; attempt++ )); do
    snapshot=$(activity_exact_processes "$expected") || return 1
    processes=(${(f)snapshot})
    if (( ${#processes} == 0 )); then
      empty_rounds=$(( empty_rounds + 1 ))
      if (( empty_rounds >= 2 )); then
        return 0
      fi
    else
      ACTIVITY_STOP_MATCHED=1
      empty_rounds=0
      signal=TERM
      if (( attempt > term_rounds )); then
        signal=KILL
      fi
      for entry in "$processes[@]"; do
        pid=${entry%%${tab}*}
        started=${entry#*${tab}}
        signal_exact_activity_pid "$expected" "$pid" "$started" "$signal" "$signaler" || true
      done
    fi
    sleep "$delay"
  done

  snapshot=$(activity_exact_processes "$expected") || return 1
  processes=(${(f)snapshot})
  (( ${#processes} == 0 ))
}

clear_activity_for_runtime_rollback() {
  local expected=$1
  local attempts=${2:-50}
  local delay=${3:-0.1}
  local signaler=${4:-/bin/kill}
  if stop_exact_activity_processes "$expected" "$attempts" "$delay" "$signaler"; then
    return 0
  fi
  print -u2 "runtime rollback incomplete: Activity helper is still running; current bundle retained"
  return 1
}

reopen_exact_activity() {
  local app=$1
  local executable=$2
  local opener=${3:-/usr/bin/open}
  local attempts=${4:-50}
  local delay=${5:-0.1}
  local attempt
  "$opener" "$app"
  for (( attempt = 0; attempt < attempts; attempt++ )); do
    activity_has_exact_process "$executable" && return 0
    sleep "$delay"
  done
  return 1
}
