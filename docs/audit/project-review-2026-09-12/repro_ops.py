"""Audit counterexamples on temporary local data; exit 0 confirms the defects."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eimemory.api.runtime import Runtime
from eimemory.cli.main import _nightly_cli_summary
from eimemory.governance.supervisor import build_supervisor_contract
from eimemory.scheduler.jobs import run_nightly_jobs


def main():
    with TemporaryDirectory(prefix="eimemory-ops-review-") as root:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("EIMEMORY_", "OPENCLAW_"))}
        environment.update({"EIMEMORY_ROOT": root, "OPENCLAW_LOOP_HOME": str(Path(root) / "loop")})
        with patch.dict(os.environ, environment, clear=True), closing(Runtime.create(root=root)) as runtime:
            scope = {"tenant_id": "audit", "agent_id": "audit", "workspace_id": "audit", "user_id": "audit"}
            with patch.object(runtime, "resume_code_evolution_transactions",
                              side_effect=RuntimeError("injected maintenance failure")):
                report = run_nightly_jobs(runtime, scope=scope, external_fetch_text=lambda _url: "")
            supervisor = build_supervisor_contract(runtime, scope=scope)
            result = {
                "code_evolution": report["code_evolution"],
                "nightly_ok": report["ok"],
                "supervisor_ok": report["supervisor_summary"]["ok"],
                "supervisor_status": supervisor["status"],
                "cli_summary": _nightly_cli_summary(report),
            }
            assert result["code_evolution"]["ok"] is False
            assert result["nightly_ok"] is result["supervisor_ok"] is True
            assert result["supervisor_status"] == "healthy"
            assert "code_evolution" not in result["cli_summary"]
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
