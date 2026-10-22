"""Durable handoff to the registered PID1 deploy job; never a detached child.

The request is an intent, not deployment evidence. Only the normal installer
and a physical receipt readback can prove the effect. An uncertain launch is
not retried under a new key and never implies permission to roll back.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import time

from eimemory.contracts.code_deployment_job import (SCHEMA, MANUAL_SCHEMA, VALIDATION_BUDGETS,
    canonical_bytes, request_key, validate_request, validate_manual_permit)
from eimemory.core.release_source_guard import open_new_file_at_directory

JOBS_ROOT = Path("/var/lib/eimemory/state/code-deployment-jobs")
from eimemory.contracts.user_control_plane import user_mode, coordinates, read_file as read_user_authority

def _control_state():
    return coordinates()['state'] if user_mode() else Path('/var/lib/eimemory-manager')

def _manager_name():
    return 'user' if user_mode() else 'system'

def _stable(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name == "posix" else None)

def _parent_fd(path: Path):
    if os.name != "posix":
        return None
    # Walk from / using O_NOFOLLOW on EVERY component. A pathname preflight
    # alone would permit replacing an ancestor between checking and opening.
    current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise

def _directory(path: Path, *, create=False) -> None:
    if create and os.name == "posix":
        if not path.is_absolute():
            raise ValueError("deployment_job_directory_not_absolute")
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = Path(path.anchor)
        try:
            for component in path.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                else:
                    # The directory entry must survive power loss before a
                    # request/CAS can refer to this newly created directory.
                    os.fsync(parent)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                current /= component
                try:
                    if _stable(os.fstat(child)) != _stable(current.lstat()):
                        raise ValueError("deployment_job_directory_changed")
                except BaseException:
                    os.close(child)
                    raise
                os.close(parent)
                parent = child
        finally:
            os.close(parent)
    elif create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or parent.is_symlink():
            raise ValueError("deployment_job_directory_unsafe")
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("deployment_job_directory_not_private")

def _read(path: Path, *, maximum: int = 65536) -> dict:
    if type(maximum) is not int or not 0 < maximum <= 8 * 1024 * 1024:
        raise ValueError("deployment_job_read_budget_invalid")
    _directory(path.parent)
    parent = _parent_fd(path)
    try:
        fd = os.open(path.name if parent is not None else path,
                     os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), dir_fd=parent)
    except BaseException:
        if parent is not None:
            os.close(parent)
        raise
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum
                or (os.name == "posix" and (before.st_uid != os.geteuid() or before.st_mode & 0o077))):
            raise ValueError("deployment_job_file_unsafe")
        raw = os.read(fd, maximum + 1)
        if (_stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(path.lstat())
                or len(raw) != before.st_size or (parent is not None
                    and _stable(os.fstat(parent)) != _stable(path.parent.lstat()))):
            raise ValueError("deployment_job_file_changed")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("deployment_job_file_invalid")
        return result
    finally:
        os.close(fd)
        if parent is not None:
            os.close(parent)

def _exclusive(path: Path, value: dict) -> None:
    _exclusive_blob(path, canonical_bytes(value))

def _exclusive_blob(path: Path, raw: bytes) -> None:
    if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024:
        raise ValueError("deployment_job_artifact_budget_invalid")
    _directory(path.parent)
    parent = _parent_fd(path)
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0))
        fd = (open_new_file_at_directory(parent, path.name, flags, 0o600) if parent is not None
              else os.open(path, flags, 0o600))
    except BaseException:
        if parent is not None:
            os.close(parent)
        raise
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    if os.name == "posix":
        try:
            os.fsync(parent)
            if _stable(os.fstat(parent)) != _stable(path.parent.lstat()):
                raise ValueError("deployment_job_parent_changed")
        finally:
            os.close(parent)

def _controller_commit() -> str:
    # The root registry launches a release-pinned entry. Neither caller env nor
    # the mutable trusted checkout selects the worker's controller code.
    root = Path(__file__).resolve().parents[3]
    if root.parent.name != "releases" or re.fullmatch(r"[0-9a-f]{40}", root.name) is None:
        raise ValueError("deployment_job_controller_not_immutable")
    return root.name

def build_request(transaction: Mapping, policy: Mapping, *, operation: str, controller_commit: str) -> dict:
    payload = transaction.get("payload") or {}
    prior = transaction.get("prior_commit") or transaction.get("base_commit")
    if prior != transaction.get("base_commit"):
        raise ValueError("deployment_job_prior_not_base")
    return validate_request({
        "schema": SCHEMA, "operation": operation, "transaction_id": transaction.get("transaction_id"),
        "scope": {k: transaction.get(k) for k in ("tenant_id", "agent_id", "workspace_id", "user_id")},
        "base_commit": transaction.get("base_commit"), "candidate_commit": transaction.get("candidate_commit"),
        "target_commit": transaction.get("candidate_commit") if operation == "deploy" else prior,
        "controller_commit": controller_commit, "installer_digest": (policy.get("deployment") or {}).get("installer_digest"),
        **{k: transaction.get(k) for k in ("policy_digest", "authorization_digest", "patch_digest", "candidate_tree_digest")},
        "verification_receipt_digests": sorted(payload.get("verification_receipt_digests") or []),
        "observation_deadline": transaction.get("observation_deadline"),
    })

def _bound_request(transaction: Mapping, operation: str) -> tuple[str, dict] | None:
    key = ((transaction.get("payload") or {}).get("managed_deployment_jobs") or {}).get(operation)
    if key is None:
        return None
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise ValueError("deployment_job_binding_invalid")
    body = validate_request(_read(JOBS_ROOT / key / "request.json"))
    if request_key(body) != key:
        raise ValueError("deployment_job_request_digest_mismatch")
    policy = (transaction.get("payload") or {}).get("authorized_policy") or {}
    if body != build_request(transaction, policy, operation=operation, controller_commit=body["controller_commit"]):
        raise ValueError("deployment_job_transaction_mismatch")
    return key, body

def handoff(runtime, transaction: Mapping, policy: Mapping, *, operation: str) -> dict:
    from eimemory.core.runtime_manager import submit_deployment_job, read_registry
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager

    binding = _bound_request(transaction, operation)
    if binding is None:
        body = build_request(transaction, policy, operation=operation,
                             controller_commit=read_registry()["controller_commit"])
        key = request_key(body)
        _directory(JOBS_ROOT, create=True)
        _directory(JOBS_ROOT / key, create=True)
        try:
            _exclusive(JOBS_ROOT / key / "request.json", body)
        except FileExistsError:
            if _read(JOBS_ROOT / key / "request.json") != body:
                raise ValueError("deployment_job_request_conflict")
        jobs = dict((transaction.get("payload") or {}).get("managed_deployment_jobs") or {})
        jobs[operation] = key
        CodeEvolutionTransactionManager(runtime).update_metadata(
            body["transaction_id"], payload_updates={"managed_deployment_jobs": jobs})
    else:
        key, body = binding
    try:
        _exclusive(JOBS_ROOT / key / "submitted.json", {"request_key": key})
    except FileExistsError:
        pass
    # The protected manager's durable root CAS accepts this key only once;
    # duplicate calls are status reads, including after a lost response.
    try:
        submit_deployment_job(key)
    except (OSError, RuntimeError, ValueError, TimeoutError):
        pass
    return job_state(key, body)

def job_state(key: str, body: dict) -> dict:
    from eimemory.core.runtime_manager import deployment_job_status
    try:
        native = deployment_job_status(key)
    except (OSError, RuntimeError, ValueError, TimeoutError):
        native = {}
    state = native.get("state") if isinstance(native.get("state"), dict) else {}
    unit = f"eimemory-code-deployment-job@{key}.service"
    invocation = native.get("original_invocation") or ""
    if native and (native.get("job_key") != key or native.get("manager") != _manager_name()
        or native.get("unit") != unit
        or (invocation and re.fullmatch(r"[0-9a-f]{32}", str(invocation)) is None)
        or state.get("InvocationID") not in {"", None, invocation}):
        raise ValueError("deployment_job_root_identity_mismatch")
    evidence = {"job_key": key, "operation": body["operation"], "unit": unit,
                "native_invocation": invocation, "native_state": state, "pending": True,
                "native_phase": native.get("phase"), "native_complete": native.get("complete") is True,
                "reason": "managed_deployment_job_awaiting_reconciliation"}
    if not invocation:
        # A same-UID result file cannot stand in for the root's durable
        # original Invocation, including after unit collection or a lost reply.
        evidence["reconciliation_required"] = True
        return evidence
    try:
        result = _read(JOBS_ROOT / key / "result.json")
        started = _read(JOBS_ROOT / key / "started.json")
    except FileNotFoundError:
        # Absence remains explicit even if the native unit is currently unknown.
        evidence["reconciliation_required"] = (state.get("ActiveState") not in {"active", "activating", "deactivating"}
            or state.get("SubState") == "exited" or native.get("terminal_state") is not None)
        return evidence
    if (set(result) != {"request_key", "invocation", "result"} or result["request_key"] != key
        or started != {"request_key": key, "invocation": result["invocation"]}
        or not isinstance(result["invocation"], str) or not re.fullmatch(r"[0-9a-f]{32}", result["invocation"])
        or invocation != result["invocation"] or not isinstance(result["result"], dict)):
        raise ValueError("deployment_job_result_identity_mismatch")
    if result["result"].get("ok") is True and (
        native.get("phase") != "completed" or native.get("complete") is not True
        or native.get("resumed") is not True or native.get("held_background")):
        raise ValueError("deployment_job_success_before_native_completion")
    projected = result["result"]
    if (projected.get("status") == "native_completion_unknown"
        and native.get("phase") == "completed" and native.get("complete") is True
        and native.get("resumed") is True and not native.get("held_background")):
        # A lost complete reply is reconciled from root's durable outcome;
        # never overwrite the original failed/unknown process artifact.
        receipt, receipt_native = managed_validation_report(key, "receipt", heartbeat=lambda: None)
        if receipt.get("ok") is not True or receipt_native["exit_status"] != 0:
            raise ValueError("deployment_job_completion_receipt_unverified")
        refs = {"receipt": receipt_native["artifact_digest"]}
        if body["operation"] == "deploy":
            business, business_native = managed_validation_report(key, "post_resume", heartbeat=lambda: None)
            if (business.get("ok") is not True or business.get("business_qualified") is not True
                or business_native["exit_status"] != 0):
                raise ValueError("deployment_job_completion_business_unverified")
            refs["post_resume"] = business_native["artifact_digest"]
        supplement = {"schema": "eimemory.deployment_completion_readback.v1", "request_key": key,
            "invocation": invocation, "original_result_digest": sha256(canonical_bytes(result)).hexdigest(),
            "root_phase": "completed", "artifact_digests": refs}
        path = JOBS_ROOT / key / "completion-readback.json"
        try:
            _exclusive(path, supplement)
        except FileExistsError:
            if _read(path) != supplement:
                raise ValueError("deployment_job_completion_readback_changed") from None
        projected = {**projected, "ok": True, "operational_complete": True,
            "status": "native_completion_reconciled", "reconciliation_required": False,
            "original_process_result_preserved": True, "completion_readback": supplement}
    evidence.update(pending=False, native_invocation=result["invocation"], result=projected,
                    result_digest=sha256(canonical_bytes(result)).hexdigest())
    return evidence

def pending(runtime, transaction: Mapping) -> dict | None:
    """Re-read a real bound request; caller-supplied flags cannot defer recovery."""
    operation = {"DEPLOY_INTENT": "deploy", "DEPLOYED_VERIFIED": "deploy", "HEALTHY": "deploy",
                 "ROLLBACK_INTENT": "rollback"}.get(transaction.get("current_state"))
    if not operation:
        return None
    binding = _bound_request(transaction, operation)
    if binding is None:
        return None
    evidence = job_state(*binding)
    # An absent/terminal/unknown native unit is not an endless pending lease.
    # The normal physical readback must decide success or explicit quarantine.
    state = evidence["native_state"]
    active = state.get("ActiveState") in {"active", "activating", "deactivating"} and state.get("SubState") != "exited"
    return evidence if evidence["pending"] and active else None


def post_resume_business_error(runtime, transaction: Mapping) -> str:
    """Reconcile a saved business admission; its JSON alone is not authority."""
    from eimemory.governance.release.closure_contracts import controlled_production_closure_report_ok
    from eimemory.governance.release.release_closure import read_strict_deployment_receipt
    from eimemory.evaluation.controlled_production_acceptance import verify_controlled_production_acceptance
    from eimemory.governance.release.evidence_contract import ReleaseIdentity
    from eimemory.core.runtime_manager import deployment_job_status
    payload = transaction.get("payload") or {}
    try:
        binding = _bound_request(transaction, "deploy")
        if not binding or payload.get("post_resume_business_job_key") != binding[0]:
            return "managed_post_resume_business_required"
        key, body = binding
        report = _read(JOBS_ROOT / key / "post-resume-business.json", maximum=4 * 1024 * 1024)
        original, transport = managed_validation_report(key, "post_resume", heartbeat=lambda: None)
        saved_transport = report.get("native_transport") or {}
        if ({name: value for name, value in report.items() if name != "native_transport"} != original
            or any(saved_transport.get(name) != transport.get(name) for name in (
                "exit_status", "manager", "unit", "invocation", "parent_invocation",
                "source_readonly", "artifact_digest", "target_transport"))):
            return "managed_post_resume_root_original_changed"
        if (sha256(canonical_bytes(report)).hexdigest() != payload.get("post_resume_business_digest")
            or report.get("business_qualified") is not True or report.get("observation_started") is not False
            or report.get("managed_job_key") != key or report.get("managed_transaction_id") != body["transaction_id"]
            or report.get("scope") != body["scope"] or not controlled_production_closure_report_ok(report)
            or (report.get("native_transport") or {}).get("exit_status") != 0):
            return "managed_post_resume_business_proof_invalid"
        native = deployment_job_status(key)
        invocation = payload.get("post_resume_business_invocation")
        if (not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation)
            or _read(JOBS_ROOT / key / "started.json") != {"request_key": key, "invocation": invocation}
            or native.get("phase") not in {"resumed_awaiting_business", "completed"}
            or native.get("original_invocation") != invocation
            or (native.get("state") or {}).get("InvocationID") not in {"", None, invocation}):
            return "managed_post_resume_native_identity_changed"
        policy = payload["authorized_policy"]
        receipt = read_strict_deployment_receipt(runtime, scope=body["scope"], repo_root=transaction["repository_root"],
            current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"],
            transaction_id=body["transaction_id"], deployed_commit=body["candidate_commit"])
        if receipt.get("ok") is not True or receipt != report.get("deployment_receipt"):
            return "managed_post_resume_strict_receipt_changed"
        release = ReleaseIdentity(commit=receipt["commit"], version=receipt["version"],
            receipt_id=receipt["promotion_request_id"], session_id=receipt["release_session_id"])
        checked = verify_controlled_production_acceptance(runtime, scope=body["scope"], release=release,
            record_id=str((report.get("record_ids") or {}).get("controlled_production_acceptance") or ""), limit=500)
        if checked.get("ok") is not True or checked.get("accepted") is not True:
            return "managed_post_resume_current_business_evidence_invalid"
        from eimemory.governance.evolution.code_automation_policy_owner import verify_native_standing_authority
        if verify_native_standing_authority(runtime, transaction=transaction).get("ok") is not True:
            return "managed_post_resume_standing_authority_invalid"
    except Exception as exc:
        return "managed_post_resume_readback_unavailable:" + type(exc).__name__
    return ""


def observation_completion_error(transaction: Mapping) -> str:
    """A live observation cannot certify a root transition still held open."""
    try:
        binding = _bound_request(transaction, "deploy")
        if binding is None:
            return ""
        key, _ = binding
        body = binding[1]
        from eimemory.core.runtime_manager import deployment_job_status, read_registry
        native = deployment_job_status(key)
        registry = read_registry()
        started = _read(JOBS_ROOT / key / "started.json")
        policy = (transaction.get("payload") or {}).get("authorized_policy") or {}
        current = Path(policy["deployment"]["current_link"]).resolve(strict=True)
        if (native.get("manager") != _manager_name() or native.get("unit") != f"eimemory-code-deployment-job@{key}.service"
            or native.get("phase") != "completed" or started.get("request_key") != key
            or native.get("complete") is not True or native.get("resumed") is not True
            or native.get("held_background")
            or not re.fullmatch(r"[0-9a-f]{32}", str(started.get("invocation") or ""))
            or native.get("original_invocation") != started["invocation"]
            or registry.get("runtime_commit") != body["candidate_commit"]
            or registry.get("controller_commit") != body["controller_commit"]
            or current.parent.name != "releases" or current.name != body["candidate_commit"]
            or (native.get("state") or {}).get("InvocationID") not in {"", None, started["invocation"]}):
            return "managed_native_business_completion_unverified"
    except Exception as exc:
        return "managed_native_completion_unavailable:" + type(exc).__name__
    return ""

def _after_storage_call(call, *, manager, transaction_id: str, interval: float = 30):
    """Renew the existing SQL lease only after physical storage commit.

    Manager transport has its own bounded timeout. Only its socket operation
    runs on the worker thread; SQLite stays on the original owning thread.
    This helper is never used for installer/quiesce/backup/migration calls.
    """
    from concurrent.futures import ThreadPoolExecutor, wait
    manager.renew_lease(transaction_id)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="deployment-manager") as pool:
        future = pool.submit(call)
        while not wait((future,), timeout=interval).done:
            manager.renew_lease(transaction_id)
        return future.result()


def _validation_unit(key, operation):
    if re.fullmatch(r"[0-9a-f]{64}", str(key)) is None or operation not in VALIDATION_BUDGETS:
        raise ValueError("deployment_validation_identity_invalid")
    return f"eimemory-code-validation-{operation.replace('_', '-')}@{key}.service"


def _native_child_cgroup() -> str:
    return Path("/proc/self/cgroup").read_text()


def _native_control_cgroup(value,unit):
    from eimemory.contracts.runtime_manager import fixed_system_unit_cgroup
    return fixed_system_unit_cgroup(value,unit)


def _assert_child_cgroup(native: dict, actual: str) -> None:
    expected = (native.get("state") or {}).get("ControlGroup", "")
    if (not _native_control_cgroup(expected,native.get("unit"))
        or [line[3:] for line in actual.splitlines() if line.startswith("0::")] != [expected]):
        raise ValueError("deployment_validation_actual_cgroup_unverified")


def validation_binding(key: str, operation: str, native: dict | None = None) -> tuple[dict, dict]:
    """Root original request is authoritative for manual and automatic jobs.

    The normalized target/controller coordinates are local to this validation;
    the original permit remains byte-for-byte unchanged under its original key.
    """
    from eimemory.core.runtime_manager import deployment_validation_status
    _validation_unit(key, operation)
    root = json.loads(_root_file_bytes((_control_state()/"jobs") / (key + ".json"), maximum=65536))
    original = root.get("request")
    manual = root.get("manual_release") is True
    original = validate_manual_permit(original) if manual else validate_request(original)
    if root.get("job_key") != key or sha256(canonical_bytes(original)).hexdigest() != key:
        raise ValueError("deployment_validation_root_request_invalid")
    native = deployment_validation_status(key, operation) if native is None else native
    controller = original["validation_controller_commit"] if manual else original["controller_commit"]
    target = native.get("target_commit")
    if (target not in ({original["base_commit"], original["candidate_commit"]} if manual else {original["target_commit"]})
        or native.get("controller_commit") != controller
        or native.get("launch_controller_commit", original["controller_commit"]) != original["controller_commit"]
        or native.get("manual_release", False) is not manual
        or native.get("parent_original_invocation") != root.get("original_invocation")
        or re.fullmatch(r"[0-9a-f]{32}", str(root.get("original_invocation"))) is None):
        raise ValueError("deployment_validation_root_coordinates_invalid")
    if not manual:
        if (_read(JOBS_ROOT / key / "request.json") != original
            or _read(JOBS_ROOT / key / "started.json") != {"request_key": key, "invocation": root["original_invocation"]}):
            raise ValueError("deployment_validation_local_request_changed")
    body = {**original, "controller_commit": controller, "target_commit": target,
        "manual_release": manual, "launch_controller_commit": original["controller_commit"]}
    if manual and original['base_commit'] == target:
        body['rollback_commit'] = _manual_revalidation_rollback(root, target, original['scope'])
    return body, native


def _manual_revalidation_rollback(record, target, scope):
    """Retain the real predecessor through Root-bound same-release retries."""
    seen = set()
    for _ in range(16):
        key = record.get('revalidation_of')
        if not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}', key) is None or key in seen:
            raise ValueError('deployment_revalidation_predecessor_unbound')
        seen.add(key)
        record = json.loads(_root_file_bytes((_control_state()/'jobs') / (key + '.json'), maximum=65536))
        permit = validate_manual_permit(record.get('request'))
        if (record.get('job_key') != key or sha256(canonical_bytes(permit)).hexdigest() != key
            or record.get('manual_release') is not True or permit['target_commit'] != target
            or permit['candidate_commit'] != target or permit['scope'] != scope):
            raise ValueError('deployment_revalidation_predecessor_changed')
        if permit['base_commit'] != target:
            return permit['base_commit']
    raise ValueError('deployment_revalidation_predecessor_limit')


def _validation_artifact_context(key, operation, body, native):
    return {"schema": "managed_code_deployment_validation.v2", "job_key": key, "operation": operation,
        "parent_invocation": native["parent_original_invocation"], "invocation": native["original_invocation"],
        "target_commit": body["target_commit"], "controller_commit": body["controller_commit"],
        "launch_controller_commit": body["launch_controller_commit"], "manual_release": body["manual_release"],
        "scope": body["scope"], "source_readonly": True}


def validation_context(key: str, operation: str) -> tuple[dict, dict]:
    """Verify the actual child Invocation against the protected root record."""
    unit = _validation_unit(key, operation)
    body, native = validation_binding(key, operation)
    invocation = os.environ.get("INVOCATION_ID", "")
    if (not re.fullmatch(r"[0-9a-f]{32}", invocation)
        or native.get("job_key") != key or native.get("operation") != operation
        or native.get("manager") != _manager_name() or native.get("unit") != unit
        or native.get("artifact_basename") != f"validation-{operation}-result.json"
        or native.get("original_invocation") != invocation
        or (native.get("state") or {}).get("InvocationID") != invocation
        or (native.get("state") or {}).get("ActiveState") not in {"active", "activating"}):
        raise ValueError("deployment_validation_native_context_unverified")
    _assert_child_cgroup(native, _native_child_cgroup())
    return body, native


def _root_file_bytes(path: Path, *, maximum=8 * 1024 * 1024 + 65536) -> bytes:
    if user_mode():return read_user_authority(path,maximum=maximum)
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("deployment_validation_root_artifact_parent_untrusted")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o640 or before.st_size > maximum):
            raise ValueError("deployment_validation_root_artifact_untrusted")
        raw = os.read(descriptor, maximum + 1)
        if (_stable(before) != _stable(os.fstat(descriptor)) or _stable(before) != _stable(path.lstat())
            or len(raw) != before.st_size):
            raise ValueError("deployment_validation_root_artifact_changed")
        return raw
    finally:
        os.close(descriptor)


def _root_validation_envelope(key: str, operation: str) -> bytes:
    """Read only the root's immutable copy, never a late UID1001 replacement."""
    return _root_file_bytes((_control_state()/"validation-results") / f"{key}-{operation}.json")


