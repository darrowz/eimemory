"""A fresh immutable receipt must exist before persistent maintenance resumes."""
from __future__ import annotations

import json
from dataclasses import asdict
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


@pytest.mark.skipif(sys.platform == "win32", reason="shared service receipt uses POSIX immutable service anchors")
def test_shared_service_version_remains_public_but_new_deployment_owner_is_exact(tmp_path, monkeypatch):
    from eimemory.governance.release import evidence_contract

    store = RuntimeStore(tmp_path)
    try:
        record = store.append(_receipt())
        before = record.to_dict()
        runtime = SimpleNamespace(store=store)
        caller = ScopeRef(tenant_id="caller-tenant", agent_id="caller-agent",
                          workspace_id="caller-workspace", user_id="caller-user")
        monkeypatch.setattr(evidence_contract, "_runtime_commit", lambda _runtime: COMMIT)
        # Exercise the real shared-service scanner, not a mocked identity.
        shared = evidence_contract.current_release_identity(runtime, caller)
        assert shared is not None and shared.receipt_id == record.record_id
        assert evidence_contract.current_deployment_receipt_for_owner(
            runtime, record.record_id, caller, expected_commit=COMMIT, expected_version=__version__,
        ) is None
        assert evidence_contract.current_deployment_receipt_for_owner(
            runtime, record.record_id, OWNER, expected_commit=COMMIT, expected_version=__version__,
        ) == shared
        assert store.get_by_id(record.record_id, scope=caller, exact_scope=True) is None
        assert store.get_by_id(record.record_id, scope=OWNER, exact_scope=True).to_dict() == before
    finally:
        store.close()


@pytest.mark.parametrize("invalid", ["absent", "old_commit", "old_version", "revoked", "wrong_id", "unverified", "source", "kind",
                                     "wrong_tenant", "wrong_agent", "wrong_workspace", "wrong_user"])
def test_native_receipt_recheck_rejects_missing_or_different_authority(tmp_path, monkeypatch, invalid):
    store = RuntimeStore(tmp_path)
    try:
        record = _receipt(commit="b" * 40 if invalid == "old_commit" else COMMIT,
                          version="0.0.0" if invalid == "old_version" else __version__)
        if invalid == "revoked":
            record.status = "revoked"
        if invalid == "unverified":
            record.content["gate"]["receipt_verified"] = False
        if invalid == "source":
            record.source = "fixture.untrusted"
        if invalid == "kind":
            record.kind = "reflection"
        scope_fields = {"wrong_tenant": "tenant_id", "wrong_agent": "agent_id",
                        "wrong_workspace": "workspace_id", "wrong_user": "user_id"}
        if invalid in scope_fields:
            record.scope = ScopeRef(**{**asdict(OWNER), scope_fields[invalid]: "other-owner-coordinate"})
        if invalid != "absent":
            store.append(record)
    finally:
        store.close()
    receipt_id = "rec_other" if invalid == "wrong_id" else record.record_id
    with pytest.raises(SystemExit, match="current_deployment_receipt_unavailable"):
        _run_checker(monkeypatch, tmp_path, receipt_id)


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id", "record_id"])
def test_new_deployment_owner_reader_does_not_trust_store_scope_filter(monkeypatch, field):
    from eimemory.governance.release import evidence_contract

    record = _receipt()
    expected_id = record.record_id
    if field == "record_id":
        record.record_id = "rec_foreign"
    else:
        record.scope = ScopeRef(**{**asdict(OWNER), field: "foreign"})
    calls = []
    class IgnoringStore:
        def get_by_id(self, record_id, **kwargs):
            calls.append((record_id, kwargs))
            return record
    monkeypatch.setattr(evidence_contract, "_runtime_commit", lambda _runtime: COMMIT)
    assert evidence_contract.current_deployment_receipt_for_owner(
        SimpleNamespace(store=IgnoringStore()), expected_id, OWNER,
        expected_commit=COMMIT, expected_version=__version__,
    ) is None
    assert calls == [(expected_id, {"scope": OWNER, "exact_scope": True})]


@pytest.mark.parametrize("invalid", ["runtime_commit", "expected_version", "physical_digest", "physical_projection",
                                     "missing_tenant", "empty_tenant", "nonstring_owner"])
