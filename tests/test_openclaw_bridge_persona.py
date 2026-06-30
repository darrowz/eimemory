from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_openclaw_js_bridge_prepends_persona_guidance(tmp_path) -> None:
    hook_script = tmp_path / "persona-hook.py"
    hook_script.write_text(
        """
import json
import sys
hook = sys.argv[-1]
if hook == "before_prompt_build":
    print(json.dumps({
        "persona_guidance": {
            "enabled": True,
            "scene": "coding_plan",
            "text": "Persona guidance:\\n- Current scene: coding_plan.\\n- Output: include verification."
        },
        "memory_bundle": {"items": [], "explanation": {}},
        "injection_plan": {"entries": [], "items": []}
    }))
else:
    print("{}")
""".strip(),
        encoding="utf-8",
    )
    js = """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ config: { allowPromptInjection: true }, on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_prompt_build({ prompt: '实现 persona layer' })).then((result) => {
  process.stdout.write(JSON.stringify(result));
});
"""
    env = os.environ.copy()
    env["EIMEMORY_ENABLE_PROMPT_INJECTION"] = "true"
    env["EIMEMORY_HOOK_COMMAND"] = f"{sys.executable} {hook_script} openclaw-hook"
    env["EIMEMORY_BRIDGE_COMMAND"] = f"{sys.executable} {hook_script} ei-bridge feishu"
    result = subprocess.run(
        ["node", "-e", js],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "Persona guidance:" in payload["prependContext"]
    assert "coding_plan" in payload["prependContext"]
