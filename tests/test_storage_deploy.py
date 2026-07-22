from __future__ import annotations

import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
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


def _bash_executable() -> str:
    if os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("bash")
    if found is None:
        pytest.skip("bash is unavailable")
    return found


def _run_legacy_rpc_retire_harness(
    tmp_path: Path,
    *,
    stop_status: int,
    disable_status: int,
) -> subprocess.CompletedProcess[str]:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    function_body = installer.split("_retire_system_rpc_unit() {", 1)[1].split("\n}", 1)[0]
    unit = tmp_path / "systemd" / "eimemory-rpc.service"
    unit.parent.mkdir()
    unit.write_text("[Service]\nExecStart=/legacy-rpc\n", encoding="utf-8")
    log = tmp_path / "systemctl.log"
    harness = f"""#!/usr/bin/env bash
set -u
_retire_system_rpc_unit() {{{function_body}
}}
SYSTEM_RPC_UNIT_PATH="$1"
SYSTEM_ACTIVE=1
STOP_STATUS="$2"
DISABLE_STATUS="$3"
LOG_PATH="$4"
id() {{
  if [ "${{1:-}}" = "-u" ]; then printf '0\\n'; else command id "$@"; fi
}}
systemctl() {{
  printf '%s\\n' "$*" >>"$LOG_PATH"
  case "$1" in
    is-active) [ "$SYSTEM_ACTIVE" = "1" ] && return 0 || return 3 ;;
    stop)
      if [ "$STOP_STATUS" = "0" ]; then SYSTEM_ACTIVE=0; return 0; fi
      return "$STOP_STATUS"
      ;;
    disable) return "$DISABLE_STATUS" ;;
    daemon-reload) return 0 ;;
  esac
  return 2
}}
_retire_system_rpc_unit
"""
    return subprocess.run(
        [_bash_executable(), "-c", harness, "retire-test", str(unit), str(stop_status), str(disable_status), str(log)],
        check=False,
        capture_output=True,
        text=True,
    )


def _storage_lock_harness() -> str:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    body = installer.split("_acquire_storage_deploy_lock() {", 1)[1].split("\n}", 1)[0]
    return f"""#!/usr/bin/env bash
set -u
_acquire_storage_deploy_lock() {{{body}
}}
STORAGE_DEPLOY_LOCK_PATH="$1"
_acquire_storage_deploy_lock
printf 'ready\\n'
IFS= read -r _release_lock
"""


