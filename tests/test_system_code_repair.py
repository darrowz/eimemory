from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.governance.system_code_repair import process_system_code_incidents
from eimemory.ops.release_closure_failure import record_release_closure_failure


SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
COMMIT = "a" * 40


def test_trusted_closure_incident_enters_v2_transaction_path(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    record_release_closure_failure(
        runtime,
        scope=SCOPE,
        closure_report={
            "ok": False,
            "closure_complete": False,
            "data_accumulating": False,
            "blocked_stage": "release_lineage",
            "blocked_reason": "release_lineage_not_compatible",
            "deployment": {"commit": COMMIT, "version": "1.11.40"},
            "release_lineage": {"compatible": False},
        },
        detected_at="2026-08-28T12:00:00Z",
    )
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._repository_identity",
        lambda _root: {"ok": True, "base_commit": COMMIT},
    )
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=COMMIT),
    )
    proposal_calls = []

    def proposal(_runtime, **kwargs):
        proposal_calls.append(kwargs)
        return {
            "ok": True,
            "schema_version": "code_implementation_proposal.v2",
            "transaction_id": kwargs["transaction_id"],
            "incident": kwargs["incident"],
            "repository": {"base_commit": COMMIT},
            "provider": {"capability_id": "code.implementation"},
        }

    monkeypatch.setattr(
        "eimemory.governance.code_evolution_bridge.propose_code_patch_v2",
        proposal,
    )
    evolution_calls = []

    def evolve(_runtime, **kwargs):
        evolution_calls.append(kwargs)
        return {"ok": True, "applied_count": 0, "blocked_patches": []}

    monkeypatch.setattr(Runtime, "run_autonomous_evolution", evolve)
    try:
        report = process_system_code_incidents(
            runtime,
            scope=SCOPE,
            repo_root=Path.cwd(),
            max_items=1,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["status"] == "processed"
    assert proposal_calls[0]["origin"] == "system_detector"
    assert proposal_calls[0]["known_before_detection"] is False
    assert proposal_calls[0]["prior_user_reported"] is False
    assert proposal_calls[0]["manual_bootstrap"] is False
    assert proposal_calls[0]["bounds"] == {
        "maximum_files": 2,
        "maximum_bytes_per_file": 48 * 1024,
        "maximum_total_bytes": 96 * 1024,
        "maximum_changed_lines": 400,
    }
    assert evolution_calls[0]["apply"] is True
    assert evolution_calls[0]["mine_events"] is False
    opportunity = evolution_calls[0]["opportunities"][0]
    assert opportunity["source_outcome_payload"]["source_trust"] == "system_verified"
    assert opportunity["code_evolution_proposal"]["schema_version"] == "code_implementation_proposal.v2"


def test_untrusted_incident_is_not_routed(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._repository_identity",
        lambda _root: {"ok": True, "base_commit": COMMIT},
    )
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=COMMIT),
    )
    try:
        report = process_system_code_incidents(runtime, scope=SCOPE, repo_root=tmp_path)
    finally:
        runtime.close()

    assert report == {"ok": True, "status": "idle", "processed": []}
