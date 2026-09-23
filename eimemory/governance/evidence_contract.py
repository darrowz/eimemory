from __future__ import annotations

from dataclasses import dataclass, field
import os
import json
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping

from eimemory.governance.deployment_receipt import valid_deployment_rollback_evidence
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import package_import_root

from eimemory.contracts.release_identity import (  # ARCH-01 sunk
    ReleaseIdentity,
    release_authority_key,
    release_identity_payload,
    same_release_authority,
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
    if not same_scope(record.scope, expected_scope):
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
        if not same_release_authority(actual, release):
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




def current_release_identity(
    runtime: Any,
    scope: ScopeRef | Mapping[str, Any] | None,
    *,
    limit: int = 500,
) -> ReleaseIdentity | None:
    """Return the server-verified immutable release identity for this runtime."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope or {}))
    commit = _runtime_commit(runtime)
    if not commit:
        return None
    store = getattr(runtime, "store", None)
    latest_exact = getattr(
        store,
        "latest_record_by_meta_value_exact_scope",
        None,
    )
    if hasattr(store, "sqlite") and getattr(store, "sqlite") is None:
        latest_exact = None
    if callable(latest_exact):
        for meta_key, meta_value in (
            ("commit_sha", commit),
            ("report_type", "deployment_receipt"),
        ):
            record = latest_exact(
                kind="promotion_request",
                source="eimemory.deployment_receipt",
                status="deployed",
                scope=scope_ref,
                meta_key=meta_key,
                meta_value=meta_value,
            )
            identity = _verified_receipt_identity(record)
            if identity is not None and identity.commit == commit:
                return identity
    records = runtime.store.list_records(
        kinds=["promotion_request"],
        scope=scope_ref,
        limit=max(1, int(limit)),
    )
    for record in records:
        if not same_scope(getattr(record, "scope", None), scope_ref):
            continue
        identity = _verified_receipt_identity(record)
        if identity is None or identity.commit != commit:
            continue
        return identity
    for record in bound_deployment_receipts(runtime, scope_ref):
        identity = _verified_receipt_identity(record)
        if identity is not None and identity.commit == commit:
            return identity
    return None


def _scope_receipt_bindings(scope: ScopeRef) -> list[dict]:
    # Operator configuration, never request payload or a writable memory record.
    from eimemory.adapters.runtime.host_auth import _read_private_file

    path = os.environ.get("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "")
    if not path:
        return []
    try:
        bindings = json.loads(_read_private_file(Path(path), max_bytes=64 * 1024))
    except (ValueError, UnicodeError):
        return []
    if not isinstance(bindings, list):
        return []
    keys = {"tenant_id", "agent_id", "workspace_id", "user_id"}
    return [item for item in bindings if isinstance(item, dict)
            and isinstance(item.get("scope"), dict)
            and set(item["scope"]) == keys
            and item["scope"].get("tenant_id")
            and all(isinstance(value, str) for value in item["scope"].values())
            and same_scope(item["scope"], scope)]


def bound_deployment_receipts(runtime: Any, scope: ScopeRef) -> list[Any]:
    """Read only explicitly pinned receipt IDs; never search another scope."""
    return [record for binding in _scope_receipt_bindings(scope)
            if (record := deployment_receipt_for_scope(
                runtime, str(binding.get("receipt_id") or ""), scope)) is not None]


def deployment_receipt_for_scope(runtime: Any, receipt_id: str, scope: ScopeRef) -> Any:
    """Resolve receipt applicability, without granting access to its memory scope.

    Legacy exact-scope receipts retain their existing contract. Cross-scope reuse
    requires an operator-pinned record, the same tenant and canonical service.
    Callers still verify receipt authority and the required release commit.
    """
    record = runtime.store.get_by_id(receipt_id, scope=scope)
    if record is not None and same_scope(record.scope, scope):
        return record
    for binding in _scope_receipt_bindings(scope):
        if binding.get("receipt_id") != receipt_id:
            continue
        record = runtime.store.get_by_id(receipt_id)
        identity = _verified_receipt_identity(record)
        if identity is None or record.scope.tenant_id != scope.tenant_id:
            continue
        digest = sha256(json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode()).hexdigest()
        if binding.get("receipt_sha256") != digest:
            continue
        effect = record.content["side_effect"]
        health = effect["post_deploy_health"]
        from eimemory.governance.deployment_receipt import (
            DEFAULT_DEPLOYMENT_CURRENT_LINK,
            DEFAULT_DEPLOYMENT_HEALTH_URL,
            default_deployment_releases_root,
        )

        expected_release = f"{default_deployment_releases_root().rstrip('/')}/{identity.commit}"
        expected_link = str(DEFAULT_DEPLOYMENT_CURRENT_LINK)
        expected_health = str(DEFAULT_DEPLOYMENT_HEALTH_URL)
        if (effect["release"].get("release_path") != expected_release
                or effect["deployment"].get("current_link") != expected_link
                or health.get("current_link") != expected_link
                or health.get("url") != expected_health):
            raise ValueError(
                "deployment_receipt_path_mismatch:"
                f"release_path={effect['release'].get('release_path')!r} expected={expected_release!r}; "
                f"current_link={effect['deployment'].get('current_link')!r} expected={expected_link!r}; "
                f"health_url={health.get('url')!r} expected={expected_health!r}"
            )
        evolution = effect.get("code_evolution")
        if isinstance(evolution, Mapping) and evolution.get("strict") is True:
            from eimemory.governance.deployment_receipt import strict_code_evolution_receipt_error

            if strict_code_evolution_receipt_error(
                runtime, scope=record.scope, record=record, deployed_commit=identity.commit
            ):
                continue
        return record
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


def _commit_named_under_releases(path: Path, expected_releases: str) -> str:
    for release in (path, *path.parents):
        parent = str(release.parent).replace("\\", "/").rstrip("/").casefold()
        if parent == expected_releases and re.fullmatch(r"[0-9a-f]{40}", release.name):
            return release.name.lower()
    return ""


def _resolve_existing(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _import_root_uses_current_link(root: Path, link: Path, link_target: Path) -> bool:
    """True when this process is the deployment ``current`` install."""

    candidates = [root, *root.parents]
    resolved = _resolve_existing(root)
    if resolved is not None:
        candidates.extend((resolved, *resolved.parents))
    for candidate in candidates:
        if candidate == link or candidate == link_target:
            return True
        try:
            if candidate.is_relative_to(link_target):
                return True
        except (OSError, ValueError):
            continue
    return False


def located_runtime_commit(root: Path | None = None) -> tuple[str, bool]:
    """Return ``(commit, fail_closed)`` for this process's immutable release.

    A venv that imports through the ``current`` symlink does not have the
    release sha in its unresolved path. Follow that link when the import root
    is the current install. Disagreeing commits fail closed and must not fall
    through to a test override.
    """

    from eimemory.governance.deployment_receipt import (
        default_deployment_current_link,
        default_deployment_releases_root,
    )

    configured = str(os.environ.get("EIMEMORY_RUNTIME_COMMIT") or "").strip().lower()
    if root is None:
        root = package_import_root()
    expected_releases = default_deployment_releases_root().replace("\\", "/").rstrip("/").casefold()
    root_commit = _commit_named_under_releases(root, expected_releases)
    if not root_commit:
        resolved_root = _resolve_existing(root)
        if resolved_root is not None:
            root_commit = _commit_named_under_releases(resolved_root, expected_releases)
    link_commit = ""
    link = Path(str(default_deployment_current_link())).expanduser()
    link_target = _resolve_existing(link)
    if link_target is not None:
        candidate = _commit_named_under_releases(link_target, expected_releases)
        if candidate and _import_root_uses_current_link(root, link, link_target):
            link_commit = candidate
    if root_commit and link_commit and root_commit != link_commit:
        return "", True
    chosen = root_commit or link_commit
    if re.fullmatch(r"[0-9a-f]{40}", configured) and chosen and configured != chosen:
        return "", True
    return chosen, False


def _runtime_commit(runtime: Any) -> str:
    chosen, failed_closed = located_runtime_commit()
    if failed_closed:
        return ""
    if chosen:
        return chosen
    if os.environ.get("PYTEST_CURRENT_TEST"):
        test_commit = str(getattr(runtime, "_test_runtime_commit", "") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", test_commit):
            return test_commit
    return ""


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
        and release_path.endswith("/" + commit)
        and str(health.get("commit") or "").strip().lower() == commit
        and str(health.get("release_path") or "").replace("\\", "/").rstrip("/") == release_path
        and str(deployment.get("release_path") or "").replace("\\", "/").rstrip("/") == release_path
        and valid_deployment_rollback_evidence(dict(rollback))
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


def verified_deployment_receipt_identity(record: Any) -> ReleaseIdentity | None:
    """Validate one persisted deployment receipt without assuming it is current."""

    return _verified_receipt_identity(record)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def same_scope(left: ScopeRef | Mapping[str, Any] | None, right: ScopeRef | Mapping[str, Any] | None) -> bool:
    """Compare every scope dimension exactly; storage aliasing is never evidence authority."""

    if left is None or right is None:
        return False
    left_ref = left if isinstance(left, ScopeRef) else ScopeRef.from_dict(dict(left))
    right_ref = right if isinstance(right, ScopeRef) else ScopeRef.from_dict(dict(right))
    return (
        left_ref.tenant_id == right_ref.tenant_id
        and left_ref.agent_id == right_ref.agent_id
        and left_ref.workspace_id == right_ref.workspace_id
        and left_ref.user_id == right_ref.user_id
    )


def _rejected(record_id: str, reason: str) -> EvidenceResolution:
    return EvidenceResolution(ok=False, record_id=record_id, reason=reason, record=None)
