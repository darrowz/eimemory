# shellcheck shell=bash
# Resolve the deploy scope without site-specific literals in callers.
# Precedence: EIMEMORY_DEPLOY_SCOPE_* env > settings.json "deploy_scope" > settings default_agent_id/
# default_workspace_id >
# the legacy compatibility labels below (kept only so existing installs that
# never configured a scope keep their data location; configure deploy_scope
# or the env vars on any new installation).
_eimemory_deploy_scope_setting() {
  local key="$1" settings="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}/settings.json"
  [ -f "$settings" ] || return 0
  /usr/bin/env python3 - "$settings" "$key" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    value = (data.get("deploy_scope") or {}).get(sys.argv[2], "")
    if not value and sys.argv[2] in {"agent_id", "workspace_id"}:
        value = data.get("default_" + sys.argv[2], "")
except Exception:
    value = ""
if isinstance(value, str) and value.strip():
    print(value.strip())
PY
}
eimemory_default_deploy_scope_agent() {
  local value; value="$(_eimemory_deploy_scope_setting agent_id)"
  printf '%s' "${value:-hongtu}"  # legacy compatibility label
}
eimemory_default_deploy_scope_workspace() {
  local value; value="$(_eimemory_deploy_scope_setting workspace_id)"
  printf '%s' "${value:-embodied}"  # legacy compatibility label
}
eimemory_default_deploy_scope_user() {
  local value; value="$(_eimemory_deploy_scope_setting user_id)"
  printf '%s' "${value:-$1}"
}

eimemory_default_deploy_scope_tenant() {
  local value; value="$(_eimemory_deploy_scope_setting tenant_id)"
  printf '%s' "${value:-default}"
}
