#!/usr/bin/env python3
"""Fixed PID1 User=darrow entry. Only the opaque registered job key is input."""
from pathlib import Path
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--job-key" or os.geteuid() == 0:
        raise SystemExit("deployment_job_low_privilege_single_key_required")
    from eimemory.api.runtime import Runtime
    from eimemory.governance.evolution.code_deployment_job import run_job
    # Root registry owns service configuration; neither request nor argv can
    # choose a state directory, command, environment, or Python entry point.
    runtime = Runtime.create(root="/var/lib/eimemory")
    try:
        result = run_job(runtime, sys.argv[2])
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") is True else 1
    finally:
        runtime.close()

if __name__ == "__main__":
    raise SystemExit(main())
