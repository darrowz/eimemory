#!/usr/bin/env bash
set -euo pipefail

EIMEMORY_BIN="${EIMEMORY_BIN:-/opt/eimemory/current/.venv/bin/eimemory}"
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
  if grep -q "^Environment=$key=" "$NIGHTLY_UNIT" || grep -q "^Environment=\"$key=" "$NIGHTLY_UNIT"; then
    sed -i "s|^Environment=$key=.*|$line|" "$NIGHTLY_UNIT"
    sed -i "s|^Environment=\"$key=.*|$line|" "$NIGHTLY_UNIT"
  else
    printf '%s\n' "$line" >> "$NIGHTLY_UNIT"
  fi
}

enable_openclaw_memory_behavior() {
  require_file "$OPENCLAW_CONFIG"
  /opt/eimemory/current/.venv/bin/python - "$OPENCLAW_CONFIG" <<'PY'
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
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  mkdir -p "$OPENCLAW_GATEWAY_DROPIN_DIR"
  cat >"$OPENCLAW_GATEWAY_DROPIN" <<'EOF'
[Service]
Environment=EIMEMORY_ENABLE_PROMPT_INJECTION=true
EOF
  systemctl --user daemon-reload
  systemctl --user restart "$OPENCLAW_GATEWAY_UNIT"
  curl -fsS http://127.0.0.1:18789/readyz >/dev/null
}

require_file "$EIMEMORY_BIN"
require_file "$NIGHTLY_UNIT"

readiness_json="$("$EIMEMORY_BIN" learn l5-readiness --persist --json)"
stage="$(printf '%s' "$readiness_json" | /opt/eimemory/current/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin).get("current_stage",""))')"
case "$stage" in
  L4|L4.5|L5) ;;
  *)
    echo "blocked_stage=$stage" >&2
    exit 3
    ;;
esac

"$EIMEMORY_BIN" ops timer-monitor --stale-after-minutes 90 >/tmp/eimemory-l5-observation-gate-timer-monitor.json

if systemctl --user --failed --no-legend 'eimemory*' | grep -q .; then
  systemctl --user --failed --no-legend 'eimemory*' >&2
  exit 4
fi

ensure_env "EIMEMORY_AUTONOMOUS_LEARNING_APPLY" "Environment=EIMEMORY_AUTONOMOUS_LEARNING_APPLY=1"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_REPO" "Environment=EIMEMORY_AUTONOMOUS_CODE_REPO=/dev-project/eimemory"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_COMMIT" "Environment=EIMEMORY_AUTONOMOUS_CODE_COMMIT=1"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_DEPLOY" "Environment=EIMEMORY_AUTONOMOUS_CODE_DEPLOY=1"
ensure_env "EIMEMORY_AUTONOMOUS_CODE_VERIFY_COMMAND" "Environment=\"EIMEMORY_AUTONOMOUS_CODE_VERIFY_COMMAND=/opt/eimemory/current/.venv/bin/python -m compileall -q eimemory\""
ensure_env "EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND" "Environment=\"EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND=COMMIT=\\\"\\$(git rev-parse HEAD)\\\" && bash ./deploy/install_immutable_release.sh \\\"\\$COMMIT\\\" && systemctl --user restart eimemory-rpc.service\""
ensure_env "EIMEMORY_AUTONOMOUS_CODE_HEALTH_COMMAND" "Environment=\"EIMEMORY_AUTONOMOUS_CODE_HEALTH_COMMAND=curl -fsS http://127.0.0.1:8091/health\""
enable_openclaw_memory_behavior

systemctl --user daemon-reload
systemctl --user disable --now "$GATE_TIMER" >/dev/null 2>&1 || true

echo "ok=l5_observation_gate"
echo "stage=$stage"
echo "autonomous_learning_apply=1"
echo "autonomous_code_commit=1"
echo "autonomous_code_deploy=1"
echo "openclaw_memory_behavior=enabled"
