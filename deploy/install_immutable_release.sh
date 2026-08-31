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
EIMEMORY_LOG_DIR="${EIMEMORY_LOG_DIR:-$EIMEMORY_ROOT/logs}"
GOVERNANCE_ENV_FILE="${EIMEMORY_GOVERNANCE_ENV_FILE:-$EIMEMORY_CONFIG_DIR/governance.env}"
EVIDENCE_RECEIPT_ENV_FILE="${EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE:-$EIMEMORY_CONFIG_DIR/evidence-receipt.env}"
HERMES_INTEGRATION_DEPLOY="${EIMEMORY_HERMES_INTEGRATION_DEPLOY:-1}"
HERMES_HOME_DIR="${EIMEMORY_HERMES_HOME:-$SERVICE_HOME/.hermes}"
HERMES_PYTHON="${EIMEMORY_HERMES_PYTHON:-$HERMES_HOME_DIR/hermes-agent/venv/bin/python}"
HERMES_ATTESTATION_REGISTRY="${EIMEMORY_ATTESTATION_TOKENS_FILE:-$EIMEMORY_CONFIG_DIR/attestation-producers.json}"
HERMES_ATTESTATION_TOKEN_FILE="${EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE:-$EIMEMORY_CONFIG_DIR/hermes-attestation.token}"
HERMES_RECEIPT_HANDOFF_FILE="${EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE:-$EIMEMORY_ROOT/state/adapter-receipt-handoff.sqlite3}"
USER_SYSTEMD_ENABLE_SERVICE="${USER_SYSTEMD_ENABLE_SERVICE:-1}"
USER_SYSTEMD_DIR="${USER_SYSTEMD_DIR:-$SERVICE_HOME/.config/systemd/user}"
SYSTEM_RPC_UNIT_PATH="${SYSTEM_RPC_UNIT_PATH:-/etc/systemd/system/eimemory-rpc.service}"
SYSTEM_SYSTEMD_DIR="${SYSTEM_SYSTEMD_DIR:-$(dirname "$SYSTEM_RPC_UNIT_PATH")}"
SYSTEM_RPC_DROPIN_DIR="${SYSTEM_RPC_DROPIN_DIR:-$SYSTEM_SYSTEMD_DIR/eimemory-rpc.service.d}"
OPENCLAW_LOOP_DEPLOY_VERIFY="${OPENCLAW_LOOP_DEPLOY_VERIFY:-1}"
OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS="${OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS:-0}"
OPENCLAW_LOOP_CONFIG_PATH="${OPENCLAW_LOOP_CONFIG_PATH:-$SERVICE_HOME/.openclaw/openclaw.json}"
OPENCLAW_LOOP_COMPAT_SCRIPT="${OPENCLAW_LOOP_COMPAT_SCRIPT:-$SERVICE_HOME/.openclaw/workspace/scripts/openclaw_loop.py}"
OPENCLAW_BIN="${OPENCLAW_BIN:-$SERVICE_HOME/n/bin/openclaw}"
EIMEMORY_OPENCLAW_ADAPTER="${EIMEMORY_OPENCLAW_ADAPTER:-auto}"
OPENCLAW_ADAPTER_ENABLED=0
EIMEMORY_POST_SWITCH_GATES="${EIMEMORY_POST_SWITCH_GATES:-1}"
EIMEMORY_RELEASE_CLOSURE_MODE="${EIMEMORY_RELEASE_CLOSURE_MODE:-auto}"
EIMEMORY_HEALTH_URL="${EIMEMORY_HEALTH_URL:-http://127.0.0.1:8091/health}"
EIMEMORY_DEPLOY_SCOPE_AGENT="${EIMEMORY_DEPLOY_SCOPE_AGENT:-hongtu}"
EIMEMORY_DEPLOY_SCOPE_WORKSPACE="${EIMEMORY_DEPLOY_SCOPE_WORKSPACE:-embodied}"
EIMEMORY_DEPLOY_SCOPE_USER="${EIMEMORY_DEPLOY_SCOPE_USER:-darrow}"
EIMEMORY_STORAGE_MIGRATION="${EIMEMORY_STORAGE_MIGRATION:-1}"
EIMEMORY_STORAGE_SNAPSHOT_ROOT="${EIMEMORY_STORAGE_SNAPSHOT_ROOT:-$EIMEMORY_ROOT/state/release-snapshots}"
EIMEMORY_STORAGE_BATCH_SIZE="${EIMEMORY_STORAGE_BATCH_SIZE:-200}"
EIMEMORY_STORAGE_MAX_BATCHES="${EIMEMORY_STORAGE_MAX_BATCHES:-10000}"
EIMEMORY_STORAGE_MAX_SECONDS="${EIMEMORY_STORAGE_MAX_SECONDS:-3600}"
EIMEMORY_STORAGE_SNAPSHOT_RETENTION="${EIMEMORY_STORAGE_SNAPSHOT_RETENTION:-2}"
EIMEMORY_DEPLOY_FAIL_STORAGE_STOP_UNIT="${EIMEMORY_DEPLOY_FAIL_STORAGE_STOP_UNIT:-}"
EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE="${EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE:-0}"
EIMEMORY_CODE_EVOLUTION_TRANSACTION_ID="${EIMEMORY_CODE_EVOLUTION_TRANSACTION_ID:-}"
EIMEMORY_CODE_EVOLUTION_AUTHORIZATION_DIGEST="${EIMEMORY_CODE_EVOLUTION_AUTHORIZATION_DIGEST:-}"
EIMEMORY_CODE_EVOLUTION_POLICY_DIGEST="${EIMEMORY_CODE_EVOLUTION_POLICY_DIGEST:-}"
EIMEMORY_CODE_EVOLUTION_PATCH_DIGEST="${EIMEMORY_CODE_EVOLUTION_PATCH_DIGEST:-}"
EIMEMORY_CODE_EVOLUTION_CANDIDATE_TREE_DIGEST="${EIMEMORY_CODE_EVOLUTION_CANDIDATE_TREE_DIGEST:-}"
EIMEMORY_CODE_EVOLUTION_VERIFICATION_RECEIPTS="${EIMEMORY_CODE_EVOLUTION_VERIFICATION_RECEIPTS:-}"
EIMEMORY_CODE_EVOLUTION_OBSERVATION_DEADLINE="${EIMEMORY_CODE_EVOLUTION_OBSERVATION_DEADLINE:-}"
EIMEMORY_CODE_EVOLUTION_PROVIDER_DIGEST="${EIMEMORY_CODE_EVOLUTION_PROVIDER_DIGEST:-}"
EIMEMORY_CODE_EVOLUTION_LINEAGE_JSON="${EIMEMORY_CODE_EVOLUTION_LINEAGE_JSON:-}"
STORAGE_TRANSACTION_MARKER="${EIMEMORY_STORAGE_TRANSACTION_MARKER:-$EIMEMORY_ROOT/state/storage-release-transaction.json}"
STORAGE_TRANSACTION_LIBEXEC="${EIMEMORY_STORAGE_TRANSACTION_LIBEXEC:-$INSTALL_ROOT/libexec}"
STORAGE_TRANSACTION_HELPER="$STORAGE_TRANSACTION_LIBEXEC/storage-release-transaction.py"
STORAGE_DEPLOY_LOCK_PATH="${EIMEMORY_STORAGE_DEPLOY_LOCK_PATH:-$INSTALL_ROOT/.storage-release-install.lock}"
CANDIDATE_VALIDATION_LOCK_PATH="${EIMEMORY_CANDIDATE_VALIDATION_LOCK_PATH:-$INSTALL_ROOT/.candidate-validation.lock}"
STORAGE_TRANSACTION_CLEARING="$(dirname "$STORAGE_TRANSACTION_MARKER")/.$(basename "$STORAGE_TRANSACTION_MARKER").clearing"
STORAGE_TRANSACTION_RECOVERY="$(dirname "$STORAGE_TRANSACTION_MARKER")/.$(basename "$STORAGE_TRANSACTION_MARKER").recovery"

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
case "$EIMEMORY_RELEASE_CLOSURE_MODE" in
  auto|always|never) ;;
  *)
    echo "EIMEMORY_RELEASE_CLOSURE_MODE must be auto, always, or never." >&2
    exit 2
    ;;
esac
EIMEMORY_DEPLOY_FAIL_STAGE="${EIMEMORY_DEPLOY_FAIL_STAGE:-}"
COMMIT="${1:-$(git -C "$REPO_DIR" rev-parse HEAD)}"
DEPLOY_MODE="${2:-deploy}"
if [ "$#" -gt 2 ] || { [ "$DEPLOY_MODE" != "deploy" ] && [ "$DEPLOY_MODE" != "--recover-only" ]; }; then
  echo "Usage: install_immutable_release.sh <full-commit> [--recover-only]" >&2
  exit 2
fi
if [ "$DEPLOY_MODE" = "--recover-only" ] && [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" = "1" ]; then
  echo "Recovery-only cannot create or qualify a code-evolution deployment." >&2
  exit 2
fi
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
if [ "$(id -u)" -eq 0 ] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "service_user=failed missing user=$SERVICE_USER" >&2
  exit 2
fi
if [ -n "${EIMEMORY_STORAGE_ATTEMPT_ID:-}" ]; then
  STORAGE_ATTEMPT_ID="$EIMEMORY_STORAGE_ATTEMPT_ID"
else
  STORAGE_ATTEMPT_ID="${COMMIT}-$($PYTHON_BIN -I -B -c 'import uuid; print(uuid.uuid4().hex)')"
fi
STORAGE_SNAPSHOT_DIR="$EIMEMORY_STORAGE_SNAPSHOT_ROOT/$STORAGE_ATTEMPT_ID"

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

_hermes_is_installed() {
  [ "$HERMES_INTEGRATION_DEPLOY" = "1" ] && \
    [ -x "$HERMES_PYTHON" ] && [ -d "$HERMES_HOME_DIR/hermes-agent" ]
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
  if ! command -v systemctl >/dev/null 2>&1; then
    if [ -e "$SYSTEM_RPC_UNIT_PATH" ] || [ -L "$SYSTEM_RPC_UNIT_PATH" ]; then
      echo "legacy_system_rpc=state_failed systemd_unavailable" >&2
      return 2
    fi
    return
  fi
  local state was_active=0 unit_present=0
  if [ -e "$SYSTEM_RPC_UNIT_PATH" ] || [ -L "$SYSTEM_RPC_UNIT_PATH" ]; then
    unit_present=1
  fi
  if systemctl is-active --quiet eimemory-rpc.service; then
    state=0
  else
    state=$?
  fi
  case "$state" in
    0) was_active=1 ;;
    3|4) ;;
    *)
      echo "legacy_system_rpc=state_failed status=$state" >&2
      return 2
      ;;
  esac
  if [ "$unit_present" = "1" ] || [ "$was_active" = "1" ]; then
    if [ "$(id -u)" -ne 0 ]; then
      echo "legacy_system_rpc=stop_failed root_required" >&2
      return 2
    fi
    if [ "$was_active" = "1" ] && ! systemctl stop eimemory-rpc.service; then
      echo "legacy_system_rpc=stop_failed" >&2
      return 2
    fi
    if systemctl is-active --quiet eimemory-rpc.service; then
      echo "legacy_system_rpc=stop_failed still_active" >&2
      return 2
    else
      state=$?
      if [ "$state" != "3" ] && [ "$state" != "4" ]; then
        echo "legacy_system_rpc=stop_failed verify_status=$state" >&2
        return 2
      fi
    fi
    if ! systemctl disable eimemory-rpc.service; then
      echo "legacy_system_rpc=disable_failed" >&2
      return 2
    fi
  fi
  if [ -e "$SYSTEM_RPC_UNIT_PATH" ] || [ -L "$SYSTEM_RPC_UNIT_PATH" ]; then
    local retired_path="$SYSTEM_RPC_UNIT_PATH.retired-by-eimemory-user-systemd"
    mv -f "$SYSTEM_RPC_UNIT_PATH" "$retired_path"
    echo "retired_systemd_unit=$retired_path"
    if ! systemctl daemon-reload; then
      echo "legacy_system_rpc=daemon_reload_failed" >&2
      return 2
    fi
  fi
}

_resolve_openclaw_adapter() {
  # Selection is topology, never a version allowlist or a failed health probe.
  # An existing but broken installation must not silently become optional.
  local present=0
  if [ -e "$OPENCLAW_BIN" ] || [ -L "$OPENCLAW_BIN" ] || \
     [ -e "$OPENCLAW_LOOP_CONFIG_PATH" ] || [ -L "$OPENCLAW_LOOP_CONFIG_PATH" ]; then
    present=1
  fi
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    local load_state
    if ! load_state="$(_user_systemctl show openclaw-gateway.service --property=LoadState --value)"; then
      echo "openclaw_adapter=failed topology_unavailable" >&2
      return 2
    fi
    case "$load_state" in
      not-found|"") ;;
      *) present=1 ;;
    esac
  fi
  case "$EIMEMORY_OPENCLAW_ADAPTER" in
    auto) OPENCLAW_ADAPTER_ENABLED="$present" ;;
    enabled) OPENCLAW_ADAPTER_ENABLED=1 ;;
    disabled) OPENCLAW_ADAPTER_ENABLED=0 ;;
    *) echo "EIMEMORY_OPENCLAW_ADAPTER must be auto, enabled, or disabled" >&2; return 2 ;;
  esac
  if [ "$OPENCLAW_ADAPTER_ENABLED" = "1" ] && \
     { [ ! -x "$OPENCLAW_BIN" ] || [ ! -e "$OPENCLAW_LOOP_CONFIG_PATH" ]; }; then
    echo "openclaw_adapter=failed selected_adapter_incomplete" >&2
    return 2
  fi
  echo "openclaw_adapter=$OPENCLAW_ADAPTER_ENABLED selection=$EIMEMORY_OPENCLAW_ADAPTER"
}

