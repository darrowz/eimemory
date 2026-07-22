from __future__ import annotations

from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

import deploy.bootstrap_production_recall as bootstrap_deploy
from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"tenant_id": "default", "agent_id": "main", "workspace_id": "production", "user_id": "darrow"}


class _BootstrapRuntime:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _bootstrap_args(tmp_path: Path, *, dataset: str | None = None) -> list[str]:
    args = [
        "--candidate-commit",
        "a" * 40,
        "--prior-commit",
        "b" * 40,
        "--current-link",
        str(tmp_path / "current"),
        "--health-url",
        "http://127.0.0.1:1/health",
        "--root",
        str(tmp_path / "runtime"),
        "--agent",
        SCOPE["agent_id"],
        "--workspace",
        SCOPE["workspace_id"],
        "--user",
        SCOPE["user_id"],
    ]
    if dataset is not None:
        args.extend(["--dataset", dataset])
    return args


def _patch_ready_accumulated_gate(monkeypatch, tmp_path: Path) -> tuple[_BootstrapRuntime, dict[str, list]]:
    runtime = _BootstrapRuntime()
    calls: dict[str, list] = {"build": [], "write": [], "gate": []}
    monkeypatch.setattr(bootstrap_deploy.Runtime, "create", lambda **_kwargs: runtime)
    monkeypatch.setattr(
        bootstrap_deploy,
        "collect_pending_production_queries",
        lambda *_args, **_kwargs: {"created": 2, "skipped": {"duplicate": 1}},
    )

    def build(*_args, **_kwargs):
        calls["build"].append(True)
        return {"ready": True, "dataset": {"schema": "production_redacted_v1"}}

    def write(dataset, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dataset), encoding="utf-8")
        calls["write"].append(target)

    monkeypatch.setattr(bootstrap_deploy, "build_production_query_dataset", build)
    monkeypatch.setattr(bootstrap_deploy, "write_production_query_dataset", write)
    monkeypatch.setattr(
        bootstrap_deploy,
        "load_json_dataset_with_evidence",
        lambda path: ({"schema": "production_redacted_v1"}, {"path": str(path)}),
    )
    monkeypatch.setattr(
        bootstrap_deploy,
        "freeze_production_recall_dataset",
        lambda _dataset: {"eligibility": {"ok": True}},
    )

    def gate(*_args, **_kwargs):
        calls["gate"].append(True)
        return {"ok": True, "bootstrap_status": "anchor_ready"}

    monkeypatch.setattr(bootstrap_deploy, "bootstrap_production_recall_baseline", gate)
    return runtime, calls


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


@contextmanager
def _health(payload: dict):
    raw = json.dumps(payload).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("dataset_source", ["cli", "environment"])