def _run_rollback_control_harness(
    tmp_path: Path,
    *,
    failure_point: str,
    transaction_active: bool,
    current_switched: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    body = installer.split("_rollback_current_release() {", 1)[1].split("\n}", 1)[0]
    trace = tmp_path / "rollback.trace"
    current = tmp_path / "install" / "current"
    current.parent.mkdir(parents=True)
    previous = tmp_path / "install" / "releases" / ("1" * 40)
    previous.mkdir(parents=True)
    candidate = tmp_path / "install" / "releases" / ("2" * 40)
    candidate.mkdir()
    harness = f"""#!/usr/bin/env bash
set -u
_rollback_current_release() {{{body}
}}
FAILURE_POINT="$1"
TRACE_PATH="$2"
CURRENT_LINK="$3"
PREVIOUS_CURRENT="$4"
PREVIOUS_COMMIT="{'1' * 40}"
RELEASE_DIR="$5"
STORAGE_TRANSACTION_ACTIVE="{1 if transaction_active else 0}"
STORAGE_SNAPSHOT_READY=1
STORAGE_VACUUM_BACKUP=""
STORAGE_WRITERS_STOPPED=0
CURRENT_SWITCHED="{1 if current_switched else 0}"
USER_SYSTEMD_ENABLE_SERVICE=1
EIMEMORY_DEPLOY_FAIL_ROLLBACK_STAGE=""
trace() {{ printf '%s\n' "$1" >>"$TRACE_PATH"; }}
systemctl() {{ return 0; }}
_capture_storage_writers() {{ trace capture; [ "$FAILURE_POINT" != capture ]; }}
_begin_storage_release_transaction() {{
  trace begin
  if [ "$FAILURE_POINT" = begin ]; then return 42; fi
  STORAGE_TRANSACTION_ACTIVE=1
}}
_update_storage_release_transaction() {{
  trace "update:$1"
  [ "$FAILURE_POINT" != "update_$1" ]
}}
_stop_storage_writers() {{ trace stop; return 0; }}
_fsync_install_root() {{ trace fsync_link; [ "$FAILURE_POINT" != fsync_link ]; }}
_restore_storage_snapshot() {{ trace restore; return 0; }}
_cleanup_storage_vacuum_backup() {{ trace cleanup_vacuum; return 0; }}
_refresh_openclaw_gateway_metadata() {{ trace gateway_metadata; return 0; }}
_install_current_runtime_metadata() {{ trace runtime_metadata; return 0; }}
_install_openclaw_loop_compat_script() {{ trace compat; return 0; }}
_refresh_openclaw_plugin_registry() {{ trace registry; return 0; }}
_clear_storage_release_transaction() {{ trace clear; [ "$FAILURE_POINT" != clear ]; }}
_restart_storage_writers() {{ trace restart; return 0; }}
_inspect_openclaw_plugin_runtime() {{ trace inspect; return 0; }}
_verify_release_health() {{ trace health; return 0; }}
set +e
_rollback_current_release
exit $?
"""
    result = subprocess.run(
        [
            _bash_executable(),
            "-c",
            harness,
            "rollback-control",
            failure_point,
            str(trace),
            str(current),
            str(previous),
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    events = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    return result, events


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


def test_vacuum_cleanup_rejects_reparse_journal_before_reading_it(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    db_path.parent.mkdir()
    db_path.write_bytes(b"sqlite")
    backup = db_path.parent / f".{db_path.name}.pre-vacuum-{'a' * 32}.bak"
    backup.write_bytes(b"backup")
    journal = db_path.parent / ".storage-vacuum-journal.json"
    journal.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(storage_release, "_is_reparse", lambda path: Path(path) == journal)

    with pytest.raises(maintenance.StorageMaintenanceError, match="journal is unsafe"):
        storage_release._cleanup_vacuum_backup(db_path, str(backup))


def test_vacuum_cleanup_requires_journal_binding_for_existing_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    db_path.parent.mkdir()
    db_path.write_bytes(b"sqlite")
    backup = db_path.parent / f".{db_path.name}.pre-vacuum-{'a' * 32}.bak"
    backup.write_bytes(b"must remain")

    with pytest.raises(maintenance.StorageMaintenanceError, match="journal binding"):
        storage_release._cleanup_vacuum_backup(db_path, str(backup))

    assert backup.read_bytes() == b"must remain"


def test_vacuum_cleanup_is_idempotent_when_backup_and_journal_are_absent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    db_path.parent.mkdir()
    db_path.write_bytes(b"sqlite")
    backup = db_path.parent / f".{db_path.name}.pre-vacuum-{'a' * 32}.bak"

    result = storage_release._cleanup_vacuum_backup(db_path, str(backup))

    assert result == {"schema": "storage_vacuum_cleanup.v1", "ok": True, "removed": False}


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
    assert prepare.index('if [ "$protected_write_needed" != "1" ]; then') < prepare.index(
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
    no_pending = prepare.index('if [ "$protected_write_needed" != "1" ]; then')
    assert no_pending < prepare.index("return", no_pending) < prepare.index(
        "_storage_release_action snapshot"
    )
    disabled = prepare.index('if [ "$EIMEMORY_STORAGE_MIGRATION" != "1" ]; then')
    assert 'storage_needed=0' in prepare[disabled:]

    stop = script[
        script.index("_stop_storage_writers() {") : script.index(
            "_restart_storage_writers() {", script.index("_stop_storage_writers() {")
        )
    ]
    assert "storage_writer_stop=failed systemd_unavailable" in stop
    assert 'STORAGE_WRITERS_STOPPED=1\n    return' not in stop
    assert "if ! _capture_storage_writers; then" in stop
    assert "storage_writer_capture=failed" in stop

    load = script.split("_load_storage_release_transaction() {", 1)[1].split("\n}", 1)[0]
    assert "STORAGE_WRITERS_STOPPED=0" in load
    assert "STORAGE_WRITERS_STOPPED=1" not in load
    assert "STORAGE_WRITERS_CAPTURED=0" in load
    assert "STORAGE_WRITERS_RELOADED=1" in load

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


@pytest.mark.parametrize(
    ("stop_status", "disable_status", "failed_operation"),
    ((1, 0, "stop"), (0, 1, "disable")),
)
def test_legacy_system_rpc_retirement_fails_closed_on_systemctl_error(
    tmp_path: Path,
    stop_status: int,
    disable_status: int,
    failed_operation: str,
) -> None:
    result = _run_legacy_rpc_retire_harness(
        tmp_path,
        stop_status=stop_status,
        disable_status=disable_status,
    )

    assert result.returncode != 0
    assert f"legacy_system_rpc={failed_operation}_failed" in result.stderr
    assert (tmp_path / "systemd" / "eimemory-rpc.service").is_file()


def test_active_legacy_system_rpc_is_stopped_confirmed_disabled_and_retired(tmp_path: Path) -> None:
    result = _run_legacy_rpc_retire_harness(tmp_path, stop_status=0, disable_status=0)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert calls[:4] == [
        "is-active --quiet eimemory-rpc.service",
        "stop eimemory-rpc.service",
        "is-active --quiet eimemory-rpc.service",
        "disable eimemory-rpc.service",
    ]
    assert not (tmp_path / "systemd" / "eimemory-rpc.service").exists()
    assert (
        tmp_path / "systemd" / "eimemory-rpc.service.retired-by-eimemory-user-systemd"
    ).is_file()


def test_legacy_system_rpc_is_quiesced_fail_closed_before_marker_and_user_writers() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    prepare = script.split("_prepare_storage_for_release() {", 1)[1].split(
        "\n}", 1
    )[0]

    assert "protected_bootstrap_requires_database" in prepare
    marker = prepare.index("_begin_storage_release_transaction")
    legacy = prepare.index("_retire_system_rpc_unit")
    user_writers = prepare.index("_stop_storage_writers")
    assert legacy < marker < user_writers
    assert "if ! _retire_system_rpc_unit; then" in prepare

    main = script[script.rindex("_prepare_storage_for_release\n") - 200 :]
    assert main.index("_prepare_storage_for_release") < main.index("_retire_system_rpc_unit")


def test_production_recall_bootstrap_runs_only_after_snapshot_protects_writes() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    prepare = script.split("_prepare_storage_for_release() {", 1)[1].split(
        "\n}", 1
    )[0]

    marker = prepare.index("_begin_storage_release_transaction")
    stop = prepare.index("_stop_storage_writers", marker)
    snapshot_ready = prepare.index("STORAGE_SNAPSHOT_READY=1", stop)
    destructive = prepare.index("storage_destructive 1", snapshot_ready)
    bootstrap = prepare.index("_run_pre_switch_production_recall_bootstrap", destructive)
    migrate = prepare.index("_storage_release_action migrate", bootstrap)
    assert marker < stop < snapshot_ready < destructive < bootstrap < migrate

    main = script[script.rindex("_prepare_storage_for_release\n") - 300 :]
    assert "_run_pre_switch_production_recall_bootstrap\n" not in main.split(
        "_prepare_storage_for_release\n", 1
    )[0]
    cleanup = script.split("cleanup_stage() {", 1)[1].split("\n}", 1)[0]
    rollback_condition = cleanup[: cleanup.index("_rollback_current_release")]
    assert '"$STORAGE_SNAPSHOT_READY" = "1"' in rollback_condition
    assert "STORAGE_MIGRATION_REQUIRED" not in rollback_condition
    switch = script.rindex('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"')
    metadata = script.index("_install_candidate_runtime_metadata", switch)
    clear = script.index("_clear_storage_release_transaction", metadata)
    assert switch < metadata < clear

def test_storage_release_transaction_marker_fails_closed_and_clears_atomically(tmp_path) -> None:
    from deploy.storage_release_transaction import (
        StorageReleaseTransactionError,
        begin_storage_release_transaction,
        clear_storage_release_transaction,
        guard_allows_start,
        load_storage_release_transaction,
        storage_release_abort_is_safe,
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
    assert storage_release_abort_is_safe(transaction) is True
    leftovers = [
        path
        for path in marker.parent.glob(f".{marker.name}.*")
        if path.name != f".{marker.name}.lock"
    ]
    assert not leftovers

    updated = update_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-1",
        phase="storage_destructive",
        snapshot_manifest_sha256="a" * 64,
        storage_destructive=True,
    )
    assert updated["storage_destructive"] is True
    assert updated["snapshot_manifest_sha256"] == "a" * 64
    assert storage_release_abort_is_safe(updated) is False

    clear_storage_release_transaction(marker, expected_attempt_id="attempt-1")
    assert guard_allows_start(marker) is True
    assert not marker.exists()

    marker.write_text("{broken", encoding="utf-8")
    assert guard_allows_start(marker) is False
    with pytest.raises(StorageReleaseTransactionError, match="invalid"):
        load_storage_release_transaction(marker)


@pytest.mark.parametrize("failure_call", [1, 2])
def test_storage_release_clear_fsync_failure_preserves_a_blocking_credential(
    tmp_path: Path, monkeypatch, failure_call: int
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-clear-fsync",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    calls = 0

    def fail_selected_parent_sync(_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError(errno.EIO, "injected parent fsync failure")

    monkeypatch.setattr(transaction, "_fsync_directory", fail_selected_parent_sync)

    with pytest.raises(transaction.StorageReleaseTransactionError):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id="attempt-clear-fsync",
        )

    tombstone = marker.with_name(f".{marker.name}.clearing")
    recovery = marker.with_name(f".{marker.name}.recovery")
    assert any(
        path.exists() or path.is_symlink() for path in (marker, tombstone, recovery)
    )
    assert transaction.guard_allows_start(marker) is False


def test_storage_release_clear_recovery_precreate_enospc_keeps_tombstone(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    recovery = marker.with_name(f".{marker.name}.recovery")
    tombstone = marker.with_name(f".{marker.name}.clearing")
    pending_prefix = f".{recovery.name}.pending-"
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-recovery-enospc",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_open = transaction.os.open

    def fail_recovery_create(path, flags, mode=0o777, **kwargs):
        if Path(str(path)).name.startswith(pending_prefix) and flags & os.O_CREAT:
            raise OSError(errno.ENOSPC, "injected recovery create failure")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(transaction.os, "open", fail_recovery_create)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="recovery blocker creation failed",
    ):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id="attempt-recovery-enospc",
        )

    assert tombstone.exists()
    assert not recovery.exists()
    assert transaction.guard_allows_start(marker) is False


@pytest.mark.parametrize("error_code", [errno.EIO, errno.ENOSPC])
def test_storage_release_clear_partial_recovery_write_never_publishes_corrupt_blocker(
    tmp_path: Path, monkeypatch, error_code: int
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    recovery = marker.with_name(f".{marker.name}.recovery")
    tombstone = marker.with_name(f".{marker.name}.clearing")
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id=f"attempt-partial-{error_code}",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_write = transaction.os.write
    writes = 0

    def fail_after_partial_write(descriptor: int, payload) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, payload[:8])
        raise OSError(error_code, "injected recovery write failure")

    monkeypatch.setattr(transaction.os, "write", fail_after_partial_write)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="recovery blocker creation failed",
    ):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id=f"attempt-partial-{error_code}",
        )

    assert tombstone.exists()
    assert not recovery.exists()
    assert not list(marker.parent.glob(f".{recovery.name}.pending-*"))
    assert transaction.guard_allows_start(marker) is False

    monkeypatch.setattr(transaction.os, "write", real_write)
    transaction.clear_storage_release_transaction(
        marker,
        expected_attempt_id=f"attempt-partial-{error_code}",
    )
    assert transaction.guard_allows_start(marker) is True


@pytest.mark.parametrize("failure", ["cleanup", "publish"])
def test_storage_release_clear_unpublished_temp_failure_does_not_poison_retry(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    recovery = marker.with_name(f".{marker.name}.recovery")
    tombstone = marker.with_name(f".{marker.name}.clearing")
    pending_prefix = f".{recovery.name}.pending-"
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id=f"attempt-{failure}-failure",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_write = transaction.os.write
    real_unlink = transaction.os.unlink
    real_replace = transaction.os.replace
    writes = 0

    def fail_after_partial_write(descriptor: int, payload) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, payload[:8])
        raise OSError(errno.EIO, "injected recovery write failure")

    def fail_pending_cleanup(path, **kwargs) -> None:
        if Path(str(path)).name.startswith(pending_prefix):
            raise OSError(errno.EACCES, "injected pending cleanup failure")
        real_unlink(path, **kwargs)

    def fail_recovery_publish(source, destination, **kwargs) -> None:
        if Path(str(destination)).name == recovery.name:
            raise OSError(errno.EIO, "injected recovery publish failure")
        real_replace(source, destination, **kwargs)

    if failure == "cleanup":
        monkeypatch.setattr(transaction.os, "write", fail_after_partial_write)
        monkeypatch.setattr(transaction.os, "unlink", fail_pending_cleanup)
    else:
        monkeypatch.setattr(transaction.os, "replace", fail_recovery_publish)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="recovery blocker creation failed",
    ):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id=f"attempt-{failure}-failure",
        )

    pending = list(marker.parent.glob(f"{pending_prefix}*"))
    assert tombstone.exists()
    assert not recovery.exists()
    assert len(pending) == (1 if failure == "cleanup" else 0)
    assert transaction.guard_allows_start(marker) is False

    monkeypatch.setattr(transaction.os, "write", real_write)
    monkeypatch.setattr(transaction.os, "unlink", real_unlink)
    monkeypatch.setattr(transaction.os, "replace", real_replace)
    transaction.clear_storage_release_transaction(
        marker,
        expected_attempt_id=f"attempt-{failure}-failure",
    )
    assert transaction.guard_allows_start(marker) is True


def test_storage_release_clear_uses_precreated_recovery_when_delete_sync_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    recovery = marker.with_name(f".{marker.name}.recovery")
    tombstone = marker.with_name(f".{marker.name}.clearing")
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-precreated-recovery",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_open = transaction.os.open
    real_sync = transaction._sync_marker_parent
    calls = 0
    delete_sync_failed = False

    def fail_tombstone_delete_sync(path: Path, *, parent_fd: int | None) -> None:
        nonlocal calls, delete_sync_failed
        calls += 1
        if calls == 3:
            delete_sync_failed = True
            raise OSError(errno.EIO, "injected tombstone delete fsync failure")
        real_sync(path, parent_fd=parent_fd)

    def reject_late_blocker_creation(path, flags, mode=0o777, **kwargs):
        if (
            delete_sync_failed
            and flags & os.O_CREAT
            and Path(str(path)).name in {marker.name, recovery.name, tombstone.name}
        ):
            raise OSError(errno.ENOSPC, "injected late create failure")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(transaction, "_sync_marker_parent", fail_tombstone_delete_sync)
    monkeypatch.setattr(transaction.os, "open", reject_late_blocker_creation)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="storage release transaction clear is not durable",
    ):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id="attempt-precreated-recovery",
        )

    assert delete_sync_failed is True
    assert recovery.exists()
    assert not tombstone.exists()
    assert transaction.guard_allows_start(marker) is False

    transaction.clear_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-precreated-recovery",
    )
    assert transaction.guard_allows_start(marker) is True


