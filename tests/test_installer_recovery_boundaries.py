from __future__ import annotations

import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

import pytest

from deploy.storage_release_transaction import begin_storage_release_transaction, update_storage_release_transaction
from eimemory.storage.maintenance import create_consistent_storage_snapshot
from eimemory.storage.sqlite_store import SqliteRecordStore


pytestmark = [pytest.mark.linux_deployment, pytest.mark.skipif(sys.platform != "linux", reason="Linux installer")]
INSTALLER = Path("deploy/install_immutable_release.sh")


def function(name):
    source = INSTALLER.read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}\n"


def shell(tmp_path, body, **values):
    variables = "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + variables + "\n" + body],
                          cwd=Path.cwd(), text=True, capture_output=True, timeout=30)


SYSTEMD = r'''
systemctl() { return 0; }
_user_systemctl() {
  printf '%s\n' "$*" >> "$TRACE"
  local action="$1" unit
  shift
  case "$action" in
    cat) [ "$1" = "$WATCHER" ]; return ;;
    is-enabled) [ -e "$CONTROL/$WATCHER.enabled" ]; return ;;
    is-active) [ -e "$CONTROL/$WATCHER.active" ]; return ;;
    stop) for unit in "$@"; do rm -f "$CONTROL/$unit.active"; done ;;
    enable)
      for unit in "$@"; do
        if [[ "$unit" != -* ]]; then touch "$CONTROL/$unit.enabled"; fi
      done
      if [ "${1:-}" = --now ] && [ "${FAIL_WATCHER:-0}" != 1 ]; then touch "$CONTROL/$WATCHER.active"; fi ;;
    start)
      if [ "${FAIL_WATCHER:-0}" != 1 ]; then
        for unit in "$@"; do touch "$CONTROL/$unit.active"; done
      fi ;;
  esac
}
_openclaw_is_enabled() { return 1; }
_restart_hermes_gateway() { :; }
_verify_effective_runtime_metadata() { :; }
_verify_release_health() { echo health >> "$TRACE"; }
'''


@pytest.mark.parametrize("watcher", ["eimemory-release-closure.path", "eimemory-release-closure.timer"])
@pytest.mark.parametrize("failed", [False, True])
def test_recover_only_restores_and_verifies_closure_watcher(tmp_path, watcher, failed):
    control = tmp_path / "systemd"
    control.mkdir()
    (control / f"{watcher}.enabled").touch()
    (control / f"{watcher}.active").touch()
    trace = tmp_path / "trace"
    source = INSTALLER.read_text()
    recovery = 'if [ "$DEPLOY_MODE" = "--recover-only" ]; then' + source.split(
        'if [ "$DEPLOY_MODE" = "--recover-only" ]; then', 2
    )[-1].split("\nfi", 1)[0] + "\nfi\n"
    body = SYSTEMD + "".join(function(name) for name in (
        "_pause_release_closure_reconcile", "_resume_release_closure_reconcile", "_restart_current_services"
    )) + recovery
    result = shell(tmp_path, body, CONTROL=control, TRACE=trace, WATCHER=watcher,
                   FAIL_WATCHER=int(failed), DEPLOY_MODE="--recover-only", USER_SYSTEMD_ENABLE_SERVICE=1,
                   PREVIOUS_COMMIT="1" * 40, PREVIOUS_CURRENT=tmp_path, REPO_DIR=Path.cwd(),
                   EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE=0)
    if failed:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "storage_release_recovery=verified" not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (control / f"{watcher}.enabled").exists()
        assert (control / f"{watcher}.active").exists()
        events = trace.read_text()
        assert events.index("health") < events.index("is-active")


