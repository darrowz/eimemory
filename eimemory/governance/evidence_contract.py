from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Mapping

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import package_import_root
from eimemory.version import __version__


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    commit: str
    version: str
    receipt_id: str
    session_id: str

    @property
    def complete(self) -> bool:
        return bool(
            len(self.commit) == 40
            and self.version
            and self.receipt_id
            and self.session_id
        )


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    kinds: frozenset[str] = field(default_factory=frozenset)
    sources: frozenset[str] = field(default_factory=frozenset)
    statuses: frozenset[str] = field(default_factory=frozenset)
    evidence_classes: frozenset[str] = field(default_factory=frozenset)
    release_bound: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    ok: bool
    record_id: str
    reason: str
    record: RecordEnvelope | None


def resolve_evidence(
    runtime: Any,
    reference: str,
    requirement: EvidenceRequirement,
    scope: ScopeRef | Mapping[str, Any] | None,
    release: ReleaseIdentity,
) -> EvidenceResolution:
    """Resolve a persisted record against one exact scope, type, and release contract."""

    record_id = str(reference or "").strip()
    if not record_id:
        return _rejected(record_id, "empty_reference")
    record = runtime.store.get_by_id(record_id)
    if record is None:
        return _rejected(record_id, "record_not_found")
    expected_scope = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope or {}))
    if not _same_scope(record.scope, expected_scope):
        return _rejected(record_id, "scope_mismatch")
    if requirement.kinds and str(record.kind or "") not in requirement.kinds:
        return _rejected(record_id, "kind_mismatch")
    if requirement.sources and str(record.source or "") not in requirement.sources:
        return _rejected(record_id, "source_mismatch")
    if requirement.statuses and str(record.status or "") not in requirement.statuses:
        return _rejected(record_id, "status_mismatch")
    evidence_class = _first_text(
        _payload_value(record, "evidence_class"),
        _payload_value(record, "class"),
    )
    if requirement.evidence_classes and evidence_class not in requirement.evidence_classes:
        return _rejected(record_id, "evidence_class_mismatch")
    if requirement.release_bound:
        actual = release_identity_from_record(record)
        if not release.complete or actual != release:
            return _rejected(record_id, "release_mismatch")
    return EvidenceResolution(ok=True, record_id=record_id, reason="ok", record=record)


def release_identity_from_record(record: Any) -> ReleaseIdentity:
    return ReleaseIdentity(
        commit=_first_text(
            _payload_value(record, "release_commit"),
            _payload_value(record, "deployment_commit"),
            _payload_value(record, "commit_sha"),
        ).lower(),
        version=_first_text(
            _payload_value(record, "release_version"),
            _payload_value(record, "deployment_version"),
            _payload_value(record, "version"),
        ),
        receipt_id=_first_text(
            _payload_value(record, "deployment_receipt_id"),
            _payload_value(record, "promotion_request_id"),
            _payload_value(record, "receipt_id"),
        ),
        session_id=_first_text(
            _payload_value(record, "release_session_id"),
            _payload_value(record, "closure_session_id"),
            _payload_value(record, "deployment_session_id"),
        ),
    )


def release_identity_payload(release: ReleaseIdentity) -> dict[str, str]:
    return {
        "release_commit": release.commit,
        "release_version": release.version,
        "deployment_receipt_id": release.receipt_id,
        "release_session_id": release.session_id,
    }


