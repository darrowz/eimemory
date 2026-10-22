"""Wait for a native user deployment before opening the nightly database.

Restored persistent timers may fire while the deployment worker is still
finishing. This barrier observes its real native exit; it never converts a
failed deployment into release or autonomous repair evidence.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from eimemory.contracts import runtime_manager as contract
from eimemory.contracts import user_control_plane as user
from eimemory.core import runtime_manager as manager

CURRENT_LINK = Path("/opt/eimemory/current")
RELEASES = Path("/opt/eimemory/releases")


def _current_boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _restored_boot_failure(result, transition):
    """Admit diagnostics after a proven interruption, never invent an exit."""
    recovery = result.get("terminal_recovery")
    if not isinstance(recovery, dict) or recovery.get("failure_kind") != "boot_change":
        return False
    if recovery.get("phase") in {"failure_verified", "restoring"}:
        return False
    key, invocation = result.get("job_key"), result.get("original_invocation")
    registry = manager.read_registry()
    request = result.get("request") or {}
    target = CURRENT_LINK.resolve(strict=True)
    boot = _current_boot_id()
    record = {"job_key": key, "unit": result["unit"], "original_invocation": invocation,
        "native_hold": result.get("native_hold"), "restore_plan": result.get("restore_plan"),
        "registry": {"generation": result.get("launch_registry_generation")}, "request": request,
        "resumed": result.get("resumed"), "finalized": result.get("complete"), "terminal_recovery": recovery}
    if (result.get("terminal_state") is not None or result.get("complete") is not False
            or result.get("background_held") is not False or result.get("held_background")
            or result.get("phase") != "resumed_awaiting_business"
            or transition != {"job_key": key, "complete": False}
            or request.get("operation") not in {"deploy", "rollback"} or target.parent != RELEASES
            or (request.get("operation") == "deploy"
                and target.name not in {request.get("base_commit"), request.get("candidate_commit")})
            or (request.get("operation") == "rollback" and target.name != request.get("base_commit"))
            or target.name != recovery.get("current_commit") or target.name != registry.get("runtime_commit")
            or recovery.get("controller_commit") != registry.get("controller_commit")
            or recovery.get("registry_generation") != registry.get("generation")
            or not contract.restored_boot_failure(record, result.get("state"), current_boot=boot)):
        raise ValueError("nightly_deployment_boot_failure_unverified")
    if manager.read_registry() != registry or CURRENT_LINK.resolve(strict=True) != target or _current_boot_id() != boot:
        raise ValueError("nightly_deployment_boot_failure_changed")
    return True


def wait_for_deployment_terminal(*, timeout_seconds: float = 610) -> dict:
    if not user.user_mode():
        return {"status": "not_user_managed"}
    manager.read_registry()
    path = user.coordinates()["state"] / "active-transition.json"
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not os.path.lexists(path):
            return {"status": "no_deployment_transition"}
        raw = user.read_file(path)
        transition = contract.strict_json(raw)
        if (not isinstance(transition, dict)
                or type(transition.get("complete")) is not bool):
            raise ValueError("nightly_deployment_transition_invalid")
        key = transition.get("job_key")
        contract.hex_value(key, 64, "nightly_deployment_job_invalid")
        result = manager.deployment_job_status(key)
        if (result.get("job_key") != key or result.get("manager") != "user"
                or result.get("unit") != contract.job_unit(key)):
            raise ValueError("nightly_deployment_observation_unbound")
        terminal = result.get("terminal_state")
        if terminal is None and _restored_boot_failure(result, transition):
            if user.read_file(path) != raw:
                raise ValueError("nightly_deployment_transition_changed")
            return {"status": "terminal", "job_key": key, "worker_succeeded": False, "release_complete": False,
                "worker_terminal_kind": "boot_interruption", "native_exit_observed": False,
                "autonomous_repair_credit": False}
        if terminal is not None:
            if (not contract.native_terminal_exit(terminal, result.get("original_invocation"), result["unit"])
                    or not contract.native_terminal_readback(terminal, result.get("state", {}),
                        result.get("original_invocation"), result["unit"])):
                raise ValueError("nightly_deployment_terminal_unverified")
            if user.read_file(path) != raw:
                raise ValueError("nightly_deployment_transition_changed")
            success = terminal.get("Result") == "success" and terminal.get("ExecMainStatus") == "0"
            if success:
                if (transition["complete"] is not True or result.get("complete") is not True
                        or result.get("phase") != "completed"):
                    raise ValueError("nightly_deployment_completion_missing")
                # This is outside the worker, after its actual successful exit.
                # The controller releases its completed unit and recovers any
                # eligible observers exactly once. It cannot re-run the worker.
                released = manager.request("release_job", job_key=key)
                if released.get("job_key") != key or released.get("release_requested") is not True:
                    raise ValueError("nightly_deployment_release_unverified")
                if user.read_file(path) != raw:
                    raise ValueError("nightly_deployment_transition_changed")
            return {"status": "terminal", "job_key": key,
                    "worker_succeeded": success, "release_complete": result.get("complete") is True}
        if time.monotonic() >= deadline:
            raise ValueError("nightly_deployment_terminal_wait_expired")
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
