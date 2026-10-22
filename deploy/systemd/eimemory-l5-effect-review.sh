#!/usr/bin/env bash
set -Eeuo pipefail
if [ -z "${EIMEMORY_CONTROL_PLANE:-}" ] && [ "$(id -u)" -ne 0 ] &&
   python3 -I -B -c 'import os,pwd,pathlib; p=pathlib.Path(pwd.getpwuid(os.geteuid()).pw_dir)/".config/eimemory/control-plane"; raise SystemExit(0 if any(os.path.lexists(p/n) for n in ("installation.v1.json","runtime-manager.v1.json")) else 1)'; then
  export EIMEMORY_CONTROL_PLANE=user
fi
case "${EIMEMORY_CONTROL_PLANE:-}" in ""|user) ;; *) echo user_control_plane_mode_invalid >&2; exit 2 ;; esac
CONTROL_PYCACHE_PREFIX=/var/lib/eimemory-manager/empty-pycache
CONTROL_MANAGER_LAUNCH=/usr/libexec/eimemory-manager/runtime_manager_launch.py
if [ "${EIMEMORY_CONTROL_PLANE:-}" = user ]; then
  if [ "$(id -u)" -eq 0 ]; then echo user_control_plane_nonroot_required >&2; exit 2; fi
  CONTROL_USER_HOME="$(python3 -I -B -c 'import os,pwd; print(pwd.getpwuid(os.geteuid()).pw_dir)')"
  CONTROL_PYCACHE_PREFIX="$CONTROL_USER_HOME/.local/lib/eimemory/control-plane/empty-pycache"
  CONTROL_MANAGER_LAUNCH="$CONTROL_USER_HOME/.local/lib/eimemory/control-plane/runtime_manager_launch.py"
fi

umask 077

EIMEMORY_BIN="${EIMEMORY_BIN:-/opt/eimemory/current/.venv/bin/eimemory}"
EIMEMORY_PYTHON_BIN="${EIMEMORY_PYTHON_BIN:-/opt/eimemory/current/.venv/bin/python}"
EIMEMORY_ROOT="${EIMEMORY_ROOT:-/var/lib/eimemory}"
EIMEMORY_CONFIG_DIR="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}"
EIMEMORY_REPORT_PATH="${EIMEMORY_REPORT_PATH:-$EIMEMORY_ROOT/reports/l5-48h-effect.json}"

report_dir="$(dirname "$EIMEMORY_REPORT_PATH")"
mkdir -p "$report_dir"
temporary="$(mktemp "$report_dir/.l5-48h-effect.XXXXXX")"

cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT

readiness_exit=0
managed_effect_review=0
while IFS= read -r cgroup; do
  case "$cgroup" in
    0::/system.slice/*eimemory-l5-effect-review.service)
      if [ "${EIMEMORY_CONTROL_PLANE:-}" != user ]; then managed_effect_review=1; fi ;;
    0::/user.slice/user-"$(id -u)".slice/user@"$(id -u)".service/*/eimemory-l5-effect-review.service)
      if [ "${EIMEMORY_CONTROL_PLANE:-}" = user ]; then managed_effect_review=1; fi ;;
  esac
done </proc/self/cgroup
if [ "$managed_effect_review" = 1 ]; then
  EIMEMORY_TRUSTED_REPOSITORY_ROOT="$("$EIMEMORY_PYTHON_BIN" -I -B -X pycache_prefix="$CONTROL_PYCACHE_PREFIX" \
    "$CONTROL_MANAGER_LAUNCH" --repository-root)"
  export EIMEMORY_TRUSTED_REPOSITORY_ROOT
fi
if [[ -z "${EIMEMORY_TRUSTED_REPOSITORY_ROOT:-}${EIMEMORY_DEPLOYMENT_REPO_ROOT:-}" ]]; then
  echo "trusted_repository_root_unset" >&2
  exit 1
fi
if [ "$managed_effect_review" = 1 ]; then
  # The fixed PID1 owner cannot select a console-copy override. The reviewed
  # helper requires the protected registry and actual venv/raw-source identity.
  "$EIMEMORY_PYTHON_BIN" -I -B -X pycache_prefix="$CONTROL_PYCACHE_PREFIX" \
    "$CONTROL_MANAGER_LAUNCH" --product-module \
    eimemory.cli.main learn l5-readiness --json >"$temporary" || readiness_exit=$?
else
EIMEMORY_ROOT="$EIMEMORY_ROOT" \
EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPYCACHEPREFIX="$CONTROL_PYCACHE_PREFIX" \
  "$EIMEMORY_BIN" learn l5-readiness --json >"$temporary" || readiness_exit=$?
fi

"$EIMEMORY_PYTHON_BIN" -I -B -X pycache_prefix="$CONTROL_PYCACHE_PREFIX" - "$temporary" "$readiness_exit" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
readiness_exit = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit("invalid L5 readiness report") from exc
if not isinstance(payload, dict) or payload.get("report_type") != "l5_readiness_report":
    raise SystemExit("invalid L5 readiness report")
if payload.get("schema_version") in {"l5_readiness.v4", "l5_readiness.v5"}:
    # Capturing an incomplete assessment succeeds without claiming L5 or
    # changing its evidence. The v4 reader has no legacy current_stage field.
    complete = payload.get("product_l5_complete")
    gaps = payload.get("gaps")
    if (
        payload.get("schema") != {"l5_readiness.v4":"l5.reader.v4", "l5_readiness.v5":"l5.reader.v5"}[payload["schema_version"]]
        or readiness_exit not in ({0} if complete else {0, 1})
        or type(complete) is not bool
        or payload.get("ok") is not complete
        or payload.get("status") != ("ready" if complete else "incomplete")
        or payload.get("completion_status") != ("complete" if complete else "incomplete")
        or not isinstance(gaps, list)
        or not all(isinstance(gap, str) for gap in gaps)
        or (complete and gaps)
        or not isinstance(payload.get("code_evolution"), dict)
    ):
        raise SystemExit("invalid L5 readiness report")
    print(f"l5_effect_status={payload['status']} product_l5_complete={str(complete).lower()}")
elif (
    readiness_exit != 0
    or payload.get("schema") == "l5.reader.v4"
    or "product_l5_complete" in payload
    or payload.get("ok") is not True
    or payload.get("current_stage") not in {"L3.5", "L4", "L4.5", "L5"}
):
    raise SystemExit("invalid L5 readiness report")
else:
    print(f"l5_effect_status={payload['current_stage']}")
PY

mv -f -- "$temporary" "$EIMEMORY_REPORT_PATH"
trap - EXIT
printf 'l5_effect_report=%s\n' "$EIMEMORY_REPORT_PATH"
