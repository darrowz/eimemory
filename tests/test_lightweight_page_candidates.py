"""Offline page-pooling regressions; no model, index, or remote service."""
from __future__ import annotations

from dataclasses import replace

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRequest, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.evidence_fragments import POLICY, evidence_fragments
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text


SCOPE = ScopeRef(tenant_id="page-test", agent_id="agent", workspace_id="workspace", user_id="user")
QUERY = "项目Nova服务价格是多少？"


def _record(text, *, page="contract", source_id="source", scope=SCOPE):
    return RecordEnvelope.create(
        kind="memory", title="Contract ledger", summary=text, content={"text": text, "page_id": page},
        source_id=source_id, scope=scope, meta={"memory_type": "fact"},
    )


def _hints(item, cosine):
    fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
    return {"dense_vector_score": cosine, "vector_score": cosine,
            "fragment_policy": POLICY, "evidence_fragment_id": fragment["id"]}


def _fuse(items, hints):
    engine = GovernedRecallEngine(store=None, candidate_source=None)
    engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
    references = {engine._record_key(item) for item in items}
    pooled, state = engine._fuse_and_pool_items(
        items=items, query=QUERY, request=CandidateRequest.create(query=QUERY, scope=SCOPE),
        task_context={}, scope_group_by_exact={ExactScope.from_scope(SCOPE): 0},
        component_hints_by_ref={engine._record_key(item): hints[item.record_id] for item in items},
        identity_evidence_by_ref={}, base_ids=references, memory_usage_adjustments={},
    )
    return pooled, state, engine.relevance_admission


def test_lightweight_page_keeps_answering_sibling_after_dense_leader_fails_attribute():
    topic = _record("项目Nova服务价格说明：需要先完成签署流程，具体价格尚未记录。")
    answer = _record("项目Nova服务价格为1200元。")
    hints = {topic.record_id: _hints(topic, .97), answer.record_id: _hints(answer, .78)}
    pooled, state, gate = _fuse([topic, answer], hints)
    chosen, report = gate.select(
        pooled, query=QUERY, limit=5, validate=lambda item: item.status == "active",
        hints_for=lambda item: hints[item.record_id], backend_available=True,
    )
    assert chosen == [answer]
    assert report["dropped_reasons"]["requested_attribute_missing"] == 1
    assert len(pooled) == 2
    assert set(state["authorized_exact_refs"]) == {
        GovernedRecallEngine._record_key(topic), GovernedRecallEngine._record_key(answer),
    }


def test_lightweight_final_page_representative_uses_own_admitted_evidence():
    first = _record("项目Nova合同金额为1200元。")
    second = _record("项目Nova合同金额为1200元，另附已签字的审批单。")
    other = _record("项目Nova合同金额为1200元，另附电子发票。", page="invoice")
    hints = {item.record_id: _hints(item, score) for item, score in
             ((first, .80), (second, .79), (other, .79))}
    chosen, report = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [first, second, other], query=QUERY, limit=5, validate=lambda _: True,
        hints_for=lambda item: hints[item.record_id], backend_available=True,
    )
    assert chosen == [first, other]
    assert report["dropped_reasons"]["same_page_representative"] == 1


def test_lightweight_page_bound_preserves_diversity_and_reports_truncation():
    items = [_record(f"项目Nova合同金额为{1200 + index}元。") for index in range(6)]
    other = _record("项目Nova合同金额为1200元。", page="other-page")
    items.append(other)
    hints = {item.record_id: _hints(item, .95 - index * .01) for index, item in enumerate(items)}
    pooled, state, _ = _fuse(items, hints)
    assert len(pooled) == 4
    assert other in pooled
    assert sum(item.content["page_id"] == "contract" for item in pooled) == 3
    assert state["page_candidate_budget_dropped_count"] == 3


def test_lightweight_same_page_name_cannot_merge_permission_or_source_partitions():
    first = _record("项目Nova合同金额为1200元。")
    other_source = _record("项目Nova合同金额为1200元。", source_id="other-source")
    other_scope = _record("项目Nova合同金额为1200元。", scope=replace(SCOPE, user_id="other"))
    records = [first, other_source, other_scope]
    hints = {item.record_id: _hints(item, .8) for item in records}
    gate = LightweightAdmission(LightweightConfig(enabled=True))
    chosen, _ = gate.select(records, query=QUERY, limit=5, validate=lambda _: True,
                           hints_for=lambda item: hints[item.record_id], backend_available=True)
    assert set(item.record_id for item in chosen) == set(item.record_id for item in records)
    chosen, _ = gate.select(records, query=QUERY, limit=5, validate=lambda item: item.scope == SCOPE,
                           hints_for=lambda item: hints[item.record_id], backend_available=True)
    assert set(item.record_id for item in chosen) == {first.record_id, other_source.record_id}


def test_exact_title_lookup_keeps_one_representative_per_authorized_page():
    first = _record("First original contract fragment.")
    sibling = _record("Second original contract fragment.")
    other_page = _record("Separate invoice fragment.", page="invoice")
    other_source = _record("Other source's contract fragment.", source_id="other-source")
    chosen, report = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [first, sibling, other_page, other_source], query="Contract ledger", limit=5,
        validate=lambda _: True,
    )
    assert chosen == [first, other_page, other_source]
    assert report["mode"] == "identity_lookup"
    assert report["dropped_reasons"]["same_page_representative"] == 1
    assert report["candidate_stages"]["identity_match_count"] == 4
    assert report["candidate_stages"]["page_representative_count"] == 3
    assert report["candidate_stages"]["final_count"] == 3
