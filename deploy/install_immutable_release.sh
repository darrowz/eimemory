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
SYSTEM_SYSTEMD_DIR="${SYSTEM_SYSTEMD_DIR:-$(dirname "$SYSTEM_RPC_UNIT_PATH")}"
SYSTEM_RPC_DROPIN_DIR="${SYSTEM_RPC_DROPIN_DIR:-$SYSTEM_SYSTEMD_DIR/eimemory-rpc.service.d}"
OPENCLAW_LOOP_DEPLOY_VERIFY="${OPENCLAW_LOOP_DEPLOY_VERIFY:-1}"
OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS="${OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS:-0}"
OPENCLAW_LOOP_CONFIG_PATH="${OPENCLAW_LOOP_CONFIG_PATH:-$SERVICE_HOME/.openclaw/openclaw.json}"
OPENCLAW_LOOP_COMPAT_SCRIPT="${OPENCLAW_LOOP_COMPAT_SCRIPT:-$SERVICE_HOME/.openclaw/workspace/scripts/openclaw_loop.py}"
OPENCLAW_BIN="${OPENCLAW_BIN:-$SERVICE_HOME/n/bin/openclaw}"
EIMEMORY_POST_SWITCH_GATES="${EIMEMORY_POST_SWITCH_GATES:-1}"
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
STORAGE_TRANSACTION_MARKER="${EIMEMORY_STORAGE_TRANSACTION_MARKER:-$EIMEMORY_ROOT/state/storage-release-transaction.json}"
STORAGE_TRANSACTION_LIBEXEC="${EIMEMORY_STORAGE_TRANSACTION_LIBEXEC:-$INSTALL_ROOT/libexec}"
STORAGE_TRANSACTION_HELPER="$STORAGE_TRANSACTION_LIBEXEC/storage-release-transaction.py"
STORAGE_DEPLOY_LOCK_PATH="${EIMEMORY_STORAGE_DEPLOY_LOCK_PATH:-$INSTALL_ROOT/.storage-release-install.lock}"

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

