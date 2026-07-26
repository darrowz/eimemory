#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
release_id="$(basename "$(readlink -f /opt/eimemory/current)")"
export PYTHONPYCACHEPREFIX="/var/lib/eimemory/.pycache/$release_id"

EIMEMORY_BIN="${EIMEMORY_BIN:-/opt/eimemory/current/.venv/bin/eimemory}"
EIMEMORY_PYTHON_BIN="${EIMEMORY_PYTHON_BIN:-/opt/eimemory/current/.venv/bin/python}"
EIMEMORY_CURL_BIN="${EIMEMORY_CURL_BIN:-curl}"
NIGHTLY_UNIT="${EIMEMORY_NIGHTLY_UNIT_PATH:-$HOME/.config/systemd/user/eimemory-nightly.service}"
OPENCLAW_CONFIG="${OPENCLAW_CONFIG_PATH:-$HOME/.openclaw/openclaw.json}"
OPENCLAW_GATEWAY_DROPIN_DIR="${OPENCLAW_GATEWAY_DROPIN_DIR:-$HOME/.config/systemd/user/openclaw-gateway.service.d}"
OPENCLAW_GATEWAY_DROPIN="${OPENCLAW_GATEWAY_DROPIN:-$OPENCLAW_GATEWAY_DROPIN_DIR/eimemory-prompt-injection.conf}"
OPENCLAW_GATEWAY_UNIT="${OPENCLAW_GATEWAY_UNIT:-openclaw-gateway.service}"
GATE_TIMER="${EIMEMORY_L5_GATE_TIMER:-eimemory-l5-observation-gate.timer}"

require_file() {
  if [ ! -f "$1" ]; then
    echo "missing_file=$1" >&2
    exit 2
  fi
}

ensure_env() {
  local key="$1"
  local line="$2"
  "$EIMEMORY_PYTHON_BIN" - "$NIGHTLY_UNIT" "$key" "$line" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
replacement = sys.argv[3]
prefixes = (f"Environment={key}=", f'Environment="{key}=')
output = []
replaced = False
for existing in path.read_text(encoding="utf-8").splitlines():
    if existing.startswith(prefixes):
        if not replaced:
            output.append(replacement)
            replaced = True
        continue
    output.append(existing)
if not replaced:
    output.append(replacement)
path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
}

enable_openclaw_memory_behavior() {
  require_file "$OPENCLAW_CONFIG"
  "$EIMEMORY_PYTHON_BIN" - "$OPENCLAW_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
plugins = config.setdefault("plugins", {})
entries = plugins.setdefault("entries", {})
bridge = entries.setdefault("eimemory-bridge", {})
hooks = bridge.setdefault("hooks", {})
hooks["allowPromptInjection"] = True
hooks["allowConversationAccess"] = True
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  mkdir -p "$OPENCLAW_GATEWAY_DROPIN_DIR"
  cat >"$OPENCLAW_GATEWAY_DROPIN" <<'EOF'
[Service]
Environment=EIMEMORY_ENABLE_PROMPT_INJECTION=true
EOF
  systemctl --user daemon-reload
  systemctl --user restart "$OPENCLAW_GATEWAY_UNIT"
  "$EIMEMORY_CURL_BIN" -fsS http://127.0.0.1:18789/readyz >/dev/null
}

require_file "$EIMEMORY_BIN"
require_file "$EIMEMORY_PYTHON_BIN"
require_file "$NIGHTLY_UNIT"

readiness_json="$("$EIMEMORY_BIN" learn l5-readiness --persist --json)"
readiness_fields="$(
  printf '%s' "$readiness_json" | "$EIMEMORY_PYTHON_BIN" -c '
import json
import math
import sys

value = json.load(sys.stdin)
if not isinstance(value, dict):
    raise ValueError("readiness report must be an object")
ok = value.get("ok")
stage = value.get("current_stage")
score = value.get("readiness_score")
if not isinstance(ok, bool):
    raise ValueError("readiness ok must be a boolean")
if stage not in {"L3.5", "L4", "L4.5", "L5"}:
    raise ValueError("readiness current_stage is unsupported")
if isinstance(score, bool) or not isinstance(score, (int, float)):
    raise ValueError("readiness_score must be numeric")
if not math.isfinite(float(score)) or not 0.0 <= float(score) <= 1.0:
    raise ValueError("readiness_score must be finite and between zero and one")
if stage == "L5" and float(score) != 1.0:
    raise ValueError("L5 readiness_score must equal one")