def _sealed_validation_artifact(native: dict, *, key: str, operation: str, child: str, parent: str) -> tuple[dict, str]:
    reference = native.get("sealed_artifact")
    common = {"job_key": key, "operation": operation, "invocation": child, "parent_invocation": parent,
        "artifact_sha256": native.get("artifact_sha256")}
    if (not isinstance(reference, dict) or set(reference) != {"schema", "envelope_sha256", *common}
        or reference.get("schema") != "eimemory.deployment_validation_artifact.v1"
        or any(reference.get(name) != value for name, value in common.items())
        or re.fullmatch(r"[0-9a-f]{64}", str(common["artifact_sha256"])) is None):
        raise ValueError("deployment_validation_root_artifact_reference_invalid")
    raw = _root_validation_envelope(key, operation)
    if sha256(raw).hexdigest() != reference.get("envelope_sha256"):
        raise ValueError("deployment_validation_root_envelope_digest_mismatch")
    envelope = json.loads(raw)
    if (not isinstance(envelope, dict) or set(envelope) != {"schema", "raw_hex", *common}
        or envelope.get("schema") != "eimemory.deployment_validation_seal.v1"
        or any(envelope.get(name) != value for name, value in common.items())
        or not isinstance(envelope.get("raw_hex"), str) or len(envelope["raw_hex"]) > 8 * 1024 * 1024
        or re.fullmatch(r"(?:[0-9a-f]{2})*", envelope["raw_hex"]) is None):
        raise ValueError("deployment_validation_root_envelope_identity_invalid")
    artifact_raw = bytes.fromhex(envelope["raw_hex"])
    if sha256(artifact_raw).hexdigest() != common["artifact_sha256"]:
        raise ValueError("deployment_validation_root_artifact_digest_mismatch")
    artifact = json.loads(artifact_raw)
    if not isinstance(artifact, dict) or canonical_bytes(artifact) != artifact_raw:
        raise ValueError("deployment_validation_root_artifact_encoding_invalid")
    return artifact, common["artifact_sha256"]


