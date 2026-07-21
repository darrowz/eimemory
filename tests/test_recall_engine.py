from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import (
    CandidateBatch,
    CandidateHit,
    CandidateRef,
    CandidateRequest,
    ExactScope,
)
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="workspace-a", user_id="user-a")


class FakeCandidateSource:
    name = "fake"

    def __init__(self, hits: tuple[CandidateHit, ...]) -> None:
        self.hits = hits
        self.requests: list[CandidateRequest] = []

    def search(self, request: CandidateRequest) -> CandidateBatch:
        self.requests.append(request)
        return CandidateBatch(
            hits=self.hits,
            diagnostics={
                "source_name": self.name,
                "candidate_count": len(self.hits),
                "candidate_limit": request.limit,
                "elapsed_ms": 0.1,
                "drops": {},
                "fallback": False,
                "fallback_reason": "",
                "policy_version": "sqlite-recall.v1",
            },
        )


class RecordingRecallEngine:
    def __init__(self, bundle) -> None:
        self.bundle = bundle
        self.requests: list[CandidateRequest] = []

    def recall(self, request: CandidateRequest):
        self.requests.append(request)
        return self.bundle


def _record(*, text: str, source_id: str = "alpha", status: str = "active", scope: ScopeRef = SCOPE) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind="memory",
        title=text,
        summary=text,
        content={"text": text, "memory_type": "fact"},
        scope=scope,
        source="test",
        source_id=source_id,
        meta={"memory_type": "fact", "quality": {"capture_decision": "accept", "salience_score": 0.9}},
    )
    record.status = status
    return record


def _hit(record: RecordEnvelope, *, rank: int = 1) -> CandidateHit:
    return CandidateHit(
        ref=CandidateRef(
            record_id=record.record_id,
            scope=ExactScope.from_scope(record.scope),
            source_id=record.source_id,
        ),
        source_rank=rank,
        source_score=1.0 / rank,
        component_hints=(("final_score", 1.0 / rank),),
        evidence_hints=("sqlite_hybrid",),
    )


def test_candidate_contracts_are_frozen_and_id_only() -> None:
    ref = CandidateRef(record_id="r1", scope=ExactScope.from_scope(SCOPE), source_id="alpha")
    hit = CandidateHit(ref=ref, source_rank=1, source_score=0.9)
    request = CandidateRequest(query="query", scope=ExactScope.from_scope(SCOPE), limit=3, budget=9)

    assert set(ref.__dataclass_fields__) == {"record_id", "scope", "source_id"}
    assert not hasattr(hit, "record")
    assert not hasattr(hit, "content")
    with pytest.raises(FrozenInstanceError):
        ref.record_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        request.limit = 4  # type: ignore[misc]


def test_candidate_request_round_trips_empty_nested_container_types() -> None:
    request = CandidateRequest.create(
        query="query",
        scope=SCOPE,
        task_context={"source_ids": [], "nested": {"empty_list": [], "empty_dict": {}}},
        recall_filters={"blocked_recall_lanes": [], "source_weights": {}},
    )

    assert request.task_context_dict() == {
        "source_ids": [],
        "nested": {"empty_list": [], "empty_dict": {}},
    }
    assert request.recall_filter_dict() == {"blocked_recall_lanes": [], "source_weights": {}}


