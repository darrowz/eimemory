from __future__ import annotations

from hashlib import sha256
import json

from eimemory.cli.main import main as cli_main
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(agent_id="agent", workspace_id="workspace", user_id="user")


def _prepare_legacy_payload(root) -> None:
    store = SqliteRecordStore(root / "state" / "eimemory.sqlite", archive_writes=False)
    store.upsert(
        RecordEnvelope.create(
            kind="capability_score",
            title="legacy score",
            scope=SCOPE,
            content={
                "capability": "memory.recall",
                "score": 0.9,
                "report": {"samples": ["x" * 100_000]},
            },
            meta={"capability": "memory.recall", "score": 0.9},
        )
    )
    store.close()


def test_storage_status_and_vacuum_default_are_read_only(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    _prepare_legacy_payload(root)
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    db_path = root / "state" / "eimemory.sqlite"
    before = sha256(db_path.read_bytes()).hexdigest()

    assert cli_main(["storage", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert "records.payload_archive.v1" in status["pending"]
    assert status["footprint"]["sqlite_bytes"] > 0

    assert cli_main(["storage", "vacuum"]) == 0
    vacuum = json.loads(capsys.readouterr().out)
    assert vacuum["applied"] is False
    assert sha256(db_path.read_bytes()).hexdigest() == before


def test_storage_migrate_requires_offline_verified_snapshot(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    _prepare_legacy_payload(root)
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    snapshot = tmp_path / "snapshot"

    assert cli_main(["storage", "migrate", "--batch-size", "1"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error"] == "offline_snapshot_required"

    assert cli_main(
        ["storage", "snapshot", "--offline", "--snapshot-dir", str(snapshot)]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["ok"] is True

    assert cli_main(
        [
            "storage",
            "migrate",
            "--offline",
            "--snapshot-dir",
            str(snapshot),
            "--batch-size",
            "1",
            "--max-batches",
            "20",
        ]
    ) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["ok"] is True
    assert migrated["pending"] == []