def test_storage_release_clear_rejects_mismatched_tombstone_and_recovery(
    tmp_path: Path,
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    recovery = marker.with_name(f".{marker.name}.recovery")
    tombstone = marker.with_name(f".{marker.name}.clearing")
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-mismatch",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    marker.replace(tombstone)
    recovery_payload = json.loads(tombstone.read_text(encoding="utf-8"))
    recovery_payload["attempt_id"] = "different-attempt"
    recovery.write_text(json.dumps(recovery_payload), encoding="utf-8")

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="recovery blocker does not match tombstone",
    ):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id="attempt-mismatch",
        )
    assert transaction.guard_allows_start(marker) is False


def test_storage_release_clear_cleanup_fsync_failure_is_safe_after_durable_delete(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-cleanup-fsync",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_sync = transaction._sync_marker_parent
    calls = 0

    def fail_cleanup_sync(path: Path, *, parent_fd: int | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError(errno.EIO, "injected recovery cleanup fsync failure")
        real_sync(path, parent_fd=parent_fd)

    monkeypatch.setattr(transaction, "_sync_marker_parent", fail_cleanup_sync)

    transaction.clear_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-cleanup-fsync",
    )
    assert calls == 4
    assert transaction.guard_allows_start(marker) is True


def test_storage_release_guard_waits_for_clear_parent_durability(
    tmp_path: Path, monkeypatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-clear-lock",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_sync = transaction._sync_marker_parent
    entered_second_sync = Event()
    release_second_sync = Event()
    guard_started = Event()
    guard_returned = Event()
    calls = 0

    def delay_second_parent_sync(path: Path, *, parent_fd: int | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            entered_second_sync.set()
            if not release_second_sync.wait(timeout=5):
                raise AssertionError("timed out waiting to release parent fsync")
        real_sync(path, parent_fd=parent_fd)

    monkeypatch.setattr(transaction, "_sync_marker_parent", delay_second_parent_sync)

    def run_guard() -> bool:
        guard_started.set()
        result = transaction.guard_allows_start(marker)
        guard_returned.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        clear_future = executor.submit(
            transaction.clear_storage_release_transaction,
            marker,
            expected_attempt_id="attempt-clear-lock",
        )
        assert entered_second_sync.wait(timeout=5)
        guard_future = executor.submit(run_guard)
        assert guard_started.wait(timeout=5)
        try:
            assert not guard_returned.wait(timeout=0.2)
        finally:
            release_second_sync.set()
        clear_future.result(timeout=5)
        assert guard_future.result(timeout=5) is True


def test_storage_release_clear_recovery_file_fsync_failure_keeps_tombstone(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "storage-release-transaction.json"
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "install" / "current",
        attempt_id="attempt-clear-restore",
        snapshot_dir=tmp_path / "state" / "snapshot",
        active_writer_units=[],
    )
    real_fsync = transaction.os.fsync
    monkeypatch.setattr(
        transaction.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "injected blocker fsync")),
    )

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="recovery blocker creation failed; tombstone remains active",
    ):
        transaction.clear_storage_release_transaction(
            marker,
            expected_attempt_id="attempt-clear-restore",
        )

    tombstone = marker.with_name(f".{marker.name}.clearing")
    recovery = marker.with_name(f".{marker.name}.recovery")
    assert tombstone.exists()
    assert not recovery.exists()
    assert not list(marker.parent.glob(f".{recovery.name}.pending-*"))
    assert transaction.guard_allows_start(marker) is False

    monkeypatch.setattr(transaction.os, "fsync", real_fsync)
    transaction.clear_storage_release_transaction(
        marker,
        expected_attempt_id="attempt-clear-restore",
    )
    assert transaction.guard_allows_start(marker) is True


