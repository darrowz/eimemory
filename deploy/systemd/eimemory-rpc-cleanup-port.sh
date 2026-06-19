#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8091}"
MATCH="${2:-serve-eibrain-rpc}"
GRACE_SECONDS="${EIMEMORY_RPC_CLEANUP_GRACE_SECONDS:-1}"

_listener_pids() {
  ss -ltnp 2>/dev/null |
    awk -v port=":${PORT} " '
      index($0, port) {
        line = $0
        while (match(line, /pid=[0-9]+/)) {
          print substr(line, RSTART + 4, RLENGTH - 4)
          line = substr(line, RSTART + RLENGTH)
        }
      }
    ' |
    sort -u
}

_cmdline() {
  local pid="$1"
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true
}

_matching_listeners() {
  local pid cmd
  while read -r pid; do
    [ -n "$pid" ] || continue
    cmd="$(_cmdline "$pid")"
    case "$cmd" in
      *eimemory*"$MATCH"*) printf '%s\n' "$pid" ;;
    esac
  done
}

mapfile -t pids < <(_listener_pids | _matching_listeners)
if [ "${#pids[@]}" -eq 0 ]; then
  exit 0
fi

for pid in "${pids[@]}"; do
  kill -TERM "$pid" 2>/dev/null || true
done

sleep "$GRACE_SECONDS"

for pid in "${pids[@]}"; do
  if [ -d "/proc/$pid" ]; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
done