print(f"{str(ok).lower()}\t{stage}\t{score}")
'
)"
IFS=$'\t' read -r readiness_ok stage readiness_score <<<"$readiness_fields"
if [ "$readiness_ok" != "true" ]; then
  echo "readiness_ok=false" >&2
  exit 3
fi

"$EIMEMORY_BIN" ops timer-monitor --stale-after-minutes 90 >/tmp/eimemory-l5-observation-gate-timer-monitor.json

if ! failed_units="$(systemctl --user --failed --no-legend 'eimemory*' 2>&1)"; then
  printf '%s\n' "$failed_units" >&2
  exit 4
fi
if [ -n "$failed_units" ]; then
  printf '%s\n' "$failed_units" >&2
  exit 4
fi

if [ "$stage" != "L5" ]; then
  echo "status=observation_pending"
  echo "stage=$stage"
  echo "readiness_score=$readiness_score"
  exit 0
fi

require_file "$OPENCLAW_CONFIG"
activation_backup_dir="$(mktemp -d)"
cp -p -- "$NIGHTLY_UNIT" "$activation_backup_dir/nightly.service"
cp -p -- "$OPENCLAW_CONFIG" "$activation_backup_dir/openclaw.json"
dropin_existed=0
if [ -f "$OPENCLAW_GATEWAY_DROPIN" ]; then
  cp -p -- "$OPENCLAW_GATEWAY_DROPIN" "$activation_backup_dir/gateway-dropin.conf"
  dropin_existed=1
fi

rollback_l5_activation() {
  local exit_code=$?
  trap - ERR
  set +e
  cp -p -- "$activation_backup_dir/nightly.service" "$NIGHTLY_UNIT"
  cp -p -- "$activation_backup_dir/openclaw.json" "$OPENCLAW_CONFIG"
  if [ "$dropin_existed" = "1" ]; then
    mkdir -p "$OPENCLAW_GATEWAY_DROPIN_DIR"
    cp -p -- "$activation_backup_dir/gateway-dropin.conf" "$OPENCLAW_GATEWAY_DROPIN"
  else
    rm -f -- "$OPENCLAW_GATEWAY_DROPIN"
  fi
  systemctl --user daemon-reload >/dev/null 2>&1
  systemctl --user restart "$OPENCLAW_GATEWAY_UNIT" >/dev/null 2>&1
  systemctl --user enable --now "$GATE_TIMER" >/dev/null 2>&1
  rm -rf -- "$activation_backup_dir"
  exit "$exit_code"
}
trap rollback_l5_activation ERR

systemctl --user disable --now "$GATE_TIMER" >/dev/null
ensure_env "EIMEMORY_AUTONOMOUS_LEARNING_APPLY" "Environment=EIMEMORY_AUTONOMOUS_LEARNING_APPLY=1"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_REPO" "Environment=EIMEMORY_AUTONOMOUS_CODE_REPO=/dev-project/eimemory"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_COMMIT" "Environment=EIMEMORY_AUTONOMOUS_CODE_COMMIT=1"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_DEPLOY" "Environment=EIMEMORY_AUTONOMOUS_CODE_DEPLOY=1"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_VERIFY_COMMAND" 'Environment="EIMEMORY_AUTONOMOUS_CODE_VERIFY_COMMAND=[\"/opt/eimemory/current/.venv/bin/python\",\"-m\",\"compileall\",\"-q\",\"eimemory\"]"'
ensure_env "EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND" 'Environment="EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND=[\"bash\",\"-lc\",\"COMMIT=\\\"$(git rev-parse HEAD)\\\" && bash ./deploy/install_immutable_release.sh \\\"$COMMIT\\\" && systemctl --user restart eimemory-rpc.service\"]"'
ensure_env "EIMEMORY_AUTONOMOUS_CODE_HEALTH_COMMAND" 'Environment="EIMEMORY_AUTONOMOUS_CODE_HEALTH_COMMAND=[\"curl\",\"-fsS\",\"http://127.0.0.1:8091/health\"]"'
enable_openclaw_memory_behavior

systemctl --user daemon-reload
trap - ERR
rm -rf -- "$activation_backup_dir"

echo "ok=l5_observation_gate"
echo "status=l5_enabled"
echo "stage=$stage"
echo "readiness_score=$readiness_score"
echo "autonomous_learning_apply=1"
echo "autonomous_code_commit=1"
echo "autonomous_code_deploy=1"
echo "openclaw_memory_behavior=enabled"