def test_storage_release_transaction_rejects_symlinked_marker_and_lock(tmp_path) -> None:
    from deploy.storage_release_transaction import (
        StorageReleaseTransactionError,
        begin_storage_release_transaction,
        load_storage_release_transaction,
    )

    marker = tmp_path / "state" / "storage-release-transaction.json"
    marker.parent.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    try:
        marker.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks require additional Windows privileges")

    with pytest.raises(StorageReleaseTransactionError, match="invalid"):
        load_storage_release_transaction(marker)
    marker.unlink()
    lock = marker.with_name(f".{marker.name}.lock")
    lock.unlink()
    lock.symlink_to(outside)
    with pytest.raises(StorageReleaseTransactionError, match="lock is a symlink"):
        begin_storage_release_transaction(
            marker,
            prior_commit="1" * 40,
            candidate_commit="2" * 40,
            current_link=tmp_path / "install" / "current",
            attempt_id="attempt-1",
            snapshot_dir=tmp_path / "state" / "snapshots" / "attempt-1",
            active_writer_units=[],
        )
    assert outside.read_text(encoding="utf-8") == "{}\n"


def test_installer_clears_only_pre_destructive_marker_before_restarting_writers() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    cleanup = script.split("cleanup_stage() {", 1)[1].split("\n}", 1)[0]
    prepare = script.split("_prepare_storage_for_release() {", 1)[1].split("\n}", 1)[0]

    safe_check = cleanup.index("abort-safe")
    clear = cleanup.index("_clear_storage_release_transaction", safe_check)
    restart = cleanup.index("_restart_storage_writers", clear)
    assert safe_check < clear < restart
    assert "pre_destructive_abort_failed_closed" in cleanup

    stopped = prepare.index("_maybe_fail_stage storage_writer_stop")
    preflight = prepare.index("_maybe_fail_stage storage_preflight")
    snapshot = prepare.index('_storage_release_action snapshot)"')
    parse = prepare.index('STORAGE_SNAPSHOT_MANIFEST_SHA256="', snapshot)
    ready = prepare.index("STORAGE_SNAPSHOT_READY=1", parse)
    destructive = prepare.index("storage_destructive 1", ready)
    assert stopped < preflight < snapshot < parse < ready < destructive


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
        expected_current_commit_for_reconcile,
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
    assert expected_current_commit_for_reconcile(transaction) == "1" * 40
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
    assert expected_current_commit_for_reconcile(transaction) == "1" * 40
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
    assert expected_current_commit_for_reconcile(transaction) == "2" * 40
    assert (
        classify_storage_release_reconcile(
            transaction,
            current_commit="2" * 40,
            migrations_complete=True,
        )
        == "resume_rollback"
    )