@pytest.mark.parametrize("boundary", ["fsync", "rename_status"])
@pytest.mark.parametrize("watcher_failed", [False, True])
def test_current_rename_then_fsync_failure_restores_code_data_and_journal(tmp_path, boundary, watcher_failed):
    install = tmp_path / "install"
    prior = install / "releases" / ("1" * 40)
    candidate = install / "releases" / ("2" * 40)
    prior.mkdir(parents=True)
    candidate.mkdir()
    current = install / "current"
    current.symlink_to(prior)
    runtime = tmp_path / "runtime"
    state = runtime / "state"
    state.mkdir(parents=True)
    db = state / "eimemory.sqlite"
    SqliteRecordStore(db).close()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE audit_state(value TEXT)")
        conn.execute("INSERT INTO audit_state VALUES ('prior')")
    snapshot = state / "snapshots" / "switch-fsync"
    report = create_consistent_storage_snapshot(
        db_path=db, segment_root=state / "payload_segments", snapshot_dir=snapshot, offline=True,
        binding={"attempt_id": "switch-fsync", "candidate_commit": "2" * 40}, seal=True,
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit_state SET value='candidate'")
    marker = install / "transaction.json"
    begin_storage_release_transaction(marker, prior_commit="1" * 40, candidate_commit="2" * 40,
        current_link=current, attempt_id="switch-fsync", snapshot_dir=snapshot, active_writer_units=[])
    update_storage_release_transaction(marker, expected_attempt_id="switch-fsync", phase="storage_migrated",
        snapshot_manifest_sha256=report["manifest_sha256"], storage_destructive=True)
    control = tmp_path / "systemd"
    control.mkdir()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"commit": "1" * 40}))
    functions = "".join(function(name) for name in (
        "_rollback_current_release", "_update_storage_release_transaction", "_clear_storage_release_transaction",
        "_restore_storage_snapshot", "_cleanup_storage_vacuum_backup", "_pause_release_closure_reconcile",
        "_resume_release_closure_reconcile", "_restart_current_services", "cleanup_stage",
    ))
    # Exercise the real restore CLI with this checkout on the import path.
    services = "\n".join(f"{name}() {{ :; }}" for name in (
        "_stop_storage_writers", "_restart_storage_writers", "_refresh_openclaw_gateway_metadata",
        "_install_current_runtime_metadata", "_install_openclaw_loop_compat_script",
        "_install_openclaw_bundled_bridge", "_refresh_openclaw_plugin_registry",
        "_verify_hermes_integration", "_start_code_implementation_owner", "_inspect_openclaw_plugin_runtime",
    ))
    source = INSTALLER.read_text()
    switch = source[source.rindex('ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"'):].split(
        "_install_candidate_runtime_metadata", 1
    )[0]
    # One real fsync boundary fails; rollback's directory fsync remains real.
    fsync = function("_fsync_install_root").replace("        os.fsync(descriptor)",
        "        fault = os.path.join(path, 'fail-once')\n"
        "        if os.path.exists(fault):\n"
        "            os.unlink(fault)\n"
        "            raise OSError('injected directory fsync failure')\n"
        "        os.fsync(descriptor)")
    if boundary == "fsync":
        (install / "fail-once").touch()
    body = SYSTEMD + functions + services + "\n" + fsync + r'''
_acquire_candidate_validation_lock() { exec 9>"$INSTALL_ROOT/validation.lock"; flock -x 9; }
_storage_release_action() {
  "$PYTHON_BIN" -m deploy.migrate_storage_release "$1" --root "$EIMEMORY_ROOT" \
    --snapshot-root "$SNAPSHOT_ROOT" --snapshot-dir "$STORAGE_SNAPSHOT_DIR" \
    --candidate-commit "$COMMIT" --attempt-id "$STORAGE_ATTEMPT_ID" \
    --snapshot-manifest-sha256 "$STORAGE_SNAPSHOT_MANIFEST_SHA256"
}
trap cleanup_stage EXIT
'''
    if boundary == "rename_status":
        # The syscall completes, but the process reports failure before the
        # caller can set CURRENT_SWITCHED (also exercises trap reconciliation).
        body += r'''
mv() {
  command mv "$@"
  if [ "${RENAME_FAULT_FIRED:-0}" != 1 ]; then
    RENAME_FAULT_FIRED=1
    echo 'injected rename status failure' >&2
    return 97
  fi
}
'''
    body += switch
    result = shell(tmp_path, body, INSTALL_ROOT=install, CURRENT_LINK=current, PREVIOUS_CURRENT=prior,
        PREVIOUS_COMMIT="1" * 40, COMMIT="2" * 40, RELEASE_DIR=candidate, REPO_DIR=Path.cwd(),
        PYTHON_BIN=sys.executable, EIMEMORY_ROOT=runtime, SNAPSHOT_ROOT=snapshot.parent,
        STORAGE_SNAPSHOT_DIR=snapshot, STORAGE_SNAPSHOT_MANIFEST_SHA256=report["manifest_sha256"],
        STORAGE_TRANSACTION_MARKER=marker, STORAGE_TRANSACTION_HELPER=Path.cwd() / "deploy/storage_release_transaction.py",
        STORAGE_ATTEMPT_ID="switch-fsync", STORAGE_TRANSACTION_ACTIVE=1, STORAGE_SNAPSHOT_READY=1,
        STORAGE_RESTORED=0, STORAGE_VACUUM_BACKUP="", STORAGE_WRITERS_STOPPED=0, CURRENT_SWITCHED=0,
        COMMITTED=0, FINAL_REPLACED=0, OPENCLAW_CONFIG_SWITCHED=0, OPENCLAW_CONFIG_RESTORED=1,
        USER_SYSTEMD_ENABLE_SERVICE=1, EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE=0,
        CONTROL=control, TRACE=tmp_path / "trace", WATCHER="eimemory-release-closure.path",
        FAIL_WATCHER=int(watcher_failed))
    assert result.returncode != 0
    assert f"injected {'directory fsync' if boundary == 'fsync' else 'rename status'} failure" in result.stderr
    assert current.resolve() == prior, result.stdout + result.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM audit_state").fetchone() == ("prior",)
    if watcher_failed:
        assert json.loads(marker.read_text())["phase"] == "rollback_validating"
        assert "rollback_current_release=restored" not in result.stderr
        assert not (control / "eimemory-release-closure.path.active").exists()
    else:
        assert not marker.exists(), result.stdout + result.stderr
        assert (control / "eimemory-release-closure.path.active").exists()
    assert not (state / ".storage-restore-journal.json").exists()
    assert json.loads(receipt.read_text()) == {"commit": "1" * 40}
