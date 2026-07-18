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
USER_SYSTEMD_ENABLE_SERVICE="${USER_SYSTEMD_ENABLE_SERVICE:-1}"
USER_SYSTEMD_DIR="${USER_SYSTEMD_DIR:-$SERVICE_HOME/.config/systemd/user}"
SYSTEM_RPC_UNIT_PATH="${SYSTEM_RPC_UNIT_PATH:-/etc/systemd/system/eimemory-rpc.service}"
OPENCLAW_LOOP_DEPLOY_VERIFY="${OPENCLAW_LOOP_DEPLOY_VERIFY:-1}"
OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS="${OPENCLAW_LOOP_DEPLOY_LIVE_CHECKS:-0}"
OPENCLAW_LOOP_CONFIG_PATH="${OPENCLAW_LOOP_CONFIG_PATH:-$SERVICE_HOME/.openclaw/openclaw.json}"
OPENCLAW_LOOP_COMPAT_SCRIPT="${OPENCLAW_LOOP_COMPAT_SCRIPT:-$SERVICE_HOME/.openclaw/workspace/scripts/openclaw_loop.py}"
OPENCLAW_BIN="${OPENCLAW_BIN:-$SERVICE_HOME/n/bin/openclaw}"
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
  if [ -z "$OPENCLAW_LOOP_COMPAT_SCRIPT" ]; then
    return
  fi
  local compat_dir
  compat_dir="$(dirname "$OPENCLAW_LOOP_COMPAT_SCRIPT")"
  _run_as_service_user mkdir -p "$compat_dir"
  chmod +x "$RELEASE_DIR/scripts/openclaw_loop.py" 2>/dev/null || true
  _run_as_service_user rm -f "$OPENCLAW_LOOP_COMPAT_SCRIPT"
  _install_as_service_user 0755 \
    "$RELEASE_DIR/scripts/openclaw_loop.py" "$OPENCLAW_LOOP_COMPAT_SCRIPT"
}

_refresh_openclaw_plugin_registry() {
  if [ ! -x "$OPENCLAW_BIN" ]; then
    echo "openclaw_plugin_registry_refresh=skipped binary_not_found" >&2
    return
  fi
  _run_as_service_user env HOME="$SERVICE_HOME" \
    "$OPENCLAW_BIN" plugins registry --refresh --json >/dev/null
}