_openclaw_is_enabled() {
  [ "${OPENCLAW_ADAPTER_ENABLED:-0}" = "1" ]
}

_is_unselected_openclaw_unit() {
  if _openclaw_is_enabled; then return 1; fi
  case "$1" in
    openclaw-gateway.service|openclaw-loop-watch.service|openclaw-loop-compact.service) return 0 ;;
    *) return 1 ;;
  esac
}

_run_openclaw_loop_deploy_verify() {
  if ! _openclaw_is_enabled; then return 0; fi
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
  if ! _openclaw_is_enabled; then return 0; fi
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
  if ! _openclaw_is_enabled; then return 0; fi
  if [ ! -x "$OPENCLAW_BIN" ]; then
    echo "openclaw_plugin_registry_refresh=failed binary_not_found" >&2
    return 2
  fi
  _run_as_service_user env HOME="$SERVICE_HOME" OPENCLAW_CONFIG_PATH="$OPENCLAW_LOOP_CONFIG_PATH" \
    timeout 240 "$OPENCLAW_BIN" plugins registry --refresh --json >/dev/null
}

_install_openclaw_bundled_bridge() {
  if ! _openclaw_is_enabled; then return 0; fi
  local target_release="${1:-$RELEASE_DIR}"
  local helper_release="${2:-$target_release}"
  if [ ! -x "$OPENCLAW_BIN" ]; then
    echo "openclaw_external_bridge=failed binary_not_found" >&2
    return 2
  fi
  # Historical helper filename; it now uses only official external loading.
  "$PYTHON_BIN" -I -B "$helper_release/deploy/ensure_openclaw_bundled_bridge.py" \
    --bin "$OPENCLAW_BIN" \
    --bridge-dir "$target_release/integrations/openclaw/eimemory-bridge" \
    --config "$OPENCLAW_LOOP_CONFIG_PATH" --preflight
  "$PYTHON_BIN" -I -B "$helper_release/deploy/ensure_openclaw_bundled_bridge.py" \
    --bin "$OPENCLAW_BIN" \
    --bridge-dir "$target_release/integrations/openclaw/eimemory-bridge" \
    --config "$OPENCLAW_LOOP_CONFIG_PATH"
}

_preflight_openclaw_adapter() {
  if ! _openclaw_is_enabled; then return 0; fi
  _run_as_service_user env HOME="$SERVICE_HOME" OPENCLAW_CONFIG_PATH="$OPENCLAW_LOOP_CONFIG_PATH" \
    timeout 120 "$OPENCLAW_BIN" config validate --json >/dev/null
  _refresh_openclaw_plugin_registry
  echo "openclaw_adapter_preflight=verified"
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

STORAGE_WRITER_UNITS=(
  eimemory-code-implementation-refresh.timer
  eimemory-nightly.timer
  eimemory-learn-watch.timer
  eimemory-learn-think.timer
  eimemory-learn-dashboard.timer
  eimemory-audit-verify.timer
  eimemory-timer-monitor.timer
  eimemory-experience-autopromote.timer
  openclaw-loop-watch.timer
  openclaw-loop-compact.timer
  eimemory-release-closure.service
  eimemory-code-implementation-refresh.service
  eimemory-nightly.service
  eimemory-learn-watch.service
  eimemory-learn-think.service
  eimemory-learn-dashboard.service
  eimemory-audit-verify.service
  eimemory-timer-monitor.service
  eimemory-experience-autopromote.service
  openclaw-loop-watch.service
  openclaw-loop-compact.service
  openclaw-gateway.service
  eimemory-rpc.service
)
ACTIVE_STORAGE_WRITER_UNITS=()

_storage_unit_is_active() {
  local unit="$1"
  local status
  if _user_systemctl is-active --quiet "$unit"; then
    return 0
  else
    status=$?
  fi
  # systemd uses 3 for inactive and 4 for unknown. Authorization, D-Bus, and
  # transport failures must not be misclassified as safely stopped.
  if [ "$status" = "3" ] || [ "$status" = "4" ]; then
    return 1
  fi
  echo "storage_writer_state=failed unit=$unit status=$status" >&2
  return 2
}

_stop_storage_writers() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    echo "storage_writer_stop=failed systemd_unavailable" >&2
    return 2
  fi
  if ! _capture_storage_writers; then
    echo "storage_writer_capture=failed" >&2
    return 2
  fi
  # Set this before the first stop so cleanup restarts the captured set after a
  # partial stop failure.
  STORAGE_WRITERS_STOPPED=1
  local unit state
  for unit in "${STORAGE_WRITER_UNITS[@]}"; do
    if _storage_unit_is_active "$unit"; then
      state=0
    else
      state=$?
    fi
    if [ "$state" = "0" ]; then
      if [ -n "$EIMEMORY_DEPLOY_FAIL_STORAGE_STOP_UNIT" ] && \
         [ "$EIMEMORY_DEPLOY_FAIL_STORAGE_STOP_UNIT" = "$unit" ]; then
        echo "storage_writer_stop=failed injected_unit=$unit" >&2
        return 98
      fi
      if ! _user_systemctl stop "$unit"; then
        echo "storage_writer_stop=failed unit=$unit" >&2
        return 2
      fi
    elif [ "$state" != "1" ]; then
      return 2
    fi
  done
  for unit in "${STORAGE_WRITER_UNITS[@]}"; do
    if _storage_unit_is_active "$unit"; then
      echo "storage_writer_stop=failed still_active=$unit" >&2
      return 2
    else
      state=$?
      if [ "$state" != "1" ]; then
        return 2
      fi
    fi
  done
  echo "storage_writer_stop=complete captured=${#ACTIVE_STORAGE_WRITER_UNITS[@]}"
}

_capture_storage_writers() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    echo "storage_writer_capture=failed systemd_unavailable" >&2
    return 2
  fi
  if [ "$STORAGE_WRITERS_CAPTURED" != "1" ]; then
    local candidate state
    if [ "$STORAGE_WRITERS_RELOADED" != "1" ]; then
      ACTIVE_STORAGE_WRITER_UNITS=()
    fi
    for candidate in "${STORAGE_WRITER_UNITS[@]}"; do
      if _storage_unit_is_active "$candidate"; then
        state=0
      else
        state=$?
      fi
      case "$state" in
        0)
          if [ "$STORAGE_WRITERS_RELOADED" != "1" ]; then
            ACTIVE_STORAGE_WRITER_UNITS+=("$candidate")
          fi
          ;;
        1) ;;
        *) return 2 ;;
      esac
    done
    STORAGE_WRITERS_CAPTURED=1
    STORAGE_WRITERS_RELOADED=0
  fi
  echo "storage_writer_capture=complete captured=${#ACTIVE_STORAGE_WRITER_UNITS[@]}"
}

_restart_storage_writers() {
  if [ "$STORAGE_WRITERS_STOPPED" != "1" ]; then
    return 0
  fi
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    STORAGE_WRITERS_STOPPED=0
    return
  fi
  local unit
  for unit in "${ACTIVE_STORAGE_WRITER_UNITS[@]}"; do
    if ! _user_systemctl start "$unit"; then
      echo "storage_writer_restart=failed unit=$unit" >&2
      return 2
    fi
  done
  STORAGE_WRITERS_STOPPED=0
  echo "storage_writer_restart=complete restored=${#ACTIVE_STORAGE_WRITER_UNITS[@]}"
}

_install_storage_release_transaction_helper() {
  local helper_source="$REPO_DIR/deploy/storage_release_transaction.py"
  if [ ! -f "$helper_source" ] || [ -L "$helper_source" ]; then
    echo "storage_release_transaction=failed helper_source_missing" >&2
    return 2
  fi
  "$PYTHON_BIN" -I -B - "$REPO_DIR" "$helper_source" "$INSTALL_ROOT" \
    "$STORAGE_TRANSACTION_LIBEXEC" <<'PY'
from pathlib import Path
import os
import sys

repo, source, install_root, libexec = map(Path, sys.argv[1:])
for label, path in (("repository", repo), ("install root", install_root)):
    if path.resolve(True) != Path(os.path.abspath(path)):
        raise SystemExit(f"storage release transaction {label} traverses a symlink")
if source.resolve(True) != Path(os.path.abspath(source)) or source.parent != repo / "deploy":
    raise SystemExit("storage release transaction helper source is not trusted")
try:
    libexec.relative_to(install_root)
except ValueError as exc:
    raise SystemExit("storage release transaction libexec escapes install root") from exc
if not libexec.parent.is_dir() or libexec.parent.resolve(True) != Path(os.path.abspath(libexec.parent)):
    raise SystemExit("storage release transaction libexec parent is not trusted")
libexec.mkdir(mode=0o755, exist_ok=True)
if not libexec.is_dir() or libexec.resolve(True) != Path(os.path.abspath(libexec)):
    raise SystemExit("storage release transaction libexec traverses a symlink")
PY
  mkdir -p "$STORAGE_TRANSACTION_LIBEXEC"
  chmod 0755 "$STORAGE_TRANSACTION_LIBEXEC"
  local staged_helper
  staged_helper="$(mktemp "$STORAGE_TRANSACTION_LIBEXEC/.storage-release-transaction-XXXXXXXX")"
  if ! install -m 0755 "$helper_source" "$staged_helper"; then
    rm -f "$staged_helper"
    return 2
  fi
  if [ -L "$STORAGE_TRANSACTION_HELPER" ]; then
    rm -f "$staged_helper"
    echo "storage_release_transaction=failed helper_target_symlink" >&2
    return 2
  fi
  mv -Tf "$staged_helper" "$STORAGE_TRANSACTION_HELPER"
  "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" fsync-path \
    --path "$STORAGE_TRANSACTION_HELPER" --boundary "$INSTALL_ROOT"
}

_acquire_storage_deploy_lock() {
  case "$(uname -s 2>/dev/null || true)" in
    MINGW*|MSYS*)
      echo "storage_deploy_lock=skipped non_posix_test_runtime" >&2
      return
      ;;
  esac
  local lock_parent resolved_parent lexical_parent holder_output_fd holder_status
  lock_parent="$(dirname "$STORAGE_DEPLOY_LOCK_PATH")"
  if ! resolved_parent="$(realpath -e -- "$lock_parent")" || \
     ! lexical_parent="$(realpath -m -s -- "$lock_parent")" || \
     [ "$resolved_parent" != "$lexical_parent" ]; then
    echo "storage_deploy_lock=failed ancestor_symlink" >&2
    return 2
  fi
  if [ -L "$STORAGE_DEPLOY_LOCK_PATH" ]; then
    echo "storage_deploy_lock=failed symlink" >&2
    return 2
  fi
  coproc EIMEMORY_STORAGE_DEPLOY_HOLDER {
    exec "$PYTHON_BIN" -I -B \
      "$REPO_DIR/deploy/hold_parent_bound_lock.py" \
      --path "$STORAGE_DEPLOY_LOCK_PATH" --parent-pid "$$" \
      --label storage_deploy_lock
  }
  STORAGE_DEPLOY_HOLDER_PID="$EIMEMORY_STORAGE_DEPLOY_HOLDER_PID"
  holder_output_fd="${EIMEMORY_STORAGE_DEPLOY_HOLDER[0]}"
  if ! IFS= read -r -t 10 holder_status <&"$holder_output_fd"; then
    holder_status="storage_deploy_lock=failed holder_unavailable"
  fi
  if [ "$holder_status" != "storage_deploy_lock=ready" ]; then
    kill "$STORAGE_DEPLOY_HOLDER_PID" >/dev/null 2>&1 || true
    wait "$STORAGE_DEPLOY_HOLDER_PID" >/dev/null 2>&1 || true
    exec {holder_output_fd}<&-
    if [ "$holder_status" = "storage_deploy_lock=contended" ]; then
      echo "storage_deploy_lock=contended" >&2
      return 73
    fi
    echo "storage_deploy_lock=failed holder_unavailable" >&2
    return 2
  fi
  exec {holder_output_fd}<&-
  echo "storage_deploy_lock=acquired"
}

_release_storage_deploy_lock() {
  if [ -z "${STORAGE_DEPLOY_HOLDER_PID:-}" ]; then
    return
  fi
  kill "$STORAGE_DEPLOY_HOLDER_PID" >/dev/null 2>&1 || true
  wait "$STORAGE_DEPLOY_HOLDER_PID" >/dev/null 2>&1 || true
  STORAGE_DEPLOY_HOLDER_PID=""
  echo "storage_deploy_lock=released"
}

