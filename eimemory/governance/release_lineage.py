from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any, Mapping

from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    current_release_identity,
    same_release_authority,
    same_scope,
    verified_deployment_receipt_identity,
)
from eimemory.governance.capability_replay_packs import CORE_REPLAY_CAPABILITIES
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
        "eimemory/api/runtime.py",
        "eimemory/evaluation",
        "eimemory/experience",
        "eimemory/governance",
    ),
    "channel.openclaw": (
        "deploy/openclaw",
        "deploy/ensure_openclaw",
        "deploy/install_immutable_release.sh",
        "deploy/patch_openclaw",
        "deploy/systemd/openclaw-",
        "deploy/verify_openclaw",
        "eimemory/adapters/openclaw",
        "eimemory/adapters/runtime",
        "eimemory/ei_bridge",
        "eimemory/ops/openclaw_feishu_reply_watchdog.py",
        "eimemory/ops/openclaw_loop.py",
        "integrations/openclaw",
    ),
    "storage.integrity": (
        "deploy/migrate_storage_release.py",
        "deploy/install_immutable_release.sh",
        "deploy/storage",
        "deploy/storage_release_transaction.py",
        "deploy/systemd/eimemory-storage",
        "deploy/verify_storage_release.py",
        "eimemory/storage",
    ),
    "deployment.runtime": (
        "deploy/capture_prior_health",
        "deploy/ensure_evidence_receipt",
        "deploy/install_immutable_release.sh",
        "deploy/record_deployment_receipt.py",
        "deploy/record_release_lineage.py",
        "deploy/systemd/eimemory-",
        "deploy/verify_release_health.py",
        "eimemory/adapters/eibrain/rpc_server.py",
        "eimemory/governance/deployment_receipt.py",
        "eimemory/runtime_identity.py",
    ),
}
IGNORED_PATH_PREFIXES = ("docs/", "tests/", ".github/")
IGNORED_PATHS = {
    "CHANGELOG.md",
    "deploy/systemd/README.md",
    "scripts/test_openclaw_loop.py",
}
INTEGRATION_VERSION_PATHS = {
    "integrations/codex/eimemory/.codex-plugin/plugin.json",
    "integrations/hermes/eimemory/plugin.yaml",
}
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
RECEIPT_PAGE_SIZE = 200
WEAK_REPLAY_CAPABILITIES = frozenset(
    {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
)


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
        evidence=_lineage_evidence_references(lineage),
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
    selected_ancestor: ReleaseIdentity | None = None
    selected_ancestor_loaded = False
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
        if not same_release_authority(stored_current, current_release):
            continue
        if not selected_ancestor_loaded:
            selected_ancestor = _newest_verified_ancestor(
                runtime,
                scope=scope_ref,
                repo=repo,
                current_release=current_release,
            )
            selected_ancestor_loaded = True
        stored_ancestor = _identity_from_payload(stored.get("ancestor_release"))
        if stored_ancestor is None:
            mismatch_seen = True
            continue
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
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    repo_root: str | Path,
    domain: str,
    current_release: ReleaseIdentity,
    expected_record_id: str = "",
) -> ReleaseIdentity:
    lineage = current_release_lineage(
        runtime,
        scope=scope,
        repo_root=repo_root,
        current_release=current_release,
    )
    if (
        not isinstance(lineage, dict)
        or lineage.get("ok") is not True
        or lineage.get("validated") is not True
        or lineage.get("schema_version") != SCHEMA_VERSION
        or not same_release_authority(
            _identity_from_payload(lineage.get("current_release")),
            current_release,
        )
    ):
        raise ValueError("lineage is not a validated current-release attestation")
    expected_id = str(expected_record_id or "").strip()
    if expected_id and str(lineage.get("record_id") or "") != expected_id:
        raise ValueError("lineage record mismatch")
    domain_name = str(domain or "")
    state = lineage.get("domains", {}).get(domain_name)
    if not isinstance(state, dict) or state.get("mode") not in {"inherited", "current"}:
        raise ValueError(f"domain is not inheritable: {domain_name}")
    identity = _identity_from_payload(state.get("evidence_release"))
    if identity is None:
        raise ValueError(f"domain evidence release is invalid: {domain_name}")
    if state["mode"] == "current":
        if not same_release_authority(identity, current_release):
            raise ValueError(f"domain evidence release is invalid: {domain_name}")
    else:
        scope_ref = _scope_ref(scope)
        repo = Path(repo_root).expanduser().resolve()
        distances = _ancestor_distances(repo, current_release.commit)
        if (
            _receipt_identity(runtime, scope_ref, identity) is None
            or distances is None
            or int(distances.get(identity.commit, 0)) <= 0
        ):
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
    change = _release_change_summary(
        repo,
        ancestor=ancestor_release.commit,
        current=current_release.commit,
    )
    if change is None:
        return {"ok": False, "error": "release_diff_unavailable"}
    changed_paths, classified, unknown = change
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
        changed = bool(
            unknown
            or domain_changed_paths
            or ancestor_digest != current_digest
        )
        current_references = normalized_gates[domain]
        current_gate_errors = (
            _gate_errors(
                runtime,
                scope=scope,
                domain=domain,
                current_release=current_release,
                references=current_references,
                domain_changed=changed,
            )
            if current_references
            else {}
        )
        if current_references and not current_gate_errors:
            mode = "current"
            evidence_release = current_release
            evidence_references = current_references
        elif not changed:
            inherited = _nearest_verified_domain_evidence(
                runtime,
                scope=scope,
                repo=repo,
                domain=domain,
                current_release=current_release,
            )
            if inherited is not None:
                mode = "inherited"
                evidence_release, evidence_references = inherited
            else:
                mode = "changed_unverified"
                evidence_release = None
                evidence_references = current_references
        else:
            mode = "changed_unverified"
            evidence_release = None
            evidence_references = current_references
        domains[domain] = {
            "mode": mode,
            "changed": changed,
            "paths": list(DOMAIN_PATHS[domain]),
            "changed_paths": domain_changed_paths,
            "ancestor_digest": ancestor_digest,
            "current_digest": current_digest,
            "gate_evidence": evidence_references,
            "current_gate_evidence": current_references,
            "gate_errors": current_gate_errors,
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
        or not same_release_authority(
            current_release_identity(runtime, scope),
            release,
        )
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
    return actual if same_release_authority(actual, expected) else None


def _nearest_verified_domain_evidence(
    runtime: Any,
    *,
    scope: ScopeRef,
    repo: Path,
    domain: str,
    current_release: ReleaseIdentity,
) -> tuple[ReleaseIdentity, list[str]] | None:
    current_receipt = runtime.store.get_by_id(current_release.receipt_id, scope=scope)
    current_rowid = (
        _record_rowid(runtime, record=current_receipt, scope=scope)
        if current_receipt is not None
        else None
    )
    distances = _ancestor_distances(repo, current_release.commit)
    if current_receipt is None or current_rowid is None or distances is None:
        return None
    candidates: list[tuple[int, str, ReleaseIdentity, list[str]]] = []
    records = _release_lineage_records(
        runtime,
        scope=scope,
        before_rowid=current_rowid,
    )
    for record in records:
        if (
            not same_scope(record.scope, scope)
            or str(record.meta.get("report_type") or "") != "release_lineage"
        ):
            continue
        stored = record.content.get("lineage") if isinstance(record.content, dict) else None
        if (
            not isinstance(stored, dict)
            or stored.get("schema_version") != SCHEMA_VERSION
            or stored.get("source") != SOURCE
            or stored.get("scope") != asdict(scope)
        ):
            continue
        candidate = _identity_from_payload(stored.get("current_release"))
        state = (
            stored.get("domains", {}).get(domain)
            if isinstance(stored.get("domains"), dict)
            else None
        )
        if (
            candidate is None
            or same_release_authority(candidate, current_release)
            or not isinstance(state, dict)
            or state.get("mode") != "current"
            or not same_release_authority(
                _identity_from_payload(state.get("evidence_release")),
                candidate,
            )
            or _receipt_identity(runtime, scope, candidate) is None
            or int(distances.get(candidate.commit, 0)) <= 0
        ):
            continue
        candidate_receipt = runtime.store.get_by_id(candidate.receipt_id, scope=scope)
        if (
            candidate_receipt is None
            or not _record_precedes(
                runtime,
                earlier=candidate_receipt,
                later=current_receipt,
                scope=scope,
            )
        ):
            continue
        references = _normalized_gate_evidence(stored.get("gate_evidence"))[domain]
        if (
            not references
            or state.get("gate_evidence") != references
            or (
                isinstance(state.get("current_gate_evidence"), list)
                and state.get("current_gate_evidence") != references
            )
            or _gate_errors(
                runtime,
                scope=scope,
                domain=domain,
                current_release=candidate,
                references=references,
            )
        ):
            continue
        evidence_records = [
            runtime.store.get_by_id(reference, scope=scope) for reference in references
        ]
        if any(
            evidence_record is None
            or not _record_precedes(
                runtime,
                earlier=evidence_record,
                later=record,
                scope=scope,
            )
            for evidence_record in evidence_records
        ):
            continue
        continuity = _domain_change_summary(
            repo,
            domain=domain,
            ancestor=candidate.commit,
            current=current_release.commit,
        )
        if continuity is None or continuity["changed"]:
            continue
        candidates.append(
            (
                int(distances[candidate.commit]),
                candidate.receipt_id,
                candidate,
                references,
            )
        )
    if not candidates:
        return None
    _distance, _receipt_id, release, references = min(candidates)
    return release, references


def _release_lineage_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    before_rowid: int,
) -> Iterator[Any]:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        return
    cursor = before_rowid
    while True:
        rows = conn.execute(
            """
            SELECT rowid, record_id, source_id
            FROM records
            WHERE kind = ?
              AND source = ?
              AND status = ?
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
              AND rowid < ?
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (
                "l5_self_continuity",
                SOURCE,
                "active",
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                cursor,
                RECEIPT_PAGE_SIZE,
            ),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            record = runtime.store.get_by_exact_ref(
                str(row["record_id"]),
                scope=scope,
                source_id=str(row["source_id"]),
            )
            if record is not None:
                yield record
        next_cursor = min(int(row["rowid"]) for row in rows)
        if next_cursor >= cursor:
            break
        cursor = next_cursor


def _newest_verified_ancestor(
    runtime: Any,
    *,
    scope: ScopeRef,
    repo: Path,
    current_release: ReleaseIdentity,
) -> ReleaseIdentity | None:
    current_record = runtime.store.get_by_id(current_release.receipt_id, scope=scope)
    if current_record is None:
        return None
    current_rowid = _record_rowid(runtime, record=current_record, scope=scope)
    if current_rowid is None:
        return None
    verified: list[tuple[int, ReleaseIdentity]] = []
    for sequence, record in enumerate(
        _deployment_receipt_records(
            runtime,
            scope=scope,
            before_rowid=current_rowid,
        )
    ):
        identity = verified_deployment_receipt_identity(record)
        if (
            identity is None
            or same_release_authority(identity, current_release)
        ):
            continue
        verified.append((sequence, identity))

    if not verified:
        return None
    distances = _ancestor_distances(repo, current_release.commit)
    if distances is None:
        return None
    candidates = [
        (
            distances[identity.commit],
            sequence,
            identity.commit,
            identity.receipt_id,
            identity,
        )
        for sequence, identity in verified
        if identity.commit in distances and distances[identity.commit] > 0
    ]
    return min(candidates)[4] if candidates else None


def _deployment_receipt_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    before_rowid: int | None,
) -> Iterator[Any]:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None or before_rowid is None:
        return
    cursor = before_rowid
    while True:
        rows = conn.execute(
            """
            SELECT rowid, record_id, source_id
            FROM records
            WHERE kind = ?
              AND source = ?
              AND status = ?
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
              AND rowid < ?
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (
                "promotion_request",
                "eimemory.deployment_receipt",
                "deployed",
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                cursor,
                RECEIPT_PAGE_SIZE,
            ),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            record = runtime.store.get_by_exact_ref(
                str(row["record_id"]),
                scope=scope,
                source_id=str(row["source_id"]),
            )
            if record is not None:
                yield record
        next_cursor = min(int(row["rowid"]) for row in rows)
        if next_cursor >= cursor:
            break
        cursor = next_cursor


def _ancestor_distances(repo: Path, current: str) -> dict[str, int] | None:
    raw = _git_bytes(repo, "rev-list", "--parents", current)
    if raw is None:
        return None
    parents: dict[str, tuple[str, ...]] = {}
    for raw_line in raw.decode("ascii", errors="strict").splitlines():
        values = tuple(raw_line.strip().lower().split())
        if not values or not all(COMMIT_RE.fullmatch(value) for value in values):
            return None
        parents[values[0]] = values[1:]
    if current not in parents:
        return None
    distances = {current: 0}
    queue = deque([current])
    while queue:
        commit = queue.popleft()
        next_distance = distances[commit] + 1
        for parent in parents.get(commit, ()):
            if parent not in distances or next_distance < distances[parent]:
                distances[parent] = next_distance
                queue.append(parent)
    return distances


def _record_precedes(runtime: Any, *, earlier: Any, later: Any, scope: ScopeRef) -> bool:
    earlier_rowid = _record_rowid(runtime, record=earlier, scope=scope)
    later_rowid = _record_rowid(runtime, record=later, scope=scope)
    if earlier_rowid is not None and later_rowid is not None:
        return earlier_rowid < later_rowid
    earlier_time = _strict_record_time(earlier)
    later_time = _strict_record_time(later)
    return bool(
        earlier_time is not None
        and later_time is not None
        and earlier_time < later_time
    )


def _record_rowid(runtime: Any, *, record: Any, scope: ScopeRef) -> int | None:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        return None
    row = conn.execute(
        """
        SELECT rowid
        FROM records
        WHERE record_id = ?
          AND source = ?
          AND tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (
            str(getattr(record, "record_id", "") or ""),
            str(getattr(record, "source", "") or ""),
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ),
    ).fetchone()
    return None if row is None else int(row["rowid"])


def _strict_record_time(record: Any) -> datetime | None:
    raw = str(getattr(getattr(record, "time", None), "created_at", "") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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


def _release_change_summary(
    repo: Path,
    *,
    ancestor: str,
    current: str,
) -> tuple[list[str], dict[str, set[str]], list[str]] | None:
    changed_paths = _changed_paths(repo, ancestor, current)
    if changed_paths is None:
        return None
    classified = {
        path: _domains_for_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        )
        for path in changed_paths
    }
    unknown = sorted(
        path
        for path, domains in classified.items()
        if not domains
        and not _ignored_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        )
    )
    return changed_paths, classified, unknown


def _domain_change_summary(
    repo: Path,
    *,
    domain: str,
    ancestor: str,
    current: str,
) -> dict[str, Any] | None:
    change = _release_change_summary(repo, ancestor=ancestor, current=current)
    if change is None:
        return None
    changed_paths, classified, unknown = change
    ancestor_digest = _domain_digest(repo, ancestor, DOMAIN_PATHS[domain])
    current_digest = _domain_digest(repo, current, DOMAIN_PATHS[domain])
    if ancestor_digest is None or current_digest is None:
        return None
    domain_changed_paths = sorted(
        path for path, affected in classified.items() if domain in affected
    )
    return {
        "changed": bool(
            unknown
            or domain_changed_paths
            or ancestor_digest != current_digest
        ),
        "changed_paths": changed_paths,
        "domain_changed_paths": domain_changed_paths,
        "unknown_production_paths": unknown,
        "ancestor_digest": ancestor_digest,
        "current_digest": current_digest,
    }


def _domains_for_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> set[str]:
    if path in {"pyproject.toml", "eimemory/version.py"}:
        return set() if _version_metadata_only_change(
            repo,
            path=path,
            ancestor=ancestor,
            current=current,
        ) else set(DOMAINS)
    return {
        domain
        for domain, rules in DOMAIN_PATHS.items()
        if any(_path_matches_rule(path, rule) for rule in rules)
    }


def _path_matches_rule(path: str, rule: str) -> bool:
    return bool(
        path == rule
        or path.startswith(rule.rstrip("/") + "/")
        or (rule.endswith(("-", "_")) and path.startswith(rule))
    )


def _ignored_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    return (
        path in IGNORED_PATHS
        or path.startswith(IGNORED_PATH_PREFIXES)
        or path.startswith("README")
        or path.startswith("CHANGELOG")
        or (
            path in {"pyproject.toml", "eimemory/version.py"}
            and _version_metadata_only_change(
                repo,
                path=path,
                ancestor=ancestor,
                current=current,
            )
        )
        or (
            path in INTEGRATION_VERSION_PATHS
            and _integration_version_only_change(
                repo,
                path=path,
                ancestor=ancestor,
                current=current,
            )
        )
    )


def _version_metadata_only_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    before = _git_bytes(repo, "show", f"{ancestor}:{path}")
    after = _git_bytes(repo, "show", f"{current}:{path}")
    if before is None or after is None:
        return False
    try:
        if path == "pyproject.toml":
            before_payload = deepcopy(tomllib.loads(before.decode("utf-8")))
            after_payload = deepcopy(tomllib.loads(after.decode("utf-8")))
            for payload in (before_payload, after_payload):
                project = payload.get("project")
                if isinstance(project, dict):
                    project.pop("version", None)
            return before_payload == after_payload
        return _normalized_version_module(before) == _normalized_version_module(after)
    except (SyntaxError, UnicodeError, ValueError, TypeError):
        return False


def _integration_version_only_change(
    repo: Path,
    *,
    path: str,
    ancestor: str,
    current: str,
) -> bool:
    before = _git_bytes(repo, "show", f"{ancestor}:{path}")
    after = _git_bytes(repo, "show", f"{current}:{path}")
    if before is None or after is None:
        return False
    try:
        if path.endswith(".json"):
            before_payload = json.loads(before.decode("utf-8"))
            after_payload = json.loads(after.decode("utf-8"))
            if not isinstance(before_payload, dict) or not isinstance(after_payload, dict):
                return False
            before_payload.pop("version", None)
            after_payload.pop("version", None)
            return before_payload == after_payload
        version_line = re.compile(r"^version\s*:")

        def normalized_lines(raw: bytes) -> tuple[str, ...]:
            return tuple(
                line.rstrip()
                for line in raw.decode("utf-8").splitlines()
                if version_line.match(line) is None
            )

        return normalized_lines(before) == normalized_lines(after)
    except (UnicodeError, ValueError, TypeError):
        return False


def _normalized_version_module(raw: bytes) -> str:
    tree = ast.parse(raw.decode("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
                node.value = ast.Constant(value="<release-version>")
    return ast.dump(tree, include_attributes=False)


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
    domain_changed: bool = False,
) -> dict[str, str]:
    authorized_sources = {
        "memory.recall": {
            "eimemory.capability_replay",
            "eimemory.evaluation.production_recall",
            "eimemory.evaluation.production_recall.bootstrap",
        },
        "memory.governance": {"eimemory.capability_replay"},
        "channel.openclaw": {"eimemory.openclaw.channel_acceptance"},
        "storage.integrity": {"eimemory.live_task_acceptance"},
        "deployment.runtime": {"eimemory.deployment_receipt"},
    }
    errors: dict[str, str] = {}
    records: dict[str, Any] = {}
    current_receipt = runtime.store.get_by_id(current_release.receipt_id, scope=scope)
    if current_receipt is None:
        return {"__contract__": "current_deployment_receipt_missing"}
    candidate_records = {
        reference: runtime.store.get_by_id(reference, scope=scope)
        for reference in references
    }
    candidate_sources = {
        str(record.source or "")
        for record in candidate_records.values()
        if record is not None
    }
    pending_recall_contract = bool(
        domain == "memory.recall"
        and candidate_sources
        == {
            "eimemory.capability_replay",
            "eimemory.evaluation.production_recall.bootstrap",
        }
    )
    for reference in references:
        record = candidate_records[reference]
        if record is None:
            errors[reference] = "record_not_found"
            continue
        if not same_scope(record.scope, scope):
            errors[reference] = "scope_mismatch"
            continue
        if record.source not in authorized_sources[domain]:
            errors[reference] = "source_not_authorized_for_domain"
            continue
        if _explicit_failure(record):
            errors[reference] = "explicit_failure"
            continue
        if domain == "deployment.runtime":
            if reference != current_release.receipt_id:
                errors[reference] = "not_current_deployment_receipt"
                continue
        elif (
            pending_recall_contract
            and record.source == "eimemory.evaluation.production_recall.bootstrap"
        ):
            pass
        elif not _record_precedes(
            runtime,
            earlier=current_receipt,
            later=record,
            scope=scope,
        ):
            errors[reference] = "gate_not_after_current_receipt"
            continue
        records[reference] = record
    if errors:
        return errors

    if domain == "memory.recall":
        contract_error = _recall_gate_contract_error(
            runtime,
            scope=scope,
            current_release=current_release,
            references=references,
            records=records,
            domain_changed=domain_changed,
        )
    elif domain == "memory.governance":
        contract_error = _governance_gate_contract_error(
            runtime,
            scope=scope,
            current_release=current_release,
            references=references,
        )
    elif domain == "channel.openclaw":
        contract_error = _channel_acceptance_contract_error(
            current_release=current_release,
            references=references,
            records=records,
        )
    elif domain == "storage.integrity":
        contract_error = _live_acceptance_contract_error(
            runtime,
            scope=scope,
            current_release=current_release,
            references=references,
            records=records,
        )
    else:
        contract_error = (
            ""
            if references == [current_release.receipt_id]
            and same_release_authority(
                verified_deployment_receipt_identity(current_receipt),
                current_release,
            )
            else "exact_current_deployment_receipt_required"
        )
    if contract_error:
        errors["__contract__"] = contract_error
    return errors


def _explicit_failure(record: Any) -> bool:
    content = record.content if isinstance(getattr(record, "content", None), dict) else {}
    meta = record.meta if isinstance(getattr(record, "meta", None), dict) else {}
    verdict = str(content.get("verdict") or meta.get("verdict") or "").strip().lower()
    return bool(
        any(content.get(key) is False for key in ("passed", "ok", "accepted", "complete"))
        or verdict in {"fail", "failed", "blocked", "rejected"}
    )


def _recall_gate_contract_error(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity,
    references: list[str],
    records: dict[str, Any],
    domain_changed: bool,
) -> str:
    sources = {str(record.source or "") for record in records.values()}
    if sources == {
        "eimemory.capability_replay",
        "eimemory.evaluation.production_recall.bootstrap",
    }:
        return _pending_recall_gate_contract_error(
            runtime,
            scope=scope,
            current_release=current_release,
            references=references,
            records=records,
            domain_changed=domain_changed,
        )
    if sources != {
        "eimemory.evaluation.production_recall",
        "eimemory.evaluation.production_recall.bootstrap",
    }:
        return "recall_gate_evidence_contract_unrecognized"

    from eimemory.evaluation.real_query_gate import (
        verify_current_production_recall_gate,
        verify_current_production_recall_strict_state,
    )

    gate = verify_current_production_recall_gate(
        runtime,
        scope=scope,
        release=current_release,
        limit=500,
    )
    gate_id = str(gate.get("record_id") or "")
    if gate.get("ok") is not True or gate.get("status") != "accepted" or not gate_id:
        return str(gate.get("reason") or "current_production_recall_gate_invalid")
    strict = verify_current_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current_release,
        gate_record_id=gate_id,
    )
    strict_id = str(strict.get("record_id") or "")
    if (
        strict.get("ok") is not True
        or strict.get("status") != "strict_activated"
        or str(strict.get("candidate_commit") or "") != current_release.commit
        or str(strict.get("gate_record_id") or "") != gate_id
        or not strict_id
    ):
        return str(strict.get("reason") or "current_production_recall_strict_state_invalid")
    return "" if set(references) == {gate_id, strict_id} and len(references) == 2 else (
        "exact_recall_gate_and_strict_state_required"
    )


def _pending_recall_gate_contract_error(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity,
    references: list[str],
    records: dict[str, Any],
    domain_changed: bool,
) -> str:
    if domain_changed:
        return "bootstrap_pending_requires_unchanged_recall_domain"

    from eimemory.evaluation.real_query_gate import (
        verify_current_bootstrap_data_pending,
    )
    from eimemory.governance.l5_readiness import _verified_replay_summary

    pending = verify_current_bootstrap_data_pending(
        runtime,
        scope=scope,
        release=current_release,
    )
    pending_id = str(pending.get("record_id") or "")
    if (
        pending.get("ok") is not True
        or pending.get("status") != "bootstrap_data_pending"
        or not pending_id
    ):
        return str(pending.get("reason") or "current_bootstrap_pending_invalid")

    missing_field = "core_replay_capabilities_missing"
    replay = _verified_replay_summary(
        runtime,
        scope=scope,
        limit=2000,
        capabilities={"memory.recall"},
        missing_field=missing_field,
        release=current_release,
    )
    manifest_ids = {
        str(record_id or "")
        for record_id in dict(replay.get("manifest_record_ids") or {}).values()
        if str(record_id or "")
    }
    if (
        replay.get(missing_field)
        or replay.get("manifest_rejection_reasons")
        or replay.get("rejection_reasons")
        or int(replay.get("fail_count") or 0) != 0
        or int(replay.get("not_run_count") or 0) != 0
        or int(replay.get("executed_count") or 0)
        < int(replay.get("minimum_executed") or 1)
        or int(replay.get("pass_count") or 0)
        != int(replay.get("executed_count") or 0)
        or len(manifest_ids) != 1
    ):
        return "current_release_recall_replay_incomplete"
    core_manifest_id = next(iter(manifest_ids))
    if (
        set(references) != {pending_id, core_manifest_id}
        or len(references) != 2
        or pending_id not in records
        or core_manifest_id not in records
    ):
        return "exact_bootstrap_pending_and_recall_replay_required"
    if not _record_precedes(
        runtime,
        earlier=records[pending_id],
        later=records[core_manifest_id],
        scope=scope,
    ):
        return "bootstrap_pending_must_precede_recall_replay"
    return ""


def _governance_gate_contract_error(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity,
    references: list[str],
) -> str:
    from eimemory.governance.l5_readiness import _verified_replay_summary

    checks = (
        (
            _verified_replay_summary(
                runtime,
                scope=scope,
                limit=2000,
                capabilities=set(WEAK_REPLAY_CAPABILITIES),
                missing_field="weak_replay_capabilities_missing",
                release=current_release,
            ),
            "weak_replay_capabilities_missing",
        ),
        (
            _verified_replay_summary(
                runtime,
                scope=scope,
                limit=2000,
                capabilities=set(CORE_REPLAY_CAPABILITIES),
                missing_field="core_replay_capabilities_missing",
                release=current_release,
            ),
            "core_replay_capabilities_missing",
        ),
    )
    expected: set[str] = set()
    for summary, missing_field in checks:
        expected.update(
            str(record_id or "")
            for record_id in dict(summary.get("manifest_record_ids") or {}).values()
            if str(record_id or "")
        )
        if (
            summary.get(missing_field)
            or summary.get("manifest_rejection_reasons")
            or summary.get("rejection_reasons")
            or int(summary.get("fail_count") or 0) != 0
            or int(summary.get("not_run_count") or 0) != 0
            or int(summary.get("executed_count") or 0)
            < int(summary.get("minimum_executed") or 1)
            or int(summary.get("pass_count") or 0)
            != int(summary.get("executed_count") or 0)
        ):
            return "current_release_replay_manifests_incomplete"
    return "" if expected and set(references) == expected and len(references) == len(expected) else (
        "exact_current_release_replay_manifests_required"
    )


def _channel_acceptance_contract_error(
    *,
    current_release: ReleaseIdentity,
    references: list[str],
    records: dict[str, Any],
) -> str:
    from eimemory.governance.openclaw_channel_acceptance import (
        validate_openclaw_channel_acceptance,
    )

    if len(references) != 1 or set(references) != set(records):
        return "exact_current_channel_acceptance_required"
    record = records.get(references[0])
    return (
        ""
        if validate_openclaw_channel_acceptance(
            record,
            current_release=current_release,
        )
        else "current_channel_acceptance_invalid"
    )


def _live_acceptance_contract_error(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity,
    references: list[str],
    records: dict[str, Any],
) -> str:
    from eimemory.governance.live_task_acceptance import (
        LIVE_ACCEPTANCE_CASE_IDS,
        live_acceptance_task_type,
        validate_live_acceptance_case,
    )

    receipt = runtime.store.get_by_id(current_release.receipt_id, scope=scope)
    content = receipt.content if receipt is not None and isinstance(receipt.content, dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    release = side_effect.get("release") if isinstance(side_effect.get("release"), dict) else {}
    identity = {
        "commit": current_release.commit,
        "version": current_release.version,
        "release_path": str(release.get("release_path") or ""),
        "promotion_request_id": current_release.receipt_id,
        "release_session_id": current_release.session_id,
    }
    expected_ids = set(LIVE_ACCEPTANCE_CASE_IDS)
    by_case: dict[str, Any] = {}
    for reference in references:
        record = records.get(reference)
        payload = record.content if record is not None and isinstance(record.content, dict) else {}
        case_id = str(payload.get("case_id") or "")
        task_type = str(payload.get("task_type") or "")
        trace_id = str(payload.get("trace_id") or "")
        passed = payload.get("passed")
        if (
            case_id in by_case
            or passed is not True
            or not validate_live_acceptance_case(
                runtime,
                scope=scope,
                evidence=record,
                case_id=case_id,
                task_type=task_type,
                trace_id=trace_id,
                deployment_commit=current_release.commit,
                passed=True,
                identity=identity,
            )
            or task_type != live_acceptance_task_type(case_id)
        ):
            return "live_acceptance_case_invalid"
        by_case[case_id] = record
    return "" if set(by_case) == expected_ids and len(references) == len(expected_ids) else (
        "complete_canonical_live_acceptance_set_required"
    )


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


def _lineage_evidence_references(lineage: Mapping[str, Any]) -> list[str]:
    references = [
        str(
            (
                lineage.get("current_release")
                if isinstance(lineage.get("current_release"), Mapping)
                else {}
            ).get("receipt_id")
            or ""
        )
    ]
    ancestor = (
        lineage.get("ancestor_release")
        if isinstance(lineage.get("ancestor_release"), Mapping)
        else {}
    )
    references.append(str(ancestor.get("receipt_id") or ""))
    domains = lineage.get("domains") if isinstance(lineage.get("domains"), Mapping) else {}
    for state in domains.values():
        if not isinstance(state, Mapping):
            continue
        evidence_release = (
            state.get("evidence_release")
            if isinstance(state.get("evidence_release"), Mapping)
            else {}
        )
        references.append(str(evidence_release.get("receipt_id") or ""))
        references.extend(
            str(reference or "")
            for reference in (
                state.get("gate_evidence")
                if isinstance(state.get("gate_evidence"), list)
                else []
            )
        )
    return list(dict.fromkeys(reference for reference in references if reference))


def _public_report(lineage: dict[str, Any], *, record_id: str) -> dict[str, Any]:
    return {
        **lineage,
        "record_id": record_id,
        "validated": True,
    }
