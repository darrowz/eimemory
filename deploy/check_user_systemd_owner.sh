#!/usr/bin/env bash
set -euo pipefail

# Canonical loopback health only. Tailscale/primary URLs are not required for
# owner proof after authenticated loopback RPC; requiring them caused false
# failures and curl write/pipe exits (e.g. 23) to poison outer ok=false.
LOOPBACK_HEALTH_URL="${LOOPBACK_HEALTH_URL:-http://127.0.0.1:8091/health}"
COLLECTOR="${EIMEMORY_HEALTH_COLLECTOR:-}"
if [ -z "$COLLECTOR" ]; then
  COLLECTOR="$(cd "$(dirname "$0")" && pwd)/collect_release_health.py"
fi

_fail() {
  echo "ok=false"
  echo "reason=$1"
  exit 1
}

# Compatibility command name; ownership is now declared in the protected
# registry and the host bridge verifies the inactive old user owner as well.
CONTROL="$(cd "$(dirname "$0")" && pwd)/runtime_manager_control.py"
if ! python3 -I -B "$CONTROL" single-owner; then
  _fail "registered_rpc_owner_not_unique"
fi

[ -f "$COLLECTOR" ] || _fail "health_collector_unavailable"
HEALTH_PYTHON="${EIMEMORY_HEALTH_PYTHON:-python3}"
command -v "$HEALTH_PYTHON" >/dev/null 2>&1 || _fail "health_python_unavailable"
# The collector's verifier binds its own release root under -I; never drop -I.
if ! "$HEALTH_PYTHON" -I -B "$COLLECTOR" --url "$LOOPBACK_HEALTH_URL" --probe-only >/dev/null; then
  _fail "loopback_health_failed"
fi

echo "ok=registered_systemd_owner"
