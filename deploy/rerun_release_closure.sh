#!/usr/bin/env bash
# Re-run (or register) the post-deploy business closure for the LIVE release so
# the capture is recorded exactly like the installer's own closure: the raw
# output goes through record_release_closure_incident.py into
# $EIMEMORY_LOG_DIR/release-closure-captures with an attempt id, instead of
# being an orphaned JSON file.
#
#   rerun_release_closure.sh --prior-commit <40-hex>            # run + register
#   rerun_release_closure.sh --register <closure.json> [--exit-status N]
#                                                               # register an existing capture
#
# Run as the service user (e.g. inside `systemd-run --user --wait --pipe`).
set -Eeuo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/opt/eimemory}"
CURRENT_LINK="$INSTALL_ROOT/current"
REPO_DIR="${REPO_DIR:-${EIMEMORY_TRUSTED_REPOSITORY_ROOT:-$(pwd)}}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
EIMEMORY_ROOT="${EIMEMORY_ROOT:-/var/lib/eimemory}"
EIMEMORY_CONFIG_DIR="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}"
EIMEMORY_LOG_DIR="${EIMEMORY_LOG_DIR:-$EIMEMORY_ROOT/logs}"
GOVERNANCE_ENV_FILE="${EIMEMORY_GOVERNANCE_ENV_FILE:-$EIMEMORY_CONFIG_DIR/governance.env}"
EVIDENCE_RECEIPT_ENV_FILE="${EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE:-$EIMEMORY_CONFIG_DIR/evidence-receipt.env}"
EIMEMORY_HEALTH_URL="${EIMEMORY_HEALTH_URL:-http://127.0.0.1:8091/health}"
# shellcheck source=deploy/deploy_scope_default.sh
. "$(dirname "${BASH_SOURCE[0]}")/deploy_scope_default.sh"
SCOPE_AGENT="${EIMEMORY_DEPLOY_SCOPE_AGENT:-$(eimemory_default_deploy_scope_agent)}"
SCOPE_WORKSPACE="${EIMEMORY_DEPLOY_SCOPE_WORKSPACE:-$(eimemory_default_deploy_scope_workspace)}"
SCOPE_USER="${EIMEMORY_DEPLOY_SCOPE_USER:-$(eimemory_default_deploy_scope_user "$(id -un)")}"

original_args=("$@")
prior="" register="" exit_status=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prior-commit) prior="${2:-}"; shift 2 ;;
    --register) register="${2:-}"; shift 2 ;;
    --exit-status) exit_status="${2:-}"; shift 2 ;;
    *) echo "usage: $0 --prior-commit <commit> | --register <closure.json> [--exit-status N]" >&2; exit 64 ;;
  esac
done

if [ -z "$register" ] && [[ ! "$prior" =~ ^[0-9a-f]{40}$ ]]; then
  echo "--prior-commit <40-hex> is required to run a closure" >&2
  exit 64
fi
# A root operator must not create root-owned database/capture state. Delegate
# the whole operation before creating temporary files or invoking any Python.
if [ "$(id -u)" -eq 0 ]; then
  database="$EIMEMORY_ROOT/state/eimemory.sqlite"
  if [ ! -f "$database" ] || [ -L "$database" ]; then
    echo "a regular live SQLite database is required to resolve the service user" >&2
    exit 2
  fi
  owner_uid="$(stat -c '%u' -- "$database")"
  service_user="$(id -nu "$owner_uid")"
  if [ -n "${SERVICE_USER:-}" ] && [ "$(id -u "$SERVICE_USER")" != "$owner_uid" ]; then
    echo "SERVICE_USER does not own the live SQLite database" >&2
    exit 2
  fi
  if [ "$owner_uid" != "0" ]; then
    exec runuser -u "$service_user" -- env \
      INSTALL_ROOT="$INSTALL_ROOT" REPO_DIR="$REPO_DIR" PYTHON_BIN="$PYTHON_BIN" \
      EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
      EIMEMORY_LOG_DIR="$EIMEMORY_LOG_DIR" EIMEMORY_HEALTH_URL="$EIMEMORY_HEALTH_URL" \
      EIMEMORY_GOVERNANCE_ENV_FILE="$GOVERNANCE_ENV_FILE" \
      EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
      bash "$0" "${original_args[@]}"
  fi
fi

RELEASE_DIR="$(readlink -f "$CURRENT_LINK")"
COMMIT="$(basename "$RELEASE_DIR")"
if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "current release is not an immutable commit directory: $RELEASE_DIR" >&2
  exit 2
fi
ATTEMPT_ID="${COMMIT}-manual-$(date -u +%Y%m%dT%H%M%SZ)-$$"

if [ -n "$register" ]; then
  [ -f "$register" ] || { echo "capture not found: $register" >&2; exit 66; }
  closure_output="$register"
  closure_status="${exit_status:-0}"
else
  if [[ ! "$prior" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--prior-commit <40-hex> is required to run a closure" >&2
    exit 64
  fi
  closure_output="$(mktemp "$INSTALL_ROOT/.release-closure-${COMMIT}-XXXXXXXX.json")"
  chmod 0600 "$closure_output"
  set +e
  # Same invocation as install_immutable_release.sh _run_release_closure.
  env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
    EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE="$EVIDENCE_RECEIPT_ENV_FILE" \
    EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
    "$PYTHON_BIN" -I -B "$RELEASE_DIR/deploy/run_with_governance_env.py" \
      --env-file "$GOVERNANCE_ENV_FILE" --optional \
      --runtime-recall-config-dir "$EIMEMORY_CONFIG_DIR" \
      --runtime-recall-root "$EIMEMORY_ROOT" \
      --optional-runtime-authority-env-file "$EIMEMORY_CONFIG_DIR/runtime-authority.env" -- \
      "$RELEASE_DIR/.venv/bin/eimemory" learn release-closure \
        --repo-root "$REPO_DIR" --current-link "$CURRENT_LINK" \
        --health-url "$EIMEMORY_HEALTH_URL" --prior-commit "$prior" \
        --scope-agent "$SCOPE_AGENT" --scope-workspace "$SCOPE_WORKSPACE" \
        --scope-user "$SCOPE_USER" --json >"$closure_output"
  closure_status=$?
  set -e
fi

env EIMEMORY_ROOT="$EIMEMORY_ROOT" EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
  "$RELEASE_DIR/.venv/bin/python" -I -B "$RELEASE_DIR/deploy/record_release_closure_incident.py" \
  --path "$closure_output" \
  --evidence-dir "$EIMEMORY_LOG_DIR/release-closure-captures" \
  --expected-commit "$COMMIT" --attempt-id "$ATTEMPT_ID" \
  --closure-exit-status "$closure_status" \
  --scope-agent "$SCOPE_AGENT" --scope-workspace "$SCOPE_WORKSPACE" --scope-user "$SCOPE_USER"
