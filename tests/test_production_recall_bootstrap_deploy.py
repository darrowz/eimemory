from __future__ import annotations

from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from threading import Thread

from eimemory.api.runtime import Runtime
from eimemory.evaluation import real_query_gate
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"tenant_id": "default", "agent_id": "main", "workspace_id": "production", "user_id": "darrow"}


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


def test_installer_runs_candidate_bootstrap_before_atomic_current_switch() -> None:
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    invocation = installer.index("_run_pre_switch_production_recall_bootstrap\n")
    switch = installer.index('ln -sfn "$RELEASE_DIR" "$CURRENT_LINK.next"')
    assert invocation < switch
    function = installer[installer.index("_run_pre_switch_production_recall_bootstrap()") : invocation]
    assert '"$RELEASE_DIR/.venv/bin/python"' in function
    assert '--candidate-commit "$COMMIT" --prior-commit "$PREVIOUS_COMMIT"' in function
    bootstrap = Path("deploy/bootstrap_production_recall.py").read_text(encoding="utf-8")
    assert "current_release_identity" not in bootstrap


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
