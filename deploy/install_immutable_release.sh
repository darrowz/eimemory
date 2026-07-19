#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

REPO_DIR="${REPO_DIR:-/dev-project/eimemory}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/eimemory}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
SERVICE_USER="${SERVICE_USER:-darrow}"
SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
SERVICE_HOME="${SERVICE_HOME:-/home/$SERVICE_USER}"
EIMEMORY_ROOT="${EIMEMORY_ROOT:-/var/lib/eimemory}"
EIMEMORY_CONFIG_DIR="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}"
EIMEMORY_LOG_DIR="${EIMEMORY_LOG_DIR:-$SERVICE_HOME/.openclaw/logs}"
GOVERNANCE_ENV_FILE="${EIMEMORY_GOVERNANCE_ENV_FILE:-$EIMEMORY_CONFIG_DIR/governance.env}"
EVIDENCE_RECEIPT_ENV_FILE="${EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE:-$EIMEMORY_CONFIG_DIR/evidence-receipt.env}"
USER_SYSTEMD_ENABLE_SERVICE="${USER_SYSTEMD_ENABLE_SERVICE:-1}"
USER_SYSTEMD_DIR="${USER_SYSTEMD_DIR:-$SERVICE_HOME/.config/systemd/user}"
SYSTEM_RPC_UNIT_PATH="${SYSTEM_RPC_UNIT_PATH:-/etc/systemd/system/eimemory-rpc.service}"
OPENCLAW_LOOP_DEPLOY_VERIFY="${OPENCLAW_LOOP_DEPLOY_VERIFY:-1}"
OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS="${OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS:-0}"
OPENCLAW_LOOP_CONFIG_PATH="${OPENCLAW_LOOP_CONFIG_PATH:-$SERVICE_HOME/.openclaw/openclaw.json}"
OPENCLAW_LOOP_COMPAT_SCRIPT="${OPENCLAW_LOOP_COMPAT_SCRIPT-$SERVICE_HOME/.openclaw/workspace/scripts/openclaw_loop.py}"
OPENCLAW_BIN="${OPENCLAW_BIN:-$SERVICE_HOME/n/bin/openclaw}"
EIMEMORY_POST_SWITCH_GATES="${EIMEMORY_POST_SWITCH_GATES:-1}"
EIMEMORY_HEALTH_URL="${EIMEMORY_HEALTH_URL:-http://127.0.0.1:8091/health}"
EIMEMORY_DEPLOY_SCOPE_AGENT="${EIMEMORY_DEPLOY_SCOPE_AGENT:-hongtu}"
EIMEMORY_DEPLOY_SCOPE_WORKSPACE="${EIMEMORY_DEPLOY_SCOPE_WORKSPACE:-embodied}"
EIMEMORY_DEPLOY_SCOPE_USER="${EIMEMORY_DEPLOY_SCOPE_USER:-darrow}"