def test_memory_api_is_thin_facade_over_explicit_engine_injection(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record(text="engine injection marker"))
    source = FakeCandidateSource((_hit(record),))
    engine = GovernedRecallEngine(store=store, candidate_source=source)
    memory = MemoryAPI(store, recall_engine=engine)

    bundle = memory.recall(
        query="engine injection marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )

    assert [item.record_id for item in bundle.items] == [record.record_id]
    assert source.requests
    assert source.requests[0].source_ids == ("alpha",)
    assert bundle.explanation["engine_diagnostics"]["engine_name"] == "governed"
    store.close()


def test_memory_api_facade_calls_injected_recall_engine_once_without_reordering(tmp_path) -> None:
    from eimemory.models.records import RecallBundle

    store = RuntimeStore(tmp_path)
    first = _record(text="facade first")
    second = _record(text="facade second")
    expected = RecallBundle(
        items=[second, first],
        rules=[],
        reflections=[],
        confidence=0.5,
        next_action_hint="keep-order",
        explanation={"owner": "injected"},
    )
    engine = RecordingRecallEngine(expected)
    memory = MemoryAPI(store, recall_engine=engine)

    actual = memory.recall(query=" facade query ", scope=asdict(SCOPE), limit=2)

    assert actual is expected
    assert [item.record_id for item in actual.items] == [second.record_id, first.record_id]
    assert len(engine.requests) == 1
    assert engine.requests[0].query == "facade query"
    store.close()


def test_engine_drops_cross_scope_cross_source_inactive_missing_and_corrupt_refs(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    valid = store.append(_record(text="strict hydration marker", source_id="alpha"))
    inactive = store.append(_record(text="strict hydration marker inactive", source_id="alpha", status="removed"))
    other_scope = store.append(
        _record(
            text="strict hydration marker other scope",
            source_id="alpha",
            scope=ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="other", user_id="user-a"),
        )
    )
    other_source = store.append(_record(text="strict hydration marker other source", source_id="beta"))
    corrupt = store.append(_record(text="strict hydration marker corrupt", source_id="alpha"))
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = '{bad' WHERE record_id = ? AND source_id = ?",
        (corrupt.record_id, corrupt.source_id),
    )
    store.sqlite.conn.commit()
    hits = (
        _hit(valid, rank=1),
        _hit(inactive, rank=2),
        CandidateHit(
            ref=CandidateRef(other_scope.record_id, ExactScope.from_scope(SCOPE), "alpha"),
            source_rank=3,
            source_score=0.3,
        ),
        CandidateHit(
            ref=CandidateRef(other_source.record_id, ExactScope.from_scope(SCOPE), "alpha"),
            source_rank=4,
            source_score=0.2,
        ),
        CandidateHit(
            ref=CandidateRef("missing", ExactScope.from_scope(SCOPE), "alpha"),
            source_rank=5,
            source_score=0.1,
        ),
        _hit(corrupt, rank=6),
    )
    source = FakeCandidateSource(hits)
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="strict hydration marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=10,
    )

    assert [item.record_id for item in bundle.items] == [valid.record_id]
    drops = bundle.explanation["engine_diagnostics"]["drops"]
    assert drops["inactive_record"] == 1
    assert drops["missing_or_corrupt_record"] == 4
    assert "strict hydration marker" not in repr(bundle.explanation["engine_diagnostics"])
    store.close()


def test_empty_source_allowlist_is_not_all_sources(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="empty allowlist marker", source_id="alpha"))
    memory = MemoryAPI(store)

    bundle = memory.recall(
        query="empty allowlist marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": []},
        limit=5,
    )

    assert bundle.items == []
    assert bundle.rules == []
    assert bundle.reflections == []
    assert bundle.explanation["engine_diagnostics"]["candidate_count"] == 0
    store.close()