@pytest.mark.parametrize(
    ("phase", "expected"),
    (
        ("writers_stopped", "prior"),
        ("snapshot_ready", "prior"),
        ("storage_destructive", "prior"),
        ("storage_migrated", "prior"),
        ("vacuum_complete", "prior"),
        ("current_switched", "candidate"),
        ("metadata_ready", "candidate"),
        ("rollback_started", "candidate"),
        ("rollback_link_restored", "prior"),
        ("rollback_storage_restored", "prior"),
        ("rollback_metadata_ready", "prior"),
    ),
)
def test_missing_current_reconcile_has_one_phase_bound_release(
    tmp_path: Path, phase: str, expected: str
) -> None:
    from deploy.storage_release_transaction import (
        begin_storage_release_transaction,
        expected_current_commit_for_reconcile,
        update_storage_release_transaction,
    )

    marker = tmp_path / "marker.json"
    transaction = begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=tmp_path / "current",
        attempt_id=f"missing-{phase}",
        snapshot_dir=tmp_path / "snapshot",
        active_writer_units=[],
    )
    kwargs: dict[str, object] = {}
    if phase in {
        "storage_destructive",
        "storage_migrated",
        "vacuum_complete",
        "current_switched",
        "metadata_ready",
        "rollback_started",
        "rollback_link_restored",
        "rollback_storage_restored",
        "rollback_metadata_ready",
    }:
        kwargs = {"snapshot_manifest_sha256": "c" * 64, "storage_destructive": True}
    transaction = update_storage_release_transaction(
        marker,
        expected_attempt_id=f"missing-{phase}",
        phase=phase,
        **kwargs,
    )

    expected_commit = "1" * 40 if expected == "prior" else "2" * 40
    assert expected_current_commit_for_reconcile(transaction) == expected_commit


