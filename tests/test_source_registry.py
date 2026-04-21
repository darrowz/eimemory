from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.intake.registry import SourceRegistry


def test_source_registry_add_list_and_scan_persists_candidates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    registry = SourceRegistry(runtime.sources.path)

    enabled = registry.add_source(
        {
            "source_kind": "rss",
            "title": "AI Research Feed",
            "uri": "https://example.com/feed",
            "tags": ["news", "paper"],
            "enabled": True,
            "metadata": {"topic": "ai"},
        }
    )
    registry.add_source(
        {
            "source_kind": "manual",
            "title": "Paused reading list",
            "uri": "notes://reading-list",
            "tags": ["review"],
            "enabled": False,
        }
    )

    listed = registry.list_sources()
    report = runtime.sources.scan_sources(store=runtime.store, scope={"agent_id": "main"}, persist=True)
    persisted = runtime.store.list_records(kinds=["source_candidate"], scope={"agent_id": "main"}, limit=10)
    recall = runtime.memory.recall(
        query="AI Research Feed",
        scope={"agent_id": "main"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert len(listed) == 2
    assert listed[0].source_id == enabled.source_id
    assert listed[0].enabled is True
    assert report["candidate_count"] == 1
    assert report["scanned_count"] == 1
    assert report["skipped_count"] == 1
    assert report["candidates"][0]["source_id"] == enabled.source_id
    assert report["candidates"][0]["provenance"]["source_id"] == enabled.source_id
    assert persisted[0].kind == "source_candidate"
    assert persisted[0].provenance["source_id"] == enabled.source_id
    assert persisted[0].meta["source_id"] == enabled.source_id
    assert recall.items == []


def test_cli_source_commands_add_list_and_scan(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    add_code = cli_main(
        [
            "source",
            "add",
            "--source-kind",
            "manual",
            "--title",
            "Reading list",
            "--uri",
            "notes://reading-list",
            "--tag",
            "paper",
            "--tag",
            "review",
        ]
    )
    add_output = json.loads(capsys.readouterr().out)

    list_code = cli_main(["source", "list"])
    list_output = json.loads(capsys.readouterr().out)

    scan_code = cli_main(["source", "scan", "--persist"])
    scan_output = json.loads(capsys.readouterr().out)

    runtime = Runtime.create(root=tmp_path)
    persisted = runtime.store.list_records(kinds=["source_candidate"], scope={"agent_id": "main"}, limit=10)

    assert add_code == 0
    assert add_output["source_kind"] == "manual"
    assert list_code == 0
    assert list_output[0]["title"] == "Reading list"
    assert scan_code == 0
    assert scan_output["candidate_count"] == 1
    assert persisted[0].kind == "source_candidate"
    assert persisted[0].provenance["source_id"] == list_output[0]["source_id"]