STORAGE_WRITER_UNITS=(
  eimemory-nightly.timer
  eimemory-learn-watch.timer
  eimemory-learn-think.timer
  eimemory-learn-dashboard.timer
  eimemory-l5-observation-gate.timer
  eimemory-audit-verify.timer
  eimemory-timer-monitor.timer
  eimemory-experience-autopromote.timer
  openclaw-loop-watch.timer
  openclaw-loop-compact.timer
  openclaw-stuck-watchdog.timer
  eimemory-nightly.service
  eimemory-learn-watch.service
  eimemory-learn-think.service
  eimemory-learn-dashboard.service
  eimemory-l5-observation-gate.service
  eimemory-audit-verify.service
  eimemory-timer-monitor.service
  eimemory-experience-autopromote.service
  openclaw-loop-watch.service
  openclaw-loop-compact.service
  openclaw-stuck-watchdog.service
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
    return
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
  if ! command -v flock >/dev/null 2>&1; then
    echo "storage_deploy_lock=failed flock_unavailable" >&2
    return 2
  fi
  local lock_parent resolved_parent lexical_parent parent_identity_before
  local parent_identity_after fd_identity path_identity
  lock_parent="$(dirname "$STORAGE_DEPLOY_LOCK_PATH")"
  if ! resolved_parent="$(realpath -e -- "$lock_parent")" || \
     ! lexical_parent="$(realpath -m -s -- "$lock_parent")" || \
     [ "$resolved_parent" != "$lexical_parent" ]; then
    echo "storage_deploy_lock=failed ancestor_symlink" >&2
    return 2
  fi
  if ! parent_identity_before="$(stat -Lc '%d:%i:%F' "$lock_parent")"; then
    echo "storage_deploy_lock=failed ancestor_identity" >&2
    return 2
  fi
  if [ -L "$STORAGE_DEPLOY_LOCK_PATH" ]; then
    echo "storage_deploy_lock=failed symlink" >&2
    return 2
  fi
  if ! exec {STORAGE_DEPLOY_LOCK_FD}<>"$STORAGE_DEPLOY_LOCK_PATH"; then
    echo "storage_deploy_lock=failed open" >&2
    return 2
  fi
  if [ -L "$STORAGE_DEPLOY_LOCK_PATH" ] || [ ! -f "$STORAGE_DEPLOY_LOCK_PATH" ] || \
     [ "$(stat -c %h "$STORAGE_DEPLOY_LOCK_PATH")" != "1" ]; then
    echo "storage_deploy_lock=failed unsafe_file" >&2
    return 2
  fi
  if ! flock -n "$STORAGE_DEPLOY_LOCK_FD"; then
    echo "storage_deploy_lock=contended" >&2
    return 73
  fi
  if ! resolved_parent="$(realpath -e -- "$lock_parent")" || \
     ! lexical_parent="$(realpath -m -s -- "$lock_parent")" || \
     [ "$resolved_parent" != "$lexical_parent" ]; then
    echo "storage_deploy_lock=failed ancestor_changed" >&2
    return 2
  fi
  if ! parent_identity_after="$(stat -Lc '%d:%i:%F' "$lock_parent")" || \
     [ "$parent_identity_before" != "$parent_identity_after" ]; then
    echo "storage_deploy_lock=failed ancestor_inode_changed" >&2
    return 2
  fi
  if ! fd_identity="$(stat -Lc '%d:%i:%h:%F' "/proc/$$/fd/$STORAGE_DEPLOY_LOCK_FD")" || \
     ! path_identity="$(stat -Lc '%d:%i:%h:%F' "$STORAGE_DEPLOY_LOCK_PATH")" || \
     [ "$fd_identity" != "$path_identity" ]; then
    echo "storage_deploy_lock=failed inode_changed" >&2
    return 2
  fi
  echo "storage_deploy_lock=acquired"
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
  if [ ! -e "$STORAGE_TRANSACTION_MARKER" ] && [ ! -L "$STORAGE_TRANSACTION_MARKER" ]; then
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
  local current_target current_commit migrations_complete=0 action status_report
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
  if [ "$current_commit" = "$STORAGE_TRANSACTION_CANDIDATE_COMMIT" ] && \
     [[ "$STORAGE_TRANSACTION_PHASE" == rollback_* ]]; then
    migrations_complete=1
  elif [ "$current_commit" = "$STORAGE_TRANSACTION_CANDIDATE_COMMIT" ]; then
    if [ -e "$EIMEMORY_ROOT/state/.storage-vacuum-journal.json" ] || \
       [ -L "$EIMEMORY_ROOT/state/.storage-vacuum-journal.json" ]; then
      _storage_release_action recover-vacuum
    fi
    if status_report="$(_storage_release_action status)"; then
      printf '%s\n' "$status_report"
      migrations_complete=1
    else
      printf '%s\n' "$status_report" >&2
      echo "storage_release_reconcile=failed candidate_storage_incomplete" >&2
      return 2
    fi
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
    restore_prior)
      STORAGE_SNAPSHOT_READY=1
      if [ -e "$EIMEMORY_ROOT/state/.storage-vacuum-journal.json" ] || \
         [ -L "$EIMEMORY_ROOT/state/.storage-vacuum-journal.json" ]; then
        _storage_release_action recover-vacuum
      fi
      _restore_storage_snapshot
      _update_storage_release_transaction rollback_storage_restored 1 "$STORAGE_VACUUM_BACKUP"
      _cleanup_storage_vacuum_backup
      _refresh_openclaw_gateway_metadata "$REPO_DIR" "$STORAGE_TRANSACTION_PRIOR_COMMIT"
      _install_current_runtime_metadata "$current_target" "$STORAGE_TRANSACTION_PRIOR_COMMIT" "$REPO_DIR"
      _install_openclaw_loop_compat_script "$current_target"
      _refresh_openclaw_plugin_registry
      _update_storage_release_transaction rollback_metadata_ready 1
      _clear_storage_release_transaction
      _restart_storage_writers
      _reset_storage_transaction_state
      echo "storage_release_reconcile=restored_prior"
      ;;
    finalize_candidate)
      STORAGE_SNAPSHOT_READY=1
      _install_candidate_runtime_metadata
      _update_storage_release_transaction metadata_ready 1 "$STORAGE_VACUUM_BACKUP"
      _cleanup_storage_vacuum_backup
      _clear_storage_release_transaction
      _restart_storage_writers
      _reset_storage_transaction_state
      echo "storage_release_reconcile=finalized_candidate"
      ;;
    resume_rollback)
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
      _rollback_current_release
      _reset_storage_transaction_state
      CURRENT_SWITCHED=0
      echo "storage_release_reconcile=resumed_rollback"
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
    if [ "$EIMEMORY_POST_SWITCH_GATES" = "1" ] && [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ]; then
      echo "storage_release_migration=failed protected_bootstrap_requires_database" >&2
      return 2
    fi
    echo "storage_release_migration=skipped database_missing"
    return
  fi
  local needs_report storage_needed bootstrap_needed=0 protected_write_needed=0
  needs_report="$(_storage_release_action needs)"
  printf '%s\n' "$needs_report"
  if [ ! -d "$EIMEMORY_STORAGE_SNAPSHOT_ROOT" ] || [ -L "$EIMEMORY_STORAGE_SNAPSHOT_ROOT" ]; then
    echo "storage_release_migration=failed unsafe_snapshot_root" >&2
    return 2
  fi
  storage_needed="$(printf '%s' "$needs_report" | \
    "$RELEASE_DIR/.venv/bin/python" -I -B -c \
      'import json,sys; print("1" if json.load(sys.stdin).get("needed") is True else "0")')"
  if [ "$EIMEMORY_POST_SWITCH_GATES" = "1" ] && [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ]; then
    bootstrap_needed=1
  fi
  if [ "$EIMEMORY_STORAGE_MIGRATION" != "1" ]; then
    if [ "$storage_needed" = "1" ]; then
      echo "storage_release_migration=blocked disabled_with_pending_migrations" >&2
      return 2
    fi
    echo "storage_release_migration=skipped disabled_no_pending_migrations"
    storage_needed=0
  fi
  if [ "$storage_needed" = "1" ] || [ "$bootstrap_needed" = "1" ]; then
    protected_write_needed=1
  fi
  if [ "$protected_write_needed" != "1" ]; then
    STORAGE_MIGRATION_REQUIRED=0
    echo "storage_release_migration=skipped no_pending_migrations"
    return
  fi
  STORAGE_MIGRATION_REQUIRED="$storage_needed"
  if [ "$bootstrap_needed" = "1" ]; then
    if ! _capture_prior_health_snapshot; then
      echo "prior_health_capture=failed before_transaction" >&2
      return 2
    fi
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
  snapshot_report="$(_storage_release_action snapshot)"
  printf '%s\n' "$snapshot_report"
  STORAGE_SNAPSHOT_MANIFEST_SHA256="$(printf '%s' "$snapshot_report" | \
    "$RELEASE_DIR/.venv/bin/python" -I -B -c \
      'import json,re,sys; value=str(json.load(sys.stdin).get("manifest_sha256") or ""); sys.exit(2) if re.fullmatch(r"[0-9a-f]{64}", value) is None else print(value)')"
  # The snapshot operation itself is read-only against live storage. Arm the
  # rollback trap only after its immutable identity has been parsed safely.
  STORAGE_SNAPSHOT_READY=1
  _update_storage_release_transaction snapshot_ready
  _maybe_fail_stage storage_snapshot
  _update_storage_release_transaction storage_destructive 1
  if [ "$bootstrap_needed" = "1" ]; then
    _run_pre_switch_production_recall_bootstrap
    _maybe_fail_stage pre_switch_recall_bootstrap
  fi
  if [ "$storage_needed" != "1" ]; then
    echo "storage_release_migration=skipped no_pending_migrations_after_protected_bootstrap"
    return
  fi
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
  _storage_release_action restore
  STORAGE_RESTORED=1
  echo "storage_snapshot_restore=complete snapshot=$STORAGE_SNAPSHOT_DIR" >&2
}