_refresh_current_runtime_metadata() {
  if [ "$USER_SYSTEMD_ENABLE_SERVICE" != "1" ] || ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR"
  SERVICE_UID="$(id -u "$SERVICE_USER" 2>/dev/null || id -u)"
  if ! PYTHON_RUNTIME_UNIT_OUTPUT="$(_run_as_service_user bash -s -- "$USER_SYSTEMD_DIR" < "$RELEASE_DIR/deploy/discover_python_runtime_units.sh")"; then
    echo "Unable to discover Python runtime systemd units" >&2
    return 2
  fi
  mapfile -t PYTHON_RUNTIME_UNITS <<< "$PYTHON_RUNTIME_UNIT_OUTPUT"
  for runtime_unit in "${PYTHON_RUNTIME_UNITS[@]}"; do
    _run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/$runtime_unit.d"
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/install_managed_systemd_dropin.py" \
      --source "$RELEASE_DIR/deploy/systemd/eimemory-python-runtime.conf" \
      --target "$USER_SYSTEMD_DIR/$runtime_unit.d/90-eimemory-python-runtime.conf" \
      --root "$USER_SYSTEMD_DIR" --owner-uid "$SERVICE_UID" --render-commit "$COMMIT"
  done
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"
  if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "user_systemd_restart_hint=run as $SERVICE_USER: systemctl --user restart eimemory-rpc.service"
  else
    systemctl --user daemon-reload
    systemctl --user enable eimemory-rpc.service
    systemctl --user restart eimemory-rpc.service
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

# Threat boundary: the deployment UID and its same-UID processes are trusted.
# This transaction rejects pre-existing links, other-UID writes, and partial
# failures. A hostile same-UID process requires a separate deployment account.
if { [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ] || [ -d "$CURRENT_LINK" ]; } && \
   [[ -d "$RELEASE_DIR" && ! -L "$RELEASE_DIR" ]] && \
   "$PYTHON_BIN" -I -B -c \
   'from pathlib import Path; import sys; raise SystemExit(0 if Path(sys.argv[1]).resolve(strict=True) == Path(sys.argv[2]).resolve(strict=True) else 1)' \
   "$CURRENT_LINK" "$RELEASE_DIR"; then
  _clean_existing_release_and_validate_source
  _refresh_current_runtime_metadata
  echo "release=$RELEASE_DIR"
  echo "current=$CURRENT_LINK"
  echo "commit=$COMMIT"
  echo "already_current=1"
  exit 0
fi

if [ -e "$RELEASE_DIR" ]; then
  _clean_existing_release_and_validate_source
fi

STAGE_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.eimemory-stage-${COMMIT}-XXXXXXXX")"
chmod 0700 "$STAGE_DIR"
BACKUP_DIR=""
FINAL_REPLACED=0
COMMITTED=0
cleanup_stage() {
  if [ "$COMMITTED" != "1" ] && [ "$FINAL_REPLACED" = "1" ]; then
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
  "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/install_managed_systemd_dropin.py" \
    --source "$RELEASE_DIR/deploy/systemd/openclaw-gateway-eimemory.conf" \
    --target "$USER_SYSTEMD_DIR/openclaw-gateway.service.d/90-eimemory-runtime.conf" \
    --root "$USER_SYSTEMD_DIR" --owner-uid "$SERVICE_UID" --render-commit "$COMMIT"
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
      --root "$USER_SYSTEMD_DIR" --owner-uid "$SERVICE_UID" --render-commit "$COMMIT"
  done
  _install_as_service_user 0644 \
    "$RELEASE_DIR/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"
  if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "user_systemd_enable_hint=run as $SERVICE_USER: systemctl --user enable eimemory-rpc.service"
  else
    systemctl --user daemon-reload
    systemctl --user enable eimemory-rpc.service
    systemctl --user enable --now openclaw-loop-watch.timer
    systemctl --user enable --now openclaw-loop-compact.timer
    systemctl --user enable --now openclaw-stuck-watchdog.timer
    systemctl --user enable openclaw-feishu-reply-watchdog.service
  fi
fi
_install_openclaw_loop_compat_script

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"
mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"
COMMITTED=1
_refresh_openclaw_plugin_registry
if [ "$USER_SYSTEMD_ENABLE_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "user_systemd_restart_hint=run as $SERVICE_USER: systemctl --user restart eimemory-rpc.service"
    echo "user_systemd_reply_watchdog_restart_hint=run as $SERVICE_USER: systemctl --user restart openclaw-feishu-reply-watchdog.service"
  else
    systemctl --user restart eimemory-rpc.service
    systemctl --user restart openclaw-feishu-reply-watchdog.service
    systemctl --user try-restart openclaw-gateway.service
  fi
fi
if [ -n "$BACKUP_DIR" ] && [ -e "$BACKUP_DIR" ]; then
  "$PYTHON_BIN" -I -B "$REPO_DIR/deploy/clean_release_bytecode.py" \
    --remove-stage --release-dir "$BACKUP_DIR" --releases-root "$INSTALL_ROOT/releases" || \
    echo "warning: unable to remove prior release backup: $BACKUP_DIR" >&2
fi
trap - EXIT

if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
  chown -h "$SERVICE_USER:$SERVICE_GROUP" "$CURRENT_LINK" 2>/dev/null || true
fi

echo "release=$RELEASE_DIR"
echo "current=$CURRENT_LINK"
echo "commit=$COMMIT"
echo "service_user=$SERVICE_USER"
echo "user_systemd_unit=$USER_SYSTEMD_DIR/eimemory-rpc.service"
