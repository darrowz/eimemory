from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake.packs import export_knowledge_pack, import_knowledge_pack
from eimemory.models.records import RecordEnvelope, ScopeRef


def _runtime(tmp_path, name: str = "runtime") -> Runtime:
    return Runtime.create(root=tmp_path / name)


def _record(
    runtime: Runtime,
    *,
    kind: str,
    title: str,
    scope: ScopeRef,
    status: str = "active",
) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=f"{title} summary",
        detail=f"{title} detail",
        content={"text": f"{title} durable text"},
        scope=scope,
        status=status,
        source="test.pack",
        meta={"source_marker": title},
    )
    return runtime.store.append(record)


def test_export_knowledge_pack_writes_manifest_and_records(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    _record(runtime, kind="memory", title="Stable memory", scope=scope)
    _record(runtime, kind="claim_card", title="Claim", scope=scope)
    _record(runtime, kind="knowledge_page", title="Page", scope=scope)
    _record(runtime, kind="paper_source", title="Paper", scope=scope)
    _record(runtime, kind="knowledge_candidate", title="Candidate", scope=scope, status="candidate")
    _record(
        runtime,
        kind="memory",
        title="Other tenant",
        scope=ScopeRef(tenant_id="tenant-b", agent_id="agent-a", workspace_id="repo-a"),
    )

    report = export_knowledge_pack(runtime, tmp_path / "pack", scope)

    manifest = json.loads((tmp_path / "pack" / "manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (tmp_path / "pack" / "records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert report["record_count"] == 4
    assert manifest["format_version"] == "eimemory-pack-v1"
    assert manifest["scope"] == asdict(scope)
    assert manifest["record_count"] == 4
    assert manifest["kind_counts"] == {
        "claim_card": 1,
        "knowledge_page": 1,
        "memory": 1,
        "paper_source": 1,
    }
    assert len(manifest["sha256"]) == 64
    assert {record["kind"] for record in records} == {"memory", "claim_card", "knowledge_page", "paper_source"}


def test_import_knowledge_pack_dry_run_reports_without_writing(tmp_path) -> None:
    source = _runtime(tmp_path, "source")
    target = _runtime(tmp_path, "target")
    source_scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    target_scope = {"tenant_id": "tenant-b", "agent_id": "agent-b", "workspace_id": "repo-b"}
    _record(source, kind="memory", title="Portable memory", scope=source_scope)
    export_knowledge_pack(source, tmp_path / "pack", source_scope)

    report = import_knowledge_pack(target, tmp_path / "pack", target_scope, dry_run=True)

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["record_count"] == 1
    assert target.store.list_records(scope=target_scope, limit=10) == []


def test_export_knowledge_pack_can_include_reviewable_candidates(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    _record(runtime, kind="knowledge_candidate", title="Candidate", scope=scope, status="candidate")
    _record(runtime, kind="knowledge_candidate", title="Reviewed", scope=scope, status="reviewed")
    _record(runtime, kind="knowledge_candidate", title="Promoted", scope=scope, status="promoted")
    _record(runtime, kind="knowledge_candidate", title="Rejected", scope=scope, status="rejected")

    report = export_knowledge_pack(runtime, tmp_path / "pack", scope, include_candidates=True)

    records = [
        json.loads(line)
        for line in (tmp_path / "pack" / "records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert report["kind_counts"] == {"knowledge_candidate": 3}
    assert {record["status"] for record in records} == {"candidate", "reviewed", "promoted"}


def test_import_knowledge_pack_rewrites_scope_to_target_scope(tmp_path) -> None:
    source = _runtime(tmp_path, "source")
    target = _runtime(tmp_path, "target")
    source_scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a", user_id="alice")
    target_scope = {"tenant_id": "tenant-b", "agent_id": "agent-b", "workspace_id": "repo-b", "user_id": "bob"}
    exported = _record(source, kind="memory", title="Portable memory", scope=source_scope)
    export_knowledge_pack(source, tmp_path / "pack", source_scope)

    report = import_knowledge_pack(target, tmp_path / "pack", target_scope)

    imported = target.store.get_by_id(exported.record_id)
    assert report["written_count"] == 1
    assert imported is not None
    assert imported.scope == ScopeRef.from_dict(target_scope)
    assert target.store.list_records(scope=source_scope, limit=10) == []


def test_import_knowledge_pack_rejects_hash_mismatch(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    _record(runtime, kind="memory", title="Portable memory", scope=scope)
    export_knowledge_pack(runtime, tmp_path / "pack", scope)
    with (tmp_path / "pack" / "records.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(ValueError, match="hash mismatch"):
        import_knowledge_pack(runtime, tmp_path / "pack", scope)


def test_import_knowledge_pack_rejects_record_id_collision(tmp_path) -> None:
    source = _runtime(tmp_path, "source")
    target = _runtime(tmp_path, "target")
    source_scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a")
    target_scope = ScopeRef(tenant_id="tenant-b", agent_id="agent-b")
    record = _record(source, kind="memory", title="Portable memory", scope=source_scope)
    target.store.append(record)
    export_knowledge_pack(source, tmp_path / "pack-collision", source_scope)

    dry_run = import_knowledge_pack(target, tmp_path / "pack-collision", target_scope, dry_run=True)

    assert dry_run["collision_count"] == 1
    assert dry_run["collisions"] == [record.record_id]
    with pytest.raises(ValueError, match="record id collision"):
        import_knowledge_pack(target, tmp_path / "pack-collision", target_scope)


def test_import_knowledge_pack_rejects_invalid_manifest(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    _record(runtime, kind="memory", title="Portable memory", scope=scope)
    export_knowledge_pack(runtime, tmp_path / "pack", scope)
    (tmp_path / "pack" / "manifest.json").write_text(
        json.dumps({"format_version": "wrong"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid manifest"):
        import_knowledge_pack(runtime, tmp_path / "pack", scope)