def run_validation(runtime, *, key: str, operation: str) -> dict:
    """Fixed controller dispatch; the actual target interpreter performs gates."""
    from eimemory.governance.evolution.code_evolution_transaction import effect_execution_authorized
    from eimemory.storage.code_evolution_store import CodeEvolutionStore
    body, native = validation_context(key, operation)
    release = Path(__file__).resolve().parents[3]
    if (release.parent.name != "releases" or release.name != body["controller_commit"]
        or os.name != "posix" or not os.statvfs(release).f_flag & os.ST_RDONLY
        or not os.statvfs(release / "eimemory").f_flag & os.ST_RDONLY):
        raise ValueError("deployment_validation_actual_source_not_readonly")
    invocation = native["original_invocation"]
    context = _validation_artifact_context(key, operation, body, native)
    _directory(JOBS_ROOT / key, create=True)
    _exclusive(JOBS_ROOT / key / f"validation-{operation}-started.json", context)
    target_transport = {"exit_status": None, "executed": None}
    try:
        transaction = None
        if body["manual_release"]:
            from eimemory.core.runtime_manager import read_registry
            registry = read_registry()
            policy = {"deployment": {"current_link": "/opt/eimemory/current", "health_url": "http://127.0.0.1:8091/health"}}
            if operation == "pre_observation":
                raise ValueError("deployment_manual_has_no_automatic_observation")
            if operation == "receipt":
                arguments = dict(scope=body["scope"], repo_root=registry["repository_root"],
                    current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"],
                    prior_commit=body.get("rollback_commit", body["base_commit"]) if body["target_commit"] == body["candidate_commit"] else body["candidate_commit"],
                    deployed_commit=body["target_commit"])
            else:
                if body["target_commit"] != body["candidate_commit"]:
                    raise ValueError("deployment_manual_business_requires_candidate")
                arguments = native
        else:
            transaction = CodeEvolutionStore(runtime.store).get_transaction(body["transaction_id"])
            original = {name: value for name, value in body.items()
                if name not in {"manual_release", "launch_controller_commit"}}
            if (not transaction or not effect_execution_authorized(transaction)
                or _bound_request(transaction, body["operation"]) != (key, original)):
                raise ValueError("deployment_validation_transaction_changed")
            policy = transaction["payload"]["authorized_policy"]
            if operation == "receipt":
                expected = "DEPLOY_INTENT" if body["operation"] == "deploy" else "ROLLBACK_INTENT"
                if transaction.get("current_state") != expected:
                    raise ValueError("deployment_validation_receipt_state_invalid")
                arguments = dict(scope=body["scope"], repo_root=transaction["repository_root"],
                    current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"],
                    prior_commit=body["base_commit"] if body["operation"] == "deploy" else body["candidate_commit"],
                    deployed_commit=body["target_commit"])
                if body["operation"] == "deploy":
                    from eimemory.governance.evolution.code_evolution_effects import _code_evolution_lineage
                    arguments.update(strict_transaction=True, transaction_id=body["transaction_id"],
                        **{name: body[name] for name in ("authorization_digest", "policy_digest", "patch_digest",
                            "candidate_tree_digest", "verification_receipt_digests", "observation_deadline")},
                        provider_implementation_digest=transaction["implementation_digest"],
                        code_evolution_lineage=_code_evolution_lineage(transaction))
            else:
                if body["operation"] != "deploy":
                    raise ValueError("deployment_validation_business_requires_forward_job")
                if operation == "pre_observation":
                    receipt, _ = managed_validation_report(key, "receipt", heartbeat=lambda: None)
                    arguments = dict(receipt=receipt, transaction_id=body["transaction_id"], identity_kwargs=dict(
                        scope=body["scope"], repo_root=transaction["repository_root"],
                        current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"],
                        prior_commit=body["base_commit"], deployed_commit=body["target_commit"]))
                else:
                    arguments = native
        result, target_transport = _target_validation_report(runtime, key=key, operation=operation,
            body=body, transaction=transaction, arguments=arguments)
        if body["manual_release"] is True and operation == "post_resume" and result.get("ok") is True:
            from deploy.run_code_deployment_validation import verify_worker_finalization
            result["worker_finalization"] = verify_worker_finalization(runtime, body=body, report=result)
        _, after = validation_context(key, operation)
        if any(native.get(name) != after.get(name) for name in ("unit", "original_invocation",
            "parent_original_invocation", "target_commit", "controller_commit", "phase", "manual_release", "launch_controller_commit")):
            raise ValueError("deployment_validation_native_context_changed")
        if (Path(policy["deployment"]["current_link"]).resolve(strict=True) != release.parent / body["target_commit"]
            or not os.statvfs(release).f_flag & os.ST_RDONLY):
            raise ValueError("deployment_validation_current_or_source_changed")
    except Exception as exc:
        result = {"ok": False, "business_qualified": False,
            "blocked_reason": "deployment_validation_failed", "error_type": type(exc).__name__,
            "error_code": str(exc) if re.fullmatch(r"[a-z0-9_]{1,120}", str(exc)) else "validation_exception"}
    artifact = {**context, "report": result, "target_transport": target_transport}
    raw = canonical_bytes(artifact)
    _exclusive_blob(JOBS_ROOT / key / f"validation-{operation}-result.json", raw)
    from eimemory.core.runtime_manager import seal_deployment_validation_result
    sealed = seal_deployment_validation_result(key, operation, invocation, sha256(raw).hexdigest())
    if (sealed.get("artifact_sha256") != sha256(raw).hexdigest()
        or not isinstance(sealed.get("sealed_artifact"), dict)):
        raise ValueError("deployment_validation_child_seal_unverified")
    stored, _ = _sealed_validation_artifact(sealed, key=key, operation=operation,
        child=invocation, parent=context["parent_invocation"])
    if stored != artifact:
        raise ValueError("deployment_validation_child_seal_original_mismatch")
    return result


