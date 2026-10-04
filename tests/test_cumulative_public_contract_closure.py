from contextlib import closing
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning import skill_validation
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_original_status_query_requires_task_evidence_without_opening_incident_lane(tmp_path):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        scope = ScopeRef(agent_id="hongtu", workspace_id="embodied")
        quality = dict(importance=.95, confidence=.95, freshness=1., reuse_potential=.95,
                       salience_score=.95, quality_tier="core", capture_decision="accept")
        for title, text, memory_type in [
            ("Project report old incident", "Project status report marker from an old incident should stay hidden.", "incident_report"),
            ("Project report fact", "Project status report marker from a durable fact should be visible.", "durable_fact"),
        ]:
            runtime.store.append(RecordEnvelope.create(kind="memory", title=title, summary=text,
                scope=scope, source="openclaw.agent_end",
                content={"text": text, "memory_type": memory_type},
                meta={"memory_type": memory_type, "quality": quality}))
        bundle = runtime.memory.recall(query="write a project status report marker",
            scope={"agent_id": "hongtu", "workspace_id": "embodied"},
            task_context={"task_type": "chat.reply"}, limit=20)
        assert bundle.items == []
        assert bundle.explanation["recall_intent"]["name"] == "task_recall"
        assert bundle.explanation["recall_filters"]["blocked_counts"]["task_evidence_missing"] == 1
        assert "incident_report" in bundle.explanation["recall_filters"]["blocked_recall_lanes"]


@pytest.mark.parametrize("value", [object(), float("nan"), ("unsupported",)])
def test_runtime_skill_validation_rejects_unsupported_input_before_normalizing_or_writing(tmp_path, value):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        before = runtime.store.count_records()
        report = runtime.validate_skill_candidate(candidate={"unknown": value}, persist=True)
        assert report["ok"] is False and report["persisted"] is False
        assert report["reasons"] == ["unsupported_skill_safety_input"]
        assert runtime.store.count_records() == before


def test_runtime_skill_validation_screens_original_record_and_unknown_fields(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(skill_validation, "source_trust_decision_from_payload",
                        lambda _: SimpleNamespace(score=.9))
    monkeypatch.setattr(skill_validation, "revalidate_source_trust_decision",
                        lambda *args, **kwargs: SimpleNamespace(score=.9))
    def recorder(payload, **kwargs):
        seen.append(payload["text"])
        return {"capability_allowed": False}
    monkeypatch.setattr(skill_validation, "evaluate_knowledge_safety", recorder)
    with closing(Runtime.create(root=tmp_path)) as runtime:
        scope = ScopeRef(tenant_id="synthetic", agent_id="fixture", workspace_id="skill", user_id="owner")
        record = runtime.store.append(RecordEnvelope.create(kind="skill_candidate",
            title="ORIGINAL_TITLE", detail="ORIGINAL_DETAIL", source="synthetic.fixture", scope=scope,
            content={"title": "shadow title", "unknown": {"tail": "UNKNOWN_TAIL"},
                     "steps": ["STEP_ONE", "STEP_TWO"], "trigger_conditions": ["TRIGGER"],
                     "acceptance_criteria": ["ACCEPT"], "risk_level": "low"}))
        before = runtime.store.get_by_exact_ref(record.record_id, scope=scope, source_id=record.source_id).to_dict()
        report = runtime.validate_skill_candidate(candidate_id=record.record_id,
            scope=dict(tenant_id="synthetic", agent_id="fixture", workspace_id="skill", user_id="owner"),
            persist=False)
        assert report["pass"] is False and report["persisted"] is False
        assert len(seen) == 1
        assert all(marker in seen[0] for marker in ["ORIGINAL_TITLE", "ORIGINAL_DETAIL", "UNKNOWN_TAIL", "STEP_ONE", "STEP_TWO"])
        assert runtime.store.get_by_exact_ref(record.record_id, scope=scope, source_id=record.source_id).to_dict() == before
