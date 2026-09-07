#!/usr/bin/env bash
set -Eeuo pipefail

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
EIMEMORY_ROOT="$EIMEMORY_ROOT" \
EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/runtime \
  "$EIMEMORY_BIN" learn l5-readiness --json >"$temporary" || readiness_exit=$?

"$EIMEMORY_PYTHON_BIN" -I -B - "$temporary" "$readiness_exit" <<'PY'
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
if payload.get("schema_version") == "l5_readiness.v4":
    # Capturing an incomplete assessment succeeds without claiming L5 or
    # changing its evidence. The v4 reader has no legacy current_stage field.
    complete = payload.get("product_l5_complete")
    gaps = payload.get("gaps")
    if (
        payload.get("schema") != "l5.reader.v4"
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
