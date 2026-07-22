from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

import deploy.migrate_storage_release as storage_release
from deploy.migrate_storage_release import main as storage_release_main
from eimemory.models.records import RecordEnvelope, ScopeRef
import eimemory.storage.maintenance as maintenance
from eimemory.storage.maintenance import create_consistent_storage_snapshot
from eimemory.storage.sqlite_store import SqliteRecordStore


COMMIT = "a" * 40
ATTEMPT = "aaaaaaaa-20260722T120000Z-1234"
SCOPE = ScopeRef(agent_id="agent", workspace_id="workspace", user_id="user")


def _legacy_record() -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="capability_score",
        title="legacy release payload",
        scope=SCOPE,
        source="eimemory.capability_ledger",
        content={
            "capability": "memory.recall",
            "score": 0.9,
            "report": {"samples": ["legacy-full-body-" + ("x" * 200_000)]},
        },
        meta={"capability": "memory.recall", "score": 0.9},
    )


def _args(action: str, root: Path, snapshot_root: Path, snapshot: Path, *extra: str) -> list[str]:
    return [
        action,
        "--root",
        str(root),
        "--snapshot-root",
        str(snapshot_root),
        "--snapshot-dir",
        str(snapshot),
        "--candidate-commit",
        COMMIT,
        "--attempt-id",
        ATTEMPT,
        *extra,
    ]


def test_release_helper_binds_snapshot_and_restores_legacy_payload_after_acceptance_failure(
    tmp_path, capsys
) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    record = _legacy_record()
    store.upsert(record)
    for index in range(64):
        store.upsert(
            RecordEnvelope.create(
                kind="capability_score",
                title=f"hot score {index}",
                scope=SCOPE,
                content={"capability": "memory.recall", "score": 1.0},
                meta={"capability": "memory.recall", "score": 1.0},
            )
        )
    store.conn.execute(
        "UPDATE records SET updated_at='2000-01-01T00:00:00+00:00' WHERE record_id=?",
        (record.record_id,),
    )
    store.conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id='records.payload_archive.v1'"
    )
    store.conn.commit()
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    snapshot = snapshot_root / ATTEMPT

    assert storage_release_main(_args("preflight", root, snapshot_root, snapshot)) == 0
    capsys.readouterr()
    assert storage_release_main(_args("snapshot", root, snapshot_root, snapshot)) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["binding"] == {
        "attempt_id": ATTEMPT,
        "candidate_commit": COMMIT,
    }
    identity = ["--snapshot-manifest-sha256", created["manifest_sha256"]]
    assert storage_release_main(
        _args(
            "migrate",
            root,
            snapshot_root,
            snapshot,
            "--batch-size",
            "1",
            "--max-batches",
            "20",
            *identity,
        )
    ) == 0
    capsys.readouterr()
    assert storage_release_main(
        _args("vacuum", root, snapshot_root, snapshot, *identity)
    ) == 0
    capsys.readouterr()

    migrated = sqlite3.connect(db_path)
    pointer = migrated.execute(
        "SELECT payload_pointer_json FROM records WHERE record_id=?", (record.record_id,)
    ).fetchone()[0]
    migrated.close()
    assert pointer

    # This is the candidate acceptance-failure path: restore before old 1.9.80 starts.
    assert storage_release_main(
        _args("restore", root, snapshot_root, snapshot, *identity)
    ) == 0
    capsys.readouterr()
    legacy = sqlite3.connect(db_path)
    payload_json = legacy.execute(
        "SELECT payload_json FROM records WHERE record_id=?", (record.record_id,)
    ).fetchone()[0]
    legacy.close()
    assert RecordEnvelope.from_dict(json.loads(payload_json)).content == record.content


