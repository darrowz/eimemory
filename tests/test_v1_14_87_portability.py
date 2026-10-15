import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _resolve(tmp_path, settings, env=None):
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    script = f'. "{ROOT}/deploy/deploy_scope_default.sh"; echo "$(eimemory_default_deploy_scope_agent) $(eimemory_default_deploy_scope_workspace) $(eimemory_default_deploy_scope_user svc)"'
    out = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "EIMEMORY_CONFIG_DIR": str(tmp_path), **(env or {})},
                         capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_deploy_scope_comes_from_settings(tmp_path):
    assert _resolve(tmp_path, {"deploy_scope": {"agent_id": "a", "workspace_id": "w", "user_id": "u"}}) == ["a", "w", "u"]


def test_deploy_scope_falls_back_to_settings_defaults_then_service_user(tmp_path):
    assert _resolve(tmp_path, {"default_agent_id": "x", "default_workspace_id": "y"}) == ["x", "y", "svc"]


def test_gateway_units_discovered_by_pattern(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("alpha-gateway.service", "hermes-gateway-beta.service", "unrelated.service"):
        (unit_dir / name).write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    text = (ROOT / "deploy/discover_python_runtime_units.sh").read_text(encoding="utf-8")
    assert "hongxin" not in text and "xiaomage" not in text
    out = subprocess.run(["bash", str(ROOT / "deploy/discover_python_runtime_units.sh"), str(unit_dir)],
                         env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "alpha-gateway.service" in out.stdout
    assert "hermes-gateway-beta.service" in out.stdout
    assert "unrelated.service" not in out.stdout