def current_release_identity(
    runtime: Any,
    scope: ScopeRef | Mapping[str, Any] | None,
    *,
    limit: int = 500,
) -> ReleaseIdentity | None:
    """Return the server-verified immutable release identity for this runtime."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope or {}))
    commit, production_runtime, test_override = _runtime_commit(runtime)
    if not commit:
        return None
    records = runtime.store.list_records(
        kinds=["promotion_request"],
        scope=scope_ref,
        limit=max(1, int(limit)),
    )
    for record in records:
        identity = _verified_receipt_identity(record)
        if identity is None or identity.commit != commit:
            continue
        if production_runtime and not test_override and identity.version != __version__:
            continue
        return identity
    return None


def _payload_value(record: Any, key: str) -> Any:
    if isinstance(record, Mapping):
        payloads = (
            record.get("meta"),
            record.get("content"),
            record.get("provenance"),
            record,
        )
    else:
        payloads = (
            getattr(record, "meta", None),
            getattr(record, "content", None),
            getattr(record, "provenance", None),
        )
    for payload in payloads:
        if isinstance(payload, Mapping) and key in payload:
            return payload.get(key)
        nested = payload.get("payload") if isinstance(payload, Mapping) else None
        if isinstance(nested, Mapping) and key in nested:
            return nested.get(key)
    return None


def _runtime_commit(runtime: Any) -> tuple[str, bool, bool]:
    configured = str(os.environ.get("EIMEMORY_RUNTIME_COMMIT") or "").strip().lower()
    root = package_import_root()
    root_commit = ""
    for release in (root, *root.parents):
        if (
            str(release.parent).replace("\\", "/").rstrip("/").casefold() == "/opt/eimemory/releases"
            and re.fullmatch(r"[0-9a-f]{40}", release.name)
        ):
            root_commit = release.name.lower()
            break
    try:
        production_runtime = root.is_relative_to(Path("/opt/eimemory"))
    except (OSError, ValueError):
        production_runtime = False
    if re.fullmatch(r"[0-9a-f]{40}", configured) and root_commit and configured != root_commit:
        return "", True, False
    if root_commit:
        return root_commit, True, False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        test_commit = str(getattr(runtime, "_test_runtime_commit", "") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", test_commit):
            return test_commit, False, True
    return "", production_runtime, False


def _verified_receipt_identity(record: Any) -> ReleaseIdentity | None:
    if (
        str(getattr(record, "kind", "") or "") != "promotion_request"
        or str(getattr(record, "source", "") or "") != "eimemory.deployment_receipt"
        or str(getattr(record, "status", "") or "") != "deployed"
    ):
        return None
    content = getattr(record, "content", None) if not isinstance(record, Mapping) else record.get("content")
    content = content if isinstance(content, Mapping) else {}
    gate = content.get("gate") if isinstance(content.get("gate"), Mapping) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), Mapping) else {}
    verification = side_effect.get("verification") if isinstance(side_effect.get("verification"), Mapping) else {}
    deployment = side_effect.get("deployment") if isinstance(side_effect.get("deployment"), Mapping) else {}
    health = side_effect.get("post_deploy_health") if isinstance(side_effect.get("post_deploy_health"), Mapping) else {}
    commit_payload = side_effect.get("commit") if isinstance(side_effect.get("commit"), Mapping) else {}
    release = side_effect.get("release") if isinstance(side_effect.get("release"), Mapping) else {}
    rollback = side_effect.get("rollback_evidence") if isinstance(side_effect.get("rollback_evidence"), Mapping) else {}
    commit = str(commit_payload.get("commit_sha") or "").strip().lower()
    version = str(release.get("version") or "").strip()
    release_path = str(release.get("release_path") or "").replace("\\", "/").rstrip("/")
    if not (
        str(content.get("report_type") or "") == "deployment_receipt"
        and content.get("promotion_target") == "code_patch"
        and content.get("action") == "code_patch"
        and gate.get("ok") is True
        and gate.get("receipt_verified") is True
        and side_effect.get("ok") is True
        and side_effect.get("production_applied") is True
        and side_effect.get("deployment_executed") is True
        and verification.get("ok") is True
        and verification.get("skipped") is not True
        and deployment.get("ok") is True
        and deployment.get("skipped") is not True
        and health.get("ok") is True
        and health.get("skipped") is not True
        and re.fullmatch(r"[0-9a-f]{40}", commit)
        and version
        and release_path.endswith("/" + commit)
        and str(health.get("commit") or "").strip().lower() == commit
        and str(health.get("version") or "").strip() == version
        and str(health.get("release_path") or "").replace("\\", "/").rstrip("/") == release_path
        and str(deployment.get("release_path") or "").replace("\\", "/").rstrip("/") == release_path
        and str(rollback.get("prior_commit_sha") or "").strip()
        and str(rollback.get("rollback_command") or "").strip()
    ):
        return None
    record_id = str(getattr(record, "record_id", "") or "")
    session_id = _first_text(
        _payload_value(record, "release_session_id"),
        _payload_value(record, "closure_session_id"),
        _payload_value(record, "deployment_session_id"),
        record_id,
    )
    identity = ReleaseIdentity(
        commit=commit,
        version=version,
        receipt_id=record_id,
        session_id=session_id,
    )
    return identity if identity.complete else None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _same_scope(left: ScopeRef, right: ScopeRef) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.agent_id == right.agent_id
        and left.workspace_id == right.workspace_id
        and left.user_id == right.user_id
    )


def _rejected(record_id: str, reason: str) -> EvidenceResolution:
    return EvidenceResolution(ok=False, record_id=record_id, reason=reason, record=None)
