from __future__ import annotations

import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parents[1] / "integrations" / "codex" / "eimemory"


def test_codex_plugin_manifest_and_native_integration_contract() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "eimemory"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "hooks" not in manifest
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    for event, groups in hooks["hooks"].items():
        command = groups[0]["hooks"][0]["command"]
        assert command == f"eimemory codex-hook --event {event}"
        assert groups[0]["hooks"][0]["timeout"] <= 2
    assert mcp["mcpServers"]["eimemory"]["command"] == "eimemory"
    assert mcp["mcpServers"]["eimemory"]["args"] == ["codex-mcp"]


def test_codex_plugin_documents_configuration_bypass_and_channel_authority() -> None:
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "EIMEMORY_RPC_URL" in readme
    assert "EIMEMORY_RPC_TOKEN" in readme
    assert "per_channel" in readme
    assert "embodied::channel::codex" in readme
    assert "fail-open" in readme
    assert "transcript_path" in readme

