"""Automatic code.evolution release authorization (``code-evolution-auto-authorization.v1``).

The user has auto-authorized self-evolution. When a release changes
evolution-engine paths that the ordinary deployment receipt does not cover,
release lineage accepts either:

* a strict code-evolution transaction receipt (the human/policy path), or
* a signed automatic authorization minted here by a distinct authority,
  ``code-evolution-auto-authorizer``. It is never an operator identity and never
  a forged strict receipt.

An automatic authorization is an HMAC-signed record over the evidence-receipt
keyring. It carries the policy version, the exact deployment receipt, the
release and ancestor commits, the changed domains and a digest of the changed
evolution paths. It is minted only when a verified current deployment receipt
and a verified deployed ancestor exist, the diff has no unknown production
paths, the policy flag is on and the code-evolution kill switch is absent.
Revocation records withdraw it, and so does turning the flag off. Every other
lineage domain gate (recall, governance, channel, storage, deployment runtime)
is unchanged and still has to pass on its own evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
from typing import Any

from eimemory.governance.release.evidence_contract import ReleaseIdentity, same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef

AUTO_AUTHORIZATION_FLAG = "EIMEMORY_CODE_EVOLUTION_AUTO_AUTHORIZATION"
POLICY_VERSION = "code-evolution-auto-authorization.v1"
SCHEMA = "code_evolution_auto_authorization.v1"
AUTHORITY = "code-evolution-auto-authorizer"
AUTHORIZATION_CLASS = "automatic"
SOURCE = "eimemory.code_evolution.auto_authorization"
REVOCATION_SOURCE = "eimemory.code_evolution.auto_authorization.revocation"
KIND = "l5_self_continuity"
_DISABLED = frozenset({"0", "false", "no", "off", "disabled"})


def auto_authorization_enabled() -> bool:
    """Automatic code.evolution authorization is on unless explicitly disabled."""

    value = str(os.environ.get(AUTO_AUTHORIZATION_FLAG, "1")).strip().lower()
    return value not in _DISABLED


def _kill_switch_present() -> bool:
    from eimemory.governance.evolution.code_automation_policy import (
        CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH,
        CODE_EVOLUTION_KILL_SWITCH_ENV,
    )

    switch = Path(os.environ.get(CODE_EVOLUTION_KILL_SWITCH_ENV) or CODE_EVOLUTION_KILL_SWITCH_DEFAULT_PATH)
    try:
        return switch.exists() or switch.is_symlink()
    except OSError:
        return True


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _signature(key: str, body: dict[str, Any]) -> str:
    return hmac.new(key.encode(), (SCHEMA + ":" + _digest(body)).encode(), sha256).hexdigest()


def _authorization_key(current: ReleaseIdentity, ancestor: ReleaseIdentity) -> str:
    return _digest({
        "schema": SCHEMA,
        "receipt_id": current.receipt_id,
        "release_commit": current.commit,
        "ancestor_receipt_id": ancestor.receipt_id,
        "ancestor_commit": ancestor.commit,
    })


def _expected_body(
    *,
    scope: ScopeRef,
    current: ReleaseIdentity,
    ancestor: ReleaseIdentity,
    evolution_paths: list[str],
    changed_domains: list[str],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "policy_version": POLICY_VERSION,
        "authority": AUTHORITY,
        "authorization_class": AUTHORIZATION_CLASS,
        "operator_authorization": False,
        "authorization_key": _authorization_key(current, ancestor),
        "scope": {
            "tenant_id": scope.tenant_id, "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id, "user_id": scope.user_id,
        },
        "release_commit": current.commit,
        "release_version": current.version,
        "deployment_receipt_id": current.receipt_id,
        "release_session_id": current.session_id,
        "ancestor_commit": ancestor.commit,
        "ancestor_receipt_id": ancestor.receipt_id,
        "changed_domains": sorted(set(changed_domains)),
        "code_evolution_changed_paths": sorted(set(evolution_paths)),
        "code_evolution_changed_paths_digest": _digest(sorted(set(evolution_paths))),
    }


def _idempotency_key(source: str, authorization_key: str) -> str:
    return f"{source}:{authorization_key}"


def _records(runtime: Any, *, scope: ScopeRef, source: str, key: str) -> list[RecordEnvelope]:
    """Indexed lookup (records.idempotency_key); never a JSON full-table scan."""

    from eimemory.governance.learning.learning_state import find_record_by_idempotency

    record = find_record_by_idempotency(
        runtime, kinds=[KIND], scope=scope, idempotency_key=_idempotency_key(source, key),
    )
    if record is None or record.source != source or not same_scope(record.scope, scope):
        return []
    return [record]


def mint_code_evolution_auto_authorization(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity,
    ancestor_release: ReleaseIdentity,
    evolution_paths: list[str],
    changed_domains: list[str],
    unknown_paths: list[str],
) -> dict[str, Any]:
    """Mint (idempotently) a signed automatic authorization for this release.

    The caller has already verified the current deployment receipt and the
    deployed ancestor receipt. Nothing is minted when the policy is off, the
    kill switch is present, the diff is not fully classified or the signing
    keyring is unavailable. Missing keys are never replaced by a default.
    """

    from eimemory.governance.tool_receipts import receipt_key_set

    if not evolution_paths:
        return {"ok": False, "minted": False, "reason": "no_code_evolution_change"}
    if not auto_authorization_enabled():
        return {"ok": False, "minted": False, "reason": "auto_authorization_disabled"}
    if _kill_switch_present():
        return {"ok": False, "minted": False, "reason": "code_evolution_kill_switch_present"}
    if unknown_paths:
        return {"ok": False, "minted": False, "reason": "unknown_production_paths"}
    body = _expected_body(
        scope=scope, current=current_release, ancestor=ancestor_release,
        evolution_paths=evolution_paths, changed_domains=changed_domains,
    )
    if _records(runtime, scope=scope, source=REVOCATION_SOURCE, key=body["authorization_key"]):
        return {"ok": False, "minted": False, "reason": "auto_authorization_revoked"}
    existing = _valid_record(runtime, scope=scope, body=body)
    if existing is not None:
        return {"ok": True, "minted": False, "record_id": existing.record_id, "reason": "already_authorized"}
    keys = receipt_key_set()
    if keys is None:
        return {"ok": False, "minted": False, "reason": "auto_authorization_signing_key_unavailable"}
    signed = {**body, "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "key_id": keys.active_id}
    signed["signature"] = _signature(keys.active_key, signed)
    record = RecordEnvelope.create(
        kind=KIND,
        title=f"Automatic code.evolution authorization {current_release.commit[:12]}",
        summary=(
            f"{AUTHORITY} authorized evolution-path changes from {ancestor_release.commit[:12]} "
            f"to {current_release.commit[:12]} under {POLICY_VERSION}."
        ),
        scope=scope,
        source=SOURCE,
        status="active",
        content=signed,
        meta={
            "report_type": "code_evolution_auto_authorization",
            "idempotency_key": _idempotency_key(SOURCE, body["authorization_key"]),
            "code_evolution_authorization_key": body["authorization_key"],
            "authority": AUTHORITY,
            "authorization_class": AUTHORIZATION_CLASS,
            "policy_version": POLICY_VERSION,
            "release_commit": current_release.commit,
            "deployment_receipt_id": current_release.receipt_id,
        },
        evidence=[current_release.receipt_id, ancestor_release.receipt_id],
    )
    stored = runtime.store.append(record)
    return {"ok": True, "minted": True, "record_id": stored.record_id, "reason": ""}


def _record_error(record: RecordEnvelope, *, body: dict[str, Any]) -> str:
    from eimemory.governance.tool_receipts import receipt_key_set

    content = dict(record.content) if isinstance(record.content, dict) else {}
    signature = str(content.pop("signature", "") or "")
    keys = receipt_key_set()
    key = keys.verification_keys.get(str(content.get("key_id") or ""), "") if keys else ""
    if not key:
        return "auto_authorization_signing_key_unavailable"
    if not signature or not hmac.compare_digest(signature, _signature(key, content)):
        return "auto_authorization_signature_invalid"
    if record.status != "active":
        return "auto_authorization_inactive"
    for field, expected in body.items():
        if content.get(field) != expected:
            return f"auto_authorization_{field}_mismatch"
    return ""


def _valid_record(runtime: Any, *, scope: ScopeRef, body: dict[str, Any]) -> RecordEnvelope | None:
    for record in _records(runtime, scope=scope, source=SOURCE, key=body["authorization_key"]):
        if not _record_error(record, body=body):
            return record
    return None


def code_evolution_auto_authorization_error(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity,
    ancestor_release: ReleaseIdentity,
    evolution_paths: list[str],
    changed_domains: list[str],
) -> tuple[str, str]:
    """Return ``(error, record_id)``; an empty error means the release is authorized."""

    if not auto_authorization_enabled():
        return "auto_authorization_disabled", ""
    body = _expected_body(
        scope=scope, current=current_release, ancestor=ancestor_release,
        evolution_paths=evolution_paths, changed_domains=changed_domains,
    )
    if _records(runtime, scope=scope, source=REVOCATION_SOURCE, key=body["authorization_key"]):
        return "auto_authorization_revoked", ""
    records = _records(runtime, scope=scope, source=SOURCE, key=body["authorization_key"])
    if not records:
        return "auto_authorization_missing", ""
    last_error = ""
    for record in records:
        last_error = _record_error(record, body=body)
        if not last_error:
            return "", record.record_id
    return last_error, ""


def revoke_code_evolution_auto_authorization(
    runtime: Any, *, scope: ScopeRef, record_id: str, reason: str, revoked_by: str,
) -> dict[str, Any]:
    """Withdraw an automatic authorization; lineage revalidation then fails closed."""

    record = runtime.store.get_by_id(record_id, scope=scope)
    if record is None or record.source != SOURCE or not isinstance(record.content, dict):
        return {"ok": False, "reason": "auto_authorization_not_found"}
    if not str(reason or "").strip() or not str(revoked_by or "").strip():
        return {"ok": False, "reason": "revocation_reason_and_actor_required"}
    key = str(record.content.get("authorization_key") or "")
    existing = _records(runtime, scope=scope, source=REVOCATION_SOURCE, key=key)
    if existing:
        return {"ok": True, "revocation_record_id": existing[0].record_id,
                "authorization_record_id": record_id, "already_revoked": True}
    revocation = RecordEnvelope.create(
        kind=KIND,
        title=f"Revoked automatic code.evolution authorization {record_id}",
        summary=str(reason)[:500],
        scope=scope,
        source=REVOCATION_SOURCE,
        status="active",
        content={"schema": SCHEMA + ".revocation", "authorization_record_id": record_id,
                 "authorization_key": key, "reason": str(reason)[:2000], "revoked_by": str(revoked_by)[:200],
                 "revoked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        meta={"report_type": "code_evolution_auto_authorization_revocation",
              "idempotency_key": _idempotency_key(REVOCATION_SOURCE, key),
              "code_evolution_authorization_key": key},
        evidence=[record_id],
    )
    stored = runtime.store.append(revocation)
    return {"ok": True, "revocation_record_id": stored.record_id, "authorization_record_id": record_id}
