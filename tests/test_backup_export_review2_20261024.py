"""Failed logical snapshots must preserve the last recoverable generation."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest

from eimemory.api.runtime import Runtime
from eimemory.compatibility import migration_helpers as helper
from eimemory.models.records import RecordEnvelope, ScopeRef


def add_record(runtime, number=1):
    return runtime.store.append(RecordEnvelope.create(
        kind="memory", title=f"Backup generation {number}",
        summary=f"Preserve recovery evidence for generation {number}.",
        scope=ScopeRef(agent_id="review", workspace_id="backup"),
        source_id="review-source",
    ))


@pytest.mark.parametrize("operation", ["export", "backup"])
def test_unreadable_journal_preserves_the_last_complete_output(tmp_path, operation):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        add_record(runtime)
        target = tmp_path / "snapshot"
        if operation == "export":
            helper.export_records(runtime, target)
            retained = {target: target.read_bytes()}
        else:
            prior = helper.backup_create(runtime, target)
            retained = {Path(prior[key]): Path(prior[key]).read_bytes()
                        for key in ("data_path", "manifest_path")}
        with runtime.store.log.path.open("ab") as handle:
            handle.write(b'{"incomplete_journal":\n')
        with pytest.raises(ValueError):
            if operation == "export":
                helper.export_records(runtime, target)
            else:
                helper.backup_create(runtime, target)
        assert all(path.read_bytes() == content for path, content in retained.items())
        if operation == "backup":
            assert helper.backup_verify(target)["ok"] is True
        assert not list(tmp_path.glob(".eimemory-export-*"))


def test_manifest_preparation_failure_keeps_previous_backup_valid(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        first = add_record(runtime)
        target = tmp_path / "snapshot"
        original = helper.backup_create(runtime, target)
        original_data = Path(original["data_path"]).read_bytes()
        add_record(runtime, 2)

        def fail_timestamp():
            raise OSError("manifest preparation failed")

        monkeypatch.setattr(helper, "now_iso", fail_timestamp)
        with pytest.raises(OSError, match="manifest preparation failed"):
            helper.backup_create(runtime, target)
        verified = helper.backup_verify(target)
        assert verified["ok"] is True
        assert verified["record_count"] == 1
        assert Path(original["data_path"]).read_bytes() == original_data
        assert json.loads(original_data)["record_id"] == first.record_id


@pytest.mark.parametrize("log_only", [False, True])
def test_export_of_scope_move_cannot_resurrect_the_previous_identity(tmp_path, log_only):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        record = add_record(runtime)
        previous_scope = record.scope
        record.scope = ScopeRef(agent_id="review", workspace_id="moved")
        runtime.store.rewrite(record, previous_scope=previous_scope)
        reader = (SimpleNamespace(store=SimpleNamespace(
            list_records=lambda **_: [], log=runtime.store.log,
        )) if log_only else runtime)
        target = tmp_path / "snapshot.jsonl"
        assert helper.export_records(reader, target) == 1
        payload = json.loads(target.read_text())
        assert payload["scope"] == record.to_dict()["scope"]
        assert payload["source_id"] == record.source_id


@pytest.mark.parametrize("after_publication", [False, True])
def test_backup_manifest_failure_leaves_a_complete_verifiable_generation(tmp_path, monkeypatch, after_publication):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        add_record(runtime)
        target = tmp_path / "snapshot"
        original = helper.backup_create(runtime, target)
        old_data = Path(original["data_path"]).read_bytes()
        add_record(runtime, 2)
        publish = helper.atomic_write_json

        def fail_publication(path, manifest):
            # The old reader remains valid while the new complete data exists.
            assert helper.backup_verify(target)["record_count"] == 1
            assert len((Path(path).parent / manifest["data_file"]).read_text().splitlines()) == 2
            if after_publication:
                publish(path, manifest)
            raise OSError("manifest publication interrupted")

        monkeypatch.setattr(helper, "atomic_write_json", fail_publication)
        with pytest.raises(OSError, match="publication interrupted"):
            helper.backup_create(runtime, target)
        verified = helper.backup_verify(target)
        assert verified["ok"] is True
        assert verified["record_count"] == (2 if after_publication else 1)
        assert Path(original["data_path"]).read_bytes() == old_data
        assert not list(tmp_path.glob(".eimemory-export-*"))


def test_replacement_backup_uses_its_manifest_and_survives_relocation(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        add_record(runtime)
        folder = tmp_path / "backups"
        folder.mkdir()
        first = helper.backup_create(runtime, folder)
        add_record(runtime, 2)
        second = helper.backup_create(runtime, folder)
        assert second["data_path"] != first["data_path"]
        assert not Path(second["manifest"]["data_file"]).is_absolute()
        moved = tmp_path / "relocated"
        folder.rename(moved)
        verified = helper.backup_verify(moved)
        assert verified["ok"] is True
        assert verified["record_count"] == 2


def test_export_drains_committed_scope_move_before_journal_fallback(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        record = add_record(runtime)
        previous = record.scope
        record.scope = ScopeRef(agent_id="review", workspace_id="moved")
        with monkeypatch.context() as fault:
            def fail_flush(*_args):
                raise OSError("journal temporarily unavailable")
            fault.setattr(runtime.store, "_flush_committed_exports", fail_flush)
            runtime.store.rewrite(record, previous_scope=previous)
        with runtime.store.locked() as sqlite:
            assert sqlite.pending_exports(limit=1)
        target = tmp_path / "snapshot"
        assert helper.export_records(runtime, target) == 1
        assert json.loads(target.read_text())["scope"] == record.to_dict()["scope"]
        with runtime.store.locked() as sqlite:
            assert not sqlite.pending_exports(limit=1)


def test_pending_journal_rejects_backup_without_losing_previous_generation(tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        add_record(runtime)
        target = tmp_path / "snapshot"
        original = helper.backup_create(runtime, target)
        monkeypatch.setattr(runtime.store, "flush_exports", lambda **_: {"exported": 0, "remaining": 1})
        with pytest.raises(RuntimeError, match="backup_journal_exports_pending"):
            helper.backup_create(runtime, target)
        assert helper.backup_verify(target)["sha256"] == original["manifest"]["sha256"]
        with runtime.store.locked() as sqlite:
            assert not sqlite.in_transaction


def test_scope_move_on_another_connection_waits_for_export_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    with closing(Runtime.create(root=root)) as runtime, closing(Runtime.create(root=root)) as writer:
        record = add_record(runtime)
        previous = record.scope
        record.scope = ScopeRef(agent_id="review", workspace_id="moved")
        started, finished = Event(), Event()
        native = helper._records_from_sqlite
        pending = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            def move():
                started.set()
                try:
                    return writer.store.rewrite(record, previous_scope=previous)
                finally:
                    finished.set()

            def paused_reader(value):
                yield from native(value)
                pending.append(pool.submit(move))
                assert started.wait(timeout=5)
                assert not finished.wait(timeout=0.15)

            monkeypatch.setattr(helper, "_records_from_sqlite", paused_reader)
            target = tmp_path / "snapshot"
            assert helper.export_records(runtime, target) == 1
            pending[0].result(timeout=5)
        assert json.loads(target.read_text())["scope"]["workspace_id"] == previous.workspace_id
        assert runtime.store.get_by_id(record.record_id, scope=previous, exact_scope=True) is None


def test_late_duplicate_scope_move_does_not_retire_a_newer_return_move(tmp_path):
    with closing(Runtime.create(root=tmp_path / "runtime")) as runtime:
        record = add_record(runtime)
        first_scope = record.scope
        record.scope = ScopeRef(agent_id="review", workspace_id="moved")
        runtime.store.rewrite(record, previous_scope=first_scope)
        marker = next(entry for entry in runtime.store.log.scan_strict() if "_record_scope_move" in entry.payload)
        second_scope = record.scope
        record.scope = first_scope
        runtime.store.rewrite(record, previous_scope=second_scope)
        with runtime.store.log.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**marker.payload, "_operation_id": marker.operation_id,
                                     "_payload_digest": marker.payload_digest}) + "\n")
        reader = SimpleNamespace(store=SimpleNamespace(list_records=lambda **_: [], log=runtime.store.log))
        target = tmp_path / "snapshot"
        assert helper.export_records(reader, target) == 1
        assert json.loads(target.read_text())["scope"] == record.to_dict()["scope"]
