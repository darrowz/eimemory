#!/usr/bin/env bash
set -euo pipefail

USER_SYSTEMD_DIR="${1:?user systemd directory is required}"

BASE_UNITS=(
  eimemory-audit-verify.service
  eimemory-console.service
  eimemory-l5-observation-gate.service
  eimemory-learn-dashboard.service
  eimemory-learn-think.service
  eimemory-learn-watch.service
  eimemory-nightly.service
  eimemory-rpc.service
  eimemory-timer-monitor.service
  openclaw-loop-watch.service
  openclaw-loop-compact.service
  openclaw-stuck-watchdog.service
)

declare -A SEEN=()
emit_once() {
  local unit="$1"
  if [[ ! "$unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || [ -n "${SEEN[$unit]:-}" ]; then
    return
  fi
  SEEN["$unit"]=1
  printf '%s\n' "$unit"
}

for unit in "${BASE_UNITS[@]}"; do
  emit_once "$unit"
done

if [ ! -d "$USER_SYSTEMD_DIR" ] || [ -L "$USER_SYSTEMD_DIR" ]; then
  exit 0
fi

find "$USER_SYSTEMD_DIR" -maxdepth 1 -type f -name '*.service' -print0 | sort -z | while IFS= read -r -d '' unit_path; do
  unit="$(basename "$unit_path")"
  if grep -Fq '/opt/eimemory/current' "$unit_path"; then
    emit_once "$unit"
  else
    grep_status="$?"
    if [ "$grep_status" -gt 1 ]; then
      exit "$grep_status"
    fi
  fi
done