def test_explicit_missing_dataset_fails_closed_without_building_or_running_gate(
    tmp_path,
    monkeypatch,
    capsys,
    dataset_source: str,
) -> None:
    runtime, calls = _patch_ready_accumulated_gate(monkeypatch, tmp_path)
    missing = tmp_path / "operator-selected" / "missing.json"
    monkeypatch.delenv("EIMEMORY_PRODUCTION_RECALL_DATASET", raising=False)
    args = _bootstrap_args(tmp_path)
    if dataset_source == "cli":
        args.extend(["--dataset", str(missing)])
    else:
        monkeypatch.setenv("EIMEMORY_PRODUCTION_RECALL_DATASET", str(missing))

    exit_code = bootstrap_deploy.main(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code != 0
    assert payload == {
        "collection": {"created": 2, "skipped": {"duplicate": 1}},
        "ok": False,
        "path": str(missing),
        "reason": "dataset_path_unavailable",
        "status": "blocked",
    }
    assert calls == {"build": [], "write": [], "gate": []}
    assert not (tmp_path / "runtime" / "evaluation" / "production_recall.json").exists()
    assert runtime.closed is True


def test_unspecified_dataset_keeps_accumulated_build_path(tmp_path, monkeypatch, capsys) -> None:
    runtime, calls = _patch_ready_accumulated_gate(monkeypatch, tmp_path)
    monkeypatch.delenv("EIMEMORY_PRODUCTION_RECALL_DATASET", raising=False)

    exit_code = bootstrap_deploy.main(_bootstrap_args(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    conventional = tmp_path / "runtime" / "evaluation" / "production_recall.json"
    assert exit_code == 0
    assert calls == {"build": [True], "write": [conventional], "gate": [True]}
    assert conventional.is_file()
    assert payload["collection"] == {"created": 2, "skipped": {"duplicate": 1}}
    assert runtime.closed is True


def test_early_pending_report_has_the_same_collection_shape(tmp_path, monkeypatch, capsys) -> None:
    runtime = _BootstrapRuntime()
    monkeypatch.delenv("EIMEMORY_PRODUCTION_RECALL_DATASET", raising=False)
    monkeypatch.setattr(bootstrap_deploy.Runtime, "create", lambda **_kwargs: runtime)
    monkeypatch.setattr(
        bootstrap_deploy,
        "collect_pending_production_queries",
        lambda *_args, **_kwargs: {"created": 3, "skipped": {"duplicate": 2}},
    )
    monkeypatch.setattr(
        bootstrap_deploy,
        "build_production_query_dataset",
        lambda *_args, **_kwargs: {"ready": False, "progress": {"case_count": 4}},
    )
    monkeypatch.setattr(
        bootstrap_deploy,
        "record_production_recall_bootstrap_pending",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "bootstrap_data_pending",
            "reason": "production_dataset_not_ready",
        },
    )

    exit_code = bootstrap_deploy.main(_bootstrap_args(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["collection"] == {"created": 3, "skipped": {"duplicate": 2}}
    assert runtime.closed is True


def test_progress_thresholds_use_real_query_gate_constants(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap_deploy, "_REAL_QUERY_MIN_CASES", 27, raising=False)
    monkeypatch.setattr(
        bootstrap_deploy,
        "_REAL_QUERY_MIN_CASES_PER_CHANNEL",
        9,
        raising=False,
    )

    progress = bootstrap_deploy._progress({"eligibility": {}})

    assert progress["required_case_count"] == 27
    assert progress["required_per_channel"] == 9


def _receipt(runtime: Runtime, *, commit: str, prior_commit: str) -> ReleaseIdentity:
    release_path = f"/opt/eimemory/releases/{commit}"
    record = RecordEnvelope.create(
        kind="promotion_request",
        title="Verified prior deployment",
        source="eimemory.deployment_receipt",
        status="deployed",
        scope=ScopeRef.from_dict(SCOPE),
        content={
            "report_type": "deployment_receipt",
            "promotion_target": "code_patch",
            "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": True},
            "side_effect": {
                "ok": True,
                "production_applied": True,
                "deployment_executed": True,
                "verification": {"ok": True, "skipped": False, "prior_commit": prior_commit},
                "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                "post_deploy_health": {"ok": True, "skipped": False, "commit": commit, "version": "1.9.80", "release_path": release_path},
                "commit": {"commit_sha": commit},
                "release": {"version": "1.9.80", "release_path": release_path},
                "rollback_evidence": {"prior_commit_sha": prior_commit, "rollback_command": "verified rollback"},
            },
        },
        meta={"report_type": "deployment_receipt"},
    )
    runtime.store.append(record)
    return ReleaseIdentity(commit, "1.9.80", record.record_id, record.record_id)


def test_prior_identity_is_taken_from_live_link_health_and_receipt_not_candidate_import_root(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    prior = _receipt(runtime, commit="b" * 40, prior_commit="c" * 40)
    release = tmp_path / "releases" / prior.commit
    release.mkdir(parents=True)
    current = tmp_path / "current"
    _link(current, release)
    payload = {"ok": True, "commit": prior.commit, "version": prior.version, "paths": {"release": str(release)}}
    with _health(payload) as health_url:
        monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_CURRENT_LINK", str(current))
        monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_HEALTH_URL", health_url)
        monkeypatch.setattr(
            real_query_gate,
            "current_release_identity",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("candidate import identity must not select prior")),
        )
        resolved, reason = real_query_gate._verified_live_prior_release(
            runtime,
            scope=ScopeRef.from_dict(SCOPE),
            prior_commit=prior.commit,
            current_link=str(current),
            health_url=health_url,
        )
    runtime.close()

    assert reason == ""
    assert resolved == prior


def test_prior_identity_accepts_bound_snapshot_when_live_health_is_already_stopped(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    prior = _receipt(runtime, commit="b" * 40, prior_commit="c" * 40)
    release = tmp_path / "releases" / prior.commit
    release.mkdir(parents=True)
    current = tmp_path / "current"
    _link(current, release)
    health_url = "http://127.0.0.1:1/health"
    snapshot = {
        "schema": "prior_health_snapshot.v1",
        "health_url": health_url,
        "health": {
            "ok": True,
            "commit": prior.commit,
            "version": prior.version,
            "paths": {"release": str(release)},
        },
    }
    monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_CURRENT_LINK", str(current))
    monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_HEALTH_URL", health_url)
    monkeypatch.setattr(
        real_query_gate,
        "_fetch_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must use the protected snapshot")),
    )

    resolved, reason = real_query_gate._verified_live_prior_release(
        runtime,
        scope=ScopeRef.from_dict(SCOPE),
        prior_commit=prior.commit,
        current_link=str(current),
        health_url=health_url,
        prior_health_snapshot=snapshot,
    )
    runtime.close()

    assert reason == ""
    assert resolved == prior


@pytest.mark.parametrize(
    "snapshot",
    [
        [],
        {"schema": "prior_health_snapshot.v0", "health_url": "http://127.0.0.1:1/health", "health": {}},
        {"schema": "prior_health_snapshot.v1", "health_url": "http://127.0.0.1:2/health", "health": {}},
        {"schema": "prior_health_snapshot.v1", "health_url": "http://127.0.0.1:1/health", "health": []},
        {
            "schema": "prior_health_snapshot.v1",
            "health_url": "http://127.0.0.1:1/health",
            "health": {"padding": "x" * 65536},
        },
    ],
    ids=["non-dict", "schema", "url", "health-non-dict", "oversized"],
)
def test_prior_health_snapshot_rejects_non_dict_mismatched_or_oversized_payloads(
    tmp_path,
    monkeypatch,
    snapshot,
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    prior = _receipt(runtime, commit="b" * 40, prior_commit="c" * 40)
    release = tmp_path / "releases" / prior.commit
    release.mkdir(parents=True)
    current = tmp_path / "current"
    _link(current, release)
    health_url = "http://127.0.0.1:1/health"
    monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_CURRENT_LINK", str(current))
    monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_HEALTH_URL", health_url)

    resolved, reason = real_query_gate._verified_live_prior_release(
        runtime,
        scope=ScopeRef.from_dict(SCOPE),
        prior_commit=prior.commit,
        current_link=str(current),
        health_url=health_url,
        prior_health_snapshot=snapshot,
    )
    runtime.close()

    assert resolved is None
    assert reason == "prior_health_snapshot_invalid"


@pytest.mark.parametrize("field", ["commit", "version", "release"])
def test_prior_health_snapshot_cannot_forge_release_identity(tmp_path, monkeypatch, field: str) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    prior = _receipt(runtime, commit="b" * 40, prior_commit="c" * 40)
    release = tmp_path / "releases" / prior.commit
    release.mkdir(parents=True)
    current = tmp_path / "current"
    _link(current, release)
    health_url = "http://127.0.0.1:1/health"
    health = {
        "ok": True,
        "commit": prior.commit,
        "version": prior.version,
        "paths": {"release": str(release)},
    }
    if field == "commit":
        health["commit"] = "d" * 40
    elif field == "version":
        health["version"] = "9.9.9"
    else:
        forged = tmp_path / "releases" / ("d" * 40)
        forged.mkdir(parents=True)
        health["paths"]["release"] = str(forged)
    snapshot = {"schema": "prior_health_snapshot.v1", "health_url": health_url, "health": health}
    monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_CURRENT_LINK", str(current))
    monkeypatch.setattr(real_query_gate, "DEFAULT_DEPLOYMENT_HEALTH_URL", health_url)

    resolved, reason = real_query_gate._verified_live_prior_release(
        runtime,
        scope=ScopeRef.from_dict(SCOPE),
        prior_commit=prior.commit,
        current_link=str(current),
        health_url=health_url,
        prior_health_snapshot=snapshot,
    )
    runtime.close()

    assert resolved is None
    assert reason == "prior_health_identity_mismatch"


def test_bootstrap_cli_loads_a_bounded_snapshot_and_passes_it_to_the_gate(tmp_path, monkeypatch, capsys) -> None:
    runtime, calls = _patch_ready_accumulated_gate(monkeypatch, tmp_path)
    captured: list[dict] = []
    monkeypatch.setattr(
        bootstrap_deploy,
        "bootstrap_production_recall_baseline",
        lambda *_args, **kwargs: captured.append(kwargs) or {"ok": True, "bootstrap_status": "anchor_ready"},
    )
    snapshot = {
        "schema": "prior_health_snapshot.v1",
        "health_url": "http://127.0.0.1:1/health",
        "health": {"ok": True},
    }
    snapshot_path = tmp_path / "prior-health.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    args = _bootstrap_args(tmp_path) + ["--prior-health-snapshot", str(snapshot_path)]

    exit_code = bootstrap_deploy.main(args)
    capsys.readouterr()

    assert exit_code == 0
    assert calls["gate"] == []
    assert captured[0]["prior_health_snapshot"] == snapshot
    assert runtime.closed is True


def test_prior_health_capture_runs_in_isolated_mode_and_never_echoes_failed_payload() -> None:
    helper = Path("deploy/capture_prior_health_snapshot.py").resolve()
    payload = {"ok": True, "commit": "b" * 40, "version": "1.9.80"}
    with _health(payload) as health_url:
        captured = subprocess.run(
            [sys.executable, "-I", "-B", str(helper), "--health-url", health_url],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert captured.returncode == 0, captured.stderr
    snapshot = json.loads(captured.stdout)
    assert snapshot == {
        "schema": "prior_health_snapshot.v1",
        "health_url": health_url,
        "health": payload,
    }

    with _health(["sensitive-response-must-not-leak"]) as invalid_url:
        rejected = subprocess.run(
            [sys.executable, "-I", "-B", str(helper), "--health-url", invalid_url],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert rejected.stderr.strip() == "prior_health_capture_failed"


@pytest.mark.parametrize(
    "payload",
    ["[]", json.dumps({"padding": "x" * 65536})],
    ids=["non-dict", "oversized"],
)
def test_bootstrap_cli_rejects_non_dict_or_oversized_snapshot_before_opening_runtime(
    tmp_path,
    monkeypatch,
    capsys,
    payload: str,
) -> None:
    snapshot_path = tmp_path / "prior-health.json"
    snapshot_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        bootstrap_deploy.Runtime,
        "create",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("invalid snapshot must fail before runtime writes")),
    )

    exit_code = bootstrap_deploy.main(
        _bootstrap_args(tmp_path) + ["--prior-health-snapshot", str(snapshot_path)]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert report == {"ok": False, "reason": "prior_health_snapshot_invalid", "status": "blocked"}


def test_installer_runs_candidate_bootstrap_before_atomic_current_switch() -> None:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    invocation = installer.index("_run_pre_switch_production_recall_bootstrap\n")
    switch = installer.index('ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"')
    assert invocation < switch
    function_start = installer.index("_run_pre_switch_production_recall_bootstrap()")
    function_end = installer.index("\n}", function_start)
    function = installer[function_start:function_end]
    assert '"$RELEASE_DIR/.venv/bin/python"' in function
    assert '--candidate-commit "$COMMIT" --prior-commit "$PREVIOUS_COMMIT"' in function
    bootstrap = Path("deploy/bootstrap_production_recall.py").read_text(encoding="utf-8")
    assert "current_release_identity" not in bootstrap


def test_installer_captures_prior_health_before_quiesce_and_bootstrap_consumes_snapshot() -> None:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    prepare = installer.split("_prepare_storage_for_release() {", 1)[1].split("\n}", 1)[0]
    capture = prepare.index("if ! _capture_prior_health_snapshot; then")
    writer_capture = prepare.index("_capture_storage_writers", capture)
    retire = prepare.index("_retire_system_rpc_unit", writer_capture)
    marker = prepare.index("_begin_storage_release_transaction", retire)
    stop = prepare.index("_stop_storage_writers", marker)
    snapshot_ready = prepare.index("STORAGE_SNAPSHOT_READY=1", stop)
    bootstrap = prepare.index("_run_pre_switch_production_recall_bootstrap", snapshot_ready)

    assert capture < writer_capture < retire < marker < stop < snapshot_ready < bootstrap
    assert "prior_health_capture=failed before_transaction" in prepare[capture:writer_capture]
    assert "return 2" in prepare[capture:writer_capture]
    runner = installer.split("_run_pre_switch_production_recall_bootstrap() {", 1)[1].split("\n}", 1)[0]
    assert 'if [ -z "$PRIOR_HEALTH_SNAPSHOT_FILE" ] || [ ! -f "$PRIOR_HEALTH_SNAPSHOT_FILE" ] ||' in runner
    assert '--prior-health-snapshot "$PRIOR_HEALTH_SNAPSHOT_FILE"' in runner


def test_bootstrap_state_uses_full_record_identity_not_fuzzy_reflection_title(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict(SCOPE)
    common = {
        "schema": real_query_gate.PRODUCTION_RECALL_BOOTSTRAP_STATE_SCHEMA,
        "state": "bootstrap_data_pending",
        "prior_release": {
            "release_commit": "b" * 40,
            "release_version": "1.9.79",
            "deployment_receipt_id": "receipt-prior",
            "release_session_id": "session-prior",
        },
        "scope": SCOPE,
        "reason": "production_dataset_not_ready",
        "progress": {"case_count": 1},
        "previous_record_id": "",
        "generated_at": "2026-07-22T00:00:00+00:00",
    }
    first_record = real_query_gate._bootstrap_state_record(
        {**common, "candidate_commit": "123456789abc" + "a" * 28},
        scope=scope,
    )
    second_record = real_query_gate._bootstrap_state_record(
        {**common, "candidate_commit": "123456789abc" + "d" * 28},
        scope=scope,
    )

    first = runtime.store.append(first_record)
    second = runtime.store.append(second_record)
    retried = runtime.store.append(second_record)
    stored = runtime.store.list_records(
        kinds=["reflection"],
        scope=scope,
        limit=10,
    )
    runtime.close()

    assert first.record_id == first_record.record_id
    assert second.record_id == second_record.record_id
    assert first.record_id != second.record_id
    assert retried.record_id == second.record_id
    assert {record.record_id for record in stored} == {first.record_id, second.record_id}


def test_strict_activation_is_idempotent_for_current_commit_and_bound_gate(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict(SCOPE)
    prior = ReleaseIdentity("b" * 40, "1.9.79", "receipt-prior", "session-prior")
    current = ReleaseIdentity("a" * 40, "1.9.80", "receipt-current", "session-current")
    real_query_gate._persist_bootstrap_state(
        runtime,
        scope=scope,
        state="anchor_ready",
        candidate_commit=current.commit,
        prior_release=prior,
        reason="pre_switch_bootstrap_anchor",
        progress={"baseline_record_id": "baseline-current"},
    )
    monkeypatch.setattr(
        real_query_gate,
        "verify_current_production_recall_gate",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "accepted",
            "record_id": "prg-current",
        },
    )

    first = real_query_gate.activate_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-current",
    )
    second = real_query_gate.activate_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-current",
    )
    monkeypatch.setattr(
        real_query_gate,
        "verify_current_production_recall_gate",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "accepted",
            "record_id": "prg-replacement",
        },
    )
    rebound = real_query_gate.activate_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-replacement",
    )
    verified = real_query_gate.verify_current_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-current",
    )
    runtime.close()

    assert first["ok"] is True and first["status"] == "strict_activated"
    assert second == first
    assert verified == first
    assert rebound["ok"] is False
    assert rebound["reason"] == "strict_gate_record_mismatch"


