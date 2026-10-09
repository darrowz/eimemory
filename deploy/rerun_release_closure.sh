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
REPO_DIR="${REPO_DIR:-/dev-project/eimemory}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
EIMEMORY_ROOT="${EIMEMORY_ROOT:-/var/lib/eimemory}"
EIMEMORY_CONFIG_DIR="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}"
EIMEMORY_LOG_DIR="${EIMEMORY_LOG_DIR:-$EIMEMORY_ROOT/logs}"
GOVERNANCE_ENV_FILE="${EIMEMORY_GOVERNANCE_ENV_FILE:-$EIMEMORY_CONFIG_DIR/governance.env}"
EVIDENCE_RECEIPT_ENV_FILE="${EIMEMORY_EVIDENCE_RECEIPT_ENV_FILE:-$EIMEMORY_CONFIG_DIR/evidence-receipt.env}"
EIMEMORY_HEALTH_URL="${EIMEMORY_HEALTH_URL:-http://127.0.0.1:8091/health}"
SCOPE_AGENT="${EIMEMORY_DEPLOY_SCOPE_AGENT:-hongtu}"
SCOPE_WORKSPACE="${EIMEMORY_DEPLOY_SCOPE_WORKSPACE:-embodied}"
SCOPE_USER="${EIMEMORY_DEPLOY_SCOPE_USER:-$(id -un)}"

prior="" register="" exit_status=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prior-commit) prior="${2:-}"; shift 2 ;;
    --register) register="${2:-}"; shift 2 ;;
    --exit-status) exit_status="${2:-}"; shift 2 ;;
    *) echo "usage: $0 --prior-commit <commit> | --register <closure.json> [--exit-status N]" >&2; exit 64 ;;
  esac
done

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
      --env-file "$GOVERNANCE_ENV_FILE" --optional -- \
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
