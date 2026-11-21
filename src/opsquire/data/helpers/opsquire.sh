#!/usr/bin/env bash
# Structured events for bash scripts run by `opsquire run`: source "$OPSQUIRE_HELPERS/opsquire.sh"

OPSQUIRE_MODE="${OPSQUIRE_MODE:-dry}"
export OPSQUIRE_MODE

# Redefine sq_cleanup after sourcing to undo scratch work; it runs on Ctrl-C, SIGTERM and SIGHUP.
sq_cleanup() { :; }
sq__interrupted() {
  trap - INT TERM HUP
  sq_cleanup
  sq_warn "interrupted by the operator"
  exit 130
}
trap sq__interrupted INT TERM HUP

sq_emit() {
  python3 -c 'import json, sys; a = sys.argv[1:]; print("@@OPSQUIRE " + json.dumps(dict(zip(a[0::2], a[1::2]))), flush=True)' "$@"
}

sq_step() { sq_emit event step msg "$*"; }
sq_result() { sq_emit event result msg "$*"; }
sq_warn() { sq_emit event warn msg "$*"; }
sq_error() { sq_emit event error msg "$*"; }
sq_progress() { sq_emit event progress msg "$1" "done" "$2" total "$3" unit "${4:-}"; }
sq_target() { sq_emit event target target "$1" status "$2" msg "${3:-}"; }

sq_hosts() {
  local t
  for t in ${OPSQUIRE_TARGETS:-}; do echo "${t%%/*}"; done | awk '!seen[$0]++'
}

sq_lock_token() {
  local pair
  for pair in ${OPSQUIRE_LOCK_TOKENS:-}; do
    if [[ "${pair%=*}" == "$1" ]]; then
      printf '%s\n' "${pair##*=}"
      return 0
    fi
  done
  return 1
}

sq__announce_prompt() {
  sq_emit event waiting msg "$*"
  if [[ -n "${OPSQUIRE_SYNC_PATH:-}" ]]; then
    read -r -t 10 -n 1 _ <>"$OPSQUIRE_SYNC_PATH" 2>/dev/null || true
  fi
}

sq_confirm() {
  local answer="" rc
  sq__announce_prompt "$*"
  if { printf '\n>>> %s [y/N] ' "$*" >/dev/tty; } 2>/dev/null; then
    # In slices of a second: a trapped Ctrl-C runs between them whether or not this bash restarts an interrupted read.
    while :; do
      rc=0
      read -r -t 1 answer </dev/tty || rc=$?
      if ((rc == 0)); then break; fi
      if ((rc <= 128)); then answer=""; break; fi
    done
  fi
  if [[ "${answer,,}" == "y" ]]; then
    sq_emit event confirm msg "$*" answer y
  else
    sq_emit event confirm msg "$*" answer n
    exit 3
  fi
}
