from __future__ import annotations

from dataclasses import asdict
import json

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.cli.main import _build_parser, _dispatch_recall
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.recall import classify_recall_intent
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")


@pytest.mark.parametrize(
    "query",
    [
        "部署失败后的根因",
        "线上事故怎么回滚",
        "发布后服务不可用",
        "production deployment outage",
        "incident root cause and rollback",
    ],
)
def test_operational_issue_intent_requires_bounded_deployment_or_incident_cues(query: str) -> None:
    assert classify_recall_intent(query).name == "operational_issue"


@pytest.mark.parametrize("query", ["问题", "这个问题怎么办", "question"])
def test_bare_problem_language_does_not_enable_operational_recall(query: str) -> None:
    assert classify_recall_intent(query).name != "operational_issue"
    assert MemoryAPI._allows_operational_recall(query, {}) is False


def test_operational_issue_intent_and_memory_permission_share_the_same_boundary() -> None:
    intent = classify_recall_intent("部署失败后的根因")

    assert intent.name == "operational_issue"
    assert MemoryAPI._allows_operational_recall("部署失败后的根因", {}) is True
    assert MemoryAPI._allows_operational_recall("问题", {"intent": "operational_issue"}) is False
    assert MemoryAPI._allows_operational_recall("问题", {"include_operational_recall": True}) is True


def test_intent_lane_policy_suppresses_knowledge_and_news_but_keeps_research() -> None:
    preference = classify_recall_intent("沟通风格偏好")
    project = classify_recall_intent("UUMit 交付品质")
    operational = classify_recall_intent("部署失败后的根因")
    research = classify_recall_intent("Graphiti temporal knowledge graph 论文")

    for intent in (preference, project, operational):
        assert "knowledge_page" in intent.suppressed_kinds
        assert "news" in intent.suppressed_kinds
    assert "knowledge_page" not in research.suppressed_kinds
    assert "news" not in research.suppressed_kinds


def test_explicit_recall_lane_boundary_overrides_intent_kind_suppression(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    page = store.append(
        RecordEnvelope.create(
            kind="knowledge_page",
            title="UUMit delivery benchmark paper",
            summary="UUMit delivery benchmark paper",
            content={"text": "UUMit delivery benchmark paper"},
            scope=SCOPE,
            source_id="research",
            source="external.research",
            meta={"page_type": "paper"},
        )
    )

    bundle = MemoryAPI(store).recall(
        query="UUMit delivery benchmark paper",
        scope=asdict(SCOPE),
        task_context={
            "source_ids": ["research"],
            "allowed_recall_lanes": ["external_knowledge"],
        },
        limit=3,
    )

    assert [item.record_id for item in bundle.items] == [page.record_id]
    store.close()


def test_scope_strategy_exact_canonical_first_and_legacy_union_are_explicit(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    canonical = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="canonical scope strategy marker",
            summary="canonical scope strategy marker",
            content={"text": "canonical scope strategy marker"},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )
    legacy_scope = ScopeRef(agent_id="main", workspace_id="repo-x", user_id="darrow")
    legacy = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="legacy-only-token scope strategy marker",
            summary="legacy-only-token scope strategy marker",
            content={"text": "legacy-only-token scope strategy marker"},
            scope=legacy_scope,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )
    memory = MemoryAPI(store)

    exact = memory.recall(
        query="scope strategy marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "scope_strategy": "exact"},
        limit=5,
    )
    union = memory.recall(
        query="scope strategy marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "scope_strategy": "legacy_union"},
        limit=5,
    )

    assert [item.record_id for item in exact.items] == [canonical.record_id]
    assert {item.record_id for item in union.items} == {canonical.record_id, legacy.record_id}

    canonical_first_canonical = memory.recall(
        query="canonical scope strategy marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "scope_strategy": "canonical_first"},
        limit=5,
    )
    assert [item.record_id for item in canonical_first_canonical.items] == [canonical.record_id]
    assert canonical_first_canonical.explanation["scope_fallback"] == "not_needed"

    canonical_first_hit = memory.recall(
        query="legacy-only-token",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "scope_strategy": "canonical_first"},
        limit=5,
    )
    assert canonical_first_hit.explanation["scope_strategy"] == "canonical_first"
    assert canonical_first_hit.explanation["scope_fallback"] == "legacy_union"
    store.close()


def test_chinese_operational_exact_query_drops_deployment_distractor(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    root_cause = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="部署失败根因与回滚",
            summary="部署失败根因与回滚",
            content={"text": "部署失败根因与回滚"},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="部署失败关键词噪声",
            summary="部署失败关键词噪声",
            content={"text": "部署失败关键词噪声"},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )

    bundle = MemoryAPI(store).recall(
        query="部署失败根因与回滚",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "scope_strategy": "exact"},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [root_cause.record_id]
    assert bundle.explanation["recall_intent"]["name"] == "operational_issue"
    store.close()