def test_installer_rebuilds_only_a_truly_missing_current_from_marker_phase() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    reconcile = script.split("_reconcile_interrupted_storage_release() {", 1)[1].split(
        "\n}", 1
    )[0]

    stop = reconcile.index("_stop_storage_writers")
    missing = reconcile.index('if [ ! -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]')
    expected = reconcile.index("expected-current", missing)
    recreate = reconcile.index('ln -sfn "$expected_release" "$CURRENT_LINK.next"', expected)
    assert stop < missing < expected < recreate
    assert "current_dangling_or_ambiguous" in reconcile


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
    marker_begin = prepare_body.index("_begin_storage_release_transaction")
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
    guard_body = script.split("_install_storage_release_guards() {", 1)[1].split("\n}", 1)[0]
    assert '"$SYSTEM_RPC_DROPIN_DIR/05-eimemory-storage-release-guard.conf"' in guard_body
    assert '--root "$SYSTEM_SYSTEMD_DIR" --owner-uid 0' in guard_body
    assert guard_body.index("SYSTEM_RPC_DROPIN_DIR") < guard_body.index(
        "_user_systemctl daemon-reload"
    )

    guard = Path("deploy/systemd/eimemory-storage-release-guard.conf").read_text(
        encoding="utf-8"
    )
    assert "ExecCondition=" in guard
    assert "/opt/eimemory/current" not in guard
    assert "@EIMEMORY_STORAGE_TRANSACTION_HELPER@" in guard


def test_durable_guard_sync_fsyncs_file_and_every_parent_and_propagates_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    boundary = tmp_path / "install"
    libexec = boundary / "libexec"
    libexec.mkdir(parents=True)
    helper = libexec / "storage-release-transaction.py"
    helper.write_text("helper", encoding="utf-8")
    opened: dict[int, Path] = {}
    synced: list[Path] = []
    next_fd = iter(range(100, 200))

    def metadata(path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=(0o100600 if path == helper else 0o040700),
            st_dev=1,
            st_ino=abs(hash(str(path))),
        )

    def fake_open(path, _flags, _mode=0o777, *, dir_fd=None):
        descriptor = next(next_fd)
        opened[descriptor] = (
            Path(path) if dir_fd is None else opened[int(dir_fd)] / str(path)
        )
        return descriptor

    def fake_stat(path, *, dir_fd=None, follow_symlinks=True):
        del follow_symlinks
        resolved = Path(path) if dir_fd is None else opened[int(dir_fd)] / str(path)
        return metadata(resolved)

    monkeypatch.setattr(transaction.os, "open", fake_open)
    monkeypatch.setattr(transaction.os, "close", lambda _fd: None)
    monkeypatch.setattr(transaction.os, "stat", fake_stat)
    monkeypatch.setattr(transaction.os, "fsync", lambda fd: synced.append(opened[fd]))
    monkeypatch.setattr(transaction.os, "fstat", lambda fd: metadata(opened[fd]))

    transaction._durably_sync_path_posix(helper, boundary=boundary)

    assert synced == [helper, libexec, boundary]

    def fail_libexec(fd):
        if opened[fd] == libexec:
            raise OSError("injected directory fsync failure")

    monkeypatch.setattr(transaction.os, "fsync", fail_libexec)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        transaction._durably_sync_path_posix(helper, boundary=boundary)