def test_release_helper_rejects_snapshot_from_another_attempt(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    store.upsert(_legacy_record())
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    snapshot = snapshot_root / ATTEMPT
    assert storage_release_main(_args("snapshot", root, snapshot_root, snapshot)) == 0
    capsys.readouterr()

    wrong = _args("verify", root, snapshot_root, snapshot)
    wrong[wrong.index(COMMIT)] = "b" * 40
    assert storage_release_main(wrong) == 2
    report = json.loads(capsys.readouterr().out)
    assert "binding" in report["detail"]


def test_release_helper_rejects_snapshot_root_escape_without_creating_it(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    store = SqliteRecordStore(root / "state" / "eimemory.sqlite", archive_writes=False)
    store.upsert(_legacy_record())
    store.close()
    outside = tmp_path / "must-not-be-created"
    snapshot = outside / ATTEMPT

    assert storage_release_main(_args("preflight", root, outside, snapshot)) == 2
    report = json.loads(capsys.readouterr().out)
    assert "within runtime state" in report["detail"]
    assert not outside.exists()


def test_vacuum_backup_is_kept_until_explicit_release_cleanup(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    store = SqliteRecordStore(root / "state" / "eimemory.sqlite", archive_writes=False)
    store.upsert(_legacy_record())
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    snapshot = snapshot_root / ATTEMPT
    assert storage_release_main(_args("snapshot", root, snapshot_root, snapshot)) == 0
    created = json.loads(capsys.readouterr().out)
    identity = ["--snapshot-manifest-sha256", created["manifest_sha256"]]

    assert storage_release_main(
        _args("vacuum", root, snapshot_root, snapshot, *identity)
    ) == 0
    vacuum = json.loads(capsys.readouterr().out)
    backup = Path(vacuum["backup_path"])
    assert backup.is_file()
    assert snapshot.is_dir()

    assert storage_release_main(
        _args(
            "cleanup-vacuum",
            root,
            snapshot_root,
            snapshot,
            "--backup-path",
            str(backup),
            *identity,
        )
    ) == 0
    cleanup = json.loads(capsys.readouterr().out)
    assert cleanup["removed"] is True
    assert not backup.exists()
    assert not (root / "state" / ".storage-vacuum-journal.json").exists()
    assert snapshot.is_dir()


def test_release_helper_recovers_missing_live_database_before_safety_check(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.close()
    original = db_path.read_bytes()
    snapshot_root = root / "state" / "release-snapshots"
    snapshot = snapshot_root / ATTEMPT
    snapshot_root.mkdir(parents=True)
    temporary = db_path.parent / f".{db_path.name}.vacuum-{'d' * 32}"
    backup = db_path.parent / f".{db_path.name}.pre-vacuum-{'d' * 32}.bak"
    temporary.write_bytes(original)
    os.replace(db_path, backup)
    maintenance.atomic_write_json(
        db_path.parent / ".storage-vacuum-journal.json",
        {
            "schema": "storage_vacuum_journal.v1",
            "status": "in_progress",
            "phase": "live_moved",
            "database": str(db_path),
            "temporary": str(temporary),
            "backup": str(backup),
            "before_sha256": sha256(original).hexdigest(),
        },
    )

    assert storage_release_main(_args("recover-vacuum", root, snapshot_root, snapshot)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recovered"] == "rolled_back"
    assert db_path.read_bytes() == original


def test_needs_is_read_only_and_does_not_create_snapshot_when_nothing_is_pending(
    tmp_path, capsys
) -> None:
    root = tmp_path / "runtime"
    store = SqliteRecordStore(root / "state" / "eimemory.sqlite")
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    snapshot = snapshot_root / ATTEMPT

    assert storage_release_main(_args("needs", root, snapshot_root, snapshot)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["needed"] is False
    assert report["pending"] == []
    assert not snapshot.exists()


def test_snapshot_retention_keeps_current_and_one_previous(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    attempts = [f"{COMMIT}-20260722T12000{index}Z-{index}" for index in range(3)]
    for attempt in attempts:
        create_consistent_storage_snapshot(
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            snapshot_dir=snapshot_root / attempt,
            offline=True,
            binding={"candidate_commit": COMMIT, "attempt_id": attempt},
        )
    corrupt = snapshot_root / "corrupt-but-must-not-be-deleted"
    corrupt.mkdir()
    (corrupt / "storage-snapshot.json").write_text("[]", encoding="utf-8")

    current = attempts[2]
    args = _args("prune-snapshots", root, snapshot_root, snapshot_root / current)
    args[args.index(ATTEMPT, args.index("--attempt-id"))] = current
    args.extend(["--retain-snapshots", "2"])
    assert storage_release_main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["retained"] == 2
    assert len(report["removed"]) == 1
    assert (snapshot_root / attempts[2]).is_dir()
    assert (snapshot_root / attempts[1]).is_dir()
    assert not (snapshot_root / attempts[0]).exists()
    assert corrupt.is_dir()


def test_snapshot_retention_clamps_one_to_current_plus_previous(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    attempts = [f"{COMMIT}-20260722T13000{index}Z-{index}" for index in range(3)]
    for attempt in attempts:
        create_consistent_storage_snapshot(
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            snapshot_dir=snapshot_root / attempt,
            offline=True,
            binding={"candidate_commit": COMMIT, "attempt_id": attempt},
        )
    current = attempts[-1]
    args = _args("prune-snapshots", root, snapshot_root, snapshot_root / current)
    args[args.index(ATTEMPT, args.index("--attempt-id"))] = current
    args.extend(["--retain-snapshots", "1"])

    assert storage_release_main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["retained"] == 2
    assert (snapshot_root / attempts[-1]).is_dir()
    assert (snapshot_root / attempts[-2]).is_dir()


def test_snapshot_retention_never_deletes_deep_corrupt_candidate(tmp_path, capsys) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    attempts = [f"{COMMIT}-20260722T14000{index}Z-{index}" for index in range(3)]
    for attempt in attempts:
        create_consistent_storage_snapshot(
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            snapshot_dir=snapshot_root / attempt,
            offline=True,
            binding={"candidate_commit": COMMIT, "attempt_id": attempt},
        )
    corrupt = snapshot_root / attempts[0] / db_path.name
    os.chmod(corrupt, 0o600)
    with corrupt.open("ab") as handle:
        handle.write(b"corrupt")
    current = attempts[-1]
    args = _args("prune-snapshots", root, snapshot_root, snapshot_root / current)
    args[args.index(ATTEMPT, args.index("--attempt-id"))] = current
    args.extend(["--retain-snapshots", "2"])

    assert storage_release_main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert (snapshot_root / attempts[0]).is_dir()
    assert str(snapshot_root / attempts[0]) in report["ignored"]


def test_snapshot_retention_deep_verifies_only_deletion_candidates(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    attempts = [f"{COMMIT}-20260722T15000{index}Z-{index}" for index in range(3)]
    for attempt in attempts:
        create_consistent_storage_snapshot(
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            snapshot_dir=snapshot_root / attempt,
            offline=True,
            binding={"candidate_commit": COMMIT, "attempt_id": attempt},
        )
    verified: list[str] = []
    original_verify = storage_release.verify_storage_snapshot

    def counted_verify(path):
        verified.append(Path(path).name)
        return original_verify(path)

    monkeypatch.setattr(storage_release, "verify_storage_snapshot", counted_verify)
    current = attempts[-1]
    args = SimpleNamespace(
        action="prune-snapshots",
        root=str(root),
        snapshot_root=str(snapshot_root),
        snapshot_dir=str(snapshot_root / current),
        candidate_commit=COMMIT,
        attempt_id=current,
        snapshot_manifest_sha256="",
        backup_path="",
        retain_snapshots=2,
        batch_size=10,
        max_batches=20,
        max_seconds=60.0,
    )

    assert storage_release.run_action(args)["ok"] is True
    assert verified == [attempts[0]]


def test_release_migration_deep_verifies_once_then_uses_sealed_identity(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    db_path = root / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    store.conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id='records.payload_archive.v1'"
    )
    store.conn.commit()
    store.close()
    snapshot_root = root / "state" / "release-snapshots"
    snapshot = snapshot_root / ATTEMPT
    deep_verifications = 0
    original_verify = maintenance.verify_storage_snapshot

    def counted_verify(path):
        nonlocal deep_verifications
        deep_verifications += 1
        return original_verify(path)

    monkeypatch.setattr(maintenance, "verify_storage_snapshot", counted_verify)
    monkeypatch.setattr(storage_release, "verify_storage_snapshot", counted_verify)

    args = SimpleNamespace(
        action="snapshot",
        root=str(root),
        snapshot_root=str(snapshot_root),
        snapshot_dir=str(snapshot),
        candidate_commit=COMMIT,
        attempt_id=ATTEMPT,
        snapshot_manifest_sha256="",
        backup_path="",
        retain_snapshots=2,
        batch_size=10,
        max_batches=20,
        max_seconds=60.0,
    )
    created = storage_release.run_action(args)
    args.snapshot_manifest_sha256 = created["manifest_sha256"]
    args.action = "migrate"
    assert storage_release.run_action(args)["ok"] is True
    args.action = "vacuum"
    assert storage_release.run_action(args)["ok"] is True

    assert deep_verifications == 1


def test_installer_storage_transaction_order_and_writer_stop_contract() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    call_site = script.index("_run_pre_switch_production_recall_bootstrap\n")
    switch = script.index('ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"', call_site)
    assert call_site < script.index("_prepare_storage_for_release\n", call_site) < switch
    assert "eimemory-rpc.service" in script
    assert "openclaw-gateway.service" in script
    assert "eimemory-nightly.timer" in script
    assert "eimemory-learn-watch.timer" in script
    assert "eimemory-l5-observation-gate.timer" in script
    assert "EIMEMORY_DEPLOY_FAIL_STORAGE_STOP_UNIT" in script
    assert '_storage_unit_is_active "$unit"' in script
    assert 'if ! _user_systemctl stop "$unit"' in script
    assert '[ "$status" = "3" ] || [ "$status" = "4" ]' in script
    assert "transport failures must not be misclassified" in script

    prepare = script[
        script.index("_prepare_storage_for_release() {") : script.index(
            "_restore_storage_snapshot() {", script.index("_prepare_storage_for_release() {")
        )
    ]
    assert prepare.index("_storage_release_action needs") < prepare.index(
        'if [ "$EIMEMORY_STORAGE_MIGRATION" != "1" ]'
    )
    assert prepare.index('if [ "$storage_needed" != "1" ]; then') < prepare.index(
        "_stop_storage_writers"
    )
    assert prepare.index("_storage_release_action needs") < prepare.index(
        "_storage_release_action snapshot"
    )
    assert "_storage_release_action verify" not in prepare
    snapshot_action = prepare.index('_storage_release_action snapshot)"')
    snapshot_identity = prepare.index('STORAGE_SNAPSHOT_MANIFEST_SHA256="', snapshot_action)
    snapshot_ready = prepare.index("STORAGE_SNAPSHOT_READY=1", snapshot_action)
    migrate_action = prepare.index("_storage_release_action migrate", snapshot_action)
    assert snapshot_action < snapshot_identity < snapshot_ready < migrate_action
    no_pending = prepare.index('if [ "$storage_needed" != "1" ]; then')
    assert no_pending < prepare.index("return", no_pending) < prepare.index(
        "_storage_release_action snapshot"
    )
    disabled = prepare.index('if [ "$EIMEMORY_STORAGE_MIGRATION" != "1" ]; then')
    assert 'if [ "$storage_needed" = "1" ]; then' in prepare[disabled:]

    stop = script[
        script.index("_stop_storage_writers() {") : script.index(
            "_restart_storage_writers() {", script.index("_stop_storage_writers() {")
        )
    ]
    assert "storage_writer_stop=failed systemd_unavailable" in stop
    assert 'STORAGE_WRITERS_STOPPED=1\n    return' not in stop

    prune = script[
        script.index("_prune_storage_snapshots() {") : script.index(
            "_maybe_fail_stage() {", script.index("_prune_storage_snapshots() {")
        )
    ]
    assert '[ "$STORAGE_SNAPSHOT_READY" != "1" ]' in prune

    cleanup = script[script.index("cleanup_stage() {") : script.index("trap cleanup_stage EXIT")]
    assert '"$STORAGE_SNAPSHOT_READY" = "1"' in cleanup

    rollback = script[
        script.index("_rollback_current_release() {") : script.index(
            "if [[ ! \"$COMMIT\" =~", script.index("_rollback_current_release() {")
        )
    ]
    assert rollback.index("_stop_storage_writers") < rollback.index("_restore_storage_snapshot")
    assert rollback.index("_restore_storage_snapshot") < rollback.index(
        "_install_current_runtime_metadata"
    )
    assert rollback.index("_install_current_runtime_metadata") < rollback.index(
        "_clear_storage_release_transaction"
    ) < rollback.index("_restart_storage_writers")

    restart_background = script.index("_restart_storage_writers\n", switch)
    acceptance = script.index("_run_post_switch_closure\n", restart_background)
    cleanup_backup = script.index("_cleanup_storage_vacuum_backup\n", acceptance)
    prune_snapshots = script.index("_prune_storage_snapshots\n", cleanup_backup)
    assert switch < restart_background < acceptance < cleanup_backup < prune_snapshots


def test_storage_release_transaction_marker_fails_closed_and_clears_atomically(tmp_path) -> None:
    from deploy.storage_release_transaction import (
        StorageReleaseTransactionError,
        begin_storage_release_transaction,
        clear_storage_release_transaction,
        guard_allows_start,
        load_storage_release_transaction,
        update_storage_release_transaction,
    )

    marker = tmp_path / "state" / "storage-release-transaction.json"
    transaction = begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-1",
        snapshot_dir=tmp_path / "state" / "release-snapshots" / "attempt-1",
        active_writer_units=["eimemory-rpc.service", "eimemory-nightly.timer"],
    )
    assert transaction["phase"] == "writers_captured"
    assert transaction["storage_destructive"] is False
    assert transaction["active_writer_units"] == [
        "eimemory-rpc.service",
        "eimemory-nightly.timer",
    ]
    assert guard_allows_start(marker) is False
    assert not list(marker.parent.glob(f".{marker.name}.*"))

    updated = update_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-1",
        phase="storage_destructive",
        snapshot_manifest_sha256="a" * 64,
        storage_destructive=True,
    )
    assert updated["storage_destructive"] is True
    assert updated["snapshot_manifest_sha256"] == "a" * 64

    clear_storage_release_transaction(marker, expected_attempt_id="attempt-1")
    assert guard_allows_start(marker) is True
    assert not marker.exists()

    marker.write_text("{broken", encoding="utf-8")
    assert guard_allows_start(marker) is False
    with pytest.raises(StorageReleaseTransactionError, match="invalid"):
        load_storage_release_transaction(marker)


def test_storage_release_guard_blocks_fresh_process_start_on_valid_or_corrupt_marker(
    tmp_path,
) -> None:
    from deploy.storage_release_transaction import begin_storage_release_transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    helper = Path("deploy/storage_release_transaction.py").resolve()
    begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-guard",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=["eimemory-rpc.service"],
    )

    blocked = subprocess.run(
        [sys.executable, str(helper), "guard", "--marker", str(marker)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 75
    assert "blocked" in blocked.stderr

    marker.write_text("[]", encoding="utf-8")
    corrupt = subprocess.run(
        [sys.executable, str(helper), "guard", "--marker", str(marker)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert corrupt.returncode == 75

    marker.unlink()
    allowed = subprocess.run(
        [sys.executable, str(helper), "guard", "--marker", str(marker)],
        check=False,
    )
    assert allowed.returncode == 0


def test_storage_release_reconcile_classifies_prior_and_candidate_paths(tmp_path) -> None:
    from deploy.storage_release_transaction import (
        StorageReleaseTransactionError,
        begin_storage_release_transaction,
        classify_storage_release_reconcile,
        update_storage_release_transaction,
    )

    marker = tmp_path / "storage-release-transaction.json"
    transaction = begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "current",
        attempt_id="attempt-2",
        snapshot_dir=tmp_path / "snapshot",
        active_writer_units=["eimemory-rpc.service"],
    )
    assert (
        classify_storage_release_reconcile(
            transaction,
            current_commit="1" * 40,
            migrations_complete=False,
        )
        == "clear_prior"
    )
    transaction = update_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-2",
        phase="storage_destructive",
        snapshot_manifest_sha256="b" * 64,
        storage_destructive=True,
    )
    assert (
        classify_storage_release_reconcile(
            transaction,
            current_commit="1" * 40,
            migrations_complete=False,
        )
        == "restore_prior"
    )
    assert (
        classify_storage_release_reconcile(
            transaction,
            current_commit="2" * 40,
            migrations_complete=True,
        )
        == "finalize_candidate"
    )
    with pytest.raises(StorageReleaseTransactionError, match="inconsistent"):
        classify_storage_release_reconcile(
            transaction,
            current_commit="3" * 40,
            migrations_complete=True,
        )
    transaction = update_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-2",
        phase="rollback_started",
        storage_destructive=True,
    )
    assert (
        classify_storage_release_reconcile(
            transaction,
            current_commit="2" * 40,
            migrations_complete=True,
        )
        == "resume_rollback"
    )


def test_installer_installs_stable_guard_before_marker_and_delays_candidate_metadata() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    main_start = script.index('STORAGE_TRANSACTION_ACTIVE=0\n\n_ensure_runtime_dir')
    guard_install = script.index("_install_storage_release_guards\n", main_start)
    prepare_call = script.rindex("_prepare_storage_for_release\n")
    switch = script.index('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"', prepare_call)
    metadata_call = script.index("_install_candidate_runtime_metadata\n", switch)
    marker_clear = script.index("_clear_storage_release_transaction\n", metadata_call)
    prepare_body = script[
        script.index("_prepare_storage_for_release() {") : script.index(
            "_restore_storage_snapshot() {"
        )
    ]
    marker_begin = prepare_body.index("_begin_storage_release_transaction\n")
    migrate = prepare_body.index("_storage_release_action migrate")
    metadata_body = script[
        script.index("_install_candidate_runtime_metadata() {") : script.index(
            "_verify_release_health() {"
        )
    ]

    assert guard_install < prepare_call < switch < metadata_call < marker_clear
    assert marker_begin < migrate
    assert "_install_openclaw_loop_compat_script" in metadata_body
    assert "_fsync_install_root" in script[switch:metadata_call]
    assert "STORAGE_TRANSACTION_MARKER" in script
    assert "STORAGE_TRANSACTION_HELPER" in script

    guard = Path("deploy/systemd/eimemory-storage-release-guard.conf").read_text(
        encoding="utf-8"
    )
    assert "ExecCondition=" in guard
    assert "/opt/eimemory/current" not in guard
    assert "@EIMEMORY_STORAGE_TRANSACTION_HELPER@" in guard


def test_installer_rollback_is_guarded_until_prior_storage_link_and_metadata_match() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    rollback = script[
        script.index("_rollback_current_release() {") : script.index(
            'if [[ ! "$COMMIT" =~', script.index("_rollback_current_release() {")
        )
    ]
    marker_begin = rollback.index("_begin_storage_release_transaction")
    marker_rollback = rollback.index("rollback_started", marker_begin)
    stop = rollback.index("_stop_storage_writers", marker_rollback)
    restore = rollback.index("_restore_storage_snapshot", stop)
    metadata = rollback.index("_install_current_runtime_metadata", restore)
    marker_clear = rollback.index("_clear_storage_release_transaction", metadata)
    restart = rollback.index("_restart_storage_writers", marker_clear)

    assert marker_begin < marker_rollback < stop < restore < metadata < marker_clear < restart
    assert "_refresh_current_runtime_metadata" not in rollback