def test_empty_source_allowlist_cannot_trigger_policy_gap_side_effect(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    rule = RecordEnvelope.create(
        kind="rule",
        title="open gap",
        summary="open gap",
        scope=SCOPE,
        source_id="beta",
        status="active",
        meta={
            "task_type": "recall.test",
            "retrieval_policy": {"open_unknown_on_low_confidence": True},
        },
    )
    store.append(rule)
    memory = MemoryAPI(store)

    bundle = memory.recall(
        query="missing source-isolated knowledge",
        scope=asdict(SCOPE),
        task_context={"task_type": "recall.test", "source_ids": []},
        limit=5,
    )

    assert bundle.items == []
    assert bundle.rules == []
    assert bundle.reflections == []
    assert store.list_records(kinds=["unknown"], scope=SCOPE) == []
    assert bundle.explanation["engine_diagnostics"]["fallback_reason"] == "empty_source_allowlist"
    store.close()


def test_default_sqlite_source_returns_only_refs_and_bounded_body_free_diagnostics(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    secret = "ZZBODYOPAQUE8675309"
    store.append(_record(text=f"candidate marker {secret}", source_id="alpha"))
    source = SQLiteCandidateSource(store)
    batch = source.search(
        CandidateRequest(
            query="candidate marker",
            scope=ExactScope.from_scope(SCOPE),
            kinds=("memory",),
            source_ids=("alpha",),
            limit=5,
            budget=15,
        )
    )

    assert batch.hits
    assert all(not hasattr(hit, "record") for hit in batch.hits)
    assert secret not in repr(batch)
    assert len(batch.hits) <= 5
    assert len(batch.diagnostics) <= 12
    assert batch.diagnostic_dict()["candidate_limit"] <= 15
    store.close()


def test_two_default_memory_apis_do_not_share_engine_or_source_state(tmp_path) -> None:
    left = MemoryAPI(RuntimeStore(tmp_path / "left"))
    right = MemoryAPI(RuntimeStore(tmp_path / "right"))
    try:
        assert left.recall_engine is not right.recall_engine
        assert left.recall_engine.candidate_source is not right.recall_engine.candidate_source
    finally:
        left.store.close()
        right.store.close()


def test_runtime_exposes_explicit_candidate_source_injection(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record(text="runtime source injection"))
    source = FakeCandidateSource((_hit(record),))
    runtime = Runtime(store, candidate_source=source)
    try:
        bundle = runtime.memory.recall(
            query="runtime source injection",
            scope=asdict(SCOPE),
            task_context={"source_ids": ["alpha"]},
            limit=1,
        )
        assert [item.record_id for item in bundle.items] == [record.record_id]
        assert runtime.memory.recall_engine.candidate_source is source
    finally:
        runtime.close()


def test_exact_candidate_ref_wins_over_newer_hongtu_alias_and_global_user(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    canonical_scope = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    canonical = _record(text="canonical exact marker", scope=canonical_scope)
    canonical.record_id = "shared_exact_record"
    store.append(canonical)
    legacy = _record(
        text="legacy alias must not replace canonical",
        scope=ScopeRef(tenant_id="default", agent_id="main", workspace_id="repo-x", user_id="darrow"),
    )
    legacy.record_id = canonical.record_id
    legacy.time.updated_at = "2099-01-01T00:00:00+00:00"
    store.append(legacy)
    global_user = _record(
        text="global user must not replace canonical",
        scope=ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id=""),
    )
    global_user.record_id = canonical.record_id
    global_user.time.updated_at = "2099-01-02T00:00:00+00:00"
    store.append(global_user)
    source = FakeCandidateSource((_hit(canonical),))
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="canonical exact marker",
        scope=asdict(canonical_scope),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )

    assert [item.summary for item in bundle.items] == ["canonical exact marker"]
    store.close()


def test_graph_does_not_revive_removed_or_cross_source_records(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    removed = _record(text="removed graph secret", source_id="alpha", status="removed")
    beta = _record(text="beta graph secret", source_id="beta")
    base = _record(text="active graph anchor", source_id="alpha")
    base.links = [
        LinkRef(relation="related", target_kind="memory", target_id=removed.record_id),
        LinkRef(relation="related", target_kind="memory", target_id=beta.record_id),
    ]
    store.append(removed)
    store.append(beta)
    store.append(base)
    source = FakeCandidateSource((_hit(base),))
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="active graph anchor",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "recall_profile": "exploratory"},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [base.record_id]
    store.close()


def test_report_rule_reflection_and_usage_paths_obey_source_allowlist(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    first = store.append(_record(text="source governed first", source_id="alpha"))
    second = store.append(_record(text="source governed second", source_id="alpha"))
    report = RecordEnvelope.create(
        kind="reflection",
        title="beta report",
        summary="beta report secret",
        content={"report_type": "rule_evolution"},
        scope=SCOPE,
        source="eimemory.rule_evolution_loop",
        source_id="beta",
        meta={"report_type": "rule_evolution"},
    )
    report.record_id = "rule_evolution_beta_secret"
    store.append(report)
    beta_rule = RecordEnvelope.create(
        kind="rule",
        title="source governed",
        summary="beta rule secret",
        content={"text": "source governed"},
        scope=SCOPE,
        source="test",
        source_id="beta",
        meta={"memory_type": "preference"},
    )
    store.append(beta_rule)
    feedback = RecordEnvelope.create(
        kind="feedback",
        title="beta usage",
        summary="beta usage",
        content={
            "report_type": "memory_usage_telemetry",
            "used_record_ids": [second.record_id],
            "rejected_record_ids": [],
        },
        scope=SCOPE,
        source="test",
        source_id="beta",
        meta={"report_type": "memory_usage_telemetry"},
    )
    store.append(feedback)
    source = FakeCandidateSource((_hit(first, rank=1), _hit(second, rank=2)))
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="rule_evolution_beta_secret source governed",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=5,
    )

    assert [item.record_id for item in bundle.items] == [first.record_id, second.record_id]
    assert bundle.rules == []
    assert bundle.reflections == []
    assert bundle.explanation["memory_telemetry"]["applied"] is False
    store.close()


def test_raw_hybrid_rechecks_active_exact_scope_and_source(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    alpha = RecordEnvelope.create(
        kind="raw_chunk",
        title="raw isolation marker alpha",
        summary="raw isolation marker alpha",
        content={"raw_text": "raw isolation marker alpha"},
        scope=SCOPE,
        source="raw",
        source_id="alpha",
    )
    beta = RecordEnvelope.create(
        kind="raw_chunk",
        title="raw isolation marker beta",
        summary="raw isolation marker beta",
        content={"raw_text": "raw isolation marker beta"},
        scope=SCOPE,
        source="raw",
        source_id="beta",
    )
    removed = RecordEnvelope.create(
        kind="raw_chunk",
        title="raw isolation marker removed",
        summary="raw isolation marker removed",
        content={"raw_text": "raw isolation marker removed"},
        scope=SCOPE,
        source="raw",
        source_id="alpha",
    )
    removed.status = "removed"
    for item in (alpha, beta, removed):
        store.append(item)
    memory = MemoryAPI(store)

    bundle = memory.recall(
        query="raw isolation marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "recall_mode": "raw_hybrid"},
        limit=5,
    )

    raw_ids = [str(item.get("record", {}).get("record_id") or "") for item in bundle.explanation["raw_evidence"]]
    assert raw_ids == [alpha.record_id]
    store.close()


def test_raw_evidence_cannot_reuse_allowed_id_with_cross_source_body(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    alpha = _record(text="authoritative alpha raw", source_id="alpha")
    alpha.record_id = "shared_raw_id"
    store.append(alpha)

    def forged_raw(*args, **kwargs):
        return [
            {
                "record": {
                    "record_id": alpha.record_id,
                    "source_id": "beta",
                    "scope": asdict(SCOPE),
                    "text": "FORGED-BETA-BODY",
                },
                "final_score": 1.0,
            }
        ]

    monkeypatch.setattr("eimemory.retrieval.engine.search_raw_chunks", forged_raw)
    memory = MemoryAPI(store)

    bundle = memory.recall(
        query="authoritative alpha raw",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "recall_mode": "raw_hybrid"},
        limit=5,
    )

    assert bundle.explanation["raw_evidence"] == []
    assert bundle.explanation["engine_diagnostics"]["drops"]["raw_source_not_allowed"] == 1
    store.close()


def test_projection_payload_scope_or_source_mismatch_fails_closed(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record(text="projection mismatch marker", source_id="alpha"))
    payload = record.to_dict()
    payload["source_id"] = "beta"
    import json

    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? WHERE record_id = ? AND source_id = ?",
        (json.dumps(payload), record.record_id, "alpha"),
    )
    store.sqlite.conn.commit()
    source = FakeCandidateSource((_hit(record),))
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="projection mismatch marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=5,
    )

    assert bundle.items == []
    assert bundle.explanation["engine_diagnostics"]["drops"]["ref_mismatch"] == 1
    store.close()


def test_default_engine_golden_bundle_preserves_local_rank_and_explanation_shape(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    older = store.append(_record(text="golden recall exact first detail", source_id="alpha"))
    newer = store.append(_record(text="golden recall partial", source_id="alpha"))
    memory = MemoryAPI(store)

    bundle = memory.recall(
        query="golden recall exact first detail",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "recall_profile": "balanced"},
        limit=2,
    )

    assert [item.record_id for item in bundle.items] == [older.record_id, newer.record_id]
    assert bundle.confidence == 0.81
    assert bundle.next_action_hint == older.title.lower()
    assert bundle.explanation["recall_profile"] == "balanced"
    assert bundle.explanation["pipeline"]["phase_names"] == [
        "prepare",
        "retrieve",
        "graph_expand",
        "score_filter",
        "package",
    ]
    assert [item["record_id"] for item in bundle.explanation["scoring"]] == [older.record_id, newer.record_id]
    store.close()