def test_durable_sync_and_marker_use_fd_relative_inode_validation() -> None:
    source = Path("deploy/storage_release_transaction.py").read_text(encoding="utf-8")

    assert "dir_fd=parent_fd" in source
    assert "st_dev" in source and "st_ino" in source
    assert "durable sync entry changed during fsync" in source
    assert "storage release transaction lock changed while held" in source


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat race behavior is required")
def test_durable_sync_rejects_target_inode_replacement_after_open(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    boundary = tmp_path / "install"
    helper = boundary / "libexec" / "storage-release-transaction.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("trusted", encoding="utf-8")
    replacement = boundary / "replacement"
    replacement.write_text("replacement", encoding="utf-8")
    real_fsync = transaction.os.fsync
    replaced = False

    def replace_after_file_open(descriptor: int) -> None:
        nonlocal replaced
        if not replaced and stat.S_ISREG(transaction.os.fstat(descriptor).st_mode):
            replaced = True
            transaction.os.replace(replacement, helper)
        real_fsync(descriptor)

    monkeypatch.setattr(transaction.os, "fsync", replace_after_file_open)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="durable sync entry changed during fsync",
    ):
        transaction._durably_sync_path_posix(helper, boundary=boundary)


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat race behavior is required")
def test_durable_sync_rejects_ancestor_inode_replacement_after_open(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    boundary = tmp_path / "install"
    libexec = boundary / "libexec"
    helper = libexec / "storage-release-transaction.py"
    libexec.mkdir(parents=True)
    helper.write_text("trusted", encoding="utf-8")
    real_fsync = transaction.os.fsync
    replaced = False

    def replace_after_file_sync(descriptor: int) -> None:
        nonlocal replaced
        real_fsync(descriptor)
        if not replaced and stat.S_ISREG(transaction.os.fstat(descriptor).st_mode):
            replaced = True
            libexec.replace(boundary / "libexec.old")
            libexec.mkdir()

    monkeypatch.setattr(transaction.os, "fsync", replace_after_file_sync)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="durable sync entry changed during fsync",
    ):
        transaction._durably_sync_path_posix(helper, boundary=boundary)


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat race behavior is required")
def test_marker_lock_rejects_inode_replacement_before_entering_critical_section(
    tmp_path: Path, monkeypatch
) -> None:
    import fcntl
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "transaction.json"
    marker.parent.mkdir()
    lock = marker.with_name(f".{marker.name}.lock")
    real_flock = fcntl.flock
    replaced = False

    def replace_before_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        if operation & fcntl.LOCK_EX and not replaced:
            replaced = True
            lock.replace(lock.with_suffix(".old"))
            lock.write_text("replacement", encoding="utf-8")
        real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", replace_before_lock)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="storage release transaction lock changed while held",
    ):
        transaction.begin_storage_release_transaction(
            marker,
            prior_commit="1" * 40,
            candidate_commit="2" * 40,
            current_link=tmp_path / "install" / "current",
            attempt_id="attempt-1",
            snapshot_dir=tmp_path / "snapshot",
            active_writer_units=[],
        )
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat race behavior is required")
def test_marker_lock_rejects_ancestor_replacement_before_critical_section(
    tmp_path: Path, monkeypatch
) -> None:
    import fcntl
    from deploy import storage_release_transaction as transaction

    state = tmp_path / "state"
    marker = state / "transaction.json"
    state.mkdir()
    real_flock = fcntl.flock
    replaced = False

    def replace_parent_before_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        if operation & fcntl.LOCK_EX and not replaced:
            replaced = True
            state.replace(tmp_path / "state.old")
            state.mkdir()
        real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", replace_parent_before_lock)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="storage release transaction lock parent changed while held",
    ):
        transaction.begin_storage_release_transaction(
            marker,
            prior_commit="1" * 40,
            candidate_commit="2" * 40,
            current_link=tmp_path / "install" / "current",
            attempt_id="attempt-1",
            snapshot_dir=tmp_path / "snapshot",
            active_writer_units=[],
        )
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat race behavior is required")
def test_marker_write_rejects_published_inode_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy import storage_release_transaction as transaction

    marker = tmp_path / "state" / "transaction.json"
    marker.parent.mkdir()
    real_replace = transaction.os.replace

    def replace_published_marker(source, destination, **kwargs) -> None:
        real_replace(source, destination, **kwargs)
        parent_fd = kwargs["dst_dir_fd"]
        transaction.os.unlink(destination, dir_fd=parent_fd)
        replacement = transaction.os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        transaction.os.close(replacement)

    monkeypatch.setattr(transaction.os, "replace", replace_published_marker)

    with pytest.raises(
        transaction.StorageReleaseTransactionError,
        match="storage release transaction marker changed while publishing",
    ):
        transaction.begin_storage_release_transaction(
            marker,
            prior_commit="1" * 40,
            candidate_commit="2" * 40,
            current_link=tmp_path / "install" / "current",
            attempt_id="attempt-1",
            snapshot_dir=tmp_path / "snapshot",
            active_writer_units=[],
        )


def test_installer_durably_syncs_stable_helper_and_guard_dropin_ancestors() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    helper_install = script.split(
        "_install_storage_release_transaction_helper() {", 1
    )[1].split("\n}", 1)[0]
    guards = script.split("_install_storage_release_guards() {", 1)[1].split("\n}", 1)[0]

    assert "mktemp" in helper_install
    assert 'mv -Tf "$staged_helper" "$STORAGE_TRANSACTION_HELPER"' in helper_install
    assert "fsync-path" in helper_install
    assert '--boundary "$INSTALL_ROOT"' in helper_install
    assert guards.count("fsync-path") >= 2
    assert '--boundary "$SYSTEM_SYSTEMD_DIR"' in guards
    assert '--boundary "$SERVICE_HOME"' in guards


def test_installer_acquires_stable_global_lock_before_reconcile_or_begin() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    main = script[script.index('mkdir -p "$INSTALL_ROOT/releases"') :]
    acquire = main.index("_acquire_storage_deploy_lock")
    guard = main.index("_install_storage_release_guards")
    reconcile = main.index("_reconcile_interrupted_storage_release")
    prepare = main.rindex("_prepare_storage_for_release")
    assert acquire < guard < reconcile < prepare
    lock_body = script.split("_acquire_storage_deploy_lock() {", 1)[1].split("\n}", 1)[0]
    assert "flock -n" in lock_body
    assert "storage_deploy_lock=contended" in lock_body
    assert 'if [ -L "$STORAGE_DEPLOY_LOCK_PATH" ]' in lock_body
    assert '/proc/$$/fd/$STORAGE_DEPLOY_LOCK_FD' in lock_body
    assert "storage_deploy_lock=failed inode_changed" in lock_body
    assert "parent_identity_before" in lock_body
    assert "parent_identity_after" in lock_body
    assert "storage_deploy_lock=failed ancestor_inode_changed" in lock_body


