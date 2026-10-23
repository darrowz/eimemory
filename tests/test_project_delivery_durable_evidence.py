from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.engine import GovernedRecallEngine


SCOPE = ScopeRef(tenant_id="default", agent_id="hongrui", workspace_id="delivery", user_id="darrow")


def _memory(title: str, summary: str, *, memory_type: str = "preference") -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=title,
        summary=summary,
        scope=SCOPE,
        source="operator.correction",
        meta={"memory_type": memory_type},
    )


def test_project_delivery_durable_evidence_requires_procedure_phrasing() -> None:
    engine = object.__new__(GovernedRecallEngine)
    query = "部署切换，跑一遍nightly并验收"
    target = _memory(
        "提交部署验收偏好",
        "提交部署验收；分层业务验收；真实读回版本、收据、健康与召回",
    )
    draft = _memory("Codex instruction", "未验收草稿，请勿提交部署", memory_type="instruction")
    soft = _memory("鸿哥偏好", "集中完成当前索引构建并以实际状态验收。")
    order = _memory("外部订单", "以后外部订单先对需求清单逐条验收，再交付。")

    assert engine._is_project_delivery_durable_evidence(
        query, target, engine._record_evidence_text(target)
    )
    assert not engine._is_project_delivery_durable_evidence(
        query, draft, engine._record_evidence_text(draft)
    )
    assert not engine._is_project_delivery_durable_evidence(
        query, soft, engine._record_evidence_text(soft)
    )
    assert engine._is_project_delivery_durable_evidence(
        query, order, engine._record_evidence_text(order)
    )


def test_procedure_query_without_episodic_cues() -> None:
    engine = object.__new__(GovernedRecallEngine)
    assert engine._is_procedure_query_without_episodic_cues(
        "先做收据验收再跑一次nightly Context entities: hermes completed turn"
    )
    assert not engine._is_procedure_query_without_episodic_cues("刚才部署验收了吗")
    assert not engine._is_procedure_query_without_episodic_cues("今天天气怎么样")
