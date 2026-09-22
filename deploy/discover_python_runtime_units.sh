#!/usr/bin/env bash
set -euo pipefail

USER_SYSTEMD_DIR="${1:?user systemd directory is required}"

BASE_UNITS=(
  eimemory-audit-verify.service
  eimemory-code-implementation-refresh.service
  eimemory-learn-dashboard.service
  eimemory-learn-think.service
  eimemory-learn-watch.service
  eimemory-nightly.service
  eimemory-rpc.service
  eimemory-timer-monitor.service
  # Core gateways must always be eligible for runtime-identity refresh so a
  # deploy cannot leave them on a stale release commit / colleague identity.
  hermes-gateway.service
  openclaw-gateway.service
)

# Colleague/agent gateway units on multi-profile hosts (鸿欣/鸿泰/小马哥/鸿睿).
# Emit only when the unit file already exists so standalone installs stay lean.
COLLEAGUE_GATEWAY_UNITS=(
  hongxin-gateway.service
  hongtai-gateway.service
  xiaomage-gateway.service
  hongrui-gateway.service
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

for unit in "${COLLEAGUE_GATEWAY_UNITS[@]}"; do
  if [ -f "$USER_SYSTEMD_DIR/$unit" ] && [ ! -L "$USER_SYSTEMD_DIR/$unit" ]; then
    emit_once "$unit"
  fi
done

# Capture find output first so a failing find exits the script (installer contract).
_service_paths="$(find "$USER_SYSTEMD_DIR" -maxdepth 1 -type f -name '*.service' -print0 | sort -z)" || exit $?
if [ -n "${_service_paths}" ]; then
  while IFS= read -r -d '' unit_path; do
    unit="$(basename "$unit_path")"
    if grep -Fq '/opt/eimemory/current' "$unit_path"; then
      emit_once "$unit"
    else
      grep_status="$?"
      if [ "$grep_status" -gt 1 ]; then
        exit "$grep_status"
      fi
    fi
  done < <(printf '%s' "$_service_paths")
fi

_dropin_dirs="$(find "$USER_SYSTEMD_DIR" -maxdepth 1 -type d -name '*.service.d' -print0 | sort -z)" || exit $?
if [ -n "$_dropin_dirs" ]; then
  while IFS= read -r -d '' dropin_dir; do
    unit="$(basename "$dropin_dir" .d)"
    if [[ ! "$unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
      continue
    fi
    if find "$dropin_dir" -maxdepth 1 -type f \( -name '*eimemory*' -o -name '*python-runtime*' \) -print -quit | grep -q .; then
      emit_once "$unit"
    fi
  done < <(printf '%s' "$_dropin_dirs")
fi

_gateway_paths="$(find "$USER_SYSTEMD_DIR" -maxdepth 1 -type f -name '*-gateway.service' -print0 | sort -z)" || exit $?
if [ -n "$_gateway_paths" ]; then
  while IFS= read -r -d '' unit_path; do
    emit_once "$(basename "$unit_path")"
  done < <(printf '%s' "$_gateway_paths")
fi