def _target_validation_report(runtime, *, key, operation, body, transaction, arguments):
    """No old CLI flags or controller-as-target identity; all source is RO."""
    from eimemory.governance.evolution.code_evolution_effects import _run_native_json_report
    target = Path("/opt/eimemory/releases") / body["target_commit"]
    if target.is_symlink() or not os.statvfs(target).f_flag & os.ST_RDONLY:
        raise ValueError("deployment_validation_target_not_readonly")
    program = '''
import inspect,json,os,sys
from dataclasses import asdict
from pathlib import Path
expected=Path(sys.argv[1]);key=sys.argv[2];operation=sys.argv[3];arguments=json.loads(sys.argv[4])
sys.path.insert(0,str(expected))
import eimemory
if Path(eimemory.__file__).resolve().parent.parent!=expected or not os.statvfs(expected).f_flag & os.ST_RDONLY:
    raise SystemExit('deployment_validation_target_import_or_mount_invalid')
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
runtime=Runtime.create(root=sys.argv[5], **({"bootstrap_schema":False} if "bootstrap_schema" in inspect.signature(Runtime.create).parameters else {}))
try:
    if operation=='receipt':
        if asdict(ScopeRef.from_dict(arguments['scope']))!=arguments['scope']:
            raise SystemExit('deployment_validation_legacy_exact_scope_unsupported')
        if not set(arguments)<=set(inspect.signature(runtime.verify_and_record_deployment).parameters):
            raise SystemExit('deployment_validation_legacy_native_api_unsupported')
        report=runtime.verify_and_record_deployment(**arguments)
    elif operation=='pre_observation':
        from eimemory.governance.release.release_pre_observation import run_pre_observation_closure
        report=run_pre_observation_closure(runtime,**arguments)
    elif operation=='post_resume':
        from eimemory.governance.release.release_closure import run_managed_post_resume_closure,run_manual_post_resume_closure
        entry=run_manual_post_resume_closure if arguments.get('manual_release') is True else run_managed_post_resume_closure
        report=entry(runtime,job_key=key,validation_context=arguments)
    else:
        raise SystemExit('deployment_validation_operation_invalid')
    if Path(eimemory.__file__).resolve().parent.parent!=expected or not os.statvfs(expected).f_flag & os.ST_RDONLY:
        raise SystemExit('deployment_validation_target_changed')
    readback=None
    if operation=='receipt' and report.get('ok') is True:
        runtime.close()
        runtime=Runtime.create(root=sys.argv[5], **({'bootstrap_schema':False} if 'bootstrap_schema' in inspect.signature(Runtime.create).parameters else {}))
        from eimemory.governance.release import evidence_contract
        from eimemory.version import __version__
        owner=ScopeRef.from_dict(arguments['scope'])
        reader=getattr(evidence_contract,'current_deployment_receipt_for_owner',None)
        if callable(reader):
            identity=reader(runtime,report.get('promotion_request_id'),owner,
                expected_commit=expected.name,expected_version=__version__)
        else:
            # The installed 154 rollback target predates the new owner reader.
            # Its original shared current identity is still necessary, but is
            # never sufficient evidence of this newly written receipt's owner.
            if (__version__!='1.14.154'
                or expected.name!='f14770f01e17984d0cb7945baaebeb1db63502ab'):
                raise SystemExit('deployment_validation_exact_owner_reader_unavailable')
            shared=evidence_contract.current_release_identity(runtime,owner)
            record=runtime.store.get_by_id(report.get('promotion_request_id'),scope=owner,exact_scope=True)
            physical=evidence_contract.verified_deployment_receipt_identity(record)
            if (record is None or not evidence_contract.same_scope(record.scope,owner)
                or record.record_id!=report.get('promotion_request_id')
                or evidence_contract._runtime_commit(runtime)!=expected.name
                or shared is None or shared.commit!=expected.name or shared.version!=__version__
                or physical is None or physical.receipt_id!=report.get('promotion_request_id')
                or physical.commit!=expected.name or physical.version!=__version__):
                identity=None
            else:
                identity=physical
        if identity is None or identity.commit!=expected.name or identity.receipt_id!=report.get('promotion_request_id') or identity.version!=report.get('version'):
            raise SystemExit('current_deployment_receipt_unavailable')
        readback={'current_deployment_receipt_verified':True,'commit':identity.commit,'receipt_id':identity.receipt_id}
    if operation=='pre_observation':
        from eimemory.governance.release.release_pre_observation import pre_observation_report_ok
        passed=pre_observation_report_ok(report)
    else:
        passed=report.get('ok') is True and (operation=='receipt' or report.get('business_qualified') is True)
    capture=None
    if operation in {'pre_observation','post_resume'}:
        from eimemory.governance.evolution.code_deployment_job import JOBS_ROOT,_exclusive_blob,canonical_bytes
        from eimemory.ops.closure_capture import process_capture
        source=JOBS_ROOT/key/('validation-'+operation+'-report.json')
        _exclusive_blob(source,canonical_bytes(report))
        scope=report.get('scope') or arguments.get('identity_kwargs',{}).get('scope')
        if not isinstance(scope,dict):
            raise SystemExit('deployment_validation_capture_scope_missing')
        capture=process_capture(source,evidence_dir=Path(sys.argv[5])/'logs/release-closure-captures',
            scope=scope,expected_commit=expected.name,attempt_id=key,closure_exit_status=0 if passed else 1,
            runtime_factory=lambda:Runtime.create(root=sys.argv[5],bootstrap_schema=False))
        passed=passed and capture.get('recording_ok') is True and capture.get('capture_saved') is True and capture.get('exit_code')==0
    print(json.dumps({'report':report,'source_commit':expected.name,'source_readonly':True,'physical_receipt_readback':readback,'closure_capture':capture},sort_keys=True,allow_nan=False))
    raise SystemExit(0 if passed else 1)
finally:
    runtime.close()
'''
    env = dict(os.environ, EIMEMORY_RUNTIME_COMMIT=body["target_commit"])
    argv = [str(target / ".venv/bin/python"), "-I", "-B", "-X",
        "pycache_prefix="+(str(coordinates()["pycache"]) if user_mode() else "/var/lib/eimemory-manager/empty-pycache"), "-c", program,
        str(target), key, operation, json.dumps(arguments, sort_keys=True, separators=(",", ":")), str(runtime.store.root)]
    envelope, transport, raw, errors = _run_native_json_report(argv, cwd=target, env=env, heartbeat=lambda: None,
        max_seconds=VALIDATION_BUDGETS[operation][0])
    transport["executed"] = True
    _exclusive_blob(JOBS_ROOT / key / f"validation-{operation}-target.stdout", raw)
    _exclusive_blob(JOBS_ROOT / key / f"validation-{operation}-target.stderr", errors)
    if (set(envelope) != {"report", "source_commit", "source_readonly", "physical_receipt_readback", "closure_capture"}
        or envelope.get("source_commit") != body["target_commit"] or envelope.get("source_readonly") is not True
        or not isinstance(envelope.get("report"), dict)):
        raise ValueError("deployment_validation_target_report_unverified")
    transport["physical_receipt_readback"] = envelope["physical_receipt_readback"]
    transport["closure_capture"] = envelope["closure_capture"]
    if operation == "receipt" and envelope["report"].get("ok") is True:
        readback = envelope["physical_receipt_readback"]
        if readback != {"current_deployment_receipt_verified": True, "commit": body["target_commit"],
            "receipt_id": envelope["report"].get("promotion_request_id")}:
            raise ValueError("deployment_validation_physical_receipt_unverified")
    result = envelope["report"]
    if transport["exit_status"] != 0:
        result = {**result, "ok": False, "business_qualified": False}
    return result, transport


