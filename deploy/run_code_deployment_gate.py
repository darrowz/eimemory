#!/usr/bin/env python3
"""Fixed low-privilege installer transport for the original managed gates.

No Runtime, database or acceptance evaluator runs in the writable installer
namespace. Gate evidence comes from the independently retained, sealed child.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys


def main() -> int:
    if (len(sys.argv) != 5 or sys.argv[1] != "--job-key" or sys.argv[3] != "--operation"
        or re.fullmatch(r"[0-9a-f]{64}", sys.argv[2]) is None
        or sys.argv[4] not in {"receipt", "pre_observation", "post_resume"}
        or os.geteuid() != 1001):
        raise SystemExit("deployment_gate_fixed_low_privilege_arguments_required")
    key, operation = sys.argv[2], sys.argv[4]
    # This bootstrap only authenticates protected files/socket, before product
    # imports. It is also used by the fixed child entry itself.
    path = Path(__file__).with_name("run_code_deployment_validation.py")
    spec = importlib.util.spec_from_file_location("_fixed_validation_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = module._root_job(key)
    original = record["request"]
    manual = record.get("manual_release") is True
    controller = original.get("validation_controller_commit") if manual else original.get("controller_commit")
    if re.fullmatch(r"[0-9a-f]{40}", str(controller)) is None:
        raise ValueError("deployment_gate_controller_invalid")
    release = Path("/opt/eimemory/releases") / controller
    if release.is_symlink() or not release.is_dir():
        raise ValueError("deployment_gate_controller_unavailable")
    sys.path.insert(0, str(release))
    from eimemory.core.runtime_manager import deployment_job_status, finalize_deployment_job, complete_deployment_job
    from eimemory.governance.evolution.code_deployment_job import managed_validation_report
    parent = deployment_job_status(key)
    if (parent.get("original_invocation") != record.get("original_invocation")
        or parent.get("manual_release", False) is not manual):
        raise ValueError("deployment_gate_parent_identity_changed")
    if operation == "post_resume":
        if not manual:
            raise ValueError("deployment_gate_automatic_owner_finishes_business")
        if parent.get("phase") != "resumed_awaiting_business":
            restored = finalize_deployment_job(key)
            if (restored.get("job_key") != key or restored.get("phase") != "resumed_awaiting_business"
                or restored.get("resumed") is not True):
                raise ValueError("deployment_gate_manual_restoration_held")
        elif parent.get("resumed") is not True or parent.get("held_background"):
            raise ValueError("deployment_gate_manual_restoration_held")
    report, native = managed_validation_report(key, operation, heartbeat=lambda: None)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if native["exit_status"] != 0 or report.get("ok") is not True:
        return 2
    if operation == "receipt":
        readback = native["target_transport"].get("physical_receipt_readback")
        if readback != {"current_deployment_receipt_verified": True, "commit": report.get("commit"),
            "receipt_id": report.get("promotion_request_id")}:
            raise ValueError("deployment_gate_physical_receipt_unverified")
        print(json.dumps(readback, sort_keys=True))
        print("deployment_receipt_id=" + readback["receipt_id"] + " commit=" + readback["commit"])
    else:
        capture = native["target_transport"].get("closure_capture")
        if (not isinstance(capture, dict) or capture.get("capture_saved") is not True
            or capture.get("recording_ok") is not True or capture.get("exit_code") != 0):
            raise ValueError("deployment_gate_original_capture_unverified")
        print(json.dumps(capture, ensure_ascii=False, sort_keys=True, allow_nan=False))
        if operation == "post_resume":
            if report.get("business_qualified") is not True or report.get("l5_certification") != "not_certified":
                raise ValueError("deployment_gate_manual_business_not_qualified")
            completed = complete_deployment_job(key)
            if (completed.get("job_key") != key or completed.get("complete") is not True
                or completed.get("phase") != "completed" or completed.get("resumed") is not True
                or completed.get("operational_complete") is not True or completed.get("held_background")):
                raise ValueError("deployment_gate_manual_completion_unknown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
