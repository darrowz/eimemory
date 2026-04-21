import json
import sqlite3
from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.compatibility.migration_helpers import (
    build_review_report,
    backup_create,
    backup_verify,
    import_candidates,
    scan_migration_source,
)


def test_scan_markdown_only_accepts_substantive_notes(tmp_path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "good.md").write_text("# Preference\n\nRemember concise operator replies for mobile voice output.\n", encoding="utf-8")
    (notes / "bad.md").write_text("ok\n", encoding="utf-8")

    report = scan_migration_source(notes)

    accepted = [item for item in report["candidates"] if item["decision"] == "accept"]
    rejected = [item for item in report["candidates"] if item["decision"] == "reject"]
    assert len(accepted) == 1
    assert accepted[0]["title"] == "Preference"
    assert rejected[0]["reason"] == "content_too_thin"


def test_scan_jsonl_filters_non_memory_records(tmp_path) -> None:
    source = tmp_path / "records.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "record_id": "mem_1",
                        "kind": "memory",
                        "title": "Portable note",
                        "summary": "Remember the deployment checklist for honxin production.",
                        "content": {"text": "Remember the deployment checklist for honxin production."},
                        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
                        "time": {"created_at": "2026-04-20T00:00:00+00:00", "updated_at": "2026-04-20T00:00:00+00:00", "occurred_at": "2026-04-20T00:00:00+00:00"},
                    }
                ),
                json.dumps(
                    {
                        "record_id": "inc_1",
                        "kind": "incident",
                        "title": "Incident",
                        "summary": "Do not import this into long-term memory.",
                        "content": {"text": "Do not import this into long-term memory."},
                        "scope": {"agent_id": "main", "workspace_id": "repo-x"},
                        "time": {"created_at": "2026-04-20T00:00:00+00:00", "updated_at": "2026-04-20T00:00:00+00:00", "occurred_at": "2026-04-20T00:00:00+00:00"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = scan_migration_source(source)

    assert [item["decision"] for item in report["candidates"]] == ["accept", "reject"]
    assert report["candidates"][1]["reason"] == "unsupported_kind"


def test_scan_sqlite_supports_openclaw_chunks_table(tmp_path) -> None:
    source = tmp_path / "main.sqlite"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL)")
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, chunk_text TEXT NOT NULL)")
    conn.execute("INSERT INTO files (id, path) VALUES (1, 'notes/operator.md')")
    conn.execute("INSERT INTO chunks (file_id, chunk_text) VALUES (1, 'Remember to keep replies short for the operator.')")
    conn.commit()
    conn.close()

    report = scan_migration_source(source)

    accepted = [item for item in report["candidates"] if item["decision"] == "accept"]
    assert len(accepted) == 1
    assert accepted[0]["source_ref"].endswith("notes/operator.md")


def test_scan_sqlite_supports_openclaw_path_text_schema(tmp_path) -> None:
    source = tmp_path / "main.sqlite"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'memory', hash TEXT NOT NULL, mtime INTEGER NOT NULL, size INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, path TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'memory', start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, hash TEXT NOT NULL, model TEXT NOT NULL, text TEXT NOT NULL, embedding TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO files (path, source, hash, mtime, size) VALUES ('notes/voice.md', 'memory', 'h1', 0, 10)")
    conn.execute(
        "INSERT INTO chunks (id, path, source, start_line, end_line, hash, model, text, embedding, updated_at) VALUES ('c1', 'notes/voice.md', 'memory', 1, 3, 'h1', 'local', 'Remember to keep replies short for mobile voice output.', '[]', 0)"
    )
    conn.commit()
    conn.close()

    report = scan_migration_source(source)

    accepted = [item for item in report["candidates"] if item["decision"] == "accept"]
    assert len(accepted) == 1
    assert accepted[0]["source_ref"].endswith("notes/voice.md")


def test_import_candidates_only_ingests_selected_high_confidence_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "good.md").write_text("# Keep replies short\n\nRemember concise replies for embodied output.\n", encoding="utf-8")
    (notes / "skip.md").write_text("fine\n", encoding="utf-8")

    report = scan_migration_source(notes)
    imported = import_candidates(
        runtime,
        report["candidates"],
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )

    bundle = runtime.memory.recall(
        query="concise embodied output",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    assert imported == 1
    assert len(bundle.items) == 1
    assert bundle.items[0].title == "Keep replies short"


def test_cli_migrate_scan_and_import_round_trip(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "good.md").write_text("# Deployment note\n\nRemember the honxin deployment checklist and rollback steps.\n", encoding="utf-8")

    assert cli_main(["migrate", "scan", str(notes)]) == 0
    scan_output = json.loads(capsys.readouterr().out)
    accepted_ids = [item["candidate_id"] for item in scan_output["candidates"] if item["decision"] == "accept"]

    assert cli_main(["migrate", "import", str(notes), "--candidate-id", accepted_ids[0]]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["imported"] == 1


def test_build_review_report_renders_batch_review_template(tmp_path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "good.md").write_text("# Deployment note\n\nRemember the honxin deployment checklist and rollback steps.\n", encoding="utf-8")
    (notes / "bad.md").write_text("ok\n", encoding="utf-8")

    report = scan_migration_source(notes)
    rendered = build_review_report(report)

    assert "# Migration Review Report" in rendered
    assert "- [ ] Import `md-" in rendered
    assert "- [x] Reject `md-" in rendered
    assert "eimemory migrate import" in rendered


def test_cli_migrate_report_writes_markdown_template(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "good.md").write_text("# Review me\n\nRemember the approved rollback note for production.\n", encoding="utf-8")
    output_path = tmp_path / "review.md"

    assert cli_main(["migrate", "report", str(notes), "--output", str(output_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = output_path.read_text(encoding="utf-8")

    assert payload["ok"] is True
    assert payload["output"] == str(output_path)
    assert "Migration Review Report" in rendered


def test_backup_create_and_verify_round_trip_for_directory_target(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.memory.ingest(
        text="Backup must preserve records",
        memory_type="fact",
        title="Backup note",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    report = backup_create(runtime, backup_dir)
    verified = backup_verify(backup_dir)

    assert report["ok"] is True
    assert report["data_path"].endswith("backup.jsonl")
    assert report["manifest_path"].endswith("backup.manifest.json")
    assert verified["ok"] is True
    assert verified["record_count"] == 1
    assert verified["manifest"]["record_count"] == 1
    assert verified["manifest"]["format_version"] == 1


def test_backup_create_and_verify_round_trip_for_base_path(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.memory.ingest(
        text="Backup should support a base path",
        memory_type="fact",
        title="Base path backup",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    backup_base = tmp_path / "snapshot"

    report = backup_create(runtime, backup_base)
    verified = backup_verify(backup_base)

    assert report["ok"] is True
    assert report["data_path"].endswith("snapshot.jsonl")
    assert report["manifest_path"].endswith("snapshot.manifest.json")
    assert verified["ok"] is True
    assert verified["sha256"] == report["manifest"]["sha256"]


def test_backup_verify_detects_corruption(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.memory.ingest(
        text="Corruption should be detected",
        memory_type="fact",
        title="Corruption note",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )
    backup_base = tmp_path / "corruptible"
    report = backup_create(runtime, backup_base)
    data_path = tmp_path / "corruptible.jsonl"
    data_path.write_text("not-json\n", encoding="utf-8")

    verified = backup_verify(backup_base)

    assert report["ok"] is True
    assert verified["ok"] is False
    assert verified["errors"]
