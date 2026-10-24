"""Synthetic release assertions and intent-bound ranking; no provider calls."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.answer_requirements import requested_attribute, supports_answer_requirements
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, CandidateRequest, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id="release-test", agent_id="agent", workspace_id="offline", user_id="owner")
QUERY = "项目Orion v2.3.4 部署状态是什么？"


@pytest.mark.parametrize("assertion", [
    "已部署成功", "部署失败", "正在滚动发布", "部署被阻塞", "已回滚",
    "尚未部署", "部署尚未完成", "发布已取消",
    "deployment succeeded", "deployment failed", "deployment is rolling",
    "deployment is blocked", "was rolled back", "was not deployed",
])
def test_release_status_accepts_factual_outcomes_without_success_bias(assertion):
    assert supports_answer_requirements(QUERY, f"项目Orion v2.3.4 {assertion}。")


@pytest.mark.parametrize("assertion", [
    "部署中", "发布中", "deploy failed", "is deploying", "deployment pending",
    "release_status: success", "release_status: failed", "release_status: rolling",
    "release_status: blocked", "release_status: rollback",
])
def test_common_release_status_forms_bind_an_independent_project_and_version(assertion):
    assert supports_answer_requirements("Alpha v1.15.26 部署状态", f"Alpha v1.15.26 {assertion}。")


@pytest.mark.parametrize("assertion", [
    "计划部署中", "如果发布中则暂停", "if deploy failed then retry",
    "might be deploying", "deployment pending?",
])
def test_common_release_forms_do_not_turn_plans_or_questions_into_facts(assertion):
    assert not supports_answer_requirements("Alpha v1.15.26 部署状态", f"Alpha v1.15.26 {assertion}")


@pytest.mark.parametrize("assertion", [
    "计划部署成功后再验收", "如果部署失败则回滚", "应该先确认已部署成功",
    "将会部署完成", "是否已部署成功？", "deployment will be completed",
    "if deployment failed then rollback", "should be deployed successfully",
])
def test_release_procedures_hypotheses_and_questions_are_not_execution(assertion):
    assert not supports_answer_requirements(QUERY, f"项目Orion v2.3.4 {assertion}")


@pytest.mark.parametrize("text", [
    "项目Orion v2.3.4 的部署说明；项目Boreal v2.3.4 已部署成功。",
    "Orion v2.3.4 deployment notes; Boreal v2.3.4 deployment completed successfully.",
    "项目Orion v2.3.4 的部署说明；项目Orion v2.3.5 已部署成功。",
    "项目Orion v2.3.4 部署状态未知。项目Orion v2.3.5 已部署成功。",
])
def test_release_assertion_cannot_borrow_another_project_or_version(text):
    assert not supports_answer_requirements(QUERY, text)


@pytest.mark.parametrize("query,text,expected", [
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 在2026-10-20已部署成功。", True),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 在2026-10-21已部署成功。", False),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 已部署成功。", False),
    ("项目Orion v2.3.4 在2026年10月20日的部署状态", "项目Orion v2.3.4 在2026-10-20已部署成功。", True),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "2026-10-20发布计划。项目Orion v2.3.4 在2026-10-21已部署成功。", False),
])
def test_absolute_release_dates_belong_to_the_same_assertion(query, text, expected):
    assert supports_answer_requirements(query, text) is expected


@pytest.mark.parametrize("query", [
    "部署验收的当前结果是什么？", "Orion v2.3.4 部署成功了吗？",
    "部署为什么失败？", "2026-10-20 部署验收记录", "发布是否已回滚？",
    "What is the deployment status?", "Was the release rolled back?",
])
def test_status_and_history_are_not_procedure_requests(query):
    assert not GovernedRecallEngine._is_procedure_query_without_episodic_cues(query)


@pytest.mark.parametrize("query", [
    "如何部署并验收？", "部署验收流程是什么？", "先做收据验收再跑一次nightly",
    "What are the deployment requirements?", "How should I deploy the release?",
])
def test_explicit_procedure_requests_keep_their_route(query):
    assert GovernedRecallEngine._is_procedure_query_without_episodic_cues(query)


@pytest.mark.parametrize("query", [QUERY, "如何部署并验收？"])
def test_episodic_type_alone_never_erases_independent_vector_evidence(query):
    item = RecordEnvelope.create(kind="memory", title="Synthetic release observation",
        summary="项目Orion v2.3.4 已部署成功。", content={"text": "项目Orion v2.3.4 已部署成功。"},
        scope=SCOPE, source="synthetic.release", source_id="offline", meta={"memory_type": "episodic"})
    engine = GovernedRecallEngine(store=None, candidate_source=None)
    engine.relevance_admission = None
    ref = engine._record_key(item)
    hints = {ref: {"vector_score": .9, "dense_vector_score": .9, "lexical_score": .5}}
    _, state = engine._fuse_and_pool_items(items=[item], query=query,
        request=CandidateRequest.create(query=query, scope=SCOPE), task_context={},
        scope_group_by_exact={ExactScope.from_scope(SCOPE): 0}, component_hints_by_ref=hints,
        identity_evidence_by_ref={}, base_ids={ref}, memory_usage_adjustments={})
    assert state["detail_by_ref"][ref]["contributions"]["vector"] > 0
    assert hints[ref]["dense_vector_score"] == .9


@pytest.mark.parametrize("text,expected", [
    ("项目Orion合同金额说明：具体金额详见附件。", False),
    ("项目Orion合同金额为1200元。", True),
    ("项目Orion合同金额详见附件，预付款为1200元。", False),
])
def test_contract_role_requires_its_own_actual_amount(text, expected):
    assert supports_answer_requirements("项目Orion合同总金额是多少？", text) is expected


@pytest.mark.parametrize("query,attribute", [
    ("最近已授权任务、进展、待验收", "task_status"),
    ("最近任务进展和待验收", "task_status"),
    ("为什么 eimemory-rpc 部署失败", ""),
    ("Why did the deployment fail?", ""),
    ("项目Orion v2.3.4 验收状态是什么？", "release_status"),
])
def test_release_shape_preserves_task_progress_and_causal_requests(query, attribute):
    assert requested_attribute(query) == attribute


@pytest.mark.parametrize("query,evidence,attribute,supported", [
    ("deployment rollback", "deployment rollback runbook", "", True),
    ("部署回滚", "部署回滚操作手册", "", True),
    ("deployment failed", "deployment failed troubleshooting guide", "", True),
    ("部署失败", "部署失败排查手册", "", True),
    ("部署回滚了吗？", "已回滚。", "release_status", True),
    ("部署回滚状态", "正在回滚。", "release_status", True),
    ("部署回滚状态", "回滚中。", "release_status", True),
    ("部署回滚了吗？", "部署回滚操作手册", "release_status", False),
    ("部署是否失败？", "部署失败。", "release_status", True),
    ("部署失败了吗？", "计划部署成功后再验收。", "release_status", False),
    ("Was the deployment rolled back?", "The deployment was rolled back.", "release_status", True),
    ("Was the deployment rolled back?", "deployment rollback runbook", "release_status", False),
    ("Has the deployment failed?", "The deployment failed.", "release_status", True),
    ("Did the deployment fail?", "If deployment failed, retry.", "release_status", False),
    ("deployment rollback status", "The deployment was rolled back.", "release_status", True),
    ("部署回滚状态", "已回滚。", "release_status", True),
])
def test_release_topics_do_not_impose_state_answer_requirements(query, evidence, attribute, supported):
    assert requested_attribute(query) == attribute
    assert supports_answer_requirements(query, evidence) is supported


@pytest.mark.parametrize("text", [
    "项目Orion v2.3.4 是否已部署成功",
    "项目Orion v2.3.4 已部署成功，还是部署失败？",
    "如果批准，项目Orion v2.3.4 已部署成功。",
    "If approved, Orion v2.3.4 will be deployed.",
    "项目Orion v2.3.4 尚未确认是否已部署成功。",
    "项目Orion v2.3.4 可能已经部署成功。",
    "Orion v2.3.4 might have been deployed.",
])
def test_uncertainty_and_whole_sentence_questions_cannot_become_release_facts(text):
    assert not supports_answer_requirements(QUERY, text)


@pytest.mark.parametrize("query,text,expected", [
    (QUERY, "项目Orion v2.3.4 已部署成功！", True),
    (QUERY, "项目Orion v2.3.4\n部署失败。", True),
    (QUERY, "项目Orion v2.3.4\n\n部署失败。", False),
    (QUERY, "项目Orion v2.3.4\n其他部署记录\n部署失败。", False),
    (QUERY, "项目Orion v2.3.4 已回滚至v2.3.3。", True),
    ("项目Orion v2.3.3 部署状态", "项目Orion v2.3.4 已回滚至v2.3.3。", False),
    (QUERY, "Orion deployed v2.3.5 instead of v2.3.4 in production.", False),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 2026-10-20\n部署失败。", True),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 2026-10-20\n2026-10-21已部署成功。", False),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 2026-10-20记录提到在2026-10-21已部署成功。", False),
])
def test_release_binding_respects_heading_boundaries_and_observation_targets(query, text, expected):
    assert supports_answer_requirements(query, text) is expected


def _ranked_memory(text: str, memory_type: str) -> RecordEnvelope:
    return RecordEnvelope.create(kind="memory", title="Release evidence", summary=text,
        content={"text": text}, scope=SCOPE, source="synthetic.release", source_id="offline",
        meta={"memory_type": memory_type, "quality": {"capture_decision": "accept"}})


@pytest.mark.parametrize("query,scores,preferred_first,exact", [
    ("如何部署并验收？", (.7, .4), False, False),
    ("如何部署并验收？", (.7, .7), True, False),
    ("部署验收的当前结果是什么？", (.7, .7), False, False),
    ("上次如何部署并验收？", (.7, .7), False, False),
    ("如何部署并验收？", (.7, .7), False, True),
])
def test_durable_guidance_breaks_only_explicit_procedure_equal_score_ties(tmp_path, query, scores, preferred_first, exact):
    with closing(RuntimeStore(tmp_path)) as store:
        observed = _ranked_memory("项目Orion v2.3.4 已部署成功，验收通过。", "episodic")
        if exact:
            observed.title = query
        observed = store.append(observed)
        preference = store.append(_ranked_memory("项目Orion v2.3.4 已部署成功，提交部署验收时需保留收据。", "preference"))
        engine = GovernedRecallEngine(store=store, candidate_source=None)
        engine.relevance_admission = None
        records = [observed, preference]
        refs = [engine._record_key(item) for item in records]
        selected, report = engine._select_post_fusion_items(records, query=query, limit=2,
            fusion_state={"detail_by_ref": {ref: {"score": score} for ref, score in zip(refs, scores)},
                          "evidence_by_ref": {ref: {"exact_title" if exact and ref == refs[0] else "keyword_exact"}
                                              for ref in refs},
                          "authorized_exact_refs": set(refs)},
            component_hints_by_ref={ref: {"lexical_score": .5} for ref in refs})
        expected = [preference, observed] if preferred_first else [observed, preference]
        assert [item.record_id for item in selected] == [item.record_id for item in expected]
        assert report["delivery_priority_applied"] is preferred_first
        assert report["selected_count"] == 2
        assert not report["dropped_reasons"].get("authority_changed")


@pytest.mark.parametrize("query,text,memory_type,expected", [
    *[(QUERY, f"项目Orion v2.3.4 {status}。", "fact", True) for status in (
        "已部署成功", "部署失败", "正在滚动发布", "部署被阻塞", "已回滚",
        "部署中", "发布中", "deploy failed", "is deploying", "deployment pending", "尚未部署",
    )],
    (QUERY, "项目Orion v2.3.4 已回滚。", "episodic", True),
    (QUERY, "项目Orion v2.3.4 部署失败。", "preference", False),
    (QUERY, "项目Boreal v2.3.4 部署失败。", "fact", False),
    (QUERY, "项目Orion v2.3.5 部署失败。", "fact", False),
    (QUERY, "项目Orion v2.3.4 计划已部署成功后验收。", "fact", False),
    (QUERY, "项目Orion v2.3.4 是否已回滚？", "fact", False),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 在2026-10-20已回滚。", "fact", True),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 在2026-10-21已回滚。", "fact", False),
    ("项目Orion v2.3.4 在2026-10-20的部署状态", "项目Orion v2.3.4 已回滚。", "fact", False),
])
def test_native_memory_api_release_status_survives_task_gate(tmp_path, query, text, memory_type, expected):
    """Exercise pre-filters, authority hydration and final delivery, not just a predicate."""
    with closing(RuntimeStore(tmp_path)) as store:
        record = store.append(_ranked_memory(text, memory_type))

        class Source:
            name = "offline-release-observation"

            def search(self, request):
                return CandidateBatch(hits=(CandidateHit(
                    ref=CandidateRef(record.record_id, ExactScope.from_scope(record.scope), record.source_id),
                    source_rank=1, source_score=.9,
                    component_hints={"lexical_score": .9, "vector_score": .9},
                ),))

        engine = GovernedRecallEngine(store=store, candidate_source=Source())
        engine.relevance_admission = None
        bundle = MemoryAPI(store, recall_engine=engine).recall(
            query=query, scope=asdict(SCOPE), limit=5,
            task_context={"exact_scope_only": True, "source_ids": ["offline"],
                          "target_source_id": "offline"},
        )
        assert [item.record_id for item in bundle.items] == ([record.record_id] if expected else [])
