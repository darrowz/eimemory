"""Exercise public core APIs in a fresh interpreter without optional hosts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def test_legacy_qmd_export_is_only_a_compatibility_facade():
    from eimemory.adapters.openclaw import qmd_export
    from eimemory.storage import record_export

    for name in qmd_export.__all__:
        assert getattr(qmd_export, name) is getattr(record_export, name)


def test_core_public_apis_without_openclaw(tmp_path):
    script = r'''
import importlib.abc
import json
import os
from pathlib import Path
import shutil
import sys

class NoOpenClaw(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if "openclaw" in fullname.lower():
            raise AssertionError("optional adapter import attempted: " + fullname)
        return None

sys.meta_path.insert(0, NoOpenClaw())
assert shutil.which("openclaw") is None
assert not Path(os.environ["OPENCLAW_CONFIG_PATH"]).exists()

from eimemory.api.runtime import Runtime

scope = {"tenant_id": "test", "agent_id": "core", "workspace_id": "independent", "user_id": "test"}
runtime = Runtime.create(root=Path(sys.argv[1]))
try:
    record = runtime.memory.ingest(
        text="The user prefers concise status updates before implementation details.",
        memory_type="preference",
        title="Communication preference",
        scope=scope,
        force_capture=True,
    )
    recalled = runtime.memory.recall(
        query="concise status updates implementation details",
        scope=scope,
        limit=5,
    )
    assert record.record_id in {item.record_id for item in recalled.items}
    definitions = runtime.capabilities.list_definitions(
        runtime_scope=scope, capability_scope="global",
    )
    assert definitions == []  # Opening core does not seed a host-specific taxonomy.
    status = runtime.capabilities.status()
    assert status["schema"] == "capability.service_status.v1"
    assert status["audit_export"]["pending"] == 0
    assert not any("openclaw" in name.lower() for name in sys.modules)
    print(json.dumps({"write": True, "recall": True, "catalog_read": True,
                      "capability_status": True, "openclaw_imported": False}))
finally:
    runtime.close()
'''
    env = {key: value for key, value in os.environ.items() if "OPENCLAW" not in key.upper()}
    env.update({
        "PATH": "",
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "OPENCLAW_CONFIG_PATH": str(tmp_path / "absent-openclaw.json"),
    })
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "core")],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "write": True,
        "recall": True,
        "catalog_read": True,
        "capability_status": True,
        "openclaw_imported": False,
    }
