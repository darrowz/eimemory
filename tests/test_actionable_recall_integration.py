from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_project_delivery_recall_routes_away_from_research_pages(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "delivery", "user_id": "darrow"}
    scope_ref = ScopeRef.from_dict(scope)
    project_memory = runtime.memory.ingest(
        text="UUMit 外部订单交付品质：海报 v2 必须按需求清单逐项验收，保留证据，不要臆测。",
        memory_type="preference",
        title="UUMit delivery acceptance checklist",
        scope=scope,
        source="operator.correction",
        force_capture=True,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="knowledge_page",
            title="SIREN multimodal recommendation research",
            summary="SIREN 论文讨论多模态推荐、品质建模和交付系统，但不涉及 UUMit 海报 v2 验收。",
            scope=scope_ref,
            source="eimemory.knowledge.compiler",
            meta={"page_type": "paper"},
        )
    )

    bundle = runtime.memory.recall(
        query="UUMit 交付品质 海报 v2",
        scope=scope,
        task_context={"task_type": "cli.recall"},
        limit=5,
    )

    assert bundle.items
    assert bundle.items[0].record_id == project_memory.record_id
    assert all("SIREN" not in item.title for item in bundle.items)
    assert bundle.explanation["recall_intent"]["name"] == "project_delivery"
    assert bundle.explanation["recall_intent"]["memory_cube"] == "project"


def test_research_intent_keeps_knowledge_pages_available(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "research", "user_id": "darrow"}
    scope_ref = ScopeRef.from_dict(scope)
    page = RecordEnvelope.create(
        kind="knowledge_page",
        title="Graphiti temporal knowledge graph paper",
        summary="Graphiti 论文介绍 temporal knowledge graph for LLM agent memory.",
        scope=scope_ref,
        source="eimemory.knowledge.compiler",
        meta={"page_type": "paper"},
    )
    runtime.store.append(page)

    bundle = runtime.memory.recall(
        query="Graphiti temporal knowledge graph 论文",
        scope=scope,
        task_context={"task_type": "research"},
        limit=5,
    )

    assert bundle.items
    assert bundle.items[0].record_id == page.record_id
    assert bundle.explanation["recall_intent"]["name"] == "research"


def test_research_context_keeps_project_paper_pages_available(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "research", "user_id": "darrow"}
    scope_ref = ScopeRef.from_dict(scope)
    page = RecordEnvelope.create(
        kind="knowledge_page",
        title="UUMit delivery benchmark paper",
        summary="A benchmark paper discussing UUMit delivery quality evaluation.",
        scope=scope_ref,
        source="eimemory.knowledge.compiler",
        meta={"page_type": "paper"},
    )
    runtime.store.append(page)

    bundle = runtime.memory.recall(
        query="UUMit delivery benchmark paper",
        scope=scope,
        task_context={"task_type": "research"},
        limit=5,
    )

    assert bundle.items
    assert bundle.items[0].record_id == page.record_id
    assert bundle.explanation["recall_intent"]["name"] == "research"
    assert "knowledge_page" in bundle.explanation["recall_intent"]["preferred_kinds"]
