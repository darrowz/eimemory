from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    current_release_identity,
    release_identity_from_record,
    same_scope,
    verified_deployment_receipt_identity,
)
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


SCHEMA_VERSION = "release_lineage.v1"
SOURCE = "eimemory.release_lineage"
DOMAINS = (
    "memory.recall",
    "memory.governance",
    "channel.openclaw",
    "storage.integrity",
    "deployment.runtime",
)
DOMAIN_PATHS: dict[str, tuple[str, ...]] = {
    "memory.recall": (
        "eimemory/api/memory.py",
        "eimemory/embeddings",
        "eimemory/recall",
        "eimemory/retrieval",
        "eimemory/scoring",
        "eimemory/storage/runtime_store.py",
        "eimemory/storage/sqlite_store.py",
    ),
    "memory.governance": (
        "eimemory/evaluation",
        "eimemory/experience",
        "eimemory/governance",
    ),
    "channel.openclaw": (
        "eimemory/adapters/openclaw",
        "eimemory/adapters/runtime",
        "eimemory/ei_bridge",
        "eimemory/ops/openclaw_loop.py",
        "integrations/openclaw",
    ),
    "storage.integrity": (
        "deploy/storage_release_transaction.py",
        "deploy/systemd/eimemory-storage",
        "eimemory/storage",
    ),
    "deployment.runtime": (
        "deploy",
        "eimemory/governance/deployment_receipt.py",
        "eimemory/runtime_identity.py",
    ),
}
DOMAIN_GATE_SOURCES: dict[str, frozenset[str]] = {
    "memory.recall": frozenset(
        {
            "eimemory.evaluation.production_recall",
            "eimemory.evaluation.production_recall.bootstrap",
        }
    ),
    "memory.governance": frozenset(
        {
            "eimemory.capability_replay",
            "eimemory.l5_loop",
            "eimemory.l5_readiness",
            "eimemory.prompt_safety",
        }
    ),
    "channel.openclaw": frozenset({"eimemory.live_task_acceptance"}),
    "storage.integrity": frozenset({"eimemory.live_task_acceptance"}),
    "deployment.runtime": frozenset({"eimemory.deployment_receipt"}),
}
IGNORED_PATH_PREFIXES = ("docs/", "tests/", ".github/")
IGNORED_PATHS = {
    "CHANGELOG.md",
    "eimemory/version.py",
    "pyproject.toml",
}
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def record_release_lineage(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    repo_root: str | Path,
    current_release: ReleaseIdentity,
    gate_evidence: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    repo = Path(repo_root).expanduser().resolve()
    current_error = _current_release_error(runtime, scope_ref, current_release)
    if current_error:
        return current_error
    ancestor = _newest_verified_ancestor(
        runtime,
        scope=scope_ref,
        repo=repo,
        current_release=current_release,
    )
    if ancestor is None:
        return {"ok": False, "error": "verified_ancestor_receipt_not_found"}
    lineage = _compute_lineage(
        runtime,
        scope=scope_ref,
        repo=repo,
        current_release=current_release,
        ancestor_release=ancestor,
        gate_evidence=gate_evidence,
    )
    if not lineage.get("ok"):
        return lineage
    semantic_payload = json.dumps(
        {
            "current_receipt": current_release.receipt_id,
            "ancestor_receipt": ancestor.receipt_id,
            "gate_evidence": lineage["gate_evidence"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    record = append_learning_record_once(
        runtime,
        kind="l5_self_continuity",
        title=f"Release capability lineage {current_release.commit[:12]}",
        summary=(
            f"Verified capability lineage from {ancestor.commit[:12]} "
            f"to {current_release.commit[:12]}."
        ),
        scope=scope_ref,
        loop_id=f"release_lineage_{current_release.commit[:12]}",
        step_name="release_lineage",
        semantic_key=stable_semantic_key("release_lineage", semantic_payload),
        authority_tier="L0",
        status="active",
        content={"report_type": "release_lineage", "lineage": lineage},
        meta={
            "report_type": "release_lineage",
            "schema_version": SCHEMA_VERSION,
            "current_commit": current_release.commit,
            "ancestor_commit": ancestor.commit,
            "compatible": bool(lineage["compatible"]),
        },
        evidence=[
            current_release.receipt_id,
            ancestor.receipt_id,
            *[
                reference
                for references in lineage["gate_evidence"].values()
                for reference in references
            ],
        ],
        source=SOURCE,
    )
    return _public_report(lineage, record_id=record.record_id)


def current_release_lineage(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    current_release: ReleaseIdentity,
    repo_root: str | Path = "/dev-project/eimemory",
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    repo = Path(repo_root).expanduser().resolve()
    current_error = _current_release_error(runtime, scope_ref, current_release)
    if current_error:
        return current_error
    mismatch_seen = False
    records = runtime.store.list_records(
        kinds=["l5_self_continuity"],
        scope=scope_ref,
        limit=500,
    )
    for record in records:
        if (
            record.source != SOURCE
            or record.status != "active"
            or not same_scope(record.scope, scope_ref)
            or str(record.meta.get("report_type") or "") != "release_lineage"
        ):
            continue
        stored = record.content.get("lineage") if isinstance(record.content, dict) else None
        if not isinstance(stored, dict):
            mismatch_seen = True
            continue
        stored_current = _identity_from_payload(stored.get("current_release"))
        if stored_current != current_release:
            continue
        stored_ancestor = _identity_from_payload(stored.get("ancestor_release"))
        if stored_ancestor is None:
            mismatch_seen = True
            continue
        selected_ancestor = _newest_verified_ancestor(
            runtime,
            scope=scope_ref,
            repo=repo,
            current_release=current_release,
        )
        if selected_ancestor != stored_ancestor:
            mismatch_seen = True
            continue
        recomputed = _compute_lineage(
            runtime,
            scope=scope_ref,
            repo=repo,
            current_release=current_release,
            ancestor_release=selected_ancestor,
            gate_evidence=stored.get("gate_evidence"),
        )
        if recomputed != stored:
            mismatch_seen = True
            continue
        return _public_report(recomputed, record_id=record.record_id)
    if mismatch_seen:
        return {"ok": False, "error": "lineage_attestation_mismatch"}
    return {"ok": False, "error": "current_release_lineage_not_found"}


def evidence_release_for_domain(
    lineage: dict[str, Any],
    domain: str,
    current_release: ReleaseIdentity,
) -> ReleaseIdentity:
    if (
        not isinstance(lineage, dict)
        or lineage.get("ok") is not True
        or lineage.get("validated") is not True
        or lineage.get("schema_version") != SCHEMA_VERSION
        or _identity_from_payload(lineage.get("current_release")) != current_release
    ):
        raise ValueError("lineage is not a validated current-release attestation")
    domain_name = str(domain or "")
    state = lineage.get("domains", {}).get(domain_name)
    if not isinstance(state, dict) or state.get("mode") not in {"inherited", "current"}:
        raise ValueError(f"domain is not inheritable: {domain_name}")
    identity = _identity_from_payload(state.get("evidence_release"))
    expected = (
        _identity_from_payload(lineage.get("ancestor_release"))
        if state["mode"] == "inherited"
        else current_release
    )
    if identity is None or identity != expected:
        raise ValueError(f"domain evidence release is invalid: {domain_name}")
    return identity


def _compute_lineage(
    runtime: Any,
    *,
    scope: ScopeRef,
    repo: Path,
    current_release: ReleaseIdentity,
    ancestor_release: ReleaseIdentity,
    gate_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if _receipt_identity(runtime, scope, ancestor_release) is None:
        return {"ok": False, "error": "ancestor_release_receipt_invalid"}
    if not _is_ancestor(repo, ancestor_release.commit, current_release.commit):
        return {"ok": False, "error": "ancestor_release_not_ancestor"}
    changed_paths = _changed_paths(repo, ancestor_release.commit, current_release.commit)
    if changed_paths is None:
        return {"ok": False, "error": "release_diff_unavailable"}
    classified = {path: _domains_for_path(path) for path in changed_paths}
    unknown = sorted(
        path
        for path, domains in classified.items()
        if not domains and not _ignored_path(path)
    )
    normalized_gates = _normalized_gate_evidence(gate_evidence)
    domains: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        ancestor_digest = _domain_digest(repo, ancestor_release.commit, DOMAIN_PATHS[domain])
        current_digest = _domain_digest(repo, current_release.commit, DOMAIN_PATHS[domain])
        if ancestor_digest is None or current_digest is None:
            return {"ok": False, "error": "domain_digest_unavailable", "domain": domain}
        domain_changed_paths = sorted(
            path for path, affected in classified.items() if domain in affected
        )
        changed = bool(unknown or ancestor_digest != current_digest)
        references = normalized_gates[domain]
        gate_errors = (
            _gate_errors(
                runtime,
                scope=scope,
                domain=domain,
                current_release=current_release,
                references=references,
            )
            if changed
            else {}
        )
        if not changed:
            mode = "inherited"
            evidence_release = ancestor_release
        elif references and not gate_errors:
            mode = "current"
            evidence_release = current_release
        else:
            mode = "changed_unverified"
            evidence_release = None
        domains[domain] = {
            "mode": mode,
            "changed": changed,
            "paths": list(DOMAIN_PATHS[domain]),
            "changed_paths": domain_changed_paths,
            "ancestor_digest": ancestor_digest,
            "current_digest": current_digest,
            "gate_evidence": references,
            "gate_errors": gate_errors,
            "evidence_release": (
                _identity_payload(evidence_release) if evidence_release is not None else {}
            ),
        }
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "scope": asdict(scope),
        "current_release": _identity_payload(current_release),
        "ancestor_release": _identity_payload(ancestor_release),
        "ancestry": {
            "is_ancestor": True,
            "ancestor_commit": ancestor_release.commit,
            "current_commit": current_release.commit,
        },
        "changed_paths": changed_paths,
        "unknown_production_paths": unknown,
        "gate_evidence": normalized_gates,
        "domains": domains,
        "compatible": all(
            state["mode"] != "changed_unverified" for state in domains.values()
        ),
    }


def _current_release_error(
    runtime: Any,
    scope: ScopeRef,
    release: ReleaseIdentity,
) -> dict[str, Any]:
    if (
        not isinstance(release, ReleaseIdentity)
        or not release.complete
        or not COMMIT_RE.fullmatch(release.commit)
        or _receipt_identity(runtime, scope, release) is None
        or current_release_identity(runtime, scope) != release
    ):
        return {"ok": False, "error": "current_release_receipt_invalid"}
    return {}


def _receipt_identity(
    runtime: Any,
    scope: ScopeRef,
    expected: ReleaseIdentity,
) -> ReleaseIdentity | None:
    record = runtime.store.get_by_id(expected.receipt_id, scope=scope)
    if record is None or not same_scope(record.scope, scope):
        return None
    actual = verified_deployment_receipt_identity(record)
    return actual if actual == expected else None


def _newest_verified_ancestor(
    runtime: Any,
    *,
    scope: ScopeRef,
    repo: Path,
    current_release: ReleaseIdentity,
) -> ReleaseIdentity | None:
    candidates: list[tuple[int, ReleaseIdentity]] = []
    records = runtime.store.list_records(
        kinds=["promotion_request"],
        scope=scope,
        limit=1000,
    )
    for record in records:
        if not same_scope(record.scope, scope):
            continue
        identity = verified_deployment_receipt_identity(record)
        if identity is None or identity == current_release:
            continue
        if not _is_ancestor(repo, identity.commit, current_release.commit):
            continue
        distance = _git_text(
            repo,
            "rev-list",
            "--count",
            f"{identity.commit}..{current_release.commit}",
        )
        try:
            candidates.append((int(distance), identity))
        except (TypeError, ValueError):
            continue
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _domain_digest(repo: Path, commit: str, paths: tuple[str, ...]) -> str | None:
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", commit, "--", *paths)
    if raw is None:
        return None
    rows: list[tuple[bytes, bytes, bytes]] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, path = entry.split(b"\t", 1)
            mode, _object_type, object_id = metadata.split(b" ", 2)
        except ValueError:
            return None
        rows.append((path, mode, object_id))
    digest = sha256()
    for path, mode, object_id in sorted(rows):
        digest.update(mode + b"\0" + object_id + b"\0" + path + b"\0")
    return digest.hexdigest()


def _changed_paths(repo: Path, ancestor: str, current: str) -> list[str] | None:
    raw = _git_bytes(repo, "diff", "--name-only", "-z", f"{ancestor}..{current}")
    if raw is None:
        return None
    return sorted(
        path.decode("utf-8", errors="surrogateescape")
        for path in raw.split(b"\0")
        if path
    )


def _domains_for_path(path: str) -> set[str]:
    return {
        domain
        for domain, rules in DOMAIN_PATHS.items()
        if any(path == rule or path.startswith(rule.rstrip("/") + "/") for rule in rules)
    }


def _ignored_path(path: str) -> bool:
    return (
        path in IGNORED_PATHS
        or path.startswith(IGNORED_PATH_PREFIXES)
        or path.startswith("README")
        or path.startswith("CHANGELOG")
    )


def _normalized_gate_evidence(value: Mapping[str, Any] | None) -> dict[str, list[str]]:
    supplied = value if isinstance(value, Mapping) else {}
    return {
        domain: list(
            dict.fromkeys(
                str(reference or "").strip()
                for reference in (
                    supplied.get(domain)
                    if isinstance(supplied.get(domain), (list, tuple))
                    else []
                )
                if str(reference or "").strip()
            )
        )
        for domain in DOMAINS
    }


def _gate_errors(
    runtime: Any,
    *,
    scope: ScopeRef,
    domain: str,
    current_release: ReleaseIdentity,
    references: list[str],
) -> dict[str, str]:
    errors: dict[str, str] = {}
    for reference in references:
        record = runtime.store.get_by_id(reference, scope=scope)
        if record is None:
            errors[reference] = "record_not_found"
            continue
        if not same_scope(record.scope, scope):
            errors[reference] = "scope_mismatch"
            continue
        if record.source not in DOMAIN_GATE_SOURCES[domain]:
            errors[reference] = "source_not_authorized_for_domain"
            continue
        if record.source == "eimemory.deployment_receipt":
            valid_release = verified_deployment_receipt_identity(record)
        else:
            valid_release = release_identity_from_record(record)
        if valid_release != current_release:
            errors[reference] = "release_mismatch"
            continue
        if record.source != "eimemory.deployment_receipt" and not _gate_passed(record):
            errors[reference] = "gate_not_passed"
    return errors


def _gate_passed(record: Any) -> bool:
    content = record.content if isinstance(getattr(record, "content", None), dict) else {}
    meta = record.meta if isinstance(getattr(record, "meta", None), dict) else {}
    return bool(
        content.get("passed") is True
        or content.get("ok") is True
        or content.get("accepted") is True
        or str(content.get("verdict") or meta.get("verdict") or "").lower() in {"pass", "passed"}
    )


def _is_ancestor(repo: Path, ancestor: str, current: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, current],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _git_text(repo: Path, *args: str) -> str:
    raw = _git_bytes(repo, *args)
    return "" if raw is None else raw.decode("utf-8", errors="replace").strip()


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _scope_ref(scope: ScopeRef | dict | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _identity_payload(release: ReleaseIdentity) -> dict[str, str]:
    return {
        "commit": release.commit,
        "version": release.version,
        "receipt_id": release.receipt_id,
        "session_id": release.session_id,
    }


def _identity_from_payload(value: Any) -> ReleaseIdentity | None:
    payload = value if isinstance(value, Mapping) else {}
    identity = ReleaseIdentity(
        commit=str(payload.get("commit") or "").strip().lower(),
        version=str(payload.get("version") or "").strip(),
        receipt_id=str(payload.get("receipt_id") or "").strip(),
        session_id=str(payload.get("session_id") or "").strip(),
    )
    return identity if identity.complete and COMMIT_RE.fullmatch(identity.commit) else None


def _public_report(lineage: dict[str, Any], *, record_id: str) -> dict[str, Any]:
    return {
        **lineage,
        "record_id": record_id,
        "validated": True,
    }
