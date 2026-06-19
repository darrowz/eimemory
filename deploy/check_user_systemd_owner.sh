#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${HEALTH_URL:-http://100.105.189.120:8091/health}"
LOOPBACK_HEALTH_URL="${LOOPBACK_HEALTH_URL:-http://127.0.0.1:8091/health}"

_fail() {
  echo "ok=false"
  echo "reason=$1"
  exit 1
}

system_owner_active="$(systemctl is-active eimemory-rpc.service 2>/dev/null || true)"
system_owner_enabled="$(systemctl is-enabled eimemory-rpc.service 2>/dev/null || true)"
user_owner_active="$(systemctl --user is-active eimemory-rpc.service 2>/dev/null || true)"
user_owner_enabled="$(systemctl --user is-enabled eimemory-rpc.service 2>/dev/null || true)"

echo "system_owner_active=${system_owner_active:-unknown}"
echo "system_owner_enabled=${system_owner_enabled:-unknown}"
echo "user_owner_active=${user_owner_active:-unknown}"
echo "user_owner_enabled=${user_owner_enabled:-unknown}"

[ "$system_owner_active" != "active" ] || _fail "system_rpc_service_active"
[ "$system_owner_enabled" != "enabled" ] || _fail "system_rpc_service_enabled"
[ "$user_owner_active" = "active" ] || _fail "user_rpc_service_not_active"
[ "$user_owner_enabled" = "enabled" ] || _fail "user_rpc_service_not_enabled"

if command -v curl >/dev/null 2>&1; then
  curl -fsS "$LOOPBACK_HEALTH_URL" >/dev/null || _fail "loopback_health_failed"
  curl -fsS "$HEALTH_URL" >/dev/null || _fail "primary_health_failed"
fi

echo "ok=user_systemd_owner"