def test_new_deployment_owner_reader_rechecks_current_and_physical_identity(tmp_path, monkeypatch, invalid):
    from eimemory.governance.release import evidence_contract

    store = RuntimeStore(tmp_path)
    try:
        record = store.append(_receipt())
        if invalid == "physical_digest":
            with store._lock:
                store.sqlite.conn.execute("UPDATE records SET payload_digest=? WHERE record_id=?",
                                          ("f" * 64, record.record_id))
                store.sqlite.conn.commit()
        if invalid == "physical_projection":
            from eimemory.storage.jsonl import payload_digest
            payload = record.to_dict()
            payload["scope"]["tenant_id"] = "foreign-payload-owner"
            with store._lock:
                store.sqlite.conn.execute("UPDATE records SET payload_json=?,payload_digest=? WHERE record_id=?",
                                          (json.dumps(payload), payload_digest(payload), record.record_id))
                store.sqlite.conn.commit()
        monkeypatch.setattr(evidence_contract, "_runtime_commit",
                            lambda _runtime: "b" * 40 if invalid == "runtime_commit" else COMMIT)
        scope = asdict(OWNER)
        if invalid == "missing_tenant":
            del scope["tenant_id"]
        if invalid == "empty_tenant":
            scope["tenant_id"] = ""
        if invalid == "nonstring_owner":
            scope["user_id"] = None
        assert evidence_contract.current_deployment_receipt_for_owner(
            SimpleNamespace(store=store), record.record_id, scope, expected_commit=COMMIT,
            expected_version="0.0.0" if invalid == "expected_version" else __version__,
        ) is None
    finally:
        store.close()


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_strict_receipt_validator_rejects_foreign_owner_before_ledger_access(monkeypatch, field):
    from eimemory.governance.release import deployment_receipt

    record = _receipt()
    record.scope = ScopeRef(**{**asdict(OWNER), field: "foreign"})
    record.content["side_effect"]["code_evolution"] = dict(strict=True, transaction_id="tx-owner",
        authorization_digest="1" * 64, policy_digest="2" * 64, patch_digest="3" * 64,
        candidate_tree_digest="4" * 64, provider_implementation_digest="5" * 64,
        observation_deadline="2030-01-01T00:00:00+00:00",
        verification_receipt_digests=[digit * 64 for digit in ("6", "7", "8")],
        lineage=dict(ok=True, compatible=True))
    monkeypatch.setattr(deployment_receipt, "_strict_transaction_error",
                        lambda *a, **kw: pytest.fail("foreign receipt must not reach a different owner's ledger"))
    assert deployment_receipt.strict_code_evolution_receipt_error(
        SimpleNamespace(store=None), scope=OWNER, record=record, deployed_commit=COMMIT,
    ) == "code_evolution_receipt_scope_mismatch"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("newer_shared", [False, True])
@pytest.mark.parametrize("field", [None, "tenant_id", "agent_id", "workspace_id", "user_id"])
def test_target_receipt_readback_reopens_committed_exact_owner_for_new_and_154(
    tmp_path, monkeypatch, capsys, legacy, newer_shared, field,
):
    """Run the actual target program against a separately reopened SQLite DB.

    Only the live installation writer/mount is isolated; the receipt, physical
    owner hydration and native identity readers are the production code.
    """
    import ast
    import inspect
    import os
    import eimemory
    from eimemory.api.runtime import Runtime
    from eimemory.governance.evolution import code_deployment_job
    from eimemory.governance.release import evidence_contract
    from eimemory import version

    if newer_shared and sys.platform == "win32":
        pytest.skip("actual shared service scanner uses POSIX immutable service anchors")
    target_version = "1.14.154" if legacy else __version__
    target_commit = "f14770f01e17984d0cb7945baaebeb1db63502ab" if legacy else COMMIT
    if legacy:
        monkeypatch.delattr(evidence_contract, "current_deployment_receipt_for_owner")
        monkeypatch.setattr(version, "__version__", target_version)
    record = _receipt(commit=target_commit, version=target_version)
    if newer_shared:
        # Isolated ordering fixtures, not a deployment/observation clock.
        # Native list_records sorts updated_at DESC, then record_id DESC.
        record.time.updated_at = "2000-01-01T00:00:00+00:00"
    if field is not None:
        record.scope = ScopeRef(**{**asdict(OWNER), field: "foreign"})
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    peer = None
    try:
        store.append(record)
        if newer_shared:
            peer = _receipt(commit=target_commit, version=target_version)
            peer.scope = ScopeRef(tenant_id="peer-tenant", agent_id="peer-agent",
                                  workspace_id="peer-workspace", user_id="peer-user")
            peer.time.updated_at = "2000-01-02T00:00:00+00:00"
            store.append(peer)
    finally:
        store.close()
    creations = []
    report = dict(ok=True, promotion_request_id=record.record_id, version=target_version)
    def verify_and_record_deployment(*, scope):
        assert scope == asdict(OWNER)
        return dict(report)
    def create(*, root, bootstrap_schema=True):
        reopened = RuntimeStore(root, bootstrap_schema=bootstrap_schema)
        creations.append(reopened)
        return SimpleNamespace(store=reopened, close=reopened.close,
            verify_and_record_deployment=verify_and_record_deployment)
    monkeypatch.setattr(Runtime, "create", create)
    monkeypatch.setattr(evidence_contract, "_runtime_commit", lambda _runtime: target_commit)
    if newer_shared:
        probe = RuntimeStore(root)
        try:
            selected = evidence_contract.current_release_identity(SimpleNamespace(store=probe), OWNER)
            assert selected is not None and selected.receipt_id == peer.record_id != record.record_id
        finally:
            probe.close()
    expected = tmp_path / "releases" / target_commit
    monkeypatch.setattr(eimemory, "__file__", str(expected / "eimemory/__init__.py"))
    monkeypatch.setattr(os, "ST_RDONLY", 1, raising=False)
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=1), raising=False)
    monkeypatch.setattr(sys, "argv", ["-c", str(expected), "a" * 64, "receipt",
        json.dumps(dict(scope=asdict(OWNER))), str(root)])
    function = ast.parse(inspect.getsource(code_deployment_job._target_validation_report))
    program = next(node.value.value for node in ast.walk(function)
                   if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                   and any(isinstance(target, ast.Name) and target.id == "program" for target in node.targets))
    with pytest.raises(SystemExit) as outcome:
        exec(compile(program, "native_target_receipt_program", "exec"), {})
    assert len(creations) == 2 and creations[0] is not creations[1]
    if field is None:
        assert outcome.value.code == 0
        value = json.loads(capsys.readouterr().out)
        assert value["physical_receipt_readback"] == {
            "current_deployment_receipt_verified": True, "commit": target_commit, "receipt_id": record.record_id,
        }
    else:
        assert outcome.value.code == "current_deployment_receipt_unavailable"
        assert not capsys.readouterr().out


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
