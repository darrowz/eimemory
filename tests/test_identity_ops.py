from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.identity import hongtu_scope
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_hongtu_scope_recall_reads_legacy_main_and_honjia_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="Legacy main scope memory for unified Hongtu recall.",
        memory_type="fact",
        title="Legacy main memory",
        scope={"agent_id": "main", "workspace_id": ""},
        source="openclaw.agent_end",
    )
    runtime.memory.ingest(
        text="Legacy honjia body memory for unified Hongtu recall.",
        memory_type="fact",
        title="Legacy honjia memory",
        scope={"agent_id": "honxin", "workspace_id": "honjia"},
        source="eibrain.dialogue",
    )

    bundle = runtime.memory.recall(
        query="legacy unified Hongtu recall",
        scope=hongtu_scope({"user_id": "darrow"}),
        task_context={"task_type": "chat.reply"},
        limit=10,
    )
    titles = {item.title for item in bundle.items}

    assert "Legacy main memory" in titles
    assert "Legacy honjia memory" in titles


def test_identity_repair_rewrites_legacy_scope_and_backfills_identity(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    record = runtime.memory.ingest(
        text="Legacy memory awaiting unified Hongtu identity repair.",
        memory_type="fact",
        title="Repair candidate",
        scope={"agent_id": "main", "workspace_id": ""},
        source="openclaw.agent_end",
    )

    report = repair_hongtu_identity(runtime, apply=True)
    repaired = runtime.store.get_by_id(
        record.record_id,
        scope={"agent_id": "hongtu", "workspace_id": "embodied"},
    )
    legacy = runtime.store.get_by_id(
        record.record_id,
        scope={"agent_id": "main", "workspace_id": ""},
    )

    assert report["candidate_count"] >= 1
    assert report["repaired_count"] >= 1
    assert repaired is not None
    assert repaired.scope.agent_id == "hongtu"
    assert repaired.scope.workspace_id == "embodied"
    assert repaired.meta["identity"] == "hongtu"
    assert repaired.meta["communication_channel_role"] == "official"
    assert legacy is None


def test_cli_identity_report_and_repair(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.memory.ingest(
        text="Legacy CLI repair candidate",
        memory_type="fact",
        title="CLI repair candidate",
        scope={"agent_id": "honxin", "workspace_id": "honjia"},
        source="eibrain.dialogue",
    )
    runtime.close()

    assert cli_main(["identity", "report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["legacy_scope_records"] >= 1

    assert cli_main(["identity", "repair", "--apply"]) == 0
    repair = json.loads(capsys.readouterr().out)
    assert repair["repaired_count"] >= 1

    assert cli_main(["identity", "report"]) == 0
    final_report = json.loads(capsys.readouterr().out)
    assert final_report["legacy_scope_records"] == 0
    assert final_report["hongtu_identity_records"] >= 1


def test_cli_nightly_normalizes_default_scope_and_repairs_identity(tmp_path, monkeypatch, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "settings.json"
    note = tmp_path / "nightly.md"
    config_path.write_text(
        json.dumps({"default_agent_id": "honxin", "default_workspace_id": "honjia"}),
        encoding="utf-8",
    )
    note.write_text(
        "Nightly Hongtu intake should enter the unified embodied memory subject.",
        encoding="utf-8",
    )
    monkeypatch.setenv("EIMEMORY_ROOT", str(runtime_root))
    monkeypatch.setenv("EIMEMORY_CONFIG_PATH", str(config_path))

    assert cli_main(["source", "add", "--source-kind", "manual", "--title", "Nightly Hongtu", "--uri", str(note)]) == 0
    capsys.readouterr()
    assert cli_main(["nightly"]) == 0
    nightly = json.loads(capsys.readouterr().out)

    runtime = Runtime.create(root=runtime_root)
    report = repair_hongtu_identity(runtime, apply=False)
    records = runtime.store.list_records(limit=100)
    runtime.close()

    assert nightly["identity_repair"]["candidate_count"] >= 1
    assert nightly["identity_repair"]["repaired_count"] >= 1
    assert report["legacy_scope_records"] == 0
    assert report["repair_candidate_count"] == 0
    assert any(record.kind == "knowledge_candidate" for record in records)
    assert all(record.scope.agent_id == "hongtu" for record in records)
    assert all(record.scope.workspace_id == "embodied" for record in records)
    assert all(record.meta.get("identity") == "hongtu" for record in records)


def test_identity_repair_rewrites_hongtu_source_records_from_orphan_scopes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    blank = runtime.memory.ingest(
        text="OpenClaw orphan scope record must still belong to Hongtu unified memory.",
        memory_type="fact",
        title="OpenClaw orphan",
        scope={"agent_id": "", "workspace_id": ""},
        source="openclaw.agent_end",
    )
    smoke = RecordEnvelope.create(
        kind="recall_view",
        title="Smoke recall orphan",
        summary="Recall view created by OpenClaw smoke with a non-canonical scope.",
        detail="Recall view created by OpenClaw smoke with a non-canonical scope.",
        scope=ScopeRef(tenant_id="tenant-smoke", agent_id="main", workspace_id="repo-smoke", user_id="user-smoke"),
        source="openclaw.before_prompt_build",
    )
    runtime.store.append(smoke)

    preview = repair_hongtu_identity(runtime, apply=False)
    applied = repair_hongtu_identity(runtime, apply=True)
    repaired_blank = runtime.store.get_by_id(blank.record_id, scope={"agent_id": "hongtu", "workspace_id": "embodied"})
    repaired_smoke = runtime.store.get_by_id(smoke.record_id, scope=hongtu_scope({"tenant_id": "tenant-smoke", "user_id": "user-smoke"}))
    runtime.close()

    assert preview["candidate_count"] == 2
    assert applied["repaired_count"] == 2
    assert applied["repair_candidate_count"] == 0
    assert repaired_blank is not None
    assert repaired_blank.meta["identity"] == "hongtu"
    assert repaired_smoke is not None
    assert repaired_smoke.scope.tenant_id == "tenant-smoke"
    assert repaired_smoke.scope.user_id == "user-smoke"
    assert repaired_smoke.scope.agent_id == "hongtu"
    assert repaired_smoke.scope.workspace_id == "embodied"
    assert repaired_smoke.meta["identity"] == "hongtu"