def managed_validation_report(key: str, operation: str, *, heartbeat) -> tuple[dict, dict]:
    """One root-managed child per key/op; an unknown result is never rerun."""
    from eimemory.core.runtime_manager import validate_deployment_job, deployment_validation_status
    unit = _validation_unit(key, operation)
    native = validate_deployment_job(key, operation)
    body, native = validation_binding(key, operation, native)
    parent = {"request_key": key, "invocation": native["parent_original_invocation"]}
    begin = time.monotonic()
    deadline = begin + VALIDATION_BUDGETS[operation][2]
    next_heartbeat = begin
    while True:
        state = native.get("state") if isinstance(native.get("state"), dict) else {}
        if (native.get("job_key") != key or native.get("operation") != operation
            or native.get("manager") != _manager_name() or native.get("unit") != unit
            or native.get("target_commit") != body["target_commit"]
            or native.get("controller_commit") != body["controller_commit"]
            or native.get("parent_original_invocation") != parent.get("invocation")
            or parent.get("request_key") != key
            or native.get("artifact_basename") != f"validation-{operation}-result.json"):
            raise ValueError("deployment_validation_root_identity_changed")
        child = native.get("original_invocation")
        if child:
            if (re.fullmatch(r"[0-9a-f]{32}", str(child)) is None
                or state.get("InvocationID") not in {"", None, child}):
                raise ValueError("deployment_validation_invocation_changed")
            sealed = native.get("terminal_state")
            if sealed is not None:
                retained_exit = (isinstance(sealed, dict)
                    and sealed.get("ActiveState") == "active" and sealed.get("SubState") == "exited"
                    and sealed.get("MainPID") == "0"
                    and re.fullmatch(r"[1-9][0-9]*", str(sealed.get("ExecMainExitTimestampMonotonic", ""))) is not None
                    and sealed.get("Result") == "success" and sealed.get("ExecMainStatus") == "0")
                if (not isinstance(sealed, dict) or sealed.get("InvocationID") != child
                    or sealed.get("LoadState") != "loaded"
                    or (sealed.get("ActiveState") not in {"inactive", "failed"} and not retained_exit)
                    or re.fullmatch(r"[0-9a-f]{64}", str(native.get("artifact_sha256"))) is None):
                    raise ValueError("deployment_validation_terminal_seal_invalid")
                if state.get("ActiveState") in {"active", "activating"}:
                    fields = ("LoadState", "ActiveState", "SubState", "InvocationID", "MainPID",
                        "ExecMainExitTimestampMonotonic", "Result", "ExecMainStatus")
                    if not retained_exit or any(state.get(name) != sealed.get(name) for name in fields):
                        raise ValueError("deployment_validation_terminal_seal_invalid")
                state = sealed
                break
            # A collected unit reports inactive/default exit status zero even
            # when PID1's original exit was never observed. Only the root's
            # independently pinned native terminal state can complete a child.
            if state.get("LoadState") == "not-found" or state.get("ActiveState") in {"inactive", "failed"}:
                raise ValueError("deployment_validation_terminal_state_unsealed")
        elif time.monotonic() - begin >= 30:
            raise RuntimeError("deployment_validation_start_unknown")
        if time.monotonic() >= deadline:
            raise TimeoutError("deployment_validation_result_unknown")
        if time.monotonic() >= next_heartbeat:
            heartbeat()
            next_heartbeat = time.monotonic() + 30
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        native = deployment_validation_status(key, operation)
    artifact, artifact_digest = _sealed_validation_artifact(native, key=key, operation=operation,
        child=child, parent=parent["invocation"])
    expected = _validation_artifact_context(key, operation, body, native)
    if (set(artifact) != set(expected) | {"report", "target_transport"} or any(artifact.get(name) != value for name, value in expected.items())
        or not isinstance(artifact.get("report"), dict)
        or not isinstance(artifact.get("target_transport"), dict)
        or native.get("artifact_sha256") != artifact_digest
        or _read(JOBS_ROOT / key / f"validation-{operation}-started.json") != expected):
        raise ValueError("deployment_validation_artifact_identity_changed")
    try:
        exit_status = int(state["ExecMainStatus"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("deployment_validation_exit_unknown") from None
    if state.get("Result") != "success" and exit_status == 0:
        raise ValueError("deployment_validation_native_result_contradiction")
    if exit_status == 0 and artifact["report"].get("ok") is True and (
        artifact["target_transport"].get("exit_status") != 0 or artifact["target_transport"].get("executed") is not True):
        raise ValueError("deployment_validation_target_result_contradiction")
    report = dict(artifact["report"])
    if exit_status != 0 or state.get("Result") != "success":
        report.update(ok=False, business_qualified=False)
    transport = {"exit_status": exit_status, "manager": _manager_name(), "unit": unit,
        "invocation": child, "parent_invocation": parent["invocation"], "source_readonly": True,
        "artifact_digest": artifact_digest, "native_state": state, "current_native_state": native.get("state"),
        "target_transport": artifact["target_transport"]}
    return report, transport


def run_job(runtime, key: str, *, wait_seconds: float = 310) -> dict:
    """The fixed low-privilege entry; parent lease loss never overlaps effects."""
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager, effect_execution_authorized
    from eimemory.storage.code_evolution_store import CodeEvolutionConflict
    from eimemory.governance.evolution.code_evolution_effects import ProductionEffectAdapter
    if re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise ValueError("deployment_job_key_invalid")
    body = validate_request(_read(JOBS_ROOT / key / "request.json"))
    if request_key(body) != key or body["controller_commit"] != _controller_commit():
        raise ValueError("deployment_job_controller_or_key_mismatch")
    invocation = os.environ.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ValueError("deployment_job_native_invocation_required")
    manager = CodeEvolutionTransactionManager(runtime, owner_id=f"deployment-job:{key}:{invocation}")
    deadline = time.monotonic() + min(max(wait_seconds, 0), 310)
    while True:
        try:
            manager.acquire_lease(body["transaction_id"])
            break
        except CodeEvolutionConflict:
            if time.monotonic() >= deadline:
                raise RuntimeError("deployment_job_parent_lease_not_released") from None
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    try:
        tx = manager.store.get_transaction(body["transaction_id"])
        policy = (tx.get("payload") or {}).get("authorized_policy") or {} if tx else {}
        expected = "DEPLOY_INTENT" if body["operation"] == "deploy" else "ROLLBACK_INTENT"
        if (not tx or tx.get("current_state") != expected or not effect_execution_authorized(tx)
            or policy.get("ok") is not True or policy.get("policy_digest") != body["policy_digest"]
            or (policy.get("effects") or {}).get("deployment" if body["operation"] == "deploy" else "rollback") is not True
            or _bound_request(tx, body["operation"]) != (key, body)):
            raise ValueError("deployment_job_authorization_mismatch")
        receipts = manager.store.list_verification_receipts(body["transaction_id"])
        passed = sorted(r["receipt_digest"] for r in receipts if r.get("result") == "pass" and r.get("exit_status") == 0)
        if passed != body["verification_receipt_digests"]:
            raise ValueError("deployment_job_verification_receipts_mismatch")
        adapter = ProductionEffectAdapter()
        current = Path(policy["deployment"]["current_link"]).resolve(strict=True)
        if current.parent.name != "releases" or current.name not in {body["base_commit"], body["candidate_commit"]}:
            raise ValueError("deployment_job_current_release_unbound")
        if body["operation"] == "deploy":
            from eimemory.governance.evolution.code_evolution_effects import _dynamic_patch_authority_error, _live_proposal_authority_error
            from eimemory.governance.evolution.code_automation_policy_owner import verify_native_standing_authority
            switch = str(policy.get("kill_switch_path") or "")
            brake = bool(switch and Path(switch).exists())
            if brake or _dynamic_patch_authority_error(runtime, tx, policy=policy) or _live_proposal_authority_error(runtime, tx):
                raise ValueError("deployment_job_live_authority_rejected")
            authority = verify_native_standing_authority(runtime, transaction=tx)
            if authority.get("ok") is not True:
                raise ValueError("deployment_job_standing_authority_rejected")
        # Exclusive execution claim survives crashes. Replaying this unit cannot
        # rerun an installer whose previous result is unknown.
        _exclusive(JOBS_ROOT / key / "started.json", {"request_key": key, "invocation": invocation})
        # Closing is mandatory BEFORE the original installer quiesces writers.
        # The independent native unit/one-shot claim protects this interval;
        # there are no SQLite lease writes during backup/migration/cutover.
        runtime.close()
        runtime_closed = True
        result = adapter.run_managed_installer(runtime, transaction=tx, policy=policy,
            operation=body["operation"], job_key=key)
        _exclusive(JOBS_ROOT / key / "technical-install.json", {
            "request_key": key, "invocation": invocation, "result": result})
        from eimemory.api.runtime import Runtime
        reopened = Runtime.create(root=runtime.store.root, bootstrap_schema=False)
        manager = CodeEvolutionTransactionManager(reopened, owner_id=f"deployment-job:{key}:{invocation}")
        deadline = time.monotonic() + min(max(wait_seconds, 0), 310)
        while True:
            try:
                manager.acquire_lease(body["transaction_id"])
                break
            except CodeEvolutionConflict:
                if time.monotonic() >= deadline:
                    raise RuntimeError("deployment_job_post_install_lease_unavailable") from None
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        fresh = manager.store.get_transaction(body["transaction_id"])
        if not fresh or _bound_request(fresh, body["operation"]) != (key, body):
            raise ValueError("deployment_job_post_install_ledger_changed")
        if fresh.get("current_state") != expected:
            raise ValueError("deployment_job_post_install_state_changed")
        from eimemory.governance.evolution.code_evolution_effects import CodeEvolutionEffectOwner, _storage_release_state
        from eimemory.governance.evolution.code_evolution_transaction import reconcile_rollback
        if result.get("ok") is True:
            adapter._verify_managed_result(reopened, fresh, policy, result, operation=body["operation"])
            if _storage_release_state(reopened) != "clean":
                raise ValueError("deployment_job_storage_commit_unverified")
            if body["operation"] == "deploy":
                payload = {**dict(fresh.get("payload") or {}), "deployment_receipt_digest": result["receipt_digest"],
                    "candidate_pushed_and_deployed": True, "deployment_version": result["version"]}
                fresh = manager.record_result(body["transaction_id"], step="deployment", result_state="DEPLOYED_VERIFIED",
                    output_data={"deployment_receipt_digest": result["receipt_digest"], "job_key": key},
                    updates={"deployed_commit": body["candidate_commit"], "payload_json": payload})
                fresh = manager.record_result(body["transaction_id"], step="health", result_state="HEALTHY",
                    output_data={"deployment_receipt_digest": result["receipt_digest"], "job_key": key})
                from eimemory.core.runtime_manager import finalize_deployment_job
                finalized = _after_storage_call(lambda: finalize_deployment_job(key),
                    manager=manager, transaction_id=body["transaction_id"])
                _exclusive(JOBS_ROOT / key / "finalized.json", {"request_key": key, "invocation": invocation, "native": finalized})
                if (finalized.get("job_key") != key or finalized.get("resumed") is not True
                    or finalized.get("phase") != "resumed_awaiting_business"):
                    result.update(ok=False, deployment_verified=True, automation_resumed=False,
                                  status="held_background", finalization=finalized)
                else:
                    business = adapter.run_post_resume_business(reopened, transaction=fresh, policy=policy,
                        job_key=key, heartbeat=lambda: manager.renew_lease(body["transaction_id"]))
                    _exclusive(JOBS_ROOT / key / "post-resume-business.json", business)
                    business_digest = sha256(canonical_bytes(business)).hexdigest()
                    result.update(post_resume_business_digest=business_digest,
                                  business_qualified=business.get("business_qualified") is True)
                    if business.get("ok") is not True or business.get("business_qualified") is not True:
                        result.update(ok=False, deployment_verified=True, reason="post_resume_business_not_qualified")
                    else:
                        adapter._verify_managed_result(reopened, fresh, policy, result, operation="deploy")
                        fresh = manager.update_metadata(body["transaction_id"], payload_updates={
                            "post_resume_business_digest": business_digest, "post_resume_business_job_key": key,
                            "post_resume_business_invocation": invocation,
                            "post_resume_business_record_ids": business.get("record_ids") or {}})
                        error = post_resume_business_error(reopened, fresh)
                        if error:
                            raise ValueError(error)
                        owner = CodeEvolutionEffectOwner(reopened, owner_id=manager.owner_id, adapter=adapter,
                            policy_loader=lambda: policy, policy_consumer=lambda **kw: None)
                        fresh = owner._start_observing(body["transaction_id"], fresh, policy)
                        result.update(deployment_verified=True, automation_resumed=True,
                                      business_qualified=True, status="observing")
            else:
                decision = reconcile_rollback({"current_commit": result["commit"], "prior_commit": body["base_commit"],
                    "receipt_valid": True, "health_ok": True, "storage_clean": _storage_release_state(reopened) == "clean"})
                fresh = manager.reconcile(body["transaction_id"], step="rollback", decision=decision,
                    success_state="ROLLED_BACK_HEALTHY" if decision.status == "rolled_back_healthy" else None)
            result["ledger_state"] = fresh["current_state"]
        elif body["operation"] == "deploy":
            fresh = manager.begin_intent(body["transaction_id"], step="rollback", intent_state="ROLLBACK_INTENT",
                input_data={"deployment_evidence_digest": sha256(canonical_bytes(result)).hexdigest(), "job_key": key})
        else:
            fresh = manager.reconcile(body["transaction_id"], step="rollback",
                decision=reconcile_rollback({"current_commit": "", "prior_commit": body["base_commit"],
                    "receipt_valid": False, "health_ok": False, "storage_clean": False}))
            result["ledger_state"] = fresh["current_state"]
        if body["operation"] == "deploy" and result.get("ok") is not True and fresh["current_state"] != "ROLLBACK_INTENT":
            fresh = manager.begin_intent(body["transaction_id"], step="rollback", intent_state="ROLLBACK_INTENT",
                input_data={"deployment_evidence_digest": sha256(canonical_bytes(result)).hexdigest(), "job_key": key})
        result["ledger_state"] = fresh["current_state"]
        if result.get("ok") is True and body["operation"] == "rollback" and fresh["current_state"] == "ROLLED_BACK_HEALTHY":
            # Native background writers stay held until their transaction has
            # durably advanced. In particular, an old rollback watch cannot
            # race the worker and replay an installer it does not understand.
            from eimemory.core.runtime_manager import finalize_deployment_job
            finalized = _after_storage_call(lambda: finalize_deployment_job(key),
                manager=manager, transaction_id=body["transaction_id"])
            _exclusive(JOBS_ROOT / key / "finalized.json", {"request_key": key, "invocation": invocation, "native": finalized})
            if finalized.get("job_key") != key or finalized.get("resumed") is not True:
                result.update(ok=False, deployment_verified=True, automation_resumed=False,
                              status="held_background", finalization=finalized)
            else:
                result.update(deployment_verified=True, automation_resumed=True, status="rolled_back_healthy")
        if (body["operation"] == "deploy" and fresh["current_state"] == "OBSERVING" and result.get("ok") is True
            or body["operation"] == "rollback" and fresh["current_state"] == "ROLLED_BACK_HEALTHY"):
            from eimemory.core.runtime_manager import complete_deployment_job
            # Root retains the global transition until real business admission
            # and the observation anchor (or a rollback terminal) are durable.
            try:
                completed = _after_storage_call(lambda: complete_deployment_job(key),
                    manager=manager, transaction_id=body["transaction_id"])
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                completed = {"job_key": key, "complete": False, "error_type": type(exc).__name__}
            _exclusive(JOBS_ROOT / key / "completed.json", {
                "request_key": key, "invocation": invocation, "native": completed})
            result["completion"] = completed
            held = result.get("status") == "held_background"
            expected_phase = "completed_held" if held else "completed"
            if (completed.get("job_key") != key or completed.get("complete") is not True
                or completed.get("phase") != expected_phase or completed.get("resumed") is not (not held)
                or completed.get("operational_complete") is not (not held)):
                result.update(ok=False, status="native_completion_unknown", reconciliation_required=True)
            result["operational_complete"] = result.get("ok") is True and not held
        else:
            result["operational_complete"] = False
        # Final outcome is published only after restoration/business/complete.
        # The immutable technical-install artifact keeps the physical effect
        # separate from a held or unknown operational result.
        _exclusive(JOBS_ROOT / key / "result.json", {"request_key": key, "invocation": invocation, "result": result})
        if body["operation"] == "deploy" and result.get("ok") is not True and fresh["current_state"] == "ROLLBACK_INTENT":
            handoff(reopened, fresh, policy, operation="rollback")
        return result
    except Exception as exc:
        # A crash/exception after the exclusive execution claim is an unknown
        # effect, not permission to rerun the installer or declare success.
        # Preserve a bounded, non-secret diagnostic even when no Runtime/SQL
        # connection can safely be used for reconciliation at this point.
        try:
            if (_read(JOBS_ROOT / key / "started.json") == {"request_key": key, "invocation": invocation}
                and not (JOBS_ROOT / key / "result.json").exists()):
                _exclusive(JOBS_ROOT / key / "result.json", {"request_key": key, "invocation": invocation,
                    "result": {"ok": False, "operational_complete": False,
                        "status": "job_execution_unknown", "reconciliation_required": True,
                        "error_type": type(exc).__name__}})
        except (OSError, ValueError):
            # The original failure remains the native unit's failure; an
            # unavailable private artifact cannot turn it into success.
            pass
        raise
    finally:
        try:
            if "reopened" in locals() or not locals().get("runtime_closed", False):
                try:
                    manager.release_lease(body["transaction_id"])
                except CodeEvolutionConflict:
                    # State/receipt are already durable before native resume;
                    # a freshly resumed watcher may now own this transaction.
                    pass
        finally:
            if "reopened" in locals():
                reopened.close()
