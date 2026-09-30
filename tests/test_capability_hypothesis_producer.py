from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability import hypothesis_producer as producer
from eimemory.governance.capability import capability_hypotheses

SCOPE = {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}


def _plan(*bindings: str) -> dict:
    return {
        "projection_digest": "p" * 64,
        "input_watermark": "w" * 64,
        "work_items": [
            {
                "work_item_id": f"wi-{binding}",
                "status": "blocked",
                "capability_id": "code.implementation",
                "capability_revision_id": "code.implementation:v2",
                "provider_binding_id": binding,
                "reason": "hypothesis_missing_or_ambiguous",
                "detail": {"candidate_hypothesis_count": 0},
            }
            for binding in bindings
        ]
        + [
            {
                "work_item_id": "wi-other",
                "status": "blocked",
                "capability_id": "x",
                "capability_revision_id": "x:v1",
                "provider_binding_id": "b-x",
                "reason": "profile_selected_evaluation_case_missing",
                "detail": {"hypothesis_id": "h"},
            }
        ],
    }


def test_real_gap_without_registered_link_reports_reason_and_writes_nothing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = producer.produce_capability_hypotheses(
            runtime, profile_key="l5.default", runtime_scope=SCOPE, plan=_plan("b1", "b2")
        )
        assert report["ok"] is True
        assert report["gap_count"] == 2
        assert report["revision_count"] == 1
        assert report["created"] == []
        assert report["skipped"][0]["reason"] == "no_applicable_knowledge_link_for_gap_revision"
        assert report["skipped"][0]["provider_binding_ids"] == ["b1", "b2"]
        assert report["status"] == "no_eligible_evidence"
        assert capability_hypotheses.list_capability_hypotheses(runtime, runtime_scope=SCOPE) == []
    finally:
        runtime.close()


def test_ambiguous_links_are_not_chosen(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(producer, "_applicable_link_rows", lambda *a, **k: [{"link_id": "l1"}, {"link_id": "l2"}])
    runtime = Runtime.create(root=tmp_path)
    try:
        report = producer.produce_capability_hypotheses(
            runtime, profile_key="l5.default", runtime_scope=SCOPE, plan=_plan("b1")
        )
    finally:
        runtime.close()
    assert report["created"] == []
    assert report["skipped"][0]["reason"] == "ambiguous_applicable_knowledge_links"


def test_single_applicable_link_creates_hypothesis_with_gap_provenance(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        producer, "_applicable_link_rows", lambda *a, **k: [{"link_id": "link-1", "link_digest": "d" * 64}]
    )
    calls: list[dict] = []

    class Rec:
        record_id = "capability_hypothesis_x"
        status = "candidate"
        content = {"blocked_reasons": []}

    def fake_create(runtime, **kwargs):
        calls.append(kwargs)
        return Rec()

    monkeypatch.setattr(capability_hypotheses, "create_capability_hypothesis", fake_create)
    runtime = Runtime.create(root=tmp_path)
    try:
        report = producer.produce_capability_hypotheses(
            runtime, profile_key="l5.default", runtime_scope=SCOPE, plan=_plan("b1", "b2")
        )
    finally:
        runtime.close()
    assert report["status"] == "produced"
    assert [item["hypothesis_id"] for item in report["created"]] == ["capability_hypothesis_x"]
    assert len(calls) == 1
    call = calls[0]
    assert call["link_id"] == "link-1"
    evidence = call["expected_metric"]["gap_evidence"]
    assert evidence["work_item_ids"] == ["wi-b1", "wi-b2"]
    assert evidence["projection_digest"] == "p" * 64
    assert evidence["input_watermark"] == "w" * 64
    assert call["candidate_bounds"]["max_changes"] == 0


def test_producer_flag_disables(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIMEMORY_CAPABILITY_HYPOTHESIS_PRODUCER", "0")
    runtime = Runtime.create(root=tmp_path)
    try:
        report = producer.produce_capability_hypotheses(
            runtime, profile_key="l5.default", runtime_scope=SCOPE, plan=_plan("b1")
        )
    finally:
        runtime.close()
    assert report["status"] == "disabled"
    assert report["created"] == []


def test_revocation_is_append_only_and_scoped(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        assert producer.revoked_hypothesis_ids(runtime, runtime_scope=SCOPE) == set()
        producer.revoke_produced_hypothesis(
            runtime, runtime_scope=SCOPE, hypothesis_id="capability_hypothesis_x", reason="wrong link"
        )
        assert producer.revoked_hypothesis_ids(runtime, runtime_scope=SCOPE) == {"capability_hypothesis_x"}
        other = {**SCOPE, "user_id": "someone-else"}
        assert producer.revoked_hypothesis_ids(runtime, runtime_scope=other) == set()
    finally:
        runtime.close()