def test_strict_state_verifier_rejects_missing_and_cross_commit_state(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict(SCOPE)
    current = ReleaseIdentity("a" * 40, "1.9.80", "receipt-current", "session-current")
    missing = real_query_gate.verify_current_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-current",
    )
    prior = ReleaseIdentity("b" * 40, "1.9.79", "receipt-prior", "session-prior")
    real_query_gate._persist_bootstrap_state(
        runtime,
        scope=scope,
        state="strict_activated",
        candidate_commit="c" * 40,
        prior_release=prior,
        reason="strict_gate_accepted",
        progress={"gate_record_id": "prg-old"},
    )
    cross_commit = real_query_gate.verify_current_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-current",
    )
    runtime.close()

    assert missing["ok"] is False
    assert missing["reason"] == "strict_state_missing"
    assert cross_commit["ok"] is False
    assert cross_commit["reason"] == "strict_state_commit_mismatch"


def test_strict_state_verifier_rejects_broken_previous_state_chain(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict(SCOPE)
    current = ReleaseIdentity("a" * 40, "1.9.80", "receipt-current", "session-current")
    prior = ReleaseIdentity("b" * 40, "1.9.79", "receipt-prior", "session-prior")
    forged = real_query_gate._bootstrap_state_record(
        {
            "schema": real_query_gate.PRODUCTION_RECALL_BOOTSTRAP_STATE_SCHEMA,
            "state": "strict_activated",
            "candidate_commit": current.commit,
            "prior_release": {
                "release_commit": prior.commit,
                "release_version": prior.version,
                "deployment_receipt_id": prior.receipt_id,
                "release_session_id": prior.session_id,
            },
            "scope": SCOPE,
            "reason": "strict_gate_accepted",
            "progress": {"gate_record_id": "prg-current"},
            "previous_record_id": "missing-anchor",
            "generated_at": "2026-07-22T00:00:00+00:00",
        },
        scope=scope,
    )
    runtime.store.append(forged)

    verified = real_query_gate.verify_current_production_recall_strict_state(
        runtime,
        scope=scope,
        release=current,
        gate_record_id="prg-current",
    )
    runtime.close()

    assert verified["ok"] is False
    assert verified["reason"] == "strict_state_chain_invalid"


def test_bootstrap_pending_can_follow_patch_lineage_but_cannot_regress_after_anchor(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    prior = _receipt(runtime, commit="b" * 40, prior_commit="c" * 40)
    monkeypatch.setattr(real_query_gate, "_verified_live_prior_release", lambda *_args, **_kwargs: (prior, ""))
    first = real_query_gate.record_production_recall_bootstrap_pending(
        runtime,
        scope=SCOPE,
        candidate_commit="a" * 40,
        prior_commit=prior.commit,
        progress={"case_count": 1},
    )
    current = _receipt(runtime, commit="a" * 40, prior_commit=prior.commit)
    assert first["status"] == "bootstrap_data_pending"
    assert real_query_gate.verify_current_bootstrap_data_pending(runtime, scope=SCOPE, release=current)["ok"] is True

    monkeypatch.setattr(real_query_gate, "_verified_live_prior_release", lambda *_args, **_kwargs: (current, ""))
    second = real_query_gate.record_production_recall_bootstrap_pending(
        runtime,
        scope=SCOPE,
        candidate_commit="d" * 40,
        prior_commit=current.commit,
        progress={"case_count": 4},
    )
    newer = _receipt(runtime, commit="d" * 40, prior_commit=current.commit)
    assert second["status"] == "bootstrap_data_pending"
    verified_second = real_query_gate.verify_current_bootstrap_data_pending(runtime, scope=SCOPE, release=newer)
    assert verified_second["ok"] is True, verified_second

    real_query_gate._persist_bootstrap_state(
        runtime,
        scope=ScopeRef.from_dict(SCOPE),
        state="anchor_ready",
        candidate_commit=newer.commit,
        prior_release=current,
        reason="pre_switch_bootstrap_anchor",
        progress={"baseline_record_id": "baseline"},
    )
    monkeypatch.setattr(real_query_gate, "_verified_live_prior_release", lambda *_args, **_kwargs: (newer, ""))
    rejected = real_query_gate.record_production_recall_bootstrap_pending(
        runtime,
        scope=SCOPE,
        candidate_commit="e" * 40,
        prior_commit=newer.commit,
    )
    runtime.close()

    assert rejected["ok"] is False
    assert rejected["reason"] == "bootstrap_pending_regression_forbidden"