_acquire_candidate_validation_lock() {
  local lock_parent resolved_parent holder_output_fd holder_status
  if [ -n "${CANDIDATE_VALIDATION_HOLDER_PID:-}" ] && \
     kill -0 "$CANDIDATE_VALIDATION_HOLDER_PID" 2>/dev/null; then
    return
  fi
  if [[ "$CANDIDATE_VALIDATION_LOCK_PATH" != /* ]]; then
    echo "candidate_validation_lock=failed non_absolute" >&2
    return 2
  fi
  lock_parent="$(dirname "$CANDIDATE_VALIDATION_LOCK_PATH")"
  if ! resolved_parent="$(realpath -e -- "$lock_parent")" || \
     [ "$resolved_parent" != "$(realpath -e -- "$INSTALL_ROOT")" ]; then
    echo "candidate_validation_lock=failed parent" >&2
    return 2
  fi
  coproc EIMEMORY_CANDIDATE_VALIDATION_HOLDER {
    exec "$PYTHON_BIN" -I -B \
      "$RELEASE_DIR/deploy/hold_parent_bound_lock.py" \
      --path "$CANDIDATE_VALIDATION_LOCK_PATH" --parent-pid "$$" \
      --label candidate_validation_lock
  }
  CANDIDATE_VALIDATION_HOLDER_PID="$EIMEMORY_CANDIDATE_VALIDATION_HOLDER_PID"
  holder_output_fd="${EIMEMORY_CANDIDATE_VALIDATION_HOLDER[0]}"
  if ! IFS= read -r -t 10 holder_status <&"$holder_output_fd" || \
     [ "$holder_status" != "candidate_validation_lock=ready" ]; then
    kill "$CANDIDATE_VALIDATION_HOLDER_PID" >/dev/null 2>&1 || true
    wait "$CANDIDATE_VALIDATION_HOLDER_PID" >/dev/null 2>&1 || true
    echo "candidate_validation_lock=failed holder_unavailable" >&2
    return 2
  fi
  exec {holder_output_fd}<&-
  echo "candidate_validation_lock=acquired"
}

_release_candidate_validation_lock() {
  if [ -z "${CANDIDATE_VALIDATION_HOLDER_PID:-}" ]; then
    return
  fi
  kill "$CANDIDATE_VALIDATION_HOLDER_PID" >/dev/null 2>&1 || true
  wait "$CANDIDATE_VALIDATION_HOLDER_PID" >/dev/null 2>&1 || true
  CANDIDATE_VALIDATION_HOLDER_PID=""
  echo "candidate_validation_lock=released"
}

_install_storage_release_guards() {
  _install_storage_release_transaction_helper
  if ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  if [ "$(id -u)" -eq 0 ]; then
    "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/install_managed_systemd_dropin.py" \
      --source "$REPO_DIR/deploy/systemd/eimemory-storage-release-guard.conf" \
      --target "$SYSTEM_RPC_DROPIN_DIR/05-eimemory-storage-release-guard.conf" \
      --root "$SYSTEM_SYSTEMD_DIR" --owner-uid 0 \
      --render-storage-transaction-python "$PYTHON_BIN" \
      --render-storage-transaction-helper "$STORAGE_TRANSACTION_HELPER" \
      --render-storage-transaction-marker "$STORAGE_TRANSACTION_MARKER"
    "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" fsync-path \
      --path "$SYSTEM_RPC_DROPIN_DIR/05-eimemory-storage-release-guard.conf" \
      --boundary "$SYSTEM_SYSTEMD_DIR"
    systemctl daemon-reload
  fi
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
  local service_uid unit
  service_uid="$(id -u "$SERVICE_USER")"
  local discovered_output=""
  if ! discovered_output="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" < "$REPO_DIR/deploy/discover_python_runtime_units.sh")"; then
    echo "Unable to discover storage runtime systemd units" >&2
    return 2
  fi
  declare -A guard_units=()
  while IFS= read -r unit; do
    if [[ "$unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
      guard_units["$unit"]=1
    fi
  done <<< "$discovered_output"
  for unit in "${STORAGE_WRITER_UNITS[@]}"; do
    if [[ "$unit" == *.service ]]; then
      # Still guard every existing/discovered writer, even an unmanaged one;
      # do not create OpenClaw unit stubs on a standalone installation.
      if _is_unselected_openclaw_unit "$unit" && \
         ! _user_systemctl cat "$unit" >/dev/null 2>&1; then
        continue
      fi
      guard_units["$unit"]=1
    fi
  done
  for unit in "${!guard_units[@]}"; do
    "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/install_managed_systemd_dropin.py" \
      --source "$REPO_DIR/deploy/systemd/eimemory-storage-release-guard.conf" \
      --target "$USER_SYSTEMD_DIR/$unit.d/05-eimemory-storage-release-guard.conf" \
      --root "$USER_SYSTEMD_DIR" --owner-uid "$service_uid" \
      --render-storage-transaction-python "$PYTHON_BIN" \
      --render-storage-transaction-helper "$STORAGE_TRANSACTION_HELPER" \
      --render-storage-transaction-marker "$STORAGE_TRANSACTION_MARKER"
    "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" fsync-path \
      --path "$USER_SYSTEMD_DIR/$unit.d/05-eimemory-storage-release-guard.conf" \
      --boundary "$SERVICE_HOME"
  done
  _user_systemctl daemon-reload
  echo "storage_release_guard=installed units=${#guard_units[@]}"
}

_begin_storage_release_transaction() {
  local active_args=() unit
  for unit in "${ACTIVE_STORAGE_WRITER_UNITS[@]}"; do
    active_args+=(--active-unit "$unit")
  done
  if ! "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" begin \
    --marker "$STORAGE_TRANSACTION_MARKER" \
    --prior-commit "$PREVIOUS_COMMIT" --candidate-commit "$COMMIT" \
    --current-link "$CURRENT_LINK" --attempt-id "$STORAGE_ATTEMPT_ID" \
    --deployment-lock-path "$STORAGE_DEPLOY_LOCK_PATH" \
    --candidate-validation-lock-path "$CANDIDATE_VALIDATION_LOCK_PATH" \
    --snapshot-dir "$STORAGE_SNAPSHOT_DIR" "${active_args[@]}" >/dev/null; then
    echo "storage_release_transaction=failed begin" >&2
    return 2
  fi
  STORAGE_TRANSACTION_ACTIVE=1
}

_update_storage_release_transaction() {
  local phase="$1"
  local destructive="${2:-}"
  local vacuum_backup="${3:-}"
  local args=(update --marker "$STORAGE_TRANSACTION_MARKER" \
    --attempt-id "$STORAGE_ATTEMPT_ID" --phase "$phase")
  if [ -n "$STORAGE_SNAPSHOT_MANIFEST_SHA256" ]; then
    args+=(--snapshot-manifest-sha256 "$STORAGE_SNAPSHOT_MANIFEST_SHA256")
  fi
  if [ -n "$destructive" ]; then
    args+=(--storage-destructive "$destructive")
  fi
  if [ -n "$vacuum_backup" ]; then
    args+=(--vacuum-backup-path "$vacuum_backup")
  fi
  "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" "${args[@]}" >/dev/null
}

_clear_storage_release_transaction() {
  if ! "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" clear \
    --marker "$STORAGE_TRANSACTION_MARKER" --attempt-id "$STORAGE_ATTEMPT_ID"; then
    echo "storage_release_transaction=failed clear" >&2
    return 2
  fi
  STORAGE_TRANSACTION_ACTIVE=0
}

_load_storage_release_transaction() {
  local transaction_json
  transaction_json="$("$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" show \
    --marker "$STORAGE_TRANSACTION_MARKER")"
  mapfile -t STORAGE_TRANSACTION_FIELDS < <(printf '%s' "$transaction_json" | \
    "$PYTHON_BIN" -I -B -c \
      'import json,sys; p=json.load(sys.stdin); print(p["attempt_id"]); print(p["candidate_commit"]); print(p["prior_commit"]); print(p["snapshot_dir"]); print(p["snapshot_manifest_sha256"]); print(p.get("vacuum_backup_path") or ""); print(p["current_link"]); print(p["phase"])')
  if [ "${#STORAGE_TRANSACTION_FIELDS[@]}" -ne 8 ]; then
    echo "storage_release_transaction=failed invalid_fields" >&2
    return 2
  fi
  STORAGE_ATTEMPT_ID="${STORAGE_TRANSACTION_FIELDS[0]}"
  STORAGE_TRANSACTION_CANDIDATE_COMMIT="${STORAGE_TRANSACTION_FIELDS[1]}"
  STORAGE_TRANSACTION_PRIOR_COMMIT="${STORAGE_TRANSACTION_FIELDS[2]}"
  STORAGE_SNAPSHOT_DIR="${STORAGE_TRANSACTION_FIELDS[3]}"
  STORAGE_SNAPSHOT_MANIFEST_SHA256="${STORAGE_TRANSACTION_FIELDS[4]}"
  STORAGE_VACUUM_BACKUP="${STORAGE_TRANSACTION_FIELDS[5]}"
  STORAGE_TRANSACTION_CURRENT_LINK="${STORAGE_TRANSACTION_FIELDS[6]}"
  STORAGE_TRANSACTION_PHASE="${STORAGE_TRANSACTION_FIELDS[7]}"
  mapfile -t ACTIVE_STORAGE_WRITER_UNITS < <(
    "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" active-units \
      --marker "$STORAGE_TRANSACTION_MARKER"
  )
  STORAGE_WRITERS_CAPTURED=0
  STORAGE_WRITERS_RELOADED=1
  STORAGE_WRITERS_STOPPED=0
  STORAGE_TRANSACTION_ACTIVE=1
}

_fsync_install_root() {
  "$PYTHON_BIN" -I -B - "$INSTALL_ROOT" <<'PY'
import os
import sys

path = sys.argv[1]
if os.name == "posix":
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

_reset_storage_transaction_state() {
  STORAGE_TRANSACTION_ACTIVE=0
  STORAGE_SNAPSHOT_READY=0
  STORAGE_RESTORED=0
  STORAGE_SNAPSHOT_MANIFEST_SHA256=""
  STORAGE_VACUUM_BACKUP=""
  STORAGE_WRITERS_CAPTURED=0
  STORAGE_WRITERS_STOPPED=0
  STORAGE_WRITERS_RELOADED=0
  ACTIVE_STORAGE_WRITER_UNITS=()
}

_reconcile_interrupted_storage_release() {
  if [ ! -e "$STORAGE_TRANSACTION_MARKER" ] && \
     [ ! -L "$STORAGE_TRANSACTION_MARKER" ] && \
     [ ! -e "$STORAGE_TRANSACTION_CLEARING" ] && \
     [ ! -L "$STORAGE_TRANSACTION_CLEARING" ] && \
     [ ! -e "$STORAGE_TRANSACTION_RECOVERY" ] && \
     [ ! -L "$STORAGE_TRANSACTION_RECOVERY" ]; then
    return
  fi
  _load_storage_release_transaction
  if [ "$STORAGE_TRANSACTION_CURRENT_LINK" != "$CURRENT_LINK" ] || \
     [ "$STORAGE_TRANSACTION_CANDIDATE_COMMIT" != "$COMMIT" ]; then
    echo "storage_release_reconcile=failed transaction_binding_mismatch" >&2
    return 2
  fi
  if [ ! -d "$RELEASE_DIR" ] || [ -L "$RELEASE_DIR" ]; then
    echo "storage_release_reconcile=failed candidate_release_missing" >&2
    return 2
  fi
  local current_target current_commit migrations_complete=0 action
  local expected_commit expected_release
  _retire_system_rpc_unit
  _stop_storage_writers
  if current_target="$(realpath -e -- "$CURRENT_LINK" 2>/dev/null)"; then
    current_commit="$(basename "$current_target")"
  elif [ ! -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ] && [ ! -d "$CURRENT_LINK" ]; then
    expected_commit="$("$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" expected-current \
      --marker "$STORAGE_TRANSACTION_MARKER")"
    if [ "$expected_commit" = "$STORAGE_TRANSACTION_CANDIDATE_COMMIT" ]; then
      expected_release="$RELEASE_DIR"
    elif [ "$expected_commit" = "$STORAGE_TRANSACTION_PRIOR_COMMIT" ]; then
      expected_release="$INSTALL_ROOT/releases/$STORAGE_TRANSACTION_PRIOR_COMMIT"
    else
      echo "storage_release_reconcile=failed expected_current_unbound" >&2
      return 2
    fi
    if [ ! -d "$expected_release" ] || [ -L "$expected_release" ]; then
      echo "storage_release_reconcile=failed expected_release_missing" >&2
      return 2
    fi
    ln -sfn "$expected_release" "$CURRENT_LINK.next"
    mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
    _fsync_install_root
    current_target="$expected_release"
    current_commit="$expected_commit"
    echo "storage_release_reconcile=recreated_missing_current commit=$current_commit"
  else
    echo "storage_release_reconcile=failed current_dangling_or_ambiguous" >&2
    return 2
  fi
  if [ "$current_commit" = "$STORAGE_TRANSACTION_CANDIDATE_COMMIT" ]; then
    migrations_complete=1
  fi
  action="$("$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" classify \
    --marker "$STORAGE_TRANSACTION_MARKER" --current-commit "$current_commit" \
    --migrations-complete "$migrations_complete")"
  case "$action" in
    clear_prior)
      _clear_storage_release_transaction
      _restart_storage_writers
      _reset_storage_transaction_state
      echo "storage_release_reconcile=cleared_safe_prior"
      ;;
    finalize_candidate)
      _cleanup_storage_vacuum_backup
      _clear_storage_release_transaction
      _restart_storage_writers
      _reset_storage_transaction_state
      echo "storage_release_reconcile=finalized_validated_candidate"
      ;;
    finalize_rollback)
      _clear_storage_release_transaction
      _restart_storage_writers
      _reset_storage_transaction_state
      echo "storage_release_reconcile=finalized_completed_rollback"
      ;;
    restore_prior|resume_rollback|resume_rollback_validation)
      PREVIOUS_COMMIT="$STORAGE_TRANSACTION_PRIOR_COMMIT"
      PREVIOUS_CURRENT="$INSTALL_ROOT/releases/$PREVIOUS_COMMIT"
      if [ ! -d "$PREVIOUS_CURRENT" ] || [ -L "$PREVIOUS_CURRENT" ]; then
        echo "storage_release_reconcile=failed prior_release_missing" >&2
        return 2
      fi
      CURRENT_SWITCHED=1
      if [ -n "$STORAGE_SNAPSHOT_MANIFEST_SHA256" ]; then
        STORAGE_SNAPSHOT_READY=1
      fi
      if [ "$action" = "resume_rollback_validation" ]; then
        _rollback_current_release resume_validation
      else
        _rollback_current_release
      fi
      _reset_storage_transaction_state
      CURRENT_SWITCHED=0
      echo "storage_release_reconcile=resumed_and_validated_rollback"
      ;;
    *)
      echo "storage_release_reconcile=failed unknown_action" >&2
      return 2
      ;;
  esac
}

_storage_release_action() {
  local action="$1"
  shift
  if [ ! -x "$RELEASE_DIR/.venv/bin/python" ]; then
    echo "storage_release_action=failed candidate_python_unavailable" >&2
    return 2
  fi
  _run_as_service_user env EIMEMORY_ROOT="$EIMEMORY_ROOT" \
    "$RELEASE_DIR/.venv/bin/python" -I -B \
      "$RELEASE_DIR/deploy/migrate_storage_release.py" "$action" \
      --root "$EIMEMORY_ROOT" \
      --snapshot-root "$EIMEMORY_STORAGE_SNAPSHOT_ROOT" \
      --snapshot-dir "$STORAGE_SNAPSHOT_DIR" \
      --candidate-commit "$COMMIT" \
      --attempt-id "$STORAGE_ATTEMPT_ID" \
      --snapshot-manifest-sha256 "$STORAGE_SNAPSHOT_MANIFEST_SHA256" \
      --batch-size "$EIMEMORY_STORAGE_BATCH_SIZE" \
      --max-batches "$EIMEMORY_STORAGE_MAX_BATCHES" \
      --max-seconds "$EIMEMORY_STORAGE_MAX_SECONDS" \
      "$@"
}

_prepare_storage_for_release() {
  if [ ! -f "$EIMEMORY_ROOT/state/eimemory.sqlite" ]; then
    echo "storage_release_migration=skipped database_missing"
    return
  fi
  local needs_report storage_needed
  needs_report="$(_storage_release_action needs)"
  printf '%s\n' "$needs_report"
  storage_needed="$(printf '%s' "$needs_report" | \
    "$RELEASE_DIR/.venv/bin/python" -I -B -c \
      'import json,sys; print("1" if json.load(sys.stdin).get("needed") is True else "0")')"
  if [ "$EIMEMORY_STORAGE_MIGRATION" != "1" ]; then
    if [ "$storage_needed" = "1" ]; then
      echo "storage_release_migration=blocked disabled_with_pending_migrations" >&2
      return 2
    fi
    echo "storage_release_migration=disabled no_pending_migrations"
    storage_needed=0
  fi
  if [ "$storage_needed" != "1" ]; then
    STORAGE_MIGRATION_REQUIRED=0
    # Candidate services and release-owned bootstraps can mutate durable
    # capability/runtime state even when the SQLite schema is current. Keep
    # those writes inside the same recoverable snapshot boundary as migrations.
    echo "storage_release_snapshot=required code_only"
  else
    STORAGE_MIGRATION_REQUIRED=1
  fi
  if [ ! -d "$EIMEMORY_STORAGE_SNAPSHOT_ROOT" ] || [ -L "$EIMEMORY_STORAGE_SNAPSHOT_ROOT" ]; then
    echo "storage_release_migration=failed unsafe_snapshot_root" >&2
    return 2
  fi
  if ! _capture_storage_writers; then
    echo "storage_writer_capture=failed before_transaction" >&2
    return 2
  fi
  if ! _retire_system_rpc_unit; then
    echo "legacy_system_rpc=failed before_storage_marker" >&2
    return 2
  fi
  if ! _begin_storage_release_transaction; then
    return 2
  fi
  _stop_storage_writers
  _update_storage_release_transaction writers_stopped
  _maybe_fail_stage storage_writer_stop
  _storage_release_action preflight
  _maybe_fail_stage storage_preflight
  local snapshot_report
  if ! snapshot_report="$(_storage_release_action snapshot)"; then
    printf '%s\n' "$snapshot_report" >&2
    echo "storage_release_snapshot=failed" >&2
    return 2
  fi
  printf '%s\n' "$snapshot_report"
  STORAGE_SNAPSHOT_MANIFEST_SHA256="$(printf '%s' "$snapshot_report" | \
    "$RELEASE_DIR/.venv/bin/python" -I -B -c \
      'import json,re,sys; value=str(json.load(sys.stdin).get("manifest_sha256") or ""); sys.exit(2) if re.fullmatch(r"[0-9a-f]{64}", value) is None else print(value)')"
  # The snapshot operation itself is read-only against live storage. Arm the
  # rollback trap only after its immutable identity has been parsed safely.
  STORAGE_SNAPSHOT_READY=1
  _update_storage_release_transaction snapshot_ready
  _maybe_fail_stage storage_snapshot
  if [ "$STORAGE_MIGRATION_REQUIRED" != "1" ]; then
    echo "storage_release_migration=skipped no_pending_migrations"
    return
  fi
  _update_storage_release_transaction storage_destructive 1
  _storage_release_action migrate
  _update_storage_release_transaction storage_migrated 1
  _maybe_fail_stage storage_migrate
  local vacuum_report
  vacuum_report="$(_storage_release_action vacuum)"
  printf '%s\n' "$vacuum_report"
  STORAGE_VACUUM_BACKUP="$(printf '%s' "$vacuum_report" | \
    "$RELEASE_DIR/.venv/bin/python" -I -B -c \
      'import json,sys; print(str(json.load(sys.stdin).get("backup_path") or ""))')"
  _update_storage_release_transaction vacuum_complete 1 "$STORAGE_VACUUM_BACKUP"
  _maybe_fail_stage storage_vacuum
  _storage_release_action status
  _maybe_fail_stage storage_status
}

_restore_storage_snapshot() {
  if [ "$STORAGE_SNAPSHOT_READY" != "1" ] || [ "$STORAGE_RESTORED" = "1" ]; then
    return
  fi
  _storage_release_action restore || return $?
  STORAGE_RESTORED=1
  echo "storage_snapshot_restore=complete snapshot=$STORAGE_SNAPSHOT_DIR" >&2
}

_cleanup_storage_vacuum_backup() {
  if [ -z "$STORAGE_VACUUM_BACKUP" ]; then
    return
  fi
  _storage_release_action cleanup-vacuum --backup-path "$STORAGE_VACUUM_BACKUP" || return $?
  STORAGE_VACUUM_BACKUP=""
}

_prune_storage_snapshots() {
  if [ "$STORAGE_SNAPSHOT_READY" != "1" ] || \
     [ ! -d "$EIMEMORY_STORAGE_SNAPSHOT_ROOT" ] || \
     [ ! -f "$EIMEMORY_ROOT/state/eimemory.sqlite" ]; then
    return
  fi
  _storage_release_action prune-snapshots \
    --retain-snapshots "$EIMEMORY_STORAGE_SNAPSHOT_RETENTION"
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
  if ! _openclaw_is_enabled; then return 0; fi
  local target_release="${1:-$RELEASE_DIR}"
  local verifier_release="${2:-$target_release}"
  local allow_legacy_runtime="${3:-0}"
  if [ ! -x "$OPENCLAW_BIN" ]; then
    echo "openclaw_plugin_runtime_inspect=failed binary_not_found" >&2
    return 2
  fi
  local inspect_json
  local legacy_arg=()
  if [ "$allow_legacy_runtime" = "1" ]; then
    legacy_arg=(--allow-legacy-runtime)
  fi
  inspect_json="$(_run_as_service_user env HOME="$SERVICE_HOME" OPENCLAW_CONFIG_PATH="$OPENCLAW_LOOP_CONFIG_PATH" \
    "$OPENCLAW_BIN" plugins inspect eimemory-bridge --runtime --json)"
  printf '%s' "$inspect_json" | \
    "$PYTHON_BIN" -I -B "$verifier_release/deploy/verify_openclaw_plugin_runtime.py" \
      --expected-root "$target_release/integrations/openclaw/eimemory-bridge" \
      "${legacy_arg[@]}"
}

_install_learning_runtime_policy() {
  local target_release="${1:-$RELEASE_DIR}"
  _user_systemctl disable --now eimemory-l5-observation-gate.timer >/dev/null 2>&1 || true
  _user_systemctl stop eimemory-l5-observation-gate.service >/dev/null 2>&1 || true
  _run_as_service_user rm -f \
    "$USER_SYSTEMD_DIR/eimemory-l5-observation-gate.sh" \
    "$USER_SYSTEMD_DIR/eimemory-l5-observation-gate.service" \
    "$USER_SYSTEMD_DIR/eimemory-l5-observation-gate.timer"
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/eimemory-nightly.service.d"
  _run_as_service_user rm -f \
    "$USER_SYSTEMD_DIR/eimemory-nightly.service.d/l5-auto-apply.conf" \
    "$USER_SYSTEMD_DIR/eimemory-nightly.service.d/99-disable-auto-promotion.conf" \
    "$USER_SYSTEMD_DIR/eimemory-nightly.service.d/zz-disable-auto-promotion.conf" \
    "$USER_SYSTEMD_DIR/eimemory-nightly.service.d/zz-l5-start-now.conf"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-nightly.service" \
    "$USER_SYSTEMD_DIR/eimemory-nightly.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-nightly.timer" \
    "$USER_SYSTEMD_DIR/eimemory-nightly.timer"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learn-watch.service" \
    "$USER_SYSTEMD_DIR/eimemory-learn-watch.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learn-watch.timer" \
    "$USER_SYSTEMD_DIR/eimemory-learn-watch.timer"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learn-think.service" \
    "$USER_SYSTEMD_DIR/eimemory-learn-think.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learn-think.timer" \
    "$USER_SYSTEMD_DIR/eimemory-learn-think.timer"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learn-dashboard.service" \
    "$USER_SYSTEMD_DIR/eimemory-learn-dashboard.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learn-dashboard.timer" \
    "$USER_SYSTEMD_DIR/eimemory-learn-dashboard.timer"
  _install_as_service_user 0755 \
    "$target_release/deploy/systemd/eimemory-l5-effect-review.sh" \
    "$USER_SYSTEMD_DIR/eimemory-l5-effect-review.sh"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-l5-effect-review.service" \
    "$USER_SYSTEMD_DIR/eimemory-l5-effect-review.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-l5-effect-review.timer" \
    "$USER_SYSTEMD_DIR/eimemory-l5-effect-review.timer"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-audit-verify.service" \
    "$USER_SYSTEMD_DIR/eimemory-audit-verify.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-audit-verify.timer" \
    "$USER_SYSTEMD_DIR/eimemory-audit-verify.timer"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-timer-monitor.service" \
    "$USER_SYSTEMD_DIR/eimemory-timer-monitor.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-timer-monitor.timer" \
    "$USER_SYSTEMD_DIR/eimemory-timer-monitor.timer"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-learning-runtime.conf" \
    "$USER_SYSTEMD_DIR/eimemory-nightly.service.d/zz-eimemory-learning-runtime.conf"
  _user_systemctl daemon-reload
  _user_systemctl enable --now eimemory-nightly.timer
  _user_systemctl enable --now eimemory-learn-watch.timer
  _user_systemctl enable --now eimemory-learn-think.timer
  _user_systemctl enable --now eimemory-learn-dashboard.timer
  _user_systemctl enable --now eimemory-l5-effect-review.timer
  _user_systemctl enable --now eimemory-audit-verify.timer
  _user_systemctl enable --now eimemory-timer-monitor.timer
}

_install_code_implementation_owner_policy() {
  local target_release="${1:-$RELEASE_DIR}"
  local service_source="$target_release/deploy/systemd/eimemory-code-implementation-refresh.service"
  local timer_source="$target_release/deploy/systemd/eimemory-code-implementation-refresh.timer"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi

  # Retire the exact temporary Grok owners before enabling the release-owned
  # timer.  Best-effort disablement is followed by removing only their known
  # unit files, preventing two processes from writing the same authority.
  _user_systemctl disable --now eimemory-code-implementation-bringup.service >/dev/null 2>&1 || true
  _user_systemctl disable --now eimemory-code-implementation-advertise.service >/dev/null 2>&1 || true
  _user_systemctl disable --now eimemory-code-implementation-advertise.timer >/dev/null 2>&1 || true
  _run_as_service_user rm -f \
    "$USER_SYSTEMD_DIR/eimemory-code-implementation-bringup.service" \
    "$USER_SYSTEMD_DIR/eimemory-code-implementation-advertise.service" \
    "$USER_SYSTEMD_DIR/eimemory-code-implementation-advertise.timer"

  # A rollback to a release predating the official owner must retire the new
  # timer instead of leaving a unit that points at an unavailable command.
  if [ ! -f "$service_source" ] || [ -L "$service_source" ] || \
     [ ! -f "$timer_source" ] || [ -L "$timer_source" ]; then
    _user_systemctl disable --now eimemory-code-implementation-refresh.timer >/dev/null 2>&1 || true
    _user_systemctl stop eimemory-code-implementation-refresh.service >/dev/null 2>&1 || true
    _run_as_service_user rm -f \
      "$USER_SYSTEMD_DIR/eimemory-code-implementation-refresh.service" \
      "$USER_SYSTEMD_DIR/eimemory-code-implementation-refresh.timer" \
      "$USER_SYSTEMD_DIR/eimemory-code-implementation-refresh.service.d/90-eimemory-python-runtime.conf"
    _run_as_service_user rmdir \
      "$USER_SYSTEMD_DIR/eimemory-code-implementation-refresh.service.d" \
      >/dev/null 2>&1 || true
    _user_systemctl daemon-reload
    echo "code_implementation_owner=retired target_release_without_owner"
    return
  fi

  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-code-implementation-refresh.service" \
    "$USER_SYSTEMD_DIR/eimemory-code-implementation-refresh.service"
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-code-implementation-refresh.timer" \
    "$USER_SYSTEMD_DIR/eimemory-code-implementation-refresh.timer"
  _user_systemctl daemon-reload
  _user_systemctl enable eimemory-code-implementation-refresh.timer
}

_start_code_implementation_owner() {
  local target_release="${1:-$RELEASE_DIR}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  if [ ! -f "$target_release/deploy/systemd/eimemory-code-implementation-refresh.service" ] || \
     [ ! -f "$target_release/deploy/systemd/eimemory-code-implementation-refresh.timer" ]; then
    echo "code_implementation_owner=skipped target_release_without_owner"
    return
  fi
  local current_target
  current_target="$(realpath -e -- "$CURRENT_LINK")"
  if [ "$current_target" != "$(realpath -e -- "$target_release")" ]; then
    echo "code_implementation_owner=failed current_release_mismatch" >&2
    return 2
  fi
  if [ ! -x "$target_release/.venv/bin/eimemory" ]; then
    echo "code_implementation_owner=failed release_cli_unavailable" >&2
    return 2
  fi
  # Hermes health must already have been verified before this helper runs.
  # The oneshot exits non-zero on a lock, registration, or live-health error.
  _user_systemctl start eimemory-code-implementation-refresh.service
  _user_systemctl start eimemory-code-implementation-refresh.timer
  if ! _user_systemctl is-active --quiet eimemory-code-implementation-refresh.timer; then
    echo "code_implementation_owner=failed timer_inactive" >&2
    return 2
  fi
  echo "code_implementation_owner=ready release=$target_release"
}

_install_current_runtime_metadata() {
  local target_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  local metadata_release="${3:-$target_release}"
  local allow_hermes_provider_only="${4:-0}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
  SERVICE_UID="$(id -u "$SERVICE_USER")"
  if ! PYTHON_RUNTIME_UNIT_OUTPUT="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" < "$target_release/deploy/discover_python_runtime_units.sh")"; then
    echo "Unable to discover Python runtime systemd units" >&2
    return 2
  fi
  local runtime_dropin_name retired_runtime_dropin_name
  if ! runtime_dropin_name="$("$PYTHON_BIN" -I -B \
      "$target_release/deploy/runtime_identity_policy.py" dropin-name)" || \
     [[ ! "$runtime_dropin_name" =~ ^[A-Za-z0-9_.-]+\.conf$ ]]; then
    echo "Unable to resolve the managed runtime identity drop-in" >&2
    return 2
  fi
  case "$runtime_dropin_name" in
    90-eimemory-python-runtime.conf)
      retired_runtime_dropin_name="zzzz-eimemory-python-runtime.conf" ;;
    zzzz-eimemory-python-runtime.conf)
      retired_runtime_dropin_name="90-eimemory-python-runtime.conf" ;;
    *)
      echo "Runtime identity policy selected an unauthorized drop-in name" >&2
      return 2 ;;
  esac
  mapfile -t PYTHON_RUNTIME_UNITS <<< "$PYTHON_RUNTIME_UNIT_OUTPUT"
  for runtime_unit in "${PYTHON_RUNTIME_UNITS[@]}"; do
    if _is_unselected_openclaw_unit "$runtime_unit"; then continue; fi
    "$PYTHON_BIN" -I -B "$metadata_release/deploy/install_managed_systemd_dropin.py" \
      --source "$metadata_release/deploy/systemd/eimemory-python-runtime.conf" \
      --target "$USER_SYSTEMD_DIR/$runtime_unit.d/$runtime_dropin_name" \
      --retire-target "$USER_SYSTEMD_DIR/$runtime_unit.d/$retired_runtime_dropin_name" \
      --root "$USER_SYSTEMD_DIR" --owner-uid "$SERVICE_UID" --render-commit "$target_commit" \
      --render-evidence-receipt-env-file "$EVIDENCE_RECEIPT_ENV_FILE"
  done
  _install_as_service_user 0644 \
    "$target_release/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"
  _install_learning_runtime_policy "$metadata_release"
  _install_code_implementation_owner_policy "$target_release"
  _install_hermes_integration \
    "$target_release" "$target_commit" "$metadata_release" "$allow_hermes_provider_only"
  _user_systemctl daemon-reload
  _user_systemctl enable eimemory-rpc.service
}

_provision_hermes_attestation() {
  if ! _hermes_is_installed; then
    echo "hermes_attestation=skipped hermes_not_installed"
    return
  fi
  "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/ensure_attestation_profile.py" \
    --registry "$HERMES_ATTESTATION_REGISTRY" \
    --hermes-token "$HERMES_ATTESTATION_TOKEN_FILE" \
    --user "$SERVICE_USER" --group "$SERVICE_GROUP"
}

_install_hermes_integration() {
  local target_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  local metadata_release="${3:-$target_release}"
  local allow_provider_only="${4:-0}"
  if ! _hermes_is_installed; then
    echo "hermes_integration=skipped hermes_not_installed"
    return
  fi
  local helper_args=(
    --release-root "$target_release" --hermes-home "$HERMES_HOME_DIR"
  )
  if [ "$allow_provider_only" = "1" ]; then
    helper_args+=(--allow-provider-only)
  fi
  _run_as_service_user env HOME="$SERVICE_HOME" HERMES_HOME="$HERMES_HOME_DIR" \
    "$HERMES_PYTHON" -I -B "$metadata_release/deploy/install_hermes_integration.py" \
      "${helper_args[@]}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  local service_uid
  service_uid="$(id -u "$SERVICE_USER")"
  local runtime_dropin_name retired_runtime_dropin_name
  if ! runtime_dropin_name="$("$PYTHON_BIN" -I -B \
      "$target_release/deploy/runtime_identity_policy.py" dropin-name)" || \
     [[ ! "$runtime_dropin_name" =~ ^[A-Za-z0-9_.-]+\.conf$ ]]; then
    echo "Unable to resolve the managed Hermes runtime identity drop-in" >&2
    return 2
  fi
  case "$runtime_dropin_name" in
    90-eimemory-python-runtime.conf)
      retired_runtime_dropin_name="zzzz-eimemory-python-runtime.conf" ;;
    zzzz-eimemory-python-runtime.conf)
      retired_runtime_dropin_name="90-eimemory-python-runtime.conf" ;;
    *)
      echo "Runtime identity policy selected an unauthorized Hermes drop-in name" >&2
      return 2 ;;
  esac
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/hermes-gateway.service.d"
  "$PYTHON_BIN" -I -B "$metadata_release/deploy/install_managed_systemd_dropin.py" \
    --source "$metadata_release/deploy/systemd/eimemory-python-runtime.conf" \
    --target "$USER_SYSTEMD_DIR/hermes-gateway.service.d/$runtime_dropin_name" \
    --retire-target "$USER_SYSTEMD_DIR/hermes-gateway.service.d/$retired_runtime_dropin_name" \
    --root "$USER_SYSTEMD_DIR" --owner-uid "$service_uid" --render-commit "$target_commit" \
    --render-evidence-receipt-env-file "$EVIDENCE_RECEIPT_ENV_FILE"
  _install_as_service_user 0644 \
    "$metadata_release/deploy/systemd/hermes-gateway-eimemory.conf" \
    "$USER_SYSTEMD_DIR/hermes-gateway.service.d/91-eimemory-hermes.conf"
  _install_as_service_user 0755 \
    "$metadata_release/deploy/systemd/hermes-gateway-eimemory.sh" \
    "$USER_SYSTEMD_DIR/hermes-gateway-eimemory.sh"
}

_refresh_current_runtime_metadata() {
  _install_current_runtime_metadata "$@"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _user_systemctl restart eimemory-rpc.service
}

_refresh_openclaw_gateway_metadata() {
  if ! _openclaw_is_enabled; then return 0; fi
  local metadata_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  local service_uid
  service_uid="$(id -u "$SERVICE_USER")"
  "$PYTHON_BIN" -I -B "$metadata_release/deploy/install_managed_systemd_dropin.py" \
    --source "$metadata_release/deploy/systemd/openclaw-gateway-eimemory.conf" \
    --target "$USER_SYSTEMD_DIR/openclaw-gateway.service.d/90-eimemory-runtime.conf" \
    --root "$USER_SYSTEMD_DIR" --owner-uid "$service_uid" --render-commit "$target_commit" \
    --render-evidence-receipt-env-file "$EVIDENCE_RECEIPT_ENV_FILE"
  _run_as_service_user rm -f \
    "$USER_SYSTEMD_DIR/openclaw-gateway.service.d/40-eimemory-prompt-bridge.conf"
}

_provision_evidence_receipt_key() {
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/ensure_evidence_receipt_key.py" \
    --path "$EVIDENCE_RECEIPT_ENV_FILE" \
    --user "$SERVICE_USER" \
    --group "$SERVICE_GROUP"
}

_find_prior_release_commit_for() {
  local deployed_commit="$1"
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/find_prior_immutable_release.py" \
    --releases-root "$INSTALL_ROOT/releases" \
    --repo-root "$REPO_DIR" \
    --deployed-commit "$deployed_commit" \
    --runtime-root "$EIMEMORY_ROOT" \
    --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
    --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
    --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER"
}

_find_prior_release_commit() {
  _find_prior_release_commit_for "$COMMIT"
}

_select_baseline_prior_commit() {
  if [[ "${PREVIOUS_COMMIT:-}" =~ ^[0-9a-fA-F]{40}$ ]] && \
     [ "$PREVIOUS_COMMIT" != "$COMMIT" ]; then
    printf '%s\n' "$PREVIOUS_COMMIT"
    return
  fi
  _find_prior_release_commit_for "$COMMIT"
}

_pause_release_closure_reconcile() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _user_systemctl stop eimemory-release-closure.path eimemory-release-closure.service \
    >/dev/null 2>&1 || true
  _user_systemctl reset-failed eimemory-release-closure.service \
    eimemory-release-closure.path >/dev/null 2>&1 || true
}

_resume_release_closure_reconcile() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _user_systemctl reset-failed eimemory-release-closure.service \
    eimemory-release-closure.path >/dev/null 2>&1 || true
  _user_systemctl start eimemory-release-closure.path
}

_restart_current_services() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  # The old release checkpoint must not race the new release's post-switch
  # closure initialization. Receipt path activation resumes afterwards.
  _pause_release_closure_reconcile
  _user_systemctl daemon-reload
  _user_systemctl restart eimemory-rpc.service
  if _openclaw_is_enabled; then
    _user_systemctl restart openclaw-gateway.service
  fi
  _restart_hermes_gateway
  # Enablement persists intent, but an enabled timer can remain inactive after
  # a first install or prior stop. Start managed loop timers only after the
  # current release and gateway are active so deployment cannot leave them idle.
  if _openclaw_is_enabled; then
    _user_systemctl start openclaw-loop-watch.timer
    _user_systemctl start openclaw-loop-compact.timer
  fi
}

_restart_hermes_gateway() {
  if ! _hermes_is_installed || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || \
     ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  if _user_systemctl cat hermes-gateway.service >/dev/null 2>&1; then
    _user_systemctl daemon-reload
    # Hermes currently reports a successful websocket close as exit status 1
    # during SIGTERM. A direct systemd restart can therefore finish the stop
    # job without starting the replacement process. Treat only that deliberate
    # stop as non-fatal, then start and verify a fresh process explicitly.
    _user_systemctl stop hermes-gateway.service >/dev/null 2>&1 || true
    _user_systemctl reset-failed hermes-gateway.service >/dev/null 2>&1 || true
    _user_systemctl start hermes-gateway.service
    local attempt main_pid gateway_pid
    for attempt in $(seq 1 60); do
      main_pid="$(_user_systemctl show hermes-gateway.service --property=MainPID --value)"
      gateway_pid=""
      if [ -r "$HERMES_HOME_DIR/gateway.pid" ]; then
        gateway_pid="$("$PYTHON_BIN" -I -B -c '
import json
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    value = text
else:
    value = payload.get("pid", "") if isinstance(payload, dict) else ""
value = str(value).strip()
if value.isdigit() and int(value) > 0:
    print(value)
' "$HERMES_HOME_DIR/gateway.pid" 2>/dev/null || true)"
      fi
      if [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] && [ "$gateway_pid" = "$main_pid" ] && \
         _user_systemctl is-active --quiet hermes-gateway.service; then
        echo "hermes_gateway_restart=ready managed_singleton=1"
        return
      fi
      sleep 1
    done
    echo "hermes_gateway_restart=failed managed_singleton_not_ready" >&2
    return 2
  fi
}

_verify_effective_runtime_metadata() {
  local target_commit="$1"
  local target_release="${2:-$RELEASE_DIR}"
  local policy_release="${3:-$target_release}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  local unit effective_commit discovered_output verification_output runtime_dropin_name
  local -a runtime_units=()
  local -a verification_args=(verification-units)
  local -a required_runtime_units=(
    eimemory-rpc.service
    eimemory-code-implementation-refresh.service
  )
  local -A seen_runtime_units=()
  if _openclaw_is_enabled; then
    required_runtime_units+=(openclaw-gateway.service openclaw-loop-watch.service)
  else
    verification_args+=(--exclude-openclaw)
  fi
  if ! discovered_output="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" \
      < "$policy_release/deploy/discover_python_runtime_units.sh")"; then
    echo "runtime_identity=failed reason=discovery_unavailable" >&2
    return 2
  fi
  if _hermes_is_installed && _user_systemctl cat hermes-gateway.service >/dev/null 2>&1; then
    verification_args+=(--include-hermes)
  fi
  if ! verification_output="$(printf '%s\n' "$discovered_output" | \
      "$PYTHON_BIN" -I -B "$policy_release/deploy/runtime_identity_policy.py" \
        "${verification_args[@]}")"; then
    echo "runtime_identity=failed reason=policy_unavailable" >&2
    return 2
  fi
  if [ -z "$verification_output" ]; then
    echo "runtime_identity=failed reason=verification_units_empty" >&2
    return 2
  fi
  mapfile -t runtime_units <<< "$verification_output"
  for unit in "${runtime_units[@]}"; do
    if [[ ! "$unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || \
       [ -n "${seen_runtime_units[$unit]:-}" ]; then
      echo "runtime_identity=failed unit=$unit reason=verification_unit_invalid" >&2
      return 2
    fi
    seen_runtime_units["$unit"]=1
  done
  if ! runtime_dropin_name="$("$PYTHON_BIN" -I -B \
      "$policy_release/deploy/runtime_identity_policy.py" dropin-name)"; then
    echo "runtime_identity=failed reason=policy_unavailable" >&2
    return 2
  fi
  if [ "$runtime_dropin_name" = "zzzz-eimemory-python-runtime.conf" ]; then
    while IFS= read -r unit; do
      if _is_unselected_openclaw_unit "$unit"; then continue; fi
      if [ -n "$unit" ] && [ -z "${seen_runtime_units[$unit]:-}" ]; then
        echo "runtime_identity=failed unit=$unit reason=discovered_unit_unverified" >&2
        return 2
      fi
    done <<< "$discovered_output"
  elif [ "$runtime_dropin_name" != "90-eimemory-python-runtime.conf" ]; then
    echo "runtime_identity=failed reason=dropin_name_unauthorized" >&2
    return 2
  fi
  if _hermes_is_installed && _user_systemctl cat hermes-gateway.service >/dev/null 2>&1; then
    required_runtime_units+=(hermes-gateway.service)
  fi
  for unit in "${required_runtime_units[@]}"; do
    if [ -z "${seen_runtime_units[$unit]:-}" ]; then
      echo "runtime_identity=failed unit=$unit reason=required_unit_unverified" >&2
      return 2
    fi
  done
  for unit in "${runtime_units[@]}"; do
    case "$unit" in
      eimemory-rpc.service|openclaw-gateway.service|hermes-gateway.service)
        if ! _user_systemctl is-active --quiet "$unit"; then
          echo "runtime_identity=failed unit=$unit reason=inactive" >&2
          return 2
        fi
        ;;
      *) ;;
    esac
    if ! effective_commit="$(
      _user_systemctl show "$unit" --property=Environment --value |
        "$PYTHON_BIN" -I -B -c '
import shlex
import sys

name = "EIMEMORY_RUNTIME_COMMIT"
try:
    assignments = shlex.split(sys.stdin.read(), posix=True)
except ValueError:
    raise SystemExit(2)
values = [
    item.split("=", 1)[1]
    for item in assignments
    if item.partition("=")[0] == name and "=" in item
]
if len(values) != 1:
    raise SystemExit(2)
print(values[0])
'
    )"; then
      echo "runtime_identity=failed unit=$unit reason=environment_unavailable" >&2
      return 2
    fi
    if [ "$effective_commit" != "$target_commit" ]; then
      echo "runtime_identity=failed unit=$unit reason=commit_mismatch" >&2
      return 2
    fi
  done
  echo "runtime_identity=verified units=${#runtime_units[@]}"
}

_verify_hermes_integration() {
  local target_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  if ! _hermes_is_installed; then
    echo "hermes_closed_loop=skipped hermes_not_installed"
    return
  fi
  local rpc_token rpc_url adapter_timeout hermes_runtime_env
  local -a hermes_runtime_values=()
  rpc_token="$("$PYTHON_BIN" -I -B -c \
    'from pathlib import Path; import sys; line=Path(sys.argv[1]).read_text(encoding="utf-8").strip(); key,sep,value=line.partition("="); sys.exit(2) if key != "EIMEMORY_RPC_AUTH_TOKEN" or not sep or not value else print(value)' \
    "$EIMEMORY_CONFIG_DIR/rpc.env")"
  hermes_runtime_env="$(_user_systemctl show hermes-gateway.service --property=Environment --value | \
    "$PYTHON_BIN" -I -B -c '
import shlex
import sys
from urllib.parse import urlsplit

try:
    assignments = shlex.split(sys.stdin.read(), posix=True)
except ValueError:
    raise SystemExit(2)

def unique_value(name):
    values = [
        item.split("=", 1)[1]
        for item in assignments
        if item.partition("=")[0] == name and "=" in item
    ]
    if len(values) != 1:
        raise SystemExit(2)
    return values[0]

rpc_url = unique_value("EIMEMORY_RPC_URL")
timeout_text = unique_value("EIMEMORY_ADAPTER_TIMEOUT_SECONDS")
parsed = urlsplit(rpc_url)
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit(2)
if parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit(2)
try:
    timeout = float(timeout_text)
except ValueError:
    raise SystemExit(2)
if not 0.1 <= timeout <= 30.0:
    raise SystemExit(2)
print(rpc_url)
print(f"{timeout:g}")
')"
  mapfile -t hermes_runtime_values <<< "$hermes_runtime_env"
  if [ "${#hermes_runtime_values[@]}" != "2" ]; then
    echo "hermes_closed_loop=failed invalid_runtime_environment" >&2
    return 2
  fi
  rpc_url="${hermes_runtime_values[0]}"
  adapter_timeout="${hermes_runtime_values[1]}"
  local -a hermes_verify_args=(
    --repo-root "$target_release" --commit "$target_commit"
    --hermes-agent-root "$HERMES_HOME_DIR/hermes-agent"
    --test-python "$REPO_DIR/.venv/bin/python"
  )
  if [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" = "1" ]; then
    hermes_verify_args+=(--expected-implementation-digest "$EIMEMORY_CODE_EVOLUTION_PROVIDER_DIGEST")
  fi
  _run_as_service_user env \
    HOME="$SERVICE_HOME" HERMES_HOME="$HERMES_HOME_DIR" \
    PYTHONPATH="$target_release:$HERMES_HOME_DIR/hermes-agent" \
    EIMEMORY_RPC_URL="$rpc_url" EIMEMORY_RPC_TOKEN="$rpc_token" \
    EIMEMORY_ADAPTER_TIMEOUT_SECONDS="$adapter_timeout" \
    EIMEMORY_ATTESTATION_HOST_PROFILE="operator-separated-v1" \
    EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE="$HERMES_ATTESTATION_TOKEN_FILE" \
    EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE="$HERMES_RECEIPT_HANDOFF_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$target_commit" \
    EIMEMORY_AGENT_ID="$EIMEMORY_DEPLOY_SCOPE_AGENT" \
    EIMEMORY_WORKSPACE_ID="$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
    EIMEMORY_USER_ID="$EIMEMORY_DEPLOY_SCOPE_USER" \
    "$HERMES_PYTHON" -I -B "$target_release/deploy/verify_hermes_integration.py" \
      "${hermes_verify_args[@]}"
  unset rpc_token rpc_url adapter_timeout hermes_runtime_env
}

_install_candidate_runtime_metadata() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
    # Migrate away from polling. The dedicated receipt signal changes only
    # after a real platform acceptance, so path activation cannot be driven by
    # unrelated high-frequency reply-ledger updates.
    _user_systemctl disable --now eimemory-release-closure.timer >/dev/null 2>&1 || true
    _run_as_service_user rm -f "$USER_SYSTEMD_DIR/eimemory-release-closure.timer"
    _user_systemctl daemon-reload
    if _openclaw_is_enabled; then
    _install_as_service_user 0644 \
      "$RELEASE_DIR/deploy/systemd/openclaw-loop-watch.service" "$USER_SYSTEMD_DIR/openclaw-loop-watch.service"
    _install_as_service_user 0644 \
      "$RELEASE_DIR/deploy/systemd/openclaw-loop-watch.timer" "$USER_SYSTEMD_DIR/openclaw-loop-watch.timer"
    _install_as_service_user 0644 \
      "$RELEASE_DIR/deploy/systemd/openclaw-loop-compact.service" "$USER_SYSTEMD_DIR/openclaw-loop-compact.service"
    _install_as_service_user 0644 \
      "$RELEASE_DIR/deploy/systemd/openclaw-loop-compact.timer" "$USER_SYSTEMD_DIR/openclaw-loop-compact.timer"
    fi
    _install_as_service_user 0644 \
      "$RELEASE_DIR/deploy/systemd/eimemory-release-closure.service" "$USER_SYSTEMD_DIR/eimemory-release-closure.service"
    _install_as_service_user 0644 \
      "$RELEASE_DIR/deploy/systemd/eimemory-release-closure.path" "$USER_SYSTEMD_DIR/eimemory-release-closure.path"
    _refresh_openclaw_gateway_metadata "$RELEASE_DIR" "$COMMIT"
    _install_current_runtime_metadata "$RELEASE_DIR" "$COMMIT" "$REPO_DIR"
    _user_systemctl daemon-reload
    _user_systemctl enable eimemory-rpc.service
    if _openclaw_is_enabled; then
      _user_systemctl enable openclaw-loop-watch.timer
      _user_systemctl enable openclaw-loop-compact.timer
    fi
    _user_systemctl enable eimemory-release-closure.path
  fi
  _install_openclaw_loop_compat_script "$RELEASE_DIR"
  _refresh_openclaw_plugin_registry
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
  local trusted_prior="${BASELINE_PRIOR_COMMIT:-${PREVIOUS_COMMIT:-}}"
  local args=(
    --repo-root "$REPO_DIR" --current-link "$CURRENT_LINK"
    --health-url "$EIMEMORY_HEALTH_URL" --prior-commit "$trusted_prior"
    --deployed-commit "$COMMIT"
    --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT"
    --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE"
    --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER" --json
  )
  if [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" = "1" ]; then
    args+=(
      --strict-transaction
      --transaction-id "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_ID"
      --authorization-digest "$EIMEMORY_CODE_EVOLUTION_AUTHORIZATION_DIGEST"
      --policy-digest "$EIMEMORY_CODE_EVOLUTION_POLICY_DIGEST"
      --patch-digest "$EIMEMORY_CODE_EVOLUTION_PATCH_DIGEST"
      --candidate-tree-digest "$EIMEMORY_CODE_EVOLUTION_CANDIDATE_TREE_DIGEST"
      --observation-deadline "$EIMEMORY_CODE_EVOLUTION_OBSERVATION_DEADLINE"
      --provider-implementation-digest "$EIMEMORY_CODE_EVOLUTION_PROVIDER_DIGEST"
      --code-evolution-lineage-json "$EIMEMORY_CODE_EVOLUTION_LINEAGE_JSON"
    )
    local receipt_digest
    IFS=',' read -r -a receipt_digests <<< "$EIMEMORY_CODE_EVOLUTION_VERIFICATION_RECEIPTS"
    for receipt_digest in "${receipt_digests[@]}"; do
      [ -n "$receipt_digest" ] && args+=(--verification-receipt-digest "$receipt_digest")
    done
  fi
  env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$RELEASE_DIR/.venv/bin/python" -I -B "$REPO_DIR/deploy/record_deployment_receipt.py" \
      "${args[@]}"
}

_record_release_lineage() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$RELEASE_DIR/.venv/bin/python" -I -B \
      "$RELEASE_DIR/deploy/record_release_lineage.py" \
      --repo-root "$REPO_DIR" --current-commit "$COMMIT" \
      --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
      --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
      --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER"
}

_capture_prior_health_snapshot() {
  if [ "$EIMEMORY_POST_SWITCH_GATES" != "1" ] || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  if [[ ! "${COMMIT:-}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "prior_health_capture=failed invalid_candidate_commit" >&2
    return 2
  fi
  local snapshot_file
  if ! snapshot_file="$(mktemp "$INSTALL_ROOT/.prior-health-${COMMIT}-XXXXXXXX.json")"; then
    echo "prior_health_capture=failed" >&2
    return 2
  fi
  PRIOR_HEALTH_SNAPSHOT_FILE="$snapshot_file"
  if ! chmod 0600 "$snapshot_file"; then
    rm -f -- "$PRIOR_HEALTH_SNAPSHOT_FILE"
    PRIOR_HEALTH_SNAPSHOT_FILE=""
    echo "prior_health_capture=failed" >&2
    return 2
  fi
  if ! "$RELEASE_DIR/.venv/bin/python" -I -B \
      "$RELEASE_DIR/deploy/capture_prior_health_snapshot.py" \
      --health-url "$EIMEMORY_HEALTH_URL" >"$snapshot_file"; then
    rm -f -- "$PRIOR_HEALTH_SNAPSHOT_FILE"
    PRIOR_HEALTH_SNAPSHOT_FILE=""
    echo "prior_health_capture=failed" >&2
    return 2
  fi
  if [ "$(id -u)" -eq 0 ]; then
    if ! chown "$SERVICE_USER:$SERVICE_GROUP" "$snapshot_file"; then
      rm -f -- "$PRIOR_HEALTH_SNAPSHOT_FILE"
      PRIOR_HEALTH_SNAPSHOT_FILE=""
      echo "prior_health_capture=failed" >&2
      return 2
    fi
  fi
}

_run_pre_switch_production_recall_bootstrap() {
  if [ "$EIMEMORY_POST_SWITCH_GATES" != "1" ] || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  local trusted_prior="${BASELINE_PRIOR_COMMIT:-${PREVIOUS_COMMIT:-}}"
  if [[ ! "$trusted_prior" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "Production recall bootstrap requires the verified prior commit" >&2
    return 2
  fi
  if [ -z "${PRIOR_HEALTH_SNAPSHOT_FILE:-}" ] || \
     [ ! -f "$PRIOR_HEALTH_SNAPSHOT_FILE" ] || \
     [ -L "$PRIOR_HEALTH_SNAPSHOT_FILE" ]; then
    echo "Production recall bootstrap requires a protected prior health snapshot" >&2
    return 2
  fi
  local bootstrap_status=0
  if _run_as_service_user env \
    EIMEMORY_ROOT="$EIMEMORY_ROOT" \
    EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/run_with_governance_env.py" \
      --env-file "$GOVERNANCE_ENV_FILE" --optional -- \
      "$RELEASE_DIR/.venv/bin/python" -I -B \
        "$RELEASE_DIR/deploy/bootstrap_production_recall.py" \
        --candidate-commit "$COMMIT" --prior-commit "$trusted_prior" \
        --current-link "$CURRENT_LINK" --health-url "$EIMEMORY_HEALTH_URL" \
        --prior-health-snapshot "$PRIOR_HEALTH_SNAPSHOT_FILE" \
        --root "$EIMEMORY_ROOT" \
        --agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
        --workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
        --user "$EIMEMORY_DEPLOY_SCOPE_USER"; then
    bootstrap_status=0
  else
    bootstrap_status=$?
  fi
  rm -f -- "$PRIOR_HEALTH_SNAPSHOT_FILE"
  PRIOR_HEALTH_SNAPSHOT_FILE=""
  return "$bootstrap_status"
}

_observe_pre_switch_l5() {
  if [ "${EIMEMORY_POST_SWITCH_GATES:-0}" != "1" ] || \
     [ "${USER_SYSTEMD_ENABLE_SERVICE:-0}" != "1" ] || \
     ! _release_closure_requested; then
    return 0
  fi
  local trusted_prior="${BASELINE_PRIOR_COMMIT:-${PREVIOUS_COMMIT:-}}"
  if ! _capture_prior_health_snapshot; then
    echo "l5_pre_switch_bootstrap=error stage=prior_health_capture" >&2
    return 0
  fi
  if [[ ! "$trusted_prior" =~ ^[0-9a-fA-F]{40}$ ]]; then
    rm -f -- "${PRIOR_HEALTH_SNAPSHOT_FILE:-}"
    PRIOR_HEALTH_SNAPSHOT_FILE=""
    echo "l5_pre_switch_bootstrap=degraded reason=prior_commit_unavailable" >&2
    return 0
  fi
  BASELINE_PRIOR_COMMIT="$trusted_prior"
  local bootstrap_status=0
  if _run_pre_switch_production_recall_bootstrap; then
    bootstrap_status=0
  else
    bootstrap_status=$?
  fi
  case "$bootstrap_status" in
    0) echo "l5_pre_switch_bootstrap=ready" ;;
    1) echo "l5_pre_switch_bootstrap=degraded exit_status=1" >&2 ;;
    *) echo "l5_pre_switch_bootstrap=error exit_status=$bootstrap_status" >&2 ;;
  esac
  return 0
}

_release_closure_requested() {
  case "${EIMEMORY_RELEASE_CLOSURE_MODE:-auto}" in
    always) return 0 ;;
    never) return 1 ;;
  esac
  if [ "${EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE:-0}" = "1" ]; then
    return 0
  fi
  local trusted_prior="${BASELINE_PRIOR_COMMIT:-${PREVIOUS_COMMIT:-}}"
  if [[ ! "$trusted_prior" =~ ^[0-9a-fA-F]{40}$ ]] || \
     [[ ! "${COMMIT:-}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    return 0
  fi
  local changed_paths
  if ! changed_paths="$(git -C "$REPO_DIR" diff --name-only "$trusted_prior..$COMMIT" --)"; then
    return 0
  fi
  local changed_count=0 path
  while IFS= read -r path; do
    [ -z "$path" ] && continue
    changed_count=$((changed_count + 1))
    case "$path" in
      eimemory/governance/release_closure.py|\
      eimemory/governance/release_lineage.py|\
      eimemory/governance/closure_rehearsal.py|\
      eimemory/governance/l5_product_completion.py|\
      eimemory/governance/l5_reader.py|\
      eimemory/governance/code_evolution_transaction.py|\
      eimemory/evaluation/production_*|\
      deploy/install_immutable_release.sh|\
      deploy/summarize_release_closure.py)
        return 0
        ;;
    esac
  done <<<"$changed_paths"
  [ "$changed_count" -ge 40 ]
}

_run_post_switch_closure() {
  if [ "$EIMEMORY_POST_SWITCH_GATES" != "1" ] || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
  fi
  if ! _release_closure_requested; then
    echo "release_closure=skipped mode=$EIMEMORY_RELEASE_CLOSURE_MODE reason=lightweight_release"
    return 0
  fi
  local closure_output closure_status summary_status
  local trusted_prior="${BASELINE_PRIOR_COMMIT:-${PREVIOUS_COMMIT:-}}"
  closure_output="$(mktemp "$INSTALL_ROOT/.release-closure-${COMMIT}-XXXXXXXX.json")"
  chmod 0600 "$closure_output"
  if env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/run_with_governance_env.py" \
      --env-file "$GOVERNANCE_ENV_FILE" --optional -- \
      "$RELEASE_DIR/.venv/bin/eimemory" learn release-closure \
        --repo-root "$REPO_DIR" --current-link "$CURRENT_LINK" \
        --health-url "$EIMEMORY_HEALTH_URL" --prior-commit "$trusted_prior" \
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
  if [ "$closure_status" != "0" ] || [ "$summary_status" != "0" ]; then
    if ! env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
      "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/record_release_closure_incident.py" \
        --path "$closure_output" \
        --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
        --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
        --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER"; then
      echo "warning: release closure failure incident could not be recorded" >&2
    fi
  fi
  rm -f "$closure_output"
  if [ "$summary_status" != "0" ]; then
    return "$summary_status"
  fi
  return "$closure_status"
}

_run_post_deploy_validation() {
  if [ "$EIMEMORY_POST_SWITCH_GATES" != "1" ] || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    echo "post_deploy_validation=skipped"
    [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" = "1" ] && return 2
    # The failed transaction-mode predicate above is the most recent status;
    # make the legacy/non-strict skip explicitly successful under `set -e`.
    return 0
  fi
  local degraded=0
  if ! _record_deployment_receipt || ! _maybe_fail_stage receipt; then
    echo "warning: post-deploy deployment receipt is pending retry" >&2
    degraded=1
  fi
  if ! _record_release_lineage || ! _maybe_fail_stage lineage; then
    echo "warning: post-deploy release lineage is pending retry" >&2
    degraded=1
  fi
  if ! _run_post_switch_closure || ! _maybe_fail_stage acceptance; then
    echo "warning: post-deploy business closure is pending retry" >&2
    degraded=1
  fi
  if [ "$degraded" = "1" ]; then
    echo "post_deploy_validation=degraded"
  else
    echo "post_deploy_validation=complete"
  fi
  if [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" = "1" ] && [ "$degraded" = "1" ]; then
    return 2
  fi
}

_rollback_current_release() {
  local rollback_failed=0
  local link_restored=1
  local rollback_mode="${1:-restore}"
  case "$rollback_mode" in
    restore) ;;
    resume_validation)
      # The validated journal classifier, not an environment switch, selects
      # this path. Never replay an old snapshot over new prior-release writes.
      if [ "$STORAGE_TRANSACTION_ACTIVE" != "1" ] || \
         [ "$(realpath -e -- "$CURRENT_LINK")" != "$PREVIOUS_CURRENT" ] || \
         [ -e "$EIMEMORY_ROOT/state/.storage-restore-journal.json" ] || \
         [ -L "$EIMEMORY_ROOT/state/.storage-restore-journal.json" ]; then
        echo "rollback_resume=failed restore_not_durably_complete" >&2
        return 1
      fi
      case "${STORAGE_TRANSACTION_PHASE:-}" in
        rollback_storage_restored|rollback_metadata_ready|rollback_validating) ;;
        *) echo "rollback_resume=failed invalid_phase" >&2; return 1 ;;
      esac
      STORAGE_RESTORED=1
      echo "rollback_resume=validation_only preserve_current_prior_data=1"
      ;;
    *) echo "rollback_resume=failed invalid_mode" >&2; return 1 ;;
  esac
  if ! rm -f "$CURRENT_LINK.next" 2>/dev/null; then
    echo "rollback_step=cleanup_next status=failed" >&2
    rollback_failed=1
  fi
  if [ "$STORAGE_TRANSACTION_ACTIVE" != "1" ] && \
     [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    if ! _capture_storage_writers; then
      echo "rollback_step=capture_writers status=failed" >&2
      return 1
    fi
    if ! _begin_storage_release_transaction; then
      echo "rollback_step=begin_transaction status=failed" >&2
      return 1
    fi
  fi
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ] && [ "$rollback_mode" = "restore" ]; then
    if [ "$STORAGE_SNAPSHOT_READY" = "1" ]; then
      if ! _update_storage_release_transaction rollback_started 1 "$STORAGE_VACUUM_BACKUP"; then
        echo "rollback_step=mark_started status=failed" >&2
        return 1
      fi
    else
      if ! _update_storage_release_transaction rollback_started 0; then
        echo "rollback_step=mark_started status=failed" >&2
        return 1
      fi
    fi
  fi
  if [ "$STORAGE_SNAPSHOT_READY" = "1" ] || \
     { [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; }; then
    if ! _stop_storage_writers; then
      echo "rollback_step=stop_writers status=failed" >&2
      return 1
    fi
  fi
  if [ "$CURRENT_SWITCHED" = "1" ] && [ "$rollback_mode" = "restore" ]; then
    if [ -z "${PREVIOUS_CURRENT:-}" ] || [ ! -d "$PREVIOUS_CURRENT" ]; then
      echo "rollback_current_release=unavailable_no_previous" >&2
      link_restored=0
    elif [ "${EIMEMORY_DEPLOY_FAIL_ROLLBACK_STAGE:-}" = "link" ] || \
       ! { ln -sfn "$PREVIOUS_CURRENT" "$CURRENT_LINK.next" && mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"; }; then
      echo "rollback_step=restore_link status=failed" >&2
      link_restored=0
    else
      if ! _fsync_install_root; then
        echo "rollback_step=sync_link status=failed" >&2
        return 1
      fi
      if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
        if ! _update_storage_release_transaction rollback_link_restored \
          "$([ "$STORAGE_SNAPSHOT_READY" = "1" ] && printf 1 || printf 0)" \
          "$STORAGE_VACUUM_BACKUP"; then
          echo "rollback_step=mark_link_restored status=failed" >&2
          return 1
        fi
      fi
    fi
  fi
  if [ "$STORAGE_SNAPSHOT_READY" = "1" ] && [ "$rollback_mode" = "restore" ]; then
    # Restore all candidate durable writes before any metadata refresh or old
    # service start. This also covers code-only capability lifecycle updates.
    if ! _restore_storage_snapshot; then
      echo "rollback_step=storage_snapshot status=failed" >&2
      return 1
    fi
  fi
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ] && [ "$rollback_mode" = "restore" ]; then
    if ! _update_storage_release_transaction rollback_storage_restored \
      "$([ "$STORAGE_SNAPSHOT_READY" = "1" ] && printf 1 || printf 0)" \
      "$STORAGE_VACUUM_BACKUP"; then
      echo "rollback_step=mark_storage_restored status=failed" >&2
      return 1
    fi
  fi
  if [ "$STORAGE_SNAPSHOT_READY" = "1" ] && [ -n "$STORAGE_VACUUM_BACKUP" ]; then
    if ! _cleanup_storage_vacuum_backup; then
      echo "rollback_step=vacuum_backup_cleanup status=failed" >&2
      rollback_failed=1
    fi
  fi
  if [ "$link_restored" != "1" ]; then
    echo "rollback_current_release=failed" >&2
    return 1
  fi
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    if ! _refresh_openclaw_gateway_metadata "$REPO_DIR" "$PREVIOUS_COMMIT"; then
      echo "rollback_step=gateway_metadata status=failed" >&2
      rollback_failed=1
    fi
    if ! _install_current_runtime_metadata \
      "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT" "$REPO_DIR" 1; then
      echo "rollback_step=runtime_metadata status=failed" >&2
      rollback_failed=1
    fi
    if ! _install_openclaw_loop_compat_script "$PREVIOUS_CURRENT"; then
      echo "rollback_step=compat_script status=failed" >&2
      rollback_failed=1
    fi
    if ! _install_openclaw_bundled_bridge "$PREVIOUS_CURRENT" "$REPO_DIR"; then
      echo "rollback_step=external_bridge status=failed" >&2
      rollback_failed=1
    fi
    if ! _refresh_openclaw_plugin_registry; then
      echo "rollback_step=plugin_registry status=failed" >&2
      rollback_failed=1
    fi
  fi
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ] && \
     { [ "$rollback_mode" = "restore" ] || [ "${STORAGE_TRANSACTION_PHASE:-}" != "rollback_validating" ]; }; then
    if ! _update_storage_release_transaction rollback_metadata_ready \
      "$([ "$STORAGE_SNAPSHOT_READY" = "1" ] && printf 1 || printf 0)"; then
      echo "rollback_step=mark_metadata_ready status=failed" >&2
      return 1
    fi
  fi
  if [ "$rollback_failed" != "0" ]; then
    echo "rollback_current_release=failed" >&2
    return 1
  fi
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
    if ! _acquire_candidate_validation_lock; then
      echo "rollback_step=validation_lock status=failed" >&2
      return 1
    fi
    if ! _update_storage_release_transaction rollback_validating \
      "$([ "$STORAGE_SNAPSHOT_READY" = "1" ] && printf 1 || printf 0)"; then
      echo "rollback_step=mark_validating status=failed" >&2
      return 1
    fi
  fi
  if ! _restart_storage_writers; then
    echo "rollback_step=background_writers status=failed" >&2
    rollback_failed=1
  fi
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    if ! _restart_current_services; then
      echo "rollback_step=selected_services_restart status=failed" >&2
      rollback_failed=1
    elif ! _verify_hermes_integration "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT"; then
      echo "rollback_step=hermes_verify status=failed" >&2
      rollback_failed=1
    elif ! _start_code_implementation_owner "$PREVIOUS_CURRENT"; then
      echo "rollback_step=code_implementation_owner status=failed" >&2
      rollback_failed=1
    fi
  fi
  if ! _inspect_openclaw_plugin_runtime "$PREVIOUS_CURRENT" "$REPO_DIR" "1"; then
    echo "rollback_step=plugin_runtime status=failed" >&2
    rollback_failed=1
  fi
  if ! _verify_effective_runtime_metadata "$PREVIOUS_COMMIT" "$PREVIOUS_CURRENT" "$REPO_DIR"; then
    echo "rollback_step=runtime_identity status=failed" >&2
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
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
    if ! _update_storage_release_transaction rollback_validated \
      "$([ "$STORAGE_SNAPSHOT_READY" = "1" ] && printf 1 || printf 0)"; then
      echo "rollback_step=mark_validated status=failed" >&2
      return 1
    fi
    if ! _clear_storage_release_transaction; then
      echo "rollback_step=clear_transaction status=failed" >&2
      return 1
    fi
  fi
  if [ "$CURRENT_SWITCHED" = "1" ]; then
    echo "rollback_current_release=restored target=$PREVIOUS_CURRENT" >&2
  else
    echo "rollback_storage_release=restored_before_switch" >&2
  fi
}

if [[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Commit must be a full 40-character SHA: $COMMIT" >&2
  exit 2
fi

if ! git -C "$REPO_DIR" rev-parse --verify "$COMMIT^{commit}" >/dev/null 2>&1; then
  echo "Unknown commit: $COMMIT" >&2
  exit 2
fi
_code_evolution_deploy_controls_match() {
  if [ "$DEPLOY_MODE" = "--recover-only" ]; then
    # Recovery intentionally uses the newer, committed repair controls, not
    # the failed release's buggy installer. It cannot produce a new receipt.
    git -C "$REPO_DIR" diff --quiet HEAD -- deploy
    return
  fi
  if git -C "$REPO_DIR" diff --quiet "$COMMIT" -- deploy; then
    return 0
  fi
  if [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" != "1" ]; then
    return 1
  fi
  local base_commit changed_deploy_paths
  base_commit="$(git -C "$REPO_DIR" rev-parse HEAD)"
  if [ "$(git -C "$REPO_DIR" rev-parse "$COMMIT^")" != "$base_commit" ]; then
    return 1
  fi
  changed_deploy_paths="$(git -C "$REPO_DIR" diff --name-only "$COMMIT" -- deploy)"
  [ "$changed_deploy_paths" = "deploy/runtime_identity_policy.py" ]
}

if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && \
   ! _code_evolution_deploy_controls_match; then
  echo "Tracked deployment control files must match the target commit" >&2
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
_acquire_storage_deploy_lock

PREVIOUS_CURRENT=""
PREVIOUS_COMMIT=""
BASELINE_PRIOR_COMMIT=""
if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ] || [ -d "$CURRENT_LINK" ]; then
  if ! PREVIOUS_CURRENT="$(realpath -e -- "$CURRENT_LINK" 2>/dev/null)"; then
    echo "Current release link is dangling or unresolvable: $CURRENT_LINK" >&2
    exit 2
  fi
  PREVIOUS_COMMIT="$(basename "$PREVIOUS_CURRENT")"
fi

STAGE_DIR=""
BACKUP_DIR=""
FINAL_REPLACED=0
CURRENT_SWITCHED=0
OPENCLAW_CONFIG_SWITCHED=0
OPENCLAW_CONFIG_RESTORED=1
COMMITTED=0
ROLLBACK_RESTORED=0
STORAGE_SNAPSHOT_READY=0
STORAGE_RESTORED=0
STORAGE_SNAPSHOT_MANIFEST_SHA256=""
STORAGE_VACUUM_BACKUP=""
STORAGE_WRITERS_CAPTURED=0
STORAGE_WRITERS_STOPPED=0
STORAGE_WRITERS_RELOADED=0
STORAGE_MIGRATION_REQUIRED=0
PRIOR_HEALTH_SNAPSHOT_FILE=""
STORAGE_TRANSACTION_ACTIVE=0

_ensure_runtime_dir "$EIMEMORY_ROOT" 0750
_resolve_openclaw_adapter
if [ "$DEPLOY_MODE" = "--recover-only" ] && \
   [ ! -e "$STORAGE_TRANSACTION_MARKER" ] && [ ! -L "$STORAGE_TRANSACTION_MARKER" ] && \
   [ ! -e "$STORAGE_TRANSACTION_CLEARING" ] && [ ! -L "$STORAGE_TRANSACTION_CLEARING" ] && \
   [ ! -e "$STORAGE_TRANSACTION_RECOVERY" ] && [ ! -L "$STORAGE_TRANSACTION_RECOVERY" ]; then
  echo "storage_release_recovery=failed no_pending_transaction" >&2
  exit 2
fi
_install_storage_release_guards
_reconcile_interrupted_storage_release
if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ] || [ -d "$CURRENT_LINK" ]; then
  PREVIOUS_CURRENT="$(realpath -e -- "$CURRENT_LINK")"
  PREVIOUS_COMMIT="$(basename "$PREVIOUS_CURRENT")"
fi
if [ "$DEPLOY_MODE" = "--recover-only" ]; then
  _restart_current_services
  _verify_effective_runtime_metadata "$PREVIOUS_COMMIT" "$PREVIOUS_CURRENT" "$REPO_DIR"
  _verify_release_health "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT"
  echo "storage_release_recovery=verified commit=$PREVIOUS_COMMIT"
  exit 0
fi
BASELINE_PRIOR_COMMIT="$(_select_baseline_prior_commit)"

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
    BASELINE_PRIOR_COMMIT="$(_find_prior_release_commit)"
  fi
  _provision_hermes_attestation
  _install_hermes_integration "$RELEASE_DIR" "$COMMIT" "$RELEASE_DIR"
  _install_code_implementation_owner_policy "$RELEASE_DIR"
  _restart_hermes_gateway
  _verify_effective_runtime_metadata "$COMMIT"
  _verify_hermes_integration "$RELEASE_DIR" "$COMMIT"
  _start_code_implementation_owner "$RELEASE_DIR"
  # The official replay imports release-bound Hermes plugins. Verify the
  # immutable tree afterwards so bytecode or other runtime artifacts cannot
  # appear after the technical release check and before the receipt.
  _verify_release_health "$RELEASE_DIR" "$COMMIT"
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
   [[ ! "$BASELINE_PRIOR_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Post-switch gates require a trusted prior immutable release commit" >&2
  exit 2
fi

STAGE_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.eimemory-stage-${COMMIT}-XXXXXXXX")"
chmod 0700 "$STAGE_DIR"
cleanup_stage() {
  local exit_code=$?
  trap - EXIT
  set +e
  if [ "$COMMITTED" != "1" ] && \
     { [ "$CURRENT_SWITCHED" = "1" ] || [ "$STORAGE_SNAPSHOT_READY" = "1" ]; }; then
    if _rollback_current_release; then
      ROLLBACK_RESTORED=1
    else
      echo "rollback_preserved_failed_release=$RELEASE_DIR" >&2
    fi
  elif [ "$COMMITTED" != "1" ] && [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
    if "$PYTHON_BIN" -I -B "$STORAGE_TRANSACTION_HELPER" abort-safe \
      --marker "$STORAGE_TRANSACTION_MARKER"; then
      if _clear_storage_release_transaction; then
        if ! _restart_storage_writers; then
          echo "rollback_step=partial_stop_restart status=failed" >&2
        fi
      else
        echo "storage_release_transaction=pre_destructive_abort_failed_closed clear_failed" >&2
      fi
    else
      echo "storage_release_transaction=pre_destructive_abort_failed_closed unsafe_marker" >&2
    fi
  elif [ "$COMMITTED" != "1" ] && [ "$STORAGE_WRITERS_STOPPED" = "1" ]; then
    if ! _restart_storage_writers; then
      echo "rollback_step=partial_stop_restart status=failed" >&2
    fi
  fi
  if [ "$COMMITTED" != "1" ] && [ "$OPENCLAW_CONFIG_SWITCHED" = "1" ] && \
     [ "$CURRENT_SWITCHED" != "1" ] && [ "$STORAGE_SNAPSHOT_READY" != "1" ]; then
    # Preflight/baseline can fail before a storage transaction exists. Never
    # delete a candidate while the optional adapter still points into it.
    OPENCLAW_CONFIG_RESTORED=0
    if [ -n "$PREVIOUS_CURRENT" ] && \
       _install_openclaw_bundled_bridge "$PREVIOUS_CURRENT" "$REPO_DIR" && \
       _refresh_openclaw_plugin_registry; then
      OPENCLAW_CONFIG_RESTORED=1
    else
      echo "rollback_preserved_failed_release=$RELEASE_DIR reason=adapter_config_restore_failed" >&2
    fi
  fi
  if [ "$COMMITTED" != "1" ] && [ "$FINAL_REPLACED" = "1" ] && \
     [ "$OPENCLAW_CONFIG_RESTORED" = "1" ] && \
     { { [ "$CURRENT_SWITCHED" != "1" ] && [ "$STORAGE_SNAPSHOT_READY" != "1" ]; } || \
       [ "$ROLLBACK_RESTORED" = "1" ]; }; then
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
  if [ -n "${PRIOR_HEALTH_SNAPSHOT_FILE:-}" ]; then
    rm -f -- "$PRIOR_HEALTH_SNAPSHOT_FILE"
  fi
  exit "$exit_code"
}
trap cleanup_stage EXIT

git -C "$REPO_DIR" archive "$COMMIT" | tar -C "$STAGE_DIR" -xf -

"$PYTHON_BIN" -I -B "$STAGE_DIR/deploy/clean_release_bytecode.py" \
  --validate-source --allow-stage \
  --release-dir "$STAGE_DIR" \
  --releases-root "$INSTALL_ROOT/releases" \
  --repo-root "$REPO_DIR" \
  --commit "$COMMIT"

"$PYTHON_BIN" -I -B -m venv --clear "$STAGE_DIR/.venv"

"$STAGE_DIR/.venv/bin/python" -I -B -m pip install "$STAGE_DIR"
"$STAGE_DIR/.venv/bin/python" -I -B -m pip check
"$STAGE_DIR/.venv/bin/python" -I -B -m compileall -q "$STAGE_DIR/eimemory"
PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON_BIN" -I -B "$STAGE_DIR/deploy/clean_release_bytecode.py" \
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

"$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/clean_release_bytecode.py" \
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
_provision_hermes_attestation
if _openclaw_is_enabled; then
  "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/ensure_openclaw_bridge_config.py" \
    --path "$OPENCLAW_LOOP_CONFIG_PATH"
  # Explicit immutable external source; upstream bundled/private boundaries
  # remain intact. Validate compatibility before baseline work or stopping IO.
  OPENCLAW_CONFIG_SWITCHED=1
  _install_openclaw_bundled_bridge "$RELEASE_DIR"
  _preflight_openclaw_adapter
fi

_observe_pre_switch_l5
_prepare_storage_for_release
_retire_system_rpc_unit

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"
mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
_fsync_install_root
CURRENT_SWITCHED=1
if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
  _update_storage_release_transaction current_switched 1 "$STORAGE_VACUUM_BACKUP"
fi
_install_candidate_runtime_metadata
if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
  _update_storage_release_transaction metadata_ready 1 "$STORAGE_VACUUM_BACKUP"
  # The guard permits only the exact candidate in this phase. Keep the sealed
  # rollback identity durable until every mandatory candidate gate has passed.
  _acquire_candidate_validation_lock
  _update_storage_release_transaction candidate_validating 1 "$STORAGE_VACUUM_BACKUP"
fi
_maybe_fail_stage registry
if [ "$STORAGE_WRITERS_STOPPED" = "1" ]; then
  _restart_storage_writers
fi
_restart_current_services
_verify_effective_runtime_metadata "$COMMIT"
_maybe_fail_stage rpc_restart
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  _inspect_openclaw_plugin_runtime
fi
_maybe_fail_stage gateway_restart
_verify_hermes_integration "$RELEASE_DIR" "$COMMIT"
_start_code_implementation_owner "$RELEASE_DIR"
_verify_release_health "$RELEASE_DIR" "$COMMIT"
_maybe_fail_stage health
_maybe_fail_stage storage_writer_restart
_run_openclaw_loop_deploy_verify "$RELEASE_DIR"
_maybe_fail_stage final_health
# Receipt-path watching must resume before the long post-switch closure.
# Leaving it stopped until after `learn release-closure` made timer-monitor
# fail and blocked the only automatic reconcile path for current-commit
# Feishu receipts.
if ! _resume_release_closure_reconcile; then
  echo "warning: release-closure path resume pending retry" >&2
fi
if [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" = "1" ]; then
  if ! _run_post_deploy_validation; then
    echo "code_evolution_commit=blocked post_deploy_validation_failed" >&2
    exit 2
  fi
  if ! _resume_release_closure_reconcile; then
    echo "code_evolution_commit=blocked release_closure_reconcile_failed" >&2
    exit 2
  fi
fi
if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
  _update_storage_release_transaction candidate_validated 1 "$STORAGE_VACUUM_BACKUP"
  _clear_storage_release_transaction
fi
COMMITTED=1
echo "commit_complete=1"
trap - EXIT
if ! _cleanup_storage_vacuum_backup; then
  echo "warning: unable to remove storage vacuum backup after commit" >&2
fi
if ! _prune_storage_snapshots; then
  echo "warning: unable to prune storage snapshots after commit" >&2
fi
# Technical commit is durable. Do not keep the global install lock across
# the long post-switch business closure, or the next immutable release
# cannot start until learn release-closure finishes scanning sqlite.
_release_candidate_validation_lock
_release_storage_deploy_lock
if [ -n "$BACKUP_DIR" ] && [ -e "$BACKUP_DIR" ]; then
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
    --remove-stage --release-dir "$BACKUP_DIR" --releases-root "$INSTALL_ROOT/releases" || \
    echo "warning: unable to remove prior release backup: $BACKUP_DIR" >&2
fi

if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
  chown -h "$SERVICE_USER:$SERVICE_GROUP" "$CURRENT_LINK" 2>/dev/null || true
fi
if [ "$EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE" != "1" ]; then
  _run_post_deploy_validation
  _resume_release_closure_reconcile
fi

echo "release=$RELEASE_DIR"
echo "current=$CURRENT_LINK"
echo "commit=$COMMIT"
echo "service_user=$SERVICE_USER"
echo "user_systemd_unit=$USER_SYSTEMD_DIR/eimemory-rpc.service"