def test_installer_misc_storage_boundaries_fail_closed() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert 'OPENCLAW_LOOP_COMPAT_SCRIPT="${OPENCLAW_LOOP_COMPAT_SCRIPT:-' in script
    assert 'OPENCLAW_LOOP_COMPAT_SCRIPT="${OPENCLAW_LOOP_COMPAT_SCRIPT-' not in script
    assert "service_user=failed missing" in script
    assert 'id -u "$SERVICE_USER" 2>/dev/null || id -u' not in script
    assert "uuid.uuid4().hex" in script
    action = script.split("_storage_release_action() {", 1)[1].split("\n}", 1)[0]
    assert 'if [ ! -x "$RELEASE_DIR/.venv/bin/python" ]; then' in action
    assert "candidate_python_unavailable" in action


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is unavailable")
def test_two_installer_processes_cannot_hold_the_storage_deploy_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "storage-release-install.lock"
    harness = _storage_lock_harness()
    first = subprocess.Popen(
        [_bash_executable(), "-c", harness, "lock-one", str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "ready"
        second = subprocess.run(
            [_bash_executable(), "-c", harness, "lock-two", str(lock_path)],
            input="release\n",
            check=False,
            capture_output=True,
            text=True,
        )
        assert second.returncode != 0
        assert "storage_deploy_lock=contended" in second.stderr
    finally:
        if first.stdin is not None:
            first.stdin.write("release\n")
            first.stdin.close()
        first.wait(timeout=10)


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is unavailable")
def test_global_installer_lock_rejects_path_inode_replacement_during_flock(
    tmp_path: Path,
) -> None:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    body = installer.split("_acquire_storage_deploy_lock() {", 1)[1].split("\n}", 1)[0]
    lock = tmp_path / "storage-release-install.lock"
    harness = f"""#!/usr/bin/env bash
set -u
_acquire_storage_deploy_lock() {{{body}
}}
STORAGE_DEPLOY_LOCK_PATH="$1"
flock() {{
  mv "$STORAGE_DEPLOY_LOCK_PATH" "$STORAGE_DEPLOY_LOCK_PATH.old"
  : >"$STORAGE_DEPLOY_LOCK_PATH"
  command flock "$@"
}}
_acquire_storage_deploy_lock
"""

    result = subprocess.run(
        [_bash_executable(), "-c", harness, "lock-race", str(lock)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "storage_deploy_lock=failed inode_changed" in result.stderr


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is unavailable")
def test_global_installer_lock_rejects_parent_inode_replacement_during_flock(
    tmp_path: Path,
) -> None:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    body = installer.split("_acquire_storage_deploy_lock() {", 1)[1].split("\n}", 1)[0]
    lock_parent = tmp_path / "locks"
    lock_parent.mkdir()
    lock = lock_parent / "storage-release-install.lock"
    harness = f"""#!/usr/bin/env bash
set -u
_acquire_storage_deploy_lock() {{{body}
}}
STORAGE_DEPLOY_LOCK_PATH="$1"
flock() {{
  local parent
  parent="$(dirname "$STORAGE_DEPLOY_LOCK_PATH")"
  mv "$parent" "$parent.old"
  mkdir "$parent"
  ln "$parent.old/$(basename "$STORAGE_DEPLOY_LOCK_PATH")" \
    "$STORAGE_DEPLOY_LOCK_PATH"
  command flock "$@"
}}
_acquire_storage_deploy_lock
"""

    result = subprocess.run(
        [_bash_executable(), "-c", harness, "lock-parent-race", str(lock)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "storage_deploy_lock=failed ancestor_inode_changed" in result.stderr


def test_concurrent_marker_begin_has_exactly_one_process_winner(tmp_path: Path) -> None:
    marker = tmp_path / "state" / "transaction.json"
    start = tmp_path / "start"
    code = r"""
from pathlib import Path
import sys
import time
import deploy.storage_release_transaction as transaction

marker = Path(sys.argv[1])
start = Path(sys.argv[2])
real_write = transaction._atomic_write_json

def delayed_write(path, payload, **kwargs):
    time.sleep(0.2)
    real_write(path, payload, **kwargs)

transaction._atomic_write_json = delayed_write
while not start.exists():
    time.sleep(0.005)
try:
    transaction.begin_storage_release_transaction(
        marker,
        prior_commit="1" * 40,
        candidate_commit="2" * 40,
        current_link=marker.parent / "current",
        attempt_id=f"process-{sys.argv[3]}",
        snapshot_dir=marker.parent / "snapshot",
        active_writer_units=[],
    )
except transaction.StorageReleaseTransactionError:
    raise SystemExit(2)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(marker), str(start), str(index)],
            cwd=Path.cwd(),
        )
        for index in range(4)
    ]
    start.write_text("go", encoding="utf-8")
    returncodes = [process.wait(timeout=15) for process in processes]

    assert returncodes.count(0) == 1
    assert returncodes.count(2) == 3


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


@pytest.mark.parametrize(
    ("failure_point", "transaction_active"),
    [("capture", False), ("begin", False), ("update_rollback_started", True)],
)
def test_rollback_never_restores_storage_without_durable_transaction_progress(
    tmp_path: Path, failure_point: str, transaction_active: bool
) -> None:
    result, events = _run_rollback_control_harness(
        tmp_path,
        failure_point=failure_point,
        transaction_active=transaction_active,
    )

    assert result.returncode != 0
    assert "restore" not in events
    assert "clear" not in events
    assert "restart" not in events


@pytest.mark.parametrize(
    ("failure_point", "current_switched", "forbidden"),
    [
        ("fsync_link", True, "restore"),
        ("update_rollback_link_restored", True, "restore"),
        ("update_rollback_storage_restored", False, "gateway_metadata"),
        ("update_rollback_metadata_ready", False, "clear"),
        ("clear", False, "restart"),
    ],
)
def test_rollback_phase_or_clear_failure_stays_fail_closed(
    tmp_path: Path, failure_point: str, current_switched: bool, forbidden: str
) -> None:
    result, events = _run_rollback_control_harness(
        tmp_path,
        failure_point=failure_point,
        transaction_active=True,
        current_switched=current_switched,
    )

    assert result.returncode != 0
    assert forbidden not in events