def test_recall_selector_preserves_order_filters_distractors_and_does_not_pad(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    exact = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Exact deployment rule",
            summary="Exact deployment rule",
            content={"text": "Exact deployment rule"},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Unrelated deployment distractor",
            summary="A distractor with only a weak deployment mention.",
            content={"text": "A distractor with only a weak deployment mention."},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )

    bundle = MemoryAPI(store).recall(
        query="Exact deployment rule",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "scope_strategy": "exact"},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [exact.record_id]
    selector = bundle.explanation["relevance_selector"]
    assert selector["thresholds"]["non_exact_min_grounding"] > 0
    assert selector["padding"] is False
    store.close()


def test_recall_selector_preserves_authorized_duplicate_exact_identities(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    duplicates = []
    for source_id in ("alpha", "beta"):
        duplicates.append(
            store.append(
                RecordEnvelope.create(
                    kind="memory",
                    title="Duplicate exact identity",
                    summary="Duplicate exact identity",
                    content={"text": "Duplicate exact identity"},
                    scope=SCOPE,
                    source_id=source_id,
                    meta={"memory_type": "fact", "force_capture": True},
                )
            )
        )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Duplicate exact distractor",
            summary="A weaker duplicate distractor",
            content={"text": "A weaker duplicate distractor"},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )

    bundle = MemoryAPI(store).recall(
        query="Duplicate exact identity",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha", "beta"], "scope_strategy": "exact"},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [item.record_id for item in duplicates]
    assert bundle.explanation["relevance_selector"]["authorized_exact_duplicates"] == 2
    store.close()


def test_canonical_first_exact_identity_dominates_lower_generic_tail(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    exact = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="小马哥自主模型路由主任务",
            summary="小马哥自主模型路由主任务",
            content={"text": "小马哥自主模型路由主任务"},
            scope=SCOPE,
            source_id="default",
            meta={"memory_type": "fact", "force_capture": True},
        )
    )
    for index in range(4):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"模型路由研究项 {index}",
                summary="与主任务无关的研究材料",
                content={"text": "模型路由研究材料与其他主任务"},
                scope=SCOPE,
                source_id="default",
                meta={"memory_type": "fact", "force_capture": True},
            )
        )

    bundle = MemoryAPI(store).recall(
        query="小马哥自主模型路由主任务",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["default"], "scope_strategy": "canonical_first"},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [exact.record_id]
    selector = bundle.explanation["relevance_selector"]
    assert selector["safe_exact_dominance"] is True
    assert selector["thresholds"]["top_score_margin"] == pytest.approx(0.15)
    assert selector["thresholds"]["non_exact_min_score"] == pytest.approx(0.18)
    store.close()


def test_generic_query_recalls_strongly_lexical_durable_event_without_operational_pollution(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    event = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="龙海区政府算力中心沟通",
            summary="2026-08-23早上去龙海区政府沟通算力中心事项",
            content={"text": "2026-08-23早上去龙海区政府沟通算力中心事项", "memory_type": "event"},
            scope=SCOPE,
            source_id="default",
            source="cli",
            meta={"memory_type": "event", "force_capture": True},
        )
    )
    for memory_type in ("run_log", "audit_record", "incident_report", "evolution_artifact"):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"龙海区政府算力中心 {memory_type}",
                summary="operational pollution",
                content={"text": "龙海区政府算力中心 operational pollution", "memory_type": memory_type},
                scope=SCOPE,
                source_id="default",
                meta={"memory_type": memory_type, "force_capture": True},
            )
        )

    bundle = MemoryAPI(store).recall(
        query="龙海区政府算力中心",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["default"], "scope_strategy": "canonical_first"},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [event.record_id]
    assert set(bundle.explanation["recall_filters"]["blocked_recall_lanes"]) >= {
        "run_log",
        "audit_record",
        "incident_report",
        "evolution_artifact",
    }
    store.close()


