#!/bin/bash
set -euo pipefail
export HOME=/home/darrow
export EIMEMORY_ROOT=/var/lib/eimemory
export EIMEMORY_CONFIG_DIR=/etc/eimemory
export PYTHONDONTWRITEBYTECODE=1
BIN=/opt/eimemory/current/.venv/bin/python
CURRENT=/opt/eimemory/current
REPO=/dev-project/eimemory
HEALTH=http://127.0.0.1:8091/health
PRIOR="${EIMEMORY_PRIOR_COMMIT:-}"
LOG=${EIMEMORY_L5_FUSED_LOG:-/home/darrow/.hermes/logs/eimemory-l5-fused-closure.log}
STATUS_JSON=${EIMEMORY_L5_QUERY_STATUS_JSON:-/home/darrow/.hermes/logs/eimemory-l5-production-query-status.json}
export EIMEMORY_L5_QUERY_STATUS_JSON="$STATUS_JSON"

mkdir -p "$(dirname "$LOG")" "$(dirname "$STATUS_JSON")"
exec >>"$LOG" 2>&1
echo "fused_closure_start=$(date -Iseconds)"

restart_l1() {
  systemctl --user start eimemory-l1-extract.timer 2>/dev/null || true
}
trap restart_l1 EXIT

if ! printf '%s' "$PRIOR" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "error=prior_commit_required"
  exit 2
fi

systemctl --user stop eimemory-l1-extract.timer eimemory-l1-extract.service 2>/dev/null || true

echo "stage=memory_plane_eval"
EIMEMORY_L1_WORKER_ACTION=eval "$BIN" -m eimemory.cli.l1_worker
echo "stage=memory_plane_eval ok"

echo "stage=production_query_collect"
"$BIN" -m eimemory.cli.main eval production-query collect \
  --scope-agent hongtu \
  --scope-workspace embodied \
  --scope-user darrow \
  --limit 80
echo "stage=production_query_collect ok"

echo "stage=production_query_status"
"$BIN" -m eimemory.cli.main eval production-query status \
  --scope-agent hongtu \
  --scope-workspace embodied \
  --scope-user darrow | tee "$STATUS_JSON"
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
  --scope-agent hongtu \
  --scope-workspace embodied \
  --scope-user darrow \
  --json
echo "stage=release_closure_finished=$(date -Iseconds)"
echo "fused_closure_end=$(date -Iseconds)"
