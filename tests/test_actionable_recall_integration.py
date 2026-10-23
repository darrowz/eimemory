from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.memory_edges import MemoryEdge
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
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Operational page: Research digest 2026-05-27",
            summary="Recent papers: SIREN and PRISM recommendation research. Notable claim mentions delivery quality.",
            scope=scope_ref,
            source="eimemory.knowledge.projectors",
            meta={
                "projection_type": "operational_knowledge",
                "source_record_id": "digest-test",
                "memory_type": "fact",
            },
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
    assert all("Operational page: Research digest" not in item.title for item in bundle.items)
    assert bundle.explanation["recall_intent"]["name"] == "project_delivery"
    assert bundle.explanation["recall_intent"]["memory_cube"] == "project"


def test_recall_uses_causal_graph_route_and_evidence_refs(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "ops", "user_id": "darrow"}
    scope_ref = ScopeRef.from_dict(scope)
    cause = runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rollback ledger detail",
            summary="rollback_plan was empty and the rollback command could not run.",
            scope=scope_ref,
            source="test",
            meta={"service": "eimemory-rpc"},
        )
    )
    symptom = runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="eimemory-rpc deploy failed",
            summary="The deployment failed after the 8091 health check.",
            scope=scope_ref,
            source="test",
            meta={"service": "eimemory-rpc", "commit_sha": "abc123", "release_path": "/opt/eimemory/releases/abc123"},
        )
    )
    edge = runtime.store.upsert_memory_edge(
        MemoryEdge.create(
            from_id=cause.record_id,
            to_id=symptom.record_id,
            edge_type="causal",
            confidence=0.9,
            evidence_id=symptom.record_id,
            scope=scope_ref,
            reason="test_causal_link",
        )
    )

    bundle = runtime.memory.recall(
        query="为什么 eimemory-rpc 部署失败",
        scope=scope,
        task_context={"task_type": "cli.recall"},
        limit=5,
    )

    item_ids = [item.record_id for item in bundle.items]
    assert symptom.record_id in item_ids
    assert cause.record_id in item_ids
    assert bundle.explanation["graph_route"]["primary"] == "causal"
    assert "causal" in bundle.explanation["graph_route"]["edge_types"]
    cause_ref = next(ref for ref in bundle.explanation["evidence_refs"] if ref["record_id"] == cause.record_id)
    symptom_ref = next(ref for ref in bundle.explanation["evidence_refs"] if ref["record_id"] == symptom.record_id)
    assert edge.edge_id in cause_ref["edge_ids"]
    assert symptom_ref["commit_sha"] == "abc123"
    assert symptom_ref["release_path"] == "/opt/eimemory/releases/abc123"
    assert bundle.explanation["timeline"]


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


def test_project_delivery_filters_operational_digest_projections_before_ranking(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "delivery", "user_id": "darrow"}
    scope_ref = ScopeRef.from_dict(scope)
    project_memory = runtime.memory.ingest(
        text="以后外部订单先对需求清单逐条验收，再交付。",
        memory_type="preference",
        title="外部订单交付验收规则",
        scope=scope,
        source="operator.correction",
        force_capture=True,
    )
    for index in range(20):
        runtime.store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"Operational page: Research digest 2026-05-{index + 1:02d}",
                summary="UUMit 交付品质 海报 v2 research digest projection.",
                scope=scope_ref,
                source="eimemory.knowledge.projectors",
                meta={
                    "projection_type": "operational_knowledge",
                    "source_record_id": f"digest-{index}",
                    "memory_type": "fact",
                },
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
    assert all("Operational page: Research digest" not in item.title for item in bundle.items)
    assert "operational_knowledge" in bundle.explanation["recall_filters"]["blocked_projection_types"]


def test_procedure_query_prefers_durable_delivery_over_episodic(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongrui", "workspace_id": "embodied", "user_id": "darrow"}
    preference = runtime.memory.ingest(
        text="提交部署验收；分层业务验收；真实读回版本、收据、健康与召回。",
        memory_type="preference",
        title="提交部署验收偏好",
        scope=scope,
        source="operator.correction",
        force_capture=True,
    )
    runtime.memory.ingest(
        text="昨晚部署切换时日志里出现过 transient vector sync lag，稍后恢复。",
        memory_type="episodic",
        title="昨晚部署切换日志",
        scope=scope,
        source="agent.session",
        force_capture=True,
    )
    soft = runtime.memory.ingest(
        text="明确要求暂停时，停止无关修复、部署和评估，集中完成当前索引构建并以实际状态验收。",
        memory_type="preference",
        title="暂停时以实际状态验收",
        scope=scope,
        source="operator.correction",
        force_capture=True,
    )

    bundle = runtime.memory.recall(
        query="拉起最新eimemory源码，部署切换，跑一遍nightly并验收",
        scope=scope,
        task_context={"task_type": "cli.recall", "recall_profile": "precision"},
        limit=5,
    )

    assert bundle.items
    assert bundle.items[0].record_id == preference.record_id
    assert soft.record_id not in {item.record_id for item in bundle.items[:1]}
