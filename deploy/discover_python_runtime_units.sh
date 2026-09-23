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
)

# Colleague/agent gateway units on multi-profile hosts (鸿欣/鸿泰/小马哥/鸿睿).
# Emit only when the unit file already exists so standalone installs stay lean.
COLLEAGUE_GATEWAY_UNITS=(
  openclaw-gateway.service
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

# Capture find output first so a failing find exits the script (installer
# contract; pipefail propagates find's exact exit code through `|| exit $?`).
# Output is newline-delimited on purpose: systemd unit file names cannot contain
# newlines, and command substitution would silently strip the NUL bytes that
# `-print0` emits, collapsing every path into one unreadable record.
_service_paths="$(find "$USER_SYSTEMD_DIR" -maxdepth 1 -type f -name '*.service' | sort)" || exit $?
if [ -n "${_service_paths}" ]; then
  while IFS= read -r unit_path; do
    [ -n "$unit_path" ] || continue
    unit="$(basename "$unit_path")"
    if grep -Fq '/opt/eimemory/current' "$unit_path"; then
      emit_once "$unit"
    else
      grep_status="$?"
      if [ "$grep_status" -gt 1 ]; then
        exit "$grep_status"
      fi
    fi
  done <<< "$_service_paths"
fi

_dropin_dirs="$(find "$USER_SYSTEMD_DIR" -maxdepth 1 -type d -name '*.service.d' | sort)" || exit $?
if [ -n "$_dropin_dirs" ]; then
  while IFS= read -r dropin_dir; do
    [ -n "$dropin_dir" ] || continue
    unit="$(basename "$dropin_dir" .d)"
    if [[ ! "$unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
      continue
    fi
    # A storage start guard is not a runtime identity binding. Guards can
    # exist for optional, absent writers and use the system interpreter.
    if find "$dropin_dir" -maxdepth 1 -type f \( -name '*eimemory*' -o -name '*python-runtime*' \) ! -name '05-eimemory-storage-release-guard.conf' -print -quit | grep -q .; then
      # A leftover identity drop-in does not constitute a service. Preserve
      # vendor/generated units only when systemd confirms they are loaded;
      # query failures must remain fatal, not be treated as absence.
      if [ ! -f "$USER_SYSTEMD_DIR/$unit" ]; then
        load_state="$(systemctl --user show "$unit" --property=LoadState --value)" || exit $?
        case "$load_state" in
          loaded) ;;
          not-found) continue ;;
          *) printf 'runtime discovery: invalid load state for %s: %s\n' "$unit" "$load_state" >&2; exit 2 ;;
        esac
      elif [ -L "$USER_SYSTEMD_DIR/$unit" ]; then
        continue
      fi
      emit_once "$unit"
    fi
  done <<< "$_dropin_dirs"
fi

_gateway_paths="$(find "$USER_SYSTEMD_DIR" -maxdepth 1 -type f -name '*-gateway.service' | sort)" || exit $?
if [ -n "$_gateway_paths" ]; then
  while IFS= read -r unit_path; do
    [ -n "$unit_path" ] || continue
    emit_once "$(basename "$unit_path")"
  done <<< "$_gateway_paths"
fi
