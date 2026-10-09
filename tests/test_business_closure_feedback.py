from __future__ import annotations

import pytest
from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge.projectors import project_operational_knowledge, stable_projection_id
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.records import ScopeRef
from eimemory.governance.closed_loop import evaluate_result


def test_negative_business_outcome_overrides_execution_and_success_label():
    result = evaluate_result(
        None, {"ok": True, "status": "ok", "outcome": "bad"},
        source_record=SimpleNamespace(meta={"primary_label": "success"}, content={}),
    )
    assert result["ok"] is False
    assert result["primary_label"] == "bad"


@pytest.mark.parametrize("outcome,success", [("good", True), ("bad", False),
                                          ("uncertain", False), ("verification_missing", False)])
def test_raw_business_outcome_controls_persisted_feedback_and_reward(tmp_path, outcome, success):
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "audit", "workspace_id": "feedback"}
    try:
        event = runtime.record_event({"id": "business-event", "event_type": "task"}, scope=scope)
        result = runtime.record_outcome(event["id"], {"outcome": outcome}, scope=scope)
        loop = result["closed_loop"]
        assert loop["eval"]["ok"] is success
        assert loop["eval"]["outcome_status"] == outcome
        feedback = runtime.store.get_by_id(loop["memory"]["record_id"], scope=scope)
        assert feedback.content["evaluation"]["ok"] is success
        transition = runtime.store.get_by_id(loop["rl"]["transition_record_id"], scope=scope)
        assert (transition.content["reward"]["reward"] > 0) is success
    finally:
        runtime.close()


@pytest.mark.parametrize("projection", ["jsonl", "markdown"])
def test_committed_terminal_projection_failure_still_feeds_learning(tmp_path, monkeypatch, projection):
    runtime = Runtime.create(root=tmp_path)
    service = AgentRuntimeMemoryService(runtime)
    scope = {"agent_id": "audit", "workspace_id": "terminal"}

    def fail(*args, **kwargs):
        raise OSError("projection unavailable")

    try:
        with monkeypatch.context() as fault:
            if projection == "jsonl":
                fault.setattr(runtime.store, "_flush_committed_exports", fail)
            else:
                fault.setattr("eimemory.storage.runtime_store.export_record_markdown", fail)
            result = service.record_terminal(
                channel="codex", scope=scope, end_kind="stop", session_id="session",
                event_id="turn", task_type="code.fix", success=False,
                result="verification failed", receipt_ids=[],
            )
        assert result["ok"] is True
        assert result["outcome"]["closed_loop"]["rl"]["ok"] is True
        assert result["outcome"]["closed_loop"]["eval"]["ok"] is False
        assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM event_outcomes").fetchone()[0] == 1
        assert runtime.store.flush_exports()["remaining"] == 0
        retried = service.record_terminal(
            channel="codex", scope=scope, end_kind="stop", session_id="session",
            event_id="turn", task_type="code.fix", success=False,
            result="verification failed", receipt_ids=[],
        )
        assert retried["outcome_trace"]["idempotent"] is True
        assert retried["outcome"]["closed_loop"]["rl"]["idempotent"] is True
    finally:
        runtime.close()


def test_compiler_lineage_projects_exact_claim_versions_and_rejects_changes(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="audit", workspace_id="knowledge")
    try:
        claim = ClaimCard(
            claim_card_id="claim-audit", paper_source_id="paper-audit", paper_extract_id="extract-audit",
            claim_text="OpenClaw runtime recall must preserve canonical source provenance for operational decisions.",
            confidence=.95,
        ).to_record(scope=scope)
        claim.source_id = "authorized-source"
        runtime.store.append(claim)
        compilation = compile_paper_knowledge(paper_source_id="paper-audit", claim_records=[claim])
        page = compilation.to_records(scope=scope)[0]
        assert page.source_id == claim.source_id
        runtime.store.append(page)
        report = project_operational_knowledge(runtime.store, scope=scope)
        assert stable_projection_id(page) in report["projected_ids"]
        claim.summary = "Changed claim version"
        claim.touch()
        runtime.store.rewrite(claim)
        report = project_operational_knowledge(runtime.store, scope=scope)
        assert {"record_id": page.record_id, "reason": "support_changed"} in report["skipped"]
        wrong_scope = compilation.to_records(scope=ScopeRef(agent_id="other"))[0]
        assert "supporting_claim_refs" not in wrong_scope.content
        ungrounded = compile_paper_knowledge(paper_source_id="paper-audit", claims=[claim.summary])
        assert "supporting_claim_refs" not in ungrounded.to_records(scope=scope)[0].content
    finally:
        runtime.close()