_cleanup_storage_vacuum_backup() {
  if [ -z "$STORAGE_VACUUM_BACKUP" ]; then
    return
  fi
  _storage_release_action cleanup-vacuum --backup-path "$STORAGE_VACUUM_BACKUP"
  STORAGE_VACUUM_BACKUP=""
}

_prune_storage_snapshots() {
  if [ "$STORAGE_SNAPSHOT_READY" != "1" ] || \
     [ "$EIMEMORY_STORAGE_MIGRATION" != "1" ] || \
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

_install_current_runtime_metadata() {
  local target_release="${1:-$RELEASE_DIR}"
  local target_commit="${2:-$COMMIT}"
  local metadata_release="${3:-$target_release}"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
  SERVICE_UID="$(id -u "$SERVICE_USER")"
  if ! PYTHON_RUNTIME_UNIT_OUTPUT="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" < "$target_release/deploy/discover_python_runtime_units.sh")"; then
    echo "Unable to discover Python runtime systemd units" >&2
    return 2
  fi
  mapfile -t PYTHON_RUNTIME_UNITS <<< "$PYTHON_RUNTIME_UNIT_OUTPUT"
  for runtime_unit in "${PYTHON_RUNTIME_UNITS[@]}"; do
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
}

_refresh_current_runtime_metadata() {
  _install_current_runtime_metadata "$@"
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _user_systemctl restart eimemory-rpc.service
}

_refresh_openclaw_gateway_metadata() {
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
  # Feishu reply watchdog permanently removed: dual-path delivery caused double sends.
  # Keep unit masked; do not install/enable/restart it on deploy.
  _user_systemctl restart openclaw-gateway.service
}

