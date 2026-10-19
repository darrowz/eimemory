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

from eimemory.contracts.code_deployment_job import SCHEMA, canonical_bytes, request_key, validate_request

JOBS_ROOT = Path("/var/lib/eimemory/state/code-deployment-jobs")

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
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or parent.is_symlink():
            raise ValueError("deployment_job_directory_unsafe")
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("deployment_job_directory_not_private")

def _read(path: Path) -> dict:
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
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536
                or (os.name == "posix" and (before.st_uid != os.geteuid() or before.st_mode & 0o077))):
            raise ValueError("deployment_job_file_unsafe")
        raw = os.read(fd, 65537)
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
    _directory(path.parent)
    parent = _parent_fd(path)
    try:
        fd = os.open(path.name if parent is not None else path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600, dir_fd=parent)
    except BaseException:
        if parent is not None:
            os.close(parent)
        raise
    try:
        raw = canonical_bytes(value)
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
    invocation = state.get("InvocationID", "")
    evidence = {"job_key": key, "operation": body["operation"], "unit": f"eimemory-code-deployment-job@{key}.service",
                "native_invocation": invocation, "native_state": state, "pending": True,
                "reason": "managed_deployment_job_awaiting_reconciliation"}
    try:
        result = _read(JOBS_ROOT / key / "result.json")
        started = _read(JOBS_ROOT / key / "started.json")
    except FileNotFoundError:
        # Absence remains explicit even if the native unit is currently unknown.
        evidence["reconciliation_required"] = state.get("ActiveState") not in {"active", "activating", "deactivating"}
        return evidence
    if (set(result) != {"request_key", "invocation", "result"} or result["request_key"] != key
        or started != {"request_key": key, "invocation": result["invocation"]}
        or not isinstance(result["invocation"], str) or not re.fullmatch(r"[0-9a-f]{32}", result["invocation"])
        or (invocation and invocation != result["invocation"]) or not isinstance(result["result"], dict)):
        raise ValueError("deployment_job_result_identity_mismatch")
    evidence.update(pending=False, native_invocation=result["invocation"], result=result["result"],
                    result_digest=sha256(canonical_bytes(result)).hexdigest())
    return evidence

def pending(runtime, transaction: Mapping) -> dict | None:
    """Re-read a real bound request; caller-supplied flags cannot defer recovery."""
    operation = {"DEPLOY_INTENT": "deploy", "ROLLBACK_INTENT": "rollback"}.get(transaction.get("current_state"))
    if not operation:
        return None
    binding = _bound_request(transaction, operation)
    if binding is None:
        return None
    evidence = job_state(*binding)
    # An absent/terminal/unknown native unit is not an endless pending lease.
    # The normal physical readback must decide success or explicit quarantine.
    active = evidence["native_state"].get("ActiveState") in {"active", "activating", "deactivating"}
    return evidence if evidence["pending"] and active else None

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
        from eimemory.api.runtime import Runtime
        reopened = Runtime.create(root=runtime.store.root)
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
            if body["operation"] == "deploy":
                payload = {**dict(fresh.get("payload") or {}), "deployment_receipt_digest": result["receipt_digest"],
                    "candidate_pushed_and_deployed": True, "deployment_version": result["version"]}
                fresh = manager.record_result(body["transaction_id"], step="deployment", result_state="DEPLOYED_VERIFIED",
                    output_data={"deployment_receipt_digest": result["receipt_digest"], "job_key": key},
                    updates={"deployed_commit": body["candidate_commit"], "payload_json": payload})
                fresh = manager.record_result(body["transaction_id"], step="health", result_state="HEALTHY",
                    output_data={"deployment_receipt_digest": result["receipt_digest"], "job_key": key})
                owner = CodeEvolutionEffectOwner(reopened, owner_id=manager.owner_id, adapter=adapter,
                    policy_loader=lambda: policy, policy_consumer=lambda **kw: None)
                fresh = owner._start_observing(body["transaction_id"], fresh, policy)
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
        _exclusive(JOBS_ROOT / key / "result.json", {"request_key": key, "invocation": invocation, "result": result})
        if result.get("ok") is True and fresh["current_state"] in {"OBSERVING", "ROLLED_BACK_HEALTHY"}:
            # Native background writers stay held until their transaction has
            # durably advanced. In particular, an old rollback watch cannot
            # race the worker and replay an installer it does not understand.
            from eimemory.core.runtime_manager import finalize_deployment_job
            finalized = finalize_deployment_job(key)
            _exclusive(JOBS_ROOT / key / "finalized.json", {"request_key": key, "invocation": invocation, "native": finalized})
        elif body["operation"] == "deploy":
            handoff(reopened, fresh, policy, operation="rollback")
        return result
    finally:
        try:
            if "reopened" in locals() or not locals().get("runtime_closed", False):
                manager.release_lease(body["transaction_id"])
        finally:
            if "reopened" in locals():
                reopened.close()
