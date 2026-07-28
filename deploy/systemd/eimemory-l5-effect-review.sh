#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

EIMEMORY_BIN="${EIMEMORY_BIN:-/opt/eimemory/current/.venv/bin/eimemory}"
EIMEMORY_PYTHON_BIN="${EIMEMORY_PYTHON_BIN:-/opt/eimemory/current/.venv/bin/python}"
EIMEMORY_ROOT="${EIMEMORY_ROOT:-/var/lib/eimemory}"
EIMEMORY_CONFIG_DIR="${EIMEMORY_CONFIG_DIR:-/etc/eimemory}"
EIMEMORY_REPORT_PATH="${EIMEMORY_REPORT_PATH:-$HOME/.openclaw/reports/l5-48h-effect.json}"

report_dir="$(dirname "$EIMEMORY_REPORT_PATH")"
mkdir -p "$report_dir"
temporary="$(mktemp "$report_dir/.l5-48h-effect.XXXXXX")"

cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT

EIMEMORY_ROOT="$EIMEMORY_ROOT" \
EIMEMORY_CONFIG_DIR="$EIMEMORY_CONFIG_DIR" \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/runtime \
  "$EIMEMORY_BIN" learn l5-readiness --json >"$temporary"

"$EIMEMORY_PYTHON_BIN" -I -B - "$temporary" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit("invalid L5 readiness report") from exc
if (
    not isinstance(payload, dict)
    or payload.get("ok") is not True
    or payload.get("report_type") != "l5_readiness_report"
    or payload.get("current_stage") not in {"L3.5", "L4", "L4.5", "L5"}
):
    raise SystemExit("invalid L5 readiness report")
PY

mv -f -- "$temporary" "$EIMEMORY_REPORT_PATH"
trap - EXIT
printf 'l5_effect_report=%s\n' "$EIMEMORY_REPORT_PATH"
