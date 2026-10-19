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
SCOPE_TENANT="${EIMEMORY_DEPLOY_SCOPE_TENANT:-${EIMEMORY_TENANT_ID:-default}}"
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

# Root selects the fixed L1 pair and saves its original active set. A runtime
# shell has no generic unit-control permission and cannot choose another owner.
CONTROL="$CURRENT/deploy/runtime_manager_control.py"
CACHE=/var/lib/eimemory-manager/empty-pycache
mapfile -t coordinates < <("$BIN" -I -B -X "pycache_prefix=$CACHE" "$CONTROL" fused-scope)
if [ "${#coordinates[@]}" != 4 ]; then
  echo "error=fused_owner_scope_unavailable"
  exit 2
fi
SCOPE_TENANT="${coordinates[0]}"
SCOPE_AGENT="${coordinates[1]}"
SCOPE_WORKSPACE="${coordinates[2]}"
SCOPE_USER="${coordinates[3]}"
context_key="$("$BIN" -I -B -X "pycache_prefix=$CACHE" "$CONTROL" fused-begin)" || {
  echo "error=l1_managed_pause_failed"
  exit 2
}
if ! printf '%s' "$context_key" | grep -Eq '^[0-9a-f]{64}$'; then
  echo "error=l1_managed_context_invalid"
  exit 2
fi
restore_l1() {
  local exit_status=$?
  trap - EXIT
  if ! "$BIN" -I -B -X "pycache_prefix=$CACHE" "$CONTROL" fused-end "$context_key"; then
    echo "error=l1_managed_restore_failed"
    if [ "$exit_status" = 0 ]; then exit_status=2; fi
  fi
  exit "$exit_status"
}
trap restore_l1 EXIT
export EIMEMORY_DEPLOY_SCOPE_TENANT="$SCOPE_TENANT"
export EIMEMORY_DEPLOY_SCOPE_AGENT="$SCOPE_AGENT"
export EIMEMORY_DEPLOY_SCOPE_WORKSPACE="$SCOPE_WORKSPACE"
export EIMEMORY_DEPLOY_SCOPE_USER="$SCOPE_USER"

echo "stage=memory_plane_eval"
EIMEMORY_L1_WORKER_ACTION=eval "$BIN" -I -B -X "pycache_prefix=$CACHE" /usr/libexec/eimemory-manager/runtime_manager_launch.py --product-module eimemory.cli.l1_worker
echo "stage=memory_plane_eval ok"

echo "stage=production_query_collect"
"$BIN" -I -B -X "pycache_prefix=$CACHE" /usr/libexec/eimemory-manager/runtime_manager_launch.py --product-module eimemory.cli.main eval production-query collect \
  --scope-tenant "$SCOPE_TENANT" \
  --scope-agent "$SCOPE_AGENT" \
  --scope-workspace "$SCOPE_WORKSPACE" \
  --scope-user "$SCOPE_USER" \
  --limit 80
echo "stage=production_query_collect ok"

echo "stage=production_query_status"
"$BIN" -I -B -X "pycache_prefix=$CACHE" /usr/libexec/eimemory-manager/runtime_manager_launch.py --product-module eimemory.cli.main eval production-query status \
  --scope-tenant "$SCOPE_TENANT" \
  --scope-agent "$SCOPE_AGENT" \
  --scope-workspace "$SCOPE_WORKSPACE" \
  --scope-user "$SCOPE_USER" | tee "$STATUS_JSON"
echo "stage=production_query_status ok"

ready="$("$BIN" -I -B -X "pycache_prefix=$CACHE" -c 'import json, os; from pathlib import Path; obj=json.loads(Path(os.environ["EIMEMORY_L5_QUERY_STATUS_JSON"]).read_text(encoding="utf-8")); print("1" if obj.get("ok") is True and obj.get("ready") is True else "0")')"
if [ "$ready" != "1" ]; then
  echo "blocked_stage=production_query_dataset"
  echo "blocked_reason=production_query_not_ready"
  echo "fused_closure_end=$(date -Iseconds)"
  exit 2
fi

echo "stage=release_closure"
"$BIN" -I -B -X "pycache_prefix=$CACHE" /usr/libexec/eimemory-manager/runtime_manager_launch.py --product-module eimemory.cli.main learn release-closure \
  --repo-root "$REPO" \
  --current-link "$CURRENT" \
  --health-url "$HEALTH" \
  --prior-commit "$PRIOR" \
  --scope-tenant "$SCOPE_TENANT" \
  --scope-agent "$SCOPE_AGENT" \
  --scope-workspace "$SCOPE_WORKSPACE" \
  --scope-user "$SCOPE_USER" \
  --json
echo "stage=release_closure_finished=$(date -Iseconds)"
echo "fused_closure_end=$(date -Iseconds)"
