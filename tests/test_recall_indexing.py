from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import (
    RecallIndexDocument,
    build_recall_index_document,
    is_outcome_pollution_record,
    classify_recall_lane,
    classify_recall_visibility,
    classify_source_class,
)


SCOPE = ScopeRef(agent_id="main", workspace_id="recall")


def _record(*, kind: str, title: str, source: str, **kwargs) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind=kind,
        title=title,
        scope=SCOPE,
        summary=kwargs.get("summary", title),
        source=source,
        content=kwargs.get("content", {}),
        meta=kwargs.get("meta", {}),
    )


def test_recall_index_classifies_openclaw_agent_outcome_as_operational_non_default() -> None:
    record = _record(
        kind="memory",
        title="OpenClaw agent outcome",
        source="openclaw.agent_end",
        meta={"memory_type": "conversation"},
        content={"text": "OpenClaw outcome summary for current task."},
    )
    doc = build_recall_index_document(record)

    assert isinstance(doc, RecallIndexDocument)
    assert doc.lane == "operational"
    assert doc.visibility in {"evidence_only", "report_only"}
    assert classify_recall_lane(record) == "operational"
    assert classify_recall_visibility(record) in {"evidence_only", "report_only"}
    assert classify_source_class(record) == "agent_outcome"


def test_recall_index_classifies_tool_call_json_as_operational_non_default() -> None:
    record = _record(
        kind="memory",
        title="OpenClaw agent outcome",
        source="openclaw.agent_end",
        summary="tool call transcript",
        content={
            "text": (
                '{"type":"toolCall","name":"message","arguments":{"message":"test"}}'
            )
        },
        meta={"memory_type": "conversation"},
    )
    doc = build_recall_index_document(record)

    assert doc.lane == "operational"
    assert doc.source_class == "tool_call"
    assert doc.visibility in {"evidence_only", "report_only"}


def test_recall_index_empty_projection_does_not_force_report_visibility() -> None:
    record = _record(
        kind="memory",
        title="OpenClaw agent outcome",
        source="openclaw.agent_end",
        summary="普通执行过程记录。",
        meta={"memory_type": "conversation"},
    )

    assert classify_recall_lane(record) == "operational"
    assert classify_recall_visibility(record) == "evidence_only"


def test_recall_index_classifies_operator_preference_memory_as_primary_default() -> None:
    record = _record(
        kind="memory",
        title="Communication preference",
        source="operator.correction",
        summary="用户偏好：先给结论再给证据。",
        content={"text": "用户偏好：先给结论再给证据。", "memory_type": "preference"},
        meta={"memory_type": "preference"},
    )

    assert classify_recall_lane(record) == "primary"
    assert classify_recall_visibility(record) == "default"
    assert classify_source_class(record) == "preference"
    assert build_recall_index_document(record).source_class == "preference"


def test_recall_index_keeps_actionable_agent_outcome_as_operational_evidence() -> None:
    record = _record(
        kind="memory",
        title="OpenClaw agent outcome",
        source="openclaw.agent_end",
        summary="已记到长期记忆。以后外部订单先对需求清单逐条验收，再交付。",
        meta={"memory_type": "conversation"},
    )
    doc = build_recall_index_document(record)

    assert doc.source_class == "agent_outcome"
    assert doc.lane == "operational"
    assert doc.visibility == "evidence_only"


def test_recall_index_keeps_openclaw_fact_memory_with_domain_title_in_default_recall() -> None:
    record = _record(
        kind="memory",
        title="Vision object preference",
        source="openclaw.agent_end",
        summary="Darrow asked eibrain to describe real objects on the desk.",
        meta={"memory_type": "fact"},
    )
    doc = build_recall_index_document(record)

    assert doc.source_class == "default"
    assert doc.lane == "primary"
    assert doc.visibility == "default"
    assert is_outcome_pollution_record(record) is False


def test_outcome_pollution_classifier_does_not_block_research_outcomes() -> None:
    outcomes_page = _record(
        kind="knowledge_page",
        title="Treatment outcomes evidence",
        source="research.outcomes",
        summary="Clinical outcome evidence belongs in the knowledge lane.",
    )
    modeling_page = _record(
        kind="knowledge_page",
        title="Agent outcome modeling methods",
        source="papers.agent-evaluation",
        summary="A research paper about outcome modeling is not a terminal event.",
    )

    assert is_outcome_pollution_record(outcomes_page) is False
    assert is_outcome_pollution_record(modeling_page) is False


def test_recall_index_classifies_knowledge_page_and_claim_card_as_knowledge_default() -> None:
    knowledge_page = _record(
        kind="knowledge_page",
        title="Knowledge summary",
        source="eimemory.knowledge.synthesis",
        summary="这是一个可检索的知识页面。",
    )
    claim_card = _record(
        kind="claim_card",
        title="Atomic claim",
        source="eimemory.knowledge.claims",
        summary="Claim card should be in knowledge lane.",
    )

    assert classify_recall_lane(knowledge_page) == "knowledge"
    assert classify_recall_visibility(knowledge_page) == "default"
    assert classify_recall_lane(claim_card) == "knowledge"
    assert classify_recall_visibility(claim_card) == "default"
    assert build_recall_index_document(knowledge_page).source_class == "knowledge"
    assert build_recall_index_document(claim_card).source_class == "knowledge"


def test_recall_index_classifies_raw_chunk_as_raw_evidence_only() -> None:
    record = _record(
        kind="raw_chunk",
        title="Raw user chunk",
        source="raw_chunk",
        summary="用户原始音频片段转录。",
    )

    doc = build_recall_index_document(record)
    assert doc.lane == "raw"
    assert doc.visibility == "evidence_only"
    assert doc.source_class == "default"


def test_recall_index_classifies_reflection_as_operational_report_only() -> None:
    record = _record(
        kind="reflection",
        title="OpenClaw reflection report",
        source="eimemory.rule_evolution",
        summary="用于系统反思与治理的执行记录。",
    )

    assert classify_recall_lane(record) == "operational"
    assert classify_recall_visibility(record) == "report_only"
    assert build_recall_index_document(record).visibility == "report_only"


def test_recall_index_classifies_news_digest_as_news_default_or_report_only() -> None:
    record = _record(
        kind="news",
        title="Daily AI brief",
        source="eimemory.news.collect",
        summary="News digest: 产业动态与产品发布摘要。",
        content={"item_url": "https://example.test/news"},
    )

    doc = build_recall_index_document(record)
    assert doc.lane == "news"
    assert doc.source_class == "news"
    assert doc.visibility in {"default", "report_only"}


def test_recall_index_document_contains_required_fields() -> None:
    record = _record(
        kind="memory",
        title="Preference index smoke test",
        source="openclaw.message_received",
        content={"text": "偏好优先"},
        meta={"memory_type": "preference"},
    )
    document = build_recall_index_document(record)

    assert document.record_id == record.record_id
    assert document.scope["tenant_id"] == "default"
    assert document.kind == "memory"
    assert document.title_text == "Preference index smoke test"
    assert document.body_text
    assert isinstance(document.anchor_terms, tuple)
    assert isinstance(document.quality_score, float)
    assert document.updated_at == record.time.updated_at
