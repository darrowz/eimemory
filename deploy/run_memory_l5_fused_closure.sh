#!/bin/bash
set -euo pipefail
export HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
export EIMEMORY_ROOT=/var/lib/eimemory
export EIMEMORY_CONFIG_DIR=/etc/eimemory
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX=/var/lib/eimemory-manager/empty-pycache
CURRENT="${EIMEMORY_DEPLOYMENT_CURRENT_LINK:-/opt/eimemory/current}"
BIN="${CURRENT}/.venv/bin/python"
REPO="${EIMEMORY_TRUSTED_REPOSITORY_ROOT:-${EIMEMORY_DEPLOYMENT_REPO_ROOT:-}}"
HEALTH="${EIMEMORY_DEPLOYMENT_HEALTH_URL:-http://127.0.0.1:8091/health}"
SCOPE_AGENT="${EIMEMORY_DEPLOY_SCOPE_AGENT:-${EIMEMORY_AGENT_ID:-main}}"
SCOPE_WORKSPACE="${EIMEMORY_DEPLOY_SCOPE_WORKSPACE:-${EIMEMORY_WORKSPACE_ID:-default}}"
SCOPE_USER="${EIMEMORY_DEPLOY_SCOPE_USER:-${EIMEMORY_USER_ID:-$(id -un)}}"
PRIOR="${EIMEMORY_PRIOR_COMMIT:-}"
LOG=${EIMEMORY_L5_FUSED_LOG:-${HOME}/.hermes/logs/eimemory-l5-fused-closure.log}
STATUS_JSON=${EIMEMORY_L5_QUERY_STATUS_JSON:-${HOME}/.hermes/logs/eimemory-l5-production-query-status.json}
export EIMEMORY_L5_QUERY_STATUS_JSON="$STATUS_JSON"

mkdir -p "$(dirname "$LOG")" "$(dirname "$STATUS_JSON")"
exec >>"$LOG" 2>&1
echo "fused_closure_start=$(date -Iseconds)"

if [ -z "$REPO" ]; then
  echo "error=repository_root_unset"
  exit 2
fi

if ! printf '%s' "$PRIOR" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "error=prior_commit_required"
  exit 2
fi

if [ ! -d "$REPO" ] || [ ! -x "$BIN" ]; then
  echo "error=closure_runtime_unavailable"
  exit 2
fi

# Capture both resources before changing either. An invocation with invalid
# configuration or an unreadable state must not start a previously idle timer.
L1_UNITS=(eimemory-l1-extract.timer eimemory-l1-extract.service)
L1_STATES=()
L1_PAUSED=()
for unit in "${L1_UNITS[@]}"; do
  if ! state="$(systemctl --user show --property=ActiveState --value "$unit")"; then
    echo "error=l1_state_unavailable unit=$unit"
    exit 2
  fi
  case "$state" in
    active|activating|reloading|inactive|failed) L1_STATES+=("$state") ;;
    *) echo "error=l1_state_unavailable unit=$unit"; exit 2 ;;
  esac
done

restore_l1() {
  local exit_status=$? restore_failed=0 index
  trap - EXIT
  # Restore the service before its timer. A successful stop is owned even if
  # its following state probe fails; do not strand an originally active unit.
  for ((index=${#L1_PAUSED[@]}-1; index>=0; index--)); do
    if ! systemctl --user start "${L1_PAUSED[index]}"; then
      echo "error=l1_restore_failed unit=${L1_PAUSED[index]}"
      restore_failed=1
    fi
  done
  if [ "$restore_failed" = 1 ] && [ "$exit_status" = 0 ]; then
    exit_status=2
  fi
  exit "$exit_status"
}
trap restore_l1 EXIT

for index in "${!L1_UNITS[@]}"; do
  unit="${L1_UNITS[index]}"
  stop_status=0
  systemctl --user stop "$unit" || stop_status=$?
  state="$(systemctl --user show --property=ActiveState --value "$unit")" || state=unknown
  if [ "$stop_status" = 0 ] || [ "$state" = inactive ]; then
    case "${L1_STATES[index]}" in
      active|activating|reloading) L1_PAUSED+=("$unit") ;;
    esac
  fi
  if [ "$stop_status" != 0 ] || [ "$state" != inactive ]; then
    echo "error=l1_stop_failed unit=$unit"
    exit 2
  fi
done

echo "stage=memory_plane_eval"
EIMEMORY_L1_WORKER_ACTION=eval "$BIN" -m eimemory.cli.l1_worker
echo "stage=memory_plane_eval ok"

echo "stage=production_query_collect"
"$BIN" -m eimemory.cli.main eval production-query collect \
  --scope-agent "$SCOPE_AGENT" \
  --scope-workspace "$SCOPE_WORKSPACE" \
  --scope-user "$SCOPE_USER" \
  --limit 80
echo "stage=production_query_collect ok"

echo "stage=production_query_status"
"$BIN" -m eimemory.cli.main eval production-query status \
  --scope-agent "$SCOPE_AGENT" \
  --scope-workspace "$SCOPE_WORKSPACE" \
  --scope-user "$SCOPE_USER" | tee "$STATUS_JSON"
echo "stage=production_query_status ok"

ready="$("$BIN" -c 'import json, os; from pathlib import Path; obj=json.loads(Path(os.environ["EIMEMORY_L5_QUERY_STATUS_JSON"]).read_text(encoding="utf-8")); print("1" if obj.get("ok") is True and obj.get("ready") is True else "0")')"
if [ "$ready" != "1" ]; then
  echo "blocked_stage=production_query_dataset"
  echo "blocked_reason=production_query_not_ready"
  echo "fused_closure_end=$(date -Iseconds)"
  exit 2
fi

echo "stage=release_closure"
"$BIN" -m eimemory.cli.main learn release-closure \
  --repo-root "$REPO" \
  --current-link "$CURRENT" \
  --health-url "$HEALTH" \
  --prior-commit "$PRIOR" \
  --scope-agent "$SCOPE_AGENT" \
  --scope-workspace "$SCOPE_WORKSPACE" \
  --scope-user "$SCOPE_USER" \
  --json
echo "stage=release_closure_finished=$(date -Iseconds)"
echo "fused_closure_end=$(date -Iseconds)"