_install_candidate_runtime_metadata() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
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
    # openclaw-feishu-reply-watchdog intentionally not installed/enabled.
    _refresh_openclaw_gateway_metadata "$RELEASE_DIR" "$COMMIT"
    _install_current_runtime_metadata "$RELEASE_DIR" "$COMMIT" "$REPO_DIR"
    _user_systemctl enable eimemory-rpc.service
    _user_systemctl enable openclaw-loop-watch.timer
    _user_systemctl enable openclaw-loop-compact.timer
    _user_systemctl enable openclaw-stuck-watchdog.timer
    _user_systemctl daemon-reload
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

_capture_prior_health_snapshot() {
  if [ "$EIMEMORY_POST_SWITCH_GATES" != "1" ] || [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ]; then
    return
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
  if [[ ! "$PREVIOUS_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "Production recall bootstrap requires the verified prior commit" >&2
    return 2
  fi
  if [ -z "$PRIOR_HEALTH_SNAPSHOT_FILE" ] || [ ! -f "$PRIOR_HEALTH_SNAPSHOT_FILE" ] || \
     [ -L "$PRIOR_HEALTH_SNAPSHOT_FILE" ]; then
    echo "Production recall bootstrap requires a protected prior health snapshot" >&2
    return 2
  fi
  local bootstrap_status=0
  if _run_as_service_user env \
    EIMEMORY_ROOT="$EIMEMORY_ROOT" \
    EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/run_with_governance_env.py" \
      --env-file "$GOVERNANCE_ENV_FILE" --optional -- \
      "$RELEASE_DIR/.venv/bin/python" -I -B \
        "$RELEASE_DIR/deploy/bootstrap_production_recall.py" \
        --candidate-commit "$COMMIT" --prior-commit "$PREVIOUS_COMMIT" \
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
  local link_restored=1
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
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
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
  if [ "$CURRENT_SWITCHED" = "1" ]; then
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
  # This must complete before any metadata refresh or old service start. The
  # previous release (including 1.9.80) relies on full payload_json rows.
  if ! _restore_storage_snapshot; then
    echo "rollback_step=storage_snapshot status=failed" >&2
    return 1
  fi
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
    if ! _update_storage_release_transaction rollback_storage_restored \
      "$([ "$STORAGE_SNAPSHOT_READY" = "1" ] && printf 1 || printf 0)" \
      "$STORAGE_VACUUM_BACKUP"; then
      echo "rollback_step=mark_storage_restored status=failed" >&2
      return 1
    fi
  fi
  if ! _cleanup_storage_vacuum_backup; then
    echo "rollback_step=vacuum_backup_cleanup status=failed" >&2
    rollback_failed=1
  fi
  if [ "$link_restored" != "1" ]; then
    echo "rollback_current_release=failed" >&2
    return 1
  fi
  if ! _refresh_openclaw_gateway_metadata "$REPO_DIR" "$PREVIOUS_COMMIT"; then
    echo "rollback_step=gateway_metadata status=failed" >&2
    rollback_failed=1
  fi
  if ! _install_current_runtime_metadata "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT" "$REPO_DIR"; then
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
  if [ "$STORAGE_TRANSACTION_ACTIVE" = "1" ]; then
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
    if ! _clear_storage_release_transaction; then
      echo "rollback_step=clear_transaction status=failed" >&2
      return 1
    fi
  fi
  if ! _restart_storage_writers; then
    echo "rollback_step=background_writers status=failed" >&2
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
_acquire_storage_deploy_lock

PREVIOUS_CURRENT=""
PREVIOUS_COMMIT=""
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
_install_storage_release_guards
_reconcile_interrupted_storage_release
if [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ] || [ -d "$CURRENT_LINK" ]; then
  PREVIOUS_CURRENT="$(realpath -e -- "$CURRENT_LINK")"
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
  _run_post_switch_closure
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
  if [ "$COMMITTED" != "1" ] && [ "$FINAL_REPLACED" = "1" ] && \
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
  if [ -n "$PRIOR_HEALTH_SNAPSHOT_FILE" ]; then
    rm -f -- "$PRIOR_HEALTH_SNAPSHOT_FILE"
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
  _clear_storage_release_transaction
fi
_maybe_fail_stage registry
if [ "$STORAGE_WRITERS_STOPPED" = "1" ]; then
  _restart_storage_writers
else
  _restart_current_services
fi
_maybe_fail_stage rpc_restart
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  _inspect_openclaw_plugin_runtime
fi
_maybe_fail_stage gateway_restart
_verify_release_health "$RELEASE_DIR" "$COMMIT"
_maybe_fail_stage health
_maybe_fail_stage storage_writer_restart
_verify_release_health "$RELEASE_DIR" "$COMMIT"
_maybe_fail_stage final_health
_record_deployment_receipt
_maybe_fail_stage receipt
_run_post_switch_closure
_maybe_fail_stage acceptance
_cleanup_storage_vacuum_backup
_prune_storage_snapshots
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