_require_nonblank_deploy_scope() {
  case "$1" in
    *[![:space:]]*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$EIMEMORY_POST_SWITCH_GATES" = "1" ]; then
  if ! _require_nonblank_deploy_scope "$EIMEMORY_DEPLOY_SCOPE_AGENT" || \
     ! _require_nonblank_deploy_scope "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" || \
     ! _require_nonblank_deploy_scope "$EIMEMORY_DEPLOY_SCOPE_USER"; then
    echo "Deployment scope triple must be non-blank (agent/workspace/user)." >&2
    exit 2
  fi
fi
EIMEMORY_DEPLOY_FAIL_STAGE="${EIMEMORY_DEPLOY_FAIL_STAGE:-}"
COMMIT="${1:-$(git -C "$REPO_DIR" rev-parse HEAD)}"
RELEASE_DIR="$INSTALL_ROOT/releases/$COMMIT"
CURRENT_LINK="$INSTALL_ROOT/current"

if [[ "$PYTHON_BIN" != /* ]]; then
  echo "PYTHON_BIN must be an absolute trusted interpreter path" >&2
  exit 2
fi
if ! PYTHON_BIN="$(realpath -e -- "$PYTHON_BIN")" || [ ! -x "$PYTHON_BIN" ]; then
  echo "Unable to resolve trusted Python interpreter: $PYTHON_BIN" >&2
  exit 2
fi

_ensure_runtime_dir() {
  local path="$1"
  local mode="${2:-0750}"
  if mkdir -p "$path" 2>/dev/null; then
    chmod "$mode" "$path" 2>/dev/null || true
    if [ "$(id -u)" -eq 0 ]; then
      if id "$SERVICE_USER" >/dev/null 2>&1; then
        chown -R "$SERVICE_USER:$SERVICE_GROUP" "$path"
      else
        echo "warning: service user not found for ownership: $SERVICE_USER" >&2
      fi
    fi
  else
    echo "warning: unable to create runtime directory: $path" >&2
  fi
}

_run_as_service_user() {
  if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    if ! command -v runuser >/dev/null 2>&1; then
      echo "runuser is required for root deployment into service-user paths" >&2
      return 2
    fi
    runuser -u "$SERVICE_USER" -- "$@"
  else
    "$@"
  fi
}

_install_as_service_user() {
  local mode="$1"
  local source="$2"
  local target="$3"
  if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    local staged_source
    staged_source="$(mktemp)"
    if ! install -m "$mode" "$source" "$staged_source" || \
       ! chown "$SERVICE_USER:$SERVICE_GROUP" "$staged_source" || \
       ! _run_as_service_user install -m "$mode" "$staged_source" "$target"; then
      rm -f "$staged_source"
      return 2
    fi
    rm -f "$staged_source"
  else
    install -m "$mode" "$source" "$target"
  fi
}

_clean_existing_release_and_validate_source() {
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
    --release-dir "$RELEASE_DIR" --releases-root "$INSTALL_ROOT/releases"
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
    --validate-source --release-dir "$RELEASE_DIR" \
    --releases-root "$INSTALL_ROOT/releases" --repo-root "$REPO_DIR" --commit "$COMMIT"
}

_retire_system_rpc_unit() {
  if [ "$(id -u)" -ne 0 ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  systemctl disable --now eimemory-rpc.service >/dev/null 2>&1 || true
  if [ -e "$SYSTEM_RPC_UNIT_PATH" ] || [ -L "$SYSTEM_RPC_UNIT_PATH" ]; then
    local retired_path="$SYSTEM_RPC_UNIT_PATH.retired-by-eimemory-user-systemd"
    mv -f "$SYSTEM_RPC_UNIT_PATH" "$retired_path"
    echo "retired_systemd_unit=$retired_path"
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
}

_run_openclaw_loop_deploy_verify() {
  if [ "$OPENCLAW_LOOP_DEPLOY_VERIFY" != "1" ]; then
    return
  fi
  local live_arg=(--no-live)
  if [ "$OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS" = "1" ]; then
    live_arg=()
  fi
  local config_arg=()
  local target_release="${1:-$RELEASE_DIR}"
  if [ -n "$OPENCLAW_LOOP_CONFIG_PATH" ] && [ -f "$OPENCLAW_LOOP_CONFIG_PATH" ]; then
    config_arg=(--config "$OPENCLAW_LOOP_CONFIG_PATH")
  fi
  "$target_release/.venv/bin/python" "$target_release/scripts/openclaw_loop.py" deploy-verify \
    --commit "$COMMIT" \
    --release-path "$target_release" \
    "${config_arg[@]}" \
    "${live_arg[@]}"
}

_install_openclaw_loop_compat_script() {
  local target_release="${1:-$RELEASE_DIR}"
  if [ -z "$OPENCLAW_LOOP_COMPAT_SCRIPT" ]; then
    return
  fi
  local compat_dir
  compat_dir="$(dirname "$OPENCLAW_LOOP_COMPAT_SCRIPT")"
  _run_as_service_user mkdir -p "$compat_dir"
  chmod +x "$target_release/scripts/openclaw_loop.py" 2>/dev/null || true
  _run_as_service_user rm -f "$OPENCLAW_LOOP_COMPAT_SCRIPT"
  _install_as_service_user 0755 \
    "$target_release/scripts/openclaw_loop.py" "$OPENCLAW_LOOP_COMPAT_SCRIPT"
}

_refresh_openclaw_plugin_registry() {
  if [ ! -x "$OPENCLAW_BIN" ]; then
    echo "openclaw_plugin_registry_refresh=skipped binary_not_found" >&2
    return
  fi
  _run_as_service_user env HOME="$SERVICE_HOME" \
    "$OPENCLAW_BIN" plugins registry --refresh --json >/dev/null
}

_user_systemctl() {
  if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    local service_uid
    service_uid="$(id -u "$SERVICE_USER")"
    _run_as_service_user env \
      XDG_RUNTIME_DIR="/run/user/$service_uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$service_uid/bus" \
      systemctl --user "$@"
  else
    systemctl --user "$@"
  fi
}

_maybe_fail_stage() {
  local stage="$1"
  if [ -n "$EIMEMORY_DEPLOY_FAIL_STAGE" ] && [ "$EIMEMORY_DEPLOY_FAIL_STAGE" = "$stage" ]; then
    echo "injected_post_switch_failure=$stage" >&2
    return 97
  fi
}

_release_version() {
  local target_release="$1"
  "$PYTHON_BIN" -I -B -c \
    'import pathlib, sys, tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]["version"])' \
    "$target_release/pyproject.toml"
}

_inspect_openclaw_plugin_runtime() {
  local target_release="${1:-$RELEASE_DIR}"
  local verifier_release="${2:-$target_release}"
  local allow_legacy_runtime="${3:-0}"
  if [ ! -x "$OPENCLAW_BIN" ]; then
    echo "openclaw_plugin_runtime_inspect=skipped binary_not_found" >&2
    return
  fi
  local inspect_json
  local legacy_arg=()
  if [ "$allow_legacy_runtime" = "1" ]; then
    legacy_arg=(--allow-legacy-runtime)
  fi
  inspect_json="$(_run_as_service_user env HOME="$SERVICE_HOME" \
    "$OPENCLAW_BIN" plugins inspect eimemory-bridge --runtime --json)"
  printf '%s' "$inspect_json" | \
    "$PYTHON_BIN" -I -B "$verifier_release/deploy/verify_openclaw_plugin_runtime.py" \
      --expected-root "$target_release/integrations/openclaw/eimemory-bridge" \
      "${legacy_arg[@]}"
}

_refresh_current_runtime_metadata() {
  local target_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  local metadata_release="${3:-$target_release}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
  SERVICE_UID="$(id -u "$SERVICE_USER" 2>/dev/null || id -u)"
  if ! PYTHON_RUNTIME_UNIT_OUTPUT="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" < "$target_release/deploy/discover_python_runtime_units.sh")"; then
    echo "Unable to discover Python runtime systemd units" >&2
    return 2
  fi
  mapfile -t PYTHON_RUNTIME_UNITS <<< "$PYTHON_RUNTIME_UNIT_OUTPUT"
  for runtime_unit in "${PYTHON_RUNTIME_UNITS[@]}"; do
    _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/$runtime_unit.d"
    "$PYTHON_BIN" -I -B "$metadata_release/deploy/install_managed_systemd_dropin.py" \
      --source "$metadata_release/deploy/systemd/eimemory-python-runtime.conf" \
      --target "$USER_SYSTEMD_DIR/$runtime_unit.d/90-eimemory-python-runtime.conf" \
      --root "$USER_SYSTEMD_DIR" --owner-uid "$SERVICE_UID" --render-commit "$target_commit" \
      --render-evidence-receipt-env-file "$EVIDENCE_RECEIPT_ENV_FILE"
  done
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"
  _user_systemctl daemon-reload
  _user_systemctl enable eimemory-rpc.service
  _user_systemctl restart eimemory-rpc.service
}

_refresh_openclaw_gateway_metadata() {
  local metadata_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/openclaw-gateway.service.d"
  local service_uid
  service_uid="$(id -u "$SERVICE_USER" 2>/dev/null || id -u)"
  "$PYTHON_BIN" -I -B "$metadata_release/deploy/install_managed_systemd_dropin.py" \
    --source "$metadata_release/deploy/systemd/openclaw-gateway-eimemory.conf" \
    --target "$USER_SYSTEMD_DIR/openclaw-gateway.service.d/90-eimemory-runtime.conf" \
    --root "$USER_SYSTEMD_DIR" --owner-uid "$service_uid" --render-commit "$target_commit" \
    --render-evidence-receipt-env-file "$EVIDENCE_RECEIPT_ENV_FILE"
}

_provision_evidence_receipt_key() {
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/ensure_evidence_receipt_key.py" \
    --path "$EVIDENCE_RECEIPT_ENV_FILE" \
    --user "$SERVICE_USER" \
    --group "$SERVICE_GROUP"
}

_find_prior_release_commit() {
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/find_prior_immutable_release.py" \
    --releases-root "$INSTALL_ROOT/releases" \
    --repo-root "$REPO_DIR" \
    --deployed-commit "$COMMIT" \
    --runtime-root "$EIMEMORY_ROOT" \
    --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
    --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
    --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER"
}

_restart_current_services() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _user_systemctl daemon-reload
  _user_systemctl restart eimemory-rpc.service
  _user_systemctl restart openclaw-feishu-reply-watchdog.service
  _user_systemctl restart openclaw-gateway.service
}

_verify_release_health() {
  local target_release="$1"
  local target_commit="$2"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  local target_version
  target_version="$(_release_version "$target_release")"
  local verifier="$target_release/deploy/verify_release_health.py"
  if [ ! -f "$verifier" ]; then
    verifier="$REPO_DIR/deploy/verify_release_health.py"
  fi
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$target_release/.venv/bin/python" -I -B \
      "$verifier" \
      --url "$EIMEMORY_HEALTH_URL" --commit "$target_commit" \
      --version "$target_version" --release-dir "$target_release"; then
      return
    fi
    sleep 1
  done
  return 2
}

_record_deployment_receipt() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$RELEASE_DIR/.venv/bin/python" -I -B "$REPO_DIR/deploy/record_deployment_receipt.py" \
      --repo-root "$REPO_DIR" --current-link "$CURRENT_LINK" \
      --health-url "$EIMEMORY_HEALTH_URL" --prior-commit "$PREVIOUS_COMMIT" \
      --deployed-commit "$COMMIT" \
      --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
      --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
      --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER" --json
}

_run_post_switch_closure() {
  if [ "$EIMEMORY_POST_SWITCH_GATES" != "1" ] || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  local closure_output closure_status summary_status
  closure_output="$(mktemp "$INSTALL_ROOT/.release-closure-${COMMIT}-XXXXXXXX.json")"
  chmod 0600 "$closure_output"
  if env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/run_with_governance_env.py" \
      --env-file "$GOVERNANCE_ENV_FILE" --optional -- \
      "$RELEASE_DIR/.venv/bin/eimemory" learn release-closure \
        --repo-root "$REPO_DIR" --current-link "$CURRENT_LINK" \
        --health-url "$EIMEMORY_HEALTH_URL" --prior-commit "$PREVIOUS_COMMIT" \
        --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
        --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
        --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER" --json \
        >"$closure_output"; then
    closure_status=0
  else
    closure_status=$?
  fi
  if "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/summarize_release_closure.py" \
    --path "$closure_output"; then
    summary_status=0
  else
    summary_status=$?
  fi
  rm -f "$closure_output"
  if [ "$summary_status" != "0" ]; then
    return "$summary_status"
  fi
  return "$closure_status"
}

_rollback_current_release() {
  local rollback_failed=0
  if ! rm -f "$CURRENT_LINK.next" 2>/dev/null; then
    echo "rollback_step=cleanup_next status=failed" >&2
    rollback_failed=1
  fi
  if [ -z "${PREVIOUS_CURRENT:-}" ] || [ ! -d "$PREVIOUS_CURRENT" ]; then
    echo "rollback_current_release=unavailable_no_previous" >&2
    echo "rollback_preserved_failed_release=$RELEASE_DIR" >&2
    return 1
  fi
  if [ "${EIMEMORY_DEPLOY_FAIL_ROLLBACK_STAGE:-}" = "link" ] || \
     ! { ln -sfn "$PREVIOUS_CURRENT" "$CURRENT_LINK.next" && mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"; }; then
    echo "rollback_step=restore_link status=failed" >&2
    echo "rollback_current_release=failed" >&2
    return 1
  fi
  if ! _refresh_openclaw_gateway_metadata "$REPO_DIR" "$PREVIOUS_COMMIT"; then
    echo "rollback_step=gateway_metadata status=failed" >&2
    rollback_failed=1
  fi
  if ! _refresh_current_runtime_metadata "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT" "$REPO_DIR"; then
    echo "rollback_step=runtime_metadata status=failed" >&2
    rollback_failed=1
  fi
  if ! _install_openclaw_loop_compat_script "$PREVIOUS_CURRENT"; then
    echo "rollback_step=compat_script status=failed" >&2
    rollback_failed=1
  fi
  if ! _refresh_openclaw_plugin_registry; then
    echo "rollback_step=plugin_registry status=failed" >&2
    rollback_failed=1
  fi
  if ! _restart_current_services; then
    echo "rollback_step=services status=failed" >&2
    rollback_failed=1
  fi
  if ! _inspect_openclaw_plugin_runtime "$PREVIOUS_CURRENT" "$RELEASE_DIR" "1"; then
    echo "rollback_step=plugin_runtime status=failed" >&2
    rollback_failed=1
  fi
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ]; then
    if ! _verify_release_health "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT"; then
      echo "rollback_step=previous_health status=failed" >&2
      rollback_failed=1
    fi
  fi
  if [ "$rollback_failed" != "0" ]; then
    echo "rollback_current_release=failed" >&2
    return 1
  fi
  echo "rollback_current_release=restored target=$PREVIOUS_CURRENT" >&2
}

if [[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Commit must be a full 40-character SHA: $COMMIT" >&2
  exit 2
fi

if ! git -C "$REPO_DIR" rev-parse --verify "$COMMIT^{commit}" >/dev/null 2>&1; then
  echo "Unknown commit: $COMMIT" >&2
  exit 2
fi
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && \
   [ -n "$(git -C "$REPO_DIR" status --porcelain --untracked-files=all)" ]; then
  echo "Authoritative deployment repository must be clean" >&2
  exit 2
fi

mkdir -p "$INSTALL_ROOT/releases"
if [ -L "$INSTALL_ROOT/releases" ] || [ -L "$RELEASE_DIR" ]; then
  echo "Unsafe symlink in immutable release path" >&2
  exit 2
fi
if [ "$(stat -c %u "$INSTALL_ROOT/releases")" != "$(id -u)" ]; then
  echo "Immutable releases root must be owned by the deployment user" >&2
  exit 2
fi
chmod 0700 "$INSTALL_ROOT/releases"

PREVIOUS_CURRENT=""
PREVIOUS_COMMIT=""
if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ] || [ -d "$CURRENT_LINK" ]; then
  if ! PREVIOUS_CURRENT="$(realpath -e -- "$CURRENT_LINK" 2>/dev/null)"; then
    echo "Current release link is dangling or unresolvable: $CURRENT_LINK" >&2
    exit 2
  fi
  PREVIOUS_COMMIT="$(basename "$PREVIOUS_CURRENT")"
fi

# Threat boundary: the deployment UID and its same-UID processes are trusted.
# This transaction rejects pre-existing links, other-UID writes, and partial
# failures. A hostile same-UID process requires a separate deployment account.
if { [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ] || [ -d "$CURRENT_LINK" ]; } && \
   [[ -d "$RELEASE_DIR" && ! -L "$RELEASE_DIR" ]] && \
   "$PYTHON_BIN" -I -B -c \
   'from pathlib import Path; import sys; raise SystemExit(0 if Path(sys.argv[1]).resolve(strict=True) == Path(sys.argv[2]).resolve(strict=True) else 1)' \
  "$CURRENT_LINK" "$RELEASE_DIR"; then
  _clean_existing_release_and_validate_source
  if [ "$PREVIOUS_COMMIT" = "$COMMIT" ]; then
    PREVIOUS_COMMIT="$(_find_prior_release_commit)"
  fi
  _ensure_runtime_dir "$EIMEMORY_CONFIG_DIR" 0750
  _provision_evidence_receipt_key
  if [ -x "$OPENCLAW_BIN" ]; then
    "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/ensure_openclaw_bridge_config.py" \
      --path "$OPENCLAW_LOOP_CONFIG_PATH"
  fi
  _refresh_openclaw_gateway_metadata "$REPO_DIR" "$COMMIT"
  _refresh_current_runtime_metadata "$RELEASE_DIR" "$COMMIT" "$REPO_DIR"
  _restart_current_services
  _verify_release_health "$RELEASE_DIR" "$COMMIT"
  _record_deployment_receipt
  echo "release=$RELEASE_DIR"
  echo "current=$CURRENT_LINK"
  echo "commit=$COMMIT"
  echo "already_current=1"
  exit 0
fi

if [ -e "$RELEASE_DIR" ]; then
  _clean_existing_release_and_validate_source
fi

if [ "$EIMEMORY_POST_SWITCH_GATES" = "1" ] && [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && \
   [[ ! "$PREVIOUS_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Post-switch gates require a prior immutable release commit" >&2
  exit 2
fi

STAGE_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.eimemory-stage-${COMMIT}-XXXXXXXX")"
chmod 0700 "$STAGE_DIR"
BACKUP_DIR=""
FINAL_REPLACED=0
CURRENT_SWITCHED=0
COMMITTED=0
ROLLBACK_RESTORED=0
cleanup_stage() {
  local exit_code=$?
  trap - EXIT
  set +e
  if [ "$COMMITTED" != "1" ] && [ "$CURRENT_SWITCHED" = "1" ]; then
    if _rollback_current_release; then
      ROLLBACK_RESTORED=1
    else
      echo "rollback_preserved_failed_release=$RELEASE_DIR" >&2
    fi
  fi
  if [ "$COMMITTED" != "1" ] && [ "$FINAL_REPLACED" = "1" ] && \
     { [ "$CURRENT_SWITCHED" != "1" ] || [ "$ROLLBACK_RESTORED" = "1" ]; }; then
    FAILED_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.eimemory-stage-${COMMIT}-XXXXXXXX")"
    rmdir "$FAILED_DIR"
    mv -T "$RELEASE_DIR" "$FAILED_DIR" 2>/dev/null || true
    if [ -n "$BACKUP_DIR" ] && [ -e "$BACKUP_DIR" ]; then
      mv -T "$BACKUP_DIR" "$RELEASE_DIR" 2>/dev/null || true
    fi
    "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
      --remove-stage --release-dir "$FAILED_DIR" --releases-root "$INSTALL_ROOT/releases" || true
  fi
  if [ -n "${STAGE_DIR:-}" ] && [ -e "$STAGE_DIR" ]; then
    "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
      --remove-stage --release-dir "$STAGE_DIR" --releases-root "$INSTALL_ROOT/releases" || true
  fi
  exit "$exit_code"
}
trap cleanup_stage EXIT

git -C "$REPO_DIR" archive "$COMMIT" | tar -C "$STAGE_DIR" -xf -

"$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
  --validate-source --allow-stage \
  --release-dir "$STAGE_DIR" \
  --releases-root "$INSTALL_ROOT/releases" \
  --repo-root "$REPO_DIR" \
  --commit "$COMMIT"

"$PYTHON_BIN" -I -B -m venv --clear "$STAGE_DIR/.venv"

"$STAGE_DIR/.venv/bin/python" -I -B -m pip install --upgrade pip
"$STAGE_DIR/.venv/bin/python" -I -B -m pip install "$STAGE_DIR"

_run_openclaw_loop_deploy_verify "$STAGE_DIR"
PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
  --allow-stage --release-dir "$STAGE_DIR" --releases-root "$INSTALL_ROOT/releases"

if [ -e "$RELEASE_DIR" ]; then
  BACKUP_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.eimemory-backup-${COMMIT}-XXXXXXXX")"
  rmdir "$BACKUP_DIR"
  mv -T "$RELEASE_DIR" "$BACKUP_DIR"
fi
OLD_STAGE_PATH="$STAGE_DIR"
if ! mv -T "$STAGE_DIR" "$RELEASE_DIR"; then
  if [ -n "$BACKUP_DIR" ] && [ -e "$BACKUP_DIR" ]; then
    mv -T "$BACKUP_DIR" "$RELEASE_DIR"
  fi
  exit 2
fi
STAGE_DIR=""
FINAL_REPLACED=1

"$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
  --relocate-venv \
  --release-dir "$RELEASE_DIR" \
  --releases-root "$INSTALL_ROOT/releases" \
  --from-stage "$OLD_STAGE_PATH" \
  --to-release "$RELEASE_DIR"
for console_script in eimemory eimemory-qmd pip pip3; do
  if [ -f "$RELEASE_DIR/.venv/bin/$console_script" ] && \
     head -n 1 "$RELEASE_DIR/.venv/bin/$console_script" | grep -F "$OLD_STAGE_PATH" >/dev/null; then
    echo "Virtualenv script still references staging path: $console_script" >&2
    exit 2
  fi
done
"$RELEASE_DIR/.venv/bin/eimemory" --help >/dev/null

chmod 0755 "$INSTALL_ROOT" 2>/dev/null || true
_ensure_runtime_dir "$EIMEMORY_ROOT" 0750
_ensure_runtime_dir "$EIMEMORY_CONFIG_DIR" 0750
_ensure_runtime_dir "$EIMEMORY_LOG_DIR" 0750
"$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/ensure_rpc_auth.py" \
  --path "$EIMEMORY_CONFIG_DIR/rpc.env" \
  --user "$SERVICE_USER" \
  --group "$SERVICE_GROUP"
_provision_evidence_receipt_key
if [ -x "$OPENCLAW_BIN" ]; then
  "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/ensure_openclaw_bridge_config.py" \
    --path "$OPENCLAW_LOOP_CONFIG_PATH"
fi
_retire_system_rpc_unit
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/openclaw-gateway.service.d"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-loop-watch.service" "$USER_SYSTEMD_DIR/openclaw-loop-watch.service"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-loop-watch.timer" "$USER_SYSTEMD_DIR/openclaw-loop-watch.timer"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-loop-compact.service" "$USER_SYSTEMD_DIR/openclaw-loop-compact.service"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-loop-compact.timer" "$USER_SYSTEMD_DIR/openclaw-loop-compact.timer"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-stuck-watchdog.service" "$USER_SYSTEMD_DIR/openclaw-stuck-watchdog.service"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-stuck-watchdog.timer" "$USER_SYSTEMD_DIR/openclaw-stuck-watchdog.timer"
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/openclaw-feishu-reply-watchdog.service" "$USER_SYSTEMD_DIR/openclaw-feishu-reply-watchdog.service"
  SERVICE_UID="$(id -u "$SERVICE_USER" 2>/dev/null || id -u)"
  _refresh_openclaw_gateway_metadata "$RELEASE_DIR" "$COMMIT"
  if ! PYTHON_RUNTIME_UNIT_OUTPUT="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" < "$RELEASE_DIR/deploy/discover_python_runtime_units.sh")"; then
    echo "Unable to discover Python runtime systemd units" >&2
    exit 2
  fi
  mapfile -t PYTHON_RUNTIME_UNITS <<< "$PYTHON_RUNTIME_UNIT_OUTPUT"
  for runtime_unit in "${PYTHON_RUNTIME_UNITS[@]}"; do
    _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/$runtime_unit.d"
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/install_managed_systemd_dropin.py" \
      --source "$RELEASE_DIR/deploy/systemd/eimemory-python-runtime.conf" \
      --target "$USER_SYSTEMD_DIR/$runtime_unit.d/90-eimemory-python-runtime.conf" \
      --root "$USER_SYSTEMD_DIR" --owner-uid "$SERVICE_UID" --render-commit "$COMMIT" \
      --render-evidence-receipt-env-file "$EVIDENCE_RECEIPT_ENV_FILE"
  done
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"
  _user_systemctl daemon-reload
  _user_systemctl enable eimemory-rpc.service
  _user_systemctl enable --now openclaw-loop-watch.timer
  _user_systemctl enable --now openclaw-loop-compact.timer
  _user_systemctl enable --now openclaw-stuck-watchdog.timer
  _user_systemctl enable openclaw-feishu-reply-watchdog.service
fi
_install_openclaw_loop_compat_script

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"
mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
CURRENT_SWITCHED=1
_refresh_openclaw_plugin_registry
_maybe_fail_stage registry
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  _user_systemctl restart eimemory-rpc.service
fi
_maybe_fail_stage rpc_restart
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  _user_systemctl restart openclaw-feishu-reply-watchdog.service
  _user_systemctl restart openclaw-gateway.service
  _inspect_openclaw_plugin_runtime
fi
_maybe_fail_stage gateway_restart
_verify_release_health "$RELEASE_DIR" "$COMMIT"
_maybe_fail_stage health
_record_deployment_receipt
_maybe_fail_stage receipt
_run_post_switch_closure
_maybe_fail_stage acceptance
if [ -n "$BACKUP_DIR" ] && [ -e "$BACKUP_DIR" ]; then
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
    --remove-stage --release-dir "$BACKUP_DIR" --releases-root "$INSTALL_ROOT/releases" || \
    echo "warning: unable to remove prior release backup: $BACKUP_DIR" >&2
fi

if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
  chown -h "$SERVICE_USER:$SERVICE_GROUP" "$CURRENT_LINK" 2>/dev/null || true
fi
COMMITTED=1
echo "commit_complete=1"
trap - EXIT

echo "release=$RELEASE_DIR"
echo "current=$CURRENT_LINK"
echo "commit=$COMMIT"
echo "service_user=$SERVICE_USER"
echo "user_systemd_unit=$USER_SYSTEMD_DIR/eimemory-rpc.service"