def test_recall_bundle_compact_schema_is_bounded_and_to_dict_is_unchanged() -> None:
    records = [
        RecordEnvelope.create(
            kind="memory",
            title=f"compact {index}",
            summary="s" * 5000,
            detail="d" * 5000,
            content={"text": "t" * 10_000, "nested": {"heavy": ["x"] * 100}},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "quality": {"score": 0.9}, "living": {"large": "x" * 1000}},
        )
        for index in range(5)
    ]
    bundle = RecallBundle(
        items=records,
        rules=[],
        reflections=[],
        confidence=0.9,
        next_action_hint="compact",
        explanation={
            "scoring": [{"record_id": item.record_id, "living": {"x": "y"}} for item in records],
            "fusion": {"selected": records[0].to_dict()},
            "query_scopes": [asdict(SCOPE)] * 64,
            "recall_scope_aliases": ["legacy"] * 64,
            "recall_intent": {"name": "generic"},
        },
    )
    full = bundle.to_dict()
    compact_top_1 = bundle.to_compact_dict(limit=1)
    compact_top_5 = bundle.to_compact_dict(limit=5, include_explanation=True)

    assert full["items"][0]["detail"] == "d" * 5000
    assert compact_top_1["schema_version"] == "recall_bundle.compact.v1"
    assert len(json.dumps(compact_top_1, ensure_ascii=False).encode("utf-8")) <= 4_096
    assert len(json.dumps(compact_top_5, ensure_ascii=False).encode("utf-8")) <= 16_384
    compact_json = json.dumps(compact_top_5, ensure_ascii=False)
    assert '"scoring"' not in compact_json
    assert '"fusion"' not in compact_json
    assert '"query_scopes"' not in compact_json
    assert '"recall_scope_aliases"' not in compact_json
    assert compact_top_5["explanation"]["recall_intent"]["name"] == "generic"


def test_named_selector_can_be_used_without_padding_for_graph_and_anchor_tails() -> None:
    assert hasattr(GovernedRecallEngine, "_select_post_fusion_items")


def test_graph_and_anchor_tails_use_the_same_grounding_gate(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    engine = GovernedRecallEngine(store=store, candidate_source=object())
    records = [
        RecordEnvelope.create(
            kind="memory",
            title=title,
            summary=title,
            content={"text": title},
            scope=SCOPE,
            source_id="alpha",
            meta={"memory_type": "fact", "force_capture": True},
        )
        for title in (
            "deployment rollback anchor",
            "deployment rollback health check",
            "deployment rollback runbook",
            "unrelated recipe anchor",
        )
    ]
    refs = [engine._record_key(record) for record in records]
    state = {
        "detail_by_ref": {
            refs[0]: {"score": 0.80},
            refs[1]: {"score": 0.01},
            refs[2]: {"score": 0.65},
            refs[3]: {"score": 0.69},
        },
        "evidence_by_ref": {
            refs[0]: {"keyword_exact"},
            refs[1]: {"graph_path"},
            refs[2]: {"graph_path"},
            refs[3]: {"graph_path"},
        },
        "pool_members": {ref: [record] for ref, record in zip(refs, records)},
        "graph_expanded_refs": {refs[1]},
    }
    hints = {
        refs[0]: {"lexical_score": 0.20},
        refs[1]: {"lexical_score": 0.18, "candidate_sources": ["graph"]},
        refs[2]: {"semantic_score": 0.20, "vector_score": 0.40, "candidate_sources": ["anchor"]},
        refs[3]: {"candidate_sources": ["anchor"]},
    }

    selected, selector = engine._select_post_fusion_items(
        items=records,
        query="deployment rollback",
        limit=4,
        fusion_state=state,
        component_hints_by_ref=hints,
    )

    assert [item.record_id for item in selected] == [record.record_id for record in records[:3]]
    assert selector["preserved_fused_order"] is True
    assert selector["padding"] is False
    assert selector["dropped_reasons"]["grounding"] == 1
    store.close()


def test_cli_compact_is_canonical_first_and_full_output_warns_without_breaking_json(capsys) -> None:
    class FakeMemory:
        def __init__(self) -> None:
            self.calls = []

        def recall(self, **kwargs):
            self.calls.append(kwargs)
            return RecallBundle(
                items=[],
                rules=[],
                reflections=[],
                confidence=0.0,
                next_action_hint="",
                explanation={"scope_strategy": kwargs["task_context"].get("scope_strategy")},
            )

    class FakeRuntime:
        def __init__(self) -> None:
            self.memory = FakeMemory()

    runtime = FakeRuntime()
    compact_args = _build_parser().parse_args(
        ["recall", "deployment failed", "--compact", "--explain", "--limit", "3"]
    )
    assert _dispatch_recall(compact_args, runtime, asdict(SCOPE)) == 0
    compact_output = json.loads(capsys.readouterr().out)
    assert compact_output["schema_version"] == "recall_bundle.compact.v1"
    assert compact_output["explanation"]["scope_strategy"] == "canonical_first"
    assert runtime.memory.calls[-1]["limit"] == 3

    full_args = _build_parser().parse_args(["recall", "deployment failed"])
    assert _dispatch_recall(full_args, runtime, asdict(SCOPE)) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["items"] == []
    assert "deprecated" in captured.err.lower()
