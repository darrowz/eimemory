"""A fresh immutable receipt must exist before persistent maintenance resumes."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.version import __version__


INSTALLER = Path("deploy/install_immutable_release.sh")
COMMIT = "a" * 40
OWNER = ScopeRef(tenant_id="receipt-tenant", agent_id="receipt-owner", workspace_id="workspace", user_id="operator")


def _function(name):
    source = INSTALLER.read_text(encoding="utf-8")
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}\n"


def _checker():
    return _function("_verify_current_deployment_receipt").split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def _receipt(*, commit=COMMIT, version=__version__):
    path = "/opt/eimemory/releases/" + commit
    return RecordEnvelope.create(
        kind="promotion_request", title="Isolated receipt fixture", scope=OWNER,
        status="deployed", source="eimemory.deployment_receipt",
        content={
            "report_type": "deployment_receipt", "promotion_target": "code_patch", "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": True},
            "side_effect": {
                "ok": True, "production_applied": True, "deployment_executed": True,
                "verification": {"ok": True},
                "commit": {"commit_sha": commit},
                "release": {"release_path": path, "version": version},
                "deployment": {"ok": True, "release_path": path, "current_link": "/opt/eimemory/current"},
                "post_deploy_health": {"ok": True, "commit": commit, "release_path": path,
                                       "current_link": "/opt/eimemory/current", "url": "http://127.0.0.1:8091/health"},
                "rollback_evidence": {"rollback_not_required": True,
                                      "reason": "initial_immutable_deployment",
                                      "strategy": "initial_immutable_deployment", "commands": []},
            },
        },
    )


def _run_checker(monkeypatch, root, receipt_id):
    from eimemory.governance.release import evidence_contract
    monkeypatch.setattr(evidence_contract, "_runtime_commit", lambda _runtime: COMMIT)
    monkeypatch.setattr("eimemory.config.loader.load_settings", lambda: SimpleNamespace(root=root))
    monkeypatch.setattr(sys, "argv", ["-", COMMIT, receipt_id, OWNER.agent_id, OWNER.workspace_id,
                                    OWNER.user_id, OWNER.tenant_id])
    exec(compile(_checker(), str(INSTALLER), "exec"), {})


def test_native_receipt_recheck_reads_a_separate_committed_store_and_no_runtime(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("eimemory.api.runtime.Runtime.create", lambda **_kw: pytest.fail("no Runtime/provider allowed"))
    store = RuntimeStore(tmp_path)
    try:
        record = store.append(_receipt())
    finally:
        store.close()
    _run_checker(monkeypatch, tmp_path, record.record_id)
    result = json.loads(capsys.readouterr().out)
    assert result == {"current_deployment_receipt_verified": True, "commit": COMMIT, "receipt_id": record.record_id}


@pytest.mark.parametrize("invalid", ["absent", "old_commit", "old_version", "revoked", "wrong_id", "unverified", "wrong_tenant"])
def test_native_receipt_recheck_rejects_missing_or_different_authority(tmp_path, monkeypatch, invalid):
    store = RuntimeStore(tmp_path)
    try:
        record = _receipt(commit="b" * 40 if invalid == "old_commit" else COMMIT,
                          version="0.0.0" if invalid == "old_version" else __version__)
        if invalid == "revoked":
            record.status = "revoked"
        if invalid == "unverified":
            record.content["gate"]["receipt_verified"] = False
        if invalid == "wrong_tenant":
            record.scope = ScopeRef(tenant_id="other-tenant", agent_id=OWNER.agent_id,
                                    workspace_id=OWNER.workspace_id, user_id=OWNER.user_id)
        if invalid != "absent":
            store.append(record)
    finally:
        store.close()
    receipt_id = "rec_other" if invalid == "wrong_id" else record.record_id
    with pytest.raises(SystemExit, match="current_deployment_receipt_unavailable"):
        _run_checker(monkeypatch, tmp_path, receipt_id)


def test_repeated_native_recheck_is_idempotent_and_does_not_reuse_revoked_success(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    try:
        record = store.append(_receipt())
    finally:
        store.close()
    _run_checker(monkeypatch, tmp_path, record.record_id)
    _run_checker(monkeypatch, tmp_path, record.record_id)
    store = RuntimeStore(tmp_path)
    try:
        assert store.get_by_id(record.record_id).to_dict() == record.to_dict()
        assert len(store.list_records(kinds=["promotion_request"], scope=OWNER)) == 1
        record.status = "revoked"
        store.rewrite(record)
    finally:
        store.close()
    with pytest.raises(SystemExit, match="current_deployment_receipt_unavailable"):
        _run_checker(monkeypatch, tmp_path, record.record_id)


def test_candidate_receipt_precedes_every_maintenance_resume_and_durable_commit():
    source = INSTALLER.read_text(encoding="utf-8")
    main = source[source.rindex('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"'):]
    receipt = main.index("_record_deployment_receipt")
    assert main.index('_verify_release_health "$RELEASE_DIR" "$COMMIT"') < receipt
    assert receipt < main.index("_resume_release_closure_reconcile") < main.index("_restart_storage_writers")
    assert receipt < main.index("candidate_validated") < main.index("_clear_storage_release_transaction") < main.index("COMMITTED=1")
    assert receipt < main.index("_start_managed_runtime_timers")
    assert main.index("_run_post_deploy_validation") < main.index("COMMITTED=1")


def _shell(body, **values):
    if sys.platform != "linux":
        pytest.skip("native Linux Bash deployment boundary")
    variables = "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + variables + "\n" + body],
                          text=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("stopped", [0, 1])
@pytest.mark.parametrize("strict", [0, 1])
def test_metadata_never_dispatches_a_timer_before_first_receipt(tmp_path, stopped, strict):
    body = _function("_install_learning_runtime_policy") + r'''
_run_as_service_user() { :; }
_install_as_service_user() { :; }
_user_systemctl() {
  echo "$*" >> "$TRACE"
  case "$1" in start) return 94;; esac
}
_start_learning_runtime_timers() { echo premature_dispatch >> "$TRACE"; return 95; }
LEARNING_TIMER_UNITS=(eimemory-learn-watch.timer eimemory-nightly.timer)
_install_learning_runtime_policy
'''
    result = _shell(body, TRACE=tmp_path / "trace", RELEASE_DIR=tmp_path,
                    USER_SYSTEMD_DIR=tmp_path / "units", STORAGE_WRITERS_STOPPED=stopped,
                    EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE=strict)
    assert result.returncode == 0, result.stderr
    events = (tmp_path / "trace").read_text().splitlines()
    assert "enable eimemory-learn-watch.timer" in events
    assert "enable eimemory-nightly.timer" in events
    assert "premature_dispatch" not in events
    assert not any(event.startswith("start ") for event in events)


@pytest.mark.parametrize("strict", [0, 1])
@pytest.mark.parametrize("failure", [False, True])
def test_actual_main_receipt_failure_cannot_resume_candidate_or_clear_transaction(tmp_path, strict, failure):
    source = INSTALLER.read_text(encoding="utf-8")
    main = source[source.rindex('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"'):]
    # The existing rename/rollback suite covers the switch itself; exercise
    # the actual candidate startup statements immediately after that syscall.
    main = main.split("\n", 1)[1]
    main = main[:main.index('echo "technical_commit_complete=1"')]
    noops = ["_fsync_install_root", "_install_candidate_runtime_metadata", "_source_checkpoint",
             "_acquire_candidate_validation_lock", "_maybe_fail_stage", "_restart_current_services",
             "_verify_effective_runtime_metadata", "_inspect_openclaw_plugin_runtime", "_verify_hermes_integration",
             "_start_code_implementation_owner", "_verify_release_health", "_run_openclaw_loop_deploy_verify"]
    body = "\n".join(name + "() { :; }" for name in noops) + r'''
trap 'echo "exit_committed:$COMMITTED" >> "$TRACE"' EXIT
_update_storage_release_transaction() { echo "phase:$1" >> "$TRACE"; }
_clear_storage_release_transaction() { echo clear >> "$TRACE"; }
_record_deployment_receipt() {
  echo receipt >> "$TRACE"
  [ "$FAILURE" = 0 ] || return 91
  echo committed > "$RECEIPT"
}
_resume_release_closure_reconcile() { test -s "$RECEIPT"; echo watcher >> "$TRACE"; }
_restart_storage_writers() { test -s "$RECEIPT"; echo writers >> "$TRACE"; }
_run_post_deploy_validation() { test -s "$RECEIPT"; echo validation >> "$TRACE"; }
''' + main + '\necho "committed:$COMMITTED" >> "$TRACE"\n'
    result = _shell(body, TRACE=tmp_path / "trace", RECEIPT=tmp_path / "receipt", FAILURE=int(failure),
                    RELEASE_DIR=tmp_path, CURRENT_SWITCHED=0, STORAGE_TRANSACTION_ACTIVE=1,
                    STORAGE_VACUUM_BACKUP="", USER_SYSTEMD_ENABLE_SERVICE=1, COMMIT=COMMIT,
                    STORAGE_WRITERS_STOPPED=1, EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE=strict, COMMITTED=0)
    events = (tmp_path / "trace").read_text().splitlines()
    if failure:
        assert result.returncode == 91
        assert "receipt" in events
        assert not {"watcher", "writers", "validation", "clear", "committed:1"}.intersection(events)
        assert "phase:candidate_validated" not in events
        assert "exit_committed:0" in events
    else:
        assert result.returncode == 0, result.stderr
        assert events.index("receipt") < events.index("watcher") < events.index("clear") < events.index("committed:1")
        assert "exit_committed:1" in events
        if strict:
            assert "writers" not in events
            assert events.index("validation") < events.index("clear")
        else:
            assert events.index("receipt") < events.index("writers") < events.index("clear")


@pytest.mark.parametrize("failure", ["bad_json", "exit_zero_false", "other_commit", "nested_id", "not_visible", "none"])
def test_actual_recorder_exit_zero_is_not_a_visible_native_receipt(tmp_path, failure):
    release = tmp_path / "release"
    binary = release / ".venv" / "bin"
    binary.mkdir(parents=True)
    report = {"ok": True, "report_type": "deployment_receipt", "commit": COMMIT,
              "promotion_request_id": "rec_fixture"}
    if failure == "exit_zero_false":
        report["ok"] = False
    if failure == "other_commit":
        report["commit"] = "b" * 40
    if failure == "nested_id":
        report["nested"] = {"promotion_request_id": report.pop("promotion_request_id")}
    output = "not-json" if failure == "bad_json" else json.dumps(report)
    recorder = binary / "python"
    recorder.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(output) + "\n", encoding="utf-8")
    recorder.chmod(0o700)
    body = _function("_record_deployment_receipt") + r'''
_prepare_release_receipt_source() { :; }
_verify_current_deployment_receipt() {
  echo native_readback >> "$TRACE"
  [ "$FAILURE" != not_visible ] || return 92
}
if _record_deployment_receipt; then echo success >> "$TRACE"; else echo rejected >> "$TRACE"; fi
'''
    result = _shell(body, USER_SYSTEMD_ENABLE_SERVICE=1, BASELINE_PRIOR_COMMIT="b" * 40, COMMIT=COMMIT,
                    REPO_DIR=tmp_path, CURRENT_LINK=tmp_path / "current", EIMEMORY_HEALTH_URL="http://127.0.0.1/health",
                    EIMEMORY_DEPLOY_SCOPE_TENANT=OWNER.tenant_id,
                    EIMEMORY_DEPLOY_SCOPE_AGENT=OWNER.agent_id, EIMEMORY_DEPLOY_SCOPE_WORKSPACE=OWNER.workspace_id,
                    EIMEMORY_DEPLOY_SCOPE_USER=OWNER.user_id, EIMEMORY_ROOT=tmp_path, EIMEMORY_CONFIG_DIR=tmp_path,
                    EVIDENCE_RECEIPT_ENV_FILE=tmp_path / "receipt.env", RELEASE_DIR=release,
                    EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE=0, TRACE=tmp_path / "trace", FAILURE=failure)
    assert result.returncode == 0, result.stderr
    events = (tmp_path / "trace").read_text().splitlines()
    if failure == "none":
        assert events == ["native_readback", "success"]
        assert "deployment_receipt_id=rec_fixture" in result.stdout
    else:
        assert events[-1] == "rejected"
        assert "deployment_receipt_id=" not in result.stdout
        assert ("native_readback" in events) == (failure == "not_visible")
